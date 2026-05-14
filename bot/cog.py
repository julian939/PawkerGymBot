"""ChallengesCog — slash command registration and dispatch."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.models import TYPE_ATTACK, TYPE_DEFEND
from bot.repository import ChallengeRepository
from bot.service import ChallengeService
from bot.views import CancelPickerView

log = logging.getLogger(__name__)


class ChallengesCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        service: ChallengeService,
        repo: ChallengeRepository,
    ):
        self.bot = bot
        self.service = service
        self.repo = repo
        self.expire_loop.start()

    def cog_unload(self) -> None:
        self.expire_loop.cancel()

    # ------------------------------------------------------------------ #
    # Background: expire stale PENDING (>24h) and ACCEPTED (>24h) matches
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def expire_loop(self) -> None:
        hours = getattr(self.bot.config, "pending_expiry_hours", 24)
        deadline = datetime.now(timezone.utc) - timedelta(hours=hours)
        deadline_iso = deadline.isoformat()

        try:
            expired_accepted = await self.repo.fetch_expired_accepted(
                deadline_iso=deadline_iso
            )
        except Exception:
            log.exception("expire_loop: fetch accepted failed")
            expired_accepted = []

        for ch in expired_accepted:
            try:
                await self.service.expire_accepted_match(ch)
            except Exception:
                log.exception(
                    "expire_loop: failed to expire accepted match %s", ch.id
                )

        try:
            expired_pending = await self.repo.fetch_expired_pending(
                deadline_iso=deadline_iso
            )
        except Exception:
            log.exception("expire_loop: fetch pending failed")
            expired_pending = []

        for ch in expired_pending:
            try:
                await self.service.expire_pending_challenge(ch)
            except Exception:
                log.exception(
                    "expire_loop: failed to expire pending challenge %s", ch.id
                )

    @expire_loop.before_loop
    async def _before_expire_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # /attack
    # ------------------------------------------------------------------ #

    @app_commands.command(name="attack", description="Challenge as Attacker")
    @app_commands.describe(
        user="Optional. If omitted, posts an Open Challenge."
    )
    async def attack(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if user is not None:
            await self.service.create_direct_challenge(
                interaction, opponent=user, challenge_type=TYPE_ATTACK
            )
        else:
            await self.service.create_or_match_open(
                interaction, challenge_type=TYPE_ATTACK
            )

    # ------------------------------------------------------------------ #
    # /defend
    # ------------------------------------------------------------------ #

    @app_commands.command(name="defend", description="Challenge as Defender")
    @app_commands.describe(
        user="Optional. If omitted, posts an Open Challenge."
    )
    async def defend(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if user is not None:
            await self.service.create_direct_challenge(
                interaction, opponent=user, challenge_type=TYPE_DEFEND
            )
        else:
            await self.service.create_or_match_open(
                interaction, challenge_type=TYPE_DEFEND
            )

    # ------------------------------------------------------------------ #
    # /cancel
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="cancel", description="Cancel your active challenge"
    )
    async def cancel(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command only works in a server, not in DMs.",
                ephemeral=True,
            )
            return

        active = await self.repo.find_active_for_user(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )

        if not active:
            await interaction.response.send_message(
                "You have no active challenges to cancel.", ephemeral=True
            )
            return

        if len(active) == 1:
            await self.service.cancel_challenge(
                challenge=active[0], cancelled_by=interaction.user.id
            )
            await interaction.response.send_message(
                "Challenge cancelled.", ephemeral=True
            )
            return

        view = await CancelPickerView.build(
            active,
            user_id=interaction.user.id,
            service=self.service,
            guild=interaction.guild,
            client=self.bot,
        )
        await interaction.response.send_message(
            view.page_content(),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    # ------------------------------------------------------------------ #
    # /result
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="result",
        description="Submit a result screenshot for your active match",
    )
    @app_commands.describe(screenshot="Screenshot of the match result")
    async def result(
        self,
        interaction: discord.Interaction,
        screenshot: discord.Attachment,
    ) -> None:
        await self.service.submit_result(interaction, screenshot)
