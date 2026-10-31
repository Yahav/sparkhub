"""OpenAI-compatible gateway: /v1/* through to the loaded vLLM.

- API-key auth; model aliases fast / quality / custom / auto and their -thinking variants;
  custom only while a recipe is assigned to it (custom.py)
- per-key concurrency caps with a measured queue wait
- streaming passed through event by event; usage requested from vLLM on every stream and stripped again
  when the client didn't ask for it
- the upstream request is closed when the client disconnects, so vLLM stops generating
- per-request numbers (time to first token, generation speed, tokens, outcome) go to the activity table and
  /metrics. No prompt or response content is stored.
- /v1/embeddings and /v1/rerank go to the embedding service (embeddings.py), started on demand
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import struct
import time
from typing import Any

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .activity import ClientState, RequestRecord
from .embeddings import EmbedUnavailable, estimate_tokens
from .keys import Principal, verify_key
from .presets import MODES

log = logging.getLogger("sparkhub.gateway")
router = APIRouter()

MAX_BODY = 32 * 1024 * 1024
EFFORTS = ("low", "medium", "high", "xhigh", "max")   # what Flash's (effort-alias) template accepts
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=120.0, pool=30.0)   # 30-min reads


def oai_error(status: int, message: str, etype: str = "invalid_request_error", **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": {"message": message, "type": etype, "code": status}}
    body["error"].update(extra)
    headers = {"Retry-After": "30"} if status == 503 else None
    return JSONResponse(body, status_code=status, headers=headers)


def bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.headers.get("x-api-key")


def key_principal(request: Request) -> Principal | None:
    raw = bearer(request)
    return verify_key(request.app.state.db, raw) if raw else None


def resolve_model(hub, name: str | None) -> tuple[str | None, bool | None]:
    """model name -> (mode, thinking override). mode None = unknown name."""
    name = (name or "auto").strip()
    thinking = None
    if name.endswith("-thinking"):
        name, thinking = name[: -len("-thinking")], True
    if name == "auto":
        return hub.modes.loaded or hub.modes.target or hub.modes.previous, thinking
    if name in MODES:
        return (name if name in hub.presets else None), thinking
    return hub.presets.by_served_name(name), thinking


@router.get("/v1/models")
async def list_models(request: Request):
    who = key_principal(request)
    if who is None:
        return oai_error(401, "invalid or missing API key", "authentication_error")
    hub = request.app.state.hub
    loaded = hub.modes.loaded
    data = []
    now = int(time.time())
    for alias in ("auto", *hub.presets.available):
        for suffix in ("", "-thinking"):
            mode = loaded if alias == "auto" else alias
            s = hub.presets.settings(mode) if mode else {}
            data.append({
                "id": alias + suffix, "object": "model", "created": now, "owned_by": "sparkhub",
                "max_model_len": s.get("max_model_len"),
                "sparkhub": {"mode": mode, "loaded": bool(mode and mode == loaded and hub.modes.state == "ready"),
                             "thinking": bool(suffix) or bool(s.get("default_thinking")),
                             "served_name": hub.presets[mode].served_name if mode else None},
            })
    em = hub.embed
    for name, svc in em.services.items():
        if em.policy(name) != "off":
            data.append({"id": svc.served_name, "object": "model", "created": now, "owned_by": "sparkhub",
                         "sparkhub": {"kind": "embedding" if name == "embed" else "rerank",
                                      "endpoint": svc.raw["endpoint"], "state": svc.state, "policy": em.policy(name),
                                      **({"dimensions": em.raw.get("dimensions")} if name == "embed" else {})}})
    return {"object": "list", "data": data, "sparkhub": {"mode": loaded, "state": hub.modes.state}}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await proxy(request, "/v1/chat/completions", chat=True)


@router.post("/v1/completions")
async def completions(request: Request):
    return await proxy(request, "/v1/completions", chat=False)


def _usage_into(rec: RequestRecord, usage: dict | None) -> None:
    if not usage:
        return
    rec.prompt_tokens = usage.get("prompt_tokens")
    rec.completion_tokens = usage.get("completion_tokens")
    details = usage.get("prompt_tokens_details") or {}
    rec.cached_tokens = details.get("cached_tokens")
    rec.reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")


def _has_output(choice: dict, chat: bool) -> bool:
    if not chat:
        return bool(choice.get("text"))
    d = choice.get("delta") or {}
    return bool(d.get("content") or d.get("reasoning_content") or d.get("reasoning") or d.get("tool_calls"))


async def proxy(request: Request, path: str, chat: bool):
    hub = request.app.state.hub
    db = request.app.state.db
    client_ip = request.client.host if request.client else None
    rec = RequestRecord(None, None, client_ip, None, None, path, False)

    def refuse(status: int, msg: str, etype: str = "invalid_request_error", **extra) -> JSONResponse:
        rec.status, rec.outcome = status, "refused"
        if rec.key_id is not None:   # anonymous refusals are not logged per request (only counted)
            hub.tracker.record(rec)
        return oai_error(status, msg, etype, **extra)

    who = key_principal(request)
    if who is None:
        rec.status, rec.outcome = 401, "refused"
        from . import metrics
        metrics.REQUESTS.labels("none", "unauthorized").inc()
        return oai_error(401, "invalid or missing API key", "authentication_error")
    rec.key_id, rec.key_name, rec.user_id, rec.user_name = who.id, who.name, who.owner_id, who.owner_name

    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return refuse(413, "request body too large")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return refuse(413, "request body too large")
    try:
        body = json.loads(raw)
        assert isinstance(body, dict)
    except Exception:
        return refuse(400, "request body must be a JSON object")

    rec.model = str(body.get("model") or "auto")[:64]
    mode, thinking = resolve_model(hub, rec.model)
    if mode is None:
        em = hub.embed
        for name, svc in em.services.items():
            if rec.model == svc.served_name or (rec.model and em.by_model(rec.model, name)):
                return refuse(400, f"{rec.model!r} is not a chat model; use {svc.raw['endpoint']}")
        if hub.modes.loaded is None and rec.model.split("-thinking")[0] == "auto":
            return refuse(503, f"no model is loaded ({hub.modes.state}: {hub.modes.message})", "service_unavailable")
        if rec.model.removesuffix("-thinking") == "custom":
            return refuse(404, "the custom mode has no recipe assigned (an admin sets one up on the Models page)",
                          "not_found_error")
        return refuse(404, f"unknown model {rec.model!r}; use {', '.join(['auto', *hub.presets.available])} "
                           f"(optionally with -thinking)", "not_found_error")
    rec.mode = mode
    rec.stream = bool(body.get("stream"))

    benchmark = hub.bench.blocks(who.id)
    if benchmark:
        return refuse(503, benchmark, "service_unavailable", sparkhub_mode=hub.modes.loaded,
                      sparkhub_state="benchmark")
    blocked = hub.modes.accepting(mode, who.id)
    if blocked:
        status, msg = blocked
        return refuse(status, msg, "mode_not_loaded" if status == 409 else "service_unavailable",
                      sparkhub_mode=hub.modes.loaded, sparkhub_state=hub.modes.state)

    # rewrite the request for vLLM
    preset = hub.presets[mode]
    s = hub.presets.settings(mode)
    body["model"] = preset.served_name
    if chat:
        ctk = body.get("chat_template_kwargs")
        if not isinstance(ctk, dict):
            ctk = {}
        # reasoning_effort (OpenAI style; OpenCode's model variants send it) means "think":
        # none/minimal/off = no thinking, any other level = thinking on. Only Flash's template knows effort
        # levels; for other modes the field is dropped. Priority: explicit enable_thinking > -thinking alias >
        # reasoning_effort > the mode's default.
        effort = body.get("reasoning_effort")
        effort_thinking = None
        if isinstance(effort, str):
            e = effort.strip().lower()
            effort_thinking = e not in ("none", "minimal", "off", "")
            if not effort_thinking or not preset.raw.get("reasoning_effort"):
                body.pop("reasoning_effort", None)
            elif e not in EFFORTS:
                body["reasoning_effort"] = "high"
        elif effort is not None:
            body.pop("reasoning_effort", None)
        if "enable_thinking" not in ctk:
            if thinking is not None:
                ctk["enable_thinking"] = thinking
            elif effort_thinking is not None:
                ctk["enable_thinking"] = effort_thinking
            else:
                ctk["enable_thinking"] = bool(s["default_thinking"])
        elif thinking is not None:
            ctk["enable_thinking"] = thinking
        if not ctk["enable_thinking"]:
            body.pop("reasoning_effort", None)
        body["chat_template_kwargs"] = ctk
        rec.thinking = bool(ctk["enable_thinking"])
    if body.get("max_tokens") is None and body.get("max_completion_tokens") is None:
        body["max_tokens"] = s["default_max_tokens"]
    client_wants_usage = False
    if rec.stream:
        so = body.get("stream_options")
        if not isinstance(so, dict):
            so = {}
        client_wants_usage = bool(so.get("include_usage"))
        so["include_usage"] = True
        body["stream_options"] = so

    # per-key concurrency slot
    tracker = hub.tracker
    c: ClientState = tracker.client(who.id, who.name)
    cap = who.max_concurrency or db.get_setting("key_max_concurrency")
    sem = tracker.semaphore(c, cap)
    t_wait = time.time()
    c.waiting += 1
    try:
        await asyncio.wait_for(sem.acquire(), timeout=db.get_setting("queue_timeout_s"))
    except asyncio.TimeoutError:
        c.waiting -= 1
        return refuse(429, f"this key already has {cap} requests running; waited too long for a slot",
                      "rate_limit_error")
    except asyncio.CancelledError:
        c.waiting -= 1
        raise
    c.waiting -= 1
    rec.queue_wait = time.time() - t_wait

    # the mode may have changed while this request waited
    blocked = hub.modes.accepting(mode, who.id)
    if blocked:
        sem.release()
        status, msg = blocked
        return refuse(status, msg, "service_unavailable")

    tracker.begin(c, mode)
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            sem.release()
            tracker.end(c, mode)
            tracker.record(rec)

    url = hub.modes.upstream(mode) + path
    http: httpx.AsyncClient = hub.upstream_http
    t_send = time.time()
    try:
        if not rec.stream:
            return await _non_stream(request, http, url, body, rec, t_send, release)
        upstream_req = http.build_request("POST", url, json=body)
        resp = await http.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        rec.status, rec.outcome = 502, "error"
        release()
        return oai_error(502, f"upstream error: {type(e).__name__}", "upstream_error")
    except BaseException:
        rec.outcome = "disconnected"
        release()
        raise

    if resp.status_code != 200:
        content = await resp.aread()
        await resp.aclose()
        rec.status, rec.outcome = resp.status_code, "error"
        release()
        return Response(content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))

    rec.status = 200

    async def events():
        buf = b""
        t_first = t_last = None
        rec.outcome = "disconnected"   # until the stream ends cleanly
        try:
            async for chunk in resp.aiter_raw():
                buf += chunk
                while b"\n\n" in buf:
                    event, buf = buf.split(b"\n\n", 1)
                    forward = True
                    for line in event.split(b"\n"):
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        choices = obj.get("choices") or []
                        for ch in choices:
                            if _has_output(ch, chat):
                                now = time.time()
                                if t_first is None:
                                    t_first = now
                                t_last = now
                            if ch.get("finish_reason"):
                                rec.finish_reason = ch["finish_reason"]
                        if obj.get("usage"):
                            _usage_into(rec, obj["usage"])
                            if not choices and not client_wants_usage:
                                forward = False   # the usage-only chunk the client didn't ask for
                        if "error" in obj and not choices:
                            rec.finish_reason = "error"
                    if forward:
                        yield event + b"\n\n"
            if buf.strip():
                yield buf
            rec.outcome = {"length": "length", "error": "error"}.get(rec.finish_reason or "", "finished")
        finally:
            if t_first is not None:
                rec.ttft = t_first - t_send
                if rec.completion_tokens and rec.completion_tokens > 1 and t_last and t_last > t_first:
                    rec.gen_tps = (rec.completion_tokens - 1) / (t_last - t_first)
            with anyio.CancelScope(shield=True):
                await resp.aclose()     # closes the connection, so vLLM aborts the request
                release()

    return StreamingResponse(events(), status_code=200, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _non_stream(request: Request, http: httpx.AsyncClient, url: str, body: dict, rec: RequestRecord,
                      t_send: float, release) -> Response:
    task = asyncio.create_task(http.post(url, json=body))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=1.0)
            if done:
                break
            if await request.is_disconnected():
                task.cancel()    # closes the upstream connection; vLLM aborts the request
                rec.outcome = "disconnected"
                with anyio.CancelScope(shield=True):
                    try:
                        await task
                    except BaseException:
                        pass
                return Response(status_code=499)
        try:
            resp = task.result()
        except httpx.HTTPError as e:
            rec.status, rec.outcome = 502, "error"
            return oai_error(502, f"upstream error: {type(e).__name__}", "upstream_error")
        rec.status = resp.status_code
        if resp.status_code == 200:
            try:
                obj = resp.json()
                _usage_into(rec, obj.get("usage"))
                ch = (obj.get("choices") or [{}])[0]
                rec.finish_reason = ch.get("finish_reason")
                rec.outcome = "length" if rec.finish_reason == "length" else "finished"
                # no first-token time without streaming, so no ttft / gen_tps here; vLLM's own metrics cover it
            except ValueError:
                rec.outcome = "error"
        else:
            rec.outcome = "error"
        return Response(resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))
    except asyncio.CancelledError:
        task.cancel()
        rec.outcome = "disconnected"
        raise
    finally:
        release()


# ---------------------------------------------------------------------------------------------- embedding service
MAX_INPUTS = 2048          # OpenAI's limit for one embeddings request
MAX_DOCUMENTS = 1000


def _shorten(vec: list[float], dims: int) -> list[float]:
    """Qwen3-Embedding is trained for shortened vectors (MRL): keep the first dims, then re-normalize."""
    v = vec[:dims]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _b64(vec: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode()


@router.post("/v1/embeddings")
async def embeddings(request: Request):
    return await service_request(request, "embed", "/v1/embeddings")


@router.post("/v1/rerank")
async def rerank(request: Request):
    return await service_request(request, "rerank", "/v1/rerank")


async def service_request(request: Request, name: str, path: str):
    hub = request.app.state.hub
    em = hub.embed
    rec = RequestRecord(None, None, request.client.host if request.client else None, name, None, path, False)

    def refuse(status: int, msg: str, etype: str = "invalid_request_error") -> JSONResponse:
        rec.status, rec.outcome = status, "refused"
        if rec.key_id is not None:
            hub.tracker.record(rec)
        return oai_error(status, msg, etype)

    who = key_principal(request)
    if who is None:
        from . import metrics
        metrics.REQUESTS.labels("none", "unauthorized").inc()
        return oai_error(401, "invalid or missing API key", "authentication_error")
    rec.key_id, rec.key_name, rec.user_id, rec.user_name = who.id, who.name, who.owner_id, who.owner_name
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return refuse(413, "request body too large")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return refuse(413, "request body too large")
    try:
        body = json.loads(raw)
        assert isinstance(body, dict)
    except Exception:
        return refuse(400, "request body must be a JSON object")
    if not em.installed:
        return refuse(503, "the embedding service is not installed on this hub (./install.sh, EMBEDDINGS)",
                      "service_unavailable")
    svc = em.services[name]
    model = body.get("model")
    rec.model = str(model or svc.served_name)[:64]
    if not isinstance(model, (str, type(None))) or not em.by_model(model, name):
        other = next((s for n, s in em.services.items() if n != name and em.by_model(str(model), n)), None)
        if other:
            return refuse(400, f"{model!r} is the {other.raw['title'].lower()}; use {other.raw['endpoint']}")
        if resolve_model(hub, str(model))[0]:
            return refuse(400, f"{model!r} is a chat model; use /v1/chat/completions")
        return refuse(404, f"unknown model {model!r}; this endpoint serves {svc.served_name}", "not_found_error")

    if name == "embed":
        inp = body.get("input")
        if inp in (None, "", []):
            return refuse(400, "input is required: a string or a list of strings")
        if isinstance(inp, list) and len(inp) > MAX_INPUTS:
            return refuse(400, f"at most {MAX_INPUTS} inputs per request")
        dims = body.get("dimensions")
        full = int(em.raw.get("dimensions") or 1024)
        if dims is not None and (isinstance(dims, bool) or not isinstance(dims, int) or not 32 <= dims <= full):
            return refuse(400, f"dimensions must be an integer between 32 and {full}")
        fmt = body.get("encoding_format") or "float"
        if fmt not in ("float", "base64"):
            return refuse(400, "encoding_format must be float or base64")
        upstream = {"input": inp, "model": svc.served_name}
        tokens = estimate_tokens(inp)
    else:
        query, docs = body.get("query"), body.get("documents")
        if not isinstance(query, str) or not query:
            return refuse(400, "query is required (a string)")
        if not isinstance(docs, list) or not docs or not all(isinstance(d, str) for d in docs):
            return refuse(400, "documents is required (a list of strings)")
        if len(docs) > MAX_DOCUMENTS:
            return refuse(400, f"at most {MAX_DOCUMENTS} documents per request")
        upstream = {"model": svc.served_name, "query": query, "documents": docs}
        for k in ("top_n", "return_documents"):
            if k in body:
                upstream[k] = body[k]
        tokens = 0      # rerank calls always count as small (chat first only holds bulk embedding)

    t0 = time.time()
    try:
        await em.hold_for_chat(tokens)
        url = await em.ensure(name)
    except EmbedUnavailable as e:
        return refuse(e.status, e.message, "service_unavailable")
    rec.queue_wait = time.time() - t0
    em.begin(name)
    try:
        resp = await hub.upstream_http.post(url + path, json=upstream)
    except httpx.HTTPError as e:
        rec.status, rec.outcome = 502, "error"
        hub.tracker.record(rec)
        return oai_error(502, f"upstream error: {type(e).__name__}", "upstream_error")
    finally:
        em.end(name)
    rec.status = resp.status_code
    if resp.status_code != 200:
        rec.outcome = "error"
        hub.tracker.record(rec)
        try:
            msg = (resp.json().get("error") or {}).get("message") or resp.text[:300]
        except ValueError:
            msg = resp.text[:300]
        too_long = "too large" in msg or "context size" in msg       # llama.cpp's wording differs by version
        if too_long:
            msg += f" (one input may have at most {svc.raw.get('max_input_tokens')} tokens; split it into chunks)"
        client_err = resp.status_code < 500 or too_long
        return oai_error(400 if client_err else 502, msg, "invalid_request_error" if client_err else "upstream_error")
    try:
        obj = resp.json()
    except ValueError:
        rec.outcome = "error"
        hub.tracker.record(rec)
        return oai_error(502, "upstream answered with something that is not JSON", "upstream_error")
    usage = obj.get("usage") or {}
    rec.prompt_tokens = usage.get("prompt_tokens") or usage.get("total_tokens")
    obj["model"] = svc.served_name
    if name == "embed":
        for d in obj.get("data") or []:
            vec = d.get("embedding")
            if isinstance(vec, list):
                if dims and dims < len(vec):
                    vec = _shorten(vec, dims)
                d["embedding"] = _b64(vec) if fmt == "base64" else vec
    rec.outcome = "finished"
    hub.tracker.record(rec)
    return JSONResponse(obj)
