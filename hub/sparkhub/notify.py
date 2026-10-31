"""ntfy notifications, set on the Settings page: on/off, server, topic, high priority only.

Two senders share these settings: the hub's own pushes (model crashed, switch failed, ...) and Grafana's alert
rules, whose contact point posts to /internal/grafana-alert here (localhost only); the hub relays that body to
ntfy with ?template=grafana. So a topic change or "off" applies to both at once, with no restart.
The topic is a secret (anyone who knows it can read the alerts): it is never shown in full or logged.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

from .db import DB

log = logging.getLogger("sparkhub.notify")

HIGH = {"high", "urgent", "max", "5", "4"}
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
Post = Callable[[str, bytes, dict[str, str]], Awaitable[int]]


def masked(topic: str) -> str:
    return f"…{topic[-4:]}" if len(topic) > 6 else ("set" if topic else "")


def validate(server: str, topic: str) -> tuple[str, str]:
    server = server.strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.-]+(:\d+)?$", server):
        raise ValueError("server must look like https://ntfy.sh (no path)")
    topic = topic.strip()
    if topic and not TOPIC_RE.match(topic):
        raise ValueError("topic: letters, digits, - and _ only, up to 64 characters")
    return server, topic


async def _http_post(url: str, body: bytes, headers: dict[str, str]) -> int:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, content=body, headers=headers)
        return r.status_code


class Notifier:
    def __init__(self, db: DB, post: Post | None = None):
        self.db = db
        self.post: Post = post or _http_post
        self._tasks: set[asyncio.Task] = set()

    def config(self) -> dict[str, Any]:
        g = self.db.get_setting
        return {"enabled": bool(g("ntfy_enabled")), "high_only": bool(g("ntfy_high_only")),
                "server": g("ntfy_server"), "topic": g("ntfy_topic") or ""}

    def view(self) -> dict[str, Any]:
        c = self.config()
        return {**c, "topic": masked(c["topic"]), "has_topic": bool(c["topic"])}

    def save(self, enabled: bool, high_only: bool, server: str, topic: str | None) -> None:
        """topic None or empty keeps the current one; clear_topic is a separate action."""
        server, topic = validate(server, topic or "")
        self.db.set_setting("ntfy_enabled", enabled)
        self.db.set_setting("ntfy_high_only", high_only)
        self.db.set_setting("ntfy_server", server)
        if topic:
            self.db.set_setting("ntfy_topic", topic)

    def clear_topic(self) -> None:
        self.db.set_setting("ntfy_topic", "")

    def _target(self) -> str | None:
        c = self.config()
        if not c["enabled"] or not c["topic"]:
            return None
        return f"{c['server']}/{c['topic']}"

    async def send(self, title: str, message: str, priority: str = "default", force: bool = False) -> int | None:
        """Hub push. Returns the ntfy status, or None if not sent (off, no topic, filtered)."""
        c = self.config()
        url = f"{c['server']}/{c['topic']}" if (force and c["topic"]) else self._target()
        if url is None or (c["high_only"] and not force and priority not in HIGH):
            return None
        try:
            return await self.post(url, message.encode(), {"Title": title, "Priority": priority, "Tags": "computer"})
        except Exception as e:
            log.warning("ntfy push failed: %s", type(e).__name__)
            return None

    def fire(self, title: str, message: str, priority: str = "default") -> None:
        """Fire and forget, from sync code inside the event loop."""
        t = asyncio.create_task(self.send(title, message, priority))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def relay_grafana(self, body: bytes) -> int | None:
        """Grafana alert webhook body -> ntfy, formatted by ntfy's built-in grafana template.
        Alerts always count as high priority for the filter (they are what that option keeps)."""
        url = self._target()
        if url is None:
            return None
        try:
            return await self.post(url + "?template=grafana", body, {"Content-Type": "application/json"})
        except Exception as e:
            log.warning("ntfy relay failed: %s", type(e).__name__)
            return None


def migrate_topic_file(db: DB, data_dir) -> bool:
    """One-off: copy data/ntfy-topic into the settings (older versions kept it in a file). The file is left in place."""
    if db.one("SELECT 1 FROM settings WHERE key='ntfy_topic'"):
        return False
    try:
        topic = (data_dir / "ntfy-topic").read_text().strip()
    except OSError:
        return False
    if topic and TOPIC_RE.match(topic):
        db.set_setting("ntfy_topic", topic)
        db.event("ntfy_topic_migrated", "boot")
        return True
    return False
