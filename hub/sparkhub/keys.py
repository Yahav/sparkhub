"""API keys: shown once, stored as SHA-256 (they are 256-bit random, so a slow hash adds nothing)."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from .db import DB

SCOPES = ("infer", "switch", "admin")   # each includes the ones before it; a user's role is one of these too
ROLE_LABELS = {"infer": "inference", "switch": "switcher", "admin": "admin"}
KEY_PREFIX = "shk_"


def scope_level(scope: str) -> int:
    return SCOPES.index(scope)


def scopes_up_to(role: str) -> tuple[str, ...]:
    """The key scopes a user with this role may give a key."""
    return SCOPES[: scope_level(role) + 1]


@dataclass
class Principal:
    """Whoever is making a request: an API key or a logged-in dashboard user."""
    kind: str                 # "key" | "user"
    id: int
    name: str
    scope: str                # highest scope held
    max_concurrency: int | None = None
    session_id: str | None = None
    csrf: str | None = None
    owner_id: int | None = None       # the key's user (None: a system key made with the CLI)
    owner_name: str | None = None

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"

    def has(self, scope: str) -> bool:
        return scope_level(self.scope) >= scope_level(scope)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_key(db: DB, name: str, scope: str, created_by: str | None, expires_days: float | None = None,
               max_concurrency: int | None = None, user_id: int | None = None) -> tuple[int, str]:
    """A new key for user_id (None: a system key). Its scope can't be above the user's role."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    if user_id is not None:
        u = db.one("SELECT role FROM users WHERE id=?", (user_id,))
        if u is None:
            raise ValueError("no such user")
        if scope_level(scope) > scope_level(u["role"]):
            raise ValueError(f"scope {scope} is above the {ROLE_LABELS[u['role']]} role")
    name = name.strip()
    if not name or len(name) > 64:
        raise ValueError("name must be 1-64 characters")
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    expires = now + expires_days * 86400 if expires_days else None
    cur = db.execute(
        "INSERT INTO api_keys(name, prefix, hash, scopes, created_at, created_by, expires_at, max_concurrency, user_id)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, raw[:12], hash_key(raw), scope, now, created_by, expires, max_concurrency, user_id))
    db.event("key_created", created_by, key_id=cur.lastrowid, name=name, scope=scope)
    return cur.lastrowid, raw


def verify_key(db: DB, raw: str) -> Principal | None:
    if not raw.startswith(KEY_PREFIX) or len(raw) > 128:
        return None
    row = db.one("SELECT k.*, u.username, u.role, u.disabled FROM api_keys k LEFT JOIN users u ON u.id=k.user_id"
                 " WHERE k.hash=?", (hash_key(raw),))
    now = time.time()
    if row is None or row["revoked_at"] is not None or (row["expires_at"] and row["expires_at"] < now):
        return None
    scope = row["scopes"]
    if row["user_id"] is not None:
        if row["username"] is None or row["disabled"]:
            return None                          # the owner was deleted or suspended
        if scope_level(row["role"]) < scope_level(scope):
            scope = row["role"]                  # never above the owner's role (it may have been lowered)
    # last_used_at at most once a minute, to keep writes down
    if not row["last_used_at"] or now - row["last_used_at"] > 60:
        db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, row["id"]))
    return Principal("key", row["id"], row["name"], scope, row["max_concurrency"], owner_id=row["user_id"],
                     owner_name=row["username"])


def revoke_key(db: DB, key_id: int, actor: str | None, user_id: int | None = None) -> bool:
    """Revoke a key; with user_id, only if it is that user's."""
    sql = "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL"
    params: tuple = (time.time(), key_id)
    if user_id is not None:
        sql += " AND user_id=?"
        params += (user_id,)
    cur = db.execute(sql, params)
    if cur.rowcount:
        db.event("key_revoked", actor, key_id=key_id)
    return bool(cur.rowcount)


def revoke_all(db: DB, actor: str | None) -> int:
    cur = db.execute("UPDATE api_keys SET revoked_at=? WHERE revoked_at IS NULL", (time.time(),))
    db.event("keys_revoked_all", actor, count=cur.rowcount)
    return cur.rowcount


def list_keys(db: DB, user_id: int | None = None) -> list[dict]:
    """All keys with their owner (None: a system key), or only user_id's."""
    sql = ("SELECT k.id, k.name, k.prefix, k.scopes, k.created_at, k.created_by, k.expires_at, k.revoked_at,"
           " k.last_used_at, k.max_concurrency, k.user_id, u.username AS owner, u.role AS owner_role"
           " FROM api_keys k LEFT JOIN users u ON u.id=k.user_id")
    rows = db.all(sql + (" WHERE k.user_id=? ORDER BY k.id" if user_id is not None else " ORDER BY k.id"),
                  (user_id,) if user_id is not None else ())
    out = [dict(r) for r in rows]
    for k in out:   # the scope it works with now (capped by the owner's role)
        k["effective"] = (k["owner_role"] if k["owner_role"] and scope_level(k["owner_role"]) < scope_level(k["scopes"])
                          else k["scopes"])
    return out
