"""MCP endpoint on the hub: /mcp, Streamable HTTP transport (JSON responses, no server-sent stream), API-key auth.

Tools: sparkhub_status, sparkhub_modes, sparkhub_switch_mode. A switch through MCP waits for the new model by
default: the agent's own model request is not in flight while a tool runs, so it resumes on the new mode.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .keys import Principal, verify_key
from .lifecycle import SwitchRefused
from .presets import MODES

router = APIRouter()
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "sparkhub", "title": "SparkHub", "version": "0.1.0"}
INSTRUCTIONS = (
    "SparkHub serves one local model at a time on a DGX Spark. Fast (qwen36, ~100 tok/s) is for interactive work; "
    "Quality (Flash, ~40 tok/s, clearly better on agentic coding and writing) is for hand-off work that can run "
    "unattended. Switching takes 4-5 minutes and interrupts other clients, so switch only when the task really "
    "needs the other mode, and never back and forth.")

TOOLS = [
    {"name": "sparkhub_status",
     "title": "SparkHub status",
     "description": "Which mode (fast, quality or custom) is loaded, whether a switch is in progress, and how many other "
                    "clients are using the model right now.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "sparkhub_modes",
     "title": "SparkHub modes",
     "description": "The modes, what each is good at, their speed and context window, and which is loaded.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "sparkhub_switch_mode",
     "title": "Switch SparkHub mode",
     "description": "Load another model (fast, quality, or custom when an admin has set one up). Takes 4-5 minutes; during that time no model answers. "
                    "Refused while other clients are active, unless drain is true (wait until they are quiet). "
                    "By default the call waits until the new model is ready, so the conversation continues on it.",
     "inputSchema": {"type": "object", "properties": {
         "mode": {"type": "string", "enum": list(MODES)},
         "drain": {"type": "boolean", "default": False,
                   "description": "Wait until other clients have been quiet before switching."},
         "wait": {"type": "boolean", "default": True,
                  "description": "Return only when the switch has finished (up to ~15 minutes)."}},
         "required": ["mode"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False}},
]


def _rpc_error(id_: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}},
                        status_code=status)


def _text(obj: Any, is_error: bool = False) -> dict[str, Any]:
    text = obj if isinstance(obj, str) else json.dumps(obj, indent=1)
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if not isinstance(obj, str):
        out["structuredContent"] = obj
    return out


def _status(hub, who: Principal | None = None) -> dict[str, Any]:
    st = hub.modes.status()
    active = hub.tracker.active(hub.db.get_setting("activity_window_s"),
                                exclude_key=who.id if who and who.kind == "key" else None)
    out = {"mode": st["mode"], "state": st["state"], "message": st["message"],
           "other_clients_active": len(active)}
    if st.get("switch"):
        out["switch"] = {k: st["switch"][k] for k in ("target", "previous", "elapsed_s", "expected_load_s")}
    if st.get("last_switch"):
        ls = st["last_switch"]
        out["last_switch"] = {"to": ls["to"], "result": ls["result"], "error": ls.get("error")}
    return out


def _modes(hub) -> dict[str, Any]:
    use = {"fast": "interactive work: quick edits, questions, shell help, short tasks",
           "quality": "hand-off work: long agentic coding tasks, careful writing, anything that runs unattended",
           "custom": "a model an admin chose from a recipe; ask them what it is for"}
    out = []
    for m in hub.presets.available:
        p = hub.presets[m]
        s = hub.presets.settings(m)
        out.append({"mode": m, "model": p.raw.get("model"), "use_for": use[m], "loaded": hub.modes.loaded == m,
                    "context_window": s["max_model_len"], "concurrent_requests": s["max_num_seqs"],
                    "speed_tok_s": {"single": p.expected.get("tok_s_single"),
                                    "per_agent_with_4": p.expected.get("tok_s_per_agent_4")},
                    "load_seconds": p.expected.get("load_seconds"), "thinking_default": s["default_thinking"]})
    return {"current": hub.modes.loaded, "modes": out, "note": INSTRUCTIONS}


async def _switch(hub, who: Principal, args: dict[str, Any]) -> dict[str, Any]:
    mode = args.get("mode")
    try:
        res = hub.modes.request_switch(str(mode), who, drain=bool(args.get("drain")), force=False)
    except SwitchRefused as e:
        return _text(e.detail, is_error=True)
    if res["result"] == "already" or not args.get("wait", True):
        return _text({**res, "status": _status(hub, who)})
    deadline = time.time() + 15 * 60 + hub.db.get_setting("drain_timeout_s") * bool(args.get("drain"))
    while hub.modes.busy and time.time() < deadline:
        await asyncio.sleep(2)
    st = _status(hub, who)
    ok = st["mode"] == mode and st["state"] == "ready"
    return _text({"result": "ready" if ok else "not ready", "status": st}, is_error=not ok)


@router.get("/mcp")
async def mcp_get():
    # no server-initiated stream
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete():
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_post(request: Request):
    from .gateway import bearer
    raw = bearer(request)
    who = verify_key(request.app.state.db, raw) if raw else None
    if who is None:
        return JSONResponse({"error": "invalid or missing API key"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        msg = json.loads(await request.body())
    except ValueError:
        return _rpc_error(None, -32700, "parse error", 400)
    if isinstance(msg, list):
        return _rpc_error(None, -32600, "batches are not supported", 400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request", 400)
    method, id_, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if id_ is None:          # notification (e.g. notifications/initialized)
        return Response(status_code=202)
    hub = request.app.state.hub

    def ok(result: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})

    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        hub.db.event("mcp_call", who.label, tool=name, args={k: v for k, v in args.items() if k != "wait"})
        if name == "sparkhub_status":
            return ok(_text(_status(hub, who)))
        if name == "sparkhub_modes":
            return ok(_text(_modes(hub)))
        if name == "sparkhub_switch_mode":
            if not who.has("switch"):
                return ok(_text("This API key may not switch modes (it needs the switch scope). "
                                "Ask the user to switch from the SparkHub dashboard.", is_error=True))
            return ok(await _switch(hub, who, args))
        return _rpc_error(id_, -32602, f"unknown tool {name}")
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return _rpc_error(id_, -32601, f"method not found: {method}")
