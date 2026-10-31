"""Dashboard: server-rendered Jinja2 pages, HTMX for the live parts, uPlot for the mini-charts.

Every page needs a full session (password + TOTP). Every POST checks the session's CSRF token.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from urllib.parse import quote

import httpx

from . import keys as keymod
from .auth import Auth, Session, client_ip, qr_data_uri
from .bench import ALWAYS as BENCH_ALWAYS, OPTIONAL as BENCH_OPTIONAL, SUITES as BENCH_SUITES, BenchError
from .custom import CustomError
from .recipes import RecipeError
from .keys import Principal
from .lifecycle import SwitchRefused
from .embeddings import RUN_MODE_LABELS, RUN_MODES
from .presets import MODES, SETTING_HELP, SETTING_RULES

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
LOGIN_CSRF = "sparkhub_lc"
NEXT = "sparkhub_next"


def safe_next(v: str | None) -> str | None:
    """Only same-site paths: '/x', never '//host', '/\\host' or a scheme."""
    if not v or not v.startswith("/") or v.startswith("//") or "\\" in v or len(v) > 300:
        return None
    if any(ord(ch) < 33 for ch in v):
        return None
    return v

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _ts(ts: float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return time.strftime(fmt, time.localtime(ts)) if ts else "–"


def _ago(ts: float | None) -> str:
    if not ts:
        return "–"
    d = time.time() - ts
    if d < 90:
        return f"{d:.0f} s ago"
    if d < 5400:
        return f"{d / 60:.0f} min ago"
    if d < 172800:
        return f"{d / 3600:.1f} h ago"
    return f"{d / 86400:.0f} d ago"


def _dur(s: float | None) -> str:
    if s is None:
        return "–"
    s = int(s)
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s} s"


def _span(s: float | None) -> str:
    """A length of time for people: 45 s, 12m 5s, 1h 54m, 2d 3h."""
    if s is None:
        return "–"
    s = int(s)
    for size, a, b, bsize in ((86400, "d", "h", 3600), (3600, "h", "m", 60), (60, "m", "s", 1)):
        if s >= size:
            return f"{s // size}{a} {(s % size) // bsize}{b}"
    return f"{s} s"


def _kilo(n: float | None) -> str:
    """Token counts: 950, 39k, 1.2M."""
    if n is None:
        return "–"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 10e3 else f"{n / 1e3:.1f}k" if n >= 1e3 else f"{n:.0f}"


templates.env.filters["span"] = _span
templates.env.filters["kilo"] = _kilo
templates.env.filters["ts"] = _ts
templates.env.filters["ago"] = _ago
templates.env.filters["dur"] = _dur
def _fmt(v: Any, spec: str = "{:.1f}") -> str:
    if v is None or isinstance(v, Undefined) or isinstance(v, str):
        return "–" if not isinstance(v, str) else v
    return spec.format(v)


templates.env.filters["fmt"] = _fmt


class SecurityHeaders:
    """Pure ASGI middleware (BaseHTTPMiddleware would hide client disconnects from the gateway)."""

    def __init__(self, app, hsts: bool):
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                have = {k.lower() for k, _ in headers}

                def add(k: str, v: str) -> None:
                    if k.encode() not in have:
                        headers.append((k.encode(), v.encode()))
                if not path.startswith("/v1/"):
                    add("content-security-policy", CSP)
                    add("x-frame-options", "DENY")
                    add("referrer-policy", "same-origin")
                    if not path.startswith("/static/"):
                        add("cache-control", "no-store")
                add("x-content-type-options", "nosniff")
                if self.hsts:
                    add("strict-transport-security", "max-age=31536000")
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, send_wrapper)


class NeedLogin(Exception):
    def __init__(self, to: str = "/login"):
        self.to = to


class Forbidden(Exception):
    """A logged-in user whose role doesn't include the page."""
    def __init__(self, s: Session):
        self.s = s


# the dashboard pages and the role each needs (admin > switch > infer); the nav shows the ones a user can open
NAV = [("/", "Overview", "infer"), ("/switch", "Models", "switch"), ("/bench", "Benchmarks", "admin"),
       ("/settings", "Settings", "admin"),
       ("/activity", "Activity", "infer"), ("/keys", "Keys", "infer"), ("/users", "Users", "admin"),
       ("/logs", "Logs", "admin"), ("/connect", "Connect", "infer"), ("/account", "Account", "infer")]


