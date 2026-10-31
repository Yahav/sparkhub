"""User roles (admin, switcher, inference), the admin Users page, keys owned by users, per-user activity."""
from __future__ import annotations

import re
import sqlite3

import pyotp

from sparkhub import keys as keymod
from sparkhub.db import DB, SCHEMA

from .test_auth import PW, csrf_of, enrolled_client, login_csrf, password_step, secret

TEMP = "temporary password 1"
OWN = "my own password 123"


def add_user(admin, name, role):
    r = admin.post("/users", data={"csrf": csrf_of(admin), "username": name, "role": role, "password": TEMP,
                                   "again": TEMP}, follow_redirects=False)
    assert r.status_code == 303 and "added" in r.headers["location"], r.headers["location"]


def first_login(hub, name):
    """A new user's first login: temporary password, their own password, 2FA enrolment."""
    c = hub.client()
    r = password_step(c, name, TEMP)
    assert r.headers["location"] == "/login/password"
    # the second factor can't be reached before the new password
    assert c.get("/login/enroll", follow_redirects=False).headers["location"] == "/login/password"
    assert c.get("/", follow_redirects=False).headers["location"] == "/login/password"
    r = c.post("/login/password", data={"new": TEMP, "again": TEMP, "csrf": c.cookies.get("sparkhub_lc")})
    assert "different from the temporary one" in r.text
    r = c.post("/login/password", data={"new": OWN, "again": OWN, "csrf": c.cookies.get("sparkhub_lc")},
               follow_redirects=False)
    assert r.headers["location"] == "/login/enroll"
    c.get("/login/enroll")
    c.post("/login/enroll", data={"code": pyotp.TOTP(secret(hub, name)).now(), "csrf": c.cookies.get("sparkhub_lc")})
    assert c.get("/", follow_redirects=False).status_code == 200
    return c


def new_key(c, name, scope):
    r = c.post("/keys", data={"csrf": csrf_of(c), "name": name, "scope": scope})
    m = re.search(r'<code class="secret">([^<]+)</code>', r.text)
    return m.group(1) if m else r.text


def chat(hub, key):
    with hub.client(key) as k:
        return k.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                                                    "max_tokens": 5})


def test_roles_pages_and_nav(hub):
    admin, _ = enrolled_client(hub)
    add_user(admin, "ivy", "infer")
    add_user(admin, "sam", "switch")
    ivy, sam = first_login(hub, "ivy"), first_login(hub, "sam")
    nav = lambda c: re.findall(r'<nav>(.*?)</nav>', c.get("/").text, re.S)[0]
    assert all(x in nav(ivy) for x in ('href="/"', '"/activity"', '"/keys"', '"/connect"', '"/account"'))
    assert not any(x in nav(ivy) for x in ('"/switch"', '"/settings"', '"/users"', '"/logs"', "/grafana/", "/arcane/",
                                           "/nvidia/"))
    assert '"/switch"' in nav(sam) and '"/settings"' not in nav(sam)
    assert all(x in nav(admin) for x in ('"/settings"', '"/users"', '"/logs"', "/grafana/", "/arcane/", "/nvidia/"))
    hub.hub.cfg.arcane = hub.hub.cfg.nvidia_dashboard = False          # not installed: no header links
    try:
        assert "/arcane/" not in nav(admin) and "/nvidia/" not in nav(admin) and "/grafana/" in nav(admin)
    finally:
        hub.hub.cfg.arcane = hub.hub.cfg.nvidia_dashboard = True
    for path in ("/switch", "/ui/switch", "/settings", "/users", "/logs"):
        r = ivy.get(path)
        assert r.status_code == 403 and "Not available" in r.text, path
    for path in ("/", "/activity", "/keys", "/connect", "/account", "/ui/live"):
        assert ivy.get(path).status_code == 200, path
    assert sam.get("/switch").status_code == 200 and sam.get("/settings").status_code == 403
    r = ivy.post("/settings-hub", data={"csrf": csrf_of(ivy), "drain_quiet_s": "30"})
    assert r.status_code == 403
    r = ivy.post("/ui/switch", data={"mode": "quality", "action": "force"}, headers={"X-CSRF-Token": csrf_of(ivy)})
    assert r.status_code == 403 and hub.hub.modes.target != "quality"
    # Grafana, Arcane, NVIDIA (nginx auth_request): admins only; chat: every role
    assert ivy.get("/auth/check").status_code == 401 and admin.get("/auth/check").status_code == 204
    assert ivy.get("/auth/check-chat").status_code != 401
    # the account page is about oneself only
    page = ivy.get("/account").text
    assert "inference" in page and "My sessions" in page and ">admin<" not in page


