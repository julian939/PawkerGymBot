# Match Challenge Bot — Feature Spec

A standalone Discord bot that coordinates 1v1 card game match handshakes between users. Deployed on **Railway** with PostgreSQL.

---

## 1. Overview

The bot's only job is to **coordinate the handshake** for matches played in an external 1v1 card game: agreeing on who plays attacker vs defender, generating a unique room code, and displaying it to both players. The actual match happens outside Discord — there is no result reporting, scoring, or screenshot handling.

### Tech Stack
- Python 3.12
- **discord.py 2.4+** (slash commands, persistent views, app_commands)
- **PostgreSQL** via **asyncpg** (Railway PostgreSQL plugin)
- Pure asyncpg, no ORM (mini-tool simplicity)
- Deployed on **Railway** via Dockerfile (Nixpacks has been flaky)

### UI Language
All user-facing text in **English**. Logs in English.

---

## 2. Commands

Three slash commands. That's it.

### `/attack [user?]`
Challenge someone to a match where **the caller plays the Attacker role**.
- `user` provided → direct challenge
- `user` omitted → posted as Open Challenge in the server's queue. If a matching Open Defend Challenge exists in the same guild, **auto-match instantly** (LIFO).

### `/defend [user?]`
Same as `/attack` but the caller plays the **Defender role**.

### `/cancel`
No parameters. Withdraws the caller's currently active challenge.
- 0 active → ephemeral: `"You have no active challenges."`
- 1 active → cancel immediately, ephemeral confirmation
- 2+ active → ephemeral message with a button picker

`/cancel` works on both **PENDING** and **ACCEPTED** challenges. Cancelling an accepted match is intentional — covers the "matched by accident" case.

---

## 3. State Machine

```
            ┌─► ACCEPTED ─► CANCELLED  (cancelled mid-match)
PENDING ────┤
            ├─► CANCELLED  (challenger withdrew before accept)
            └─► EXPIRED    (24h no acceptance)
```

| Status | Meaning |
|---|---|
| `PENDING` | Challenge posted, not yet accepted |
| `ACCEPTED` | Match has begun, room code visible |
| `CANCELLED` | Withdrawn by a participant |
| `EXPIRED` | Auto-expired after 24h without acceptance |

`CANCELLED` and `EXPIRED` are terminal. There is no result/completion state.

---

## 4. Matching Logic

### Direct Challenge (`user` provided)
1. Validate: not self, target not a bot, no existing PENDING/ACCEPTED challenge between this exact pair.
2. Insert with `status=PENDING`, `opponent_id=<target>`.
3. Post embed with target mention + Accept button.
4. **Direct challenges never auto-match against the queue.**

### Queue Matching (`user` omitted)
On `/attack` with no target:
1. Look up the **most recent** PENDING Open Challenge in the same guild with `challenge_type='defend'`, `opponent_id IS NULL`, and `challenger_id != caller_id` (LIFO).
2. **Match found** → atomic update of that row to ACCEPTED + opponent_id + room_code. Edit its embed in place to LIVE.
3. **No match found** → create new PENDING Open Challenge with `challenge_type='attack'`. Post Open Challenge embed.

Symmetric for `/defend` (looks for `challenge_type='attack'` in the queue).

### Atomicity
Conditional UPDATE prevents two callers from grabbing the same open challenge simultaneously:

```sql
UPDATE challenges
SET status = 'ACCEPTED', opponent_id = $1, room_code = $2, accepted_at = NOW()
WHERE id = $3 AND status = 'PENDING' AND opponent_id IS NULL
RETURNING *;
```

If `RETURNING` yields no row → the match was taken; fall back to creating a new Open Challenge.

---

## 5. Room Code Generation

### Format
- **4 characters**, uppercase alphanumeric
- Alphabet: `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (no `0`, `O`, `1`, `I`, `L`)
- Total namespace: 30^4 = **810,000** possible codes

### Uniqueness
Codes are **permanently flagged as used** once assigned. Never recycled, except in the extremely unlikely case the entire namespace is exhausted.

```python
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

async def generate_unique_room_code(repo) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(ALPHABET) for _ in range(4))
        if not await repo.code_ever_used(code):
            return code
    return await repo.oldest_used_code()  # fallback only
