"""A fake vLLM OpenAI server for tests: instant, no GPU.

It streams `n` tokens with a delay, honours stream_options.include_usage, exposes /metrics with running and
waiting gauges, and records every request body and every request the client abandoned.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse


class FakeState:
    def __init__(self, served_name: str):
        self.served_name = served_name
        self.bodies: list[dict] = []
        self.running = 0
        self.aborted = 0
        self.completed = 0
        self.token_delay = 0.01
        self.tokens = 20


def make_app(state: FakeState) -> FastAPI:
    app = FastAPI()

    @app.post("/tokenize")
    async def tokenize(request: Request):
        body = await request.json()
        return {"count": len(str(body.get("prompt", "")).split()) * 13 // 10}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": state.served_name, "object": "model"}]}

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(
            f"vllm:num_requests_running{{model_name=\"{state.served_name}\"}} {state.running}\n"
            f"vllm:num_requests_waiting{{model_name=\"{state.served_name}\"}} 0\n"
            f"vllm:kv_cache_usage_perc{{model_name=\"{state.served_name}\"}} 0.1\n"
            "vllm:cache_config_info{block_size=\"3024\",kv_cache_size_tokens=\"320000\",num_gpu_blocks=\"4568\"} 1.0\n")

    async def generate(request: Request, chat: bool):
        body = await request.json()
        state.bodies.append(body)
        if body.get("model") != state.served_name:
            return JSONResponse({"error": {"message": "wrong model"}}, status_code=404)
        n = min(int(body.get("max_tokens") or state.tokens), state.tokens)
        delay = float(body.get("fake_delay", state.token_delay))
        rid = f"cmpl-{time.time_ns()}"
        usage = {"prompt_tokens": 7, "completion_tokens": n, "total_tokens": 7 + n,
                 "prompt_tokens_details": {"cached_tokens": 3}}
        finish = "length" if n < state.tokens else "stop"

        def choice(i: int, last: bool):
            if chat:
                return {"index": 0, "delta": {"content": f"t{i} "}, "finish_reason": finish if last else None}
            return {"index": 0, "text": f"t{i} ", "finish_reason": finish if last else None}

        if not body.get("stream"):
            # like vLLM's with_cancellation: abort the request when the client goes away
            state.running += 1
            work = asyncio.create_task(asyncio.sleep(delay * n))
            try:
                while not work.done():
                    await asyncio.wait({work}, timeout=0.05)
                    if not work.done() and await request.is_disconnected():
                        work.cancel()
                        state.aborted += 1
                        return JSONResponse({}, status_code=499)
            finally:
                state.running -= 1
            state.completed += 1
            msg = ({"index": 0, "message": {"role": "assistant", "content": " ".join(f"t{i}" for i in range(n))},
                    "finish_reason": finish} if chat else {"index": 0, "text": "x", "finish_reason": finish})
            if chat and body.get("tools"):
                fn = body["tools"][0]["function"]["name"]
                msg = {"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                       "tool_calls": [{"id": "call_1", "type": "function",
                                       "function": {"name": fn, "arguments": json.dumps({"city": "Paris"})}}]}}
            return {"id": rid, "object": "chat.completion", "model": state.served_name, "choices": [msg],
                    "usage": usage}

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        async def events():
            state.running += 1
            done = False
            try:
                for i in range(n):
                    await asyncio.sleep(delay)
                    yield f"data: {json.dumps({'id': rid, 'choices': [choice(i, i == n - 1)]})}\n\n"
                if include_usage:
                    yield f"data: {json.dumps({'id': rid, 'choices': [], 'usage': usage})}\n\n"
                yield "data: [DONE]\n\n"
                done = True
                state.completed += 1
            finally:
                state.running -= 1
                if not done:
                    state.aborted += 1

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await generate(request, True)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await generate(request, False)

    return app


class FakeEmbedState:
    """A fake llama.cpp server (embedding or rerank): deterministic unit vectors, word-overlap scores."""
    def __init__(self, served_name: str, dims: int = 1024):
        self.served_name = served_name
        self.dims = dims
        self.bodies: list[dict] = []
        self.delay = 0.0
        self.running = 0


def _vector(text: str, dims: int) -> list[float]:
    import hashlib
    import math
    raw = [b - 127.5 for b in hashlib.sha512(text.encode()).digest() * (dims // 64 + 1)][:dims]
    n = math.sqrt(sum(x * x for x in raw))
    return [x / n for x in raw]


def make_embed_app(state: FakeEmbedState) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        body = await request.json()
        state.bodies.append(body)
        inp = body["input"]
        items = [inp] if isinstance(inp, str) else inp
        if any("TOO_LARGE" in str(x) for x in items):
            return JSONResponse({"error": {"code": 500, "type": "server_error", "message":
                                           "input is too large to process. increase the physical batch size"}},
                                status_code=500)
        state.running += 1
        try:
            await asyncio.sleep(state.delay)
        finally:
            state.running -= 1
        toks = sum(len(str(x).split()) + 1 for x in items)
        return {"object": "list", "model": body.get("model"),
                "data": [{"object": "embedding", "index": i, "embedding": _vector(str(x), state.dims)}
                         for i, x in enumerate(items)],
                "usage": {"prompt_tokens": toks, "total_tokens": toks}}

    @app.post("/v1/rerank")
    async def rerank(request: Request):
        body = await request.json()
        state.bodies.append(body)
        q = set(body["query"].lower().split())
        res = [{"index": i, "relevance_score": len(q & set(d.lower().split())) / (len(q) or 1)}
               for i, d in enumerate(body["documents"])]
        res.sort(key=lambda r: -r["relevance_score"])
        if body.get("top_n"):
            res = res[:body["top_n"]]
        return {"model": body.get("model"), "results": res, "usage": {"prompt_tokens": 10, "total_tokens": 10}}

    return app
