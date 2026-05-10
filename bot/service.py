"""ChallengeService — orchestrates challenge lifecycle.

Sits between the cog (commands/buttons) and the repository. Handles:
- Validation
- Code generation
- Embed building + ping behaviour
- Discord message editing / posting
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord

from bot.code_generator import generate_unique_room_code
from bot.config import Config
from bot.embeds import (
    admin_match_started_embed,
    attacker_defender_ids,
    challenge_cancelled_embed,
    challenge_expired_embed,
    direct_challenge_embed,
    live_match_embed,
    match_cancelled_embed,
    open_challenge_embed,
)
from bot.models import (
    Challenge,
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    TYPE_ATTACK,
    TYPE_DEFEND,
)
from bot.repository import ChallengeRepository

log = logging.getLogger(__name__)


SILENT = discord.AllowedMentions.none()


def _ping_users(*user_ids: int) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=uid) for uid in user_ids],
    )


def _mentions(*user_ids: int) -> str:
    return " ".join(f"<@{uid}>" for uid in user_ids)


class ChallengeService:
    def __init__(
        self,
        bot: discord.Client,
        repo: ChallengeRepository,
        config: Config,
    ):
        self.bot = bot
        self.repo = repo
        self.config = config

    # ------------------------------------------------------------------ #
    # Public command entry points
    # ------------------------------------------------------------------ #

    async def create_direct_challenge(
        self,
        interaction: discord.Interaction,
        *,
        opponent: discord.User,
        challenge_type: str,
    ) -> None:
        caller = interaction.user

        if opponent.id == caller.id:
            await interaction.response.send_message(
                "You can't challenge yourself.", ephemeral=True
            )
            return
        if opponent.bot:
            await interaction.response.send_message(
                "Bots can't be challenged.", ephemeral=True
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return

        if await self.repo.pair_has_pending(
            caller.id, opponent.id, interaction.guild_id
        ):
            await interaction.response.send_message(
                "You already have an open challenge with this user — wait "
                "for it to be accepted, cancelled, or expired first.",
                ephemeral=True,
            )
            return

        pending_count = await self.repo.count_pending_by_challenger(caller.id)
        if pending_count >= self.config.max_pending_per_user:
            await interaction.response.send_message(
                "You have too many pending challenges.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.config.challenge_expiry_hours)

        placeholder = Challenge(
            id=0,
            challenger_id=caller.id,
            opponent_id=opponent.id,
            challenge_type=challenge_type,
            status=STATUS_PENDING,
            room_code=None,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=0,
            created_at=now,
            accepted_at=None,
            cancelled_at=None,
            cancelled_by=None,
            expires_at=expires_at,
        )

        from bot.views import AcceptButtonView

        await interaction.response.send_message(
            content=_mentions(opponent.id),
            embed=direct_challenge_embed(placeholder),
            allowed_mentions=_ping_users(opponent.id),
        )
        message = await interaction.original_response()

        await self.repo.insert_pending(
            challenger_id=caller.id,
            opponent_id=opponent.id,
            challenge_type=challenge_type,
            guild_id=interaction.guild_id,
            channel_id=message.channel.id,
            message_id=message.id,
            expires_at=expires_at,
        )

        await message.edit(view=AcceptButtonView())

    async def create_or_match_open(
        self,
        interaction: discord.Interaction,
        *,
        challenge_type: str,
    ) -> None:
        caller = interaction.user

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return

        wanted_type = TYPE_DEFEND if challenge_type == TYPE_ATTACK else TYPE_ATTACK

        match = await self.repo.find_queue_match(
            guild_id=interaction.guild_id,
            wanted_type=wanted_type,
            requester_id=caller.id,
        )

        if match is not None:
            code = await generate_unique_room_code(self.repo)
            updated = await self.repo.accept_atomic(match.id, caller.id, code)
            if updated is not None:
                # Ack the slash command immediately — Discord drops the
                # interaction after 3s and shows "application did not respond".
                await interaction.response.send_message(
                    f"Matched! Room code: `{updated.room_code}`",
                    ephemeral=True,
                )
                await self._finalize_accepted_match(updated)
                return
            # Race lost — fall through to creating a new open challenge

        if await self.repo.has_pending_open(
            challenger_id=caller.id,
            guild_id=interaction.guild_id,
            challenge_type=challenge_type,
        ):
            label = "attack" if challenge_type == TYPE_ATTACK else "defend"
            await interaction.response.send_message(
                f"You already have an open `/{label}` challenge waiting. "
                f"Cancel it first if you want to post a new one.",
                ephemeral=True,
            )
            return

        pending_count = await self.repo.count_pending_by_challenger(caller.id)
        if pending_count >= self.config.max_pending_per_user:
            await interaction.response.send_message(
                "You have too many pending challenges.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.config.challenge_expiry_hours)

        placeholder = Challenge(
            id=0,
            challenger_id=caller.id,
            opponent_id=None,
            challenge_type=challenge_type,
            status=STATUS_PENDING,
            room_code=None,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=0,
            created_at=now,
            accepted_at=None,
            cancelled_at=None,
            cancelled_by=None,
            expires_at=expires_at,
        )

        from bot.views import AcceptButtonView

        await interaction.response.send_message(
            embed=open_challenge_embed(placeholder),
            allowed_mentions=SILENT,
        )
        message = await interaction.original_response()

        await self.repo.insert_pending(
            challenger_id=caller.id,
            opponent_id=None,
            challenge_type=challenge_type,
            guild_id=interaction.guild_id,
            channel_id=message.channel.id,
            message_id=message.id,
            expires_at=expires_at,
        )

        await message.edit(view=AcceptButtonView())

    async def accept_challenge(
        self,
        interaction: discord.Interaction,
        challenge: Challenge,
    ) -> None:
        code = await generate_unique_room_code(self.repo)
        updated = await self.repo.accept_atomic(
            challenge.id, interaction.user.id, code
        )
        if updated is None:
            await interaction.response.send_message(
                "This challenge is no longer available.", ephemeral=True
            )
            return

        # Ack the button click first so Discord doesn't time it out.
        await interaction.response.send_message(
            "Match accepted!", ephemeral=True
        )
        await self._finalize_accepted_match(updated)

    async def cancel_challenge(
        self,
        *,
        challenge: Challenge,
        cancelled_by: int,
    ) -> None:
        previous_status = challenge.status
        updated = await self.repo.set_status(
            challenge.id, STATUS_CANCELLED, cancelled_by=cancelled_by
        )
        if updated is None:
            return

        if previous_status == STATUS_ACCEPTED:
            embed = match_cancelled_embed(updated, cancelled_by)
        else:
            embed = challenge_cancelled_embed(updated, cancelled_by)

        await self._edit_channel_message(updated, embed=embed)

        if previous_status == STATUS_ACCEPTED and updated.admin_message_id:
            await self._delete_admin_message(updated.admin_message_id)

    async def expire_challenge(self, challenge: Challenge) -> None:
        await self._edit_channel_message(
            challenge, embed=challenge_expired_embed(challenge)
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _finalize_accepted_match(self, updated: Challenge) -> None:
        """Common flow for any path that just transitioned to ACCEPTED.

        - Deletes the original challenge message (the new match post replaces it).
        - Posts a fresh match message in the same channel that pings both
          players so they actually get notified.
        - Posts an admin log entry if ADMIN_CHANNEL_ID is configured.
        """
        await self._delete_channel_message(updated)

        a_id, d_id = attacker_defender_ids(updated)
        try:
            channel = self.bot.get_channel(updated.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(updated.channel_id)
            await channel.send(
                content=_mentions(a_id, d_id),
                embed=live_match_embed(updated),
                allowed_mentions=_ping_users(a_id, d_id),
            )
        except discord.NotFound:
            log.warning(
                "Channel %s not found while announcing match %s",
                updated.channel_id,
                updated.id,
            )
        except discord.Forbidden:
            log.warning(
                "Missing permissions to post match %s in channel %s",
                updated.id,
                updated.channel_id,
            )

        await self._notify_admin_match_started(updated)

    async def _notify_admin_match_started(self, ch: Challenge) -> None:
        channel_id = self.config.admin_channel_id
        if channel_id is None:
            return
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            sent = await channel.send(
                embed=admin_match_started_embed(ch),
                allowed_mentions=SILENT,
            )
            await self.repo.set_admin_message_id(ch.id, sent.id)
        except discord.NotFound:
            log.warning(
                "Admin channel %s not found; skipping notification", channel_id
            )
        except discord.Forbidden:
            log.warning(
                "Missing permissions to post to admin channel %s", channel_id
            )
        except Exception:
            log.exception("Failed to send admin notification for %s", ch.id)

    async def _delete_admin_message(self, message_id: int) -> None:
        channel_id = self.config.admin_channel_id
        if channel_id is None:
            return
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            partial = channel.get_partial_message(message_id)
            await partial.delete()
        except discord.NotFound:
            pass  # already gone
        except discord.Forbidden:
            log.warning(
                "Missing permissions to delete admin message %s", message_id
            )
        except Exception:
            log.exception("Failed to delete admin message %s", message_id)

    async def _delete_channel_message(self, ch: Challenge) -> None:
        """Delete the challenge message. If we lack delete permission, fall
        back to clearing buttons + content so it's at least not actionable."""
        try:
            channel = self.bot.get_channel(ch.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(ch.channel_id)
        except discord.NotFound:
            return
        except Exception:
            log.exception("Failed to resolve channel for %s", ch.id)
            return

        partial = channel.get_partial_message(ch.message_id)
        try:
            await partial.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.info(
                "Cannot delete challenge %s message; clearing buttons instead",
                ch.id,
            )
            try:
                await partial.edit(content=None, embed=None, view=None)
            except Exception:
                log.exception(
                    "Fallback edit also failed for challenge %s", ch.id
                )
        except Exception:
            log.exception("Failed to delete channel message for %s", ch.id)

    async def _edit_channel_message(
        self,
        ch: Challenge,
        *,
        embed: discord.Embed,
    ) -> None:
        try:
            channel = self.bot.get_channel(ch.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(ch.channel_id)
            partial = channel.get_partial_message(ch.message_id)
            await partial.edit(
                content=None, embed=embed, view=None, allowed_mentions=SILENT
            )
        except discord.NotFound:
            log.info(
                "Challenge %s message/channel not found; skipping edit", ch.id
            )
        except discord.Forbidden:
            log.warning(
                "Missing permissions to edit message for challenge %s", ch.id
            )
        except Exception:
            log.exception("Unexpected error editing channel message for %s", ch.id)
