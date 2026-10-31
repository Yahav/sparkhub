"""SparkHub app: wires the database, presets, runtime, mode manager, gateway, control API and dashboard."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from . import metrics
from .activity import Tracker
from .config import Config, load_config
from .db import DB
from .embeddings import EmbedManager
from .lifecycle import ModeManager
from .notify import Notifier, migrate_topic_file
from .bench import BenchManager
from .updates import UpdateChecker
from .custom import CustomManager
from .presets import MODES, Presets
from .runtime import DockerRuntime, Runtime, cpu_times, disk_bytes, loadavg, meminfo

log = logging.getLogger("sparkhub")

# the stack containers the watcher reports on (the models have their own watchdog; the hub is the one watching)
WATCHED = {"nginx": "Web server", "grafana": "Grafana", "prometheus": "Prometheus", "node-exporter": "Host metrics",
           "arcane": "Arcane", "chatui": "Chat"}

VLLM_FIELDS = {
    "vllm:num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
    "vllm:kv_cache_usage_perc": "kv_usage",
    "vllm:generation_tokens_total": "gen_tokens_total",
    "vllm:prompt_tokens_total": "prompt_tokens_total",
    "vllm:num_preemptions_total": "preemptions_total",
    "vllm:prefix_cache_queries_total": "prefix_queries_total",
    "vllm:prefix_cache_hits_total": "prefix_hits_total",
}



def disk_usage(path) -> dict[str, float]:
    """The disk that holds data/ (the host's root disk on a default install)."""
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return {}
    gib = 2**30
    return {"used_gib": round(u.used / gib, 1), "total_gib": round(u.total / gib, 1), "pct": round(100 * u.used / u.total, 1)}

class Hub:
    def __init__(self, cfg: Config, db: DB, runtime: Runtime):
        self.cfg = cfg
        self.db = db
        self.runtime = runtime
        self.presets = Presets(cfg, db)
        self.tracker = Tracker(db)
        self.upstream_http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=1800.0, write=120.0,
                                                                     pool=30.0),
                                               limits=httpx.Limits(max_connections=200, max_keepalive_connections=20))
        for m in MODES:   # create every per-mode series at 0, so rate() works from the first request
            for h in (metrics.TTFT, metrics.GEN_TPS, metrics.QUEUE_WAIT, metrics.DURATION, metrics.LOAD_SECONDS):
                h.labels(m)
            metrics.REQUESTS.labels(m, "finished")
            for kind in ("prompt", "completion", "cached"):
                metrics.TOKENS.labels(m, kind)
        for reason in ("password", "totp", "totp_enrol"):   # so increase() sees the first failures too
            metrics.LOGIN_FAILURES.labels(reason)
        migrate_topic_file(db, cfg.data_dir)
        self.notifier = Notifier(db)
        self.modes = ModeManager(cfg, db, self.presets, runtime, self.tracker, notify=self.notify)
        self.embed = EmbedManager(cfg, db, runtime, self.modes, self.tracker)
        self.custom = CustomManager(cfg, db, self.presets, runtime, self.modes, notify=self.notify)
        self.bench = BenchManager(cfg, db, self.presets, runtime, self.modes, self.upstream_http, notify=self.notify)
        self.modes.switch_guard = self.bench.switch_block
        self.updates = UpdateChecker(cfg, db, notify=self.notify)
        self.modes.on_ready.append(self._after_load)
        self._vllm_cache: tuple[float, dict] = (0.0, {})
        self.auth = None
        self._tasks: list[asyncio.Task] = []
        self.gpu: dict[str, Any] = {}
        self.cpu: dict[str, Any] = {"cores": os.cpu_count()}
        self._cpu_last: tuple[int, int] | None = None
        self.disk_io: dict[str, Any] = {}
        self._disk_last: tuple[float, int, int] | None = None
        self._components: tuple[float, list[dict[str, Any]] | None] = (0.0, None)
        self.tls_status: dict[str, Any] = {}
        # live panel: one sample every 5 s for 15 minutes, independent of Prometheus/Grafana
        self.samples: collections.deque[dict[str, Any]] = collections.deque(maxlen=180)
        self._last_counts: tuple[float, float, float] | None = None
        self.chat_state: str | None = None     # chat-ui container: running, exited, ... (None: missing/unknown)
        self.down_grace_s = 60.0               # a stack container down this long is reported (a restart is quicker)
        self._down: dict[str, dict[str, Any]] = {}   # service -> {since, why, sent}
        self._seen: set[str] = set()                 # services seen on this install (a missing one is down)

    def notify(self, title: str, message: str, priority: str = "default") -> None:
        self.notifier.fire(title, message, priority)

    async def _after_load(self, mode: str, seconds: float) -> None:
        if mode == "custom":
            self._vllm_cache = (0.0, {})
            kv = (await self.vllm_stats()).get("kv_tokens")
            await self.custom.record_load(mode, seconds, kv)

    async def vllm_stats(self) -> dict[str, Any]:
        mode = self.modes.loaded
        if not mode or self.modes.state not in ("ready", "draining"):
            return {}
        now = time.time()
        if now - self._vllm_cache[0] < 2:
            return self._vllm_cache[1]
        out: dict[str, Any] = {}
        try:
            r = await self.upstream_http.get(self.modes.upstream(mode) + "/metrics", timeout=3)
            for fam in text_string_to_metric_families(r.text):
                for s in fam.samples:
                    key = VLLM_FIELDS.get(s.name)
                    if key:
                        out[key] = out.get(key, 0) + s.value
                    elif s.name == "vllm:cache_config_info":
                        # the KV cache size in tokens: with kv_usage, how much context the running requests hold
                        # (newer vLLM states it; blocks x block size overcounts on hybrid models like Flash)
                        try:
                            out["kv_tokens"] = int(s.labels.get("kv_cache_size_tokens") or
                                                   int(s.labels["num_gpu_blocks"]) * int(s.labels["block_size"]))
                        except (KeyError, ValueError):
                            pass
        except Exception as e:
            out = {"error": type(e).__name__}
        self._vllm_cache = (now, out)
        return out

    async def full_status(self) -> dict[str, Any]:
        st = self.modes.status()
        st["memory"] = meminfo()
        st["vllm"] = await self.vllm_stats()
        st["clients"] = {"active": self.tracker.active(self.db.get_setting("activity_window_s")),
                         "inflight": self.tracker.inflight_total}
        st["gpu"] = self.gpu
        st["cpu"] = self.cpu
        st["disk"] = {**disk_usage(self.cfg.data_dir), **self.disk_io}
        st["tls"] = self.tls_status
        st["embeddings"] = self.embed.status()
        if self.modes.loaded:
            st["settings"] = self.presets.settings(self.modes.loaded)
        st["time"] = time.time()
        return st

    async def components(self) -> list[dict[str, Any]] | None:
        """The install's containers, at most every 5 s (the overview polls every 2 s); None if Docker fails."""
        at, comps = self._components
        if time.time() - at > 5:
            try:
                comps = await asyncio.wait_for(self.runtime.components(), 5)
            except Exception as e:
                log.warning("container list: %s", type(e).__name__)
                comps = None
            self._components = (time.time(), comps)
        return comps

    async def _gpu_loop(self) -> None:
        exe = shutil.which("nvidia-smi")
        if not exe:
            log.info("nvidia-smi not found; GPU collector off")
            return
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    exe, "--query-gpu=utilization.gpu,temperature.gpu,power.draw", "--format=csv,noheader,nounits",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(proc.communicate(), 10)
                vals = [v.strip() for v in out.decode().splitlines()[0].split(",")]

                def num(v: str) -> float | None:
                    try:
                        return float(v)
                    except ValueError:
                        return None
                util, temp, power = (num(v) for v in vals[:3])
                self.gpu = {"util_pct": util, "temp_c": temp, "power_w": power}
                for g, v in ((metrics.GPU_UTIL, util), (metrics.GPU_TEMP, temp), (metrics.GPU_POWER, power)):
                    if v is not None:
                        g.set(v)
            except Exception as e:
                self.gpu = {"error": type(e).__name__}
            await asyncio.sleep(5)

    async def sample_once(self) -> dict[str, Any]:
        now = time.time()
        v = await self.vllm_stats() if self.modes.loaded else {}
        gen = prompt = None
        if "gen_tokens_total" in v:
            cur = (now, v["gen_tokens_total"], v.get("prompt_tokens_total", 0.0))
            if self._last_counts and cur[0] > self._last_counts[0] and cur[1] >= self._last_counts[1]:
                dt = cur[0] - self._last_counts[0]
                gen = (cur[1] - self._last_counts[1]) / dt
                prompt = (cur[2] - self._last_counts[2]) / dt
            self._last_counts = cur
        else:
            self._last_counts = None
        running = v.get("running")
        mem = meminfo()
        cur = cpu_times()
        if cur and self._cpu_last and cur[1] > self._cpu_last[1]:
            self.cpu = {"pct": round(100 * (cur[0] - self._cpu_last[0]) / (cur[1] - self._cpu_last[1]), 1),
                        "cores": os.cpu_count(), "load1": loadavg()}
        self._cpu_last = cur
        io = disk_bytes()
        if io and self._disk_last and now > self._disk_last[0]:
            dt = now - self._disk_last[0]
            self.disk_io = {"read_mbs": round((io[0] - self._disk_last[1]) / dt / 1e6, 1),
                            "write_mbs": round((io[1] - self._disk_last[2]) / dt / 1e6, 1)}
        self._disk_last = (now, *io) if io else None
        sample = {"t": now, "mode": self.modes.loaded, "state": self.modes.state,
                  "gen_tps": None if gen is None else round(gen, 1),
                  "prompt_tps": None if prompt is None else round(prompt, 1),
                  "per_stream_tps": round(gen / running, 1) if gen and running else None,
                  "running": running, "waiting": v.get("waiting"),
                  "kv_pct": None if v.get("kv_usage") is None else round(100 * v["kv_usage"], 1),
                  "mem_used_gib": mem.get("used_gib"), "gpu_temp_c": self.gpu.get("temp_c"),
                  "gpu_power_w": self.gpu.get("power_w"), "gpu_util_pct": self.gpu.get("util_pct"),
                  "cpu_pct": self.cpu.get("pct"), "disk_read_mbs": self.disk_io.get("read_mbs"),
                  "disk_write_mbs": self.disk_io.get("write_mbs")}
        self.samples.append(sample)
        return sample

    # the live panel's samples survive a hub restart (an update), so its graphs are not empty afterwards
    @property
    def _samples_file(self):
        return self.cfg.data_dir / "live-samples.json"

    def save_samples(self, rows: list | None = None) -> None:
        try:
            tmp = self._samples_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.samples) if rows is None else rows))
            tmp.replace(self._samples_file)
        except OSError as e:
            log.warning("saving live samples: %s", e)

    def load_samples(self) -> None:
        try:
            rows = json.loads(self._samples_file.read_text())
        except (OSError, ValueError):
            return
        cutoff = time.time() - 5 * (self.samples.maxlen or 180)
        self.samples.extend(r for r in rows if isinstance(r, dict) and isinstance(r.get("t"), (int, float))
                            and r["t"] > cutoff)

    async def _sampler(self) -> None:
        n = 0
        while True:
            try:
                await self.sample_once()
            except Exception:
                log.exception("sampler")
            n += 1
            if n % 12 == 0:                      # every minute, in case the hub is killed rather than stopped
                await asyncio.to_thread(self.save_samples, list(self.samples))   # copied here, not in the thread
            await asyncio.sleep(5)

    async def _housekeeping(self) -> None:
        """Hourly: prune old activity, check the TLS certificates; nightly (00-01 UTC): backup.
        A backup also runs if the last one is more than 30 hours old (e.g. the Spark was off at night)."""
        from . import maintenance
        last = maintenance.last_backup_time(self.cfg)
        if last:
            maintenance.BACKUP_LAST.set(last)
        while True:
            try:
                self.db.prune(int(self.db.get_setting("activity_retention_days")))
            except Exception:
                log.exception("prune")
            try:
                self.tls_status = await asyncio.to_thread(maintenance.check_certs, self.cfg)
            except Exception:
                log.exception("certificate check")
            try:
                last = maintenance.last_backup_time(self.cfg)
                age = time.time() - last if last else None
                hour = time.gmtime().tm_hour
                if age is None or age > 30 * 3600 or (hour == 0 and age > 20 * 3600):
                    await asyncio.to_thread(maintenance.run_backup, self.cfg, self.db)
            except Exception as e:
                log.exception("backup")
                self.db.event("backup_failed", None, error=f"{type(e).__name__}: {e}")
                self.notify("SparkHub: backup failed", f"{type(e).__name__}: {e}", "high")
            await asyncio.sleep(3600)

    # stack containers --------------------------------------------------------------------------------------------
    def check_stack(self, comps: list[dict[str, Any]], now: float | None = None) -> None:
        """Report a stack container that has been down (stopped, unhealthy, gone) for down_grace_s, and when it is
        back. Grafana sends the alert rules, so this is what notices Grafana or Prometheus being down."""
        now = now or time.time()
        bad: dict[str, str] = {}
        present = set()
        for c in comps:
            svc = c["service"]
            if c["model"] or c.get("embed") or svc not in WATCHED:
                continue
            present.add(svc)
            self._seen.add(svc)
            if svc == "chatui" and not self.chat_enabled():
                continue                         # turned off on the Settings page
            if c["state"] != "running":
                bad[svc] = c["status"] or c["state"]
            elif c["health"] == "unhealthy":
                bad[svc] = "unhealthy"
        for svc in self._seen - present:
            if svc != "chatui" or self.chat_enabled():
                bad[svc] = "container missing"
        for svc, why in bad.items():
            d = self._down.setdefault(svc, {"since": now, "sent": False})
            d["why"] = why
            if not d["sent"] and now - d["since"] >= self.down_grace_s:
                d["sent"] = True
                name = WATCHED[svc]
                log.warning("%s is down: %s", name, why)
                self.db.event("container_down", None, service=svc, status=why)
                extra = " Grafana alerts are not sent while it is down." if svc in ("grafana", "prometheus") else ""
                self.notify(f"SparkHub: {name} is down", f"{name} ({svc}): {why}.{extra}", "high")
        for svc in list(self._down):
            if svc not in bad:
                d = self._down.pop(svc)
                if d["sent"] and not (svc == "chatui" and not self.chat_enabled()):
                    name = WATCHED[svc]
                    log.info("%s is back", name)
                    self.db.event("container_back", None, service=svc)
                    self.notify(f"SparkHub: {name} is back", f"{name} is running again "
                                f"(down {round((now - d['since']) / 60)} min).")

    async def _stack_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                comps = await self.components()
                if comps is not None:            # Docker not answering is not a container being down
                    self.check_stack(comps)
            except Exception:
                log.exception("stack watcher")

    # chat-ui ---------------------------------------------------------------------------------------------------
    @property
    def chat_installed(self) -> bool:
        return bool(self.cfg.chat_container)

    def chat_enabled(self) -> bool:
        return self.chat_installed and bool(self.db.get_setting("chat_enabled"))

    async def apply_chat(self) -> str | None:
        """Bring chat-ui's container in line with the setting: running when on, stopped when off. Compose creates
        the container; the hub only starts and stops it. nginx refuses /chat/ while it is off either way."""
        if not self.chat_installed:
            return None
        name = self.cfg.chat_container
        st = await self.runtime.state(name)
        if st is None:
            self.chat_state = None
            return None
        want = self.chat_enabled()
        if want and not st["running"]:
            await self.runtime.start_container(name)
            log.info("chat-ui started (%s)", name)
        elif not want and st["running"]:
            await self.runtime.stop_container(name)
            log.info("chat-ui stopped (%s)", name)
        st = await self.runtime.state(name)
        self.chat_state = st["status"] if st else None
        return self.chat_state

    async def _chat_loop(self) -> None:
        # every minute: `docker compose up` (install, update) starts chat-ui even when it is turned off
        while True:
            try:
                await self.apply_chat()
            except Exception:
                log.exception("chat-ui")
            await asyncio.sleep(60)

    async def start(self) -> None:
        self.custom.load()          # Custom's spec before the mode manager adopts or loads anything
        await self.modes.startup()
        await self.custom.start()
        await self.bench.start()
        await self.updates.start()
        try:
            await self.embed.startup()
        except Exception:
            log.exception("embeddings startup")
        if self.chat_installed:
            self._tasks.append(asyncio.create_task(self._chat_loop()))
        self._tasks.append(asyncio.create_task(self._housekeeping()))
        self.load_samples()
        self._tasks.append(asyncio.create_task(self._sampler()))
        self._tasks.append(asyncio.create_task(self._stack_loop()))
        if self.cfg.gpu_collector:
            self._tasks.append(asyncio.create_task(self._gpu_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self.save_samples()
        await self.custom.stop()
        await self.bench.stop()
        await self.updates.stop()
        await self.modes.shutdown()
        await self.embed.shutdown()
        await self.upstream_http.aclose()
        await self.runtime.close()


def create_app(cfg: Config | None = None, runtime: Runtime | None = None, db: DB | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db = db or DB(cfg.db_path)
    runtime = runtime or DockerRuntime(cfg.docker_socket, cfg.helper_image, cfg.logs_dir, cfg.instance)
    hub = Hub(cfg, db, runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        yield
        await hub.stop()

    app = FastAPI(title="SparkHub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.db = db
    app.state.hub = hub

    from . import api, clients, gateway, mcp, oidc
    app.include_router(gateway.router)
    app.include_router(api.router)
    app.include_router(mcp.router)
    clients.install(app, cfg.clients_dir)
    from . import web
    web.install(app)
    oidc.install(app)

    @app.get("/metrics")
    async def prom(request: Request):
        # only for Prometheus on this host: never through nginx (which always sets X-Forwarded-For)
        if request.headers.get("x-forwarded-for") or (request.client and request.client.host not in
                                                       ("127.0.0.1", "::1")):
            return PlainTextResponse("not found", status_code=404)
        return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.post("/internal/grafana-alert")
    async def grafana_alert(request: Request):
        # Grafana's contact point (same host); the hub relays to ntfy with the current settings
        if request.headers.get("x-forwarded-for") or (request.client and request.client.host not in
                                                       ("127.0.0.1", "::1")):
            return PlainTextResponse("not found", status_code=404)
        body = await request.body()
        if len(body) > 1_000_000:
            return PlainTextResponse("too large", status_code=413)
        status = await hub.notifier.relay_grafana(body)
        return JSONResponse({"relayed": status is not None, "status": status})

    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return create_app()
