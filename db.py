"""Хранилище состояния бота.

Поддерживает два режима:
- ``DATABASE_URL`` задан (Postgres / Neon) — данные хранятся в БД.
- ``DATABASE_URL`` не задан — fallback на локальный файл ``data.json``.

В Postgres мы храним всё состояние в одной строке таблицы ``rp_bot_state``
(колонка ``data`` типа JSONB). Это даёт минимум кода и сохраняет идентичную
структуру JSON, которую использует in-memory модель бота.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_DATA_FILE = "data.json"
STATE_KEY = "main"  # один логический документ — состояние бота
DDL = """
CREATE TABLE IF NOT EXISTS rp_bot_state (
    key  TEXT PRIMARY KEY,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _normalize_dsn(dsn: str) -> str:
    """asyncpg не понимает префиксы вида ``postgresql+asyncpg://``."""
    if dsn.startswith("postgres+asyncpg://"):
        return "postgresql://" + dsn[len("postgres+asyncpg://") :]
    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + dsn[len("postgresql+asyncpg://") :]
    return dsn


class Storage:
    """Абстрактный интерфейс хранилища."""

    async def init(self) -> None:  # pragma: no cover - интерфейс
        ...

    async def load(self) -> dict[str, Any]:  # pragma: no cover - интерфейс
        ...

    async def save(self, data: dict[str, Any]) -> None:  # pragma: no cover - интерфейс
        ...

    async def close(self) -> None:  # pragma: no cover - интерфейс
        ...


class FileStorage(Storage):
    """Простое хранилище в JSON-файле (как было изначально)."""

    def __init__(self, path: str = DEFAULT_DATA_FILE) -> None:
        self.path = path

    async def init(self) -> None:
        logger.info("FileStorage: путь %s", self.path)

    async def load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось загрузить %s: %s", self.path, exc)
            return {}

    async def save(self, data: dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить %s: %s", self.path, exc)

    async def close(self) -> None:
        return None


class PostgresStorage(Storage):
    """Хранилище в Postgres (одна JSONB-строка)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = _normalize_dsn(dsn)
        self._pool: Any = None

    async def init(self) -> None:
        # импорт здесь, чтобы файловый fallback не требовал asyncpg
        import asyncpg  # type: ignore

        # Neon обычно требует SSL — он включён по умолчанию в URL (?sslmode=require),
        # но на всякий случай прокидываем ssl="prefer" если не задано.
        kwargs: dict[str, Any] = {}
        if "sslmode" not in self.dsn:
            kwargs["ssl"] = "prefer"
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4, **kwargs)
        async with self._pool.acquire() as conn:
            await conn.execute(DDL)
        logger.info("PostgresStorage: подключение установлено, таблица готова")

    async def load(self) -> dict[str, Any]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM rp_bot_state WHERE key = $1", STATE_KEY
            )
        if row is None:
            return {}
        data = row["data"]
        if isinstance(data, str):
            return json.loads(data)
        return data or {}

    async def save(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        payload = json.dumps(data, ensure_ascii=False)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO rp_bot_state (key, data, updated_at)
                VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE
                SET data = EXCLUDED.data, updated_at = NOW()
                """,
                STATE_KEY,
                payload,
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def build_storage() -> Storage:
    """Создаёт хранилище согласно ENV."""
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return PostgresStorage(dsn)
    return FileStorage(os.environ.get("RP_DATA_FILE", DEFAULT_DATA_FILE))


__all__ = ["Storage", "FileStorage", "PostgresStorage", "build_storage"]
