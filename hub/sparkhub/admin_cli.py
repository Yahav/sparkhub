"""Host CLI: docker exec -it sparkhub-hub sparkhub-admin <command>

Works on the database directly, so it also works when nobody can log in.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time

from . import keys as keymod
from .config import load_config
from .db import DB


def _fmt_ts(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def cmd_key_create(db: DB, a) -> int:
    uid = None
    if a.user:
        row = db.one("SELECT id FROM users WHERE username=?", (a.user,))
        if row is None:
            print("no such user")
            return 1
        uid = row["id"]
    try:
        kid, raw = keymod.create_key(db, a.name, a.scope, "cli", a.expires_days, a.max_concurrency, user_id=uid)
    except ValueError as e:
        print(e)
        return 1
    owner = a.user or "system (no owner)"
    print(f"key {kid} ({a.name}, {a.scope}, {owner}) created. Shown once, store it now:\n{raw}")
    return 0


def cmd_key_list(db: DB, a) -> int:
    for k in keymod.list_keys(db):
        state = "revoked" if k["revoked_at"] else ("expired" if k["expires_at"] and k["expires_at"] < time.time()
                                                   else "active")
        print(f"{k['id']:>4}  {k['name']:<24} {k['scopes']:<7} {k['prefix']}…  {state:<8} "
              f"{(k['owner'] or 'system'):<16} last used {_fmt_ts(k['last_used_at'])}")
    return 0


def cmd_key_revoke(db: DB, a) -> int:
    ok = keymod.revoke_key(db, a.id, "cli")
    print("revoked" if ok else "no such active key")
    return 0 if ok else 1


def cmd_key_revoke_all(db: DB, a) -> int:
    print(f"revoked {keymod.revoke_all(db, 'cli')} keys")
    return 0


def _password(prompt: str = "New password: ") -> str:
    while True:
        pw = getpass.getpass(prompt)
        if len(pw) < 12:
            print("at least 12 characters, please")
            continue
        if getpass.getpass("Again: ") != pw:
            print("they differ, try again")
            continue
        return pw


def cmd_user_create(db: DB, a) -> int:
    from . import auth
    pw = a.password or _password()
    uid = auth.create_user(db, a.username, pw, role=a.role)
    print(f"user {a.username} created ({keymod.ROLE_LABELS[a.role]}, id {uid}). TOTP enrolment happens at the first login.")
    return 0


def cmd_user_list(db: DB, a) -> int:
    for u in db.all("SELECT * FROM users ORDER BY id"):
        lock = f"locked until {_fmt_ts(u['locked_until'])}" if u["locked_until"] and u["locked_until"] > time.time() \
            else ""
        print(f"{u['id']:>3}  {u['username']:<20} {keymod.ROLE_LABELS.get(u['role'], u['role']):<10} "
              f"{'SUSPENDED ' if u['disabled'] else ''}totp={'on' if u['totp_enabled'] else 'off'} "
              f"failed={u['failed_count']} last login {_fmt_ts(u['last_login_at'])} {lock}")
    return 0


def cmd_user_reset_password(db: DB, a) -> int:
    from . import auth
    pw = a.password or _password()
    if not auth.set_password(db, a.username, pw, "cli"):
        print("no such user")
        return 1
    print("password set, lockout cleared, sessions ended")
    return 0


def cmd_user_reset_totp(db: DB, a) -> int:
    from . import auth
    if not auth.reset_totp(db, a.username, "cli"):
        print("no such user")
        return 1
    print("TOTP cleared; the user enrols again at the next login. Sessions ended.")
    return 0


def cmd_user_unlock(db: DB, a) -> int:
    n = db.execute("UPDATE users SET failed_count=0, locked_until=NULL WHERE username=?", (a.username,)).rowcount
    db.event("user_unlocked", "cli", username=a.username)
    print("unlocked" if n else "no such user")
    return 0 if n else 1


def cmd_user_delete(db: DB, a) -> int:
    db.execute("UPDATE api_keys SET revoked_at=? WHERE revoked_at IS NULL AND user_id=(SELECT id FROM users"
               " WHERE username=?)", (time.time(), a.username))
    db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (a.username,))
    n = db.execute("DELETE FROM users WHERE username=?", (a.username,)).rowcount
    db.event("user_deleted", "cli", username=a.username)
    print("deleted" if n else "no such user")
    return 0 if n else 1


def cmd_chat(db: DB, a) -> int:
    if a.sub in ("on", "off"):
        db.set_setting("chat_enabled", a.sub == "on")
        db.event("chat_enabled" if a.sub == "on" else "chat_disabled", "cli")
        print(f"chat {a.sub} (the hub applies it within a minute)")
    else:
        print("on" if db.get_setting("chat_enabled") else "off")
    return 0


def cmd_backup(db: DB, a) -> int:
    from .maintenance import run_backup
    dest = run_backup(load_config(), db)
    print(f"backup written to {dest}")
    return 0


def cmd_oidc_add(db: DB, a) -> int:
    from . import oidc
    secret = oidc.add_client(db, a.client_id, a.redirect_uri, "cli")
    cfg = load_config()
    print(f"client {a.client_id} saved. Issuer: {cfg.oidc_issuer}\nSecret, shown once, store it now:\n{secret}")
    return 0


def cmd_oidc_list(db: DB, a) -> int:
    from . import oidc
    for c in oidc.list_clients(db):
        print(f"{c['id']:<20} created {_fmt_ts(c['created_at'])}  {' '.join(c['redirect_uris'])}")
    return 0


def cmd_oidc_remove(db: DB, a) -> int:
    from . import oidc
    ok = oidc.remove_client(db, a.client_id, "cli")
    print("removed" if ok else "no such client")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sparkhub-admin", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("key", help="API keys").add_subparsers(dest="sub", required=True)
    p = k.add_parser("create")
    p.add_argument("name")
    p.add_argument("--scope", default="infer", choices=keymod.SCOPES)
    p.add_argument("--expires-days", type=float)
    p.add_argument("--max-concurrency", type=int)
    p.add_argument("--user", help="owner (default: none, a system key)")
    p.set_defaults(fn=cmd_key_create)
    k.add_parser("list").set_defaults(fn=cmd_key_list)
    p = k.add_parser("revoke")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_key_revoke)
    k.add_parser("revoke-all").set_defaults(fn=cmd_key_revoke_all)

    u = sub.add_parser("user", help="dashboard users").add_subparsers(dest="sub", required=True)
    p = u.add_parser("create")
    p.add_argument("username")
    p.add_argument("--role", default="admin", choices=keymod.SCOPES, help="admin, switch or infer (default admin)")
    p.add_argument("--password", help=argparse.SUPPRESS)   # for tests; prompts otherwise
    p.set_defaults(fn=cmd_user_create)
    u.add_parser("list").set_defaults(fn=cmd_user_list)
    p = u.add_parser("reset-password")
    p.add_argument("username")
    p.add_argument("--password", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_user_reset_password)
    p = u.add_parser("reset-totp")
    p.add_argument("username")
    p.set_defaults(fn=cmd_user_reset_totp)
    p = u.add_parser("unlock")
    p.add_argument("username")
    p.set_defaults(fn=cmd_user_unlock)

    p = u.add_parser("delete")
    p.add_argument("username")
    p.set_defaults(fn=cmd_user_delete)

    o = sub.add_parser("oidc", help="apps that sign in with the hub login (OIDC clients)").add_subparsers(
        dest="sub", required=True)
    p = o.add_parser("add-client", help="register or re-key a client; prints its secret once")
    p.add_argument("client_id")
    p.add_argument("--redirect-uri", action="append", required=True)
    p.set_defaults(fn=cmd_oidc_add)
    o.add_parser("list").set_defaults(fn=cmd_oidc_list)
    p = o.add_parser("remove")
    p.add_argument("client_id")
    p.set_defaults(fn=cmd_oidc_remove)

    p = sub.add_parser("chat", help="chat-ui at /chat/: turn it on or off, or show the setting")
    p.add_argument("sub", choices=("status", "on", "off"))
    p.set_defaults(fn=cmd_chat)

    sub.add_parser("backup", help="write a backup now (data/backups/YYYY-MM-DD)").set_defaults(fn=cmd_backup)

    a = ap.parse_args(argv)
    db = DB(load_config().db_path)
    try:
        return a.fn(db, a)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
