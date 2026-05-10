from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite


class Database:
    """Single shared aiosqlite connection guarded by an asyncio lock.

    SQLite serializes writes anyway; the lock keeps multi-statement
    operations atomic and avoids 'database is locked' errors under
    bursty Discord traffic.
    """

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self.lock = asyncio.Lock()

    @classmethod
    async def connect(cls, path: str) -> "Database":
        db_path = Path(path)
        if db_path.parent and str(db_path.parent) not in ("", "."):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()


async def bootstrap_schema(db: Database) -> None:
    schema_sql = Path(__file__).parent.parent.joinpath("schema.sql").read_text()
    async with db.lock:
        await db.conn.executescript(schema_sql)
        # Best-effort migration for DBs created before admin_message_id existed.
        async with db.conn.execute("PRAGMA table_info(challenges)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "admin_message_id" not in cols:
            await db.conn.execute(
                "ALTER TABLE challenges ADD COLUMN admin_message_id INTEGER"
            )
        await db.conn.commit()
