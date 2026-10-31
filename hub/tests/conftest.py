"""Test harness: the real hub app, served by uvicorn in a thread, with a fake runtime whose "containers" are
fake vLLM servers (tests/fake_vllm.py). No GPU, no Docker."""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from sparkhub.config import Config
from sparkhub.db import DB
from sparkhub.keys import create_key
from sparkhub.main import create_app
from sparkhub.runtime import Runtime

from .fake_vllm import FakeEmbedState, FakeState, make_app, make_embed_app

REPO = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeRuntime(Runtime):
    def __init__(self, presets_dir: Path):
        self.states: dict[str, FakeState] = {}
        self.servers: dict[str, tuple[uvicorn.Server | None, asyncio.Task | None]] = {}
        self.dead: dict[str, int] = {}
        self.fail: dict[str, int] = {}          # mode -> how many of the next starts fail
        self.load_delay = 0.2
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self.freed = 0
        self.apps: dict[str, bool] = {}         # other containers (chat-ui): name -> running
        self.pulled: list[str] = []
        self.fetched: list[list[dict]] = []
        self.fetch_result: dict | None = None   # what the next download helper run reports (default: all ok)
        self.fetch_delay = 0.0
        self.volumes_removed: list[str] = []
        self.ran_once: list[dict] = []
        self.run_once_result: dict | None = None
        self.bodies: dict[str, dict] = {}       # helper containers started from a full body (benchmark runner)
        self.bench_release = threading.Event()  # the fake runner finishes once this is set
        self.bench_result: dict | None = None   # what the fake runner writes to results.json
        self.labels: dict[str, dict] = {}       # model container -> labels (the preset pin)
        for m in ("fast", "quality"):
            raw = json.loads((presets_dir / f"{m}.json").read_text())
            self.states[m] = FakeState(raw["served_name"])
        embed = json.loads((presets_dir / "embed.json").read_text())
        for svc in ("embed", "rerank"):
            self.states[svc] = FakeEmbedState(embed["services"][svc]["served_name"], embed["dimensions"])

    async def start(self, spec):
        self.started.append(spec)
        mode, name = spec["mode"], spec["container"]
        self.labels[name] = {"sparkhub.pin": spec["pin"]} if spec.get("pin") else {}
        self.dead.pop(name, None)
        if self.fail.get(mode, 0) > 0:
            self.fail[mode] -= 1
            self.dead[name] = 1
            return
        if mode == "custom":       # the served name the hub put on the recipe's command line
            import re
            served = re.search(r"--served-model-name '?([^\s']+)", " ".join(spec["args"])).group(1)
            if "custom" not in self.states or self.states["custom"].served_name != served:
                self.states["custom"] = FakeState(served)
        app = make_embed_app(self.states[mode]) if mode in ("embed", "rerank") else make_app(self.states[mode])
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                                               port=spec["host_port"], log_level="warning", lifespan="off",
                                               timeout_graceful_shutdown=1))

        async def run():
            await asyncio.sleep(self.load_delay)
            await server.serve()
        self.servers[name] = (server, asyncio.create_task(run()))

    async def stop(self, mode, container):
        self.stopped.append(container)
        self.dead.pop(container, None)
        server, task = self.servers.pop(container, (None, None))
        if server:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 5)
            except Exception:
                task.cancel()

    def crash(self, container: str):
        """Simulate the model container dying."""
        server, task = self.servers.pop(container, (None, None))
        if server:
            server.should_exit = True
        self.dead[container] = 137

    async def start_container(self, container):
        self.apps[container] = True

    async def stop_container(self, container, timeout=20):
        self.apps[container] = False
        self.bench_release.set()

    async def start_container_body(self, name, body):
        """The benchmark runner: writes progress, waits for bench_release, writes results, exits."""
        self.bodies[name] = body
        self.apps[name] = True
        env = dict(e.split("=", 1) for e in body["Env"])
        cfg = json.loads(env["BENCH_CONFIG"])
        out = Path(cfg["out"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "progress.json").write_text(json.dumps({"suite": cfg["suites"][0], "n_done": 1, "n_total": 15,
                                                      "done": [], "detail": "1 solved so far"}))

        async def finish():
            while not self.bench_release.is_set() and self.apps.get(name):
                await asyncio.sleep(0.05)
            res = self.bench_result or {"runner_version": "1", "suites": {s: {
                "solved": 9, "total": 15, "n_tasks": 15, "seconds": 5,
                "tasks": {"nginx-request-logging": {"reward": 1.0, "agent_s": 30, "error": None}}}
                if s.startswith("tb2") else {"mean_accuracy": 0.8, "seconds": 5, "tasks": {}} for s in cfg["suites"]}}
            (out / "results.json").write_text(json.dumps(res))
            self.apps[name] = False
        asyncio.create_task(finish())

    async def remove_container(self, name):
        self.apps.pop(name, None)

    async def state(self, container):
        if container in self.apps:
            on = self.apps[container]
            return {"status": "running" if on else "exited", "running": on, "exit_code": 0, "oom_killed": False}
        if container in self.dead:
            return {"status": "exited", "running": False, "exit_code": self.dead[container], "oom_killed": False}
        if container in self.servers:
            return {"status": "running", "running": True, "exit_code": 0, "oom_killed": False}
        return None

    async def logs(self, mode, container, tail=300):
        return f"fake logs for {container}"

    async def free_memory(self):
        self.freed += 1
        return {}

    async def running(self):
        out = {}
        for name in list(self.servers) + list(self.dead):
            if name in self.servers and name.endswith(("-fast", "-quality", "-custom")):   # model label only
                out[name.rsplit("-", 1)[1]] = name
        return out

    async def inspect(self, container):
        if container in self.servers:
            return {"Config": {"Labels": self.labels.get(container, {})}}
        return None

    async def pull_image(self, ref, progress=None):
        self.pulled.append(ref)
        if progress:
            progress(5, 10)

    async def image_digests(self, ref):
        return [ref]

    async def run_fetch(self, files, hf_cache, progress=None):
        self.fetched.append(files)
        if progress:
            progress(1, 2, files[0]["path"] if files else "")
        await asyncio.sleep(self.fetch_delay)
        return self.fetch_result or {"ok": True, "verified": len(files), "downloaded": len(files), "present": 0}

    async def remove_volume(self, name):
        self.volumes_removed.append(name)

    async def run_once(self, spec, timeout=900):
        self.ran_once.append(spec)
        return self.run_once_result or {"exit_code": 0, "log": "[sparkhub] mods applied"}

    async def close(self):
        for name in list(self.servers):
            await self.stop("", name)


class HubServer:
    def __init__(self, tmp: Path, settings: dict | None = None, apps: dict | None = None, **cfg):
        presets = tmp / "presets"
        shutil.copytree(REPO / "presets", presets)
        for m in ("fast", "quality"):
            p = presets / f"{m}.json"
            raw = json.loads(p.read_text())
            raw["host_port"] = free_port()
            p.write_text(json.dumps(raw))
        p = presets / "embed.json"
        raw = json.loads(p.read_text())
        for svc in raw["services"].values():
            svc["host_port"] = free_port()
        p.write_text(json.dumps(raw))
        cfg.setdefault("embeddings", True)
        self.cfg = Config(data_dir=tmp / "data", presets_dir=presets, runtime="fake", gpu_collector=False,
                          cookie_secure=False, clients_dir=REPO / "clients", public_host="hub.example",
                          tls_names="hub.example,hub.tailnet.example", **cfg)
        self.db = DB(self.cfg.db_path)
        base = {"retry_delay_s": 0, "drain_quiet_s": 1.5, "drain_timeout_s": 20, "activity_window_s": 3,
                "load_timeout_s": 15, "queue_timeout_s": 10}
        base.update(settings or {})
        for k, v in base.items():
            self.db.set_setting(k, v)
        self.runtime = FakeRuntime(presets)
        self.runtime.apps.update(apps or {})
        self.app = create_app(self.cfg, runtime=self.runtime, db=self.db)
        hub = self.app.state.hub
        hub.modes.poll_s = 0.1
        hub.modes.monitor_s = 0.3
        hub.embed.loop_s = 0.2
        hub.embed.health_poll_s = 0.05
        self.hub = hub
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                                                    timeout_graceful_shutdown=1))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("hub did not start")
            time.sleep(0.02)
        self.admin_key = create_key(self.db, "admin", "admin", "test")[1]
        self.infer_key = create_key(self.db, "agent", "infer", "test")[1]
        self.switch_key = create_key(self.db, "harness", "switch", "test")[1]

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self.hub.modes.task.get_loop() if self.hub.modes.task else None

    def client(self, key: str | None = None, **kw) -> httpx.Client:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return httpx.Client(base_url=self.url, headers=headers, timeout=30, **kw)

    def wait_state(self, state: str, mode: str | None = None, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        st = {}
        while time.time() < deadline:
            st = self.hub.modes.status()
            if st["state"] == state and (mode is None or st["mode"] == mode) and not self.hub.modes.busy:
                return st
            time.sleep(0.05)
        raise AssertionError(f"state {state}/{mode} not reached; last {st}")

    def close(self):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def hub(tmp_path):
    h = HubServer(tmp_path)
    h.wait_state("ready", "fast")
    yield h
    h.close()


@pytest.fixture
def make_hub(tmp_path):
    made = []

    def make(settings=None, sub="h", **kw):
        h = HubServer(tmp_path / sub, settings, **kw)
        made.append(h)
        return h
    yield make
    for h in made:
        h.close()
