"""Gateway: auth, aliases, defaults, streaming, usage, disconnects, caps, measurements."""
from __future__ import annotations

import json
import threading
import time

import httpx


def chat(model="auto", stream=False, **kw):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    body.update(kw)
    return body


def last_activity(hub):
    return dict(hub.db.one("SELECT * FROM activity ORDER BY id DESC LIMIT 1"))


def test_bad_key_refused(hub):
    with hub.client("shk_wrong") as c:
        assert c.post("/v1/chat/completions", json=chat()).status_code == 401
    with hub.client() as c:
        assert c.post("/v1/chat/completions", json=chat()).status_code == 401
        assert c.get("/v1/models").status_code == 401


def test_revoked_key_refused(hub):
    with hub.client(hub.admin_key) as c:
        r = c.post("/api/v1/keys", json={"name": "temp", "scope": "infer"})
        assert r.status_code == 201
        kid, raw = r.json()["id"], r.json()["key"]
        with hub.client(raw) as k:
            assert k.post("/v1/chat/completions", json=chat()).status_code == 200
        assert c.delete(f"/api/v1/keys/{kid}").status_code == 200
        with hub.client(raw) as k:
            assert k.post("/v1/chat/completions", json=chat()).status_code == 401
        keys = c.get("/api/v1/keys").json()["keys"]
        assert all("hash" not in k for k in keys)


def test_non_stream_chat_and_record(hub):
    with hub.client(hub.infer_key) as c:
        r = c.post("/v1/chat/completions", json=chat("fast"))
    assert r.status_code == 200, r.text
    assert r.json()["usage"]["completion_tokens"] == 20
    a = last_activity(hub)
    assert a["mode"] == "fast" and a["outcome"] == "finished" and a["completion_tokens"] == 20
    assert a["prompt_tokens"] == 7 and a["cached_tokens"] == 3 and a["key_name"] == "agent"


def test_defaults_injected(hub):
    st = hub.runtime.states["fast"]
    with hub.client(hub.infer_key) as c:
        c.post("/v1/chat/completions", json=chat("auto"))
        sent = st.bodies[-1]
        assert sent["model"] == "qwen36"
        assert sent["chat_template_kwargs"] == {"enable_thinking": False}
        assert sent["max_tokens"] == 16000
        c.post("/v1/chat/completions", json=chat("fast-thinking", max_tokens=5))
        sent = st.bodies[-1]
        assert sent["chat_template_kwargs"]["enable_thinking"] is True and sent["max_tokens"] == 5
        # an explicit client choice wins over the default
        c.post("/v1/chat/completions", json=chat("qwen36", chat_template_kwargs={"enable_thinking": True}))
        assert st.bodies[-1]["chat_template_kwargs"]["enable_thinking"] is True
        # max_completion_tokens is left alone
        c.post("/v1/chat/completions", json=chat("fast", max_completion_tokens=7))
        assert "max_tokens" not in st.bodies[-1]


def test_wrong_mode_409_and_unknown_404(hub):
    with hub.client(hub.infer_key) as c:
        r = c.post("/v1/chat/completions", json=chat("quality"))
        assert r.status_code == 409
        assert "fast mode" in r.json()["error"]["message"]
        assert c.post("/v1/chat/completions", json=chat("gpt-4")).status_code == 404
        assert c.post("/v1/chat/completions", content=b"not json",
                      headers={"content-type": "application/json"}).status_code == 400


def test_models_lists_aliases(hub):
    with hub.client(hub.infer_key) as c:
        data = c.get("/v1/models").json()
    ids = [m["id"] for m in data["data"]]
    for want in ("auto", "fast", "quality", "auto-thinking", "fast-thinking", "quality-thinking"):
        assert want in ids
    fast = next(m for m in data["data"] if m["id"] == "fast")
    assert fast["sparkhub"]["loaded"] is True and fast["max_model_len"] == 131072
    assert next(m for m in data["data"] if m["id"] == "quality")["sparkhub"]["loaded"] is False


def stream_events(c, body):
    out = []
    with c.stream("POST", "/v1/chat/completions", json=body) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[6:]))
    return out


def test_stream_usage_stripped_unless_asked(hub):
    with hub.client(hub.infer_key) as c:
        ev = stream_events(c, chat("fast", stream=True))
        assert len(ev) == 20 and not any("usage" in e for e in ev)
        assert hub.runtime.states["fast"].bodies[-1]["stream_options"]["include_usage"] is True
        a = last_activity(hub)
        assert a["completion_tokens"] == 20 and a["outcome"] == "finished"
        assert a["ttft"] is not None and a["ttft"] > 0
        assert a["gen_tps"] is not None and a["gen_tps"] > 0
        ev = stream_events(c, chat("fast", stream=True, stream_options={"include_usage": True}))
        assert ev[-1]["usage"]["completion_tokens"] == 20


def test_length_outcome(hub):
    with hub.client(hub.infer_key) as c:
        stream_events(c, chat("fast", stream=True, max_tokens=5))
    a = last_activity(hub)
    assert a["outcome"] == "length" and a["completion_tokens"] == 5