def install(app: FastAPI) -> None:
    db = app.state.db
    hub = app.state.hub
    cfg = app.state.cfg
    auth = Auth(db, cfg.cookie_secure)
    app.state.auth = auth
    hub.auth = auth
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    app.add_middleware(SecurityHeaders, hsts=cfg.cookie_secure)

    @app.exception_handler(NeedLogin)
    async def need_login(request: Request, exc: NeedLogin):
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": exc.to})
        return RedirectResponse(exc.to, status_code=303)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        resp = page(request, "forbidden.html", exc.s)
        resp.status_code = 403
        return resp

    # ------------------------------------------------------------------ helpers
    def login_step(s: Session) -> str:
        """Where a half-logged-in session continues: a new password first (temporary one), then the second factor."""
        if s.must_change_pw:
            return "/login/password"
        return "/login/totp" if s.totp_enabled else "/login/enroll"

    def user(request: Request, need: str = "infer") -> Session:
        """The logged-in user; need: the role the page requires (infer = any user)."""
        s = auth.session(request)
        if s is None:
            raise NeedLogin("/login")
        if s.stage != "full":
            raise NeedLogin(login_step(s))
        if not s.can(need):
            raise Forbidden(s)
        return s

    async def checked(request: Request, need: str = "infer") -> tuple[Session, dict[str, Any]]:
        """Session + form, after the CSRF check (form field or HTMX header)."""
        s = user(request, need)
        form = dict(await request.form())
        token = form.get("csrf") or request.headers.get("x-csrf-token") or ""
        if not secrets.compare_digest(str(token), s.csrf):
            raise NeedLogin("/login")
        return s, form

    def principal(s: Session) -> Principal:
        return Principal("user", s.user_id, s.username, s.role, session_id=s.id, csrf=s.csrf, owner_id=s.user_id,
                         owner_name=s.username)

    def page(request: Request, name: str, s: Session | None, **ctx) -> HTMLResponse:
        # partials (polled by htmx) also refresh the header's mode badge out of band
        nav = [(h, label) for h, label, need in NAV if s and s.can(need) and (h != "/bench" or hub.bench.installed)]
        ctx.setdefault("modes", MODES)
        ctx.update(request=request, s=s, path=request.url.path, oob=name.startswith("_"),
                   status=hub.modes.status(), chat_on=hub.chat_enabled(), nav=nav, role_labels=keymod.ROLE_LABELS,
                   cfg=hub.cfg, update=hub.updates.available() if s and s.is_admin else None)
        return templates.TemplateResponse(request, name, ctx)

    def login_page(request: Request, name: str, **ctx) -> HTMLResponse:
        token = request.cookies.get(LOGIN_CSRF) or secrets.token_urlsafe(16)
        resp = templates.TemplateResponse(request, name, dict(request=request, login_csrf=token, s=None, **ctx))
        resp.set_cookie(LOGIN_CSRF, token, httponly=True, secure=cfg.cookie_secure, samesite="strict", path="/")
        return resp

    def login_csrf_ok(request: Request, value: str) -> bool:
        c = request.cookies.get(LOGIN_CSRF)
        return bool(c) and secrets.compare_digest(c, value or "")

    # ------------------------------------------------------------------ login
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str | None = None):
        resp = login_page(request, "login.html")
        nxt = safe_next(next)
        if nxt:
            resp.set_cookie(NEXT, nxt, httponly=True, secure=cfg.cookie_secure, samesite="lax", path="/",
                            max_age=900)
        return resp

    def after_login(request: Request, default: str = "/") -> RedirectResponse:
        resp = RedirectResponse(safe_next(request.cookies.get(NEXT)) or default, status_code=303)
        resp.delete_cookie(NEXT, path="/")
        return resp

    @app.post("/login")
    async def login_post(request: Request, username: str = Form(""), password: str = Form(""),
                         csrf: str = Form(""), remember: str = Form("")):
        ip = client_ip(request)
        if not login_csrf_ok(request, csrf):
            return login_page(request, "login.html", error="Form expired; please try again.")
        await asyncio.sleep(auth.ip_delay(ip))
        uid, err = auth.check_password(username, password, ip)
        if uid is None:
            return login_page(request, "login.html", error=err, username=username[:64])
        row = db.one("SELECT totp_enabled, must_change_pw FROM users WHERE id=?", (uid,))
        resp = RedirectResponse("/login/password" if row["must_change_pw"] else
                                "/login/totp" if row["totp_enabled"] else "/login/enroll", status_code=303)
        auth.start_session(resp, uid, request, remember=bool(remember))
        return resp

    # a temporary password (a new user, or an admin's reset) is replaced before the second factor
    @app.get("/login/password", response_class=HTMLResponse)
    async def newpw_form(request: Request):
        s = auth.session(request)
        if s is None or not s.must_change_pw:
            return RedirectResponse("/login" if s is None else "/", status_code=303)
        return login_page(request, "newpw.html", username=s.username)

    @app.post("/login/password")
    async def newpw_post(request: Request, new: str = Form(""), again: str = Form(""), csrf: str = Form("")):
        s = auth.session(request)
        if s is None or not s.must_change_pw:
            return RedirectResponse("/login", status_code=303)
        if not login_csrf_ok(request, csrf):
            return login_page(request, "newpw.html", username=s.username, error="Form expired; please try again.")
        if new != again:
            return login_page(request, "newpw.html", username=s.username, error="The two passwords differ.")
        from .auth import replace_temporary_password
        try:
            replace_temporary_password(db, s.user_id, new, s.id)
        except ValueError as e:
            return login_page(request, "newpw.html", username=s.username, error=str(e).capitalize() + ".")
        return RedirectResponse("/login/totp" if s.totp_enabled else "/login/enroll", status_code=303)

    @app.get("/login/totp", response_class=HTMLResponse)
    async def totp_form(request: Request):
        s = auth.session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        if s.stage == "full":
            return RedirectResponse("/", status_code=303)
        if s.must_change_pw:
            return RedirectResponse("/login/password", status_code=303)
        if not s.totp_enabled:
            return RedirectResponse("/login/enroll", status_code=303)
        return login_page(request, "totp.html", username=s.username)

    @app.post("/login/totp")
    async def totp_post(request: Request, code: str = Form(""), csrf: str = Form("")):
        s = auth.session(request)
        if s is None or s.stage == "full" or not s.totp_enabled or s.must_change_pw:
            return RedirectResponse("/login", status_code=303)
        if not login_csrf_ok(request, csrf):
            return login_page(request, "totp.html", username=s.username, error="Form expired; please try again.")
        ip = client_ip(request)
        await asyncio.sleep(auth.ip_delay(ip))
        err = auth.verify_second_factor(s, code[:32], ip)
        if err:
            if auth.session(request) is None:
                return login_page(request, "login.html", error=err)
            return login_page(request, "totp.html", username=s.username, error=err)
        left = json.loads(db.one("SELECT recovery FROM users WHERE id=?", (s.user_id,))["recovery"] or "[]")
        resp = RedirectResponse("/?recovery_left=%d" % len(left), status_code=303) if len(left) < 3 \
            else after_login(request)
        auth.finish_login(request, resp)
        return resp

    @app.get("/login/enroll", response_class=HTMLResponse)
    async def enroll_form(request: Request):
        s = auth.session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        if s.must_change_pw:
            return RedirectResponse("/login/password", status_code=303)
        if s.totp_enabled:
            return RedirectResponse("/" if s.stage == "full" else "/login/totp", status_code=303)
        secret, uri = auth.begin_enrolment(s)
        return login_page(request, "enroll.html", secret=secret, qr=qr_data_uri(uri), username=s.username)

    @app.post("/login/enroll")
    async def enroll_post(request: Request, code: str = Form(""), csrf: str = Form("")):
        s = auth.session(request)
        if s is None or s.totp_enabled or s.must_change_pw:
            return RedirectResponse("/login", status_code=303)
        if not login_csrf_ok(request, csrf):
            return RedirectResponse("/login/enroll", status_code=303)
        codes, err = auth.confirm_enrolment(s, code[:16], client_ip(request))
        if err:
            if auth.session(request) is None:
                return login_page(request, "login.html", error="Too many failed attempts; the account is locked.")
            secret, uri = auth.begin_enrolment(s)
            return login_page(request, "enroll.html", secret=secret, qr=qr_data_uri(uri), username=s.username,
                              error=err)
        resp = login_page(request, "recovery.html", codes=codes, first=True)
        auth.finish_login(request, resp)
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        s, _ = await checked(request)
        resp = RedirectResponse("/login", status_code=303)
        auth.logout(request, resp)
        return resp

    # nginx auth_request target for /grafana, /arcane and /chat: 204 with the user, else 401
    @app.get("/auth/check")
    async def auth_check(request: Request):
        s = auth.session(request)
        if s is None or s.stage != "full" or not s.is_admin:
            return Response(status_code=401)
        return Response(status_code=204, headers={"X-Auth-User": s.username})

    # nginx auth_request target for /chat/: as /auth/check, plus 403 while chat-ui is turned off (nginx then
    # shows /chat-off)
    @app.get("/auth/check-chat")
    async def auth_check_chat(request: Request):
        s = auth.session(request)
        if s is None or s.stage != "full":                  # chat is for every role
            return Response(status_code=401)
        if not hub.chat_enabled():
            return Response(status_code=403)
        return Response(status_code=204, headers={"X-Auth-User": s.username})

    @app.get("/chat-off", response_class=HTMLResponse)
    async def chat_off(request: Request):
        s = user(request)
        resp = page(request, "chat_off.html", s, installed=hub.chat_installed)
        resp.status_code = 503
        return resp

    # ------------------------------------------------------------------ overview / live
    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request, recovery_left: int | None = None):
        s = user(request)
        st = await hub.full_status()
        return page(request, "overview.html", s, st=st, live=await _live_ctx(st, s), recovery_left=recovery_left)

    COMPONENT_NAMES = {"hub": "Hub", "nginx": "Web server", "grafana": "Grafana", "prometheus": "Prometheus",
                       "node-exporter": "Host metrics", "arcane": "Arcane", "chatui": "Chat"}
    COMPONENT_ORDER = ["hub", "nginx", "chatui", "arcane", "grafana", "prometheus", "node-exporter"]

    async def _components() -> list[dict[str, Any]] | None:
        comps = await hub.components()
        if comps is None:
            return None
        out = []
        embed_up = {c["service"]: c for c in comps if c.get("embed") and c["state"] == "running"}
        for c in comps:
            if c.get("embed"):
                continue                         # listed from the run mode below, container or not
            svc, state, health = c["service"], c["state"], c["health"]
            if state == "running":
                level = {"unhealthy": "err", "starting": "warn"}.get(health or "", "ok")
            elif state in ("restarting", "created", "paused"):
                level = "warn"
            elif svc == "chatui" and not hub.chat_enabled():
                level = "off"                    # turned off on the Settings page
            else:
                level = "err"
            label = (f"Model · {svc}" if c["model"] else f"Embeddings · {svc}" if c.get("embed") else
                     COMPONENT_NAMES.get(svc, svc or c["name"]))
            text = re.sub(r"\s*\((healthy|unhealthy|health: starting)\)", "", c["status"] or state)
            text = "off" if level == "off" else (f"{health} · {text}" if health in ("unhealthy", "starting") else text)
            rank = -1 if c["model"] or c.get("embed") else (COMPONENT_ORDER.index(svc) if svc in COMPONENT_ORDER else 99)
            out.append({"label": label, "level": level, "text": text, "name": c["name"], "rank": rank,
                        "running": state == "running", "attention": level in ("warn", "err")})
        # the embedding services always show their run mode: always on (green), on-demand (blue), off (red)
        if hub.embed.installed:
            for name, sv in hub.embed.status()["services"].items():
                pol, c = sv["policy"], embed_up.get(name)
                # running: green whatever the mode · on-demand and stopped: blue · off: red · starting: amber
                text = RUN_MODE_LABELS[pol]
                if sv["state"] == "failed":
                    level, text = "err", f"{text} · {sv['error'] or 'failed'}"
                elif pol == "off":
                    level = "disabled"
                elif c or sv["state"] == "ready":
                    level = "ok"
                    text += " · " + (re.sub(r"\s*\((healthy|unhealthy|health: starting)\)", "", c["status"] or "running")
                                     if c else "running")
                elif sv["state"] == "starting":
                    level, text = "warn", text + " · starting"
                else:
                    level, text = ("demand" if pol == "on-demand" else "warn"), text + " · stopped"
                out.append({"label": "Embeddings" if name == "embed" else "Reranker",
                            "level": level, "text": text, "name": hub.embed.services[name].container,
                            "rank": -0.5, "running": c is not None or sv["state"] == "ready",
                            "attention": level == "err"})
        return sorted(out, key=lambda c: (c["rank"], c["label"]))

    def _spark(key: str | tuple[str, ...], hi: float | None = None, n: int = 60) -> str:
        """SVG polyline points (viewBox 0 0 100 24) of the last n samples (5 s apart); gaps are skipped."""
        keys = (key,) if isinstance(key, str) else key
        vals = [sum(v for k in keys if isinstance(v := smp.get(k), (int, float))) if any(
                isinstance(smp.get(k), (int, float)) for k in keys) else None for smp in list(hub.samples)[-n:]]
        nums = [v for v in vals if v is not None]
        if len(nums) < 2:
            return ""
        top = hi or max(nums) or 1
        step = 100 / (n - 1)
        off = n - len(vals)                  # fewer samples than n: the line starts further right
        return " ".join(f"{(off + i) * step:.1f},{23 - 22 * min(v, top) / top:.1f}"
                        for i, v in enumerate(vals) if v is not None)

    def _req_row(r: Any) -> dict[str, Any]:
        r = dict(r)
        fresh = (r["prompt_tokens"] or 0) - (r["cached_tokens"] or 0)
        r["prefill_tps"] = fresh / r["ttft"] if r["ttft"] and fresh > 0 else None
        r["ms_per_token"] = 1000 / r["gen_tps"] if r["gen_tps"] else None
        r["label"] = {"finished": "done", "length": "cut"}.get(r["outcome"], r["outcome"])
        return r

    CHAT_ONLY = "endpoint NOT IN ('/v1/embeddings', '/v1/rerank')"

    async def _live_ctx(st: dict[str, Any], s: Session) -> dict[str, Any]:
        last = hub.samples[-1] if hub.samples else {}
        since = time.time() - 900
        row = db.one("SELECT COUNT(*) n, AVG(ttft) ttft, AVG(gen_tps) tps, SUM(completion_tokens) toks FROM activity"
                     f" WHERE ts >= ? AND outcome NOT IN ('refused') AND {CHAT_ONLY}", (since,))
        exp = hub.presets[st["mode"]].expected if st.get("mode") else {}
        cols = ("ts, key_name, mode, outcome, prompt_tokens, cached_tokens, completion_tokens, ttft, gen_tps, "
                "duration")
        # recent requests: everyone's for an admin, a user's own otherwise
        mine = "" if s.is_admin else " AND user_id = ?"
        latest = [_req_row(r) for r in db.all(f"SELECT {cols}, user_name FROM activity WHERE outcome != 'refused'"
                                               f"{mine} ORDER BY ts DESC LIMIT 5",
                                               () if s.is_admin else (s.user_id,))]
        done = db.one(f"SELECT {cols} FROM activity WHERE outcome IN ('finished', 'length') AND gen_tps > 0 "
                      "ORDER BY ts DESC LIMIT 1")
        # totals since the loaded model came up: prompt read at the prefill rate, output written at the decode rate
        tot = None
        if st.get("loaded_since"):
            t = db.one("SELECT COUNT(*) n, SUM(prompt_tokens) p, SUM(cached_tokens) c, SUM(completion_tokens) o, "
                       "SUM(CASE WHEN ttft > 0 THEN prompt_tokens - COALESCE(cached_tokens, 0) END) pf, "
                       "SUM(CASE WHEN ttft > 0 THEN ttft END) tt, "
                       "SUM(CASE WHEN gen_tps > 0 THEN completion_tokens END) od, "
                       "SUM(CASE WHEN gen_tps > 0 THEN completion_tokens / gen_tps END) dt "
                       f"FROM activity WHERE ts >= ? AND outcome != 'refused' AND {CHAT_ONLY}", (st["loaded_since"],))
            if t and t["n"]:
                tot = dict(t, since=st["loaded_since"], prefill_tps=t["pf"] / t["tt"] if t["tt"] else None,
                           decode_tps=t["od"] / t["dt"] if t["dt"] else None)
        mode = st.get("mode")
        ctx_max = (st.get("settings") or {}).get("max_model_len")
        # context held right now: the KV cache in use, per running request (vLLM reports no per-request count)
        v = st.get("vllm") or {}
        ctx_now = None
        if v.get("running") and v.get("kv_tokens") and isinstance(v.get("kv_usage"), (int, float)):
            ctx_now = {"tokens": v["kv_usage"] * v["kv_tokens"] / v["running"], "running": int(v["running"])}
        api = f"https://{cfg.public_host}" + ("" if cfg.tls_port == 443 else f":{cfg.tls_port}") + "/v1"
        spark = {"cpu": _spark("cpu_pct", 100), "mem": _spark("mem_used_gib", (st.get("memory") or {}).get("total_gib")),
                 "gpu": _spark("gpu_util_pct", 100), "temp": _spark("gpu_temp_c", 100),
                 "io": _spark(("disk_read_mbs", "disk_write_mbs")), "tps": _spark("gen_tps"),
                 "req": _spark(("running", "waiting")), "kv": _spark("kv_pct", 100)}
        return {"sample": last, "recent": dict(row) if row else {}, "expected": exp,
                "window_min": db.get_setting("activity_window_s") // 60, "components": await _components(),
                "latest": latest, "last": _req_row(done) if done else None, "totals": tot, "ctx_max": ctx_max, "ctx_now": ctx_now,
                "model": hub.presets[mode].raw.get("model") if mode else None, "api": api, "spark": spark}

    @app.get("/ui/live", response_class=HTMLResponse)
    async def live(request: Request):
        s = user(request)
        st = await hub.full_status()
        return page(request, "_live.html", s, st=st, live=await _live_ctx(st, s))

    @app.get("/ui/series")
    async def series(request: Request):
        user(request)
        keys = ("t", "gen_tps", "per_stream_tps", "running", "waiting", "kv_pct", "mem_used_gib", "gpu_temp_c",
                "gpu_power_w")
        data = {k: [smp.get(k) for smp in hub.samples] for k in keys}
        return JSONResponse(data)

    # ------------------------------------------------------------------ switching
    @app.get("/switch", response_class=HTMLResponse)
    async def switch_page(request: Request, error: str | None = None, msg: str | None = None, all: int = 0):
        s = user(request, "switch")
        if not hub.modes.busy:
            await hub.modes.check_install()      # fresh: the installer may have just added a mode
        custom = {}
        if s.can("admin"):                       # the Custom section (recipes) is for admins only
            custom = dict(recipes=hub.custom.rows(), assigned=hub.custom.assigned(),
                          registries=hub.custom.registries(), show_all=bool(all),
                          update_checked=db.get_setting("recipe_update_checked"), custom_busy=hub.custom.custom_busy())
        return page(request, "switch.html", s, error=error, msg=msg, **custom, **_switch_ctx())

    def _switch_ctx(**extra) -> dict[str, Any]:
        window = db.get_setting("activity_window_s")
        return dict(active=hub.tracker.active(window), window_min=window // 60, presets=hub.presets,
                    st=hub.modes.status(), **extra)

    @app.get("/ui/switch", response_class=HTMLResponse)
    async def switch_partial(request: Request):
        s = user(request, "switch")
        return page(request, "_switch.html", s, **_switch_ctx())

    @app.post("/ui/switch", response_class=HTMLResponse)
    async def switch_post(request: Request):
        s, form = await checked(request, "switch")
        action = form.get("action", "switch")
        result = error = None
        try:
            if action == "cancel":
                result = hub.modes.cancel(principal(s))
            else:
                result = hub.modes.request_switch(str(form.get("mode")), principal(s),
                                                  drain=action == "drain", force=action == "force")
        except SwitchRefused as e:
            error = e.detail
        await asyncio.sleep(0.3)
        return page(request, "_switch.html", s, **_switch_ctx(result=result, error=error))

    # ------------------------------------------------------------------ settings
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: str | None = None, error: str | None = None,
                            tab: str = "models"):
        s = user(request, "admin")
        tabs = ["models", "embeddings", "hub"] + (["chat"] if hub.chat_installed else []) + ["notifications", "updates"]
        tab = tab if tab in tabs else "models"
        modes = [{"name": m, "preset": hub.presets[m], "settings": hub.presets.settings(m),
                  "overrides": hub.presets.overrides(m)} for m in hub.presets.available]
        return page(request, "settings.html", s, items=modes, help=SETTING_HELP, rules=SETTING_RULES,
                    hub_settings=db.all_settings(), ntfy=hub.notifier.view(), saved=saved, error=error,
                    tabs=tabs, tab=tab, chat={"installed": hub.chat_installed, "enabled": hub.chat_enabled(), "state": hub.chat_state},
                    embed=hub.embed.status(), embed_modes=RUN_MODE_LABELS,
                    latest=db.get_setting("update_latest") or {})

    @app.post("/settings/{mode}")
    async def settings_post(mode: str, request: Request):
        s, form = await checked(request, "admin")
        if mode not in hub.presets:
            return RedirectResponse("/settings?tab=models", status_code=303)
        if form.get("action") == "reset":
            hub.presets.reset_settings(mode, f"user:{s.username}")
            return RedirectResponse(f"/settings?tab=models&saved={mode}", status_code=303)
        values: dict[str, Any] = {}
        for k, (typ, _) in SETTING_RULES.items():
            if typ is bool:
                values[k] = k in form
            elif k in form:
                values[k] = form[k]
        try:
            hub.presets.update_settings(mode, values, f"user:{s.username}")
        except (ValueError, TypeError) as e:
            return RedirectResponse(f"/settings?tab=models&error={str(e)[:200]}", status_code=303)
        return RedirectResponse(f"/settings?tab=models&saved={mode}", status_code=303)

    HUB_SETTINGS = {"activity_window_s": (60, 3600), "drain_quiet_s": (10, 1800), "drain_timeout_s": (60, 7200),
                    "key_max_concurrency": (1, 16), "queue_timeout_s": (10, 3600), "load_timeout_s": (300, 3600),
                    "activity_retention_days": (7, 3650)}

    @app.post("/settings-hub")
    async def hub_settings_post(request: Request):
        s, form = await checked(request, "admin")
        changed = {}
        try:
            for k, (lo, hi) in HUB_SETTINGS.items():
                if k in form:
                    v = int(form[k])
                    if not lo <= v <= hi:
                        raise ValueError(f"{k} must be between {lo} and {hi}")
                    changed[k] = v
        except ValueError as e:
            return RedirectResponse(f"/settings?tab=hub&error={str(e)[:200]}", status_code=303)
        for k, v in changed.items():
            db.set_setting(k, v)
        db.event("hub_settings_changed", f"user:{s.username}", **changed)
        return RedirectResponse("/settings?tab=hub&saved=hub", status_code=303)

    @app.post("/settings-embed")
    async def embed_settings_post(request: Request):
        s, form = await checked(request, "admin")
        if not hub.embed.installed:
            return RedirectResponse("/settings?tab=embeddings&error=the embedding service is not installed "
                                    "(EMBEDDINGS in sparkhub.conf)", status_code=303)
        mode = str(form.get("mode") or "")
        try:
            if mode not in RUN_MODES:
                raise ValueError(f"run mode must be one of {', '.join(RUN_MODES)}")
            idle = int(form.get("idle_minutes") or 0)
            if not 1 <= idle <= 1440:
                raise ValueError("idle minutes must be between 1 and 1440")
        except ValueError as e:
            return RedirectResponse(f"/settings?tab=embeddings&error={str(e)[:200]}", status_code=303)
        rerank = "rerank" in form
        db.set_setting("embed_mode", mode)
        db.set_setting("embed_idle_minutes", idle)
        db.set_setting("embed_rerank", rerank)
        db.event("embed_settings_changed", f"user:{s.username}", mode=mode, idle_minutes=idle, rerank=rerank)
        try:
            await hub.embed.apply()
        except Exception as e:
            return RedirectResponse(f"/settings?tab=embeddings&error=embeddings: {str(e)[:200]}", status_code=303)
        return RedirectResponse(f"/settings?tab=embeddings&saved=embeddings: {RUN_MODE_LABELS[mode]}", status_code=303)

    @app.post("/settings-chat")
    async def chat_settings_post(request: Request):
        s, form = await checked(request, "admin")
        if not hub.chat_installed:
            return RedirectResponse("/settings?tab=chat&error=chat-ui is not installed (CHATUI in sparkhub.conf)",
                                    status_code=303)
        on = "enabled" in form
        db.set_setting("chat_enabled", on)
        db.event("chat_enabled" if on else "chat_disabled", f"user:{s.username}")
        try:
            await hub.apply_chat()
        except Exception as e:
            return RedirectResponse(f"/settings?tab=chat&error=chat-ui: {str(e)[:200]}", status_code=303)
        return RedirectResponse(f"/settings?tab=chat&saved=chat {'on' if on else 'off'}", status_code=303)

    @app.post("/settings-notify")
    async def notify_settings_post(request: Request):
        s, form = await checked(request, "admin")
        action = form.get("action", "save")
        n = hub.notifier
        if action == "test":
            status = await n.send("SparkHub: test", f"Test notification sent by {s.username} from the Settings page.",
                                  "default", force=True)
            if status is None or status >= 300:
                why = "no topic set" if not n.config()["topic"] else f"ntfy answered {status or 'nothing'}"
                return RedirectResponse(f"/settings?tab=notifications&error=Test not delivered: {why}", status_code=303)
            return RedirectResponse("/settings?tab=notifications&saved=notifications (test sent)", status_code=303)
        if action == "clear":
            n.clear_topic()
            db.event("ntfy_settings_changed", f"user:{s.username}", topic="cleared")
            return RedirectResponse("/settings?tab=notifications&saved=notifications", status_code=303)
        topic = str(form.get("topic") or "").strip()
        try:
            n.save("enabled" in form, "high_only" in form, str(form.get("server") or "https://ntfy.sh"), topic)
        except ValueError as e:
            return RedirectResponse(f"/settings?tab=notifications&error={str(e)[:200]}", status_code=303)
        db.event("ntfy_settings_changed", f"user:{s.username}", enabled="enabled" in form,
                 high_only="high_only" in form, topic="changed" if topic else "kept")
        return RedirectResponse("/settings?tab=notifications&saved=notifications", status_code=303)

    @app.post("/settings-updates")
    async def updates_settings_post(request: Request):
        s, form = await checked(request, "admin")
        if form.get("action") == "check":
            latest = await hub.updates.check() or {}
            if latest.get("error"):
                return RedirectResponse(f"/settings?tab=updates&error=Check failed: {latest['error'][:200]}",
                                        status_code=303)
            return RedirectResponse("/settings?tab=updates&saved=checked", status_code=303)
        on = "enabled" in form
        db.set_setting("update_check", on)
        db.event("update_check_changed", f"user:{s.username}", enabled=on)
        return RedirectResponse(f"/settings?tab=updates&saved=update check {'on' if on else 'off'}", status_code=303)

    # ------------------------------------------------------------------ benchmarks (bench.py)
    def _bench_ctx(**extra) -> dict[str, Any]:
        b = hub.bench
        modes = b.available_modes()
        return dict(bench=b, active=b.active(), upcoming=b.upcoming(), history=b.rows(),
                    compare=b.compare(), suites=BENCH_SUITES, always=BENCH_ALWAYS, optional=BENCH_OPTIONAL,
                    bench_modes=modes, labels={m: hub.presets[m].title for m in modes},
                    estimates={m: b.estimate(m, b.normalize([])) for m in modes},
                    loaded=hub.modes.loaded, **extra)

    @app.get("/bench", response_class=HTMLResponse)
    async def bench_page(request: Request, error: str | None = None, msg: str | None = None):
        s = user(request, "admin")
        return page(request, "bench.html", s, error=error, msg=msg, **_bench_ctx())

    @app.get("/ui/bench", response_class=HTMLResponse)
    async def bench_partial(request: Request):
        s = user(request, "admin")
        return page(request, "_bench_live.html", s, **_bench_ctx())

    @app.post("/bench/start")
    async def bench_start(request: Request):
        s, form = await checked(request, "admin")
        try:
            start_at = None
            if form.get("when") == "at":
                raw = str(form.get("at") or "")
                try:
                    start_at = time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M"))
                except ValueError:
                    raise BenchError("give a start date and time")
            suites = [str(v) for v in (await request.form()).getlist("suite")]
            rid = hub.bench.schedule(str(form.get("mode") or ""), suites, form.get("internet") == "on", start_at,
                                     f"user:{s.username}")
        except BenchError as e:
            return _to("/bench", error=str(e))
        r = hub.bench.row(rid)
        when = "starts now" if r["start_at"] <= time.time() + 30 else f"starts {_ts(r['start_at'])}"
        return _to("/bench", msg=f"Benchmark #{rid} {when}; about {_span(r['est_s'])}.")

    @app.post("/bench/{rid}/cancel")
    async def bench_cancel(request: Request, rid: int):
        s, _ = await checked(request, "admin")
        try:
            await hub.bench.cancel(rid, f"user:{s.username}")
        except BenchError as e:
            return _to("/bench", error=str(e))
        return _to("/bench", msg=f"Benchmark #{rid} cancelled.")

    @app.get("/bench/{rid}", response_class=HTMLResponse)
    async def bench_run_page(request: Request, rid: int):
        s = user(request, "admin")
        r = hub.bench.row(rid)
        if r is None:
            return _to("/bench", error="No such benchmark run.")
        return page(request, "bench_run.html", s, r=r, h=hub.bench.headline(r["results"]), suites=BENCH_SUITES,
                    summary=hub.bench.summary_line(r["results"]))

    # ------------------------------------------------------------------ custom mode (recipes, custom.py)
    def _to(url: str, **q: str) -> RedirectResponse:
        qs = "&".join(f"{k}={quote(str(v)[:300])}" for k, v in q.items() if v)
        return RedirectResponse(url + ("?" + qs if qs else ""), status_code=303)

    @app.get("/custom")
    async def custom_page(request: Request):
        # the Custom section now lives on the Models page; old links and the redirects below land there
        user(request, "admin")
        q = request.url.query
        return RedirectResponse("/switch" + ("?" + q if q else "") + "#custom", status_code=303)

    @app.post("/custom/import")
    async def custom_import(request: Request):
        s, form = await checked(request, "admin")
        actor = f"user:{s.username}"
        upload = form.get("file")
        try:
            if form.get("registry"):
                rid = await hub.custom.import_registry(str(form["registry"]), str(form.get("path") or ""), actor)
            elif str(form.get("url") or "").strip():
                rid = await hub.custom.import_url(str(form["url"]), actor)
            elif hasattr(upload, "read") and getattr(upload, "filename", ""):
                raw = await upload.read(300 * 1024)
                rid = await hub.custom.import_text(raw.decode("utf-8", "replace"), actor)
            elif str(form.get("yaml") or "").strip():
                rid = await hub.custom.import_text(str(form["yaml"]), actor)
            else:
                raise RecipeError("give a recipe URL, a file or the YAML")
        except (RecipeError, CustomError) as e:
            return _to("/custom", error=f"Import refused: {e}")
        except httpx.HTTPError as e:
            return _to("/custom", error=f"Import failed: {type(e).__name__} (network?)")
        return RedirectResponse(f"/custom/{rid}?msg=imported", status_code=303)

    @app.post("/custom/registries")
    async def custom_registries(request: Request):
        s, form = await checked(request, "admin")
        actor = f"user:{s.username}"
        action, url = form.get("action"), str(form.get("url") or "").strip()
        try:
            regs = hub.custom.registries()
            plain = [{k: r[k] for k in ("url", "name", "enabled")} for r in regs]
            if action == "add":
                hub.custom.add_registry(url, actor)
                await hub.custom.refresh_registry(f"https://github.com/{url.split('github.com/')[-1].strip('/')}"
                                                  if "github.com/" in url else url)
            elif action in ("enable", "disable"):
                for r in plain:
                    if r["url"] == url:
                        r["enabled"] = action == "enable"
                hub.custom.set_registries(plain, actor)
                if action == "enable":
                    await hub.custom.refresh_registry(url)
            elif action == "remove":
                hub.custom.set_registries([r for r in plain if r["url"] != url], actor)
            elif action == "refresh":
                await hub.custom.refresh_registries(force=True)
        except (RecipeError, CustomError) as e:
            return _to("/custom", error=str(e))
        return RedirectResponse("/switch#browse", status_code=303)

    @app.post("/custom/check-updates")
    async def custom_check_updates(request: Request):
        s, _ = await checked(request, "admin")
        n = await hub.custom.check_updates()
        return _to("/custom", msg=f"Checked for updates: {n} new." if n else "Checked for updates: none.")

    @app.get("/custom/{rid}", response_class=HTMLResponse)
    async def recipe_page(request: Request, rid: int, error: str | None = None, msg: str | None = None):
        s = user(request, "admin")
        d = hub.custom.row(rid)
        if not d:
            return _to("/custom", error="No such recipe.")
        loaded_here = hub.modes.loaded == "custom" and "custom" in hub.presets and \
            hub.presets["custom"].raw.get("recipe_id") == rid and hub.modes.state == "ready"
        return page(request, "recipe.html", s, d=d, error=error, msg=msg, loaded_here=loaded_here,
                    custom_busy=hub.custom.custom_busy())

    @app.get("/ui/custom/{rid}", response_class=HTMLResponse)
    async def recipe_status(request: Request, rid: int):
        s = user(request, "admin")
        d = hub.custom.row(rid)
        if not d:
            return HTMLResponse("")
        return page(request, "_recipe_status.html", s, d=d, custom_busy=hub.custom.custom_busy())

    @app.post("/custom/{rid}/{action}")
    async def recipe_action(request: Request, rid: int, action: str):
        s, form = await checked(request, "admin")
        actor = f"user:{s.username}"
        here = f"/custom/{rid}"
        d = hub.custom.row(rid)
        if not d:
            return _to("/custom", error="No such recipe.")
        try:
            if action == "approve":
                if str(form.get("ack") or "") != "on":
                    raise CustomError("tick the box to confirm you went through the review")
                err = auth.confirm_totp(s, str(form.get("code") or "")[:16], client_ip(request), "approve recipe")
                if err:
                    return _to(here, error=err)
                hub.custom.approve(rid, actor)
                return _to(here, msg="Approved. Next: download and verify.")
            if action == "download":
                hub.custom.start_download(rid, actor)
                return _to(here, msg="Download started.")
            if action == "assign":
                hub.custom.assign(rid, actor)
                return _to(here, msg="Assigned to Custom. Load it from the Models page.")
            if action == "unassign":
                hub.custom.unassign(actor)
                return _to(here, msg="Custom has no recipe now.")
            if action == "delete":
                if str(form.get("confirm") or "").strip() != d["name"]:
                    raise CustomError(f"type {d['name']} to confirm")
                await hub.custom.delete(rid, actor)
                return _to("/custom", msg=f"Deleted {d['name']}.")
            if action == "smoke":
                res = await hub.custom.smoke(rid, actor)
                bad = [k for k, v in res.items() if v["ok"] is False]
                return _to(here, msg="Smoke checks passed." if not bad else None,
                             error=f"Smoke checks failed: {', '.join(bad)}" if bad else None)
            if action == "update":
                new = await hub.custom.import_update(rid, actor)
                return RedirectResponse(f"/custom/{new}?msg=imported", status_code=303)
        except (CustomError, RecipeError) as e:
            return _to(here, error=str(e))
        except httpx.HTTPError as e:
            return _to(here, error=f"{type(e).__name__} (network?)")
        return _to(here)

    # ------------------------------------------------------------------ logs
    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, mode: str | None = None, tail: int = 300):
        s = user(request, "admin")
        mode = mode if mode in hub.presets else (hub.modes.loaded or hub.modes.target or "fast")
        text = await hub.runtime.logs(mode, hub.presets[mode].container, tail=max(20, min(tail, 5000)))
        return page(request, "logs.html", s, mode=mode, tail=tail, text=text, modes=hub.presets.available)

    # ------------------------------------------------------------------ keys
    # everyone manages their own keys, with scopes up to their role; an admin also sees and revokes everyone's
    # (and the system keys made with the CLI, which belong to nobody)
    def _keys_ctx(s: Session, show_dead: bool = False, **extra) -> dict[str, Any]:
        now = time.time()
        keys = keymod.list_keys(db, None if s.is_admin else s.user_id)
        for k in keys:
            k["dead"] = bool(k["revoked_at"] or (k["expires_at"] and k["expires_at"] < now))
        n_dead = sum(k["dead"] for k in keys)
        if not show_dead:          # revoked and expired keys: hidden unless asked for
            keys = [k for k in keys if not k["dead"]]
        return dict(keys=keys, n_dead=n_dead, show_dead=show_dead, scopes=keymod.scopes_up_to(s.role), now=now,
                    **extra)

    @app.get("/keys", response_class=HTMLResponse)
    async def keys_page(request: Request, revoked: int = 0):
        s = user(request)
        return page(request, "keys.html", s, **_keys_ctx(s, bool(revoked)))

    @app.post("/keys", response_class=HTMLResponse)
    async def keys_create(request: Request):
        s, form = await checked(request)
        try:
            exp = float(form["expires_days"]) if form.get("expires_days") else None
            cap = int(form["max_concurrency"]) if form.get("max_concurrency") else None
            if cap is not None and not 1 <= cap <= 16:
                raise ValueError("max concurrency must be 1-16")
            kid, raw = keymod.create_key(db, str(form.get("name", "")), str(form.get("scope", "infer")),
                                         f"user:{s.username}", exp, cap, user_id=s.user_id)
        except (ValueError, KeyError) as e:
            return page(request, "keys.html", s, **_keys_ctx(s, error=str(e)))
        return page(request, "keys.html", s, **_keys_ctx(s, new_key=raw, new_name=form.get("name")))

    @app.post("/keys/{key_id}/revoke")
    async def keys_revoke(key_id: int, request: Request):
        s, _ = await checked(request)
        keymod.revoke_key(db, key_id, f"user:{s.username}", None if s.is_admin else s.user_id)
        return RedirectResponse("/keys", status_code=303)

    # ------------------------------------------------------------------ account (any user, about themselves)
    LOGIN_EVENTS = ("'login','login_failed','account_locked','logout','totp_enrolled','totp_reset','password_reset',"
                    "'password_changed','recovery_codes_regenerated','user_suspended','user_unsuspended',"
                    "'user_unlocked','role_changed','session_ended','user_created','user_deleted'")

    def _sessions(where: str = "", params: tuple = (), current: str = "") -> list[dict[str, Any]]:
        return [dict(r) for r in db.all(
            "SELECT s.rowid AS rid, s.created_at, s.last_seen, s.expires_at, s.ip, s.ua, s.stage, s.remember,"
            f" u.username, s.id = ? AS current FROM sessions s JOIN users u ON u.id=s.user_id {where}"
            " ORDER BY s.last_seen DESC", (current, *params))]

    def _login_events(username: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM events WHERE kind IN ({LOGIN_EVENTS})"
        params: tuple = ()
        if username is not None:       # their own: as the actor, or as the subject of an admin's action
            sql += " AND (actor=? OR json_extract(detail, '$.username')=?)"
            params = (f"user:{username}", username)
        rows = [dict(r) for r in db.all(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]
        for e in rows:
            e["detail"] = json.loads(e["detail"] or "{}")
        return rows

    @app.get("/account", response_class=HTMLResponse)
    async def account_page(request: Request, msg: str | None = None, error: str | None = None):
        s = user(request)
        me = dict(db.one("SELECT username, role, totp_enabled, recovery, last_login_at, created_at FROM users"
                         " WHERE id=?", (s.user_id,)))
        me["recovery_left"] = len(json.loads(me.pop("recovery") or "[]"))
        return page(request, "account.html", s, me=me, msg=msg, error=error,
                    sessions=_sessions("WHERE s.user_id=?", (s.user_id,), s.id), logins=_login_events(s.username))

    @app.post("/sessions/{rid}/end")
    async def end_session(rid: int, request: Request):
        s, form = await checked(request)
        row = db.one("SELECT s.id, s.user_id, u.username FROM sessions s JOIN users u ON u.id=s.user_id"
                     " WHERE s.rowid=?", (rid,))
        if row and (row["user_id"] == s.user_id or s.is_admin):     # one's own, or an admin: anyone's
            db.execute("DELETE FROM sessions WHERE rowid=?", (rid,))
            db.event("session_ended", f"user:{s.username}", username=row["username"])
        back = "/users" if form.get("back") == "users" and s.is_admin else "/account"
        return RedirectResponse("/login" if row and row["id"] == s.id else back + "?msg=session+ended",
                                status_code=303)

    @app.post("/account/password")
    async def account_password(request: Request):
        s, form = await checked(request)
        uid, err = auth.check_password(s.username, str(form.get("current", "")), client_ip(request))
        if uid is None:
            return RedirectResponse("/account?error=current+password+is+wrong", status_code=303)
        if form.get("new") != form.get("again"):
            return RedirectResponse("/account?error=the+new+passwords+differ", status_code=303)
        from .auth import set_password
        try:
            set_password(db, s.username, str(form.get("new", "")), f"user:{s.username}")
        except ValueError as e:
            return RedirectResponse(f"/account?error={e}", status_code=303)
        return RedirectResponse("/login", status_code=303)

    @app.post("/account/recovery", response_class=HTMLResponse)
    async def account_recovery(request: Request):
        s, _ = await checked(request)
        from .auth import new_recovery_codes
        codes = new_recovery_codes(db, s.user_id)
        db.event("recovery_codes_regenerated", f"user:{s.username}")
        return page(request, "recovery.html", s, codes=codes, first=False)

    # ------------------------------------------------------------------ users (admins)
    @app.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request, msg: str | None = None, error: str | None = None):
        s = user(request, "admin")
        users = [dict(r) for r in db.all(
            "SELECT u.id, u.username, u.role, u.disabled, u.must_change_pw, u.totp_enabled, u.failed_count,"
            " u.locked_until, u.last_login_at, u.created_at, u.recovery,"
            " (SELECT COUNT(*) FROM api_keys k WHERE k.user_id=u.id AND k.revoked_at IS NULL"
            "  AND (k.expires_at IS NULL OR k.expires_at > ?)) AS n_keys,"
            " (SELECT MAX(ts) FROM activity a WHERE a.user_id=u.id) AS last_request"
            " FROM users u ORDER BY u.id", (time.time(),))]
        for u in users:
            u["recovery_left"] = len(json.loads(u.pop("recovery") or "[]"))
            u["locked"] = bool(u["locked_until"] and u["locked_until"] > time.time())
        return page(request, "users.html", s, users=users, sessions=_sessions(current=s.id), logins=_login_events(),
                    roles=keymod.SCOPES, msg=msg, error=error)

    def _active_admins(excluding: int) -> int:
        return db.one("SELECT COUNT(*) n FROM users WHERE role='admin' AND disabled=0 AND id!=?", (excluding,))["n"]

    def _back(msg: str = "", error: str = "") -> RedirectResponse:
        from urllib.parse import urlencode
        return RedirectResponse("/users?" + urlencode({"error": error} if error else {"msg": msg}), status_code=303)

    @app.post("/users")
    async def users_add(request: Request):
        s, form = await checked(request, "admin")
        from .auth import create_user
        name = str(form.get("username", "")).strip()
        try:
            if form.get("password") != form.get("again"):
                raise ValueError("the two passwords differ")
            create_user(db, name, str(form.get("password", "")), str(form.get("role", "infer")), must_change=True,
                        actor=f"user:{s.username}")
        except ValueError as e:
            return _back(error=f"Not added: {e}")
        return _back(f"{name} added. They choose their own password and set up 2FA at their first login.")

    @app.post("/users/{uid}/{action}")
    async def users_action(uid: int, action: str, request: Request):
        s, form = await checked(request, "admin")
        from . import auth as authmod
        row = db.one("SELECT * FROM users WHERE id=?", (uid,))
        if row is None:
            return _back(error="No such user.")
        name, actor = row["username"], f"user:{s.username}"
        own = uid == s.user_id
        if own and action in ("role", "suspend", "delete"):
            return _back(error="You can't change your own role, suspend or delete yourself (another admin can).")
        if action == "role":
            role = str(form.get("role", ""))
            if role not in keymod.SCOPES:
                return _back(error="Unknown role.")
            if row["role"] == "admin" and role != "admin" and not _active_admins(uid):
                return _back(error="That is the last admin.")
            db.execute("UPDATE users SET role=?, is_admin=? WHERE id=?", (role, int(role == "admin"), uid))
            db.event("role_changed", actor, username=name, role=role)
            return _back(f"{name} is now {keymod.ROLE_LABELS[role]}. Their keys above that work at the {role} scope.")
        if action == "suspend":
            db.execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))
            authmod.end_sessions(db, name)
            db.event("user_suspended", actor, username=name)
            return _back(f"{name} suspended: logged out, can't log in, their keys don't work.")
        if action == "unsuspend":
            db.execute("UPDATE users SET disabled=0 WHERE id=?", (uid,))
            db.event("user_unsuspended", actor, username=name)
            return _back(f"{name} can log in again; their keys work again.")
        if action == "delete":
            if str(form.get("confirm", "")).strip() != name:
                return _back(error=f"Not deleted: type the username ({name}) to confirm.")
            n = db.execute("UPDATE api_keys SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                           (time.time(), uid)).rowcount
            authmod.end_sessions(db, name)
            db.execute("DELETE FROM users WHERE id=?", (uid,))
            db.event("user_deleted", actor, username=name, keys_revoked=n)
            return _back(f"{name} deleted; {n} key(s) revoked. Their activity stays in the log.")
        if action == "password":
            try:
                if form.get("password") != form.get("again"):
                    raise ValueError("the two passwords differ")
                authmod.set_password(db, name, str(form.get("password", "")), actor, must_change=not own)
            except ValueError as e:
                return _back(error=f"Password not set: {e}")
            if own:
                return RedirectResponse("/login", status_code=303)
            return _back(f"Temporary password set for {name}; they choose their own at the next login.")
        if action == "totp":
            authmod.reset_totp(db, name, actor)
            if own:
                return RedirectResponse("/login", status_code=303)
            return _back(f"2FA cleared for {name}; they set it up again at the next login.")
        if action == "unlock":
            db.execute("UPDATE users SET failed_count=0, locked_until=NULL WHERE id=?", (uid,))
            db.event("user_unlocked", actor, username=name)
            return _back(f"{name} unlocked.")
        return _back(error="Unknown action.")

    # ------------------------------------------------------------------ activity
    @app.get("/activity", response_class=HTMLResponse)
    async def activity_page(request: Request, hours: float = 24):
        s = user(request)
        since = time.time() - hours * 3600
        mine, params = ("", ()) if s.is_admin else (" AND user_id = ?", (s.user_id,))   # a user's own; admins: all
        summary = [dict(r) for r in db.all(
            "SELECT mode, COUNT(*) n, SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(cached_tokens) cached,"
            " AVG(ttft) ttft, AVG(gen_tps) tps, SUM(outcome='error') errors, SUM(outcome='disconnected') disc,"
            " SUM(outcome='length') length, SUM(outcome='refused') refused, SUM(thinking=1) think"
            f" FROM activity WHERE ts >= ?{mine} GROUP BY mode ORDER BY mode", (since, *params))]
        rows = [dict(r) for r in db.all(f"SELECT * FROM activity WHERE 1=1{mine} ORDER BY id DESC LIMIT 200",
                                        params)]
        events = []
        if s.is_admin:
            events = [dict(r) for r in db.all("SELECT * FROM events ORDER BY id DESC LIMIT 100")]
            for e in events:
                e["detail"] = json.loads(e["detail"] or "{}")
        return page(request, "activity.html", s, summary=summary, rows=rows, events=events, hours=hours)

    # ------------------------------------------------------------------ connecting clients
    @app.get("/connect", response_class=HTMLResponse)
    async def connect_page(request: Request):
        s = user(request)
        host = request.headers.get("host", cfg.public_host).split(":")[0]
        # the local CA matters on the names it serves: every name but a public name with a trusted certificate
        trusted = host == cfg.public_host and cfg.public_cert_trusted
        needs_ca = not trusted and (cfg.data_dir / "tls" / "ca.crt").exists()
        return page(request, "connect.html", s, base=f"https://{host}", has_ca=needs_ca, embed=hub.embed.status(),
                    public_base=f"https://{cfg.public_host}", public_trusted=cfg.public_cert_trusted)

