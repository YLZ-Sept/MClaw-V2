"""SQLite-backed config + task store for the Chanjing Digital Human plugin."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

DEFAULT_CONFIG: dict[str, str] = {
    "app_id": "",
    "secret_key": "",
    "base_url": "https://open-api.chanjing.cc/open/v1",
}

# update_task 只允许写入这些列，避免字段名拼进 SQL
_ALLOWED_TASK_FIELDS = {
    "status",
    "upstream_id",
    "audio_url",
    "video_url",
    "duration",
    "error_message",
    "trace_id",
}


class TaskManager:
    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._create_tables()
        await self._seed_config()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _create_tables(self) -> None:
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                upstream_id TEXT DEFAULT '',
                audio_url TEXT DEFAULT '',
                video_url TEXT DEFAULT '',
                duration REAL,
                error_message TEXT DEFAULT '',
                trace_id TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self._db.commit()

    async def _seed_config(self) -> None:
        for key, value in DEFAULT_CONFIG.items():
            await self._db.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, value)
            )
        await self._db.commit()

    # ---- config ----

    async def get_config(self) -> dict[str, str]:
        cur = await self._db.execute("SELECT key, value FROM config")
        rows = await cur.fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def update_config(self, updates: dict[str, str]) -> None:
        for key, value in updates.items():
            await self._db.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await self._db.commit()

    # ---- tasks ----

    async def create_task(self, task_id: str, kind: str, upstream_id: str = "") -> dict[str, Any]:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        await self._db.execute(
            "INSERT OR REPLACE INTO tasks (task_id, kind, status, upstream_id, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?, ?)",
            (task_id, kind, upstream_id, now, now),
        )
        await self._db.commit()
        return await self.get_task(task_id)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        cur = await self._db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_task(self, task_id: str, **fields: Any) -> None:
        safe = {k: v for k, v in fields.items() if k in _ALLOWED_TASK_FIELDS}
        if not safe:
            return
        safe["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        cols = ", ".join(f"{k} = ?" for k in safe)
        vals = list(safe.values()) + [task_id]
        await self._db.execute(f"UPDATE tasks SET {cols} WHERE task_id = ?", vals)
        await self._db.commit()

    async def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = await self._db.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]
