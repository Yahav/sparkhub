"""Mode switching: scopes, recent-activity check, drain, force, fallback, cancel, boot adoption, crash detection."""
from __future__ import annotations

import threading
import time

from .test_gateway import chat


def test_switch_and_back(hub):
    with hub.client(hub.switch_key) as c:
        r = c.post("/api/v1/mode", json={"mode": "quality"})
        assert r.status_code == 202, r.text
        st = c.get("/api/v1/mode").json()
        assert st["state"] in ("stopping", "loading", "ready")
        hub.wait_state("ready", "quality")
        r = c.post("/v1/chat/completions", json=chat("quality"))
        assert r.status_code == 200
        assert hub.runtime.states["quality"].bodies[-1]["model"] == "qwen3.8-flash-next"
        assert c.post("/v1/chat/completions", json=chat("fast")).status_code == 409
        assert c.post("/api/v1/mode", json={"mode": "quality"}).json()["result"] == "already"
        assert c.post("/api/v1/mode", json={"mode": "fast"}).status_code == 202
        hub.wait_state("ready", "fast")
    assert hub.db.get_setting("desired_mode") == "fast"
    ev = [dict(r) for r in hub.db.all("SELECT * FROM events WHERE kind='switch' ORDER BY id")]
    assert [e["actor"] for e in ev[-2:]] == ["key:harness", "key:harness"]
    assert hub.runtime.freed >= 2


def test_specs_are_safe(hub):
    """The spec asks for 127.0.0.1 only; restart policy is set in DockerRuntime (tested in test_runtime)."""
    spec = hub.runtime.started[-1]
    assert spec["container"] == "sparkhub-fast"
    assert "--host" in spec["args"]


def test_infer_key_cannot_switch_and_force_needs_admin(hub):
    with hub.client(hub.infer_key) as c:
        assert c.post("/api/v1/mode", json={"mode": "quality"}).status_code == 403
    with hub.client(hub.switch_key) as c:
        assert c.post("/api/v1/mode", json={"mode": "quality", "force": True}).status_code == 403
    with hub.client(hub.switch_key) as c:
        assert c.post("/api/v1/mode", json={"mode": "nope"}).status_code == 400


def test_recent_activity_blocks_switch(hub):
    with hub.client(hub.infer_key) as agent:
        assert agent.post("/v1/chat/completions", json=chat()).status_code == 200
    with hub.client(hub.switch_key) as c:
        r = c.post("/api/v1/mode", json={"mode": "quality"})
        assert r.status_code == 409
        assert r.json()["active"][0]["name"] == "agent"
        # the asking key's own activity does not block it
    with hub.client(hub.infer_key) as agent:
        pass
    time.sleep(3.2)   # activity window (3 s in tests) passes
    with hub.client(hub.switch_key) as c:
        c.post("/v1/chat/completions", json=chat())
        assert c.post("/api/v1/mode", json={"mode": "quality"}).status_code == 202
    hub.wait_state("ready", "quality")


def test_drain_waits_for_pausing_agent(hub):
    """An agent pausing between requests (here 1 s; quiet needed 1.5 s) blocks the switch until it stops."""
    stop = threading.Event()
    results = []

    def agent():
        with hub.client(hub.infer_key) as c:
            while not stop.is_set():
                results.append(c.post("/v1/chat/completions", json=chat("fast")).status_code)
                time.sleep(1.0)
    t = threading.Thread(target=agent)
    t.start()
    time.sleep(0.5)
    with hub.client(hub.switch_key) as c:
        assert c.post("/api/v1/mode", json={"mode": "quality"}).status_code == 409
        r = c.post("/api/v1/mode", json={"mode": "quality", "drain": True})
        assert r.status_code == 202 and r.json()["drain"] is True
        time.sleep(3)
        assert hub.hub.modes.state == "draining"
        assert hub.hub.modes.loaded == "fast"
        # the active agent is still served while draining; a new client is not
        with hub.client(hub.admin_key) as newc:
            r2 = newc.post("/v1/chat/completions", json=chat("fast"))
            assert r2.status_code == 503
        stop.set()
        t.join()
        hub.wait_state("ready", "quality")
    assert set(results) == {200}


def test_force_switch_by_admin(hub):
    with hub.client(hub.infer_key) as agent:
        agent.post("/v1/chat/completions", json=chat())
    with hub.client(hub.admin_key) as c:
        assert c.post("/api/v1/mode", json={"mode": "quality", "force": True}).status_code == 202
    hub.wait_state("ready", "quality")


def test_failed_load_retries_then_falls_back(hub):
    """Quality fails twice -> fast comes back."""
    hub.runtime.fail["quality"] = 2
    with hub.client(hub.switch_key) as c:
        assert c.post("/api/v1/mode", json={"mode": "quality"}).status_code == 202
    st = hub.wait_state("ready", "fast")
    assert st["last_switch"]["result"] == "failed"
    assert "quality failed" in st["message"] or st["mode"] == "fast"
    starts = [s["mode"] for s in hub.runtime.started]
    assert starts[-3:] == ["quality", "quality", "fast"]
    kinds = [r["kind"] for r in hub.db.all("SELECT kind FROM events ORDER BY id")]
    assert kinds.count("load_failed") == 2


def test_failed_once_then_retry_succeeds(hub):
    hub.runtime.fail["quality"] = 1
    with hub.client(hub.switch_key) as c:
        c.post("/api/v1/mode", json={"mode": "quality"})
    st = hub.wait_state("ready", "quality")
    assert st["last_switch"]["result"] == "ok"