def test_keys_by_role_and_activity_per_user(hub):
    admin, _ = enrolled_client(hub)
    add_user(admin, "ivy", "infer")
    add_user(admin, "sam", "switch")
    ivy, sam = first_login(hub, "ivy"), first_login(hub, "sam")
    page = ivy.get("/keys").text
    assert "<option>infer</option>" in page and "<option>switch</option>" not in page
    assert "above the inference role" in new_key(ivy, "too-much", "switch")
    k_ivy = new_key(ivy, "ivy-laptop", "infer")
    k_sam = new_key(sam, "sam-agent", "switch")
    k_admin = new_key(admin, "admin-box", "admin")
    assert all(k.startswith("shk_") for k in (k_ivy, k_sam, k_admin))
    # keys: a user sees their own; an admin sees everyone's with the owner
    assert "ivy-laptop" in ivy.get("/keys").text and "sam-agent" not in ivy.get("/keys").text
    page = admin.get("/keys").text
    assert "<th>Owner</th>" in page and "ivy-laptop" in page and "sam-agent" in page
    sam_id = hub.db.one("SELECT id FROM api_keys WHERE name='sam-agent'")["id"]
    ivy.post(f"/keys/{sam_id}/revoke", data={"csrf": csrf_of(ivy)})
    assert hub.db.one("SELECT revoked_at FROM api_keys WHERE id=?", (sam_id,))["revoked_at"] is None
    # switching: the switcher's key may, the inference key may not
    with hub.client(k_ivy) as k:
        assert k.post("/api/v1/mode", json={"mode": "quality"}).status_code == 403
    for key in (k_ivy, k_sam, k_admin):
        assert chat(hub, key).status_code == 200
    # activity: one's own; an admin sees everyone's, with the user
    page = ivy.get("/activity").text
    assert "ivy-laptop" in page and "sam-agent" not in page and "admin-box" not in page and "<th>User</th>" not in page
    assert "Events (latest" not in page
    page = admin.get("/activity").text
    assert "<th>User</th>" in page and "<td>ivy</td>" in page and "<td>sam</td>" in page
    live = ivy.get("/ui/live").text
    recent = live[live.index("Recent requests"):live.index("<h2>Clients")]     # Clients: everyone's, by design
    assert "ivy-laptop" in recent and "sam-agent" not in recent and "sam-agent" in live
    row = hub.db.one("SELECT user_name FROM activity WHERE key_name='sam-agent'")
    assert row["user_name"] == "sam"
    # lowering a role caps the user's keys at once
    sam_uid = hub.db.one("SELECT id FROM users WHERE username='sam'")["id"]
    admin.post(f"/users/{sam_uid}/role", data={"csrf": csrf_of(admin), "role": "infer"})
    with hub.client(k_sam) as k:
        assert k.post("/api/v1/mode", json={"mode": "quality"}).status_code == 403
    assert chat(hub, k_sam).status_code == 200
    assert "→ infer" in admin.get("/keys").text
    assert sam.get("/switch").status_code == 403


