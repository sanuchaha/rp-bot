"""Telegram-бот: трекер состояния персонажей для RP и текстовых боёв.

Возможности:
- Несколько персонажей на одного юзера (до 5), у каждого имя, класс, max/current HP.
- /persona — меню управления (список, выбор активного, добавление, редактирование HP, удаление).
- /start — для нового юзера запускает регистрацию, для существующего открывает /persona.
- /fif — управление активным персонажем в бою (кнопки урон / лечение).
- /duel @username (или /duel в ответ на сообщение) — дуэль в группах.
- ХП НЕ восстанавливается автоматически после боя — сохраняется в карточке.
- Сохранение состояния в data.json (переживает перезапуск).
"""

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    Update,
    User,
)

from db import Storage, build_storage


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

MAX_CHARS_PER_USER = 5
MAX_NAME_LEN = 20


def _parse_chat_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            result.add(int(piece))
        except ValueError:
            logging.warning("ALLOWED_CHAT_IDS: пропускаю '%s' — не int", piece)
    return result


ALLOWED_CHAT_IDS: set[int] = _parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------


class CharClass(str, Enum):
    ATTACKER = "attacker"
    HEALER = "healer"


CLASS_LABELS: dict[CharClass, str] = {
    CharClass.ATTACKER: "⚔️ Атакующий (Боец)",
    CharClass.HEALER: "🌿 Лекарь (Медик)",
}

# Проценты от максимального ХП по классу.
CLASS_STATS: dict[CharClass, dict[str, int]] = {
    CharClass.ATTACKER: {"damage_pct": 5, "heal_pct": 5},
    CharClass.HEALER: {"damage_pct": 15, "heal_pct": 15},
}


@dataclass
class Character:
    name: str
    char_class: CharClass
    max_hp: int
    current_hp: int
    in_battle: bool = False  # эфемерный флаг — сбрасывается при рестарте


class DuelStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    FINISHED = "finished"


@dataclass
class Duel:
    duel_id: int
    chat_id: int
    initiator_id: int
    opponent_id: int
    initiator_char: str  # активный персонаж инициатора на момент дуэли
    opponent_char: str
    status: DuelStatus = DuelStatus.PENDING
    message_id: Optional[int] = None


# In-memory хранилище.
characters: dict[int, dict[str, Character]] = {}  # user_id -> {name -> Character}
active_char: dict[int, str] = {}                  # user_id -> name активного
duels: dict[int, Duel] = {}
user_duel: dict[int, int] = {}                    # user_id -> duel_id
username_to_id: dict[str, int] = {}               # username (lower) -> user_id
user_display: dict[int, str] = {}                 # user_id -> отображаемое имя

_next_duel_id: int = 1
BOT_USERNAME: Optional[str] = None

# Глобальное хранилище — инициализируется в main().
STORAGE: Optional[Storage] = None
_save_lock: Optional[asyncio.Lock] = None
_save_task: Optional[asyncio.Task[None]] = None
_save_pending: bool = False


def make_duel_id() -> int:
    global _next_duel_id
    duel_id = _next_duel_id
    _next_duel_id += 1
    return duel_id


# ---------------------------------------------------------------------------
# Хелперы для работы с персонажами
# ---------------------------------------------------------------------------


def get_chars(user_id: int) -> dict[str, Character]:
    return characters.get(user_id, {})


def char_list(user_id: int) -> list[Character]:
    return list(get_chars(user_id).values())


def char_by_idx(user_id: int, idx: int) -> Optional[Character]:
    chars = char_list(user_id)
    if 0 <= idx < len(chars):
        return chars[idx]
    return None


def char_idx(user_id: int, name: str) -> Optional[int]:
    chars = char_list(user_id)
    for i, ch in enumerate(chars):
        if ch.name == name:
            return i
    return None


def get_active(user_id: int) -> Optional[Character]:
    name = active_char.get(user_id)
    if name is None:
        return None
    return get_chars(user_id).get(name)


def remember_user(user: Optional[User]) -> None:
    if user is None or user.is_bot:
        return
    if user.username:
        username_to_id[user.username.lower()] = user.id
        user_display[user.id] = "@" + user.username
    else:
        user_display[user.id] = user.first_name or f"id{user.id}"
    save_state()


def display_name(user_id: int) -> str:
    return user_display.get(user_id, f"id{user_id}")


# ---------------------------------------------------------------------------
# Персистентное хранилище (Postgres / JSON-файл)
# ---------------------------------------------------------------------------


def _serialize_state() -> dict[str, Any]:
    return {
        "characters": {
            str(uid): {
                name: {
                    "name": ch.name,
                    "char_class": ch.char_class.value,
                    "max_hp": ch.max_hp,
                    "current_hp": ch.current_hp,
                }
                for name, ch in chars.items()
            }
            for uid, chars in characters.items()
        },
        "active_char": {str(uid): name for uid, name in active_char.items()},
        "username_to_id": username_to_id,
        "user_display": {str(uid): name for uid, name in user_display.items()},
    }


async def _do_save() -> None:
    """Дебаунсинг: сохраняем максимум одно состояние за раз, повторяя если нужно."""
    global _save_pending
    if STORAGE is None or _save_lock is None:
        return
    while True:
        _save_pending = False
        snapshot = _serialize_state()
        try:
            async with _save_lock:
                await STORAGE.save(snapshot)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Не удалось сохранить состояние: %s", exc)
        if not _save_pending:
            return


def save_state() -> None:
    """Планируем асинхронное сохранение состояния (вызывается из синк-кода)."""
    global _save_task, _save_pending
    if STORAGE is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Нет работающего event loop — например, при загрузке тестов.
        return
    if _save_task is not None and not _save_task.done():
        _save_pending = True
        return
    _save_task = loop.create_task(_do_save())


