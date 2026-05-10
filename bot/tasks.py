"""Background tasks: ExpiryLoop scans for PENDING challenges past expires_at."""

from __future__ import annotations

import logging

from discord.ext import tasks

from bot.models import STATUS_EXPIRED
from bot.repository import ChallengeRepository
from bot.service import ChallengeService

log = logging.getLogger(__name__)


class ExpiryLoop:
    """Wraps a discord.ext.tasks loop so we can keep service/repo as instance state."""

    def __init__(self, service: ChallengeService, repo: ChallengeRepository):
        self.service = service
        self.repo = repo
        self._task = self._build_task()

    def _build_task(self):
        @tasks.loop(minutes=5)
        async def _loop():
            try:
                expired = await self.repo.fetch_expired_pending()
            except Exception:
                log.exception("ExpiryLoop: fetch_expired_pending failed")
                return
            for ch in expired:
                try:
                    updated = await self.repo.set_status(ch.id, STATUS_EXPIRED)
                    if updated is not None:
                        await self.service.expire_challenge(updated)
                except Exception:
                    log.exception(
                        "ExpiryLoop: failed to expire challenge %s", ch.id
                    )

        return _loop

    def start(self) -> None:
        self._task.start()

    def stop(self) -> None:
        self._task.cancel()