def test_client_disconnect_aborts_upstream_stream(hub):
    st = hub.runtime.states["fast"]
    st.tokens = 400
    with hub.client(hub.infer_key) as c:
        with c.stream("POST", "/v1/chat/completions", json=chat("fast", stream=True, fake_delay=0.05)) as r:
            for i, _ in enumerate(r.iter_lines()):
                if i > 6:
                    break
    deadline = time.time() + 5
    while time.time() < deadline and not st.aborted:
        time.sleep(0.05)
    assert st.aborted == 1 and st.running == 0
    deadline = time.time() + 3
    while time.time() < deadline and hub.hub.tracker.inflight_total:
        time.sleep(0.05)
    assert hub.hub.tracker.inflight_total == 0
    assert last_activity(hub)["outcome"] == "disconnected"


def test_client_disconnect_aborts_upstream_non_stream(hub):
    st = hub.runtime.states["fast"]
    try:
        with hub.client(hub.infer_key) as c:
            c.post("/v1/chat/completions", json=chat("fast", fake_delay=0.5), timeout=1.0)
    except httpx.ReadTimeout:
        pass
    deadline = time.time() + 5
    while time.time() < deadline and not st.aborted:
        time.sleep(0.05)
    assert st.aborted == 1
    deadline = time.time() + 3
    while time.time() < deadline and hub.hub.tracker.inflight_total:
        time.sleep(0.05)
    assert last_activity(hub)["outcome"] == "disconnected"


def test_per_key_cap_queues(hub):
    with hub.client(hub.admin_key) as c:
        raw = c.post("/api/v1/keys", json={"name": "capped", "scope": "infer", "max_concurrency": 1}).json()["key"]
    results = []

    def one():
        with hub.client(raw) as k:
            results.append(k.post("/v1/chat/completions", json=chat("fast", fake_delay=0.05)).status_code)
    ts = [threading.Thread(target=one) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results == [200, 200]
    waits = [r["queue_wait"] for r in hub.db.all("SELECT queue_wait FROM activity WHERE key_name='capped'")]
    assert max(waits) > 0.5     # the second one waited for the first (20 tokens x 0.05 s)


def test_xff_trusted_from_localhost(hub):
    with hub.client(hub.infer_key) as c:
        c.post("/v1/chat/completions", json=chat(), headers={"X-Forwarded-For": "203.0.113.9"})
    assert last_activity(hub)["client_ip"] == "203.0.113.9"


def test_metrics_endpoint(hub):
    with hub.client(hub.infer_key) as c:
        stream_events(c, chat("fast", stream=True))
        m = c.get("/metrics").text
        assert 'sparkhub_requests_total{mode="fast",outcome="finished"}' in m
        assert "sparkhub_time_to_first_token_seconds_bucket" in m
        assert "sparkhub_generation_tokens_per_second_count" in m
        assert c.get("/metrics", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 404


def test_completions_endpoint(hub):
    with hub.client(hub.infer_key) as c:
        r = c.post("/v1/completions", json={"model": "fast", "prompt": "x", "max_tokens": 3})
    assert r.status_code == 200
    assert "chat_template_kwargs" not in hub.runtime.states["fast"].bodies[-1]


def test_reasoning_effort_means_thinking(hub):
    """OpenCode's "thinking" variant sends reasoning_effort; the hub turns it into enable_thinking."""
    fast, qual = hub.runtime.states["fast"], hub.runtime.states["quality"]
    with hub.client(hub.switch_key) as c:
        c.post("/v1/chat/completions", json=chat("auto", reasoning_effort="high"))
        sent = fast.bodies[-1]
        assert sent["chat_template_kwargs"]["enable_thinking"] is True
        assert "reasoning_effort" not in sent              # qwen36's template has no effort levels
        c.post("/v1/chat/completions", json=chat("auto", reasoning_effort="none"))
        assert fast.bodies[-1]["chat_template_kwargs"]["enable_thinking"] is False
        # an explicit enable_thinking wins over the effort
        c.post("/v1/chat/completions", json=chat("auto", reasoning_effort="high",
                                                 chat_template_kwargs={"enable_thinking": False}))
        assert fast.bodies[-1]["chat_template_kwargs"]["enable_thinking"] is False
        assert c.post("/api/v1/mode", json={"mode": "quality"}).status_code == 202
        hub.wait_state("ready", "quality")
        c.post("/v1/chat/completions", json=chat("auto", reasoning_effort="medium"))
        sent = qual.bodies[-1]
        assert sent["chat_template_kwargs"]["enable_thinking"] is True and sent["reasoning_effort"] == "medium"
        c.post("/v1/chat/completions", json=chat("auto", reasoning_effort="extreme"))   # unknown -> high
        assert qual.bodies[-1]["reasoning_effort"] == "high"
        c.post("/v1/chat/completions", json=chat("auto", reasoning_effort="minimal"))   # = off, never sent
        assert "reasoning_effort" not in qual.bodies[-1]
        assert qual.bodies[-1]["chat_template_kwargs"]["enable_thinking"] is False
        c.post("/v1/chat/completions", json=chat("auto"))                               # default: off
        assert qual.bodies[-1]["chat_template_kwargs"]["enable_thinking"] is False


def test_thinking_recorded_in_activity(hub):
    with hub.client(hub.infer_key) as c:
        c.post("/v1/chat/completions", json=chat("auto"))
        assert last_activity(hub)["thinking"] == 0
        c.post("/v1/chat/completions", json=chat("auto", reasoning_effort="high"))
        assert last_activity(hub)["thinking"] == 1
        c.post("/v1/chat/completions", json=chat("fast-thinking", stream=True))
        assert last_activity(hub)["thinking"] == 1
        c.post("/v1/completions", json={"model": "fast", "prompt": "x", "max_tokens": 3})
        assert last_activity(hub)["thinking"] is None      # plain completions have no chat template
