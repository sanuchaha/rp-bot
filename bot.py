"""Telegram-бот: трекер состояния персонажей для RP и текстовых боёв.

Возможности:
- Несколько персонажей на одного юзера (до 5), у каждого имя, класс, max/current HP.
- /persona — меню управления (список, выбор активного, добавление, редактирование HP, удаление).
- /start — для нового юзера запускает регистрацию, для существующего открывает /persona.
- /duel @username (или /duel в ответ на сообщение) — дуэль в группах.
- ХП НЕ восстанавливается автоматически после боя — сохраняется в карточке.
- Сохранение состояния в data.json (переживает перезапуск).
"""

import asyncio
import html
import json
import logging
import os
import random
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
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

# Командный бой (/buttle)
MIN_BATTLE_TEAMS = 2
MAX_BATTLE_TEAMS = 4
MAX_BATTLE_TEAM_SIZE = 5
DEFAULT_BATTLE_TIMEOUT_SEC = 5 * 60
MIN_BATTLE_TIMEOUT_MIN = 1
MAX_BATTLE_TIMEOUT_MIN = 30

TEAM_EMOJIS = ["🟥", "🟦", "🟩", "🟨"]

# Roll-базированная боевая система (/attack, /defend, /heal, /roll)
ROLL_MAX = 100                # Кубик 1–100 (используется только в /roll без аргумента)
HEAL_LOW_ROLL_THRESHOLD = 30    # Для Лекаря: ролл ≤ 30 → мини-хил
HEALER_LOW_HEAL_PCT = 5         # Мини-хил Лекаря при низком ролле

# Лечение в командном бою (/buttle): только союзников и с КД (в ходах игрока).
# Сам HEAL_COOLDOWN_BY_CLASS определён ниже — рядом с CLASS_STATS — после CharClass.
# Лекарь после полного хила накладывает на цель HoT — мини-хил на 2 хода.
HEALER_HOT_TICKS_AFTER_HEAL = 2
HEALER_HOT_PERMILLE = 15  # 1.5% от max ХП за тик HoT (в 1/1000)
ATTACKER_LOW_HEAL_PCT = 2  # Мини-хил Бойца при низком ролле (% от max ХП цели)
# Слабые атаки при низком ролле (≤ HEAL_LOW_ROLL_THRESHOLD) — половина обычного урона по классу.
ATTACKER_LOW_DAMAGE_PCT = 5  # Боец при низком ролле наносит 5% (вместо 15%).
HEALER_LOW_DAMAGE_PCT = 2    # Лекарь при низком ролле наносит 2% (вместо 5%).


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
    CharClass.ATTACKER: {"damage_pct": 15, "heal_pct": 5},
    CharClass.HEALER: {"damage_pct": 5, "heal_pct": 15},
}

