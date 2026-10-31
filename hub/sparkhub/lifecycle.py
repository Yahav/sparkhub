"""The mode manager: which model is loaded, and switching between them.

SparkHub never swaps on its own. A switch is refused while other clients are active unless the caller
asks to drain (wait for quiet) or, with admin scope, to force. A failed load is retried once; if it fails again
the previous mode is loaded back. A load can be cancelled, which also brings the previous mode back.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from . import metrics
from .activity import Tracker
from .config import Config
from .db import DB
from .keys import Principal
from .presets import BUILTIN, MODES, Presets
from .runtime import PIN_LABEL, Runtime, meminfo

log = logging.getLogger("sparkhub.lifecycle")

STATES = ("idle", "loading", "ready", "draining", "stopping", "failed")


WATCHDOG_WINDOW_S = 30 * 60   # a second crash within this window after a watchdog restart is not restarted


class SwitchRefused(Exception):
    def __init__(self, status: int, detail: dict[str, Any]):
        super().__init__(detail.get("error"))
        self.status = status
        self.detail = detail


class _Aborted(Exception):
    pass


class ModeManager:
    def __init__(self, cfg: Config, db: DB, presets: Presets, runtime: Runtime, tracker: Tracker,
                 notify=None):
        self.cfg = cfg
        self.db = db
        self.presets = presets
        self.runtime = runtime
        self.tracker = tracker
        self.notify = notify or (lambda *a, **k: None)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=180.0))
        self.loaded: str | None = None
        self.loaded_since: float | None = None
        self.state = "idle"
        self.message = "starting up"
        self.target: str | None = None
        self.previous: str | None = None
        self.switch_started: float | None = None
        self.phase_started: float | None = None
        self.attempt = 0
        self.error: str | None = None
        self.last_switch: dict[str, Any] | None = None
        self.drain_allowed: set[int] | None = None
        self.task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._monitor: asyncio.Task | None = None
        self.poll_s = 3.0           # readiness poll interval (tests shorten it)
        self._watchdog_restarts: dict[str, float] = {}
        self.monitor_s = 10.0
        self.install: dict[str, list[str]] = {m: [] for m in MODES}   # mode -> what is missing
        self._install_checked = 0.0
        self._addr: dict[str, str] = {}     # mode -> host:port on the isolated network (Custom)
        self.on_ready: list = []             # async callbacks (mode, load seconds) after a load (custom.py)
        self.switch_guard = None             # (who) -> reason a switch is refused now, or None (bench.py)
        self._update_gauge()

    # ------------------------------------------------------------------ helpers
    async def check_install(self, mode: str | None = None) -> dict[str, list[str]]:
        """Is each mode loadable: enabled on this machine, its image present, its weights complete on disk."""
        for m in ([mode] if mode else list(MODES)):
            problems: list[str] = []
            if m == "custom" and m not in self.presets:
                problems.append("no recipe assigned (an admin sets one up on the Models page)")
            elif m != "custom" and m not in self.cfg.modes_enabled:
                problems.append("not installed on this machine (enable it with ./install.sh)")
            elif self.cfg.check_install:
                image = self.presets[m].raw["image"]
                try:
                    if not await self.runtime.image_exists(image):
                        problems.append(f"image {image} not found (./install.sh builds or pulls it)")
                except Exception as e:
                    problems.append(f"image check failed: {type(e).__name__}")
                problems += self.presets.weights_problems(m)
            self.install[m] = problems
        self._install_checked = time.time()
        return self.install

    def upstream(self, mode: str) -> str:
        if mode in self._addr:
            return f"http://{self._addr[mode]}"
        return f"http://{self.cfg.upstream_host}:{self.presets[mode].host_port}"

    async def _resolve_address(self, mode: str) -> None:
        """A container on the isolated network has no published port: the hub talks to its IP."""
        self._addr.pop(mode, None)
        if self.presets[mode].raw.get("network") == "internal":
            addr = await self.runtime.address(self.presets[mode].container)
            if addr:
                self._addr[mode] = addr

    def _set(self, state: str, message: str) -> None:
        if state != self.state:
            self.phase_started = time.time()
        self.state = state
        self.message = message
        log.info("[%s] %s", state, message)
        self._update_gauge()

    def _update_gauge(self) -> None:
        metrics.MODE_INFO.clear()
        metrics.MODE_INFO.labels(self.loaded or self.target or "none", self.state).set(1)

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def status(self) -> dict[str, Any]:
        now = time.time()
        out: dict[str, Any] = {
            "mode": self.loaded, "state": self.state, "message": self.message,
            "loaded_since": self.loaded_since, "error": self.error, "last_switch": self.last_switch,
            "desired_mode": self.db.get_setting("desired_mode"),
            "install": {m: list(p) for m, p in self.install.items()},
        }
        if self.busy and self.target:
            exp = self.presets[self.target].expected.get("load_seconds")
            out["switch"] = {"target": self.target, "previous": self.previous, "attempt": self.attempt,
                             "started_at": self.switch_started,
                             "elapsed_s": round(now - (self.switch_started or now)),
                             "phase_elapsed_s": round(now - (self.phase_started or now)),
                             "expected_load_s": exp, "cancellable": self.state in ("loading", "draining")}
        if self.state == "draining":
            out["drain"] = {"quiet_s": round(min(self.tracker.quiet_for(), 1e9)),
                            "needed_s": self.db.get_setting("drain_quiet_s")}
        return out

    def accepting(self, mode: str, key_id: int | None) -> tuple[int, str] | None:
        """None if a request for `mode` may go upstream now, else (http status, message)."""
        if self.loaded == mode and self.state == "ready":
            return None
        if self.loaded == mode and self.state == "draining":
            if self.drain_allowed is not None and key_id in self.drain_allowed:
                return None
            return 503, (f"the Spark is about to switch from {mode} to {self.target}; new clients are not accepted "
                         f"until the switch is done")
        if self.state in ("loading", "stopping") or (self.busy and self.target):
            return 503, (f"the Spark is switching to {self.target} mode ({self.message}); "
                         f"retry when GET /api/v1/status says ready")
        if self.loaded and self.loaded != mode:
            return 409, (f"{mode} is not loaded; the Spark is in {self.loaded} mode. Use model 'auto' or "
                         f"'{self.loaded}', or switch with POST /api/v1/mode {{\"mode\": \"{mode}\"}}")
        return 503, f"no model is loaded ({self.state}: {self.message})"

    async def _sleep_or_cancel(self, seconds: float) -> bool:
        """Sleep; True if cancelled meanwhile."""
        try:
            await asyncio.wait_for(self._cancel.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------ public actions
    def request_switch(self, target: str, who: Principal, drain: bool = False, force: bool = False) -> dict[str, Any]:
        if target not in MODES:
            raise SwitchRefused(400, {"error": f"unknown mode {target!r}; modes: {', '.join(MODES)}"})
        if target not in self.presets:
            raise SwitchRefused(409, {"error": f"{target} mode cannot be loaded: no recipe assigned (an admin sets "
                                               f"one up on the Models page)"})
        if not who.has("switch"):
            raise SwitchRefused(403, {"error": "this key may not switch modes (needs the switch scope)"})
        if self.switch_guard and (why := self.switch_guard(who)):
            raise SwitchRefused(409, {"error": why})
        if force and not who.has("admin"):
            raise SwitchRefused(403, {"error": "force needs the admin scope"})
        if self.busy:
            raise SwitchRefused(409, {"error": f"a switch to {self.target} is already in progress",
                                      "status": self.status()})
        if target == self.loaded and self.state == "ready":
            return {"result": "already", "mode": target}
        if self.install.get(target):
            raise SwitchRefused(409, {"error": f"{target} mode cannot be loaded: {'; '.join(self.install[target])}",
                                      "install": self.install[target]})
        exclude = who.id if who.kind == "key" else None
        active = self.tracker.active(self.db.get_setting("activity_window_s"), exclude_key=exclude)
        if active and not drain and not force:
            raise SwitchRefused(409, {
                "error": f"{len(active)} other client(s) used the {self.loaded} model in the last "
                         f"{self.db.get_setting('activity_window_s') // 60} minutes; switching would break them",
                "active": active,
                "hint": "retry with \"drain\": true to wait until they are quiet, or \"force\": true (admin) to "
                        "switch now and cut them off"})
        self._cancel.clear()
        self.db.event("switch_requested", who.label, to=target, previous=self.loaded, drain=drain, force=force,
                      active=len(active))
        self.task = asyncio.create_task(self._switch(target, who.label, drain=drain and bool(active), force=force))
        return {"result": "started", "mode": target, "previous": self.loaded, "drain": drain and bool(active),
                "expected_load_s": self.presets[target].expected.get("load_seconds")}

    def cancel(self, who: Principal) -> dict[str, Any]:
        if not self.busy or self.state not in ("loading", "draining"):
            raise SwitchRefused(409, {"error": "nothing to cancel", "status": self.status()})
        self.db.event("switch_cancel_requested", who.label, target=self.target)
        self._cancel.set()
        return {"result": "cancelling", "target": self.target, "previous": self.previous}

    # ------------------------------------------------------------------ the switch
    async def _switch(self, target: str, actor: str, drain: bool, force: bool) -> None:
        prev = self.loaded
        self.target, self.previous = target, prev
        self.switch_started = time.time()
        self.error = None
        self.attempt = 0
        result = "failed"
        try:
            # checked again right before anything is stopped, so a missing image or weights never cost the
            # running model (the cached check can be minutes old)
            problems = (await self.check_install(target))[target]
            if problems:
                raise _Aborted(f"{target} cannot be loaded: {'; '.join(problems)}")
            if drain:
                await self._drain(force)
            if prev:
                await self._stop(prev)
            await self._stop_strays()
            await self._free_memory()
            ok, err = await self._load_with_retry(target)
            if ok:
                result = "ok"
                self.db.set_setting("desired_mode", target)
                return
            cancelled = err == "cancelled"
            result = "cancelled" if cancelled else "failed"
            self.error = None if cancelled else f"loading {target} failed: {err}"
            if not cancelled:
                self.notify("SparkHub: switch failed", f"Loading {target} failed twice: {err}", "high")
            if prev and prev != target:
                self._cancel.clear()
                self._set("loading", f"{'cancelled' if cancelled else target + ' failed'}; restoring {prev}")
                self.target = prev
                ok2, err2 = await self._load_with_retry(prev)
                if not ok2:
                    self.error = f"{self.error or 'cancelled'}; restoring {prev} also failed: {err2}"
                    self._set("failed", "no model loaded")
                    self.notify("SparkHub: no model loaded", self.error, "high")
            else:
                self._set("failed" if not cancelled else "idle", "no model loaded")
        except _Aborted as e:
            result = "aborted"
            self.error = str(e)
            self._set("ready" if self.loaded else "idle", f"switch to {target} aborted: {e}")
        except Exception as e:  # keep the hub alive whatever happens
            log.exception("switch failed")
            self.error = f"{type(e).__name__}: {e}"
            self._set("failed", f"switch to {target} failed")
        finally:
            took = time.time() - (self.switch_started or time.time())
            self.last_switch = {"to": target, "from": prev, "by": actor, "result": result,
                                "seconds": round(took), "at": self.switch_started, "error": self.error,
                                "mode_now": self.loaded}
            metrics.SWITCHES.labels(target, result).inc()
            self.db.event("switch", actor, **self.last_switch)
            if actor == "watchdog" and result == "ok":
                self.notify("SparkHub: model back", f"{target} reloaded by the watchdog in {round(took)} s.")
            elif actor == "upgrade":
                self.notify("SparkHub: reloaded after the upgrade" if result == "ok" else
                            "SparkHub: reload after the upgrade failed",
                            f"{target}: {result} in {round(took)} s." + (f" {self.error}" if self.error else ""),
                            "default" if result == "ok" else "high")
            elif actor == "boot" and result == "ok":
                self.notify("SparkHub ready", f"{target} loaded after the restart in {round(took)} s.")
            elif actor == "boot":
                self.notify("SparkHub: no model after restart", f"Loading {target} after the restart failed: "
                            f"{self.error}", "urgent")
            self.target = None
            self.drain_allowed = None
            self._update_gauge()

    async def _drain(self, force: bool) -> None:
        window = self.db.get_setting("activity_window_s")
        quiet = self.db.get_setting("drain_quiet_s")
        deadline = time.time() + self.db.get_setting("drain_timeout_s")
        self.drain_allowed = {c["key_id"] for c in self.tracker.active(window)}
        self._set("draining", f"waiting until clients are quiet for {quiet} s")
        while True:
            if self._cancel.is_set():
                raise _Aborted("cancelled while draining")
            if self.tracker.quiet_for() >= quiet:
                return
            if time.time() > deadline:
                if force:
                    return
                raise _Aborted(f"clients did not go quiet within {self.db.get_setting('drain_timeout_s')} s")
            await asyncio.sleep(1)

    async def _stop(self, mode: str) -> None:
        self._set("stopping", f"stopping {mode}")
        self.loaded = None
        self.loaded_since = None
        await self.runtime.stop(mode, self.presets[mode].container)

    async def _stop_strays(self) -> None:
        for mode, cname in (await self.runtime.running()).items():
            log.warning("stopping stray model container %s", cname)
            await self.runtime.stop(mode, cname)

    async def _free_memory(self) -> None:
        self._set("stopping", "returning memory (drop_caches)")
        info = await self.runtime.free_memory()
        log.info("memory: %s", info)

    async def _load_with_retry(self, mode: str) -> tuple[bool, str]:
        err = ""
        for attempt in (1, 2):
            self.attempt = attempt
            ok, err = await self._load(mode, attempt)
            if ok:
                return True, ""
            await self.runtime.stop(mode, self.presets[mode].container)
            if err == "cancelled":
                await self._free_memory()
                return False, err
            self.db.event("load_failed", None, mode=mode, attempt=attempt, error=err)
            if attempt == 1:
                await self._free_memory()
                self._set("loading", f"{mode} failed to start ({err}); retrying")
                if await self._sleep_or_cancel(self.db.get_setting("retry_delay_s")):
                    return False, "cancelled"
        return False, err

    async def _load(self, mode: str, attempt: int) -> tuple[bool, str]:
        p = self.presets[mode]
        self._set("loading", f"starting {mode} ({p.served_name}), attempt {attempt}")
        t0 = time.time()
        try:
            await self.runtime.start(self.presets.render(mode))
            await self._resolve_address(mode)
        except Exception as e:
            return False, f"container did not start: {e}"
        timeout = self.db.get_setting("load_timeout_s")
        url = self.upstream(mode)
        self._set("loading", f"loading {mode} weights")
        while True:
            if self._cancel.is_set():
                return False, "cancelled"
            st = await self.runtime.state(p.container)
            if not st or not st["running"]:
                code = st and st.get("exit_code")
                oom = st and st.get("oom_killed")
                return False, f"container exited (code {code}{', out of memory' if oom else ''})"
            try:
                r = await self.http.get(url + "/v1/models", timeout=5)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() - t0 > timeout:
                return False, f"not ready after {timeout} s"
            if await self._sleep_or_cancel(self.poll_s):
                return False, "cancelled"
        self._set("loading", f"{mode} answering; smoke test")
        try:
            r = await self.http.post(url + "/v1/chat/completions", json={
                "model": p.served_name, "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 1, "chat_template_kwargs": {"enable_thinking": False}})
            if r.status_code != 200:
                return False, f"smoke request failed: {r.status_code} {r.text[:200]}"
        except httpx.HTTPError as e:
            return False, f"smoke request failed: {e}"
        took = time.time() - t0
        metrics.LOAD_SECONDS.labels(mode).observe(took)
        self.loaded = mode
        self.loaded_since = time.time()
        self.db.event("model_ready", None, mode=mode, load_seconds=round(took), mem=meminfo())
        self._set("ready", f"{mode} ready (loaded in {took:.0f} s)")
        for cb in self.on_ready:
            try:
                await cb(mode, took)
            except Exception:
                log.exception("after-load hook")
        return True, ""

    # ------------------------------------------------------------------ boot and monitoring
    async def startup(self) -> None:
        """Adopt a model container that is already running (hub restart), else load the last mode."""
        await self.check_install()
        running = await self.runtime.running()
        desired = self.db.get_setting("desired_mode") or "fast"
        if desired == "custom" and self.install.get("custom"):
            desired = "fast"
        if desired != "custom" and desired not in self.cfg.modes_enabled and self.cfg.modes_enabled:
            desired = self.cfg.modes_enabled[0]
        if len(running) == 1 and next(iter(running)) in self.presets:
            mode = next(iter(running))
            if await self._pin_changed(mode, running[mode]):
                # an upgrade changed what this mode runs (image, weights, args): reload it now (./install.sh
                # upgrade asked before restarting the hub, and downloaded the new weights first)
                self.db.event("model_pin_changed", "upgrade", mode=mode)
                self.notify("SparkHub: reloading after the upgrade", f"{mode} changed in this release; "
                                                                     f"reloading it (~4-5 min).")
                self.task = asyncio.create_task(self._switch(mode, "upgrade", drain=False, force=True))
            else:
                self.task = asyncio.create_task(self._adopt(mode))
        elif running:
            # more than one model container: keep none of them, load the desired mode
            self.task = asyncio.create_task(self._switch(desired, "boot", drain=False, force=True))
        elif self.cfg.autostart:
            # no model container survived: the Spark or Docker restarted (a hub-only restart adopts instead)
            self.db.event("restored_after_restart", "boot", mode=desired)
            self.notify("SparkHub started", f"No model was running (Docker or the Spark restarted). "
                                            f"Loading {desired} (~4-5 min).")
            self.task = asyncio.create_task(self._switch(desired, "boot", drain=False, force=True))
        else:
            self._set("idle", "no model loaded (autostart off)")
        self._monitor = asyncio.create_task(self._monitor_loop())

    async def _pin_changed(self, mode: str, container: str) -> bool:
        """The running Fast/Quality container was started from another version of its preset file, and the new
        one can be loaded. A container from before pins (no label) is adopted as it is."""
        if mode not in BUILTIN:
            return False
        info = await self.runtime.inspect(container) or {}
        old = ((info.get("Config") or {}).get("Labels") or {}).get(PIN_LABEL)
        if not old or old == self.presets[mode].pin:
            return False
        if problems := self.install.get(mode):
            self.error = f"{mode} changed in this release but cannot be loaded yet: {'; '.join(problems)}"
            log.warning(self.error)
            return False
        return True

    async def _adopt(self, mode: str) -> None:
        p = self.presets[mode]
        self.target = mode
        self.switch_started = time.time()
        self._set("loading", f"found {p.container} running; waiting for it to answer")
        deadline = time.time() + self.db.get_setting("load_timeout_s")
        try:
            await self._resolve_address(mode)
            while time.time() < deadline:
                st = await self.runtime.state(p.container)
                if not st or not st["running"]:
                    break
                try:
                    r = await self.http.get(self.upstream(mode) + "/v1/models", timeout=5)
                    if r.status_code == 200:
                        self.loaded = mode
                        self.loaded_since = time.time()
                        self.db.set_setting("desired_mode", mode)
                        self.db.event("model_adopted", "boot", mode=mode)
                        self._set("ready", f"{mode} ready (already running)")
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self.poll_s)
        finally:
            self.target = None
        # the running container never became ready: start over
        self.task = asyncio.create_task(self._switch(mode, "boot", drain=False, force=True))

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.monitor_s)
            if time.time() - self._install_checked > 300 and not self.busy:
                try:
                    await self.check_install()
                except Exception:
                    log.exception("install check")
            try:
                if self.state == "ready" and self.loaded and not self.busy:
                    st = await self.runtime.state(self.presets[self.loaded].container)
                    if not st or not st["running"]:
                        mode = self.loaded
                        why = "removed" if not st else f"exit code {st['exit_code']}" + (
                            ", out of memory" if st.get("oom_killed") else "")
                        self.loaded = None
                        self.loaded_since = None
                        self.error = f"{mode} model stopped unexpectedly ({why})"
                        self._set("failed", self.error)
                        self.db.event("model_crashed", None, mode=mode, why=why)
                        self._watchdog(mode, why)
            except Exception:
                log.exception("monitor")

    def _watchdog(self, mode: str, why: str) -> None:
        """Reload a crashed model once. A second crash within WATCHDOG_WINDOW_S stays down (no loop)."""
        now = time.time()
        last = self._watchdog_restarts.get(mode)
        if not self.db.get_setting("watchdog"):
            self.notify("SparkHub: model stopped", f"{self.error}. Watchdog is off; switch to bring it back.", "high")
            return
        if last and now - last < WATCHDOG_WINDOW_S:
            self.db.event("watchdog_gave_up", "watchdog", mode=mode, why=why,
                          minutes_since_restart=round((now - last) / 60))
            self.notify("SparkHub: model crashed again",
                        f"{self.error}, {round((now - last) / 60)} min after the watchdog restarted it. "
                        f"Not restarting again; check the logs and switch by hand.", "urgent")
            return
        self._watchdog_restarts[mode] = now
        self.db.event("watchdog_restart", "watchdog", mode=mode, why=why)
        self.notify("SparkHub: model stopped, restarting", f"{self.error}. Reloading {mode} (~4-5 min).", "high")
        self._cancel.clear()
        self.task = asyncio.create_task(self._switch(mode, "watchdog", drain=False, force=True))

    async def shutdown(self) -> None:
        for t in (self._monitor, self.task):
            if t and not t.done():
                t.cancel()
        await self.http.aclose()