def test_suspend_delete_and_resets(hub):
    admin, _ = enrolled_client(hub)
    add_user(admin, "ivy", "infer")
    ivy = first_login(hub, "ivy")
    k_ivy = new_key(ivy, "ivy-laptop", "infer")
    assert chat(hub, k_ivy).status_code == 200
    uid = hub.db.one("SELECT id FROM users WHERE username='ivy'")["id"]
    tok = csrf_of(admin)
    # suspended: logged out, can't log in, keys refused; back again after unsuspend
    admin.post(f"/users/{uid}/suspend", data={"csrf": tok})
    assert ivy.get("/", follow_redirects=False).status_code == 303
    assert chat(hub, k_ivy).status_code == 401
    r = password_step(hub.client(), "ivy", OWN)
    assert r.status_code == 200 and "Wrong username or password" in r.text
    assert "suspended" in admin.get("/users").text
    admin.post(f"/users/{uid}/unsuspend", data={"csrf": tok})
    assert chat(hub, k_ivy).status_code == 200
    # an admin's password reset is temporary: the user picks their own at the next login
    admin.post(f"/users/{uid}/password", data={"csrf": tok, "password": TEMP, "again": TEMP})
    c = hub.client()
    assert password_step(c, "ivy", TEMP).headers["location"] == "/login/password"
    c.post("/login/password", data={"new": OWN + "x", "again": OWN + "x", "csrf": c.cookies.get("sparkhub_lc")})
    # 2FA reset: enrol again
    admin.post(f"/users/{uid}/totp", data={"csrf": tok})
    assert password_step(hub.client(), "ivy", OWN + "x").headers["location"] == "/login/enroll"
    # an admin can't demote, suspend or delete themselves; the last admin stays
    me = hub.db.one("SELECT id FROM users WHERE username='admin'")["id"]
    for action in ("suspend", "delete"):
        r = admin.post(f"/users/{me}/{action}", data={"csrf": tok, "confirm": "admin"}, follow_redirects=False)
        assert "error=" in r.headers["location"]
    r = admin.post(f"/users/{me}/role", data={"csrf": tok, "role": "infer"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert hub.db.one("SELECT role, disabled FROM users WHERE id=?", (me,))["role"] == "admin"
    # delete: needs the name typed; revokes the keys, keeps the activity
    r = admin.post(f"/users/{uid}/delete", data={"csrf": tok, "confirm": "nope"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    admin.post(f"/users/{uid}/delete", data={"csrf": tok, "confirm": "ivy"})
    assert hub.db.one("SELECT 1 FROM users WHERE username='ivy'") is None
    assert chat(hub, k_ivy).status_code == 401
    assert hub.db.one("SELECT revoked_at FROM api_keys WHERE name='ivy-laptop'")["revoked_at"]
    assert "<td>ivy</td>" in admin.get("/activity").text
    # the users page: sessions and login events of everyone
    page = admin.get("/users").text
    assert "Add a user" in page and "Recent login events" in page and "user_deleted" in page


def test_revoked_keys_hidden_by_default(hub):
    admin, _ = enrolled_client(hub)
    new_key(admin, "old-one", "infer")
    new_key(admin, "live-one", "infer")
    kid = hub.db.one("SELECT id FROM api_keys WHERE name='old-one'")["id"]
    admin.post(f"/keys/{kid}/revoke", data={"csrf": csrf_of(admin)})
    page = admin.get("/keys").text
    assert "live-one" in page and "old-one" not in page and "Show revoked and expired keys (1)" in page
    page = admin.get("/keys?revoked=1").text
    assert "old-one" in page and "Hide revoked" in page


def test_cli_keys_are_system_keys(hub, capsys, monkeypatch):
    from sparkhub import admin_cli
    monkeypatch.setattr(admin_cli, "DB", lambda path: hub.db)
    assert admin_cli.main(["key", "create", "chatui", "--scope", "infer"]) == 0
    raw = capsys.readouterr().out.strip().splitlines()[-1]
    p = keymod.verify_key(hub.db, raw)
    assert p is not None and p.owner_id is None
    assert admin_cli.main(["user", "create", "bob", "--role", "infer", "--password", PW]) == 0
    assert admin_cli.main(["key", "create", "bob-key", "--scope", "switch", "--user", "bob"]) == 1   # above the role
    assert admin_cli.main(["key", "create", "bob-key", "--user", "bob"]) == 0
    capsys.readouterr()
    admin_cli.main(["key", "list"])
    out = capsys.readouterr().out
    assert "system" in out and "bob" in out


def test_migration_assigns_key_owners(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for i, script in enumerate(SCHEMA[:4], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version={i}; COMMIT;")
    conn.execute("INSERT INTO users(username, pw_hash, is_admin, created_at) VALUES('yan', 'x', 1, 0)")
    conn.execute("INSERT INTO api_keys(name, prefix, hash, scopes, created_at, created_by)"
                 " VALUES('mine', 'p', 'h1', 'admin', 0, 'user:yan'), ('cli', 'p', 'h2', 'infer', 0, 'cli')")
    conn.execute("INSERT INTO activity(ts, key_id, key_name) VALUES(1, 1, 'mine')")
    conn.commit()
    conn.close()
    db = DB(path)
    assert db.one("SELECT role FROM users")["role"] == "admin"
    owners = {r["name"]: r["user_id"] for r in db.all("SELECT name, user_id FROM api_keys")}
    assert owners == {"mine": 1, "cli": None}
    assert dict(db.one("SELECT user_id, user_name FROM activity")) == {"user_id": 1, "user_name": "yan"}