async def load_state() -> None:
    if STORAGE is None:
        return
    try:
        data = await STORAGE.load()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Не удалось загрузить состояние: %s", exc)
        return
    if not data:
        return

    chars_data = data.get("characters", {})
    for uid_str, chars in chars_data.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        char_dict: dict[str, Character] = {}
        for name, ch_data in chars.items():
            try:
                ch = Character(
                    name=ch_data.get("name", name),
                    char_class=CharClass(ch_data["char_class"]),
                    max_hp=int(ch_data["max_hp"]),
                    current_hp=int(ch_data["current_hp"]),
                    in_battle=False,  # эфемерный флаг
                )
            except (KeyError, ValueError):
                continue
            char_dict[name] = ch
        if char_dict:
            characters[uid] = char_dict

    for uid_str, name in data.get("active_char", {}).items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        if uid in characters and name in characters[uid]:
            active_char[uid] = name

    for uname, uid in data.get("username_to_id", {}).items():
        try:
            username_to_id[uname] = int(uid)
        except (ValueError, TypeError):
            continue

    for uid_str, name in data.get("user_display", {}).items():
        try:
            user_display[int(uid_str)] = name
        except (ValueError, TypeError):
            continue


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------


class RegState(StatesGroup):
    waiting_for_name = State()
    waiting_for_class = State()
    waiting_for_max_hp = State()


class EditState(StatesGroup):
    waiting_for_hp = State()


router = Router()


# ---------------------------------------------------------------------------
# Inline-клавиатуры
# ---------------------------------------------------------------------------


def class_choice_kb(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CLASS_LABELS[CharClass.ATTACKER], callback_data=f"class:attacker:{owner_id}")],
            [InlineKeyboardButton(text=CLASS_LABELS[CharClass.HEALER], callback_data=f"class:healer:{owner_id}")],
        ]
    )


def join_battle_kb(owner_id: int, char_idx_val: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🗡 Присоединиться к битве",
                callback_data=f"battle:join:{char_idx_val}:{owner_id}",
            )]
        ]
    )


def battle_actions_kb(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🩸 Получить урон", callback_data=f"hp:damage:{owner_id}")],
            [InlineKeyboardButton(text="🩹 Получить лечение", callback_data=f"hp:heal:{owner_id}")],
        ]
    )


def duel_invite_kb(duel_id: int, opponent_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"duel:accept:{duel_id}:{opponent_id}"),
                InlineKeyboardButton(text="❌ Отказаться", callback_data=f"duel:decline:{duel_id}:{opponent_id}"),
            ]
        ]
    )


