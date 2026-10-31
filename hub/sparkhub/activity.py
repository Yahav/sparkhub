"""Who is using the models right now, per API key, and the per-request record."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from . import metrics
from .db import DB


@dataclass
class ClientState:
    key_id: int
    name: str
    inflight: int = 0
    waiting: int = 0
    last_start: float = 0.0
    last_end: float = 0.0
    sem: asyncio.Semaphore | None = None
    sem_size: int = 0

    def last_seen(self, now: float) -> float:
        return now if self.inflight or self.waiting else max(self.last_start, self.last_end)


@dataclass
class RequestRecord:
    key_id: int | None
    key_name: str | None
    client_ip: str | None
    mode: str | None
    model: str | None
    endpoint: str
    stream: bool
    started: float = field(default_factory=time.time)
    status: int = 0
    outcome: str = "error"
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    thinking: bool | None = None
    reasoning_tokens: int | None = None
    queue_wait: float | None = None
    ttft: float | None = None
    gen_tps: float | None = None
    duration: float | None = None
    user_id: int | None = None          # the key's owner
    user_name: str | None = None


class Tracker:
    def __init__(self, db: DB):
        self.db = db
        self.clients: dict[int, ClientState] = {}
        self.inflight_total = 0
        self.last_any = 0.0

    def client(self, key_id: int, name: str) -> ClientState:
        c = self.clients.get(key_id)
        if c is None:
            c = self.clients[key_id] = ClientState(key_id, name)
        return c

    def semaphore(self, c: ClientState, size: int) -> asyncio.Semaphore:
        # a changed cap takes effect once the key is idle
        if c.sem is None or (c.sem_size != size and c.inflight == 0 and c.waiting == 0):
            c.sem = asyncio.Semaphore(size)
            c.sem_size = size
        return c.sem

    def active(self, window_s: float, exclude_key: int | None = None) -> list[dict]:
        """Clients with a request in flight, waiting, or within the last window_s seconds."""
        now = time.time()
        out = []
        for c in self.clients.values():
            if c.key_id == exclude_key:
                continue
            seen = c.last_seen(now)
            if c.inflight or c.waiting or now - seen < window_s:
                out.append({"key_id": c.key_id, "name": c.name, "inflight": c.inflight, "waiting": c.waiting,
                            "idle_s": round(now - seen, 1)})
        return sorted(out, key=lambda x: x["idle_s"])

    def quiet_for(self) -> float:
        """Seconds since the last request ended; 0 while anything is in flight."""
        if self.inflight_total > 0 or any(c.waiting for c in self.clients.values()):
            return 0.0
        return time.time() - self.last_any if self.last_any else float("inf")

    def begin(self, c: ClientState | None, mode: str | None) -> None:
        now = time.time()
        self.inflight_total += 1
        self.last_any = now
        if c:
            c.inflight += 1
            c.last_start = now
        metrics.INFLIGHT.labels(mode or "none").inc()

    def end(self, c: ClientState | None, mode: str | None) -> None:
        now = time.time()
        self.inflight_total -= 1
        self.last_any = now
        if c:
            c.inflight -= 1
            c.last_end = now
        metrics.INFLIGHT.labels(mode or "none").dec()

    def record(self, r: RequestRecord) -> None:
        r.duration = time.time() - r.started
        mode = r.mode or "none"
        metrics.REQUESTS.labels(mode, r.outcome).inc()
        if r.outcome != "refused":
            metrics.DURATION.labels(mode).observe(r.duration)
        if r.prompt_tokens:
            metrics.TOKENS.labels(mode, "prompt").inc(r.prompt_tokens)
        if r.completion_tokens:
            metrics.TOKENS.labels(mode, "completion").inc(r.completion_tokens)
        if r.cached_tokens:
            metrics.TOKENS.labels(mode, "cached").inc(r.cached_tokens)
        if r.ttft is not None:
            metrics.TTFT.labels(mode).observe(r.ttft)
        if r.gen_tps is not None:
            metrics.GEN_TPS.labels(mode).observe(r.gen_tps)
        if r.queue_wait is not None:
            metrics.QUEUE_WAIT.labels(mode).observe(r.queue_wait)
        self.db.execute(
            "INSERT INTO activity(ts, key_id, key_name, client_ip, mode, model, endpoint, stream, status, outcome,"
            " finish_reason, prompt_tokens, completion_tokens, cached_tokens, queue_wait, ttft, gen_tps, duration,"
            " thinking, reasoning_tokens, user_id, user_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.started, r.key_id, r.key_name, r.client_ip, r.mode, r.model, r.endpoint, int(r.stream), r.status,
             r.outcome, r.finish_reason, r.prompt_tokens, r.completion_tokens, r.cached_tokens,
             _r(r.queue_wait), _r(r.ttft), _r(r.gen_tps), _r(r.duration),
             None if r.thinking is None else int(r.thinking), r.reasoning_tokens, r.user_id, r.user_name))


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 4)
