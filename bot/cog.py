"""ChallengesCog — slash command registration and dispatch."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.models import TYPE_ATTACK, TYPE_DEFEND
from bot.repository import ChallengeRepository
from bot.service import ChallengeService
from bot.tasks import ExpiryLoop
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
        self.expiry_loop: ExpiryLoop | None = None

    async def cog_load(self) -> None:
        self.expiry_loop = ExpiryLoop(self.service, self.repo)
        self.expiry_loop.start()

    async def cog_unload(self) -> None:
        if self.expiry_loop is not None:
            self.expiry_loop.stop()

    # ------------------------------------------------------------------ #
    # /attack
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="attack", description="Challenge as Attacker"
    )
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

    @app_commands.command(
        name="defend", description="Challenge as Defender"
    )
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
                "This command must be used in a server.", ephemeral=True
            )
            return

        active = await self.repo.find_active_for_user(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )

        if not active:
            await interaction.response.send_message(
                "You have no active challenges.", ephemeral=True
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