```

```sql
-- code_ever_used:
SELECT EXISTS(SELECT 1 FROM challenges WHERE room_code = $1);
```

DB-level race protection via `UNIQUE` index (see schema).

Code is generated only at the moment of acceptance — never on PENDING.

---

## 6. Embeds

All embeds are posted in the channel where the command was invoked. The same Discord message is **edited in place** as state changes (we keep `message_id`).

### 6.1 Direct Challenge — PENDING (Attack)

```
🗡️ Challenge — Attack

<@challenger_id> challenges <@opponent_id>
Roles: <@challenger_id> → Attacker  •  <@opponent_id> → Defender
Expires in 24h

ℹ️ Use /cancel to withdraw
```
- Color: red (attack) / blue (defend)
- Buttons: `[ Accept ✅ ]`

For `/defend`:
```
🛡️ Challenge — Defend

<@challenger_id> challenges <@opponent_id>
Roles: <@challenger_id> → Defender  •  <@opponent_id> → Attacker
Expires in 24h

ℹ️ Use /cancel to withdraw
```

### 6.2 Open Challenge — PENDING (queued)

```
⏳ Open Challenge — Looking for Defender

<@challenger_id> is waiting for an opponent.
Use /defend to take this match.
Expires in 24h.

ℹ️ Use /cancel to withdraw your challenge
```
- Buttons: `[ Accept ✅ ]` (equivalent to running `/defend` for the clicker)
- Symmetric for "Looking for Attacker" with `/attack` hint

### 6.3 Match — ACCEPTED (LIVE)

````
⚔️ Match — LIVE

🗡️ Attacker:  <@attacker_id>
🛡️ Defender:  <@defender_id>

Room Code:
```
K7M9
```

ℹ️ Matched by accident? Use /cancel
````

The triple-backtick code block gives Discord's native copy button.

`attacker_id` / `defender_id` mapping:
- If `/attack` initiated: challenger is attacker, opponent is defender
- If `/defend` initiated: challenger is defender, opponent is attacker

### 6.4 Match — CANCELLED

```
🚫 Match Cancelled

Cancelled by <@cancelled_by>
Was: <@attacker_id> (Attacker) vs <@defender_id> (Defender) — K7M9
```

### 6.5 Challenge — CANCELLED (before acceptance)

```
🚫 Challenge Cancelled

Cancelled by <@challenger_id>
```

### 6.6 Challenge — EXPIRED

```
⏰ Challenge Expired

No one accepted within 24h.
```

---

## 7. Button Behavior

Single Accept button. Permission gating via `interaction.user.id` check.

**Direct challenge:** only `opponent_id` may click.
```python
if interaction.user.id != challenge.opponent_id:
    await interaction.response.send_message(
        "This challenge isn't for you.", ephemeral=True
    )
    return
```

**Open challenge:** anyone except the challenger.
```python
if interaction.user.id == challenge.challenger_id:
    await interaction.response.send_message(
        "You can't accept your own challenge.", ephemeral=True
    )
    return
```

After successful Accept:
1. Atomic UPDATE (section 4) → ACCEPTED + room_code + opponent_id
2. Edit original message to LIVE Match embed
3. Disable / remove Accept button (set `view=None` on edit)

If atomic UPDATE returns no row → ephemeral: `"This challenge is no longer available."`

### Persistent Views (survive bot restart)

Use a single persistent view with a static `custom_id`. Look up which challenge it belongs to via `interaction.message.id`:

```python
class AcceptButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Accept",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="match_challenge:accept",
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = await repo.get_by_message_id(interaction.message.id)
        if not challenge or challenge.status != "PENDING":
            return await interaction.response.send_message(
                "This challenge is no longer available.", ephemeral=True
            )
        # ... permission check + service.accept(...)
```

Register once in `setup_hook`:
```python
async def setup_hook(self):
    self.add_view(AcceptButtonView())
```

---

## 8. Cancel Flow

```python
@app_commands.command(name="cancel", description="Cancel your active challenge")
async def cancel(self, interaction: discord.Interaction):
    active = await self.repo.find_active_for_user(
        user_id=interaction.user.id,
        guild_id=interaction.guild_id,
    )
    # active = challenges where user is challenger OR opponent
    # AND status IN ('PENDING', 'ACCEPTED')

    if not active:
        return await interaction.response.send_message(
            "You have no active challenges.", ephemeral=True
        )

    if len(active) == 1:
        await self.service.cancel_challenge(
            challenge=active[0], cancelled_by=interaction.user.id
        )
        return await interaction.response.send_message(
            "Challenge cancelled.", ephemeral=True
        )

    view = CancelPickerView(active, user_id=interaction.user.id)
    await interaction.response.send_message(
        "You have multiple active challenges. Pick one to cancel:",
        view=view, ephemeral=True,
    )
