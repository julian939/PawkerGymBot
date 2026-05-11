"""ChallengeService — orchestrates challenge lifecycle."""

from __future__ import annotations

import io
import logging

import discord

from bot.code_generator import generate_unique_room_code
from bot.config import Config
from bot.embeds import (
    admin_match_started_embed,
    admin_result_embed,
    attacker_defender_ids,
    challenge_cancelled_embed,
    direct_challenge_embed,
    live_match_embed,
    match_cancelled_embed,
    match_completed_embed,
    match_expired_embed,
    open_challenge_embed,
)
from bot.models import (
    Challenge,
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
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
                "You already have an open challenge with this user.",
                ephemeral=True,
            )
            return

        pending_count = await self.repo.count_pending_by_challenger(caller.id)
        if pending_count >= self.config.max_pending_per_user:
            await interaction.response.send_message(
                "You have too many pending challenges.", ephemeral=True
            )
            return

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
            created_at=None,  # type: ignore[arg-type]
            accepted_at=None,
            cancelled_at=None,
            cancelled_by=None,
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
                await interaction.response.send_message(
                    "Matched!", ephemeral=True
                )
                await self._finalize_accepted_match(updated)
                return
            # Race lost — fall through

        if await self.repo.has_pending_open(
            challenger_id=caller.id,
            guild_id=interaction.guild_id,
            challenge_type=challenge_type,
        ):
            label = "attack" if challenge_type == TYPE_ATTACK else "defend"
            await interaction.response.send_message(
                f"You already have an open `/{label}` challenge.",
                ephemeral=True,
            )
            return

        pending_count = await self.repo.count_pending_by_challenger(caller.id)
        if pending_count >= self.config.max_pending_per_user:
            await interaction.response.send_message(
                "You have too many pending challenges.", ephemeral=True
            )
            return

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
            created_at=None,  # type: ignore[arg-type]
            accepted_at=None,
            cancelled_at=None,
            cancelled_by=None,
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

        # Silent ack — public live embed is the user-visible confirmation.
        await interaction.response.defer()
        await self._finalize_accepted_match(updated)

    async def expire_accepted_match(self, ch: Challenge) -> None:
        updated = await self.repo.set_status(
            ch.id, STATUS_CANCELLED, cancelled_by=None
        )
        if updated is None:
            return
        await self._edit_channel_message(
            updated, embed=match_expired_embed(updated)
        )
        if updated.admin_message_id:
            await self._delete_admin_message(updated.admin_message_id)

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

    async def submit_result(
        self,
        interaction: discord.Interaction,
        screenshot: discord.Attachment,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return

        if self.config.admin_channel_id is None:
            await interaction.response.send_message(
                "Result reporting is not configured on this server.",
                ephemeral=True,
            )
            return

        if not (screenshot.content_type or "").startswith("image/"):
            await interaction.response.send_message(
                "Please upload an image file.", ephemeral=True
            )
            return

        matches = await self.repo.find_accepted_for_user(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )
        if not matches:
            await interaction.response.send_message(
                "You have no active match to report.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            file_bytes = await screenshot.read()
        except Exception:
            log.exception(
                "Failed to download result screenshot for user %s",
                interaction.user.id,
            )
            await interaction.followup.send(
                "Could not download the screenshot. Try again.", ephemeral=True
            )
            return

        if len(matches) == 1:
            _, msg = await self.finalize_result(
                match=matches[0],
                submitted_by=interaction.user.id,
                file_bytes=file_bytes,
                filename=screenshot.filename,
            )
            await interaction.followup.send(msg, ephemeral=True)
            return

        from bot.views import ResultPickerView

        view = await ResultPickerView.build(
            challenges=matches[:25],
            user_id=interaction.user.id,
            service=self,
            guild=interaction.guild,
            client=self.bot,
            file_bytes=file_bytes,
            filename=screenshot.filename,
        )
        sent = await interaction.followup.send(
            "You have multiple active matches. Pick the one to submit the result for:",
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.message = sent

    async def finalize_result(
        self,
        *,
        match: Challenge,
        submitted_by: int,
        file_bytes: bytes,
        filename: str,
    ) -> tuple[bool, str]:
        """Send screenshot to admin channel and complete the match.

        Returns (success, user_facing_message). The caller decides how to
        surface the message (followup vs edit_message) to avoid duplicate
        ephemerals.
        """
        admin_channel_id = self.config.admin_channel_id
        if admin_channel_id is None:
            return False, "Result reporting is not configured on this server."

        try:
            channel = self.bot.get_channel(admin_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(admin_channel_id)
            file = discord.File(io.BytesIO(file_bytes), filename=filename)
            await channel.send(
                embed=admin_result_embed(match, submitted_by),
                file=file,
                allowed_mentions=SILENT,
            )
        except (discord.NotFound, discord.Forbidden):
            log.warning(
                "Admin channel %s unreachable for /result", admin_channel_id
            )
            return False, "Admin channel unreachable. Result not submitted."

        completed = await self.repo.set_status(match.id, STATUS_COMPLETED)
        if completed is not None:
            await self._edit_channel_message(
                completed, embed=match_completed_embed(completed)
            )
            if completed.admin_message_id:
                await self._delete_admin_message(completed.admin_message_id)

        return True, "Result submitted."

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _finalize_accepted_match(self, updated: Challenge) -> None:
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
            pass
        except discord.Forbidden:
            log.warning(
                "Missing permissions to delete admin message %s", message_id
            )
        except Exception:
            log.exception("Failed to delete admin message %s", message_id)

    async def _delete_channel_message(self, ch: Challenge) -> None:
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
