"""Benchmarks (bench.py): scheduling without overlap, loading the run's model, blocking other clients and switches
during a run, the speed suite, the runner's results, the run's key, cancel."""
from __future__ import annotations

import json
import time

import pytest

from sparkhub import bench as benchmod

from .test_auth import csrf_of, enrolled_client
from .test_gateway import chat


@pytest.fixture
def bhub(make_hub):
    hub = make_hub(bench=True)
    hub.wait_state("ready", "fast")
    return hub


def wait_run(hub, rid, statuses, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = hub.hub.bench.row(rid)
        if r["status"] in statuses:
            return r
        time.sleep(0.1)
    raise AssertionError(f"run {rid} stayed {hub.hub.bench.row(rid)['status']}")


def start(c, **form) -> str:
    data = {"csrf": csrf_of(c), "mode": "fast", "when": "now", **form}
    r = c.post("/bench/start", data=data, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def test_run_blocks_others_measures_and_records(bhub):
    c, _ = enrolled_client(bhub)
    assert '"/bench"' in c.get("/").text                                    # in the menu when installed
    loc = start(c, suite=["s2"])
    assert "msg=Benchmark%20%231" in loc, loc
    r = wait_run(bhub, 1, ("running",))
    assert r["suites"] == ["speed", "tb2-quick", "s2"] and r["key_id"]
    # the runner was started (after the speed suite) with the run's key; until it ends nobody else gets in
    deadline = time.time() + 30
    while "sparkhub-bench" not in bhub.runtime.bodies and time.time() < deadline:
        time.sleep(0.1)
    body = bhub.runtime.bodies["sparkhub-bench"]
    env = dict(e.split("=", 1) for e in body["Env"])
    conf = json.loads(env["BENCH_CONFIG"])
    assert conf["suites"] == ["tb2-quick", "s2"] and conf["model"] == "fast" and not conf["internet"]
    assert conf["allowed_hosts"][0] == bhub.cfg.gateway_addr and conf["base_url"].startswith("https://")
    assert "deb.debian.org" in conf["allowed_hosts"] and "github.com" not in conf["allowed_hosts"]
    assert any(b.endswith(":/var/run/docker.sock") for b in body["HostConfig"]["Binds"])
    assert "GroupAdd" not in body and body["HostConfig"]["GroupAdd"]          # Docker ignores a top-level GroupAdd
    run_key = env["OPENAI_API_KEY"]
    with bhub.client(bhub.admin_key) as k:
        rr = k.post("/v1/chat/completions", json=chat("fast"))
        assert rr.status_code == 503 and "benchmark" in rr.json()["error"]["message"]
        sw = k.post("/api/v1/mode", json={"mode": "quality", "force": True})
        assert sw.status_code == 409 and "benchmark #1 is running" in sw.json()["error"]
    with bhub.client(run_key) as k:
        assert k.post("/v1/chat/completions", json=chat("fast")).status_code == 200
    assert "Running: #1" in c.get("/ui/bench").text
    bhub.runtime.bench_release.set()
    r = wait_run(bhub, 1, ("done", "failed"))
    assert r["status"] == "done", r["error"]
    sp = r["results"]["suites"]["speed"]
    assert [x["label"] for x in sp["cases"]] == [x[0] for x in benchmod.SPEED_CASES]
    assert sp["headline"]["single_tps"] > 0 and sp["cases"][0]["ttft_p50_s"] is not None
    assert r["results"]["suites"]["tb2-quick"]["solved"] == 9
    # the fake model saw different prompts (no prefix cache hits) with ignore_eos
    bodies = [b for b in bhub.runtime.states["fast"].bodies if b.get("ignore_eos")]
    assert len({b["messages"][0]["content"][:200] for b in bodies}) == len(bodies) > 10
    # the key is revoked, the hub takes requests again, the page compares
    with bhub.client(run_key) as k:
        assert k.post("/v1/chat/completions", json=chat("fast")).status_code == 401
    with bhub.client(bhub.admin_key) as k:
        assert k.post("/v1/chat/completions", json=chat("fast")).status_code == 200
    page = c.get("/bench").text
    assert "9/15" in page and "80.0%" in page
    assert "Speed" in c.get("/bench/1").text and "nginx-request-logging" in c.get("/bench/1").text
    assert "sparkhub-bench" not in bhub.runtime.apps                      # the runner was removed


def test_schedule_overlap_autoload_and_cancel(bhub):
    c, _ = enrolled_client(bhub)
    b = bhub.hub.bench
    soon = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 3600))
    loc = start(c, mode="quality", when="at", at=soon)
    assert "msg=Benchmark%20%231%20starts" in loc
    loc = start(c, when="at", at=soon)
    assert "would%20overlap%20benchmark%20%231" in loc, loc
    assert "error=that%20start%20time%20has%20passed" in start(c, when="at", at="2020-01-01T10:00")
    assert "Scheduled" in c.get("/bench").text
    # nothing is blocked before the start time
    with bhub.client(bhub.admin_key) as k:
        assert k.post("/v1/chat/completions", json=chat("fast")).status_code == 200
    # cancel the scheduled one
    r = c.post("/bench/1/cancel", data={"csrf": csrf_of(c)}, follow_redirects=False)
    assert "cancelled" in r.headers["location"] and b.row(1)["status"] == "cancelled"
    # due now for quality while fast is loaded: the hub loads quality first
    bhub.runtime.bench_release.set()
    rid = b.schedule("quality", [], False, None, "user:admin")
    r = wait_run(bhub, rid, ("done", "failed"), timeout=60)
    assert r["status"] == "done", r["error"]
    assert bhub.hub.modes.loaded == "quality"
    ev = [e["kind"] for e in bhub.db.all("SELECT kind FROM events WHERE kind LIKE 'bench_%' ORDER BY id")]
    assert ev[-3:] == ["bench_scheduled", "bench_started", "bench_done"]


