"""Entry point: instantiates the Bot, sets up DB pool, registers the cog, runs."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from bot.cog import ChallengesCog
from bot.config import Config
from bot.database import Database, bootstrap_schema
from bot.repository import ChallengeRepository
from bot.service import ChallengeService
from bot.views import AcceptButtonView


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")


class MatchChallengeBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        # No message_content intent needed for slash commands
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.db: Database | None = None
        # These are populated in setup_hook so persistent view callbacks
        # can resolve them from interaction.client.
        self.repo: ChallengeRepository | None = None
        self.service: ChallengeService | None = None

    async def setup_hook(self) -> None:
        self.db = await Database.connect(self.config.database_path)
        await bootstrap_schema(self.db)

        self.repo = ChallengeRepository(self.db)
        self.service = ChallengeService(self, self.repo, self.config)

        await self.add_cog(ChallengesCog(self, self.service, self.repo))

        # Register persistent view for Accept buttons. Static custom_id is
        # all that's needed for re-attachment; the callback resolves
        # repo/service from interaction.client (set above).
        self.add_view(AcceptButtonView())

        # Sync slash commands
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(
                "Synced commands to dev guild %s", self.config.dev_guild_id
            )
        else:
            await self.tree.sync()
            log.info("Synced global commands")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)


async def main() -> None:
    config = Config.from_env()
    bot = MatchChallengeBot(config)
    try:
        await bot.start(config.discord_token)
    finally:
        if bot.db is not None:
            await bot.db.close()


if __name__ == "__main__":
    asyncio.run(main())
