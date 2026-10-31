"""Control API /api/v1. Works with an API key (Bearer) or a dashboard session (cookie + CSRF header)."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import keys as keymod
from .keys import Principal
from .lifecycle import SwitchRefused
from .presets import SETTING_HELP

router = APIRouter(prefix="/api/v1")
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


def principal(request: Request) -> Principal:
    from .gateway import bearer
    raw = bearer(request)
    if raw:
        p = keymod.verify_key(request.app.state.db, raw)
        if p is None:
            raise HTTPException(401, "invalid API key")
        return p
    auth = getattr(request.app.state, "auth", None)
    if auth is not None:
        p = auth.session_principal(request)
        if p is not None:
            if request.method in UNSAFE and request.headers.get("x-csrf-token") != p.csrf:
                raise HTTPException(403, "missing or wrong CSRF token")
            return p
    raise HTTPException(401, "authentication required")


def need(scope: str):
    def dep(request: Request) -> Principal:
        p = principal(request)
        if not p.has(scope):
            raise HTTPException(403, f"needs the {scope} scope")
        return p
    return dep


def _refused(e: SwitchRefused) -> JSONResponse:
    return JSONResponse(e.detail, status_code=e.status)


@router.get("/health")
async def health(request: Request):
    m = request.app.state.hub.modes
    return {"ok": True, "mode": m.loaded, "state": m.state}


@router.get("/status")
async def status(request: Request, who: Principal = Depends(need("infer"))):
    return await request.app.state.hub.full_status()


@router.get("/mode")
async def get_mode(request: Request, who: Principal = Depends(need("infer"))):
    return request.app.state.hub.modes.status()


class SwitchBody(BaseModel):
    mode: str
    drain: bool = False
    force: bool = False


@router.post("/mode")
async def switch_mode(body: SwitchBody, request: Request, who: Principal = Depends(need("switch"))):
    try:
        res = request.app.state.hub.modes.request_switch(body.mode, who, drain=body.drain, force=body.force)
    except SwitchRefused as e:
        return _refused(e)
    return JSONResponse(res, status_code=202 if res["result"] == "started" else 200)


@router.post("/mode/cancel")
async def cancel_switch(request: Request, who: Principal = Depends(need("switch"))):
    try:
        return request.app.state.hub.modes.cancel(who)
    except SwitchRefused as e:
        return _refused(e)


@router.get("/modes")
async def modes(request: Request, who: Principal = Depends(need("infer"))):
    hub = request.app.state.hub
    out = []
    for m in hub.presets.available:
        p = hub.presets[m]
        out.append({"name": m, "title": p.title, "model": p.raw.get("model"), "served_name": p.served_name,
                    "loaded": hub.modes.loaded == m, "settings": hub.presets.settings(m),
                    "expected": p.expected})
    return {"modes": out, "current": hub.modes.loaded}


@router.get("/modes/{mode}/settings")
async def get_settings(mode: str, request: Request, who: Principal = Depends(need("infer"))):
    hub = request.app.state.hub
    if mode not in request.app.state.hub.presets:
        raise HTTPException(404, "unknown mode")
    return {"mode": mode, "settings": hub.presets.settings(mode), "defaults": hub.presets[mode].defaults,
            "help": SETTING_HELP, "applies": "on the next load of this mode"}


@router.put("/modes/{mode}/settings")
async def put_settings(mode: str, values: dict[str, Any], request: Request, who: Principal = Depends(need("admin"))):
    hub = request.app.state.hub
    if mode not in request.app.state.hub.presets:
        raise HTTPException(404, "unknown mode")
    try:
        s = hub.presets.update_settings(mode, values, who.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"mode": mode, "settings": s, "applies": "on the next load of this mode"}


@router.get("/modes/{mode}/spec")
async def get_spec(mode: str, request: Request, who: Principal = Depends(need("admin"))):
    if mode not in request.app.state.hub.presets:
        raise HTTPException(404, "unknown mode")
    spec = request.app.state.hub.presets.render(mode)
    if spec.get("uploads"):          # a recipe's mod files: listed, not their text (that is on the recipe page)
        spec["uploads"] = [{"path": f["path"], "bytes": len(f["data"].encode())} for f in spec["uploads"]]
    return spec


@router.get("/modes/{mode}/logs")
async def logs(mode: str, request: Request, tail: int = 300, who: Principal = Depends(need("admin"))):
    hub = request.app.state.hub
    if mode not in request.app.state.hub.presets:
        raise HTTPException(404, "unknown mode")
    text = await hub.runtime.logs(mode, hub.presets[mode].container, tail=max(10, min(tail, 5000)))
    return {"mode": mode, "logs": text}


class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scope: str = "infer"
    expires_days: float | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=16)


@router.get("/keys")
async def get_keys(request: Request, who: Principal = Depends(need("admin"))):
    return {"keys": keymod.list_keys(request.app.state.db)}


@router.post("/keys")
async def post_key(body: KeyBody, request: Request, who: Principal = Depends(need("admin"))):
    try:
        kid, raw = keymod.create_key(request.app.state.db, body.name, body.scope, who.label, body.expires_days,
                                     body.max_concurrency, user_id=who.owner_id)   # the caller's (system: none)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"id": kid, "key": raw, "note": "shown once; store it now"}, status_code=201)


@router.delete("/keys/{key_id}")
async def delete_key(key_id: int, request: Request, who: Principal = Depends(need("admin"))):
    if not keymod.revoke_key(request.app.state.db, key_id, who.label):
        raise HTTPException(404, "no such active key")
    return {"revoked": key_id}


@router.get("/activity")
async def activity(request: Request, limit: int = 100, since: float | None = None, mode: str | None = None,
                   who: Principal = Depends(need("admin"))):
    db = request.app.state.db
    q = "SELECT * FROM activity WHERE 1=1"
    params: list[Any] = []
    if since:
        q += " AND ts >= ?"
        params.append(since)
    if mode:
        q += " AND mode = ?"
        params.append(mode)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    return {"activity": [dict(r) for r in db.all(q, tuple(params))]}


@router.get("/events")
async def events(request: Request, limit: int = 100, who: Principal = Depends(need("admin"))):
    rows = request.app.state.db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),))
    return {"events": [dict(r) for r in rows]}


@router.get("/stats")
async def stats(request: Request, hours: float = 24, who: Principal = Depends(need("infer"))):
    """Per-mode summary of the hub's own request log (the history lives in Grafana)."""
    db = request.app.state.db
    since = time.time() - hours * 3600
    rows = db.all(
        "SELECT mode, COUNT(*) n, SUM(prompt_tokens) p, SUM(completion_tokens) c, AVG(ttft) ttft,"
        " AVG(gen_tps) tps, SUM(outcome='error') errors, SUM(outcome='disconnected') disc"
        " FROM activity WHERE ts >= ? AND outcome != 'refused' GROUP BY mode", (since,))
    return {"hours": hours, "modes": [dict(r) for r in rows]}
