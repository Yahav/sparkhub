"""MCP endpoint and client downloads."""
from __future__ import annotations

import time

from .test_gateway import chat


def rpc(c, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    r = c.post("/mcp", json=body, headers={"Accept": "application/json, text/event-stream"})
    return r


def test_mcp_needs_key(hub):
    with hub.client() as c:
        assert rpc(c, "initialize").status_code == 401
    with hub.client("shk_bad") as c:
        assert rpc(c, "tools/list").status_code == 401


def test_mcp_handshake_and_tools(hub):
    with hub.client(hub.infer_key) as c:
        r = rpc(c, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "1"}})
        res = r.json()["result"]
        assert res["protocolVersion"] == "2025-03-26" and "tools" in res["capabilities"]
        assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
        tools = [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]]
        assert tools == ["sparkhub_status", "sparkhub_modes", "sparkhub_switch_mode"]
        st = rpc(c, "tools/call", {"name": "sparkhub_status", "arguments": {}}).json()["result"]
        assert st["isError"] is False and st["structuredContent"]["mode"] == "fast"
        md = rpc(c, "tools/call", {"name": "sparkhub_modes", "arguments": {}}).json()["result"]["structuredContent"]
        assert [m["mode"] for m in md["modes"]] == ["fast", "quality"] and md["current"] == "fast"
        assert rpc(c, "nope").json()["error"]["code"] == -32601
        assert c.get("/mcp").status_code == 405


def test_mcp_switch_scope(hub):
    with hub.client(hub.infer_key) as c:
        res = rpc(c, "tools/call", {"name": "sparkhub_switch_mode", "arguments": {"mode": "quality"}}).json()["result"]
        assert res["isError"] is True and "switch scope" in res["content"][0]["text"]
    assert hub.hub.modes.loaded == "fast" and not hub.hub.modes.busy


def test_mcp_switch_waits_for_ready(hub):
    with hub.client(hub.switch_key) as c:
        c.post("/v1/chat/completions", json=chat())          # its own recent activity does not block it
        t0 = time.time()
        res = rpc(c, "tools/call", {"name": "sparkhub_switch_mode", "arguments": {"mode": "quality"}}).json()["result"]
        assert res["isError"] is False, res
        assert res["structuredContent"]["result"] == "ready"
        assert res["structuredContent"]["status"]["mode"] == "quality"
        assert hub.hub.modes.loaded == "quality"
        # and the conversation continues on the new mode through 'auto'
        assert c.post("/v1/chat/completions", json=chat("auto")).status_code == 200
        assert hub.runtime.states["quality"].bodies[-1]["model"] == "qwen3.8-flash-next"


def test_mcp_switch_refused_when_others_active(hub):
    with hub.client(hub.infer_key) as other:
        other.post("/v1/chat/completions", json=chat())
    with hub.client(hub.switch_key) as c:
        res = rpc(c, "tools/call", {"name": "sparkhub_switch_mode", "arguments": {"mode": "quality"}}).json()["result"]
        assert res["isError"] is True and res["structuredContent"]["active"][0]["name"] == "agent"


def test_opencode_config_and_downloads(hub):
    with hub.client() as c:   # no key needed: nothing secret in them
        cfg = c.get("/clients/opencode.json", headers={"Host": "spark.example"}).json()
        prov = cfg["provider"]["sparkhub"]
        assert prov["options"]["baseURL"] == "https://spark.example/v1"
        assert prov["options"]["apiKey"] == "{file:~/.config/sparkhub/key}"
        assert cfg["mcp"]["sparkhub"]["headers"]["Authorization"] == "Bearer {file:~/.config/sparkhub/key}"
        assert sorted(prov["models"]) == ["auto", "fast", "quality"]
        for name in ("auto", "fast", "quality"):
            m = prov["models"][name]
            # Quality (b12x recipe) defaults to 256k; auto gets the smaller window of the two
            ctx = 262144 if name == "quality" else 131072
            assert m["limit"] == {"context": ctx, "output": 32000}, name
            # OpenCode's menu: no low/medium/high, one "thinking" variant (sent as reasoning_effort)
            assert m["variants"] == {"thinking": {"reasoningEffort": "high"}}
        assert cfg["mcp"]["sparkhub"]["url"] == "https://spark.example/mcp"
        assert cfg["mcp"]["sparkhub"]["timeout"] >= 15 * 60 * 1000
        assert "shk_" not in str(cfg)
        assert c.get("/clients/opencode-skill.md").text.startswith("---\nname: sparkhub")
        cli = c.get("/clients/sparkhub", headers={"Host": "spark.example"}).text
        assert cli.startswith("#!/usr/bin/env python3")
        assert '"https://spark.example"' in cli and "sparkhub.example" not in cli   # default URL = where it came from
    # a smaller window in the settings shows up in the generated limits
    with hub.client(hub.admin_key) as c:
        r = c.put("/api/v1/modes/quality/settings", json={"max_model_len": 65536})
        assert r.status_code == 200, r.text
        cfg = c.get("/clients/opencode.json").json()
    m = cfg["provider"]["sparkhub"]["models"]
    assert m["quality"]["limit"]["context"] == 65536 and m["auto"]["limit"]["context"] == 65536
    assert m["fast"]["limit"]["context"] == 131072