def persona_main_kb(owner_id: int) -> InlineKeyboardMarkup:
    chars = char_list(owner_id)
    rows: list[list[InlineKeyboardButton]] = []
    active_name = active_char.get(owner_id)
    for i, ch in enumerate(chars):
        prefix = "🎭 " if ch.name == active_name else ""
        suffix = "  💀" if ch.current_hp <= 0 else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{prefix}{ch.name} ({ch.current_hp}/{ch.max_hp}){suffix}",
                callback_data=f"persona:open:{i}:{owner_id}",
            )
        ])
    if len(chars) < MAX_CHARS_PER_USER:
        rows.append([
            InlineKeyboardButton(
                text="➕ Добавить персонажа",
                callback_data=f"persona:add:{owner_id}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def persona_char_kb(owner_id: int, idx: int, ch: Character) -> InlineKeyboardMarkup:
    is_active = active_char.get(owner_id) == ch.name
    rows: list[list[InlineKeyboardButton]] = []

    if not is_active:
        rows.append([InlineKeyboardButton(
            text="🎭 Сделать активным",
            callback_data=f"persona:setactive:{idx}:{owner_id}",
        )])

    if not ch.in_battle and ch.current_hp > 0:
        rows.append([InlineKeyboardButton(
            text="🗡 В битву",
            callback_data=f"battle:join:{idx}:{owner_id}",
        )])

    rows.append([InlineKeyboardButton(
        text="✏️ Изменить ХП",
        callback_data=f"persona:edithp:{idx}:{owner_id}",
    )])
    rows.append([InlineKeyboardButton(
        text="🗑 Удалить",
        callback_data=f"persona:delete:{idx}:{owner_id}",
    )])
    rows.append([InlineKeyboardButton(
        text="↩ Назад к списку",
        callback_data=f"persona:back:{owner_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def persona_delete_confirm_kb(owner_id: int, idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🗑 Да, удалить",
                callback_data=f"persona:delconfirm:{idx}:{owner_id}",
            ),
            InlineKeyboardButton(
                text="↩ Отмена",
                callback_data=f"persona:open:{idx}:{owner_id}",
            ),
        ]]
    )


def _parse_owner(cb_data: str) -> Optional[int]:
    """owner_id всегда в последнем сегменте."""
    parts = cb_data.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[-1])
    except ValueError:
        return None


async def _ensure_owner(cb: CallbackQuery) -> bool:
    owner_id = _parse_owner(cb.data or "")
    if owner_id is None:
        await cb.answer("Некорректные данные кнопки", show_alert=True)
        return False
    if cb.from_user.id != owner_id:
        await cb.answer("Это не твой персонаж 🙅", show_alert=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Рендеринг сообщений
# ---------------------------------------------------------------------------


def render_char_sheet(ch: Character, is_active: bool = False) -> str:
    status = "💀 Без сознания" if ch.current_hp <= 0 else "❤️ Жив"
    active_marker = " 🎭 <i>(активный)</i>" if is_active else ""
    in_battle_marker = " ⚔️ <i>в битве</i>" if ch.in_battle else ""
    return (
        f"📜 <b>{ch.name}</b>{active_marker}{in_battle_marker}\n"
        f"Класс: {CLASS_LABELS[ch.char_class]}\n"
        f"ХП: <b>{ch.current_hp}</b> / {ch.max_hp}\n"
        f"Статус: {status}"
    )


def render_persona_overview(user_id: int) -> str:
    chars = char_list(user_id)
    header = f"👤 Вы: <b>{display_name(user_id)}</b>"
    if not chars:
        return (
            f"{header}\n\n"
            "У тебя пока нет персонажей.\n"
            "Нажми «➕ Добавить персонажа», чтобы создать первого."
        )
    active_name = active_char.get(user_id)
    lines = [f"{header}\n", "<b>Ваши персонажи:</b>"]
    for ch in chars:
        prefix = "🎭 " if ch.name == active_name else "   "
        hp = f"{ch.current_hp}/{ch.max_hp} хп"
        defeated = "  💀" if ch.current_hp <= 0 else ""
        lines.append(f"{prefix}{ch.name} — {hp}{defeated}")
    lines.append("")
    lines.append(
        f"<i>Максимум {MAX_CHARS_PER_USER} персонажей. "
        "Тыкни по персонажу, чтобы открыть его карточку.</i>"
    )
    return "\n".join(lines)


def render_battle_status(ch: Character) -> str:
    status = "Жив" if ch.current_hp > 0 else "Без сознания"
    return (
        f"⚔️ <b>Состояние в бою — {ch.name}</b>\n"
        f"Класс: {CLASS_LABELS[ch.char_class]}\n"
        f"Статус: <b>{status}</b>\n"
        f"Текущее ХП: <b>{ch.current_hp} / {ch.max_hp}</b>"
    )


def _char_line(uname: str, ch: Optional[Character]) -> str:
    if ch is None:
        return f"<b>{uname}</b>: <i>(персонаж не найден)</i>"
    emoji = "💀" if ch.current_hp <= 0 else "❤️"
    return (
        f"{emoji} <b>{uname}</b> [{ch.name}] — {CLASS_LABELS[ch.char_class]}: "
        f"<b>{ch.current_hp}</b> / {ch.max_hp}"
    )


def render_duel_status(duel: Duel) -> str:
    a = get_chars(duel.initiator_id).get(duel.initiator_char)
    b = get_chars(duel.opponent_id).get(duel.opponent_char)
    name_a = display_name(duel.initiator_id)
    name_b = display_name(duel.opponent_id)

    if duel.status == DuelStatus.PENDING:
        return (
            f"⚔️ <b>{name_a} вызывает {name_b} на дуэль!</b>\n\n"
            f"Боец инициатора: <b>{duel.initiator_char}</b>\n\n"
            f"{name_b}, прими или откажись:"
        )

    if duel.status == DuelStatus.FINISHED:
        defeated_name = None
        if a and a.current_hp <= 0:
            defeated_name = f"{name_a} ({duel.initiator_char})"
        elif b and b.current_hp <= 0:
            defeated_name = f"{name_b} ({duel.opponent_char})"
        header = "💥 <b>ДУЭЛЬ ОКОНЧЕНА</b>"
        if defeated_name:
            footer = f"\n\n{defeated_name} потерял сознание и был перенесён в больницу 🚑"
        else:
            footer = "\n\n<i>Дуэль завершена.</i>"
        return (
            f"{header}\n\n"
            f"{_char_line(name_a, a)}\n"
            f"{_char_line(name_b, b)}"
            f"{footer}"
        )

    # ACTIVE
    return (
        "⚔️ <b>ДУЭЛЬ В РАЗГАРЕ</b>\n\n"
        f"{_char_line(name_a, a)}\n"
        f"{_char_line(name_b, b)}\n\n"
        "<i>Каждый игрок управляет своим состоянием через /fif</i>"
    )


# ---------------------------------------------------------------------------
# Утилиты для дуэлей
# ---------------------------------------------------------------------------


async def _refresh_duel_message(
    bot: Bot, duel: Duel, reply_markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    if duel.message_id is None:
        return
    try:
        await bot.edit_message_text(
            text=render_duel_status(duel),
            chat_id=duel.chat_id,
            message_id=duel.message_id,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logging.warning("Не удалось обновить сообщение дуэли: %s", exc)


def _cleanup_duel(duel: Duel) -> None:
    user_duel.pop(duel.initiator_id, None)
    user_duel.pop(duel.opponent_id, None)
    duel.status = DuelStatus.FINISHED
    a = get_chars(duel.initiator_id).get(duel.initiator_char)
    b = get_chars(duel.opponent_id).get(duel.opponent_char)
    for ch in (a, b):
        if ch is not None:
            ch.in_battle = False
    save_state()


def _cancel_user_duel(user_id: int) -> Optional[Duel]:
    duel_id = user_duel.get(user_id)
    if duel_id is None:
        return None
    duel = duels.get(duel_id)
    if duel is None:
        user_duel.pop(user_id, None)
        return None
    _cleanup_duel(duel)
    return duel


# ---------------------------------------------------------------------------
# /start, /persona, регистрация (имя → класс → max_hp)
# ---------------------------------------------------------------------------


def _validate_name(name: str, user_id: int) -> Optional[str]:
    """Возвращает строку с ошибкой или None если ок."""
    name = name.strip()
    if not name:
        return "Имя не может быть пустым."
    if len(name) > MAX_NAME_LEN:
        return f"Имя слишком длинное (макс {MAX_NAME_LEN} символов)."
    if "\n" in name:
        return "В имени не должно быть переноса строки."
    if name in get_chars(user_id):
        return f"Персонаж с именем «{name}» у тебя уже есть."
    return None


async def _show_persona(message: Message, user_id: int) -> None:
    await message.answer(
        render_persona_overview(user_id),
        reply_markup=persona_main_kb(user_id),
    )


async def _start_new_char(message: Message, state: FSMContext) -> None:
    """Запускает FSM добавления нового персонажа (имя → класс → ХП)."""
    user_id = message.from_user.id
    if len(get_chars(user_id)) >= MAX_CHARS_PER_USER:
        await message.answer(
            f"У тебя уже {MAX_CHARS_PER_USER} персонажей — это максимум. "
            "Удали кого-нибудь через /persona."
        )
        return
    await state.set_state(RegState.waiting_for_name)
    await message.answer(
        "✏️ Введите <b>имя нового персонажа</b> "
        f"(до {MAX_NAME_LEN} символов):"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    remember_user(message.from_user)
    await state.clear()

    user_id = message.from_user.id

    if get_chars(user_id):
        # У юзера уже есть персонажи — открываем меню.
        await _show_persona(message, user_id)
        return

    # Первый запуск — начинаем регистрацию.
    await message.answer(
        "👋 Привет! Я бот-трекер персонажей для RP-боёв.\n\n"
        f"Давай зарегистрируем первого персонажа (потом сможешь добавить ещё, "
        f"до {MAX_CHARS_PER_USER} штук).\n\n"
        f"Введите <b>имя персонажа</b> (до {MAX_NAME_LEN} символов):"
    )
    await state.set_state(RegState.waiting_for_name)


@router.message(Command("persona"))
async def cmd_persona(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    # /persona не очищает FSM — это намеренно, можно открыть параллельно.
    await _show_persona(message, message.from_user.id)


@router.message(RegState.waiting_for_name)
async def on_char_name(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    name = (message.text or "").strip()
    err = _validate_name(name, message.from_user.id)
    if err:
        await message.answer(f"⚠️ {err} Попробуйте ещё раз:")
        return
    await state.update_data(char_name=name)
    await message.answer(
        f"Имя: <b>{name}</b>\n\nТеперь выбери класс:",
        reply_markup=class_choice_kb(message.from_user.id),
    )
    await state.set_state(RegState.waiting_for_class)


@router.callback_query(F.data.startswith("class:"))
async def on_class_selected(cb: CallbackQuery, state: FSMContext) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return

    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await cb.answer()
        return
    value = parts[1]
    try:
        char_class = CharClass(value)
    except ValueError:
        await cb.answer("Неизвестный класс", show_alert=True)
        return

    current_state = await state.get_state()
    if current_state != RegState.waiting_for_class.state:
        await cb.answer("Кнопка устарела. Открой /persona заново.", show_alert=True)
        return

    await state.update_data(char_class=char_class.value)
    await cb.message.edit_text(
        f"Класс выбран: <b>{CLASS_LABELS[char_class]}</b>\n\n"
        "Теперь введи <b>максимальное ХП</b> персонажа "
        "(целое положительное число, например: 100, 500, 1000).\n\n"
        "<i>Если ты в группе — ответь на это сообщение реплаем, иначе бот "
        "может его не увидеть.</i>"
    )
    await state.set_state(RegState.waiting_for_max_hp)
    await cb.answer()


@router.message(RegState.waiting_for_max_hp)
async def on_max_hp(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer(
            "⚠️ Это не похоже на корректное число.\n"
            "Введи целое положительное число (например: 100)."
        )
        return

    max_hp = int(text)
    data = await state.get_data()
    name = data.get("char_name")
    char_class_val = data.get("char_class")
    if not name or not char_class_val:
        await state.clear()
        await message.answer("Что-то пошло не так. Начни заново через /persona.")
        return

    user_id = message.from_user.id
    char_class = CharClass(char_class_val)
    ch = Character(name=name, char_class=char_class, max_hp=max_hp, current_hp=max_hp)
    characters.setdefault(user_id, {})[name] = ch
    # Если активного персонажа ещё нет — делаем этого активным.
    if user_id not in active_char:
        active_char[user_id] = name
    save_state()

    await state.clear()
    idx = char_idx(user_id, name) or 0
    await message.answer(
        render_char_sheet(ch, is_active=active_char.get(user_id) == name)
        + "\n\nПерсонаж создан! Можешь сразу присоединиться к битве:",
        reply_markup=join_battle_kb(user_id, idx),
    )


# ---------------------------------------------------------------------------
# /persona — обработка кнопок
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("persona:back:"))
async def on_persona_back(cb: CallbackQuery) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    user_id = cb.from_user.id
    await cb.message.edit_text(
        render_persona_overview(user_id),
        reply_markup=persona_main_kb(user_id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("persona:add:"))
async def on_persona_add(cb: CallbackQuery, state: FSMContext) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    user_id = cb.from_user.id
    if len(get_chars(user_id)) >= MAX_CHARS_PER_USER:
        await cb.answer(
            f"Уже {MAX_CHARS_PER_USER} персонажей — это максимум.",
            show_alert=True,
        )
        return
    await state.set_state(RegState.waiting_for_name)
    await cb.message.answer(
        "✏️ Введите <b>имя нового персонажа</b> "
        f"(до {MAX_NAME_LEN} символов):"
    )
    await cb.answer()


@router.callback_query(F.data.startswith("persona:open:"))
async def on_persona_open(cb: CallbackQuery) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        idx = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    user_id = cb.from_user.id
    ch = char_by_idx(user_id, idx)
    if ch is None:
        await cb.answer("Персонаж не найден", show_alert=True)
        await cb.message.edit_text(
            render_persona_overview(user_id),
            reply_markup=persona_main_kb(user_id),
        )
        return
    is_active = active_char.get(user_id) == ch.name
    await cb.message.edit_text(
        render_char_sheet(ch, is_active=is_active),
        reply_markup=persona_char_kb(user_id, idx, ch),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("persona:setactive:"))
async def on_persona_setactive(cb: CallbackQuery) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        idx = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    user_id = cb.from_user.id

    if user_id in user_duel:
        await cb.answer(
            "Ты сейчас в дуэли — нельзя менять активного персонажа до её окончания.",
            show_alert=True,
        )
        return

    ch = char_by_idx(user_id, idx)
    if ch is None:
        await cb.answer("Персонаж не найден", show_alert=True)
        return

    active_char[user_id] = ch.name
    save_state()

    await cb.message.edit_text(
        render_char_sheet(ch, is_active=True),
        reply_markup=persona_char_kb(user_id, idx, ch),
    )
    await cb.answer(f"«{ch.name}» теперь активный 🎭")


@router.callback_query(F.data.startswith("persona:delete:"))
async def on_persona_delete(cb: CallbackQuery) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        idx = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    user_id = cb.from_user.id
    ch = char_by_idx(user_id, idx)
    if ch is None:
        await cb.answer("Персонаж не найден", show_alert=True)
        return
    if user_id in user_duel and active_char.get(user_id) == ch.name:
        await cb.answer(
            "Этот персонаж сейчас в дуэли — нельзя удалить.",
            show_alert=True,
        )
        return

    await cb.message.edit_text(
        f"🗑 Удалить персонажа <b>{ch.name}</b>?\n\n"
        "Это действие нельзя отменить.",
        reply_markup=persona_delete_confirm_kb(user_id, idx),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("persona:delconfirm:"))
async def on_persona_delconfirm(cb: CallbackQuery) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        idx = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    user_id = cb.from_user.id
    ch = char_by_idx(user_id, idx)
    if ch is None:
        await cb.answer("Уже удалён", show_alert=True)
        await cb.message.edit_text(
            render_persona_overview(user_id),
            reply_markup=persona_main_kb(user_id),
        )
        return
    if user_id in user_duel and active_char.get(user_id) == ch.name:
        await cb.answer(
            "Этот персонаж сейчас в дуэли — нельзя удалить.",
            show_alert=True,
        )
        return

    name = ch.name
    characters[user_id].pop(name, None)
    if not characters[user_id]:
        characters.pop(user_id, None)
    if active_char.get(user_id) == name:
        # выбираем новый активный — первый в списке, если есть
        remaining = char_list(user_id)
        if remaining:
            active_char[user_id] = remaining[0].name
        else:
            active_char.pop(user_id, None)
    save_state()

    await cb.message.edit_text(
        f"🗑 Персонаж <b>{name}</b> удалён.\n\n" + render_persona_overview(user_id),
        reply_markup=persona_main_kb(user_id),
    )
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("persona:edithp:"))
async def on_persona_edithp(cb: CallbackQuery, state: FSMContext) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        idx = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    user_id = cb.from_user.id
    ch = char_by_idx(user_id, idx)
    if ch is None:
        await cb.answer("Персонаж не найден", show_alert=True)
        return
    if user_id in user_duel and active_char.get(user_id) == ch.name:
        await cb.answer(
            "Этот персонаж сейчас в дуэли — нельзя редактировать ХП.",
            show_alert=True,
        )
        return

    await state.set_state(EditState.waiting_for_hp)
    await state.update_data(edit_char_name=ch.name)
    await cb.message.answer(
        f"✏️ Редактирование ХП персонажа <b>{ch.name}</b>.\n"
        f"Сейчас: <b>{ch.current_hp}/{ch.max_hp}</b>\n\n"
        "Введите новое значение в формате <code>текущее/макс</code>, "
        "например <code>50/100</code>.\n"
        "Можно ввести только число — оно станет и текущим, и максимальным.\n\n"
        "<i>Если ты в группе — отвечай реплаем на это сообщение.</i>"
    )
    await cb.answer()


@router.message(EditState.waiting_for_hp)
async def on_edit_hp_input(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    text = (message.text or "").strip()
    data = await state.get_data()
    name = data.get("edit_char_name")
    if not name:
        await state.clear()
        await message.answer("Что-то пошло не так. Открой /persona заново.")
        return

    user_id = message.from_user.id
    ch = get_chars(user_id).get(name)
    if ch is None:
        await state.clear()
        await message.answer("Персонаж не найден. Открой /persona заново.")
        return

    # Поддерживаем форматы: "N", "N/M", "N / M"
    parts = [p.strip() for p in text.replace("\\", "/").split("/") if p.strip()]
    if len(parts) == 1:
        if not parts[0].isdigit():
            await message.answer("⚠️ Нужно число (или формат текущее/макс). Попробуй ещё раз:")
            return
        new_val = int(parts[0])
        if new_val <= 0:
            await message.answer("⚠️ ХП должно быть положительным.")
            return
        ch.current_hp = new_val
        ch.max_hp = new_val
    elif len(parts) == 2:
        if not (parts[0].isdigit() and parts[1].isdigit()):
            await message.answer("⚠️ Нужны два числа в формате текущее/макс. Попробуй ещё раз:")
            return
        cur, mx = int(parts[0]), int(parts[1])
        if mx <= 0:
            await message.answer("⚠️ Максимальное ХП должно быть положительным.")
            return
        if cur < 0:
            await message.answer("⚠️ Текущее ХП не может быть отрицательным.")
            return
        if cur > mx:
            await message.answer("⚠️ Текущее ХП не может превышать максимальное.")
            return
        ch.current_hp = cur
        ch.max_hp = mx
    else:
        await message.answer("⚠️ Неверный формат. Используй <code>N</code> или <code>N/M</code>.")
        return

    if ch.current_hp <= 0:
        ch.in_battle = False
    save_state()

    await state.clear()
    is_active = active_char.get(user_id) == ch.name
    idx = char_idx(user_id, ch.name) or 0
    await message.answer(
        f"✅ ХП обновлено.\n\n" + render_char_sheet(ch, is_active=is_active),
        reply_markup=persona_char_kb(user_id, idx, ch),
    )


# ---------------------------------------------------------------------------
# Бой: «Присоединиться к битве», /fif, кнопки урона/лечения
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("battle:join:"))
async def on_join_battle(cb: CallbackQuery) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        idx = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    user_id = cb.from_user.id
    ch = char_by_idx(user_id, idx)
    if ch is None:
        await cb.answer("Персонаж не найден", show_alert=True)
        return

    if ch.current_hp <= 0:
        await cb.answer(
            "Персонаж без сознания. Сначала восстанови ХП в /persona.",
            show_alert=True,
        )
        return

    # Делаем этого персонажа активным (нельзя сменить, если в дуэли)
    if user_id in user_duel:
        if active_char.get(user_id) != ch.name:
            await cb.answer(
                "Ты сейчас в дуэли другим персонажем — закончи её сначала.",
                show_alert=True,
            )
            return
    else:
        active_char[user_id] = ch.name

    ch.in_battle = True
    save_state()

    await cb.message.edit_text(
        render_char_sheet(ch, is_active=True) + "\n\n✅ <b>Зачислен в бой.</b>"
    )
    await cb.message.answer(
        "🛡 <b>Вы готовы к бою. Ожидайте начала.</b>\n"
        "Для управления состоянием в бою введите команду /fif\n\n"
        "Если хочешь сразиться с другим игроком — в групповом чате используйте "
        "<code>/duel @username</code> (или /duel в ответ на сообщение)."
    )
    await cb.answer()


@router.message(Command("fif"))
async def cmd_fif(message: Message) -> None:
    remember_user(message.from_user)
    user_id = message.from_user.id
    chars = get_chars(user_id)
    if not chars:
        await message.answer("У тебя ещё нет персонажа. Введи /start, чтобы создать.")
        return
    ch = get_active(user_id)
    if ch is None:
        await message.answer(
            "Активный персонаж не выбран. Открой /persona и выбери активного."
        )
        return
    if ch.current_hp <= 0:
        await message.answer(
            f"💀 Персонаж <b>{ch.name}</b> без сознания (0 ХП).\n"
            "Восстанови ему ХП в /persona или сделай активным другого."
        )
        return
    if not ch.in_battle:
        await message.answer(
            f"Персонаж <b>{ch.name}</b> ещё не в битве. "
            "Открой /persona → выбери его → «🗡 В битву» или используй "
            "<code>/duel @username</code> в группе."
        )
        return
    await message.answer(
        render_battle_status(ch),
        reply_markup=battle_actions_kb(user_id),
    )


@router.callback_query(F.data.startswith("hp:"))
async def on_hp_change(cb: CallbackQuery, bot: Bot) -> None:
    remember_user(cb.from_user)
    if not await _ensure_owner(cb):
        return
    user_id = cb.from_user.id
    ch = get_active(user_id)
    if ch is None or not ch.in_battle:
        await cb.answer("Ты не в бою. Открой /persona → выбери персонажа → «🗡 В битву».", show_alert=True)
        return
    if ch.current_hp <= 0:
        await cb.answer("Персонаж без сознания.", show_alert=True)
        return

    stats = CLASS_STATS[ch.char_class]
    action = cb.data.split(":")[1] if cb.data else ""
    if action == "damage":
        delta = -(ch.max_hp * stats["damage_pct"]) // 100
    elif action == "heal":
        delta = (ch.max_hp * stats["heal_pct"]) // 100
    else:
        await cb.answer()
        return

    ch.current_hp = min(ch.max_hp, ch.current_hp + delta)
    if ch.current_hp < 0:
        ch.current_hp = 0

    duel_id = user_duel.get(user_id)
    duel = duels.get(duel_id) if duel_id else None

    if ch.current_hp <= 0:
        ch.in_battle = False
        save_state()
        await cb.message.edit_text(
            f"💀 <b>{ch.name} потерял сознание и будет перенесён в больницу 🚑</b>\n\n"
            f"ХП сохранено: <b>0/{ch.max_hp}</b>. Восстанови ХП в /persona, чтобы вернуться в бой."
        )
        if duel is not None and duel.status == DuelStatus.ACTIVE:
            _cleanup_duel(duel)
            await _refresh_duel_message(bot, duel)
    else:
        save_state()
        await cb.message.edit_text(
            render_battle_status(ch),
            reply_markup=battle_actions_kb(user_id),
        )
        if duel is not None and duel.status == DuelStatus.ACTIVE:
            await _refresh_duel_message(bot, duel)
    await cb.answer()


# ---------------------------------------------------------------------------
# Дуэли
# ---------------------------------------------------------------------------


def _resolve_duel_target(message: Message) -> tuple[Optional[int], str]:
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        if not u.is_bot:
            remember_user(u)
            name = "@" + u.username if u.username else (u.first_name or f"id{u.id}")
            return u.id, name

    if message.entities and message.text:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                u = ent.user
                remember_user(u)
                name = "@" + u.username if u.username else (u.first_name or f"id{u.id}")
                return u.id, name
            if ent.type == "mention":
                mention_text = message.text[ent.offset: ent.offset + ent.length]
                uname = mention_text.lstrip("@")
                if BOT_USERNAME and uname.lower() == BOT_USERNAME.lower():
                    continue
                user_id = username_to_id.get(uname.lower())
                if user_id is not None:
                    return user_id, "@" + uname
                return None, mention_text

    return None, ""


@router.message(Command("duel"))
async def cmd_duel(message: Message, bot: Bot) -> None:
    remember_user(message.from_user)
    sender = message.from_user

    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "Команда /duel работает только в групповых чатах.\n"
            "Добавьте бота в группу и вызовите команду там."
        )
        return

    sender_char = get_active(sender.id)
    if sender_char is None:
        await message.answer(
            f"{display_name(sender.id)}, сначала зарегистрируй персонажа: /start "
            "(и выбери активного через /persona)."
        )
        return
    if sender_char.current_hp <= 0:
        await message.answer(
            f"{display_name(sender.id)}, твой активный персонаж "
            f"<b>{sender_char.name}</b> без сознания. Восстанови ХП в /persona."
        )
        return

    if sender.id in user_duel:
        await message.answer(
            f"{display_name(sender.id)}, ты уже в дуэли. "
            "Заверши её прежде, чем начинать новую."
        )
        return

    target_id, target_name = _resolve_duel_target(message)

    if target_id is None:
        if target_name:
            await message.answer(
                f"Не смог найти игрока {target_name}. "
                "Пусть он сначала напишет /start этому боту, чтобы я его запомнил."
            )
        else:
            await message.answer(
                "Не понял, кого вызываешь. Используй:\n"
                "• <code>/duel @username</code>\n"
                "• или ответь командой <code>/duel</code> на сообщение игрока."
            )
        return

    if target_id == sender.id:
        await message.answer("Самого себя на дуэль вызвать нельзя 😄")
        return

    target_char = get_active(target_id)
    if target_char is None:
        await message.answer(
            f"{target_name} ещё не зарегистрировал персонажа (или не выбрал активного). "
            "Пусть откроет /persona."
        )
        return
    if target_char.current_hp <= 0:
        await message.answer(
            f"Активный персонаж {target_name} (<b>{target_char.name}</b>) без сознания. "
            "Пусть восстановит ХП через /persona."
        )
        return

    if target_id in user_duel:
        await message.answer(f"{target_name} уже в другой дуэли.")
        return

    duel = Duel(
        duel_id=make_duel_id(),
        chat_id=message.chat.id,
        initiator_id=sender.id,
        opponent_id=target_id,
        initiator_char=sender_char.name,
        opponent_char=target_char.name,
        status=DuelStatus.PENDING,
    )
    duels[duel.duel_id] = duel
    user_duel[sender.id] = duel.duel_id
    user_duel[target_id] = duel.duel_id

    sent = await message.answer(
        render_duel_status(duel),
        reply_markup=duel_invite_kb(duel.duel_id, target_id),
    )
    duel.message_id = sent.message_id


@router.callback_query(F.data.startswith("duel:accept:"))
async def on_duel_accept(cb: CallbackQuery, bot: Bot) -> None:
    remember_user(cb.from_user)
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        duel_id = int(parts[2])
        expected_opp = int(parts[3])
    except ValueError:
        await cb.answer()
        return

    if cb.from_user.id != expected_opp:
        await cb.answer("Этот вызов адресован не тебе 🙅", show_alert=True)
        return

    duel = duels.get(duel_id)
    if duel is None or duel.status != DuelStatus.PENDING:
        await cb.answer("Этот вызов уже неактуален.", show_alert=True)
        return

    duel.status = DuelStatus.ACTIVE
    a = get_chars(duel.initiator_id).get(duel.initiator_char)
    b = get_chars(duel.opponent_id).get(duel.opponent_char)
    for ch in (a, b):
        if ch is not None:
            ch.in_battle = True
            # HP НЕ восстанавливаем — играем с тем, что есть
    save_state()

    await cb.message.edit_text(render_duel_status(duel))
    await cb.message.answer(
        f"⚔️ Дуэль между {display_name(duel.initiator_id)} ({duel.initiator_char}) "
        f"и {display_name(duel.opponent_id)} ({duel.opponent_char}) началась!\n"
        "Каждый управляет своим состоянием командой /fif."
    )
    await cb.answer("Дуэль принята")


@router.callback_query(F.data.startswith("duel:decline:"))
async def on_duel_decline(cb: CallbackQuery) -> None:
    remember_user(cb.from_user)
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        duel_id = int(parts[2])
        expected_opp = int(parts[3])
    except ValueError:
        await cb.answer()
        return

    if cb.from_user.id != expected_opp:
        await cb.answer("Этот вызов адресован не тебе 🙅", show_alert=True)
        return

    duel = duels.get(duel_id)
    if duel is None or duel.status != DuelStatus.PENDING:
        await cb.answer("Этот вызов уже неактуален.", show_alert=True)
        return

    _cleanup_duel(duel)
    await cb.message.edit_text(
        f"❌ {display_name(duel.opponent_id)} отказался от вызова "
        f"{display_name(duel.initiator_id)}."
    )
    await cb.answer()


# ---------------------------------------------------------------------------
# Сервисные команды
# ---------------------------------------------------------------------------


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    remember_user(message.from_user)
    atk = CLASS_STATS[CharClass.ATTACKER]
    heal = CLASS_STATS[CharClass.HEALER]
    await message.answer(
        "<b>📖 Команды</b>\n"
        "/start — регистрация (или открыть /persona, если уже есть персонажи)\n"
        "/persona — управление персонажами (до "
        f"{MAX_CHARS_PER_USER}): список, добавить, изменить ХП, удалить\n"
        "/fif — управление состоянием активного персонажа в бою (урон / лечение)\n"
        "/duel @username — вызвать игрока на дуэль (только в группах)\n"
        "    └ альтернативно: /duel в ответ на сообщение игрока\n"
        "/help — это сообщение\n\n"
        "<b>⚔️ Классы и проценты от макс. ХП</b>\n"
        f"{CLASS_LABELS[CharClass.ATTACKER]}\n"
        f"    🩸 урон: −{atk['damage_pct']}%   🩹 лечение: +{atk['heal_pct']}%\n"
        f"{CLASS_LABELS[CharClass.HEALER]}\n"
        f"    🩸 урон: −{heal['damage_pct']}%   🩹 лечение: +{heal['heal_pct']}%\n\n"
        "<b>🛠 Как играть</b>\n"
        "1. /start — задаёшь имя, выбираешь класс, вводишь макс. ХП (например 100).\n"
        "2. /persona — посмотреть список персонажей, переключить активного, "
        "изменить ХП или удалить.\n"
        "3. «🗡 В битву» под карточкой персонажа или прямо после регистрации — "
        "ставит персонажа в режим боя.\n"
        "4. /fif — открываешь кнопки урона/лечения для активного персонажа. "
        "Когда появится сообщение, где нужно внести хп, используйте свой ролл как "
        "здоровье, и внесите его в ответ на сообщение боту, если вы напишете это "
        "просто в группу, бот, вероятно, не заметит.\n"
        "5. Для дуэли — в группе с ботом: /duel @username или в ответ на сообщение. "
        "Дуэль идёт между активными персонажами обоих игроков.\n\n"
        "<i>ХП не восстанавливается автоматически после боя — финальное значение "
        "сохраняется в карточке персонажа. Восстановить ХП можно вручную в /persona.</i>"
    )


# ---------------------------------------------------------------------------
# Ограничение по chat_id
# ---------------------------------------------------------------------------


def _extract_chat_id(event: TelegramObject) -> Optional[int]:
    if isinstance(event, Message):
        return event.chat.id if event.chat else None
    if isinstance(event, CallbackQuery):
        if event.message and event.message.chat:
            return event.message.chat.id
        return None
    return None


class ChatWhitelistMiddleware(BaseMiddleware):
    """Пропускает только апдейты из чатов из ``ALLOWED_CHAT_IDS``.

    Если whitelist пуст — пропускает всё (удобно для локальной разработки).
    """

    def __init__(self, allowed: set[int]) -> None:
        super().__init__()
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self.allowed:
            return await handler(event, data)
        chat_id = _extract_chat_id(event)
        if chat_id is not None and chat_id in self.allowed:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            try:
                await event.answer("Бот работает только в разрешённых чатах.", show_alert=True)
            except Exception:  # noqa: BLE001
                pass
        logging.info(
            "Игнорирую апдейт из чата %s (whitelist: %s)", chat_id, self.allowed
        )
        return None


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


BOT_COMMANDS = [
    BotCommand(command="start", description="Регистрация / меню персонажей"),
    BotCommand(command="persona", description="Управление персонажами"),
    BotCommand(command="fif", description="Управление в бою"),
    BotCommand(command="duel", description="Вызов на дуэль (в группе)"),
    BotCommand(command="help", description="Помощь"),
]


async def _setup(token: str) -> tuple[Bot, Dispatcher]:
    global STORAGE, _save_lock, BOT_USERNAME

    STORAGE = build_storage()
    _save_lock = asyncio.Lock()
    await STORAGE.init()

    await load_state()
    logging.info(
        "Загружено: %d юзеров с персонажами, %d username'ов.",
        len(characters), len(username_to_id),
    )
    if ALLOWED_CHAT_IDS:
        logging.info("ALLOWED_CHAT_IDS: %s", sorted(ALLOWED_CHAT_IDS))
    else:
        logging.warning(
            "ALLOWED_CHAT_IDS не задан — бот будет отвечать в ЛЮБОМ чате. "
            "Задайте ALLOWED_CHAT_IDS, чтобы ограничить."
        )

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    middleware = ChatWhitelistMiddleware(ALLOWED_CHAT_IDS)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)

    dp.include_router(router)

    me = await bot.get_me()
    BOT_USERNAME = me.username
    logging.info("Бот авторизован как @%s.", BOT_USERNAME)

    await bot.set_my_commands(BOT_COMMANDS)

    return bot, dp


async def _run_polling(token: str) -> None:
    bot, dp = await _setup(token)
    try:
        # На всякий случай сносим webhook, чтобы getUpdates не конфликтовал.
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("Запуск в режиме long-polling.")
        await dp.start_polling(bot)
    finally:
        if STORAGE is not None:
            await STORAGE.close()


async def _run_webhook(token: str, base_url: str) -> None:
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import (
        SimpleRequestHandler,
        setup_application,
    )

    bot, dp = await _setup(token)

    # Путь делаем непредсказуемым, чтобы посторонние не дёргали вебхук.
    webhook_path = os.environ.get("WEBHOOK_PATH") or f"/tg/{token.split(':')[0]}"
    webhook_secret = os.environ.get("WEBHOOK_SECRET") or None
    webhook_url = base_url.rstrip("/") + webhook_path
    port = int(os.environ.get("PORT", "8080"))

    await bot.set_webhook(
        url=webhook_url,
        secret_token=webhook_secret,
        drop_pending_updates=False,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logging.info("Webhook установлен: %s", webhook_url)

    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=webhook_secret,
    )
    handler.register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    async def _on_cleanup(_app: web.Application) -> None:
        if STORAGE is not None:
            await STORAGE.close()

    app.on_cleanup.append(_on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("HTTP-сервер слушает 0.0.0.0:%s, путь вебхука: %s", port, webhook_path)

    # Блокируемся, пока процесс не остановят (SIGTERM от Render и т.п.).
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задана переменная окружения BOT_TOKEN. "
            "Получите токен у @BotFather и задайте BOT_TOKEN=..."
        )

    webhook_base = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if webhook_base:
        await _run_webhook(token, webhook_base)
    else:
        await _run_polling(token)


if __name__ == "__main__":
    asyncio.run(main())