```

`CancelPickerView` shows one button per challenge with labels like:
- `vs @Bob — Attack — Live`
- `Open Defend — Pending`

Buttons in a `CancelPickerView` use unique `custom_id`s per challenge id and are **non-persistent** (timeout 5 min) since they only live as long as the ephemeral message.

`service.cancel_challenge()` does:
1. UPDATE `status='CANCELLED', cancelled_at=NOW(), cancelled_by=<user_id>`
2. Edit the channel message to the appropriate cancelled embed (6.4 or 6.5)
3. Set `view=None` to remove buttons

---

## 9. Background Tasks

Single task, runs every 5 minutes:

```python
@tasks.loop(minutes=5)
async def expire_pending(self):
    expired = await self.repo.fetch_expired_pending()
    for ch in expired:
        await self.repo.set_status(ch.id, "EXPIRED")
        await self.service.edit_message_to_expired(ch)
```

```sql
SELECT * FROM challenges
WHERE status = 'PENDING' AND expires_at < NOW();
```

Started in `setup_hook`. Stopped on `cog_unload`.

---

## 10. Data Model

### Schema (PostgreSQL)

```sql
CREATE TABLE IF NOT EXISTS challenges (
    id              BIGSERIAL PRIMARY KEY,
    challenger_id   BIGINT NOT NULL,
    opponent_id     BIGINT,                       -- NULL = open challenge
    challenge_type  TEXT NOT NULL CHECK (challenge_type IN ('attack', 'defend')),
    status          TEXT NOT NULL CHECK (status IN ('PENDING', 'ACCEPTED', 'CANCELLED', 'EXPIRED')),
    room_code       TEXT,                         -- set only when ACCEPTED
    guild_id        BIGINT NOT NULL,
    channel_id      BIGINT NOT NULL,
    message_id      BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at     TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ,
    cancelled_by    BIGINT,
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS challenges_room_code_uniq
    ON challenges (room_code) WHERE room_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS challenges_queue_lookup
    ON challenges (guild_id, status, challenge_type, opponent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS challenges_challenger_status
    ON challenges (challenger_id, status);

CREATE INDEX IF NOT EXISTS challenges_opponent_status
    ON challenges (opponent_id, status);

CREATE INDEX IF NOT EXISTS challenges_expiry
    ON challenges (status, expires_at);

CREATE INDEX IF NOT EXISTS challenges_message_id
    ON challenges (message_id);
```

Schema is run on bot startup as idempotent SQL (no migration tool needed for v1).

---

## 11. Project Structure

```
match-challenge-bot/
├── pyproject.toml
├── Dockerfile
├── railway.json
├── .env.example
├── .gitignore
├── README.md
├── schema.sql
└── bot/
    ├── __init__.py
    ├── main.py             # entry point
    ├── config.py           # env loading
    ├── database.py         # asyncpg pool + schema bootstrap
    ├── models.py           # Challenge dataclass, enums
    ├── repository.py       # all DB access
    ├── service.py          # business logic, embed editing
    ├── code_generator.py   # unique code generation
    ├── embeds.py           # pure embed builders
    ├── views.py            # AcceptButtonView, CancelPickerView
    ├── cog.py              # ChallengesCog: commands + button handlers
    └── tasks.py            # ExpiryLoop
```

### Module Responsibilities
- **main.py** — instantiates Bot, sets up DB pool, registers cog, runs
- **config.py** — `Config.from_env()` returns a frozen dataclass
- **database.py** — `create_pool()`, `bootstrap_schema()`
- **cog.py** knows only `ChallengeService` — handles command registration, button callbacks, validation, delegates to service
- **service.py** orchestrates: talks to repo, generates codes, builds embeds, edits Discord messages
- **repository.py** is the only place that touches the DB — returns `Challenge` dataclasses
- **embeds.py** are pure functions: `Challenge → discord.Embed`
- **views.py** holds View subclasses
- **tasks.py** holds the expiry loop

---

## 12. Bot Bootstrapping

### `bot/main.py`

```python
import asyncio
import logging

import discord
from discord.ext import commands

from bot.config import Config
from bot.database import create_pool, bootstrap_schema
from bot.cog import ChallengesCog
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
        # No message_content needed for slash commands
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.pool = None

    async def setup_hook(self):
        self.pool = await create_pool(self.config.database_url)
        await bootstrap_schema(self.pool)

        repo = ChallengeRepository(self.pool)
        service = ChallengeService(self, repo, self.config)
        await self.add_cog(ChallengesCog(self, service, repo))

        # Register persistent view for Accept buttons
        self.add_view(AcceptButtonView(service, repo))

        # Sync slash commands
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", self.config.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Synced global commands")

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)


