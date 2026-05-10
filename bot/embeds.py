"""Discord embed builders.

Embeds give us the polished look (colors, fields, code blocks). To actually
*ping* a user, the call site sends the user mentions in the message
`content` alongside the embed — mentions inside an embed body don't trigger
notifications.
"""

import discord

from bot.models import Challenge, TYPE_ATTACK


COLOR_ATTACK = 0xE74C3C
COLOR_DEFEND = 0x3498DB
COLOR_LIVE = 0x2ECC71
COLOR_GRAY = 0x95A5A6


def attacker_defender_ids(ch: Challenge) -> tuple[int, int]:
    """Return (attacker_id, defender_id) based on challenge_type.

    For accepted challenges, ch.opponent_id must be set.
    """
    if ch.challenge_type == TYPE_ATTACK:
        return ch.challenger_id, ch.opponent_id  # type: ignore[return-value]
    return ch.opponent_id, ch.challenger_id  # type: ignore[return-value]


# ---------------------------------------------------------------------- #
# Public-channel embeds
# ---------------------------------------------------------------------- #

def direct_challenge_embed(ch: Challenge) -> discord.Embed:
    challenger = f"<@{ch.challenger_id}>"
    opponent = f"<@{ch.opponent_id}>"

    if ch.challenge_type == TYPE_ATTACK:
        title = "🗡️ Attack Challenge"
        color = COLOR_ATTACK
        roles = f"{challenger} attacks · {opponent} defends"
    else:
        title = "🛡️ Defend Challenge"
        color = COLOR_DEFEND
        roles = f"{opponent} attacks · {challenger} defends"

    embed = discord.Embed(
        title=title,
        description=(
            f"{challenger} has challenged {opponent}.\n"
            f"{roles}\n\n"
            f"Tap **Accept** below to start the match."
        ),
        color=color,
    )
    embed.set_footer(text="Expires in 24h · /cancel to withdraw")
    return embed


def open_challenge_embed(ch: Challenge) -> discord.Embed:
    challenger = f"<@{ch.challenger_id}>"

    if ch.challenge_type == TYPE_ATTACK:
        title = "⏳ Open Challenge — Looking for Defender"
        color = COLOR_ATTACK
        hint = "Tap **Accept** below or run `/defend` to take the match."
    else:
        title = "⏳ Open Challenge — Looking for Attacker"
        color = COLOR_DEFEND
        hint = "Tap **Accept** below or run `/attack` to take the match."

    embed = discord.Embed(
        title=title,
        description=f"{challenger} is waiting for an opponent.\n{hint}",
        color=color,
    )
    embed.set_footer(text="Expires in 24h · /cancel to withdraw")
    return embed


def live_match_embed(ch: Challenge) -> discord.Embed:
    a, d = attacker_defender_ids(ch)
    embed = discord.Embed(
        title="⚔️ Match",
        color=COLOR_LIVE,
    )
    embed.add_field(name="🗡️ Attacker", value=f"<@{a}>", inline=True)
    embed.add_field(name="🛡️ Defender", value=f"<@{d}>", inline=True)
    embed.add_field(
        name="🔑 Room Code",
        value=f"```\n{ch.room_code}\n```",
        inline=False,
    )
    embed.add_field(
        name="📸 Post a result screenshot to <#1499418254861664450>",
        value="Include the room code in your message.",
        inline=False,
    )
    embed.set_footer(text="Matched by mistake? Run /cancel")
    return embed


def match_cancelled_embed(ch: Challenge, cancelled_by: int) -> discord.Embed:
    a, d = attacker_defender_ids(ch)
    return discord.Embed(
        title="🚫 Match Cancelled",
        description=(
            f"Cancelled by <@{cancelled_by}>\n"
            f"Was: <@{a}> (Attacker) vs <@{d}> (Defender) · `{ch.room_code}`"
        ),
        color=COLOR_GRAY,
    )


def challenge_cancelled_embed(ch: Challenge, cancelled_by: int) -> discord.Embed:
    return discord.Embed(
        title="🚫 Challenge Cancelled",
        description=f"Cancelled by <@{cancelled_by}>",
        color=COLOR_GRAY,
    )


def challenge_expired_embed(ch: Challenge) -> discord.Embed:
    return discord.Embed(
        title="⏰ Challenge Expired",
        description="No one accepted within 24h.",
        color=COLOR_GRAY,
    )


# ---------------------------------------------------------------------- #
# Admin-channel embeds (no pings, embed-only)
# ---------------------------------------------------------------------- #

def admin_match_started_embed(ch: Challenge) -> discord.Embed:
    a, d = attacker_defender_ids(ch)
    return discord.Embed(
        description=(
            f"🔑 `{ch.room_code}`  ·  🗡️ <@{a}>  vs  🛡️ <@{d}>"
        ),
        color=COLOR_LIVE,
    )
