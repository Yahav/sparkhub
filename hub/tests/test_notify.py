"""Notifications: settings on the Settings page, high-priority filter, topic migration, Grafana relay."""
from __future__ import annotations

import asyncio
import time

import httpx
from urllib.parse import unquote

from sparkhub.db import DB
from sparkhub.notify import Notifier, masked, migrate_topic_file

from .test_auth import csrf_of, enrolled_client


class Capture:
    def __init__(self, status=200):
        self.sent: list[tuple[str, bytes, dict]] = []
        self.status = status

    async def __call__(self, url, body, headers):
        self.sent.append((url, body, headers))
        return self.status


def run(coro):
    return asyncio.run(coro)


def test_send_respects_switch_topic_and_filter(tmp_path):
    db = DB(tmp_path / "n.db")
    cap = Capture()
    n = Notifier(db, post=cap)
    assert run(n.send("t", "m")) is None                       # no topic yet
    n.save(True, False, "https://ntfy.sh/", "my-topic_123")
    assert run(n.send("SparkHub ready", "m")) == 200
    assert cap.sent[-1][0] == "https://ntfy.sh/my-topic_123" and cap.sent[-1][2]["Title"] == "SparkHub ready"
    n.save(True, True, "https://ntfy.sh", None)                # high only; empty topic keeps the old one
    assert db.get_setting("ntfy_topic") == "my-topic_123"
    assert run(n.send("SparkHub ready", "m")) is None
    assert run(n.send("SparkHub: model crashed again", "m", "high")) == 200
    assert run(n.send("x", "m", "default", force=True)) == 200   # the test button ignores the filter
    n.save(False, False, "https://ntfy.sh", None)
    assert run(n.send("x", "m", "urgent")) is None
    assert run(n.relay_grafana(b"{}")) is None
    n.save(True, False, "https://ntfy.example.org:8443", None)
    assert run(n.relay_grafana(b'{"alerts": []}')) == 200
    assert cap.sent[-1][0] == "https://ntfy.example.org:8443/my-topic_123?template=grafana"
    n.clear_topic()
    assert run(n.send("x", "m", "high")) is None


def test_validation_and_masking(tmp_path):
    n = Notifier(DB(tmp_path / "n.db"), post=Capture())
    for server, topic in (("ntfy.sh", "a"), ("https://ntfy.sh/path", "a"), ("https://ntfy.sh", "has space"),
                          ("https://ntfy.sh", "a/b")):
        try:
            n.save(True, False, server, topic)
            raise AssertionError(f"accepted {server} {topic}")
        except ValueError:
            pass
    assert masked("abcdefghij") == "…ghij" and masked("") == "" and masked("abc") == "set"


def test_topic_file_migrates_once(tmp_path):
    db = DB(tmp_path / "n.db")
    (tmp_path / "ntfy-topic").write_text("old-topic-xyz\n")
    assert migrate_topic_file(db, tmp_path) is True
    assert db.get_setting("ntfy_topic") == "old-topic-xyz"
    db.set_setting("ntfy_topic", "")                           # removed on the page: the file must not come back
    assert migrate_topic_file(db, tmp_path) is False and db.get_setting("ntfy_topic") == ""


