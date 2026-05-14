"""Discord UI views: AcceptButtonView (persistent) and CancelPickerView (per-interaction)."""

from __future__ import annotations

import logging

import discord

from bot.models import (
    Challenge,
    STATUS_ACCEPTED,
    STATUS_PENDING,
    TYPE_ATTACK,
)

log = logging.getLogger(__name__)


PAGE_SIZE = 25  # Discord hard cap on Select options


class AcceptButtonView(discord.ui.View):
    """Persistent view with a single static custom_id Accept button.

    Constructor args are optional; on bot restart, persistent views are
    re-attached without args. The callback resolves repo/service from
    `interaction.client` (set as attributes in main.py).
    """

    def __init__(self, service=None, repo=None):
        super().__init__(timeout=None)
        self.service = service
        self.repo = repo

    @discord.ui.button(
        label="Accept",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="match_challenge:accept",
    )
    async def accept(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        repo = self.repo or getattr(interaction.client, "repo", None)
        service = self.service or getattr(interaction.client, "service", None)

        if repo is None or service is None:
            log.error("AcceptButtonView callback could not resolve repo/service")
            await interaction.response.send_message(
                "Try again in a moment.", ephemeral=True
            )
            return

        challenge = await repo.get_by_message_id(interaction.message.id)
        if not challenge or challenge.status != STATUS_PENDING:
            await interaction.response.send_message(
                "This challenge is no longer available.", ephemeral=True
            )
            return

        # Permission gating (spec section 7)
        if challenge.opponent_id is not None:
            if interaction.user.id != challenge.opponent_id:
                await interaction.response.send_message(
                    "This challenge isn't for you.", ephemeral=True
                )
                return
        else:
            if interaction.user.id == challenge.challenger_id:
                await interaction.response.send_message(
                    "You can't accept your own challenge.", ephemeral=True
                )
                return

        await service.accept_challenge(interaction, challenge)


# ---------------------------------------------------------------------- #
# Cancel picker
# ---------------------------------------------------------------------- #

async def _resolve_name(
    user_id: int,
    guild: discord.Guild | None,
    client: discord.Client | None,
) -> str:
    """Best-effort name lookup. Tries guild member (cached → fetched), then
    global user. Falls back to '?' so we never expose a raw ID."""
    if guild is not None:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                member = None
        if member is not None:
            return member.display_name

    if client is not None:
        try:
            user = await client.fetch_user(user_id)
            return user.display_name or user.name
        except (discord.NotFound, discord.HTTPException):
            pass

    return "?"


async def _select_label(
    ch: Challenge,
    user_id: int,
    guild: discord.Guild | None,
    client: discord.Client | None,
) -> str:
    role = "Attack" if ch.challenge_type == TYPE_ATTACK else "Defend"

    if ch.status == STATUS_ACCEPTED:
        other_id = (
            ch.opponent_id
            if ch.challenger_id == user_id
            else ch.challenger_id
        )
        if other_id is None:
            label = f"🟢 Live match · code {ch.room_code}"
        else:
            other = await _resolve_name(other_id, guild, client)
            label = f"🟢 Live vs {other} · code {ch.room_code}"
    else:
        # PENDING
        if ch.opponent_id is None:
            label = f"⏳ Open {role.lower()} — waiting for opponent"
        elif ch.challenger_id == user_id:
            other = await _resolve_name(ch.opponent_id, guild, client)
            label = f"⏳ Sent to {other} — not yet accepted"
        else:
            other = await _resolve_name(ch.challenger_id, guild, client)
            label = f"⏳ From {other} — pending your accept"

    if len(label) > 100:
        label = label[:97] + "..."
    return label


def _select_description(ch: Challenge) -> str:
    if ch.status == STATUS_ACCEPTED and ch.room_code:
        return f"Active match · /result to submit"
    return "Pending · not yet matched"


class _CancelSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Pick a challenge to cancel…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "CancelPickerView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "This picker isn't for you.", ephemeral=True
            )
            return

        ch = view.challenges_by_id.get(int(self.values[0]))
        if ch is None:
            await interaction.response.send_message(
                "Challenge not found.", ephemeral=True
            )
            return

        await view.service.cancel_challenge(
            challenge=ch, cancelled_by=interaction.user.id
        )

        view.stop()
        await interaction.response.edit_message(
            content="Challenge cancelled.", view=None
        )


class _PageButton(discord.ui.Button):
    def __init__(self, *, label: str, custom_id: str, delta: int):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id,
        )
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "CancelPickerView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "This picker isn't for you.", ephemeral=True
            )
            return
        view.page += self.delta
        view._render()
        await interaction.response.edit_message(
            content=view.page_content(), view=view
        )