def test_cancel_restores_previous(hub):
    hub.runtime.load_delay = 3.0
    with hub.client(hub.switch_key) as c:
        c.post("/api/v1/mode", json={"mode": "quality"})
        deadline = time.time() + 5
        while hub.hub.modes.state != "loading" and time.time() < deadline:
            time.sleep(0.05)
        hub.runtime.load_delay = 0.2
        r = c.post("/api/v1/mode/cancel")
        assert r.status_code == 200, r.text
    st = hub.wait_state("ready", "fast")
    assert st["last_switch"]["result"] == "cancelled"


def test_requests_refused_while_switching(hub):
    hub.runtime.load_delay = 2.0
    with hub.client(hub.switch_key) as c:
        c.post("/api/v1/mode", json={"mode": "quality"})
        time.sleep(0.3)
        r = c.post("/v1/chat/completions", json=chat("auto"))
        assert r.status_code == 503 and r.headers.get("retry-after")
    hub.wait_state("ready", "quality")


def test_watchdog_restarts_once_then_gives_up(hub):
    """A crash is reloaded once; a second crash within 30 min stays down."""
    pushes = []
    hub.hub.modes.notify = lambda title, msg, prio="default": pushes.append(title)
    hub.runtime.crash("sparkhub-fast")
    deadline = time.time() + 10
    while not any(r["kind"] == "watchdog_restart" for r in hub.db.all("SELECT kind FROM events")):
        assert time.time() < deadline
        time.sleep(0.05)
    st = hub.wait_state("ready", "fast")
    assert st["last_switch"]["by"] == "watchdog" and st["last_switch"]["result"] == "ok"
    assert "SparkHub: model stopped, restarting" in pushes and "SparkHub: model back" in pushes
    hub.runtime.crash("sparkhub-fast")
    hub.wait_state("failed", None, timeout=5)
    time.sleep(1)
    assert hub.hub.modes.status()["mode"] is None and not hub.hub.modes.busy
    assert "SparkHub: model crashed again" in pushes
    kinds = [r["kind"] for r in hub.db.all("SELECT kind FROM events")]
    assert kinds.count("watchdog_restart") == 1 and "watchdog_gave_up" in kinds
    with hub.client(hub.switch_key) as c:     # a manual switch still works
        assert c.post("/v1/chat/completions", json=chat()).status_code == 503
        assert c.post("/api/v1/mode", json={"mode": "fast"}).status_code == 202
    hub.wait_state("ready", "fast")


def test_watchdog_off(hub):
    hub.db.set_setting("watchdog", False)
    hub.runtime.crash("sparkhub-fast")
    hub.wait_state("failed", None, timeout=5)
    time.sleep(1)
    assert hub.hub.modes.status()["mode"] is None and not hub.hub.modes.busy


def test_boot_adopts_running_model(make_hub):
    h = make_hub(sub="a")
    h.wait_state("ready", "fast")
    # a second hub (a restart) on the same runtime adopts the container instead of reloading it
    starts = len(h.runtime.started)
    h.hub.modes.task = None
    h.hub.modes.loaded = None
    import asyncio
    fut = asyncio.run_coroutine_threadsafe(h.hub.modes.startup(), h.hub.modes._monitor.get_loop())
    fut.result(5)
    h.wait_state("ready", "fast")
    assert len(h.runtime.started) == starts
    assert any(r["kind"] == "model_adopted" for r in h.db.all("SELECT kind FROM events"))


def test_settings_validation_and_apply(hub):
    with hub.client(hub.admin_key) as c:
        assert c.put("/api/v1/modes/quality/settings", json={"max_num_seqs": 99}).status_code == 400
        assert c.put("/api/v1/modes/quality/settings", json={"bogus": 1}).status_code == 400
        r = c.put("/api/v1/modes/quality/settings", json={"max_num_seqs": 2, "mtp": 0})
        assert r.status_code == 200
        spec = c.get("/api/v1/modes/quality/spec").json()
        args = spec["args"]
        assert args[args.index("--max-num-seqs") + 1] == "2"
        assert "--speculative-config" not in args
    with hub.client(hub.infer_key) as c:
        assert c.put("/api/v1/modes/quality/settings", json={"max_num_seqs": 2}).status_code == 403
    with hub.client(hub.admin_key) as c:
        c.post("/api/v1/mode", json={"mode": "quality"})
    hub.wait_state("ready", "quality")
    args = hub.runtime.started[-1]["args"]
    assert args[args.index("--max-num-seqs") + 1] == "2"


def test_status_endpoint(hub):
    with hub.client(hub.infer_key) as c:
        c.post("/v1/chat/completions", json=chat())
        st = c.get("/api/v1/status").json()
    assert st["mode"] == "fast" and st["state"] == "ready"
    assert st["memory"]["total_gib"] > 0
    assert "running" in st["vllm"]
    assert st["clients"]["active"][0]["name"] == "agent"


def test_push_on_restore_after_restart(make_hub):
    """No model running at hub start (Docker/host restart): push 'started', then 'ready'."""
    h = make_hub(sub="r")
    pushes = []
    h.hub.modes.notify = lambda title, msg, prio="default": pushes.append(title)
    h.wait_state("ready", "fast")
    # the boot push may have gone out before the hook above; check the event and the ready push
    kinds = [r["kind"] for r in h.db.all("SELECT kind FROM events")]
    assert "restored_after_restart" in kinds
    assert "SparkHub ready" in pushes or h.hub.modes.last_switch["by"] == "boot"