def test_settings_page_and_grafana_relay(hub):
    cap = Capture()
    hub.hub.notifier.post = cap
    c, _ = enrolled_client(hub)
    r = c.get("/settings")
    assert "Notifications" in r.text and "not set" in r.text
    assert r.text.index('action="/settings-notify"') > r.text.index("</title>")   # in the page body, not the title
    assert "<form" not in r.text[:r.text.index("</title>")]
    r = c.post("/settings-notify", data={"csrf": csrf_of(c), "enabled": "on", "server": "https://ntfy.sh",
                                         "topic": "secret-topic-4242", "action": "save"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=notifications" in r.headers["location"]
    page = c.get("/settings").text
    assert "secret-topic-4242" not in page and "…4242" in page   # never shown in full
    r = c.post("/settings-notify", data={"csrf": csrf_of(c), "action": "test"}, follow_redirects=False)
    assert "test sent" in unquote(r.headers["location"]) and cap.sent[-1][2]["Title"] == "SparkHub: test"
    r = c.post("/settings-notify", data={"csrf": csrf_of(c), "server": "nope", "action": "save"},
               follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = c.post("/settings-notify", data={"csrf": "wrong", "action": "clear"}, follow_redirects=False)
    assert hub.db.get_setting("ntfy_topic") == "secret-topic-4242"   # CSRF refused
    # the hub's own pushes go through the same settings
    hub.loop.call_soon_threadsafe(hub.hub.modes.notify, "SparkHub: switch failed", "x", "high")
    # Grafana's contact point: localhost only, relayed with ?template=grafana
    r = httpx.post(hub.url + "/internal/grafana-alert", content=b'{"status": "firing"}')
    assert r.json() == {"relayed": True, "status": 200}
    assert cap.sent[-1][0].endswith("/secret-topic-4242?template=grafana") and cap.sent[-1][1] == b'{"status": "firing"}'
    r = httpx.post(hub.url + "/internal/grafana-alert", content=b"{}", headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 404
    deadline = time.time() + 5
    while not any(s[2].get("Title") == "SparkHub: switch failed" for s in cap.sent):
        assert time.time() < deadline
        time.sleep(0.05)
    # off: nothing is relayed
    c.post("/settings-notify", data={"csrf": csrf_of(c), "server": "https://ntfy.sh", "action": "save"})
    assert hub.db.get_setting("ntfy_enabled") is False
    assert httpx.post(hub.url + "/internal/grafana-alert", content=b"{}").json()["relayed"] is False
    assert any(r["kind"] == "ntfy_settings_changed" for r in hub.db.all("SELECT kind FROM events"))


def test_stack_watcher(hub):
    h = hub.hub
    sent = []
    h.notify = lambda title, msg, prio="default": sent.append((title, msg, prio))

    def comp(svc, state="running", health=None, status="Up 1 hour"):
        return {"name": f"sparkhub-{svc}", "service": svc, "model": False, "embed": False, "state": state,
                "health": health, "status": status}
    up = [comp("nginx"), comp("grafana"), comp("prometheus"), comp("arcane"),
          {**comp("fast"), "model": True, "state": "exited"}]           # the model has its own watchdog
    t = 1000.0
    h.check_stack(up, t)
    assert sent == []
    grafana_down = [c if c["service"] != "grafana" else comp("grafana", "exited", status="Exited (0) 5 s ago")
                    for c in up]
    h.check_stack(grafana_down, t + 15)
    h.check_stack(grafana_down, t + 60)                                 # not yet a minute
    assert sent == []
    h.check_stack(grafana_down, t + 76)
    h.check_stack(grafana_down, t + 90)                                 # reported once
    assert len(sent) == 1 and sent[0][0] == "SparkHub: Grafana is down" and sent[0][2] == "high"
    assert "Exited (0)" in sent[0][1] and "Grafana alerts are not sent" in sent[0][1]
    h.check_stack(up, t + 200)
    assert sent[-1][0] == "SparkHub: Grafana is back" and "down 3 min" in sent[-1][1]
    # a short blip (a restart) is not reported
    h.check_stack(grafana_down, t + 300)
    h.check_stack(up, t + 330)
    assert len(sent) == 2
    # unhealthy, and a container that disappeared
    h.check_stack([comp("nginx", health="unhealthy"), comp("prometheus"), comp("arcane")], t + 400)
    h.check_stack([comp("nginx", health="unhealthy"), comp("prometheus"), comp("arcane")], t + 470)
    titles = sorted(x[0] for x in sent[2:])
    assert titles == ["SparkHub: Grafana is down", "SparkHub: Web server is down"]
    assert any("container missing" in x[1] for x in sent) and any(": unhealthy." in x[1] for x in sent)
    rows = hub.db.all("SELECT kind FROM events WHERE kind LIKE 'container_%'")
    assert [r["kind"] for r in rows].count("container_down") == 3


def test_stack_watcher_skips_chat_when_off(hub):
    h = hub.hub
    sent = []
    h.notify = lambda title, msg, prio="default": sent.append(title)
    h.cfg.chat_container = "sparkhub-chatui"
    hub.db.set_setting("chat_enabled", False)
    off = [{"name": "sparkhub-chatui", "service": "chatui", "model": False, "embed": False, "state": "exited",
            "health": None, "status": "Exited (0)"}]
    h.check_stack(off, 1000.0)
    h.check_stack(off, 1100.0)
    assert sent == []
    hub.db.set_setting("chat_enabled", True)
    h.check_stack(off, 1200.0)
    h.check_stack(off, 1300.0)
    assert sent == ["SparkHub: Chat is down"]
