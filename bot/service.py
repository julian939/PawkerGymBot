"""ChallengeService — orchestrates challenge lifecycle."""

from __future__ import annotations

import io
import logging

import aiosqlite
import discord

from bot.code_generator import generate_unique_room_code
from bot.config import Config
from bot.embeds import (
    attacker_defender_ids,
    direct_challenge_embed,
    live_match_embed,
    match_expired_embed,
    open_challenge_embed,
    result_log_content,
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

# Reject result attachments larger than this (8 MB is plenty for screenshots).
MAX_RESULT_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _ping_users(*user_ids: int) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=uid) for uid in user_ids],
    )


def _mentions(*user_ids: int) -> str:
    return " ".join(f"<@{uid}>" for uid in user_ids)


def _is_image_attachment(att: discord.Attachment) -> bool:
    if (att.content_type or "").startswith("image/"):
        return True
    name = (att.filename or "").lower()
    return name.endswith(ALLOWED_IMAGE_EXTS)


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
                "You can't challenge yourself. Pick another player.",
                ephemeral=True,
            )
            return
        if opponent.bot:
            await interaction.response.send_message(
                "Bots can't play — pick a real user.", ephemeral=True
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command only works in a server, not in DMs.",
                ephemeral=True,
            )
            return

        # Replace any previous PENDING challenge by the caller in this guild.
        await self._cancel_existing_pending(
            user_id=caller.id, guild_id=interaction.guild_id
        )

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
                "This command only works in a server, not in DMs.",
                ephemeral=True,
            )
            return

        wanted_type = TYPE_DEFEND if challenge_type == TYPE_ATTACK else TYPE_ATTACK

        # Try the queue up to twice — once normally, once after a race-loss.
        for _ in range(2):
            match = await self.repo.find_queue_match(
                guild_id=interaction.guild_id,
                wanted_type=wanted_type,
                requester_id=caller.id,
            )
            if match is None:
                break
            updated = await self._accept_with_code_retry(match, caller.id)
            if updated is not None:
                await interaction.response.send_message(
                    "Matched! Check the channel for your room code.",
                    ephemeral=True,
                )
                await self._finalize_accepted_match(updated)
                return
            # Race lost — try the queue once more.

        # No queue match — create / replace our own open challenge.
        await self._cancel_existing_pending(
            user_id=caller.id, guild_id=interaction.guild_id
        )

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
        updated = await self._accept_with_code_retry(
            challenge, interaction.user.id
        )
        if updated is None:
            await interaction.response.send_message(
                "This challenge is no longer available.", ephemeral=True
            )
            return

        # Disarm the Accept button immediately so a double-click can't trigger
        # a second accept while we're posting the live embed.
        try:
            await interaction.response.edit_message(view=None)
        except (discord.NotFound, discord.HTTPException):
            log.debug("accept_challenge: edit_message ack failed", exc_info=True)

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

    async def expire_pending_challenge(self, ch: Challenge) -> None:
        updated = await self.repo.set_status(
            ch.id, STATUS_CANCELLED, cancelled_by=None
        )
        if updated is None:
            return
        await self._delete_channel_message(updated)

    async def cancel_challenge(
        self,
        *,
        challenge: Challenge,
        cancelled_by: int,
    ) -> None:
        updated = await self.repo.set_status(
            challenge.id, STATUS_CANCELLED, cancelled_by=cancelled_by
        )
        if updated is None:
            # Already terminal (raced with /result or another cancel).
            return

        await self._delete_channel_message(updated)

    async def submit_result(
        self,
        interaction: discord.Interaction,
        screenshot: discord.Attachment,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command only works in a server, not in DMs.",
                ephemeral=True,
            )
            return

        if self.config.result_channel_id is None:
            await interaction.response.send_message(
                "Result reporting isn't configured on this server. "
                "Ask an admin to set RESULT_CHANNEL_ID.",
                ephemeral=True,
            )
            return

        if not _is_image_attachment(screenshot):
            await interaction.response.send_message(
                "Please upload an image (.png, .jpg, .webp, .gif).",
                ephemeral=True,
            )
            return

        if screenshot.size and screenshot.size > MAX_RESULT_BYTES:
            await interaction.response.send_message(
                "That screenshot is too big (max 8 MB). Compress it and try again.",
                ephemeral=True,
            )
            return

        matches = await self.repo.find_accepted_for_user(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )
        if not matches:
            await interaction.response.send_message(
                "You have no active match to report. Use /attack or /defend first.",
                ephemeral=True,
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
        """Send screenshot to the result log channel and complete the match."""
        channel_id = self.config.result_channel_id
        if channel_id is None:
            return False, "Result reporting isn't configured on this server."

        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            file = discord.File(io.BytesIO(file_bytes), filename=filename)
            await channel.send(
                content=result_log_content(match, submitted_by),
                file=file,
                allowed_mentions=SILENT,
            )
        except (discord.NotFound, discord.Forbidden):
            log.warning(
                "Result channel %s unreachable for /result", channel_id
            )
            return False, "Result channel unreachable. Result not submitted."

        completed = await self.repo.set_status(match.id, STATUS_COMPLETED)
        if completed is not None:
            # Live embed in the game channel no longer adds info — remove it.
            await self._delete_channel_message(completed)
            return True, "Result submitted. Thanks!"

        # Row was no longer ACCEPTED (already cancelled/completed). Log only.
        log.info(
            "finalize_result: challenge %s not in ACCEPTED state, log only",
            match.id,
        )
        return True, "Result logged."

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _accept_with_code_retry(
        self, match: Challenge, opponent_id: int, attempts: int = 5
    ) -> Challenge | None:
        """Try to accept; retry on rare room_code uniqueness collisions."""
        for _ in range(attempts):
            code = await generate_unique_room_code(self.repo)
            try:
                return await self.repo.accept_atomic(
                    match.id, opponent_id, code
                )
            except aiosqlite.IntegrityError:
                log.warning(
                    "Room code collision on accept for match %s; retrying",
                    match.id,
                )
                continue
        log.error(
            "Gave up generating unique room code for match %s", match.id
        )
        return None

    async def _cancel_existing_pending(
        self,
        *,
        user_id: int,
        guild_id: int,
        except_id: int | None = None,
    ) -> None:
        """Cancel all PENDING challenges by this user (and delete their embeds).

        Used when the user starts a fresh /attack or /defend to clear the
        previous attempt automatically — see also the queue-match path where
        the user just got matched and any leftover open challenge is stale.
        """
        pending = await self.repo.find_pending_by_challenger(
            user_id=user_id, guild_id=guild_id
        )
        for ch in pending:
            if except_id is not None and ch.id == except_id:
                continue
            updated = await self.repo.set_status(
                ch.id, STATUS_CANCELLED, cancelled_by=user_id
            )
            if updated is not None:
                await self._delete_channel_message(updated)

    async def _finalize_accepted_match(self, updated: Challenge) -> None:
        """Post the live embed, persist its message_id, then delete the old one.

        Posting first (instead of delete-then-post) guarantees the room code
        is visible even if a later step fails. The new message_id is written
        back to the DB so /cancel deletes the right message.
        """
        # Clear any stale PENDING challenges either participant still has.
        if updated.opponent_id is not None:
            await self._cancel_existing_pending(
                user_id=updated.challenger_id,
                guild_id=updated.guild_id,
                except_id=updated.id,
            )
            await self._cancel_existing_pending(
                user_id=updated.opponent_id,
                guild_id=updated.guild_id,
                except_id=updated.id,
            )

        old_channel_id = updated.channel_id
        old_message_id = updated.message_id

        a_id, d_id = attacker_defender_ids(updated)

        new_message: discord.Message | None = None
        try:
            channel = self.bot.get_channel(updated.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(updated.channel_id)
            new_message = await channel.send(
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

        if new_message is not None:
            await self.repo.set_message_id(updated.id, new_message.id)
            # Now delete the old PENDING embed.
            await self._delete_message(old_channel_id, old_message_id)
        else:
            # Couldn't post a new message — edit the old one in place so the
            # room code is still visible to the players.
            await self._edit_message(
                old_channel_id,
                old_message_id,
                embed=live_match_embed(updated),
            )

    # ---------------------- low-level message helpers ----------------- #

    async def _resolve_channel(self, channel_id: int):
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
        except discord.NotFound:
            return None
        except Exception:
            log.exception("Failed to resolve channel %s", channel_id)
            return None
        return channel

    async def _delete_message(self, channel_id: int, message_id: int) -> None:
        channel = await self._resolve_channel(channel_id)
        if channel is None:
            return
        partial = channel.get_partial_message(message_id)
        try:
            await partial.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.info(
                "Cannot delete message %s in %s; clearing instead",
                message_id,
                channel_id,
            )
            try:
                await partial.edit(content=None, embed=None, view=None)
            except Exception:
                log.exception(
                    "Fallback edit also failed for message %s", message_id
                )
        except Exception:
            log.exception("Failed to delete message %s", message_id)

    async def _edit_message(
        self, channel_id: int, message_id: int, *, embed: discord.Embed
    ) -> None:
        channel = await self._resolve_channel(channel_id)
        if channel is None:
            return
        partial = channel.get_partial_message(message_id)
        try:
            await partial.edit(
                content=None, embed=embed, view=None, allowed_mentions=SILENT
            )
        except discord.NotFound:
            log.info("Message %s not found; skipping edit", message_id)
        except discord.Forbidden:
            log.warning("No permission to edit message %s", message_id)
        except Exception:
            log.exception("Unexpected error editing message %s", message_id)

    async def _delete_channel_message(self, ch: Challenge) -> None:
        await self._delete_message(ch.channel_id, ch.message_id)

    async def _edit_channel_message(
        self,
        ch: Challenge,
        *,
        embed: discord.Embed,
    ) -> None:
        await self._edit_message(ch.channel_id, ch.message_id, embed=embed)
