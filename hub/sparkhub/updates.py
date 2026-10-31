"""Release check: once a day, the newest GitHub release of SparkHub (setting update_check, on by default).

Only a GET of the public releases API: nothing about this install is sent. A newer release shows a banner to admins
(with its notes and `./install.sh upgrade`) and is pushed once per version through ntfy. The upgrade itself runs on
the host (installer.py upgrade): the hub has no way to change its own code.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable

import httpx

from .config import Config
from .db import DB

log = logging.getLogger("sparkhub.updates")

CHECK_EVERY_S = 24 * 3600
FIRST_CHECK_S = 120          # after the hub starts (not during the boot model load's first minutes)
NOTES_MAX = 4000


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3); anything else (dev, a pre-release suffix is ignored) -> None."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


class UpdateChecker:
    def __init__(self, cfg: Config, db: DB, notify: Callable[..., None] | None = None):
        self.cfg = cfg
        self.db = db
        self.notify = notify or (lambda *a, **k: None)
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"https://api.github.com/repos/{self.cfg.update_repo}/releases/latest"

    async def fetch(self) -> dict[str, Any]:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": f"SparkHub/{self.cfg.version}"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(self.url, headers=headers)
            r.raise_for_status()
            return r.json()

    async def check(self) -> dict[str, Any] | None:
        """Ask GitHub now; the result is kept in the settings (update_latest) so a restart still shows it."""
        if not self.db.get_setting("update_check"):
            return None
        try:
            rel = await self.fetch()
            latest = {"tag": rel["tag_name"], "name": rel.get("name") or rel["tag_name"], "url": rel.get("html_url"),
                      "notes": (rel.get("body") or "")[:NOTES_MAX], "published_at": rel.get("published_at"),
                      "checked_at": time.time(), "error": None}
        except Exception as e:     # offline, rate-limited, no release yet: try again tomorrow
            prev = self.db.get_setting("update_latest") or {}
            latest = {**prev, "checked_at": time.time(), "error": f"{type(e).__name__}: {e}"[:300]}
            log.info("update check: %s", latest["error"])
        self.db.set_setting("update_latest", latest)
        avail = self.available()
        if avail and self.db.get_setting("update_notified") != avail["tag"]:
            self.db.set_setting("update_notified", avail["tag"])
            self.notify(f"SparkHub {avail['tag']} is available",
                        f"This install runs {self.cfg.version}. To upgrade, on the Spark: ./install.sh upgrade")
        return latest

    def available(self) -> dict[str, Any] | None:
        """The newer release, or None (up to date, not checked, check off, or a dev build)."""
        if not self.db.get_setting("update_check"):
            return None
        latest = self.db.get_setting("update_latest") or {}
        new, cur = parse_version(latest.get("tag")), parse_version(self.cfg.version)
        return latest if new and cur and new > cur else None

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_CHECK_S)
        while True:
            last = (self.db.get_setting("update_latest") or {}).get("checked_at") or 0
            wait = last + CHECK_EVERY_S - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 3600))     # re-read hourly: the setting may be turned on later
                continue
            try:
                await self.check()
            except Exception:
                log.exception("update check")
            await asyncio.sleep(60)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
