from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


STATUS_PENDING = "PENDING"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_CANCELLED = "CANCELLED"
STATUS_COMPLETED = "COMPLETED"

TYPE_ATTACK = "attack"
TYPE_DEFEND = "defend"


def _safe_get(record: Mapping, key: str):
    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class Challenge:
    id: int
    challenger_id: int
    opponent_id: int | None
    challenge_type: str
    status: str
    room_code: str | None
    guild_id: int
    channel_id: int
    message_id: int
    created_at: datetime
    accepted_at: datetime | None
    cancelled_at: datetime | None
    cancelled_by: int | None
    admin_message_id: int | None = None

    @classmethod
    def from_record(cls, record: Mapping) -> "Challenge":
        return cls(
            id=record["id"],
            challenger_id=record["challenger_id"],
            opponent_id=record["opponent_id"],
            challenge_type=record["challenge_type"],
            status=record["status"],
            room_code=record["room_code"],
            guild_id=record["guild_id"],
            channel_id=record["channel_id"],
            message_id=record["message_id"],
            created_at=_parse_dt(record["created_at"]),
            accepted_at=_parse_dt(record["accepted_at"]),
            cancelled_at=_parse_dt(record["cancelled_at"]),
            cancelled_by=record["cancelled_by"],
            admin_message_id=_safe_get(record, "admin_message_id"),
        )