class CancelPickerView(discord.ui.View):
    """Per-interaction view with paginated Select for >25 entries."""

    def __init__(
        self,
        *,
        all_options: list[discord.SelectOption],
        challenges_by_id: dict[int, Challenge],
        user_id: int,
        service,
    ):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.service = service
        self.all_options = all_options
        self.challenges_by_id = challenges_by_id
        self.page = 0
        self.message: discord.Message | discord.WebhookMessage | None = None
        self._render()

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content="Picker timed out.", view=None)
        except Exception:
            pass

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.all_options) + PAGE_SIZE - 1) // PAGE_SIZE)

    def page_content(self) -> str:
        if self.total_pages == 1:
            return "Pick a challenge to cancel:"
        return f"Pick a challenge to cancel — Page {self.page + 1}/{self.total_pages}"

    def _render(self) -> None:
        self.clear_items()

        start = self.page * PAGE_SIZE
        page_opts = self.all_options[start : start + PAGE_SIZE]
        self.add_item(_CancelSelect(page_opts))

        if self.total_pages > 1:
            prev = _PageButton(label="◀ Prev", custom_id="cancel:prev", delta=-1)
            nxt = _PageButton(label="Next ▶", custom_id="cancel:next", delta=+1)
            prev.disabled = self.page == 0
            nxt.disabled = self.page >= self.total_pages - 1
            self.add_item(prev)
            self.add_item(nxt)

    @classmethod
    async def build(
        cls,
        challenges: list[Challenge],
        user_id: int,
        service,
        guild: discord.Guild | None,
        client: discord.Client | None,
    ) -> "CancelPickerView":
        challenges_by_id = {ch.id: ch for ch in challenges}
        all_options: list[discord.SelectOption] = []
        for ch in challenges:
            all_options.append(
                discord.SelectOption(
                    label=await _select_label(ch, user_id, guild, client),
                    value=str(ch.id),
                    description=_select_description(ch),
                )
            )
        return cls(
            all_options=all_options,
            challenges_by_id=challenges_by_id,
            user_id=user_id,
            service=service,
        )


# ---------------------------------------------------------------------- #
# Result picker
# ---------------------------------------------------------------------- #

async def _result_select_label(
    ch: Challenge,
    user_id: int,
    guild: discord.Guild | None,
    client: discord.Client | None,
) -> str:
    other_id = (
        ch.opponent_id if ch.challenger_id == user_id else ch.challenger_id
    )
    if other_id is None:
        label = f"Match · {ch.room_code}"
    else:
        other = await _resolve_name(other_id, guild, client)
        label = f"vs {other} · {ch.room_code}"
    if len(label) > 100:
        label = label[:97] + "..."
    return label


class _ResultSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Pick the match to submit the result for…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "ResultPickerView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "This picker isn't for you.", ephemeral=True
            )
            return

        # Guard against double-clicks while finalize_result is in flight.
        if view.submitted:
            await interaction.response.send_message(
                "Already submitted.", ephemeral=True
            )
            return
        view.submitted = True

        ch = view.challenges_by_id.get(int(self.values[0]))
        if ch is None:
            await interaction.response.send_message(
                "Match not found.", ephemeral=True
            )
            return

        # Disable the select + ack the interaction so subsequent clicks no-op.
        self.disabled = True
        await interaction.response.edit_message(
            content="Submitting result…", view=view
        )

        _, msg = await view.service.finalize_result(
            match=ch,
            submitted_by=interaction.user.id,
            file_bytes=view.file_bytes,
            filename=view.filename,
        )
        view.stop()
        await interaction.edit_original_response(content=msg, view=None)


class ResultPickerView(discord.ui.View):
    """Per-interaction picker for /result when the user has 2+ active matches.

    Holds the already-downloaded screenshot bytes so the user does not have
    to re-upload after selecting a match.
    """

    def __init__(
        self,
        *,
        all_options: list[discord.SelectOption],
        challenges_by_id: dict[int, Challenge],
        user_id: int,
        service,
        file_bytes: bytes,
        filename: str,
    ):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.service = service
        self.all_options = all_options
        self.challenges_by_id = challenges_by_id
        self.file_bytes = file_bytes
        self.filename = filename
        self.message: discord.Message | discord.WebhookMessage | None = None
        self.submitted = False
        self.add_item(_ResultSelect(all_options))

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content="Picker timed out.", view=None)
        except Exception:
            pass

    @classmethod
    async def build(
        cls,
        challenges: list[Challenge],
        user_id: int,
        service,
        guild: discord.Guild | None,
        client: discord.Client | None,
        file_bytes: bytes,
        filename: str,
    ) -> "ResultPickerView":
        challenges_by_id = {ch.id: ch for ch in challenges}
        all_options: list[discord.SelectOption] = []
        for ch in challenges:
            all_options.append(
                discord.SelectOption(
                    label=await _result_select_label(
                        ch, user_id, guild, client
                    ),
                    value=str(ch.id),
                    description=f"Code: {ch.room_code}" if ch.room_code else None,
                )
            )
        return cls(
            all_options=all_options,
            challenges_by_id=challenges_by_id,
            user_id=user_id,
            service=service,
            file_bytes=file_bytes,
            filename=filename,
        )
