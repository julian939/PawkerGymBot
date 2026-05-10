import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    discord_token: str
    database_path: str
    dev_guild_id: int | None
    admin_channel_id: int | None
    challenge_expiry_hours: int = 24
    max_pending_per_user: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ["DISCORD_TOKEN"]
        db_path = os.environ.get("DATABASE_PATH", "data/bot.db")
        dev_guild = os.environ.get("DEV_GUILD_ID")
        admin_channel = os.environ.get("ADMIN_CHANNEL_ID")
        return cls(
            discord_token=token,
            database_path=db_path,
            dev_guild_id=int(dev_guild) if dev_guild else None,
            admin_channel_id=int(admin_channel) if admin_channel else None,
        )
