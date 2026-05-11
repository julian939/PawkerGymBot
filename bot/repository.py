from datetime import datetime, timezone

from bot.database import Database
from bot.models import (
    Challenge,
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_PENDING,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChallengeRepository:
    """All database access for challenges. Returns Challenge dataclasses."""

    def __init__(self, db: Database):
        self.db = db

    async def _fetchrow(self, sql: str, params: tuple):
        async with self.db.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple):
        async with self.db.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def _fetchval(self, sql: str, params: tuple):
        async with self.db.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return row[0]

    async def insert_pending(
        self,
        *,
        challenger_id: int,
        opponent_id: int | None,
        challenge_type: str,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> Challenge:
        sql = """
            INSERT INTO challenges (
                challenger_id, opponent_id, challenge_type, status,
                guild_id, channel_id, message_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *;
        """
        params = (
            challenger_id,
            opponent_id,
            challenge_type,
            STATUS_PENDING,
            guild_id,
            channel_id,
            message_id,
            _now_iso(),
        )
        async with self.db.lock:
            row = await self._fetchrow(sql, params)
            await self.db.conn.commit()
        return Challenge.from_record(row)

    async def get_by_id(self, challenge_id: int) -> Challenge | None:
        row = await self._fetchrow(
            "SELECT * FROM challenges WHERE id = ?;", (challenge_id,)
        )
        return Challenge.from_record(row) if row else None

    async def get_by_message_id(self, message_id: int) -> Challenge | None:
        row = await self._fetchrow(
            "SELECT * FROM challenges WHERE message_id = ?;", (message_id,)
        )
        return Challenge.from_record(row) if row else None

    async def find_queue_match(
        self,
        *,
        guild_id: int,
        wanted_type: str,
        requester_id: int,
    ) -> Challenge | None:
        sql = """
            SELECT *
            FROM challenges
            WHERE guild_id = ?
              AND status = ?
              AND challenge_type = ?
              AND opponent_id IS NULL
              AND challenger_id <> ?
            ORDER BY created_at DESC
            LIMIT 1;
        """
        row = await self._fetchrow(
            sql, (guild_id, STATUS_PENDING, wanted_type, requester_id)
        )
        return Challenge.from_record(row) if row else None

    async def accept_atomic(
        self,
        challenge_id: int,
        opponent_id: int,
        room_code: str,
    ) -> Challenge | None:
        sql = """
            UPDATE challenges
            SET status = ?,
                opponent_id = ?,
                room_code = ?,
                accepted_at = ?
            WHERE id = ?
              AND status = ?
              AND (opponent_id IS NULL OR opponent_id = ?)
            RETURNING *;
        """
        params = (
            STATUS_ACCEPTED,
            opponent_id,
            room_code,
            _now_iso(),
            challenge_id,
            STATUS_PENDING,
            opponent_id,
        )
        async with self.db.lock:
            row = await self._fetchrow(sql, params)
            await self.db.conn.commit()
        return Challenge.from_record(row) if row else None

    async def set_admin_message_id(
        self, challenge_id: int, admin_message_id: int
    ) -> None:
        async with self.db.lock:
            await self.db.conn.execute(
                "UPDATE challenges SET admin_message_id = ? WHERE id = ?;",
                (admin_message_id, challenge_id),
            )
            await self.db.conn.commit()

    async def code_ever_used(self, code: str) -> bool:
        val = await self._fetchval(
            "SELECT EXISTS(SELECT 1 FROM challenges WHERE room_code = ?);",
            (code,),
        )
        return bool(val)

    async def oldest_used_code(self) -> str:
        sql = """
            SELECT room_code
            FROM challenges
            WHERE room_code IS NOT NULL
            ORDER BY accepted_at ASC
            LIMIT 1;
        """
        return await self._fetchval(sql, ())

    async def find_active_for_user(
        self,
        *,
        user_id: int,
        guild_id: int,
    ) -> list[Challenge]:
        sql = """
            SELECT *
            FROM challenges
            WHERE guild_id = ?
              AND (
                    (status = ? AND challenger_id = ?)
                 OR (status = ? AND (challenger_id = ? OR opponent_id = ?))
              )
            ORDER BY created_at DESC;
        """
        rows = await self._fetchall(
            sql,
            (
                guild_id,
                STATUS_PENDING, user_id,
                STATUS_ACCEPTED, user_id, user_id,
            ),
        )
        return [Challenge.from_record(r) for r in rows]

    async def fetch_expired_accepted(
        self, *, deadline_iso: str
    ) -> list[Challenge]:
        sql = """
            SELECT *
            FROM challenges
            WHERE status = ?
              AND accepted_at IS NOT NULL
              AND accepted_at < ?;
        """
        rows = await self._fetchall(sql, (STATUS_ACCEPTED, deadline_iso))
        return [Challenge.from_record(r) for r in rows]

    async def find_accepted_for_user(
        self,
        *,
        user_id: int,
        guild_id: int,
    ) -> list[Challenge]:
        sql = """
            SELECT *
            FROM challenges
            WHERE guild_id = ?
              AND status = ?
              AND (challenger_id = ? OR opponent_id = ?)
            ORDER BY accepted_at DESC;
        """
        rows = await self._fetchall(
            sql,
            (guild_id, STATUS_ACCEPTED, user_id, user_id),
        )
        return [Challenge.from_record(r) for r in rows]

    async def set_status(
        self,
        challenge_id: int,
        status: str,
        *,
        cancelled_by: int | None = None,
    ) -> Challenge | None:
        if status == STATUS_CANCELLED:
            sql = """
                UPDATE challenges
                SET status = ?,
                    cancelled_at = ?,
                    cancelled_by = ?
                WHERE id = ?
                RETURNING *;
            """
            params = (status, _now_iso(), cancelled_by, challenge_id)
        else:
            sql = """
                UPDATE challenges
                SET status = ?
                WHERE id = ?
                RETURNING *;
            """
            params = (status, challenge_id)

        async with self.db.lock:
            row = await self._fetchrow(sql, params)
            await self.db.conn.commit()
        return Challenge.from_record(row) if row else None

    async def count_pending_by_challenger(self, user_id: int) -> int:
        val = await self._fetchval(
            """
            SELECT COUNT(*)
            FROM challenges
            WHERE challenger_id = ?
              AND status = ?;
            """,
            (user_id, STATUS_PENDING),
        )
        return int(val or 0)

    async def pair_has_pending(
        self,
        user_a: int,
        user_b: int,
        guild_id: int,
    ) -> bool:
        sql = """
            SELECT EXISTS(
                SELECT 1
                FROM challenges
                WHERE guild_id = ?
                  AND status = ?
                  AND opponent_id IS NOT NULL
                  AND (
                       (challenger_id = ? AND opponent_id = ?)
                    OR (challenger_id = ? AND opponent_id = ?)
                  )
            );
        """
        val = await self._fetchval(
            sql,
            (guild_id, STATUS_PENDING, user_a, user_b, user_b, user_a),
        )
        return bool(val)

    async def has_pending_open(
        self,
        *,
        challenger_id: int,
        guild_id: int,
        challenge_type: str,
    ) -> bool:
        sql = """
            SELECT EXISTS(
                SELECT 1
                FROM challenges
                WHERE guild_id = ?
                  AND status = ?
                  AND challenge_type = ?
                  AND challenger_id = ?
                  AND opponent_id IS NULL
            );
        """
        val = await self._fetchval(
            sql,
            (guild_id, STATUS_PENDING, challenge_type, challenger_id),
        )
        return bool(val)
