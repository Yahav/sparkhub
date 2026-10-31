"""Benchmarks of the loaded model, from the dashboard.

An admin starts a run now or schedules it. At its start the hub loads the run's mode if another one is loaded
(the one place the hub switches by itself), then blocks: switching is refused and the gateway
answers every other key with 503 until the run ends. Runs never overlap.

Suites:
  speed       measured here: streaming requests straight to the model (time to first token, tokens per second at
              1, 2 and 4 requests at once, 2k and 16k token prompts, one real prompt)
  tb2-quick   Terminal-Bench 2.0, 15 tasks; tb2-full: all 53 that run on arm64 (Harbor + opencode)
  s2          knowledge: MMLU-Pro CS and engineering, MMLU security and college CS, IFEval (lm-eval)
  s6          reasoning: BBH; s6-nothink: the same with thinking off
The last four run in the sparkhub-bench container (bench/runner.py), which reaches the model through the gateway
with a key made for the run and revoked after it. No prompt or response is stored in the database; the runner's
logs and samples stay in data/bench/runs/<id> for the admin and are deleted after a month.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import shutil
import statistics
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import keys as keymod
from .db import DB
from .keys import Principal
from .lifecycle import SwitchRefused

log = logging.getLogger("sparkhub.bench")

BENCH_VERSION = "1"            # bump when a suite's settings change, so old and new results aren't compared
SUITES: dict[str, dict[str, Any]] = {
    "speed": {"label": "Speed", "est_s": 15 * 60},
    "tb2-quick": {"label": "Terminal-Bench, 15 tasks", "est_s": 45 * 60},
    "tb2-full": {"label": "Terminal-Bench, all 53 tasks", "est_s": 5 * 3600},
    "s2": {"label": "Knowledge (MMLU-Pro, MMLU, IFEval)", "est_s": 3 * 3600},
    "s6": {"label": "Reasoning (BBH)", "est_s": 2 * 3600},
    "s6-nothink": {"label": "Reasoning (BBH), thinking off", "est_s": 3600},
}
ALWAYS = ("speed", "tb2-quick")
OPTIONAL = ("tb2-full", "s2", "s6", "s6-nothink")
RUNNER_SUITES = ("tb2-quick", "tb2-full", "s2", "s6", "s6-nothink")
TB2_QUICK = ("nginx-request-logging", "openssl-selfsigned-cert", "fix-code-vulnerability", "pypi-server",
             "vulnerable-secret", "configure-git-webserver", "cobol-modernization", "kv-store-grpc", "git-multibranch",
             "sqlite-with-gcov", "sparql-university", "query-optimize", "db-wal-recovery", "sqlite-db-truncate",
             "cancel-async-tasks")          # = bench/tb2-quick.txt; a full run's results give these too
# Without "internet" the agents reach the hub, package registries (tasks install their tools, e.g. nginx) and
# models.dev (opencode's model list), but no general web: no GitHub, no looking up solutions
PACKAGE_HOSTS = ("deb.debian.org", "security.debian.org", "archive.ubuntu.com", "security.ubuntu.com",
                 "ports.ubuntu.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "proxy.golang.org",
                 "sum.golang.org", "index.crates.io", "static.crates.io", "crates.io", "repo.maven.apache.org",
                 "repo1.maven.org", "models.dev")
MODE_FACTOR = {"fast": 1.0, "quality": 1.3, "custom": 1.5}   # default estimates; measured times replace them
ACTIVE = ("starting", "running")
KEEP_DAYS = 30
SWITCH_WAIT_S = 30 * 60

# speed suite: (label, concurrency, prompt tokens, requests), output 500 tokens each (ignore_eos), as the original benchmark
SPEED_CASES = (("c1-2k", 1, 2000, 4), ("c2-2k", 2, 2000, 8), ("c4-2k", 4, 2000, 16),
               ("c1-16k", 1, 16000, 3), ("c4-16k", 4, 16000, 8))
SPEED_OUTPUT = 500
REAL_PROMPT = ("Write a detailed, well-structured technical explanation (about 700 words) of how a write-ahead log "
               "works in a database, including crash recovery and checkpoints. Then give a short Python example "
               "that appends records to a log file and replays it.")
WORDS = ("system network server process memory kernel thread socket buffer packet route table index query "
         "column schema backup restore volume mount service daemon config log error retry timeout cache "
         "cluster replica shard lock queue worker job task build deploy release branch commit merge patch "
         "test suite assert value number string array object field record file path user group role key "
         "token secret policy rule audit event metric alert disk node pod image layer port host address").split()


class BenchError(Exception):
    pass


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if time.time() + 86400 > ts else \
        time.strftime("%a %H:%M", time.localtime(ts))


class BenchManager:
    def __init__(self, cfg, db: DB, presets, runtime, modes, http: httpx.AsyncClient,
                 notify: Callable[[str, str, str], None] | None = None):
        self.cfg, self.db, self.presets, self.runtime, self.modes, self.http = cfg, db, presets, runtime, modes, http
        self.notify = notify or (lambda *a: None)
        self._task: asyncio.Task | None = None
        self._loop: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._cancel: set[int] = set()
        self.principal = Principal("key", -1, "benchmark", "admin")    # the hub's own switch for a run

    # ------------------------------------------------------------------ state
    @property
    def installed(self) -> bool:
        return bool(self.cfg.bench)

    @property
    def runs_dir(self) -> Path:
        return self.cfg.data_dir / "bench" / "runs"

    def host_path(self, *parts: str) -> str:
        """A path under data/ as the host sees it (bind mounts for the runner and Harbor's task containers)."""
        return str(Path(self.cfg.host_data_dir or self.cfg.data_dir, *parts))

    def row(self, rid: int) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM bench_runs WHERE id=?", (rid,))
        if r is None:
            return None
        d = dict(r)
        for k in ("suites", "progress", "results"):
            d[k] = json.loads(d[k]) if d[k] else ({} if k != "suites" else [])
        d["ends_at"] = (d["started_at"] or d["start_at"]) + d["est_s"]
        return d

    def rows(self, limit: int = 100) -> list[dict[str, Any]]:
        return [self.row(r["id"]) for r in self.db.all("SELECT id FROM bench_runs ORDER BY start_at DESC, id DESC "
                                                       "LIMIT ?", (limit,))]

    def active(self) -> dict[str, Any] | None:
        r = self.db.one(f"SELECT id FROM bench_runs WHERE status IN {ACTIVE} ORDER BY id LIMIT 1")
        return self.row(r["id"]) if r else None

    def upcoming(self) -> list[dict[str, Any]]:
        return [self.row(r["id"]) for r in self.db.all(
            "SELECT id FROM bench_runs WHERE status='scheduled' ORDER BY start_at")]

    def blocks(self, key_id: int | None) -> str | None:
        """For the gateway: why a request from this key is refused now (None: it may go on)."""
        a = self.active()
        if a and a["status"] == "running" and key_id != a["key_id"]:
            return (f"a benchmark of the {a['mode']} model is running until about {_hhmm(a['ends_at'])}; "
                    f"the hub takes no other requests until it ends")
        return None

    def switch_block(self, who: Principal) -> str | None:
        """For the mode manager: switching is refused while a run is starting or running (except its own)."""
        a = self.active()
        if a and who is not self.principal:
            return (f"benchmark #{a['id']} is running until about {_hhmm(a['ends_at'])}; cancel it on the "
                    f"Benchmarks page first")
        return None

    # ------------------------------------------------------------------ choosing what to run
    def available_modes(self) -> list[str]:
        return [m for m in ("fast", "quality", "custom") if m in self.presets]

    def fingerprint(self, mode: str) -> tuple[str, str, int | None]:
        """(fingerprint, label, recipe id) of what `mode` serves now: same fingerprint = same model and settings."""
        p = self.presets[mode]
        rid = p.raw.get("recipe_id") if mode == "custom" else None
        blob = json.dumps({"image": p.raw.get("image"), "args": p.raw.get("args"), "env": p.raw.get("env"),
                           "settings": self.presets.settings(mode), "recipe": rid}, sort_keys=True)
        label = f"{p.title} ({p.served_name})"
        return f"{mode}-{hashlib.sha256(blob.encode()).hexdigest()[:12]}", label, rid

    def normalize(self, suites: list[str]) -> list[str]:
        out = list(ALWAYS) + [s for s in OPTIONAL if s in suites]
        if "tb2-full" in out:            # the full set includes the 15 quick tasks
            out.remove("tb2-quick")
        return out

    def estimate(self, mode: str, suites: list[str]) -> float:
        """Seconds: each suite's last measured time on this mode, else the default scaled for the mode."""
        total = 0.0
        last = {}
        for r in self.db.all("SELECT results FROM bench_runs WHERE mode=? AND status='done' ORDER BY id DESC "
                             "LIMIT 10", (mode,)):
            for s, res in (json.loads(r["results"] or "{}").get("suites") or {}).items():
                if isinstance(res, dict) and res.get("seconds") and not res.get("error"):
                    last.setdefault(s, res["seconds"])
        for s in suites:
            total += last[s] * 1.1 if s in last else SUITES[s]["est_s"] * MODE_FACTOR.get(mode, 1.5)
        return total + 10 * 60          # loading the model, starting the runner

    # ------------------------------------------------------------------ actions
    def schedule(self, mode: str, suites: list[str], internet: bool, start_at: float | None, actor: str) -> int:
        if not self.installed:
            raise BenchError("benchmarks are not installed on this hub (BENCH=1 in sparkhub.conf, then ./install.sh)")
        if mode not in self.available_modes():
            raise BenchError(f"{mode} has no model set up")
        suites = self.normalize(suites)
        now = time.time()
        start = max(start_at or now, now)
        if start_at and start_at < now - 60:
            raise BenchError("that start time has passed")
        est = self.estimate(mode, suites)
        for o in self.db.all(f"SELECT id FROM bench_runs WHERE status IN ('scheduled', {', '.join(repr(s) for s in ACTIVE)})"):
            other = self.row(o["id"])
            o_start = other["started_at"] or other["start_at"]
            if start < other["ends_at"] and o_start < start + est:
                raise BenchError(f"it would overlap benchmark #{other['id']} ({other['mode']}, "
                                 f"{_hhmm(o_start)}–{_hhmm(other['ends_at'])}); pick a time after it ends")
        fp, label, rid = self.fingerprint(mode)
        cur = self.db.execute(
            "INSERT INTO bench_runs(status, mode, recipe_id, model_label, fingerprint, suites, internet, start_at, "
            "est_s, created_at, created_by, bench_version) VALUES('scheduled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, rid, label, fp, json.dumps(suites), int(internet), start, est, now, actor, BENCH_VERSION))
        self.db.event("bench_scheduled", actor, id=cur.lastrowid, mode=mode, suites=suites, internet=internet,
                      start_at=start)
        self._wake.set()
        return cur.lastrowid

    async def cancel(self, rid: int, actor: str) -> None:
        r = self.row(rid)
        if r is None or r["status"] not in ("scheduled", *ACTIVE):
            raise BenchError("that run is not scheduled or running")
        self.db.event("bench_cancel", actor, id=rid)
        if r["status"] == "scheduled":
            self._finish(rid, "cancelled", error=f"cancelled by {actor.removeprefix('user:')} before it started")
            return
        self._cancel.add(rid)
        await self.runtime.stop_container(self.runner_name, timeout=120)

    @property
    def runner_name(self) -> str:
        return f"{self.cfg.instance}-bench"

    def _set(self, rid: int, **kw: Any) -> None:
        for k in ("progress", "results"):
            if k in kw:
                kw[k] = json.dumps(kw[k])
        sets = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE bench_runs SET {sets} WHERE id=?", (*kw.values(), rid))

    def _finish(self, rid: int, status: str, error: str | None = None, results: dict | None = None) -> None:
        kw: dict[str, Any] = {"status": status, "finished_at": time.time(), "error": error}
        if results is not None:
            kw["results"] = results
        self._set(rid, **kw)
        r = self.row(rid)
        if r and r["key_id"]:
            keymod.revoke_key(self.db, r["key_id"], "benchmark")
        self.db.event("bench_" + status, "benchmark", id=rid, error=error)

    # ------------------------------------------------------------------ the loop
    async def start(self) -> None:
        for r in self.db.all(f"SELECT id FROM bench_runs WHERE status IN {ACTIVE}"):
            self._finish(r["id"], "failed", error="interrupted: the hub restarted during the run")
        if self.installed:
            try:
                await self.runtime.remove_container(self.runner_name)
            except Exception:
                log.exception("removing a leftover benchmark runner")
        self._loop = asyncio.create_task(self._scheduler())

    async def stop(self) -> None:
        for t in (self._loop, self._task):
            if t:
                t.cancel()

    async def _scheduler(self) -> None:
        last_prune = 0.0
        while True:
            try:
                if (self._task is None or self._task.done()) and not self.active():
                    due = self.db.one("SELECT id FROM bench_runs WHERE status='scheduled' AND start_at<=? "
                                      "ORDER BY start_at LIMIT 1", (time.time(),))
                    if due:
                        self._task = asyncio.create_task(self._execute(due["id"]))
                if time.time() - last_prune > 86400:
                    last_prune = time.time()
                    self.prune()
            except Exception:
                log.exception("benchmark scheduler")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    def prune(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        for r in self.db.all("SELECT id FROM bench_runs WHERE finished_at < ?", (cutoff,)):
            shutil.rmtree(self.runs_dir / str(r["id"]), ignore_errors=True)

    async def _ensure_loaded(self, r: dict[str, Any]) -> None:
        mode = r["mode"]
        if mode == "custom" and (mode not in self.presets or self.presets[mode].raw.get("recipe_id") != r["recipe_id"]):
            raise BenchError("Custom has a different recipe assigned than when the run was scheduled")
        deadline = time.time() + SWITCH_WAIT_S
        while self.modes.busy and time.time() < deadline:          # a switch someone started: let it finish
            await asyncio.sleep(5)
        if not (self.modes.loaded == mode and self.modes.state == "ready"):
            log.info("benchmark #%s: loading %s", r["id"], mode)
            try:
                self.modes.request_switch(mode, self.principal, force=True)
            except SwitchRefused as e:
                raise BenchError(f"could not load {mode}: {e.detail.get('error')}")
            await asyncio.sleep(1)
            while self.modes.busy and time.time() < deadline:
                if r["id"] in self._cancel:
                    raise asyncio.CancelledError
                await asyncio.sleep(5)
        if not (self.modes.loaded == mode and self.modes.state == "ready"):
            raise BenchError(f"{mode} did not load ({self.modes.state}: {self.modes.message})")

    async def _execute(self, rid: int) -> None:
        r = self.row(rid)
        self._set(rid, status="starting", started_at=time.time())
        results: dict[str, Any] = {"suites": {}}
        try:
            await self._ensure_loaded(r)
            fp, label, _ = self.fingerprint(r["mode"])
            key_id, raw = keymod.create_key(self.db, f"benchmark #{rid}", "infer", "benchmark", expires_days=3,
                                            max_concurrency=8)
            self._set(rid, status="running", key_id=key_id, fingerprint=fp, model_label=label)
            self.db.event("bench_started", "benchmark", id=rid, mode=r["mode"], fingerprint=fp)
            if "speed" in r["suites"]:
                self._set(rid, progress={"suite": "speed", "detail": "starting"})
                t0 = time.time()
                results["suites"]["speed"] = await self.speed(r["mode"], rid)
                results["suites"]["speed"]["seconds"] = round(time.time() - t0)
                self._set(rid, results=results)
            rest = [s for s in r["suites"] if s in RUNNER_SUITES]
            if rest and rid not in self._cancel:
                results["suites"].update(await self._run_runner(rid, r, rest, raw))
            cancelled = rid in self._cancel
            failed = [s for s, v in results["suites"].items() if isinstance(v, dict) and v.get("error")]
            status = "cancelled" if cancelled else ("failed" if failed and len(failed) == len(r["suites"]) else "done")
            err = "cancelled" if cancelled else ("; ".join(f"{s}: {results['suites'][s]['error']}" for s in failed)
                                                  or None)
            self._finish(rid, status, error=err, results=results)
            if not cancelled:
                self.notify(f"Benchmark #{rid} {'finished' if status == 'done' else 'failed'}",
                            f"{r['mode']}: {self.summary_line(results)}" + (f" ({err})" if err else ""),
                            "default" if status == "done" else "high")
        except asyncio.CancelledError:
            self._finish(rid, "cancelled", error="cancelled", results=results)
        except Exception as e:
            log.exception("benchmark #%s", rid)
            self._finish(rid, "failed", error=str(e)[:500], results=results)
            self.notify(f"Benchmark #{rid} failed", f"{r['mode']}: {str(e)[:200]}", "high")
        finally:
            self._cancel.discard(rid)
            self._wake.set()

    # ------------------------------------------------------------------ runner container
    async def _run_runner(self, rid: int, r: dict[str, Any], suites: list[str], api_key: str) -> dict[str, Any]:
        out_host = self.host_path("bench", "runs", str(rid))
        out_here = self.runs_dir / str(rid)
        out_here.mkdir(parents=True, exist_ok=True)
        settings = self.presets.settings(r["mode"])
        port = "" if self.cfg.tls_port == 443 else f":{self.cfg.tls_port}"
        gw = self.cfg.gateway_addr
        context = int(settings.get("max_model_len") or 131072)
        conf = {"out": out_host, "suites": suites, "model": r["mode"], "base_url": f"https://{gw}{port}/v1",
                "ca": "/bench-ca/ca.crt", "internet": bool(r["internet"]),
                "allowed_hosts": [gw, *PACKAGE_HOSTS],
                "tb2_dir": self.host_path("bench", "tb2"), "cache_dir": self.host_path("bench", "cache", r["fingerprint"] or r["mode"]),
                "context": context, "max_output": min(32000, context // 4),
                "n_concurrent": min(4, int(settings.get("max_num_seqs") or 4))}
        bench = self.host_path("bench")
        body = {"Image": self.cfg.bench_image, "Cmd": ["run"],
                # the hub's user and its docker group: files in data/bench stay the hub's, Harbor reaches Docker
                "User": f"{os.getuid()}:{os.getgid()}",
                "Env": [f"BENCH_CONFIG={json.dumps(conf)}", f"OPENAI_API_KEY={api_key}", "HOME=/tmp",
                        f"HF_HOME={bench}/hf", f"NLTK_DATA={bench}/nltk"],
                "Labels": {"sparkhub.helper": "bench", "sparkhub.bench-run": str(rid)},
                "HostConfig": {"GroupAdd": [str(g) for g in os.getgroups()],     # only honoured here, not top-level
                               "Binds": [f"{self.cfg.docker_socket_host}:/var/run/docker.sock", f"{bench}:{bench}",
                                         self.host_path("tls", "ca.crt") + ":/bench-ca/ca.crt:ro"],
                               "RestartPolicy": {"Name": "no"}}}
        name = self.runner_name
        await self.runtime.start_container_body(name, body)
        try:
            while True:
                st = await self.runtime.state(name)
                prog = _read_json(out_here / "progress.json")
                if prog:
                    self._set(rid, progress=prog)
                if not st or not st["running"]:
                    break
                await asyncio.sleep(10)
            res = _read_json(out_here / "results.json") or {}
            suites_res = res.get("suites") or {}
            if not suites_res:
                tail = (await self.runtime.logs("bench", name, tail=20)).strip().splitlines()[-3:]
                raise BenchError(f"the benchmark runner exited ({st and st['exit_code']}) without results: "
                                 f"{' | '.join(tail)[:300]}")
            for v in suites_res.values():
                if isinstance(v, dict):
                    v["runner_version"] = res.get("runner_version")
            return suites_res
        finally:
            await self.runtime.remove_container(name)

    # ------------------------------------------------------------------ speed
    async def speed(self, mode: str, rid: int) -> dict[str, Any]:
        base, served = self.modes.upstream(mode), self.presets[mode].served_name
        per_word = await self._tokens_per_word(base, served)
        out: dict[str, Any] = {"cases": [], "output_tokens": SPEED_OUTPUT}
        await self._speed_case(base, served, 1, 512, 2, per_word, seed=0, out_tokens=64)      # warm-up
        for i, (label, conc, prompt_tokens, n) in enumerate(SPEED_CASES):
            if rid in self._cancel:
                raise asyncio.CancelledError
            self._set(rid, progress={"suite": "speed", "n_done": i, "n_total": len(SPEED_CASES) + 1, "detail": label})
            case = await self._speed_case(base, served, conc, prompt_tokens, n, per_word, seed=(i + 1) * 1000)
            out["cases"].append({"label": label, "concurrency": conc, "prompt_tokens": prompt_tokens, **case})
        self._set(rid, progress={"suite": "speed", "n_done": len(SPEED_CASES), "n_total": len(SPEED_CASES) + 1,
                                 "detail": "real prompt"})
        real = await self._one(base, served, REAL_PROMPT, 1200, ignore_eos=False, temperature=0.2)
        out["real_prompt"] = {"completion_tokens": real["tokens"], "seconds": round(real["total_s"], 2),
                              "tok_per_s_incl_prefill": round(real["tokens"] / real["total_s"], 1) if real["total_s"] else None,
                              "decode_tps": real["decode_tps"]}
        c1 = next(c for c in out["cases"] if c["label"] == "c1-2k")
        c4 = next(c for c in out["cases"] if c["label"] == "c4-2k")
        out["headline"] = {"single_tps": c1["per_stream_tps"], "per_stream_tps_4": c4["per_stream_tps"],
                           "total_tps_4": c4["output_tps"], "ttft_2k_s": c1["ttft_p50_s"],
                           "ttft_16k_s": next(c for c in out["cases"] if c["label"] == "c1-16k")["ttft_p50_s"]}
        return out

    async def _tokens_per_word(self, base: str, served: str) -> float:
        text = " ".join(_words(random.Random(7), 1000))
        try:
            r = await self.http.post(base + "/tokenize", json={"model": served, "prompt": text}, timeout=60)
            r.raise_for_status()
            n = r.json().get("count") or len(r.json().get("tokens") or [])
            if n:
                return n / 1000
        except (httpx.HTTPError, ValueError) as e:
            log.warning("tokenize failed (%s); assuming 1.3 tokens per word", e)
        return 1.3

    async def _speed_case(self, base: str, served: str, conc: int, prompt_tokens: int, n: int, per_word: float,
                          seed: int, out_tokens: int = SPEED_OUTPUT) -> dict[str, Any]:
        sem = asyncio.Semaphore(conc)
        words = max(10, int(prompt_tokens / per_word))

        async def one(i: int) -> dict[str, Any]:
            async with sem:
                # every prompt different, so the prefix cache can't shorten the prefill
                text = " ".join(_words(random.Random(seed + i), words))
                return await self._one(base, served, text, out_tokens)

        t0 = time.time()
        res = await asyncio.gather(*(one(i) for i in range(n)))
        wall = time.time() - t0
        ttfts = sorted(x["ttft_s"] for x in res if x["ttft_s"] is not None)
        decode = [x["decode_tps"] for x in res if x["decode_tps"]]
        return {"requests": n, "wall_s": round(wall, 2),
                "output_tps": round(sum(x["tokens"] for x in res) / wall, 1) if wall else None,
                "per_stream_tps": round(statistics.mean(decode), 1) if decode else None,
                "ttft_p50_s": round(statistics.median(ttfts), 3) if ttfts else None,
                "ttft_p95_s": round(ttfts[min(len(ttfts) - 1, int(0.95 * len(ttfts)))], 3) if ttfts else None}

    async def _one(self, base: str, served: str, text: str, max_tokens: int, ignore_eos: bool = True,
                   temperature: float = 0.0) -> dict[str, Any]:
        body = {"model": served, "messages": [{"role": "user", "content": text}], "max_tokens": max_tokens,
                "temperature": temperature, "stream": True, "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False}}
        if ignore_eos:
            body["ignore_eos"] = True
        t0 = time.time()
        first = None
        tokens = 0
        async with self.http.stream("POST", base + "/v1/chat/completions", json=body,
                                    timeout=httpx.Timeout(connect=10, read=900, write=60, pool=60)) as r:
            if r.status_code != 200:
                raise BenchError(f"the model answered {r.status_code}: {(await r.aread())[:200]!r}")
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    ev = json.loads(line[6:])
                except ValueError:
                    continue
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if first is None and (d.get("content") or d.get("reasoning_content") or d.get("reasoning")):
                        first = time.time()
                if ev.get("usage"):
                    tokens = ev["usage"].get("completion_tokens") or tokens
        end = time.time()
        decode = (tokens - 1) / (end - first) if first and tokens > 1 and end > first else None
        return {"ttft_s": first - t0 if first else None, "tokens": tokens, "total_s": end - t0,
                "decode_tps": round(decode, 1) if decode else None}

    # ------------------------------------------------------------------ results
    def headline(self, results: dict[str, Any]) -> dict[str, Any]:
        """The numbers the Compare table shows, from one run's results."""
        s = results.get("suites") or {}
        h: dict[str, Any] = {}
        sp = (s.get("speed") or {}).get("headline") or {}
        h.update(sp)
        full = s.get("tb2-full") or {}
        quick = s.get("tb2-quick") or {}
        if full.get("tasks"):
            h["tb2_full"] = (full["solved"], full["n_tasks"])
            sub = [full["tasks"].get(t, {}).get("reward") for t in TB2_QUICK]
            h["tb2_quick"] = (sum(1 for x in sub if x == 1.0), len(TB2_QUICK))
        elif quick.get("tasks"):
            h["tb2_quick"] = (quick["solved"], quick["n_tasks"])
        for k in ("s2", "s6", "s6-nothink"):
            if (s.get(k) or {}).get("mean_accuracy") is not None:
                h[k] = s[k]["mean_accuracy"]
        return h

    def summary_line(self, results: dict[str, Any]) -> str:
        h = self.headline(results)
        parts = []
        if h.get("single_tps"):
            parts.append(f"{h['single_tps']} tok/s single, {h.get('per_stream_tps_4')} per stream at 4")
        if h.get("tb2_quick"):
            parts.append(f"Terminal-Bench {h['tb2_quick'][0]}/{h['tb2_quick'][1]}")
        if h.get("tb2_full"):
            parts.append(f"full {h['tb2_full'][0]}/{h['tb2_full'][1]}")
        for k, name in (("s2", "knowledge"), ("s6", "BBH"), ("s6-nothink", "BBH no-think")):
            if h.get(k) is not None:
                parts.append(f"{name} {h[k] * 100:.1f}%")
        return ", ".join(parts) or "no results"

    def compare(self) -> list[dict[str, Any]]:
        """The newest finished run of each model (fingerprint) per internet setting, current bench version."""
        seen: set[tuple[str, int]] = set()
        out = []
        for r in self.db.all("SELECT id FROM bench_runs WHERE status='done' AND bench_version=? ORDER BY id DESC",
                             (BENCH_VERSION,)):
            d = self.row(r["id"])
            k = (d["fingerprint"], d["internet"])
            if k in seen:
                continue
            seen.add(k)
            d["headline"] = self.headline(d["results"])
            out.append(d)
        return out


def _words(rng: random.Random, n: int) -> list[str]:
    return [rng.choice(WORDS) for _ in range(n)]


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None
