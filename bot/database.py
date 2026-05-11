from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite


class Database:
    """Single shared aiosqlite connection guarded by an asyncio lock."""

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

        async with db.conn.execute("PRAGMA table_info(challenges)") as cur:
            cols = {row[1] for row in await cur.fetchall()}

        # Best-effort migration for DBs created before admin_message_id existed.
        if "admin_message_id" not in cols:
            await db.conn.execute(
                "ALTER TABLE challenges ADD COLUMN admin_message_id INTEGER"
            )

        # Migration: drop expires_at column + retire EXPIRED status if present.
        # SQLite's CHECK constraint can't be altered, so we rebuild the table.
        if "expires_at" in cols:
            await db.conn.executescript(
                """
                PRAGMA foreign_keys=OFF;

                CREATE TABLE challenges_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    challenger_id   INTEGER NOT NULL,
                    opponent_id     INTEGER,
                    challenge_type  TEXT NOT NULL CHECK (challenge_type IN ('attack', 'defend')),
                    status          TEXT NOT NULL CHECK (status IN ('PENDING', 'ACCEPTED', 'CANCELLED', 'COMPLETED')),
                    room_code       TEXT,
                    guild_id        INTEGER NOT NULL,
                    channel_id      INTEGER NOT NULL,
                    message_id      INTEGER NOT NULL,
                    created_at      TEXT NOT NULL,
                    accepted_at     TEXT,
                    cancelled_at    TEXT,
                    cancelled_by    INTEGER,
                    admin_message_id INTEGER
                );

                INSERT INTO challenges_new (
                    id, challenger_id, opponent_id, challenge_type, status,
                    room_code, guild_id, channel_id, message_id, created_at,
                    accepted_at, cancelled_at, cancelled_by, admin_message_id
                )
                SELECT
                    id, challenger_id, opponent_id, challenge_type,
                    CASE WHEN status = 'EXPIRED' THEN 'CANCELLED' ELSE status END,
                    room_code, guild_id, channel_id, message_id, created_at,
                    accepted_at, cancelled_at, cancelled_by, admin_message_id
                FROM challenges;

                DROP TABLE challenges;
                ALTER TABLE challenges_new RENAME TO challenges;

                CREATE UNIQUE INDEX IF NOT EXISTS challenges_room_code_uniq
                    ON challenges (room_code) WHERE room_code IS NOT NULL;
                CREATE INDEX IF NOT EXISTS challenges_queue_lookup
                    ON challenges (guild_id, status, challenge_type, opponent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS challenges_challenger_status
                    ON challenges (challenger_id, status);
                CREATE INDEX IF NOT EXISTS challenges_opponent_status
                    ON challenges (opponent_id, status);
                CREATE INDEX IF NOT EXISTS challenges_message_id
                    ON challenges (message_id);

                PRAGMA foreign_keys=ON;
                """
            )

        await db.conn.commit()
