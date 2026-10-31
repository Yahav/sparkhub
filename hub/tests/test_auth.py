"""Dashboard login: password + TOTP enrolment, recovery codes, lockout, CSRF, sessions, auth_request, the host CLI."""
from __future__ import annotations

import re
import threading
import time

import pyotp

from sparkhub import admin_cli
from sparkhub.auth import create_user

PW = "correct horse battery"


def login_csrf(c) -> str:
    r = c.get("/login")
    return r.cookies.get("sparkhub_lc") or c.cookies.get("sparkhub_lc")


def password_step(c, user="admin", pw=PW):
    return c.post("/login", data={"username": user, "password": pw, "csrf": login_csrf(c)}, follow_redirects=False)


def secret(hub, user="admin"):
    return hub.db.one("SELECT totp_secret FROM users WHERE username=?", (user,))["totp_secret"]


def enrolled_client(hub):
    """A logged-in client after first-login enrolment. Returns (client, recovery codes)."""
    create_user(hub.db, "admin", PW)
    c = hub.client()
    r = password_step(c)
    assert r.status_code == 303 and r.headers["location"] == "/login/enroll"
    r = c.get("/login/enroll")
    import base64
    b64 = re.search(r'src="data:image/svg\+xml;base64,([^"]+)"', r.text).group(1)
    svg = base64.b64decode(b64).decode()
    assert svg.startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in svg   # renders as an <img>
    code = pyotp.TOTP(secret(hub)).now()
    r = c.post("/login/enroll", data={"code": code, "csrf": c.cookies.get("sparkhub_lc")})
    codes = re.findall(r"<code>([0-9a-f]{6}-[0-9a-f]{6})</code>", r.text)
    assert len(codes) == 10
    return c, codes


def csrf_of(c) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/keys").text).group(1)