async def main():
    config = Config.from_env()
    bot = MatchChallengeBot(config)
    try:
        await bot.start(config.discord_token)
    finally:
        if bot.pool:
            await bot.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
```

### `bot/config.py`

```python
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    discord_token: str
    database_url: str
    dev_guild_id: int | None
    challenge_expiry_hours: int = 24
    max_pending_per_user: int = 3
    expiry_loop_interval_minutes: int = 5

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ["DISCORD_TOKEN"]
        db_url = os.environ["DATABASE_URL"]
        dev_guild = os.environ.get("DEV_GUILD_ID")
        return cls(
            discord_token=token,
            database_url=db_url,
            dev_guild_id=int(dev_guild) if dev_guild else None,
        )
```

### `bot/database.py`

```python
from pathlib import Path
import asyncpg


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )


async def bootstrap_schema(pool: asyncpg.Pool) -> None:
    schema_sql = Path(__file__).parent.parent.joinpath("schema.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema_sql)
```

`schema.sql` lives at the repo root and contains the SQL from section 10.

---

## 13. Validations & Edge Cases

### Pre-creation
- ❌ Self-challenge (`caller == target`) → ephemeral error
- ❌ Target is a bot → ephemeral error
- ❌ Pair already has active challenge in this guild → ephemeral error
- ⚠️ Caller has 3+ PENDING (configurable) → ephemeral error

### Acceptance
- ❌ Wrong user clicking Accept (direct) → ephemeral "not for you"
- ❌ Self clicking Accept (open) → ephemeral "can't accept your own"
- ❌ Atomic UPDATE returns no row → ephemeral "no longer available"

### Cancel
- ❌ No active challenges → ephemeral "no active challenges"
- ✅ Caller can cancel both PENDING and ACCEPTED where they're a participant

### Other
- **User leaves guild** → mentions render as "Unknown User"; cleanup happens via expire or `/cancel`
- **Original message deleted** → catch `discord.NotFound` on edit, proceed with DB update silently
- **Bot restart** → persistent view (single static `custom_id`) reattaches automatically; lookup by `interaction.message.id`
- **Slash command sync delays** → use `DEV_GUILD_ID` env var during development for instant per-guild sync

---

## 14. Deployment (Railway)

### Add to Railway
1. Create new project in Railway
2. Add **PostgreSQL** plugin → automatically provides `DATABASE_URL`
3. Connect this GitHub repo as a service
4. Set service env vars (next section)
5. Railway auto-deploys on git push

### Environment Variables

`.env.example`:
```
DISCORD_TOKEN=your_bot_token_here
DATABASE_URL=postgresql://user:pass@host:5432/db   # Railway provides this
DEV_GUILD_ID=                                       # optional; if set, syncs commands only to this guild
```

In Railway dashboard, set:
- `DISCORD_TOKEN` (from Discord Developer Portal → your app → Bot → Reset Token)
- `DATABASE_URL` (auto-injected by Postgres plugin if you reference the variable; otherwise copy from Postgres service)
- `DEV_GUILD_ID` (optional, only for testing)

### Dockerfile

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first (layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY . .

CMD ["python", "-m", "bot.main"]
```

### `pyproject.toml`

```toml
[project]
name = "match-challenge-bot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "discord.py>=2.4.0",
    "asyncpg>=0.29.0",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["bot"]
```

### `railway.json` (optional, locks Dockerfile build)

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "Dockerfile"
  },
  "deploy": {
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

This forces Docker build (avoids Nixpacks fallback issues).

### `.gitignore` (essentials)

```
__pycache__/
*.pyc
.env
.venv/
.idea/
.vscode/
*.log
```

### Discord Bot Setup
1. Discord Developer Portal → New Application → Bot
2. Enable nothing fancy under "Privileged Gateway Intents" (slash commands don't need any)
3. OAuth2 → URL Generator: scopes `bot` + `applications.commands`
4. Bot permissions: `Send Messages`, `Embed Links`, `Read Message History`
5. Copy token to `DISCORD_TOKEN` in Railway
6. Use the OAuth2 URL to invite the bot to your test server
7. Set `DEV_GUILD_ID` to that server's ID for instant slash command sync during dev

---

## 15. Configuration Constants

Exposed via `Config` dataclass / env vars where it makes sense:

| Constant | Default | Configurable via |
|---|---|---|
| `CHALLENGE_EXPIRY_HOURS` | 24 | env (optional) |
| `MAX_PENDING_PER_USER` | 3 | env (optional) |
| `EXPIRY_LOOP_INTERVAL_MINUTES` | 5 | env (optional) |
| `ROOM_CODE_LENGTH` | 4 | code constant |
| `ROOM_CODE_ALPHABET` | (see §5) | code constant |

---

## 16. Out of Scope (NOT in v1)

- Result reporting (win/loss)
- Screenshots
- ELO / ranking / leaderboards
- DM notifications
- Multi-game support
- Spectator features
- Admin override commands
- `/challenges` listing command
- HTTP healthcheck endpoint (Railway handles process restart on crash)

---

## 17. Implementation Checklist

- [ ] `pyproject.toml`, `Dockerfile`, `railway.json`, `.env.example`, `.gitignore`
- [ ] `schema.sql` at repo root
- [ ] `bot/config.py` — Config dataclass + `from_env()`
- [ ] `bot/database.py` — pool creation + schema bootstrap
- [ ] `bot/models.py` — `Challenge` dataclass, status/type enums
- [ ] `bot/repository.py`:
  - [ ] `insert_pending(...)`
  - [ ] `get_by_id(id)` / `get_by_message_id(msg_id)`
  - [ ] `find_queue_match(guild_id, wanted_type, requester_id)` (LIFO)
  - [ ] `accept_atomic(challenge_id, opponent_id, room_code)` (returns row or None)
  - [ ] `code_ever_used(code)` / `oldest_used_code()`
  - [ ] `find_active_for_user(user_id, guild_id)`
  - [ ] `set_status(id, status, ...)`
  - [ ] `fetch_expired_pending()`
  - [ ] `count_pending_by_challenger(user_id)`
  - [ ] `pair_has_active(user_a, user_b, guild_id)`
- [ ] `bot/code_generator.py` — generate_unique_room_code with retry + fallback
- [ ] `bot/embeds.py` — six embed builders (sections 6.1–6.6)
- [ ] `bot/views.py`:
  - [ ] `AcceptButtonView` (persistent, custom_id `match_challenge:accept`)
  - [ ] `CancelPickerView` (per-interaction, timeout 5 min)
- [ ] `bot/service.py`:
  - [ ] `create_direct_challenge(...)`
  - [ ] `create_or_match_open(...)` (handles auto-match + new queue entry)
  - [ ] `accept_challenge(...)`
  - [ ] `cancel_challenge(...)`
  - [ ] `expire_challenge(...)`
  - [ ] embed/message edit helpers
- [ ] `bot/cog.py` — `/attack`, `/defend`, `/cancel` slash commands; AcceptButtonView wiring
- [ ] `bot/tasks.py` — ExpiryLoop, started in cog_load
- [ ] `bot/main.py` — Bot subclass, setup_hook, sync logic
- [ ] Manual test plan:
  - [ ] Direct challenge → accept happy path
  - [ ] Direct challenge → wrong user clicks Accept (rejected)
  - [ ] Open `/attack` with no queue → posts open challenge
  - [ ] Open `/defend` with matching `/attack` in queue → instant match (LIFO)
  - [ ] Self-challenge rejected
  - [ ] `/cancel` with 0 / 1 / 2+ active challenges
  - [ ] Cancel mid-match (ACCEPTED → CANCELLED)
  - [ ] 24h expire (manually shorten in dev)
  - [ ] Bot restart → Accept button still works
  - [ ] Two Accepts on same open challenge race → only one wins