def test_cancel_running_and_restart_marks_failed(bhub):
    c, _ = enrolled_client(bhub)
    start(c)
    wait_run(bhub, 1, ("running",))
    deadline = time.time() + 30
    while "sparkhub-bench" not in bhub.runtime.apps and time.time() < deadline:
        time.sleep(0.1)
    r = c.post("/bench/1/cancel", data={"csrf": csrf_of(c)}, follow_redirects=False)
    assert "cancelled" in r.headers["location"]
    r = wait_run(bhub, 1, ("cancelled", "done", "failed"))
    assert r["status"] == "cancelled"
    with bhub.client(bhub.admin_key) as k:
        assert k.post("/v1/chat/completions", json=chat("fast")).status_code == 200
    # a run left "running" by a crash is failed at the next start
    bhub.db.execute("UPDATE bench_runs SET status='running' WHERE id=1")
    bhub.loop.call_soon_threadsafe(lambda: None)
    import asyncio
    asyncio.run_coroutine_threadsafe(bhub.hub.bench.start(), bhub.app.state.hub.modes.task.get_loop()).result(5)
    assert bhub.hub.bench.row(1)["status"] == "failed" and "restarted" in bhub.hub.bench.row(1)["error"]


def test_not_installed_hidden(hub):
    c, _ = enrolled_client(hub)
    assert '"/bench"' not in c.get("/").text
    assert "error=benchmarks%20are%20not%20installed" in start(c)


def test_estimate_and_normalize(bhub):
    b = bhub.hub.bench
    assert b.normalize(["tb2-full", "s6", "bogus"]) == ["speed", "tb2-full", "s6"]
    assert b.normalize([]) == ["speed", "tb2-quick"]
    assert b.estimate("quality", ["speed"]) > b.estimate("fast", ["speed"])
    h = b.headline({"suites": {"tb2-full": {"solved": 30, "n_tasks": 53, "tasks": {
        t: {"reward": 1.0} for t in benchmod.TB2_QUICK[:10]}}}})
    assert h["tb2_full"] == (30, 53) and h["tb2_quick"] == (10, 15)