# КД лечения (в ходах игрока) по классам — Боец сидит дольше, Лекарь восстанавливается быстрее.
HEAL_COOLDOWN_BY_CLASS = {
    CharClass.ATTACKER: 4,
    CharClass.HEALER: 2,
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
    # Состояние активного действия с ожиданием /defend:
    # pending_kind: "attack" (защита от удара) или "heal_resist" (сопротивление лечению не-Лекаря)
    pending_kind: Optional[str] = None
    pending_attacker_id: Optional[int] = None
    # pending_attack_cap — макс кубика, который указал инициатор (потолок);
    # pending_attack_roll — фактический случайный бросок 1..cap, который и сравнивается.
    pending_attack_cap: Optional[int] = None
    pending_attack_roll: Optional[int] = None


class BattleStatus(str, Enum):
    REGISTERING = "registering"
    ACTIVE = "active"
    FINISHED = "finished"


@dataclass
class Battle:
    battle_id: int
    chat_id: int
    initiator_id: int
    num_teams: int
    teams: dict[int, list[int]]              # team_idx -> [user_id, ...]
    char_by_user: dict[int, str]             # user_id -> имя персонажа на момент входа
    deadline_ts: float = 0.0
    status: BattleStatus = BattleStatus.REGISTERING
    message_id: Optional[int] = None
    # КД лечения по командному бою: user_id -> сколько ходов осталось до возможности лечить.
    # "Ход" = очередная попытка /heal от этого игрока. После полного хила союзника КД =
    # HEAL_COOLDOWN_BY_CLASS[char.char_class] (4 для Бойца, 2 для Лекаря).
    # Каждый последующий /heal, пока КД > 0, заблокирован, но КД уменьшается на 1.
    heal_cooldown: dict[int, int] = field(default_factory=dict)
    # HoT (heal-over-time) от Лекаря: healer_user_id -> {target_user_id, ticks_left}.
    # При полном хиле Лекарем — устанавливается HEALER_HOT_TICKS_AFTER_HEAL тиков.
    # При каждом /heal во время КД у Лекаря — союзнику-цели тикает +HEALER_HOT_PERMILLE/1000.
    heal_hot: dict[int, dict] = field(default_factory=dict)
    # Pending-атаки в командном бою: target_user_id -> {attacker_id, cap, roll}.
    # Хранится до тех пор, пока цель не ответит /defend|/attack|/heal или
    # пока один из участников не выйдет из боя.
    pending_attacks: dict[int, dict] = field(default_factory=dict)


# In-memory хранилище.
characters: dict[int, dict[str, Character]] = {}  # user_id -> {name -> Character}
active_char: dict[int, str] = {}                  # user_id -> name активного
duels: dict[int, Duel] = {}
user_duel: dict[int, int] = {}                    # user_id -> duel_id
battles: dict[int, Battle] = {}
user_battle: dict[int, int] = {}                  # user_id -> battle_id
_battle_timers: dict[int, asyncio.Task[None]] = {}
username_to_id: dict[str, int] = {}               # username (lower) -> user_id
user_display: dict[int, str] = {}                 # user_id -> отображаемое имя

_next_duel_id: int = 1
_next_battle_id: int = 1
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


def make_battle_id() -> int:
    global _next_battle_id
    battle_id = _next_battle_id
    _next_battle_id += 1
    return battle_id


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
    footer_lines = [
        "<i>В свой ход — <code>/attack &lt;твой макс&gt;</code> (бот кинет 1..N) "
        "или <code>/heal &lt;твой макс&gt;</code> (Лекарь — соло; Боец — нужен ответ соперника). "
        "Соперник может ответить <code>/defend</code>, <code>/attack</code> или "
        "<code>/heal</code> — у кого бросок больше, того действие сработает. "
        "Выйти — /yield.</i>"
    ]
    if duel.pending_attacker_id is not None and duel.pending_attack_roll is not None:
        atk_name = display_name(duel.pending_attacker_id)
        def_id = (
            duel.opponent_id
            if duel.pending_attacker_id == duel.initiator_id
            else duel.initiator_id
        )
        def_name = display_name(def_id)
        verb = "лечится" if duel.pending_kind == "heal_resist" else "атакует"
        cap_label = (
            f" (из 1–{duel.pending_attack_cap})"
            if duel.pending_attack_cap is not None
            else ""
        )
        footer_lines.append(
            f"\n⏳ <b>{html.escape(atk_name)}</b> {verb} с броском "
            f"<b>{duel.pending_attack_roll}</b>{cap_label} — "
            f"<b>{html.escape(def_name)}</b>, ответь "
            f"<code>/defend</code>, <code>/attack</code> или <code>/heal</code>."
        )
    return (
        "⚔️ <b>ДУЭЛЬ В РАЗГАРЕ</b>\n\n"
        f"{_char_line(name_a, a)}\n"
        f"{_char_line(name_b, b)}\n\n"
        + "\n".join(footer_lines)
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
    duel.pending_kind = None
    duel.pending_attacker_id = None
    duel.pending_attack_cap = None
    duel.pending_attack_roll = None
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


def _is_char_engaged(user_id: int, char_name: str) -> bool:
    """True, если этот персонаж сейчас в дуэли или в командном бою."""
    duel_id = user_duel.get(user_id)
    if duel_id is not None:
        duel = duels.get(duel_id)
        if duel is not None and duel.status != DuelStatus.FINISHED:
            if duel.initiator_id == user_id and duel.initiator_char == char_name:
                return True
            if duel.opponent_id == user_id and duel.opponent_char == char_name:
                return True
    battle_id = user_battle.get(user_id)
    if battle_id is not None:
        battle = battles.get(battle_id)
        if (
            battle is not None
            and battle.status != BattleStatus.FINISHED
            and battle.char_by_user.get(user_id) == char_name
        ):
            return True
    return False


def _engagement_label(user_id: int) -> Optional[str]:
    """Короткий ярлык, если игрок сейчас в дуэли или в бою — или None."""
    if user_id in user_duel:
        return "дуэли"
    if user_id in user_battle:
        return "командном бою"
    return None


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

    engagement = _engagement_label(user_id)
    if engagement is not None:
        await cb.answer(
            f"Ты сейчас в {engagement} — нельзя менять активного персонажа до окончания.",
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
    if _is_char_engaged(user_id, ch.name):
        await cb.answer(
            "Этот персонаж сейчас в бою — нельзя удалить.",
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
    if _is_char_engaged(user_id, ch.name):
        await cb.answer(
            "Этот персонаж сейчас в бою — нельзя удалить.",
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
    if _is_char_engaged(user_id, ch.name):
        await cb.answer(
            "Этот персонаж сейчас в бою — нельзя редактировать ХП.",
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
        "✅ ХП обновлено.\n\n" + render_char_sheet(ch, is_active=is_active),
        reply_markup=persona_char_kb(user_id, idx, ch),
    )


# ---------------------------------------------------------------------------
# Бой: «Присоединиться к битве»
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

    # Делаем этого персонажа активным (нельзя сменить, если в дуэли или бою)
    if user_id in user_duel:
        if active_char.get(user_id) != ch.name:
            await cb.answer(
                "Ты сейчас в дуэли другим персонажем — закончи её сначала.",
                show_alert=True,
            )
            return
    elif user_id in user_battle:
        battle = battles.get(user_battle[user_id])
        bound_char = battle.char_by_user.get(user_id) if battle is not None else None
        if bound_char is not None and bound_char != ch.name:
            await cb.answer(
                "Ты сейчас в командном бою другим персонажем — закончи его сначала.",
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
        "Действия: в командном бою — атака "
        "<code>/attack @user &lt;макс&gt;</code>, лечение "
        "<code>/heal @user &lt;макс&gt;</code>; выход — /yield.\n\n"
        "Если хочешь сразиться 1×1 — в групповом чате "
        "<code>/duel @username</code> (или /duel в ответ на сообщение)."
    )
    await cb.answer()


# Бонус сильному защитнику: за каждые HP_ADVANTAGE_STEP ОП, на которые max ОП защитника
# превышает max ОП атакующего, — −1 п.п. урона атакующего; но не больше HP_ADVANTAGE_MAX_REDUCTION п.п.
HP_ADVANTAGE_STEP = 100
HP_ADVANTAGE_MAX_REDUCTION = 5


def _hp_advantage_reduction(attacker_max_hp: int, defender_max_hp: Optional[int]) -> int:
    """Сколько п.п. вычесть из базового процента урона атакующего за преимущество защитника по ОП.

    Если max ОП защитника больше max ОП атакующего, то каждые 100 ОП превышения — −1 п.п.,
    потолок −5 п.п. Если атакующий сильнее или равен — 0 (бонуса нет)."""
    if defender_max_hp is None or defender_max_hp <= 0 or defender_max_hp <= attacker_max_hp:
        return 0
    diff = defender_max_hp - attacker_max_hp
    return min(HP_ADVANTAGE_MAX_REDUCTION, diff // HP_ADVANTAGE_STEP)

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
    if sender.id in user_battle:
        await message.answer(
            f"{display_name(sender.id)}, ты сейчас в командном бою. "
            "Дождись его окончания или выйди через /yield."
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
    if target_id in user_battle:
        await message.answer(f"{target_name} сейчас в командном бою.")
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
        "Действия: <code>/attack &lt;макс&gt;</code>, "
        "<code>/defend &lt;макс&gt;</code>, <code>/heal &lt;макс&gt;</code>. "
        "Сдаться — /yield."
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


@router.message(Command("yield"))
async def cmd_yield(message: Message, bot: Bot) -> None:
    remember_user(message.from_user)
    user_id = message.from_user.id

    # Командный бой имеет приоритет, если игрок одновременно где-то — но
    # такого по дизайну не бывает (взаимоисключающе).
    battle_id = user_battle.get(user_id)
    battle = battles.get(battle_id) if battle_id is not None else None
    if battle is not None:
        await _yield_from_battle(bot, message, battle, user_id)
        return

    duel_id = user_duel.get(user_id)
    duel = duels.get(duel_id) if duel_id is not None else None
    if duel is None:
        await message.answer("Ты сейчас не в дуэли и не в командном бою.")
        return

    initiator_name = display_name(duel.initiator_id)
    opponent_name = display_name(duel.opponent_id)
    quitter_name = display_name(user_id)
    other_id = (
        duel.opponent_id if user_id == duel.initiator_id else duel.initiator_id
    )
    other_name = display_name(other_id)
    was_active = duel.status == DuelStatus.ACTIVE

    _cleanup_duel(duel)

    if was_active:
        text = (
            f"🏳️ {quitter_name} вышел из дуэли с {other_name}.\n"
            f"Дуэль завершена. Оба игрока выведены из боя."
        )
    else:
        text = (
            f"❌ {quitter_name} отменил вызов "
            f"{initiator_name} → {opponent_name}."
        )

    await _refresh_duel_message(bot, duel)
    try:
        await bot.send_message(duel.chat_id, text)
    except Exception as exc:
        logging.warning("Не удалось отправить сообщение о выходе из дуэли: %s", exc)
        if message.chat.id != duel.chat_id:
            await message.answer(text)
    if message.chat.id != duel.chat_id:
        # Эхо лично вызвавшему, чтобы получил подтверждение (если писал в личку).
        await message.answer("Готово — дуэль завершена.")


# ---------------------------------------------------------------------------
# Roll-боевая система (/attack, /defend, /heal, /roll)
# Игроки пишут МАКС своего кубика (потолок): /attack 100, /defend 80.
# Бот сам кидает рандом 1..МАКС и сравнивает результаты — у кого число больше,
# тот перебил. У разных персов могут быть разные размеры куба, но рандом
# всегда присутствует.
# ---------------------------------------------------------------------------


def _roll(max_value: int = ROLL_MAX) -> int:
    """Случайный 1..max_value (для /roll без аргумента, и для теста)."""
    return random.randint(1, max_value)


def _duel_other_id(duel: Duel, user_id: int) -> Optional[int]:
    if user_id == duel.initiator_id:
        return duel.opponent_id
    if user_id == duel.opponent_id:
        return duel.initiator_id
    return None


def _duel_char_of(duel: Duel, user_id: int) -> Optional[Character]:
    """Персонаж этого юзера, который участвует в дуэли (или None)."""
    if user_id == duel.initiator_id:
        return get_chars(duel.initiator_id).get(duel.initiator_char)
    if user_id == duel.opponent_id:
        return get_chars(duel.opponent_id).get(duel.opponent_char)
    return None


def _get_active_duel(user_id: int) -> Optional[Duel]:
    duel_id = user_duel.get(user_id)
    if duel_id is None:
        return None
    duel = duels.get(duel_id)
    if duel is None or duel.status != DuelStatus.ACTIVE:
        return None
    return duel


def _parse_roll_arg(
    command: CommandObject,
) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Парсит МАКС кубика игрока и кидает рандом 1..МАКС.

    Возвращает (cap, roll, error). На успех cap = указанный игроком потолок,
    roll = random.randint(1, cap). При ошибке cap=roll=None, error — причина.
    """
    args = (command.args or "").strip()
    if not args:
        return (
            None,
            None,
            "Укажи МАКС своего кубика (потолок). Пример: <code>/attack 100</code> — "
            "бот кинет 1..100.",
        )
    first = args.split()[0]
    try:
        cap = int(first)
    except ValueError:
        return (
            None,
            None,
            f"«{html.escape(first)}» — не число. Пример: <code>/attack 100</code>.",
        )
    if cap < 1:
        return None, None, "МАКС кубика должен быть ≥ 1."
    if cap > 10_000_000:
        return None, None, "МАКС слишком большой (≥ 10 000 000)."
    roll = random.randint(1, cap)
    return cap, roll, None


def _clear_pending(duel: Duel) -> None:
    """Сбрасывает pending-атаку/хил у дуэли."""
    duel.pending_kind = None
    duel.pending_attacker_id = None
    duel.pending_attack_cap = None
    duel.pending_attack_roll = None


def _apply_attack_damage(
    attacker_ch: Character, defender_ch: Character
) -> tuple[int, int, str]:
    """Применяет урон к defender_ch.current_hp in-place. Возвращает (damage, before_hp, pct_label)."""
    stats = CLASS_STATS[attacker_ch.char_class]
    base_pct = stats["damage_pct"]
    reduction_pp = _hp_advantage_reduction(attacker_ch.max_hp, defender_ch.max_hp)
    effective_pct = max(1, base_pct - reduction_pp)
    damage = max(1, (defender_ch.max_hp * effective_pct) // 100)
    before = defender_ch.current_hp
    defender_ch.current_hp = max(0, before - damage)
    pct_label = f"{effective_pct}% от {defender_ch.max_hp} ОП"
    if reduction_pp > 0:
        pct_label += f" (−{reduction_pp} п.п. за преимущество защитника)"
    return damage, before, pct_label


def _apply_heal_self(healer_ch: Character, heal_roll: int) -> tuple[int, int, str]:
    """Применяет лечение к healer_ch.current_hp in-place. Возвращает (heal_amount, before_hp, label).

    Лекарь: бросок > HEAL_LOW_ROLL_THRESHOLD → полный хил, иначе мини-хил.
    Боец: фикс +heal_pct%% (без порога)."""
    if healer_ch.char_class == CharClass.HEALER:
        if heal_roll > HEAL_LOW_ROLL_THRESHOLD:
            heal_pct = CLASS_STATS[CharClass.HEALER]["heal_pct"]
            label = f"полное лечение (+{heal_pct}%)"
        else:
            heal_pct = HEALER_LOW_HEAL_PCT
            label = f"слабое лечение (+{heal_pct}%)"
    else:
        heal_pct = CLASS_STATS[healer_ch.char_class]["heal_pct"]
        label = f"+{heal_pct}%"
    heal_amount = max(1, (healer_ch.max_hp * heal_pct) // 100)
    before = healer_ch.current_hp
    healer_ch.current_hp = min(healer_ch.max_hp, before + heal_amount)
    return heal_amount, before, label


async def _handle_knockout(bot: Bot, duel: Duel, ch: Character) -> None:
    """Сбрасывает персонажа в больницу, завершает дуэль."""
    ch.in_battle = False
    save_state()
    try:
        await bot.send_message(
            duel.chat_id,
            f"💀 <b>{html.escape(ch.name)} потерял сознание и был "
            f"перенесён в больницу 🚑</b>",
        )
    except Exception:  # noqa: BLE001
        pass
    _cleanup_duel(duel)


async def _resolve_pending(
    bot: Bot,
    duel: Duel,
    response_kind: str,
    response_roll: int,
    response_cap: int,
    responder_id: int,
) -> None:
    """Разрешает pending-действие (attack или heal_resist) реакцией защитника.

    response_kind: 'defend' / 'attack' / 'heal'. Сравнивается бросок pending-инициатора
    с броском ответчика: у кого больше — у того действие «выигрывает». Ничья — обамимо."""
    pending_kind = duel.pending_kind
    initiator_id = duel.pending_attacker_id
    pending_roll = duel.pending_attack_roll
    pending_cap = duel.pending_attack_cap
    if pending_kind is None or initiator_id is None or pending_roll is None:
        return

    initiator_ch = _duel_char_of(duel, initiator_id)
    responder_ch = _duel_char_of(duel, responder_id)
    _clear_pending(duel)

    initiator_name = html.escape(display_name(initiator_id))
    responder_name = html.escape(display_name(responder_id))

    init_cap_label = f" (из 1–{pending_cap})" if pending_cap is not None else ""
    resp_cap_label = f" (из 1–{response_cap})"

    if pending_kind == "attack":
        init_label = f"⚔️ <b>{initiator_name}</b> атака"
    else:
        init_label = f"🩹 <b>{initiator_name}</b> лечение"

    if response_kind == "defend":
        resp_label = f"🛡 <b>{responder_name}</b> защита"
    elif response_kind == "attack":
        resp_label = f"⚔️ <b>{responder_name}</b> контр-атака"
    else:
        resp_label = f"🩹 <b>{responder_name}</b> хил-ответ"

    lines = [
        f"{init_label}: 🎲 <b>{pending_roll}</b>{init_cap_label}",
        f"{resp_label}: 🎲 <b>{response_roll}</b>{resp_cap_label}",
    ]

    pending_wins = pending_roll > response_roll
    response_wins = response_roll > pending_roll

    knocked_out: Optional[Character] = None

    if pending_wins:
        if pending_kind == "attack" and initiator_ch is not None and responder_ch is not None:
            dmg, before, label = _apply_attack_damage(initiator_ch, responder_ch)
            lines.append(
                f"💥 <b>{initiator_name}</b> попал: урон <b>{dmg}</b> ({label}). "
                f"{html.escape(responder_ch.name)}: {before} → "
                f"<b>{responder_ch.current_hp}</b>/{responder_ch.max_hp}."
            )
            if responder_ch.current_hp <= 0:
                knocked_out = responder_ch
        elif pending_kind == "heal_resist" and initiator_ch is not None:
            heal_amt, before, label = _apply_heal_self(initiator_ch, pending_roll)
            lines.append(
                f"✅ <b>{initiator_name}</b> {label}. "
                f"{html.escape(initiator_ch.name)}: {before} → "
                f"<b>{initiator_ch.current_hp}</b>/{initiator_ch.max_hp} (+{initiator_ch.current_hp - before})."
            )
        if response_kind == "attack":
            lines.append(f"⚠️ Контр-атака <b>{responder_name}</b> прервана.")
        elif response_kind == "heal":
            lines.append(f"⚠️ Хил <b>{responder_name}</b> прерван.")
    elif response_wins:
        if pending_kind == "attack":
            lines.append(f"➡️ Атака <b>{initiator_name}</b> промахнулась.")
        else:
            lines.append(f"❌ Лечение <b>{initiator_name}</b> не сработало.")
        if response_kind == "attack" and responder_ch is not None and initiator_ch is not None:
            dmg, before, label = _apply_attack_damage(responder_ch, initiator_ch)
            lines.append(
                f"💥 <b>{responder_name}</b> в ответ: урон <b>{dmg}</b> ({label}). "
                f"{html.escape(initiator_ch.name)}: {before} → "
                f"<b>{initiator_ch.current_hp}</b>/{initiator_ch.max_hp}."
            )
            if initiator_ch.current_hp <= 0:
                knocked_out = initiator_ch
        elif response_kind == "heal" and responder_ch is not None:
            heal_amt, before, label = _apply_heal_self(responder_ch, response_roll)
            lines.append(
                f"✅ <b>{responder_name}</b> {label}. "
                f"{html.escape(responder_ch.name)}: {before} → "
                f"<b>{responder_ch.current_hp}</b>/{responder_ch.max_hp} (+{responder_ch.current_hp - before})."
            )
    else:
        if pending_kind == "attack":
            lines.append(f"⚖️ Ничья — атака <b>{initiator_name}</b> мимо.")
        else:
            lines.append(f"⚖️ Ничья — лечение <b>{initiator_name}</b> не сработало.")
        if response_kind == "attack":
            lines.append(f"⚖️ Контр-атака <b>{responder_name}</b> мимо.")
        elif response_kind == "heal":
            lines.append(f"⚖️ Хил <b>{responder_name}</b> не сработал.")

    save_state()

    try:
        await bot.send_message(duel.chat_id, "\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        logging.warning("resolve pending: %s", exc)

    if knocked_out is not None:
        await _handle_knockout(bot, duel, knocked_out)

    await _refresh_duel_message(bot, duel)


@router.message(Command("attack"))
async def cmd_attack(message: Message, command: CommandObject, bot: Bot) -> None:
    remember_user(message.from_user)
    user_id = message.from_user.id

    duel = _get_active_duel(user_id)
    if duel is None:
        # Возможно, /attack вызван в активном командном бою.
        battle_id = user_battle.get(user_id)
        battle = battles.get(battle_id) if battle_id is not None else None
        if battle is not None and battle.status == BattleStatus.ACTIVE:
            pending = battle.pending_attacks.get(user_id)
            tgt_id, _tlbl, _terr = _parse_heal_team_target(message)
            if pending is not None and (
                tgt_id is None or tgt_id == pending.get("attacker_id")
            ):
                cap, roll, err = _parse_heal_cap(command)
                if cap is None or roll is None:
                    await message.answer(err or "Не понял макс кубика.")
                    return
                await _resolve_battle_pending(
                    bot, battle, user_id, "attack", roll, cap
                )
                return
            await _cmd_attack_team_battle(message, command, bot, battle, user_id)
            return
        await message.answer(
            "Команда /attack работает в активной дуэли (<code>/duel @username</code>) "
            "или в командном бою: <code>/attack @user &lt;макс&gt;</code>."
        )
        return

    attacker_ch = _duel_char_of(duel, user_id)
    if attacker_ch is None or attacker_ch.current_hp <= 0:
        await message.answer("Твой персонаж не может атаковать (выбыл или не найден).")
        return

    defender_id = _duel_other_id(duel, user_id)
    if defender_id is None:
        return
    defender_ch = _duel_char_of(duel, defender_id)
    if defender_ch is None or defender_ch.current_hp <= 0:
        await message.answer("Соперник уже выведен из боя.")
        return

    attack_cap, attack_roll, err = _parse_roll_arg(command)
    if attack_cap is None or attack_roll is None:
        await message.answer(err or "Не понял макс кубика.")
        return

    # Если есть pending-действие от соперника — разрешаем его контр-атакой.
    if duel.pending_attacker_id is not None and duel.pending_attacker_id != user_id:
        await _resolve_pending(bot, duel, "attack", attack_roll, attack_cap, user_id)
        return

    # Если ты сам в pending'е — жди ответа.
    if duel.pending_attacker_id == user_id:
        await message.answer(
            "Ты уже в pending-действии — жди ответа соперника или выходи через /yield."
        )
        return

    duel.pending_kind = "attack"
    duel.pending_attacker_id = user_id
    duel.pending_attack_cap = attack_cap
    duel.pending_attack_roll = attack_roll

    atk_name = html.escape(display_name(user_id))
    def_name = html.escape(display_name(defender_id))
    try:
        await bot.send_message(
            duel.chat_id,
            f"⚔️ <b>{atk_name}</b> ({html.escape(attacker_ch.name)}) атакует — "
            f"🎲 бот кинул <b>{attack_roll}</b> из 1–{attack_cap}.\n"
            f"🛡 <b>{def_name}</b> — ответь любым: "
            f"<code>/defend &lt;макс&gt;</code>, <code>/attack &lt;макс&gt;</code> или "
            f"<code>/heal &lt;макс&gt;</code>.",
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("attack: не удалось отправить сообщение: %s", exc)
    await _refresh_duel_message(bot, duel)


@router.message(Command("defend"))
async def cmd_defend(message: Message, command: CommandObject, bot: Bot) -> None:
    remember_user(message.from_user)
    user_id = message.from_user.id

    duel = _get_active_duel(user_id)
    if duel is None:
        # Возможно, /defend — ответ на pending-атаку в командном бою.
        battle_id = user_battle.get(user_id)
        battle = battles.get(battle_id) if battle_id is not None else None
        if battle is not None and battle.status == BattleStatus.ACTIVE:
            pending = battle.pending_attacks.get(user_id)
            if pending is None:
                await message.answer(
                    "Сейчас на тебя никто не нападал — нечего защищать."
                )
                return
            cap, roll, err = _parse_heal_cap(command)
            if cap is None or roll is None:
                await message.answer(err or "Не понял макс кубика.")
                return
            await _resolve_battle_pending(bot, battle, user_id, "defend", roll, cap)
            return
        await message.answer("Команда /defend работает только в активной дуэли.")
        return
    if duel.pending_attacker_id is None or duel.pending_kind is None:
        await message.answer("Сейчас нет атаки/лечения, на которое надо ответить.")
        return
    if duel.pending_attacker_id == user_id:
        await message.answer(
            "Ты сам инициировал действие — жди ответа соперника или таймаута."
        )
        return
    expected_defender = _duel_other_id(duel, duel.pending_attacker_id)
    if expected_defender != user_id:
        await message.answer("Это действие не на тебя.")
        return

    defender_cap, defender_roll, err = _parse_roll_arg(command)
    if defender_cap is None or defender_roll is None:
        await message.answer(err or "Не понял макс кубика.")
        return

    await _resolve_pending(bot, duel, "defend", defender_roll, defender_cap, user_id)


def _parse_heal_team_target(
    message: Message,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Парсит цель для /heal @user: возвращает (target_id, target_label, err).

    Если цель не указана — (None, None, None) (значит — само-хил).
    Если указана, но мы её не знаем (никогда не писал /start боту) — error."""
    # Reply имеет приоритет
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        if not u.is_bot:
            remember_user(u)
            label = "@" + u.username if u.username else (u.first_name or f"id{u.id}")
            return u.id, label, None

    if message.entities and message.text:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                u = ent.user
                remember_user(u)
                label = (
                    "@" + u.username if u.username else (u.first_name or f"id{u.id}")
                )
                return u.id, label, None
            if ent.type == "mention":
                mention_text = message.text[ent.offset : ent.offset + ent.length]
                uname = mention_text.lstrip("@")
                if BOT_USERNAME and uname.lower() == BOT_USERNAME.lower():
                    continue
                uid = username_to_id.get(uname.lower())
                if uid is None:
                    return (
                        None,
                        f"@{uname}",
                        f"Не нашёл @{uname}. Пусть он сначала напишет /start "
                        "этому боту, чтобы я его запомнил.",
                    )
                return uid, f"@{uname}", None
    return None, None, None


def _parse_heal_cap(command: CommandObject) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Ищет первое числовое слово в args /heal — это МАКС кубика. Возвращает (cap, roll, err)."""
    args = (command.args or "").split()
    for arg in args:
        candidate = arg.lstrip("@")
        try:
            cap = int(candidate)
        except ValueError:
            continue
        if cap < 1:
            return None, None, "МАКС кубика должен быть ≥ 1."
        if cap > 10_000_000:
            return None, None, "МАКС слишком большой (≥ 10 000 000)."
        return cap, random.randint(1, cap), None
    return (
        None,
        None,
        "Укажи МАКС кубика. Пример: <code>/heal @user 100</code> — бот кинет 1..100.",
    )


def _team_battle_heal_pct(
    healer_ch: Character, roll: int
) -> tuple[int, str]:
    """Возвращает (heal_pct, verdict) для /heal в командном бою.

    Лекарь: ролл > 30 → +15% (полное), иначе +5% (мини). Боец: ролл > 30 → +5%,
    иначе +2% (мини)."""
    if healer_ch.char_class == CharClass.HEALER:
        if roll > HEAL_LOW_ROLL_THRESHOLD:
            heal_pct = CLASS_STATS[CharClass.HEALER]["heal_pct"]
            return heal_pct, f"полное лечение (+{heal_pct}%)"
        return HEALER_LOW_HEAL_PCT, f"слабое лечение (+{HEALER_LOW_HEAL_PCT}%)"
    # Attacker / Fighter
    if roll > HEAL_LOW_ROLL_THRESHOLD:
        heal_pct = CLASS_STATS[CharClass.ATTACKER]["heal_pct"]
        return heal_pct, f"лечение Бойца (+{heal_pct}%)"
    return ATTACKER_LOW_HEAL_PCT, f"слабое лечение Бойца (+{ATTACKER_LOW_HEAL_PCT}%)"


def _team_battle_damage_pct(
    attacker_ch: Character, roll: int
) -> tuple[int, str]:
    """Возвращает (damage_pct, verdict) для /attack в командном бою.

    Боец: ролл > 30 → −15% (полный), иначе −5% (слабый). Лекарь: ролл > 30 → −5%,
    иначе −2% (слабый). Бонус сильного защитника по ОП учитывается отдельно."""
    if attacker_ch.char_class == CharClass.ATTACKER:
        if roll > HEAL_LOW_ROLL_THRESHOLD:
            pct = CLASS_STATS[CharClass.ATTACKER]["damage_pct"]
            return pct, f"полный удар (−{pct}%)"
        return ATTACKER_LOW_DAMAGE_PCT, f"слабый удар (−{ATTACKER_LOW_DAMAGE_PCT}%)"
    # HEALER
    if roll > HEAL_LOW_ROLL_THRESHOLD:
        pct = CLASS_STATS[CharClass.HEALER]["damage_pct"]
        return pct, f"удар Лекаря (−{pct}%)"
    return HEALER_LOW_DAMAGE_PCT, f"слабый удар Лекаря (−{HEALER_LOW_DAMAGE_PCT}%)"


async def _cmd_heal_team_battle(
    message: Message, command: CommandObject, bot: Bot, battle: Battle, user_id: int
) -> None:
    """Логика /heal в активном командном бою: только союзники, КД по классу, HoT для Лекаря."""
    sender_team = _battle_team_of(battle, user_id)
    if sender_team is None:
        await message.answer("Ты не в текущем командном бою.")
        return

    healer_name = battle.char_by_user.get(user_id)
    healer_ch = get_chars(user_id).get(healer_name) if healer_name else None
    if healer_ch is None:
        await message.answer("Твой персонаж в этом бою не найден.")
        return
    if healer_ch.current_hp <= 0:
        await message.answer("Твой персонаж без сознания и не может лечить.")
        return

    cap, roll, err = _parse_heal_cap(command)
    if cap is None or roll is None:
        await message.answer(err or "Не понял макс кубика.")
        return

    tgt_id, tgt_label_raw, tgt_err = _parse_heal_team_target(message)
    if tgt_err:
        await message.answer(tgt_err)
        return
    if tgt_id is None:
        # Цель не указана — лечим себя.
        tgt_id = user_id
        tgt_label_raw = display_name(user_id)

    target_team = _battle_team_of(battle, tgt_id)
    if target_team is None or target_team != sender_team:
        await message.answer(
            "🛑 В командном бою лечить можно только союзников по своей команде."
        )
        return

    target_char_name = battle.char_by_user.get(tgt_id)
    target_ch = get_chars(tgt_id).get(target_char_name) if target_char_name else None
    if target_ch is None:
        await message.answer("Союзник в этом бою не найден.")
        return
    if target_ch.current_hp <= 0:
        await message.answer(
            f"💀 {target_ch.name} без сознания — лечение не работает. Используй /jesus."
        )
        return

    healer_label = html.escape(display_name(user_id))
    target_label = html.escape(tgt_label_raw or display_name(tgt_id))
    target_char_html = html.escape(target_ch.name)
    is_healer = healer_ch.char_class == CharClass.HEALER
    self_heal = tgt_id == user_id
    target_html = "себя" if self_heal else f"<b>{target_label}</b> ({target_char_html})"

    cd_left = battle.heal_cooldown.get(user_id, 0)
    if cd_left > 0:
        # Заблокировано КД. Для Лекаря с активным HoT — применяем тик к ОРИГИНАЛЬНОЙ цели.
        lines = [
            f"🕒 <b>{healer_label}</b> пробует /heal: 🎲 <b>{roll}</b> из 1–{cap}, "
            f"но лечение на КД ({cd_left} ход(а))."
        ]
        if is_healer:
            hot = battle.heal_hot.get(user_id)
            if hot is not None:
                hot_target_id = hot.get("target_id")
                hot_target_char_name = hot.get("target_char")
                hot_target_ch = (
                    get_chars(hot_target_id).get(hot_target_char_name)
                    if hot_target_id is not None and hot_target_char_name
                    else None
                )
                if hot_target_ch is not None and hot_target_ch.current_hp > 0:
                    heal_amt = max(
                        1, (hot_target_ch.max_hp * HEALER_HOT_PERMILLE) // 1000
                    )
                    before = hot_target_ch.current_hp
                    hot_target_ch.current_hp = min(
                        hot_target_ch.max_hp, before + heal_amt
                    )
                    hot_label_raw = (
                        "ты сам" if hot_target_id == user_id else display_name(hot_target_id)
                    )
                    hot_label = html.escape(hot_label_raw)
                    lines.append(
                        f"🌿 Продолжение лечения: <b>{hot_label}</b> "
                        f"({html.escape(hot_target_ch.name)}) "
                        f"+{heal_amt} ХП ({HEALER_HOT_PERMILLE / 10:.1f}%): "
                        f"{before} → <b>{hot_target_ch.current_hp}</b>"
                        f"/{hot_target_ch.max_hp}."
                    )
                    hot["ticks_left"] = max(0, int(hot.get("ticks_left", 0)) - 1)
                    if hot["ticks_left"] <= 0:
                        battle.heal_hot.pop(user_id, None)
                else:
                    # Цель HoT недоступна — снимаем эффект.
                    battle.heal_hot.pop(user_id, None)

        battle.heal_cooldown[user_id] = cd_left - 1
        if battle.heal_cooldown[user_id] == 0:
            battle.heal_cooldown.pop(user_id, None)
            lines.append("✅ КД лечения закончится со следующего хода.")
        else:
            lines.append(
                f"⏳ КД лечения теперь: <b>{battle.heal_cooldown[user_id]}</b> ход(а)."
            )
        save_state()
        try:
            await message.answer("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logging.warning("heal (team battle, cd-tick): %s", exc)
        await _refresh_battle_message(bot, battle)
        return

    # КД = 0 — лечение успешно.
    heal_pct, verdict = _team_battle_heal_pct(healer_ch, roll)
    heal_amount = max(1, (target_ch.max_hp * heal_pct) // 100)
    before = target_ch.current_hp
    target_ch.current_hp = min(target_ch.max_hp, before + heal_amount)

    cd_turns = HEAL_COOLDOWN_BY_CLASS.get(healer_ch.char_class, 2)
    battle.heal_cooldown[user_id] = cd_turns

    hot_note = ""
    if is_healer:
        battle.heal_hot[user_id] = {
            "target_id": tgt_id,
            "target_char": target_ch.name,
            "ticks_left": HEALER_HOT_TICKS_AFTER_HEAL,
        }
        hot_note = (
            f"\n🌿 Продолжительность лечения на {target_html}: следующие "
            f"{HEALER_HOT_TICKS_AFTER_HEAL} раза, когда Лекарь во время КД пробует "
            f"/heal, цели прилетает +{HEALER_HOT_PERMILLE / 10:.1f}% ХП."
        )

    save_state()
    try:
        await message.answer(
            f"🌿 <b>{healer_label}</b> ({CLASS_LABELS[healer_ch.char_class]}) лечит "
            f"{target_html} — 🎲 <b>{roll}</b> из 1–{cap} — {verdict}.\n"
            f"❤️ {target_char_html}: {before} → <b>{target_ch.current_hp}</b>"
            f"/{target_ch.max_hp} (+{target_ch.current_hp - before}).\n"
            f"🕒 КД лечения: <b>{cd_turns}</b> хода.{hot_note}"
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("heal (team battle, apply): %s", exc)
    await _refresh_battle_message(bot, battle)


def _tick_battle_heal_cooldown(battle: Battle, user_id: int) -> Optional[int]:
    """Уменьшает КД лечения игрока на 1 при любом его действии (атака/защита/ответ).

    Возвращает оставшееся КД после тика (0 — если КД только что истёк), либо None
    если КД у пользователя не было."""
    cd = battle.heal_cooldown.get(user_id, 0)
    if cd <= 0:
        return None
    new_cd = cd - 1
    if new_cd <= 0:
        battle.heal_cooldown.pop(user_id, None)
        return 0
    battle.heal_cooldown[user_id] = new_cd
    return new_cd


async def _cmd_attack_team_battle(
    message: Message, command: CommandObject, bot: Bot, battle: Battle, user_id: int
) -> None:
    """/attack @user <макс> в командном бою.

    Поведение как в дуэлях: сохраняем pending и ждём ответ цели
    (<code>/defend</code>, <code>/attack</code>-контр или <code>/heal</code>-ответ).
    Урон применяется в _resolve_battle_pending по сравнению роллов."""
    sender_team = _battle_team_of(battle, user_id)
    if sender_team is None:
        await message.answer("Ты не в текущем командном бою.")
        return

    attacker_name = battle.char_by_user.get(user_id)
    attacker_ch = get_chars(user_id).get(attacker_name) if attacker_name else None
    if attacker_ch is None:
        await message.answer("Твой персонаж в этом бою не найден.")
        return
    if attacker_ch.current_hp <= 0:
        await message.answer("Твой персонаж без сознания и не может атаковать.")
        return

    cap, roll, err = _parse_heal_cap(command)
    if cap is None or roll is None:
        await message.answer(
            err or "Не понял макс кубика. Пример: <code>/attack @user 100</code>."
        )
        return

    tgt_id, tgt_label_raw, tgt_err = _parse_heal_team_target(message)
    if tgt_err:
        await message.answer(tgt_err)
        return
    if tgt_id is None or tgt_id == user_id:
        await message.answer(
            "Укажи цель: <code>/attack @user &lt;макс&gt;</code> (или ответом на сообщение врага)."
        )
        return

    target_team = _battle_team_of(battle, tgt_id)
    if target_team is None:
        await message.answer("Этот игрок не в текущем командном бою.")
        return
    if target_team == sender_team:
        await message.answer(
            "🛑 Это союзник по твоей команде. Атаковать можно только врагов."
        )
        return

    target_char_name = battle.char_by_user.get(tgt_id)
    target_ch = get_chars(tgt_id).get(target_char_name) if target_char_name else None
    if target_ch is None:
        await message.answer("Цель в этом бою не найдена.")
        return
    if target_ch.current_hp <= 0:
        await message.answer(f"💀 {target_ch.name} уже выведен из боя.")
        return

    # У цели уже есть незакрытый pending — пусть сначала ответит.
    existing = battle.pending_attacks.get(tgt_id)
    if existing is not None and existing.get("attacker_id") != user_id:
        prev_atk = display_name(existing["attacker_id"])
        await message.answer(
            f"⏳ У <b>{html.escape(tgt_label_raw or display_name(tgt_id))}</b> уже есть "
            f"pending-атака от <b>{html.escape(prev_atk)}</b>. "
            "Пусть сначала ответит /defend, /attack или /heal."
        )
        return

    # Запоминаем pending и ждём ответа цели.
    battle.pending_attacks[tgt_id] = {
        "attacker_id": user_id,
        "cap": cap,
        "roll": roll,
    }
    cd_after = _tick_battle_heal_cooldown(battle, user_id)
    save_state()

    atk_label = html.escape(display_name(user_id))
    atk_char_html = html.escape(attacker_ch.name)
    tgt_label = html.escape(tgt_label_raw or display_name(tgt_id))
    tgt_char_html = html.escape(target_ch.name)
    cd_note = ""
    if cd_after == 0:
        cd_note = "\n✅ КД лечения откатился."
    elif cd_after is not None:
        cd_note = f"\n⏳ КД лечения теперь: <b>{cd_after}</b> ход(а)."
    try:
        await bot.send_message(
            battle.chat_id,
            f"⚔️ <b>{atk_label}</b> ({atk_char_html}) атакует "
            f"<b>{tgt_label}</b> ({tgt_char_html}) — 🎲 бот кинул <b>{roll}</b> "
            f"из 1–{cap}.\n"
            f"🛡 <b>{tgt_label}</b> — ответь любым: "
            f"<code>/defend &lt;макс&gt;</code>, <code>/attack &lt;макс&gt;</code> "
            f"(контр) или <code>/heal &lt;макс&gt;</code> (хил-ответ).{cd_note}",
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("team-battle attack notice: %s", exc)
    await _refresh_battle_message(bot, battle)


async def _resolve_battle_pending(
    bot: Bot,
    battle: Battle,
    responder_id: int,
    response_kind: str,
    response_roll: int,
    response_cap: int,
) -> None:
    """Резолвит pending-атаку в командном бою после ответа цели.

    response_kind: 'defend' | 'attack' | 'heal'. У кого больше ролл — тот сработал.
    Ничья — оба промахнулись. Урон/хил применяются по правилам командного боя
    (полный/слабый в зависимости от ролла и класса)."""
    pending = battle.pending_attacks.pop(responder_id, None)
    if pending is None:
        return
    initiator_id = pending["attacker_id"]
    pending_cap = pending["cap"]
    pending_roll = pending["roll"]

    initiator_char_name = battle.char_by_user.get(initiator_id)
    initiator_ch = (
        get_chars(initiator_id).get(initiator_char_name)
        if initiator_char_name
        else None
    )
    responder_char_name = battle.char_by_user.get(responder_id)
    responder_ch = (
        get_chars(responder_id).get(responder_char_name)
        if responder_char_name
        else None
    )

    initiator_name = html.escape(display_name(initiator_id))
    responder_name = html.escape(display_name(responder_id))

    init_label = f"⚔️ <b>{initiator_name}</b> атака"
    if response_kind == "defend":
        resp_label = f"🛡 <b>{responder_name}</b> защита"
    elif response_kind == "attack":
        resp_label = f"⚔️ <b>{responder_name}</b> контр-атака"
    else:
        resp_label = f"🩹 <b>{responder_name}</b> хил-ответ"

    lines = [
        f"{init_label}: 🎲 <b>{pending_roll}</b> (из 1–{pending_cap})",
        f"{resp_label}: 🎲 <b>{response_roll}</b> (из 1–{response_cap})",
    ]

    pending_wins = pending_roll > response_roll
    response_wins = response_roll > pending_roll
    knocked_targets: list[tuple[int, Character]] = []

    if pending_wins and initiator_ch is not None and responder_ch is not None:
        # Удар инициатора прошёл.
        base_pct, verdict = _team_battle_damage_pct(initiator_ch, pending_roll)
        reduction_pp = _hp_advantage_reduction(initiator_ch.max_hp, responder_ch.max_hp)
        eff = max(1, base_pct - reduction_pp)
        dmg = max(1, (responder_ch.max_hp * eff) // 100)
        before = responder_ch.current_hp
        responder_ch.current_hp = max(0, before - dmg)
        red_note = f" (бонус по ОП: −{reduction_pp} п.п.)" if reduction_pp > 0 else ""
        lines.append(
            f"💥 <b>{initiator_name}</b> попал — {verdict}{red_note}. "
            f"{html.escape(responder_ch.name)}: {before} → "
            f"<b>{responder_ch.current_hp}</b>/{responder_ch.max_hp} (−{dmg})."
        )
        if responder_ch.current_hp <= 0:
            responder_ch.in_battle = False
            knocked_targets.append((responder_id, responder_ch))
        if response_kind == "attack":
            lines.append(f"⚠️ Контр-атака <b>{responder_name}</b> прервана.")
        elif response_kind == "heal":
            lines.append(f"⚠️ Хил <b>{responder_name}</b> прерван.")
    elif response_wins:
        lines.append(f"➡️ Атака <b>{initiator_name}</b> промахнулась.")
        if response_kind == "attack" and initiator_ch is not None and responder_ch is not None:
            base_pct, verdict = _team_battle_damage_pct(responder_ch, response_roll)
            reduction_pp = _hp_advantage_reduction(responder_ch.max_hp, initiator_ch.max_hp)
            eff = max(1, base_pct - reduction_pp)
            dmg = max(1, (initiator_ch.max_hp * eff) // 100)
            before = initiator_ch.current_hp
            initiator_ch.current_hp = max(0, before - dmg)
            red_note = f" (бонус по ОП: −{reduction_pp} п.п.)" if reduction_pp > 0 else ""
            lines.append(
                f"💥 <b>{responder_name}</b> в ответ — {verdict}{red_note}. "
                f"{html.escape(initiator_ch.name)}: {before} → "
                f"<b>{initiator_ch.current_hp}</b>/{initiator_ch.max_hp} (−{dmg})."
            )
            if initiator_ch.current_hp <= 0:
                initiator_ch.in_battle = False
                knocked_targets.append((initiator_id, initiator_ch))
        elif response_kind == "heal" and responder_ch is not None:
            # Само-хил по тому же распределению что и /heal в команд. бою.
            heal_pct, hverdict = _team_battle_heal_pct(responder_ch, response_roll)
            heal_amt = max(1, (responder_ch.max_hp * heal_pct) // 100)
            before = responder_ch.current_hp
            responder_ch.current_hp = min(responder_ch.max_hp, before + heal_amt)
            lines.append(
                f"✅ <b>{responder_name}</b> {hverdict}. "
                f"{html.escape(responder_ch.name)}: {before} → "
                f"<b>{responder_ch.current_hp}</b>/{responder_ch.max_hp} "
                f"(+{responder_ch.current_hp - before})."
            )
    else:
        lines.append(f"⚖️ Ничья — атака <b>{initiator_name}</b> мимо.")
        if response_kind == "attack":
            lines.append(f"⚖️ Контр-атака <b>{responder_name}</b> мимо.")
        elif response_kind == "heal":
            lines.append(f"⚖️ Хил <b>{responder_name}</b> не сработал.")

    # Любое действие в бою (включая ответ на pending) тикает КД лечения у того,
    # кто это действие совершил. Для /heal-ответа КД не тикаем тут — это сам хил,
    # его КД управляется логикой /heal (cmd_heal/_cmd_heal_team_battle).
    if response_kind != "heal":
        cd_after = _tick_battle_heal_cooldown(battle, responder_id)
        if cd_after == 0:
            lines.append(f"✅ <b>{responder_name}</b>: КД лечения откатился.")
        elif cd_after is not None:
            lines.append(
                f"⏳ <b>{responder_name}</b>: КД лечения теперь {cd_after} ход(а)."
            )

    save_state()
    try:
        await bot.send_message(battle.chat_id, "\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        logging.warning("battle resolve pending: %s", exc)

    if knocked_targets:
        for uid, ch in knocked_targets:
            # Снимем все pending'и от/к нокаут-цели.
            battle.pending_attacks.pop(uid, None)
            for tid, info in list(battle.pending_attacks.items()):
                if info.get("attacker_id") == uid:
                    battle.pending_attacks.pop(tid, None)

    await _refresh_battle_message(bot, battle)
    await _check_battle_progress(bot, battle)


@router.message(Command("heal"))
async def cmd_heal(message: Message, command: CommandObject, bot: Bot) -> None:
    remember_user(message.from_user)
    user_id = message.from_user.id

    duel = _get_active_duel(user_id)
    if duel is None:
        # Возможно, /heal вызван в активном командном бою.
        battle_id = user_battle.get(user_id)
        battle = battles.get(battle_id) if battle_id is not None else None
        if battle is not None and battle.status == BattleStatus.ACTIVE:
            # Если на пользователе висит pending и он лечит себя/без цели —
            # это хил-ответ; иначе обычный союз-хил.
            pending = battle.pending_attacks.get(user_id)
            tgt_id, _tlbl, _terr = _parse_heal_team_target(message)
            if pending is not None and (tgt_id is None or tgt_id == user_id):
                cap, roll, err = _parse_heal_cap(command)
                if cap is None or roll is None:
                    await message.answer(err or "Не понял макс кубика.")
                    return
                await _resolve_battle_pending(
                    bot, battle, user_id, "heal", roll, cap
                )
                return
            await _cmd_heal_team_battle(message, command, bot, battle, user_id)
            return
        await message.answer(
            "Команда /heal работает в активной дуэли или командном бою. "
            "Начни через /duel или /buttle."
        )
        return

    healer_ch = _duel_char_of(duel, user_id)
    if healer_ch is None or healer_ch.current_hp <= 0:
        await message.answer("Твой персонаж не может лечить (выбыл или не найден).")
        return

    heal_cap, heal_roll, err = _parse_roll_arg(command)
    if heal_cap is None or heal_roll is None:
        await message.answer(err or "Не понял макс кубика.")
        return

    # Ответ на pending соперника — хил-ответ разрешает pending.
    if duel.pending_attacker_id is not None and duel.pending_attacker_id != user_id:
        await _resolve_pending(bot, duel, "heal", heal_roll, heal_cap, user_id)
        return

    if duel.pending_attacker_id == user_id:
        await message.answer(
            "Ты уже в pending-действии — жди ответа соперника или выходи через /yield."
        )
        return

    healer_name = html.escape(display_name(user_id))
    is_healer = healer_ch.char_class == CharClass.HEALER

    if is_healer:
        # Лекарь лечится соло — без pending.
        heal_amt, before, verdict = _apply_heal_self(healer_ch, heal_roll)
        save_state()
        try:
            await bot.send_message(
                duel.chat_id,
                f"🌿 <b>{healer_name}</b> (Лекарь) лечение: 🎲 <b>{heal_roll}</b> "
                f"из 1–{heal_cap} — {verdict}.\n"
                f"❤️ {html.escape(healer_ch.name)}: {before} → "
                f"<b>{healer_ch.current_hp}</b>/{healer_ch.max_hp} (+{healer_ch.current_hp - before}).",
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("heal (healer): %s", exc)
        await _refresh_duel_message(bot, duel)
        return

    # Не-Лекарь: ставим pending heal_resist и ждём ответа соперника.
    opp_id = _duel_other_id(duel, user_id)
    if opp_id is None:
        return
    duel.pending_kind = "heal_resist"
    duel.pending_attacker_id = user_id
    duel.pending_attack_cap = heal_cap
    duel.pending_attack_roll = heal_roll

    opp_name = html.escape(display_name(opp_id))
    try:
        await bot.send_message(
            duel.chat_id,
            f"🩹 <b>{healer_name}</b> ({CLASS_LABELS[healer_ch.char_class]}) "
            f"пытается полечиться — 🎲 бот кинул <b>{heal_roll}</b> из 1–{heal_cap}.\n"
            f"🛡 <b>{opp_name}</b> — ответь любым: "
            f"<code>/defend &lt;макс&gt;</code>, <code>/attack &lt;макс&gt;</code> или "
            f"<code>/heal &lt;макс&gt;</code>.",
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("heal (non-healer pending): %s", exc)
    await _refresh_duel_message(bot, duel)


@router.message(Command("roll"))
async def cmd_roll(message: Message, command: CommandObject) -> None:
    remember_user(message.from_user)
    user_id = message.from_user.id
    name = html.escape(display_name(user_id))
    ch = get_active(user_id)
    label = f"<b>{name}</b>"
    if ch is not None:
        label += f" [{html.escape(ch.name)}]"

    args = (command.args or "").strip()
    if args:
        first = args.split()[0]
        try:
            cap = int(first)
        except ValueError:
            await message.answer(
                f"«{html.escape(first)}» — не число. Используй "
                "<code>/roll &lt;макс&gt;</code> (бот кинет 1..макс) или "
                "просто <code>/roll</code> (бот кинет 1–100)."
            )
            return
        if cap < 1 or cap > 10_000_000:
            await message.answer("Макс кубика должен быть от 1 до 10 000 000.")
            return
        value = random.randint(1, cap)
        await message.answer(f"🎲 {label} роллит: <b>{value}</b>/{cap}")
        return

    value = _roll()
    await message.answer(f"🎲 {label} роллит: <b>{value}</b>/{ROLL_MAX}")


# ---------------------------------------------------------------------------
# Командный бой /buttle
# ---------------------------------------------------------------------------


def _team_label(idx: int) -> str:
    emoji = TEAM_EMOJIS[idx] if 0 <= idx < len(TEAM_EMOJIS) else "⚫"
    return f"{emoji} Команда {idx + 1}"


def _format_time_left(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _battle_team_of(battle: Battle, user_id: int) -> Optional[int]:
    for idx, members in battle.teams.items():
        if user_id in members:
            return idx
    return None


def _battle_team_size(battle: Battle, team_idx: int) -> int:
    return len(battle.teams.get(team_idx, []))


def _battle_total_players(battle: Battle) -> int:
    return sum(len(m) for m in battle.teams.values())


def _battle_alive_teams(battle: Battle) -> list[int]:
    """Список team_idx, где есть хотя бы один живой персонаж."""
    alive: list[int] = []
    for idx in range(battle.num_teams):
        members = battle.teams.get(idx, [])
        for uid in members:
            name = battle.char_by_user.get(uid)
            if name is None:
                continue
            ch = get_chars(uid).get(name)
            if ch is not None and ch.current_hp > 0:
                alive.append(idx)
                break
    return alive


def _can_start_battle(battle: Battle) -> bool:
    non_empty = sum(1 for m in battle.teams.values() if m)
    return non_empty >= 2


def render_battle(battle: Battle, time_left: Optional[float] = None) -> str:
    if battle.status == BattleStatus.REGISTERING:
        header = "⚔️ <b>Командный бой — набор</b>"
        sub_lines = [
            f"Инициатор: {html.escape(display_name(battle.initiator_id))}",
            f"Команд: <b>{battle.num_teams}</b> · макс. <b>{MAX_BATTLE_TEAM_SIZE}</b> чел./команда",
        ]
        if time_left is not None:
            sub_lines.append(f"До авто-старта: <b>{_format_time_left(time_left)}</b>")
        sub = "\n".join(sub_lines)
    elif battle.status == BattleStatus.ACTIVE:
        header = "⚔️ <b>КОМАНДНЫЙ БОЙ В РАЗГАРЕ</b>"
        sub = (
            "<i>Бьёте врагов: <code>/attack @user &lt;макс&gt;</code>. "
            "Лечите союзников: <code>/heal @user &lt;макс&gt;</code>. "
            f"КД лечения: Лекарь — "
            f"{HEAL_COOLDOWN_BY_CLASS[CharClass.HEALER]} хода, Боец — "
            f"{HEAL_COOLDOWN_BY_CLASS[CharClass.ATTACKER]} хода. Выйти — /yield.</i>"
        )
    else:
        winners = _battle_alive_teams(battle)
        if len(winners) == 1:
            header = f"🏆 <b>ПОБЕДА: {_team_label(winners[0])}!</b>"
        elif not winners:
            header = "💥 <b>Бой окончен — ничья (никого не осталось в строю)</b>"
        else:
            header = "💥 <b>Бой завершён</b>"
        sub = ""

    lines: list[str] = [header]
    if sub:
        lines.append("")
        lines.append(sub)

    for idx in range(battle.num_teams):
        members = battle.teams.get(idx, [])
        size = len(members)
        team_header = f"\n<b>{_team_label(idx)}</b> ({size}/{MAX_BATTLE_TEAM_SIZE}):"
        lines.append(team_header)
        if not members:
            lines.append("  <i>(пусто)</i>")
            continue
        for uid in members:
            uname = html.escape(display_name(uid))
            char_name = battle.char_by_user.get(uid)
            ch = get_chars(uid).get(char_name) if char_name else None
            if ch is None:
                lines.append(f"  <i>{uname} — персонаж недоступен</i>")
                continue
            emoji = "❤️" if ch.current_hp > 0 else "💀"
            ch_name = html.escape(ch.name)
            lines.append(
                f"  {emoji} {uname} [{ch_name}] {CLASS_LABELS[ch.char_class]} "
                f"<b>{ch.current_hp}</b>/{ch.max_hp}"
            )
    return "\n".join(lines)


def battle_register_kb(battle: Battle) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    team_buttons: list[InlineKeyboardButton] = []
    for idx in range(battle.num_teams):
        emoji = TEAM_EMOJIS[idx] if 0 <= idx < len(TEAM_EMOJIS) else "⚫"
        size = _battle_team_size(battle, idx)
        team_buttons.append(
            InlineKeyboardButton(
                text=f"{emoji} В команду {idx + 1} ({size}/{MAX_BATTLE_TEAM_SIZE})",
                callback_data=f"buttle:join:{battle.battle_id}:{idx}",
            )
        )
    # По 2 кнопки в ряд.
    for i in range(0, len(team_buttons), 2):
        rows.append(team_buttons[i:i + 2])

    rows.append([
        InlineKeyboardButton(
            text="🚪 Выйти из команды",
            callback_data=f"buttle:leave:{battle.battle_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="▶️ Начать сейчас",
            callback_data=f"buttle:start:{battle.battle_id}",
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"buttle:cancel:{battle.battle_id}",
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _refresh_battle_message(
    bot: Bot,
    battle: Battle,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    time_left: Optional[float] = None,
    drop_markup: bool = False,
) -> None:
    if battle.message_id is None:
        return
    if not drop_markup and reply_markup is None and battle.status == BattleStatus.REGISTERING:
        reply_markup = battle_register_kb(battle)
    try:
        await bot.edit_message_text(
            text=render_battle(battle, time_left=time_left),
            chat_id=battle.chat_id,
            message_id=battle.message_id,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logging.warning("Не удалось обновить сообщение боя: %s", exc)


async def _cancel_battle_timer(battle_id: int) -> None:
    task = _battle_timers.pop(battle_id, None)
    if task is not None and not task.done():
        task.cancel()


def _cleanup_battle(battle: Battle, finished: bool = True) -> None:
    """Снимает блокировки с участников и (если finished) переводит бой в FINISHED."""
    for uid in list(battle.char_by_user.keys()):
        if user_battle.get(uid) == battle.battle_id:
            user_battle.pop(uid, None)
        name = battle.char_by_user.get(uid)
        if name:
            ch = get_chars(uid).get(name)
            if ch is not None:
                ch.in_battle = False
    if finished:
        battle.status = BattleStatus.FINISHED
    save_state()


async def _battle_timer(battle_id: int, deadline: float, bot: Bot) -> None:
    """Периодически обновляет сообщение и автозапускает / отменяет бой по таймеру."""
    try:
        loop = asyncio.get_running_loop()
        last_refresh = loop.time()
        while True:
            battle = battles.get(battle_id)
            if battle is None or battle.status != BattleStatus.REGISTERING:
                return
            now = loop.time()
            time_left = deadline - now
            if time_left <= 0:
                break
            # Раз в ~30 секунд (или реже) — обновляем счётчик в сообщении.
            if now - last_refresh >= 30:
                await _refresh_battle_message(bot, battle, time_left=time_left)
                last_refresh = now
            await asyncio.sleep(min(5.0, max(0.5, time_left)))
        battle = battles.get(battle_id)
        if battle is None or battle.status != BattleStatus.REGISTERING:
            return
        if _can_start_battle(battle):
            await _start_battle(bot, battle, reason="время вышло")
        else:
            await _cancel_battle(bot, battle, reason="время вышло, не набралось команд")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logging.warning("Ошибка в таймере боя %s: %s", battle_id, exc)
    finally:
        _battle_timers.pop(battle_id, None)


async def _start_battle(bot: Bot, battle: Battle, reason: str = "") -> None:
    if not _can_start_battle(battle):
        await _cancel_battle(bot, battle, reason="недостаточно команд")
        return
    battle.status = BattleStatus.ACTIVE
    for uid, name in battle.char_by_user.items():
        ch = get_chars(uid).get(name)
        if ch is not None and ch.current_hp > 0:
            ch.in_battle = True
    save_state()
    await _cancel_battle_timer(battle.battle_id)
    await _refresh_battle_message(bot, battle, drop_markup=True)
    intro = "⚔️ <b>Бой начался!</b>"
    if reason:
        intro += f" <i>({reason})</i>"
    intro += (
        "\nДействия: бить врагов — <code>/attack @user &lt;макс&gt;</code>, "
        "лечить союзников — <code>/heal @user &lt;макс&gt;</code>. "
        f"КД лечения: Лекарь — "
        f"{HEAL_COOLDOWN_BY_CLASS[CharClass.HEALER]} хода, Боец — "
        f"{HEAL_COOLDOWN_BY_CLASS[CharClass.ATTACKER]} хода. Выйти — /yield."
    )
    try:
        await bot.send_message(battle.chat_id, intro)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Не удалось отправить сообщение о старте боя: %s", exc)


async def _cancel_battle(bot: Bot, battle: Battle, reason: str = "отменено") -> None:
    _cleanup_battle(battle, finished=True)
    await _cancel_battle_timer(battle.battle_id)
    try:
        if battle.message_id is not None:
            await bot.edit_message_text(
                text=f"❌ <b>Командный бой отменён</b> ({reason}).",
                chat_id=battle.chat_id,
                message_id=battle.message_id,
                reply_markup=None,
            )
    except Exception as exc:  # noqa: BLE001
        logging.warning("Не удалось обновить сообщение отменённого боя: %s", exc)


async def _check_battle_progress(bot: Bot, battle: Battle) -> None:
    """Если осталась только одна (или ноль) команда с живыми — завершаем бой."""
    if battle.status != BattleStatus.ACTIVE:
        return
    alive = _battle_alive_teams(battle)
    if len(alive) <= 1:
        _cleanup_battle(battle, finished=True)
        await _refresh_battle_message(bot, battle, drop_markup=True)
        if len(alive) == 1:
            try:
                await bot.send_message(
                    battle.chat_id,
                    f"🏆 <b>Победила {_team_label(alive[0])}!</b>",
                )
            except Exception:  # noqa: BLE001
                pass


async def _yield_from_battle(
    bot: Bot, message: Message, battle: Battle, user_id: int
) -> None:
    quitter = display_name(user_id)
    if battle.status == BattleStatus.REGISTERING:
        cur_team = _battle_team_of(battle, user_id)
        if cur_team is not None:
            battle.teams[cur_team].remove(user_id)
        battle.char_by_user.pop(user_id, None)
        user_battle.pop(user_id, None)
        save_state()

        # Если ушёл инициатор и больше никого — отменяем сразу.
        if user_id == battle.initiator_id and _battle_total_players(battle) == 0:
            await _cancel_battle(bot, battle, reason="инициатор вышел")
            await message.answer("Готово — бой отменён, ты вышел.")
            return

        time_left = max(0.0, battle.deadline_ts - asyncio.get_running_loop().time())
        await _refresh_battle_message(bot, battle, time_left=time_left)
        try:
            await bot.send_message(
                battle.chat_id,
                f"🚪 {html.escape(quitter)} вышел из набора в командный бой.",
            )
        except Exception:  # noqa: BLE001
            pass
        if message.chat.id != battle.chat_id:
            await message.answer("Готово — ты вышел из набора.")
        return

    if battle.status == BattleStatus.ACTIVE:
        name = battle.char_by_user.get(user_id)
        ch = get_chars(user_id).get(name) if name else None
        if ch is not None and ch.current_hp > 0:
            ch.current_hp = 0
            ch.in_battle = False
            save_state()
        # Чистим pending'и с участием выбывшего.
        battle.pending_attacks.pop(user_id, None)
        for tid, info in list(battle.pending_attacks.items()):
            if info.get("attacker_id") == user_id:
                battle.pending_attacks.pop(tid, None)
        try:
            await bot.send_message(
                battle.chat_id,
                f"🏳️ {html.escape(quitter)} сдался и выбыл из командного боя.",
            )
        except Exception:  # noqa: BLE001
            pass
        await _refresh_battle_message(bot, battle)
        await _check_battle_progress(bot, battle)
        if message.chat.id != battle.chat_id:
            await message.answer("Готово — ты сдался и выбыл из боя.")
        return

    # FINISHED — на всякий случай чистим запись пользователя.
    user_battle.pop(user_id, None)
    await message.answer("Бой уже завершён.")


@router.message(Command("buttle"))
async def cmd_buttle(message: Message, command: CommandObject, bot: Bot) -> None:
    remember_user(message.from_user)
    sender = message.from_user
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "Командный бой /buttle работает только в групповых чатах."
        )
        return

    args = (command.args or "").split()
    num_teams = MIN_BATTLE_TEAMS
    timeout_sec = DEFAULT_BATTLE_TIMEOUT_SEC
    if args:
        try:
            num_teams = int(args[0])
        except ValueError:
            await message.answer(
                f"Использование: <code>/buttle [команд] [минут]</code>. "
                f"Команд: {MIN_BATTLE_TEAMS}–{MAX_BATTLE_TEAMS}, "
                f"минут: {MIN_BATTLE_TIMEOUT_MIN}–{MAX_BATTLE_TIMEOUT_MIN}."
            )
            return
        if not (MIN_BATTLE_TEAMS <= num_teams <= MAX_BATTLE_TEAMS):
            await message.answer(
                f"Количество команд: от {MIN_BATTLE_TEAMS} до {MAX_BATTLE_TEAMS}."
            )
            return
    if len(args) >= 2:
        try:
            mins = int(args[1])
        except ValueError:
            await message.answer(
                f"Минуты — целое число от {MIN_BATTLE_TIMEOUT_MIN} до {MAX_BATTLE_TIMEOUT_MIN}."
            )
            return
        if mins < MIN_BATTLE_TIMEOUT_MIN or mins > MAX_BATTLE_TIMEOUT_MIN:
            await message.answer(
                f"Минуты — от {MIN_BATTLE_TIMEOUT_MIN} до {MAX_BATTLE_TIMEOUT_MIN}."
            )
            return
        timeout_sec = mins * 60

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
            f"{display_name(sender.id)}, ты сейчас в дуэли. Заверши её прежде."
        )
        return
    if sender.id in user_battle:
        await message.answer(
            f"{display_name(sender.id)}, ты уже в другом командном бою."
        )
        return

    loop = asyncio.get_running_loop()
    battle = Battle(
        battle_id=make_battle_id(),
        chat_id=message.chat.id,
        initiator_id=sender.id,
        num_teams=num_teams,
        teams={i: [] for i in range(num_teams)},
        char_by_user={},
        status=BattleStatus.REGISTERING,
        deadline_ts=loop.time() + timeout_sec,
    )
    battle.teams[0].append(sender.id)
    battle.char_by_user[sender.id] = sender_char.name
    battles[battle.battle_id] = battle
    user_battle[sender.id] = battle.battle_id

    sent = await message.answer(
        render_battle(battle, time_left=float(timeout_sec)),
        reply_markup=battle_register_kb(battle),
    )
    battle.message_id = sent.message_id

    task = asyncio.create_task(_battle_timer(battle.battle_id, battle.deadline_ts, bot))
    _battle_timers[battle.battle_id] = task


@router.callback_query(F.data.startswith("buttle:join:"))
async def on_buttle_join(cb: CallbackQuery, bot: Bot) -> None:
    remember_user(cb.from_user)
    parts = (cb.data or "").split(":")
    if len(parts) != 4:
        await cb.answer()
        return
    try:
        battle_id = int(parts[2])
        team_idx = int(parts[3])
    except ValueError:
        await cb.answer()
        return
    battle = battles.get(battle_id)
    if battle is None or battle.status != BattleStatus.REGISTERING:
        await cb.answer("Набор уже завершён.", show_alert=True)
        return
    if team_idx < 0 or team_idx >= battle.num_teams:
        await cb.answer("Такой команды нет.", show_alert=True)
        return

    user_id = cb.from_user.id

    if user_id in user_duel:
        await cb.answer("Ты сейчас в дуэли — заверши её сначала.", show_alert=True)
        return
    other_bid = user_battle.get(user_id)
    if other_bid is not None and other_bid != battle_id:
        await cb.answer("Ты уже в другом командном бою.", show_alert=True)
        return

    ch = get_active(user_id)
    if ch is None:
        await cb.answer(
            "Сначала зарегистрируй персонажа в личке у бота: /start",
            show_alert=True,
        )
        return
    if ch.current_hp <= 0:
        await cb.answer(
            "Активный персонаж без сознания. Восстанови ХП в /persona.",
            show_alert=True,
        )
        return

    cur_team = _battle_team_of(battle, user_id)
    if cur_team == team_idx:
        await cb.answer(f"Ты уже в команде {team_idx + 1}.", show_alert=False)
        return
    if _battle_team_size(battle, team_idx) >= MAX_BATTLE_TEAM_SIZE:
        await cb.answer(
            f"В этой команде уже {MAX_BATTLE_TEAM_SIZE} человек — максимум.",
            show_alert=True,
        )
        return

    if cur_team is not None:
        battle.teams[cur_team].remove(user_id)
    battle.teams[team_idx].append(user_id)
    battle.char_by_user[user_id] = ch.name
    user_battle[user_id] = battle_id
    save_state()

    time_left = max(0.0, battle.deadline_ts - asyncio.get_running_loop().time())
    await _refresh_battle_message(bot, battle, time_left=time_left)
    await cb.answer(f"Ты в команде {team_idx + 1} ⚔️")


@router.callback_query(F.data.startswith("buttle:leave:"))
async def on_buttle_leave(cb: CallbackQuery, bot: Bot) -> None:
    remember_user(cb.from_user)
    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await cb.answer()
        return
    try:
        battle_id = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    battle = battles.get(battle_id)
    if battle is None or battle.status != BattleStatus.REGISTERING:
        await cb.answer("Набор уже завершён.", show_alert=True)
        return
    user_id = cb.from_user.id
    cur_team = _battle_team_of(battle, user_id)
    if cur_team is None:
        await cb.answer("Ты не в команде.", show_alert=True)
        return
    battle.teams[cur_team].remove(user_id)
    battle.char_by_user.pop(user_id, None)
    if user_battle.get(user_id) == battle_id:
        user_battle.pop(user_id, None)
    save_state()

    # Если ушёл инициатор и больше никого — отменяем бой.
    if user_id == battle.initiator_id and _battle_total_players(battle) == 0:
        await _cancel_battle(bot, battle, reason="инициатор вышел")
        await cb.answer("Бой отменён.")
        return

    time_left = max(0.0, battle.deadline_ts - asyncio.get_running_loop().time())
    await _refresh_battle_message(bot, battle, time_left=time_left)
    await cb.answer("Ты вышел из команды.")


@router.callback_query(F.data.startswith("buttle:start:"))
async def on_buttle_start(cb: CallbackQuery, bot: Bot) -> None:
    remember_user(cb.from_user)
    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await cb.answer()
        return
    try:
        battle_id = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    battle = battles.get(battle_id)
    if battle is None or battle.status != BattleStatus.REGISTERING:
        await cb.answer("Набор уже завершён.", show_alert=True)
        return
    if cb.from_user.id != battle.initiator_id:
        await cb.answer("Только инициатор может начать бой досрочно.", show_alert=True)
        return
    if not _can_start_battle(battle):
        await cb.answer(
            "Нужно минимум 2 команды с участниками.",
            show_alert=True,
        )
        return
    await cb.answer("Стартуем!")
    await _start_battle(bot, battle, reason="досрочный старт")


@router.callback_query(F.data.startswith("buttle:cancel:"))
async def on_buttle_cancel(cb: CallbackQuery, bot: Bot) -> None:
    remember_user(cb.from_user)
    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await cb.answer()
        return
    try:
        battle_id = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    battle = battles.get(battle_id)
    if battle is None or battle.status != BattleStatus.REGISTERING:
        await cb.answer("Уже неактуально.", show_alert=True)
        return
    if cb.from_user.id != battle.initiator_id:
        await cb.answer("Только инициатор может отменить.", show_alert=True)
        return
    await cb.answer("Отменено.")
    await _cancel_battle(bot, battle, reason="инициатор отменил")


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
        "/duel @username — вызвать игрока на дуэль 1×1 (только в группах)\n"
        "    └ альтернативно: /duel в ответ на сообщение игрока\n"
        "/attack &lt;макс&gt; — атаковать в дуэли: бот кидает 1..макс (например <code>/attack 100</code>)\n"
        "/attack @user &lt;макс&gt; — в командном бою бить врага (ролл > 30 — полный урон, ≤ 30 — слабый)\n"
        "/defend &lt;макс&gt; — ответить на атаку/лечение соперника: бот кидает 1..макс\n"
        "/heal &lt;макс&gt; — лечиться в дуэли: бот кидает 1..макс (Лекарь — соло; Боец — нужен ответ соперника)\n"
        "/heal @user &lt;макс&gt; — в командном бою лечить союзника (или себя, если без @user)\n"
        "    └ На /attack или /heal соперника можно ответить любой из трёх: /defend, /attack или /heal — у кого бросок больше, того действие сработает\n"
        "/roll [макс] — бот кинет 1..макс (без аргумента — 1–"
        f"{ROLL_MAX}, для квестов, не влияет на бой)\n"
        f"/buttle [команд] [минут] — командный бой в группе до "
        f"{MAX_BATTLE_TEAMS} команд по {MAX_BATTLE_TEAM_SIZE} чел. (по умолчанию 2 команды, 5 мин на набор)\n"
        "    └ Кнопки «в команду 1/2/3» и «▶️ Начать сейчас» — у инициатора\n"
        "    └ Бить врагов — <code>/attack @user &lt;макс&gt;</code>, "
        "лечить союзников — <code>/heal @user &lt;макс&gt;</code>.\n"
        "    └ После /attack цель отвечает <code>/defend</code>, <code>/attack</code> "
        "(контр) или <code>/heal</code> (хил-ответ) — у кого ролл больше, того действие сработает.\n"
        "    └ Атака: ролл > 30 — полный урон (15%/5% по классу); ролл ≤ 30 — слабый (5%/2%).\n"
        f"    └ КД лечения: Лекарь — {HEAL_COOLDOWN_BY_CLASS[CharClass.HEALER]} "
        f"хода, Боец — {HEAL_COOLDOWN_BY_CLASS[CharClass.ATTACKER]} хода. "
        f"После полного хила Лекаря на цели держится «продолжительность лечения» — "
        f"следующие 2 попытки /heal от этого Лекаря во время КД дадут цели "
        f"+{HEALER_HOT_PERMILLE / 10:.1f}% ХП каждая.\n"
        "/yield — выйти из текущей дуэли / командного боя (сдаться)\n"
        "/top — список всех игроков и их персонажей\n"
        "/jesus — воскресить любого без сознания персонажа (выбор из меню)\n"
        "/help — это сообщение\n\n"
        "<b>⚔️ Классы и проценты от макс. ХП</b>\n"
        f"{CLASS_LABELS[CharClass.ATTACKER]}\n"
        f"    🩸 урон: −{atk['damage_pct']}%   🩹 лечение: +{atk['heal_pct']}%\n"
        f"{CLASS_LABELS[CharClass.HEALER]}\n"
        f"    🩸 урон: −{heal['damage_pct']}%   🩹 лечение: +{heal['heal_pct']}%\n\n"
        "<b>🎲 Боевая система (бот сам кидает рандом)</b>\n"
        "• Игрок пишет МАКС своего кубика (потолок) — бот кидает рандом 1..макс.\n"
        "• <code>/attack &lt;ваш макс&gt;</code> — соперник отвечает любой из: "
        "<code>/defend</code>, <code>/attack</code> (контр-атака) или <code>/heal</code> (хил-ответ). "
        "Бот кидает обоим рандом, у кого выпало больше — тот перебил.\n"
        "  Атака перебила → урон классом% от max HP защитника (с учётом бонуса по ОП).\n"
        "  Контр-атака перебила → урон по инициатору. Хил-ответ перебил → ответчик лечится. Ничья — оба промахнулись.\n"
        f"• <code>/heal &lt;макс&gt;</code> у Лекаря: соло. Бот кидает 1..макс; "
        f"бросок > {HEAL_LOW_ROLL_THRESHOLD} → +{heal['heal_pct']}% (полный). Иначе — мини-хил +{HEALER_LOW_HEAL_PCT}%.\n"
        f"• <code>/heal &lt;макс&gt;</code> у Бойца: соперник отвечает любой из трёх. "
        f"Перебил Боец → +{atk['heal_pct']}%. Иначе — 0 (или сработает действие ответчика).\n"
        "• Pending-действие живёт пока соперник не ответит или кто-то не сделает /yield. "
        "Таймаута нет.\n"
        f"• /roll &lt;макс&gt; — бот кинет 1..макс (для квестов). "
        f"Без аргумента — 1–{ROLL_MAX}.\n\n"
        "<b>🛠 Как играть</b>\n"
        "1. /start — задаёшь имя, выбираешь класс, вводишь макс. ХП (например 100).\n"
        "2. /persona — список персонажей, переключить активного, изменить ХП или удалить.\n"
        "3. Для дуэли — в группе: /duel @username или в ответ на сообщение. "
        "Дуэль идёт между активными персонажами обоих игроков.\n"
        "4. В дуэли: <code>/attack 100</code> (твой макс кубика) → бот кинет 1..100. "
        "Соперник отвечает любой командой: <code>/defend 80</code>, "
        "<code>/attack 90</code> (контр-атака) или <code>/heal 200</code> (хил-ответ). "
        "Бот сравнивает выпавшие числа: у кого больше — того действие сработает. "
        "Группа договаривается о размере кубов между собой.\n"
        "5. Командный бой — /buttle в группе. "
        "Можно указать кол-во команд и время набора: <code>/buttle 3 7</code> = 3 команды, "
        "7 мин. Набираемся кнопками; инициатор может «▶️ Начать сейчас» досрочно. "
        "Бой заканчивается, когда в живых остаётся только одна команда.\n\n"
        "<i>ХП не восстанавливается автоматически после боя — финальное значение "
        "сохраняется в карточке персонажа. Восстановить ХП можно вручную в /persona или "
        "через /jesus.</i>\n\n"
        "<b>⚖️ Бонус сильному защитнику (только в дуэли)</b>\n"
        f"Если у защитника max ОП больше, чем у атакующего, урон атакующего снижается "
        f"за каждые {HP_ADVANTAGE_STEP} ОП превышения на 1 п.п. "
        f"(потолок −{HP_ADVANTAGE_MAX_REDUCTION} п.п.).\n"
        f"Например: атак. 200 vs защ. 500 → разница 300 → −3 п.п. "
        f"(Боец бьёт 15−3 = 12% от max ОП защитника).\n"
        "Минимум — 1% за попадание. На лечение не влияет."
    )


# ---------------------------------------------------------------------------
# /top — список всех зарегистрированных игроков и их персонажей
# ---------------------------------------------------------------------------

# Telegram ограничивает сообщение 4096 символами — режем с запасом.
TOP_MESSAGE_LIMIT = 3800


def _build_top_lines() -> list[str]:
    players: list[tuple[int, list[Character]]] = []
    for uid, chars in characters.items():
        if not chars:
            continue
        players.append((uid, list(chars.values())))

    if not players:
        return []

    players.sort(key=lambda item: (-len(item[1]), display_name(item[0]).lower()))

    total_chars = sum(len(chars) for _, chars in players)
    lines: list[str] = [
        f"🏆 <b>Игроки бота</b> — {len(players)} чел., персонажей: {total_chars}",
        "",
    ]
    for idx, (uid, chars) in enumerate(players, 1):
        uname = html.escape(display_name(uid))
        active_name = active_char.get(uid)
        lines.append(f"{idx}. <b>{uname}</b> — персонажей: {len(chars)}")
        for ch in chars:
            marker = "⭐" if ch.name == active_name else "  "
            alive = "❤️" if ch.current_hp > 0 else "💀"
            name_html = html.escape(ch.name)
            lines.append(
                f"   {marker} {alive} <i>{name_html}</i> — {CLASS_LABELS[ch.char_class]} "
                f"<b>{ch.current_hp}</b>/{ch.max_hp}"
            )
        lines.append("")
    return lines


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in lines:
        # +1 за перевод строки
        add_len = len(line) + 1
        if buf and buf_len + add_len > limit:
            chunks.append("\n".join(buf).rstrip())
            buf = []
            buf_len = 0
        buf.append(line)
        buf_len += add_len
    if buf:
        tail = "\n".join(buf).rstrip()
        if tail:
            chunks.append(tail)
    return chunks


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    remember_user(message.from_user)
    lines = _build_top_lines()
    if not lines:
        await message.answer(
            "Пока никто не зарегистрировал персонажей. Введите /start, чтобы стать первым."
        )
        return
    for chunk in _chunk_lines(lines, TOP_MESSAGE_LIMIT):
        await message.answer(chunk)


# ---------------------------------------------------------------------------
# /jesus — воскрешение любого "в больнице" персонажа (любым игроком)
# ---------------------------------------------------------------------------

# Соответствие token → [(owner_uid, char_name), ...] для открытых меню.
# Токен короткий, чтобы влезать в 64-байтный лимит callback_data.
_jesus_menus: dict[str, list[tuple[int, str]]] = {}
_MAX_JESUS_MENUS = 50  # грубая LRU-отсечка, чтобы не росло бесконечно


def _gc_jesus_menus() -> None:
    if len(_jesus_menus) > _MAX_JESUS_MENUS:
        for k in list(_jesus_menus.keys())[: len(_jesus_menus) - _MAX_JESUS_MENUS]:
            _jesus_menus.pop(k, None)


@router.message(Command("jesus"))
async def cmd_jesus(message: Message) -> None:
    remember_user(message.from_user)
    dead: list[tuple[int, Character]] = []
    for uid, chars in characters.items():
        for ch in chars.values():
            if ch.current_hp <= 0:
                dead.append((uid, ch))

    if not dead:
        await message.answer("Никого в больнице нет 🌞")
        return

    # Сортируем: сначала по владельцу, потом по имени персонажа.
    dead.sort(key=lambda item: (display_name(item[0]).lower(), item[1].name.lower()))

    import secrets
    token = secrets.token_urlsafe(6)
    _jesus_menus[token] = [(uid, ch.name) for uid, ch in dead]
    _gc_jesus_menus()

    rows: list[list[InlineKeyboardButton]] = []
    for idx, (uid, ch) in enumerate(dead):
        owner = display_name(uid)
        label = f"💀 {ch.name} · {owner}"
        # Telegram ограничивает button text — держимся в пределах.
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"jesus:{token}:{idx}")]
        )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"jesus:{token}:cancel")])

    await message.answer(
        f"⛪️ <b>Кого воскрешаем?</b> · в больнице: {len(dead)}\n"
        "<i>Любой игрок может воскресить любого персонажа. ХП восстановится до максимума.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("jesus:"))
async def on_jesus_choice(cb: CallbackQuery) -> None:
    remember_user(cb.from_user)
    parts = (cb.data or "").split(":", 2)
    if len(parts) != 3:
        await cb.answer()
        return
    _, token, payload = parts

    if payload == "cancel":
        _jesus_menus.pop(token, None)
        try:
            await cb.message.edit_text("❌ Отменено.")
        except Exception:
            pass
        await cb.answer()
        return

    try:
        idx = int(payload)
    except ValueError:
        await cb.answer()
        return

    menu = _jesus_menus.get(token)
    if menu is None or idx < 0 or idx >= len(menu):
        await cb.answer("Меню устарело, вызови /jesus заново.", show_alert=True)
        return

    uid, name = menu[idx]
    chars = characters.get(uid) or {}
    ch = chars.get(name)
    if ch is None:
        await cb.answer("Персонаж не найден (возможно, удалён).", show_alert=True)
        return
    if ch.current_hp > 0:
        await cb.answer("Этот персонаж уже жив.", show_alert=True)
        return

    ch.current_hp = ch.max_hp
    ch.in_battle = False
    save_state()
    _jesus_menus.pop(token, None)

    owner = html.escape(display_name(uid))
    healer = html.escape(display_name(cb.from_user.id))
    name_html = html.escape(ch.name)
    await cb.message.edit_text(
        f"✨ <b>{name_html}</b> воскрешён! ❤️ <b>{ch.max_hp}</b>/{ch.max_hp}\n"
        f"Владелец: {owner}\n"
        f"Чудотворец: {healer}"
    )
    await cb.answer("Воскрешено!", show_alert=False)


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
    BotCommand(command="duel", description="Дуэль 1×1 (в группе)"),
    BotCommand(command="attack", description="/attack <макс> — атака: бот кидает 1..макс"),
    BotCommand(command="defend", description="/defend <макс> — защита: бот кидает 1..макс"),
    BotCommand(command="heal", description="/heal <макс> — лечение: бот кидает 1..макс"),
    BotCommand(command="roll", description="/roll [макс] — бот кидает 1..макс (для квестов)"),
    BotCommand(command="buttle", description="Командный бой: набор в команды (в группе)"),
    BotCommand(command="yield", description="Выйти из дуэли / командного боя"),
    BotCommand(command="top", description="Список всех игроков и их персонажей"),
    BotCommand(command="jesus", description="Воскресить персонажа из больницы"),
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
    # Telegram allows only [A-Za-z0-9_-] in secret_token (1..256). Render-сгенерированный
    # секрет может содержать другие символы — отфильтруем; пустой результат => без секрета.
    _raw_secret = os.environ.get("WEBHOOK_SECRET") or ""
    _clean_secret = re.sub(r"[^A-Za-z0-9_-]", "", _raw_secret)[:256]
    webhook_secret = _clean_secret or None
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
