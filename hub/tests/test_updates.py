"""Versions and upgrades: the daily release check (banner, one notification per version, the switch to turn it off)
and the reload of a model whose pinned preset changed in a release (updates.py, lifecycle._pin_changed)."""
from __future__ import annotations

import asyncio

from sparkhub.updates import parse_version

from .test_auth import csrf_of, enrolled_client


def run(h, coro, timeout=10):
    return asyncio.run_coroutine_threadsafe(coro, h.hub.modes._monitor.get_loop()).result(timeout)


def test_parse_version():
    assert parse_version("v1.2.3") == (1, 2, 3) and parse_version("1.10.0") > parse_version("1.9.9")
    assert parse_version("dev") is None and parse_version(None) is None


def test_release_check_banner_and_notification(make_hub):
    h = make_hub(version="1.0.0")
    h.wait_state("ready", "fast")
    sent = []
    h.hub.updates.notify = lambda title, msg, *a: sent.append(title)
    release = {"tag_name": "v1.1.0", "name": "1.1.0", "html_url": "https://github.com/x/y/releases/tag/v1.1.0",
               "body": "notes", "published_at": "2026-11-05T10:00:00Z"}

    async def fetch():
        return release
    h.hub.updates.fetch = fetch
    c, _ = enrolled_client(h)
    assert "is available" not in c.get("/").text
    run(h, h.hub.updates.check())
    run(h, h.hub.updates.check())
    assert sent == ["SparkHub v1.1.0 is available"]                # once per version
    page = c.get("/").text
    assert "v1.1.0</a> is available" in page and "./install.sh upgrade" in page
    assert "newer than this install" in c.get("/settings?tab=updates").text
    # the same version as installed: no banner; a failed check keeps the last answer
    release["tag_name"] = "v1.0.0"
    run(h, h.hub.updates.check())
    assert "is available" not in c.get("/").text

    async def offline():
        raise OSError("no network")
    h.hub.updates.fetch = offline
    latest = run(h, h.hub.updates.check())
    assert latest["tag"] == "v1.0.0" and "no network" in latest["error"]
    # turned off: no banner, no request
    release["tag_name"] = "v2.0.0"
    h.hub.updates.fetch = fetch
    r = c.post("/settings-updates", data={"csrf": csrf_of(c), "action": "save"}, follow_redirects=False)
    assert "update%20check%20off" in r.headers["location"] or "update check off" in r.headers["location"]
    assert run(h, h.hub.updates.check()) is None and h.hub.updates.available() is None


def test_dev_build_never_offers(hub):
    hub.db.set_setting("update_latest", {"tag": "v9.0.0"})
    assert hub.cfg.version == "dev" and hub.hub.updates.available() is None


def restart_hub_on_same_runtime(h):
    """A hub restart (as after ./install.sh upgrade): the model container keeps running."""
    h.hub.modes.task = None
    h.hub.modes.loaded = None
    run(h, h.hub.modes.startup())


def test_changed_pin_reloads_unchanged_adopts(make_hub):
    h = make_hub(sub="a")
    h.wait_state("ready", "fast")
    starts = len(h.runtime.started)
    assert h.runtime.started[-1]["pin"] == h.hub.presets["fast"].pin
    restart_hub_on_same_runtime(h)                                  # same release: adopted
    h.wait_state("ready", "fast")
    assert len(h.runtime.started) == starts
    # the release changed Fast's image: the restarted hub reloads it, and the new container carries the new pin
    h.hub.presets["fast"].raw["image"] = "vllm/vllm-openai@sha256:" + "1" * 64
    restart_hub_on_same_runtime(h)
    h.wait_state("ready", "fast")
    assert len(h.runtime.started) == starts + 1
    assert h.runtime.started[-1]["pin"] == h.hub.presets["fast"].pin
    kinds = [r["kind"] for r in h.db.all("SELECT kind FROM events")]
    assert "model_pin_changed" in kinds
    # a container from before pins (no label) is adopted as it is
    h.runtime.labels[h.hub.presets["fast"].container] = {}
    restart_hub_on_same_runtime(h)
    h.wait_state("ready", "fast")
    assert len(h.runtime.started) == starts + 1


def test_changed_pin_without_weights_keeps_old_model(make_hub):
    h = make_hub(sub="b")
    h.wait_state("ready", "fast")
    starts = len(h.runtime.started)
    h.hub.presets["fast"].raw["image"] = "vllm/vllm-openai@sha256:" + "2" * 64

    async def missing(mode=None):
        h.hub.modes.install = {"fast": ["weights incomplete: 3 of 9 files missing"], "quality": []}
        return h.hub.modes.install
    h.hub.modes.check_install = missing
    restart_hub_on_same_runtime(h)
    h.wait_state("ready", "fast")
    assert len(h.runtime.started) == starts                       # the old container kept serving
    assert "cannot be loaded yet" in h.hub.modes.error