def test_pages_need_login(hub):
    with hub.client() as c:
        for path in ("/", "/switch", "/settings", "/keys", "/users", "/activity", "/logs", "/ui/live"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login", path
        assert c.get("/auth/check").status_code == 401


def test_enrol_login_and_pages(hub):
    c, codes = enrolled_client(hub)
    r = c.get("/")
    assert r.status_code == 200 and "Mode" in r.text
    assert "Content-Security-Policy" in r.headers
    for path in ("/switch", "/settings", "/keys", "/users", "/activity", "/logs", "/ui/live", "/ui/switch", "/connect"):
        assert c.get(path).status_code == 200, path
    assert c.get("/ui/series").json()["t"] is not None
    assert c.get("/report").status_code == 404 and ">Benchmark<" not in r.text   # the report page is gone (docs/)
    r = c.get("/auth/check")
    assert r.status_code == 204 and r.headers["x-auth-user"] == "admin"
    # the session also works for the control API, with the CSRF header on writes
    assert c.get("/api/v1/status").status_code == 200
    assert c.post("/api/v1/keys", json={"name": "x"}).status_code == 403
    tok = csrf_of(c)
    assert c.post("/api/v1/keys", json={"name": "x"}, headers={"X-CSRF-Token": tok}).status_code == 201


def test_second_login_with_totp_and_replay(hub):
    c, _ = enrolled_client(hub)
    c.close()
    c = hub.client()
    r = password_step(c)
    assert r.headers["location"] == "/login/totp"
    assert c.get("/", follow_redirects=False).headers["location"] == "/login/totp"
    # the code used at enrolment can't be replayed
    totp = pyotp.TOTP(secret(hub))
    r = c.post("/login/totp", data={"code": totp.now(), "csrf": c.cookies.get("sparkhub_lc")}, follow_redirects=False)
    assert r.status_code == 200 and "Wrong code" in r.text
    # the next step's code works
    nxt = totp.at(time.time() + 30)
    r = c.post("/login/totp", data={"code": nxt, "csrf": c.cookies.get("sparkhub_lc")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/").status_code == 200


def test_recovery_code_works_once(hub):
    c, codes = enrolled_client(hub)
    c.close()
    for attempt in (1, 2):
        c = hub.client()
        password_step(c)
        r = c.post("/login/totp", data={"code": codes[0], "csrf": c.cookies.get("sparkhub_lc")},
                   follow_redirects=False)
        if attempt == 1:
            assert r.status_code == 303
            assert c.get("/").status_code == 200
        else:
            assert r.status_code == 200 and "Wrong code" in r.text
        c.close()


def test_five_wrong_passwords_lock(hub):
    create_user(hub.db, "admin", PW)
    with hub.client() as c:
        for i in range(5):
            r = password_step(c, pw="wrong password!")
            assert r.status_code == 200
        assert "Too many failed attempts" in r.text
        # even the right password is refused while locked
        r = password_step(c)
        assert r.status_code == 200 and "Too many failed attempts" in r.text
    u = hub.db.one("SELECT locked_until FROM users WHERE username='admin'")
    assert u["locked_until"] > time.time() + 800
    kinds = [r["kind"] for r in hub.db.all("SELECT kind FROM events")]
    assert kinds.count("login_failed") >= 5 and "account_locked" in kinds
    assert 'sparkhub_lockouts_total 1.0' in hub.client().get("/metrics").text
    # an unknown username gets the same treatment
    with hub.client() as c:
        for i in range(5):
            r = password_step(c, user="ghost", pw="whatever12345")
        assert "Too many failed attempts" in r.text


def test_wrong_totp_counts_toward_lockout(hub):
    c, _ = enrolled_client(hub)
    c.close()
    c = hub.client()
    password_step(c)
    for i in range(5):
        r = c.post("/login/totp", data={"code": "000000", "csrf": c.cookies.get("sparkhub_lc")})
    assert "locked" in r.text
    assert hub.db.one("SELECT locked_until FROM users")["locked_until"] > time.time()


def test_csrf_required_on_dashboard_posts(hub):
    c, _ = enrolled_client(hub)
    r = c.post("/keys", data={"name": "evil", "scope": "admin"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.post("/keys", data={"name": "good", "scope": "infer", "csrf": csrf_of(c)})
    assert r.status_code == 200 and "shk_" in r.text
    names = [k["name"] for k in hub.db.all("SELECT name FROM api_keys")]
    assert "good" in names and "evil" not in names


def test_login_form_needs_its_csrf_cookie(hub):
    create_user(hub.db, "admin", PW)
    with hub.client() as c:
        r = c.post("/login", data={"username": "admin", "password": PW, "csrf": "forged"}, follow_redirects=False)
        assert r.status_code == 200 and "Form expired" in r.text


def test_switch_and_settings_from_dashboard(hub):
    c, _ = enrolled_client(hub)
    tok = csrf_of(c)
    r = c.post("/settings/quality", data={"csrf": tok, "max_model_len": "65536", "max_num_seqs": "4",
                                           "gpu_memory_utilization": "0.78", "mtp": "2", "prefix_caching": "on",
                                           "kv_cache_dtype": "auto", "tool_call_parser": "qwen3_coder",
                                           "default_max_tokens": "16000"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=quality" in r.headers["location"] and "tab=models" in r.headers["location"]
    assert hub.hub.presets.settings("quality")["max_model_len"] == 65536
    # Settings is tabbed: the chosen tab's panel is shown, the others are hidden, an unknown tab falls back to Models
    page = c.get("/settings?tab=hub").text
    assert '<section class="tabpanel" aria-labelledby="tab-hub">' in page
    assert '<section class="tabpanel" aria-labelledby="tab-models" hidden>' in page
    assert 'href="/settings?tab=hub" id="tab-hub" aria-current="page"' in page
    assert '<section class="tabpanel" aria-labelledby="tab-models">' in c.get("/settings?tab=bogus").text
    # a setting the Quality recipe has no flag for is shown as not used, not as an input
    assert page.count('name="gpu_memory_utilization"') == 1 and "not used" in page
    r = c.post("/ui/switch", data={"mode": "quality", "action": "switch"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    hub.wait_state("ready", "quality")
    args = hub.runtime.started[-1]["args"]
    assert args[args.index("--max-model-len") + 1] == "65536"
    ev = hub.db.all("SELECT actor FROM events WHERE kind='switch' ORDER BY id DESC LIMIT 1")[0]
    assert ev["actor"] == "user:admin"


def test_logout_ends_session(hub):
    c, _ = enrolled_client(hub)
    tok = csrf_of(c)
    c.post("/logout", data={"csrf": tok})
    assert c.get("/", follow_redirects=False).status_code == 303
    assert hub.db.one("SELECT COUNT(*) n FROM sessions")["n"] == 0


def test_password_change_ends_sessions(hub):
    c, _ = enrolled_client(hub)
    tok = csrf_of(c)
    r = c.post("/account/password", data={"csrf": tok, "current": PW, "new": "another long password",
                                          "again": "another long password"}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert c.get("/", follow_redirects=False).status_code == 303


def test_admin_cli(hub, monkeypatch, capsys):
    """Host CLI: create the first admin, reset password / TOTP, unlock, revoke all keys, from the host."""
    monkeypatch.setenv("SPARKHUB_DATA_DIR", str(hub.cfg.data_dir))
    assert admin_cli.main(["user", "create", "admin", "--password", PW]) == 0
    hub.db.execute("UPDATE users SET locked_until=? WHERE username='admin'", (time.time() + 999,))
    assert admin_cli.main(["user", "unlock", "admin"]) == 0
    assert hub.db.one("SELECT locked_until FROM users")["locked_until"] is None
    assert admin_cli.main(["user", "reset-password", "admin", "--password", "a new long password"]) == 0
    assert admin_cli.main(["user", "reset-totp", "admin"]) == 0
    assert admin_cli.main(["key", "create", "laptop", "--scope", "switch"]) == 0
    assert "shk_" in capsys.readouterr().out
    assert admin_cli.main(["key", "revoke-all"]) == 0
    assert hub.db.one("SELECT COUNT(*) n FROM api_keys WHERE revoked_at IS NULL")["n"] == 0
    with hub.client() as c:
        r = password_step(c, pw="a new long password")
        assert r.headers["location"] == "/login/enroll"


def test_pages_render_while_switching(hub):
    """No model loaded (switch in progress): every page still renders."""
    c, _ = enrolled_client(hub)
    hub.runtime.load_delay = 2.0
    c.post("/ui/switch", data={"mode": "quality", "action": "switch"}, headers={"X-CSRF-Token": csrf_of(c)})
    time.sleep(0.5)
    for path in ("/", "/ui/live", "/switch", "/ui/switch", "/logs", "/settings"):
        assert c.get(path).status_code == 200, path
    hub.wait_state("ready", "quality")


def test_next_redirect_after_login(hub):
    c, _ = enrolled_client(hub)
    c.close()
    c = hub.client()
    c.get("/login", params={"next": "/grafana/d/sparkhub"})
    password_step(c)
    nxt = pyotp.TOTP(secret(hub)).at(time.time() + 30)
    r = c.post("/login/totp", data={"code": nxt, "csrf": c.cookies.get("sparkhub_lc")}, follow_redirects=False)
    assert r.headers["location"] == "/grafana/d/sparkhub"
    from sparkhub.web import safe_next
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "evil", "/a b"):
        assert safe_next(bad) is None, bad


def test_connect_page_ca_step_only_off_the_public_name(hub):
    c, _ = enrolled_client(hub)
    (hub.cfg.data_dir / "tls").mkdir(parents=True, exist_ok=True)
    (hub.cfg.data_dir / "tls" / "ca.crt").write_text("x")
    assert "ca.crt" not in c.get("/connect", headers={"Host": "hub.example"}).text
    assert "ca.crt" in c.get("/connect", headers={"Host": "hub.tailnet.example"}).text
    # local or tailscale access with the local CA: the public name needs it too, and is checked against it
    hub.cfg.public_cert_trusted = False
    assert "ca.crt" in c.get("/connect", headers={"Host": "hub.example"}).text
    ca = str(hub.cfg.data_dir / "tls" / "ca.crt")
    assert hub.cfg.tls_check_names() == [("hub.example", ca), ("hub.tailnet.example", ca)]
    hub.cfg.public_cert_trusted = True
    assert hub.cfg.tls_check_names() == [("hub.example", None), ("hub.tailnet.example", ca)]


def _login(hub, remember: bool):
    c = hub.client()
    lc = login_csrf(c)
    data = {"username": "admin", "password": PW, "csrf": lc}
    if remember:
        data["remember"] = "1"
    c.post("/login", data=data, follow_redirects=False)
    hub.db.execute("UPDATE users SET totp_last_step=NULL")   # each test login may reuse the next step's code
    nxt = pyotp.TOTP(secret(hub)).at(time.time() + 30)
    r = c.post("/login/totp", data={"code": nxt, "csrf": lc}, follow_redirects=False)
    assert r.status_code == 303, r.text[:200]
    return c, r


def test_session_cookie_outlives_prelogin_and_remember_me(hub):
    c, _ = enrolled_client(hub)
    c.close()
    # without "remember": a browser-session cookie (no Max-Age), not the 10-minute pre-login one
    c, r = _login(hub, remember=False)
    sc = [h for h in r.headers.get_list("set-cookie") if h.startswith("sparkhub_session=")]
    assert sc and "max-age" not in sc[-1].lower(), sc
    row = hub.db.one("SELECT remember, expires_at FROM sessions WHERE stage='full' ORDER BY created_at DESC")
    assert row["remember"] == 0 and 6.9 * 86400 < row["expires_at"] - time.time() <= 7 * 86400
    # with "remember": a 30-day cookie, and no 12-hour idle limit
    c2, r = _login(hub, remember=True)
    sc = [h for h in r.headers.get_list("set-cookie") if h.startswith("sparkhub_session=")]
    assert sc and "max-age=2592000" in sc[-1].lower(), sc
    row = hub.db.one("SELECT rowid, remember, expires_at FROM sessions WHERE remember=1")
    assert 29.9 * 86400 < row["expires_at"] - time.time() <= 30 * 86400
    hub.db.execute("UPDATE sessions SET last_seen=? WHERE remember=1", (time.time() - 2 * 86400,))
    assert c2.get("/", follow_redirects=False).status_code == 200          # 2 days idle: still in
    hub.db.execute("UPDATE sessions SET last_seen=? WHERE remember=0 AND stage='full'", (time.time() - 13 * 3600,))
    assert c.get("/", follow_redirects=False).status_code == 303           # 13 h idle without remember: out
    # ending the remembered session from the Users page logs that device out
    tok = csrf_of(c2)
    c2.post(f"/sessions/{row['rowid']}/end", data={"csrf": tok})
    assert c2.get("/", follow_redirects=False).status_code == 303


def test_overview_gauges_and_components(hub):
    c, _ = enrolled_client(hub)

    async def comps():
        return [{"name": "sparkhub-hub", "service": "hub", "model": False, "state": "running", "health": "healthy",
                 "status": "Up 3 hours (healthy)"},
                {"name": "sparkhub-arcane", "service": "arcane", "model": False, "state": "running",
                 "health": "starting", "status": "Up 5 seconds (health: starting)"},
                {"name": "sparkhub-grafana", "service": "grafana", "model": False, "state": "exited", "health": None,
                 "status": "Exited (1) 2 minutes ago"},
                {"name": "sparkhub-fast", "service": "fast", "model": True, "state": "running", "health": None,
                 "status": "Up 1 hour"}]
    hub.runtime.components = comps
    hub.hub._components = (0.0, None)
    r = c.get("/ui/live")
    assert r.status_code == 200
    html = r.text
    for label in ("CPU", "Memory", "GPU", "GPU temp", "KV cache"):
        assert f'aria-label="{label}"' in html
    for card in ("Mode", "Speed", "Requests", "Context", "Storage", "Processor", "Disk I/O"):
        assert f'<span class="c-lbl">{card}</span>' in html
    assert 'class="comp lv-ok" title="sparkhub-hub"' in html and "Up 3 hours<" in html
    assert 'class="comp lv-warn" title="sparkhub-arcane"' in html and "starting · Up 5 seconds" in html
    assert 'class="comp lv-err" title="sparkhub-grafana"' in html
    assert html.index("Model · fast") < html.index(">Hub<")           # the model first
    assert ">Containers<" not in html and html.count("<i class=\"lv-") == html.count("class=\"comp lv-")  # dots on the Services block
    assert "style=" not in html                                         # CSP: no inline styles
    # last answer, context and recent requests come from the activity table
    now = time.time()
    hub.db.execute("INSERT INTO activity (ts, key_name, mode, outcome, prompt_tokens, cached_tokens, completion_tokens, "
                   "ttft, gen_tps, duration) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (now, "claude-code", "fast", "finished", 34006, 33957, 5316, 0.5, 31.1, 171.4))
    html = c.get("/ui/live").text
    assert "34,006" in html and "33,957" in html and "5,316" in html and 'class="st st-done"' in html
    assert "<td>fast</td>" in html                                      # the Mode column
    assert "/v1</code>" in html
    # speed and context are the current ones: nothing runs, so dashes, not the last answer
    assert "nothing generating" in html and "no request running" in html and "39k" not in html
    # the header badge refreshes out of band on every poll
    assert 'id="modebadge"' in html and 'hx-swap-oob="true"' in html
    # hardware boxes: a bar each, no rings, no header meta
    hw = html[html.index('class="cards c5 hw"'):]
    hw = hw[:hw.index("</section>")]
    for label in ("GPU", "Memory", "CPU", "Storage"):
        assert f'class="progress' in hw and f'aria-label="{label}"' in hw
    assert 'class="ring' not in hw and "c-meta" not in hw and "bench" not in hw


def test_overview_current_context(hub):
    """Context shows what the running requests hold now: KV use x the KV size vLLM reports, per request."""
    c, _ = enrolled_client(hub)
    def chat():
        with hub.client(hub.admin_key) as k:
            k.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                                                 "max_tokens": 30, "fake_delay": 0.1})
    t = threading.Thread(target=chat)
    t.start()
    deadline = time.time() + 5
    html = ""
    while time.time() < deadline and "the running request" not in html:
        hub.hub._vllm_cache = (0.0, {})
        html = c.get("/ui/live").text
        time.sleep(0.1)
    t.join()
    assert "the running request" in html and "32k" in html             # 10 % of 320,000 tokens


def test_live_samples_survive_a_restart(hub):
    h = hub.hub
    now = time.time()
    h.samples.clear()
    h.samples.extend([{"t": now - 2000, "cpu_pct": 1.0}, {"t": now - 10, "cpu_pct": 42.0}])
    h.save_samples()
    h.samples.clear()
    h.load_samples()
    assert [s["cpu_pct"] for s in h.samples] == [42.0]          # older than 15 minutes: dropped
    h._samples_file.write_text("not json")
    h.samples.clear()
    h.load_samples()                                            # a broken file is ignored
    assert len(h.samples) == 0
