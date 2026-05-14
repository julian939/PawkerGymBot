"""Discord embed builders. All single-line, minimal."""

import discord

from bot.models import Challenge, TYPE_ATTACK


COLOR_ATTACK = 0xE74C3C
COLOR_DEFEND = 0x3498DB
COLOR_LIVE = 0x2ECC71
COLOR_GRAY = 0x95A5A6


def attacker_defender_ids(ch: Challenge) -> tuple[int, int]:
    if ch.challenge_type == TYPE_ATTACK:
        return ch.challenger_id, ch.opponent_id  # type: ignore[return-value]
    return ch.opponent_id, ch.challenger_id  # type: ignore[return-value]


# ---------------------------------------------------------------------- #
# Public-channel embeds
# ---------------------------------------------------------------------- #

def direct_challenge_embed(ch: Challenge) -> discord.Embed:
    a = f"<@{ch.challenger_id}>"
    b = f"<@{ch.opponent_id}>"
    if ch.challenge_type == TYPE_ATTACK:
        embed = discord.Embed(
            description=f"🗡️ {a} challenges {b}", color=COLOR_ATTACK
        )
        embed.set_footer(text="Click ✅ Accept to play as Defender")
        return embed
    embed = discord.Embed(
        description=f"🛡️ {a} challenges {b}", color=COLOR_DEFEND
    )
    embed.set_footer(text="Click ✅ Accept to play as Attacker")
    return embed


def open_challenge_embed(ch: Challenge) -> discord.Embed:
    a = f"<@{ch.challenger_id}>"
    if ch.challenge_type == TYPE_ATTACK:
        embed = discord.Embed(
            description=f"🗡️ {a} looking for a defender",
            color=COLOR_ATTACK,
        )
        embed.set_footer(text="Click ✅ Accept (or use /defend) to match")
        return embed
    embed = discord.Embed(
        description=f"🛡️ {a} looking for an attacker",
        color=COLOR_DEFEND,
    )
    embed.set_footer(text="Click ✅ Accept (or use /attack) to match")
    return embed


def live_match_embed(ch: Challenge) -> discord.Embed:
    a, d = attacker_defender_ids(ch)
    embed = discord.Embed(
        description=f"🗡️ <@{a}> vs 🛡️ <@{d}> · *`{ch.room_code}`*",
        color=COLOR_LIVE,
    )
    embed.set_footer(text="Use /result to submit the match result · /cancel to abort")
    return embed


def match_expired_embed(ch: Challenge) -> discord.Embed:
    a, d = attacker_defender_ids(ch)
    return discord.Embed(
        description=(
            f"⏰ <@{a}> vs <@{d}> · no result submitted within 24h"
        ),
        color=COLOR_GRAY,
    )


# ---------------------------------------------------------------------- #
# Result log content (posted to RESULT_CHANNEL_ID — plain text, not embed)
# ---------------------------------------------------------------------- #

def result_log_content(ch: Challenge, submitted_by: int) -> str:
    a, d = attacker_defender_ids(ch)
    return (
        f"⚔️ <@{a}> vs. <@{d}> 🛡️\n"
        f"-# submitted by <@{submitted_by}>"
    )
