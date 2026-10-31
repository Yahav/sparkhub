"""The embedding service: an embedding model and a reranker next to the chat model, each in its
own llama.cpp container. Stateless like a hosted embeddings API: text in, vectors (or scores) out; the hub stores
neither texts nor vectors.

Run mode (Settings → Models → Embeddings): "always" keeps the embedder up, "on-demand" starts it on the first
request (~1-2 s) and stops it after embed_idle_minutes without one, "off" refuses requests. The reranker is always
on-demand, and off with the embedder or its own switch.

Chat first: a large embedding request (bulk indexing) halves the chat model's speed while it runs (measured
2026-10-28), so it waits while chat requests are in flight, for at most embed_hold_s. Small requests (a search
query, every rerank call) go straight through.

Their containers carry their own label (EMBED_LABEL), never the model label: a mode switch stops every container
with the model label as a stray, and must leave these running.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

import httpx

from .activity import Tracker
from .config import Config
from .db import DB
from .runtime import EMBED_LABEL, Runtime

log = logging.getLogger("sparkhub.embeddings")

RUN_MODES = ("always", "on-demand", "off")
RUN_MODE_LABELS = {"always": "Always on", "on-demand": "On-demand", "off": "Off"}
SERVICES = ("embed", "rerank")
SMALL_TOKENS = 2048          # estimated tokens; a request at or below this never waits for chat
START_TIMEOUT_S = 60         # llama.cpp answers /health within ~1-2 s on the Spark
RESTART_BACKOFF_S = 300      # "always": after a failed start, wait this long before trying again


class EmbedUnavailable(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Service:
    name: str
    raw: dict[str, Any]
    container: str
    host_port: int
    state: str = "stopped"          # stopped | starting | ready | failed
    error: str | None = None
    started_at: float | None = None
    last_used: float = 0.0
    failed_at: float = 0.0
    inflight: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def served_name(self) -> str:
        return self.raw["served_name"]


def estimate_tokens(inp: Any) -> int:
    """Rough size of an embeddings `input`: ~3 characters per token for text, one per token id."""
    if isinstance(inp, str):
        return len(inp) // 3 + 1
    if isinstance(inp, int):
        return 1
    if isinstance(inp, list):
        return sum(estimate_tokens(x) for x in inp)
    return 0


class EmbedManager:
    def __init__(self, cfg: Config, db: DB, runtime: Runtime, modes, tracker: Tracker):
        self.cfg = cfg
        self.db = db
        self.runtime = runtime
        self.modes = modes
        self.tracker = tracker
        self.raw: dict[str, Any] = {}
        self.services: dict[str, Service] = {}
        if cfg.embeddings:
            self.raw = json.loads((cfg.presets_dir / "embed.json").read_text())
            for name in SERVICES:
                raw = self.raw["services"][name]
                port = cfg.embed_ports.get(name, raw["host_port"])
                self.services[name] = Service(name, raw, f"{cfg.instance}-{name}", int(port))
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        self.health_poll_s = 0.25
        self.loop_s = 15.0
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ settings
    @property
    def installed(self) -> bool:
        return bool(self.services)

    def run_mode(self) -> str:
        if not self.installed:
            return "off"
        v = self.db.get_setting("embed_mode")
        return v if v in RUN_MODES else "on-demand"

    def policy(self, name: str) -> str:
        """always | on-demand | off for one service."""
        mode = self.run_mode()
        if name == "rerank":
            return "off" if mode == "off" or not self.db.get_setting("embed_rerank") else "on-demand"
        return mode

    def by_model(self, model: str | None, name: str) -> bool:
        """Does a request's `model` name this service? Empty means the default (this service)."""
        m = (model or "").strip()
        aliases = {"embed": ("", "embed", "embedding", "embeddings"), "rerank": ("", "rerank", "reranker")}[name]
        return m in aliases or m == self.services[name].served_name

    def upstream(self, name: str) -> str:
        return f"http://{self.cfg.upstream_host}:{self.services[name].host_port}"

    def weights_path(self, name: str) -> Path:
        paths = {"models_dir": self.cfg.host_models_dir, "hf_cache": self.cfg.host_hf_cache}
        return Path(Template(self.services[name].raw["weights"]).substitute(paths))

    def problems(self, name: str) -> list[str]:
        if not self.cfg.check_install:
            return []
        p = self.weights_path(name)
        return [] if p.is_file() else [f"model file not found ({p}); run ./install.sh"]

    def render(self, name: str) -> dict[str, Any]:
        svc = self.services[name]
        paths = {"models_dir": self.cfg.host_models_dir, "hf_cache": self.cfg.host_hf_cache}
        return {"mode": name, "container": svc.container, "image": self.raw["image"], "env": {},
                "binds": [Template(b).substitute(paths) for b in self.raw["binds"]], "args": list(svc.raw["args"]),
                "gpus": "all", "host_port": svc.host_port, "container_port": 8080,
                "labels": {EMBED_LABEL: name}, "user": f"{os.getuid()}:{os.getgid()}"}

    # ------------------------------------------------------------------ requests
    async def ensure(self, name: str) -> str:
        """The service's upstream URL, starting it first if needed. EmbedUnavailable if it can't serve now."""
        svc = self.services.get(name)
        if svc is None:
            raise EmbedUnavailable(503, "the embedding service is not installed on this hub (./install.sh, EMBEDDINGS)")
        pol = self.policy(name)
        if pol == "off":
            what = "the reranker is" if name == "rerank" and self.run_mode() != "off" else "the embedding service is"
            raise EmbedUnavailable(503, f"{what} off (Settings → Models → Embeddings)")
        svc.last_used = time.time()
        if svc.state == "ready":
            return self.upstream(name)
        async with svc.lock:
            if svc.state != "ready":
                if self.modes.state in ("loading", "stopping"):
                    # the chat model is loading: starting another GPU process now competes for its memory
                    raise EmbedUnavailable(503, f"the Spark is loading a model ({self.modes.message}); "
                                                f"the {svc.raw['title'].lower()} starts once it is ready")
                await self._start(svc)
        return self.upstream(name)

    async def hold_for_chat(self, tokens: int) -> float:
        """Chat first: a large request waits while chat requests are in flight. Returns the seconds waited."""
        if tokens <= SMALL_TOKENS or self.tracker.inflight_total == 0:
            return 0.0
        t0 = time.time()
        limit = float(self.db.get_setting("embed_hold_s"))
        while self.tracker.inflight_total > 0 and time.time() - t0 < limit:
            await asyncio.sleep(0.5)
        return time.time() - t0

    def begin(self, name: str) -> None:
        svc = self.services[name]
        svc.inflight += 1
        svc.last_used = time.time()

    def end(self, name: str) -> None:
        svc = self.services[name]
        svc.inflight -= 1
        svc.last_used = time.time()

    # ------------------------------------------------------------------ containers
    async def _healthy(self, name: str) -> bool:
        try:
            r = await self.http.get(self.upstream(name) + "/health", timeout=2)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _start(self, svc: Service) -> None:
        probs = self.problems(svc.name)
        if probs:
            svc.state, svc.error, svc.failed_at = "failed", probs[0], time.time()
            raise EmbedUnavailable(503, f"{svc.raw['title']}: {probs[0]}")
        svc.state, svc.error = "starting", None
        t0 = time.time()
        log.info("starting %s (%s)", svc.container, svc.served_name)
        try:
            await self.runtime.start(self.render(svc.name))
            while True:
                if await self._healthy(svc.name):
                    break
                st = await self.runtime.state(svc.container)
                if not st or not st["running"]:
                    raise RuntimeError(f"container exited (code {st and st.get('exit_code')})")
                if time.time() - t0 > START_TIMEOUT_S:
                    raise RuntimeError(f"not ready after {START_TIMEOUT_S} s")
                await asyncio.sleep(self.health_poll_s)
        except Exception as e:
            err = f"{svc.raw['title']} did not start: {e}"
            svc.state, svc.error, svc.failed_at = "failed", err, time.time()
            self.db.event("embed_failed", None, service=svc.name, error=str(e)[:300])
            try:
                await self.runtime.stop(svc.name, svc.container)
            except Exception:
                log.exception("removing %s", svc.container)
            raise EmbedUnavailable(503, err) from e
        svc.state, svc.started_at = "ready", time.time()
        self.db.event("embed_started", None, service=svc.name, seconds=round(time.time() - t0, 1))
        log.info("%s ready in %.1f s", svc.container, time.time() - t0)

    async def stop(self, name: str, why: str) -> None:
        svc = self.services[name]
        async with svc.lock:
            if svc.state == "stopped":
                return
            log.info("stopping %s (%s)", svc.container, why)
            await self.runtime.stop(name, svc.container)
            svc.state, svc.started_at = "stopped", None
            self.db.event("embed_stopped", None, service=name, why=why)

    async def apply(self) -> None:
        """Bring the containers in line with the settings: off stops, always starts; on-demand keeps the state."""
        for name, svc in self.services.items():
            pol = self.policy(name)
            if pol == "off" and svc.state in ("ready", "starting", "failed"):
                await self.stop(name, "turned off")
                svc.state, svc.error = "stopped", None
            elif pol == "always" and svc.state != "ready" and self.modes.state not in ("loading", "stopping"):
                if svc.state == "failed" and time.time() - svc.failed_at < RESTART_BACKOFF_S:
                    continue
                try:
                    svc.last_used = time.time()
                    async with svc.lock:
                        if svc.state != "ready":
                            await self._start(svc)
                except EmbedUnavailable as e:
                    log.warning("%s", e.message)

    async def startup(self) -> None:
        """Adopt containers that survived a hub restart (if the settings still want them), remove the rest."""
        for name, svc in self.services.items():
            st = await self.runtime.state(svc.container)
            if st is None:
                continue
            if st["running"] and self.policy(name) != "off" and await self._healthy(name):
                svc.state, svc.started_at, svc.last_used = "ready", time.time(), time.time()
                log.info("adopted %s", svc.container)
            else:
                await self.runtime.stop(name, svc.container)
        if self.installed:
            self._task = asyncio.create_task(self._loop())

    async def _tick(self) -> None:
        now = time.time()
        idle_s = 60 * float(self.db.get_setting("embed_idle_minutes"))
        for name, svc in self.services.items():
            if svc.state == "ready":
                st = await self.runtime.state(svc.container)
                if not st or not st["running"]:
                    why = "removed" if not st else f"exit code {st['exit_code']}"
                    svc.state, svc.error, svc.failed_at = "failed", f"stopped unexpectedly ({why})", now
                    self.db.event("embed_crashed", None, service=name, why=why)
                    try:
                        await self.runtime.stop(name, svc.container)
                    except Exception:
                        log.exception("removing %s", svc.container)
                    continue
                if self.policy(name) == "on-demand" and svc.inflight == 0 and now - svc.last_used > idle_s:
                    await self.stop(name, f"idle for {round((now - svc.last_used) / 60)} min")
        await self.apply()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.loop_s)
            try:
                await self._tick()
            except Exception:
                log.exception("embeddings")

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"installed": self.installed, "run_mode": self.run_mode(),
                               "rerank_enabled": bool(self.db.get_setting("embed_rerank")),
                               "idle_minutes": self.db.get_setting("embed_idle_minutes"),
                               "hold_s": self.db.get_setting("embed_hold_s"),
                               "dimensions": self.raw.get("dimensions"), "services": {}}
        for name, svc in self.services.items():
            out["services"][name] = {
                "title": svc.raw["title"], "model": svc.raw["model"], "served_name": svc.served_name,
                "endpoint": svc.raw["endpoint"], "policy": self.policy(name), "state": svc.state, "error": svc.error,
                "started_at": svc.started_at, "last_used": svc.last_used or None, "inflight": svc.inflight,
                "max_input_tokens": svc.raw.get("max_input_tokens"),
                "mem_gib": (self.raw.get("expected", {}).get("mem_gib") or {}).get(name)}
        return out

    async def shutdown(self) -> None:
        # the containers keep running; the next hub start adopts them
        if self._task and not self._task.done():
            self._task.cancel()
        await self.http.aclose()
