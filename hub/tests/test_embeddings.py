"""Embedding service: on-demand start, run modes, idle stop, chat first, model names, dimensions, rerank,
and that mode switches leave its containers alone."""
from __future__ import annotations

import base64
import math
import struct
import threading
import time

import pytest

from .test_auth import csrf_of, enrolled_client


def wait(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def embed_svc(hub, name="embed"):
    return hub.hub.embed.services[name]


def test_first_request_starts_it_on_demand(hub):
    assert embed_svc(hub).state == "stopped"
    with hub.client(hub.infer_key) as c:      # non-admin keys may use it
        r = c.post("/v1/embeddings", json={"input": ["hello world", "שלום עולם"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["model"] == "qwen3-embedding-0.6b" and len(d["data"]) == 2
    assert len(d["data"][0]["embedding"]) == 1024
    assert embed_svc(hub).state == "ready"
    spec = hub.runtime.started[-1]
    assert spec["labels"] == {"sparkhub.embed": "embed"}           # never the model label (strays)
    assert spec["container"].endswith("-embed") and spec["container_port"] == 8080
    row = dict(hub.db.one("SELECT * FROM activity ORDER BY id DESC LIMIT 1"))
    assert row["endpoint"] == "/v1/embeddings" and row["mode"] == "embed" and row["status"] == 200
    assert row["prompt_tokens"] and row["outcome"] == "finished"
    # the chat model kept serving, and the embedder isn't counted as a chat client
    assert hub.hub.modes.loaded == "fast" and hub.hub.tracker.inflight_total == 0


def test_dimensions_and_base64(hub):
    with hub.client(hub.infer_key) as c:
        r = c.post("/v1/embeddings", json={"input": "abc", "dimensions": 256})
        v = r.json()["data"][0]["embedding"]
        assert len(v) == 256 and abs(math.sqrt(sum(x * x for x in v)) - 1) < 1e-6
        r = c.post("/v1/embeddings", json={"input": "abc", "encoding_format": "base64"})
        raw = base64.b64decode(r.json()["data"][0]["embedding"])
        assert len(struct.unpack(f"<{len(raw) // 4}f", raw)) == 1024
        assert c.post("/v1/embeddings", json={"input": "abc", "dimensions": 2048}).status_code == 400
        assert c.post("/v1/embeddings", json={"input": "abc", "encoding_format": "hex"}).status_code == 400
        assert c.post("/v1/embeddings", json={}).status_code == 400
    # the upstream always gets floats and the served name, whatever the client asked
    body = hub.runtime.states["embed"].bodies[-1]
    assert body == {"input": "abc", "model": "qwen3-embedding-0.6b"}


def test_model_names(hub):
    with hub.client(hub.infer_key) as c:
        for m in ("qwen3-embedding-0.6b", "embed", "embedding", None):
            assert c.post("/v1/embeddings", json={"input": "x", "model": m}).status_code == 200, m
        assert c.post("/v1/embeddings", json={"input": "x", "model": "fast"}).status_code == 400
        assert c.post("/v1/embeddings", json={"input": "x", "model": "qwen3-reranker-0.6b"}).status_code == 400
        assert c.post("/v1/embeddings", json={"input": "x", "model": "nope"}).status_code == 404
        r = c.post("/v1/chat/completions", json={"model": "qwen3-embedding-0.6b",
                                                 "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 400 and "/v1/embeddings" in r.json()["error"]["message"]
        ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
        assert "qwen3-embedding-0.6b" in ids and "qwen3-reranker-0.6b" in ids and "fast" in ids


def test_rerank(hub):
    with hub.client(hub.infer_key) as c:
        r = c.post("/v1/rerank", json={"query": "nginx tls config", "top_n": 2,
                                       "documents": ["pasta recipe", "the nginx tls config lives here", "weather"]})
        assert r.status_code == 200, r.text
        res = r.json()["results"]
        assert len(res) == 2 and res[0]["index"] == 1
        assert c.post("/v1/rerank", json={"query": "q"}).status_code == 400
    assert embed_svc(hub, "rerank").state == "ready" and embed_svc(hub).state == "stopped"
    hub.db.set_setting("embed_rerank", False)
    with hub.client(hub.infer_key) as c:
        r = c.post("/v1/rerank", json={"query": "q", "documents": ["a"]})
        assert r.status_code == 503 and "reranker is off" in r.json()["error"]["message"]
    wait(lambda: embed_svc(hub, "rerank").state == "stopped")


def test_run_modes(hub):
    hub.db.set_setting("embed_mode", "always")
    wait(lambda: embed_svc(hub).state == "ready")           # started without a request
    assert embed_svc(hub, "rerank").state == "stopped"     # the reranker stays on-demand
    hub.db.set_setting("embed_mode", "off")
    wait(lambda: embed_svc(hub).state == "stopped")
    assert hub.runtime.stopped[-1].endswith("-embed")
    with hub.client(hub.infer_key) as c:
        r = c.post("/v1/embeddings", json={"input": "x"})
        assert r.status_code == 503 and "off" in r.json()["error"]["message"]
        ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
        assert "qwen3-embedding-0.6b" not in ids


def test_idle_stop(hub):
    hub.db.set_setting("embed_idle_minutes", 0.01)         # 0.6 s
    with hub.client(hub.infer_key) as c:
        assert c.post("/v1/embeddings", json={"input": "x"}).status_code == 200
    wait(lambda: embed_svc(hub).state == "stopped", timeout=5)
    with hub.client(hub.infer_key) as c:                  # and comes back on the next request
        assert c.post("/v1/embeddings", json={"input": "x"}).status_code == 200
    assert embed_svc(hub).state == "ready"


def test_mode_switch_leaves_it_running(hub):
    with hub.client(hub.infer_key) as c:
        assert c.post("/v1/embeddings", json={"input": "x"}).status_code == 200
    name = embed_svc(hub).container
    with hub.client(hub.admin_key) as c:
        assert c.post("/api/v1/mode", json={"mode": "quality", "force": True}).status_code == 202
    hub.wait_state("ready", "quality")
    assert name not in hub.runtime.stopped and name in hub.runtime.servers
    with hub.client(hub.infer_key) as c:
        assert c.post("/v1/embeddings", json={"input": "x"}).status_code == 200


def test_large_request_waits_for_chat(hub):
    hub.db.set_setting("embed_hold_s", 20)
    with hub.client(hub.infer_key) as c:
        assert c.post("/v1/embeddings", json={"input": "warm"}).status_code == 200
    done = {}

    def chat():
        with hub.client(hub.admin_key) as c:
            c.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                                                 "max_tokens": 20, "fake_delay": 0.1})
        done["chat"] = time.time()
    t = threading.Thread(target=chat)
    t.start()
    wait(lambda: hub.hub.tracker.inflight_total == 1)
    with hub.client(hub.infer_key) as c:
        t0 = time.time()
        assert c.post("/v1/embeddings", json={"input": "a short search query"}).status_code == 200
        assert time.time() - t0 < 1.0                       # small: straight through
        big = ["word " * 400] * 8                            # ~5k estimated tokens: bulk
        assert c.post("/v1/embeddings", json={"input": big}).status_code == 200
        finished = time.time()
    t.join()
    assert finished >= done["chat"] - 0.05                  # waited for the chat request to end
    row = dict(hub.db.one("SELECT * FROM activity WHERE endpoint='/v1/embeddings' ORDER BY id DESC LIMIT 1"))
    assert row["queue_wait"] > 0.5


def test_too_large_input_is_a_clear_400(hub):
    with hub.client(hub.infer_key) as c:
        r = c.post("/v1/embeddings", json={"input": "TOO_LARGE"})
    assert r.status_code == 400
    assert "4096 tokens" in r.json()["error"]["message"]


def test_settings_page(hub):
    c, _ = enrolled_client(hub)
    page = c.get("/settings?tab=embeddings").text
    assert 'action="/settings-embed"' in page and "qwen3-embedding-0.6b" in page
    r = c.post("/settings-embed", data={"csrf": csrf_of(c), "mode": "always", "idle_minutes": "15", "rerank": "on"},
               follow_redirects=False)
    assert r.status_code == 303 and "saved=embeddings" in r.headers["location"]
    assert hub.db.get_setting("embed_mode") == "always" and hub.db.get_setting("embed_idle_minutes") == 15
    assert embed_svc(hub).state == "ready"                  # applied at once
    r = c.post("/settings-embed", data={"csrf": csrf_of(c), "mode": "sometimes", "idle_minutes": "15"},
               follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = c.post("/settings-embed", data={"csrf": csrf_of(c), "mode": "off", "idle_minutes": "15"},
               follow_redirects=False)
    assert embed_svc(hub).state == "stopped" and hub.db.get_setting("embed_rerank") is False
    st = c.get("/api/v1/status", headers={"Authorization": f"Bearer {hub.admin_key}"}).json()
    assert st["embeddings"]["run_mode"] == "off"


@pytest.fixture
def bare_hub(make_hub):
    h = make_hub(embeddings=False)
    h.wait_state("ready", "fast")
    return h


def test_not_installed(bare_hub):
    with bare_hub.client(bare_hub.infer_key) as c:
        r = c.post("/v1/embeddings", json={"input": "x"})
        assert r.status_code == 503 and "not installed" in r.json()["error"]["message"]
    c, _ = enrolled_client(bare_hub)
    assert "Not installed on this hub" in c.get("/settings?tab=embeddings").text


def test_dashboard_pages(hub):
    c, _ = enrolled_client(hub)
    # settings: generative models and embeddings on separate tabs
    page = c.get("/settings?tab=embeddings").text
    assert ">Generative Models</a>" in page and ">Embeddings</a>" in page
    assert 'aria-labelledby="tab-embeddings">' in page and 'action="/settings-embed"' in page
    # overview: the embedding services always listed, coloured by run mode
    hub.hub._components = (0.0, None)
    html = c.get("/ui/live").text
    assert 'class="comp lv-demand"' in html and "On-demand · stopped" in html
    hub.db.set_setting("embed_mode", "off")
    hub.hub._components = (0.0, None)
    html = c.get("/ui/live").text
    assert html.count('class="comp lv-disabled"') == 2 and ">Embeddings<" in html and ">Reranker<" in html
    hub.db.set_setting("embed_mode", "always")
    wait(lambda: embed_svc(hub).state == "ready")
    hub.hub._components = (0.0, None)
    html = c.get("/ui/live").text
    assert 'class="comp lv-ok"' in html and "Always on · " in html
    # an embedding request shows in the recent requests with its mode
    hub.db.set_setting("embed_mode", "on-demand")
    with hub.client(hub.infer_key) as k:
        assert k.post("/v1/embeddings", json={"input": "x"}).status_code == 200
    hub.hub._components = (0.0, None)
    html = c.get("/ui/live").text
    assert "<td>embed</td>" in html
    # on-demand but running: green; the stopped reranker stays blue
    assert html.count('class="comp lv-ok"') >= 1 and "On-demand · running" in html
    assert html.count('class="comp lv-demand"') == 1
    # connect: how to use it
    page = c.get("/connect").text
    assert "/v1/embeddings" in page and "Instruct: Given a web search query" in page and "qwen3-reranker-0.6b" in page
