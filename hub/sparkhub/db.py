"""SQLite state: settings, API keys, users, sessions, activity, events.

One connection, guarded by a lock. Every statement is short, so calling it from the event loop is fine at this scale.
No prompt or response content is ever stored here, only numbers and names.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# Migrations, in order: entry i (from 1) brings the database to version i (PRAGMA user_version). The hub runs the
# missing ones at start, each in one transaction with its version bump: all of it applies, or nothing. Append only;
# never edit one that shipped. An entry is a SQL script, or a function(conn) for a backfill SQL can't express: it
# runs inside the transaction and must use conn.execute only (executescript would commit early).
SCHEMA: list[Any] = [
    # v1
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE mode_settings (mode TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL, updated_by TEXT);
    CREATE TABLE api_keys (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
        scopes TEXT NOT NULL, created_at REAL NOT NULL, created_by TEXT, expires_at REAL, revoked_at REAL,
        last_used_at REAL, max_concurrency INTEGER);
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, key_id INTEGER, key_name TEXT, client_ip TEXT, mode TEXT,
        model TEXT, endpoint TEXT, stream INTEGER, status INTEGER, outcome TEXT, finish_reason TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER,
        queue_wait REAL, ttft REAL, gen_tps REAL, duration REAL);
    CREATE INDEX activity_ts ON activity (ts);
    CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, actor TEXT, detail TEXT);
    CREATE INDEX events_ts ON events (ts);
    CREATE TABLE users (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 1,
        totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0, totp_last_step INTEGER, recovery TEXT,
        failed_count INTEGER NOT NULL DEFAULT 0, locked_until REAL, created_at REAL NOT NULL, last_login_at REAL,
        disabled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, stage TEXT NOT NULL, csrf TEXT NOT NULL,
        created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL, ip TEXT, ua TEXT);
    """,
    # v2: "remember this device" sessions
    """
    ALTER TABLE sessions ADD COLUMN remember INTEGER NOT NULL DEFAULT 0;
    """,
    # v3: thinking per request
    """
    ALTER TABLE activity ADD COLUMN thinking INTEGER;
    ALTER TABLE activity ADD COLUMN reasoning_tokens INTEGER;
    """,
    # v4: OIDC clients (apps that sign in with the hub login, see oidc.py)
    """
    CREATE TABLE oidc_clients (id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, redirect_uris TEXT NOT NULL,
                               created_at REAL NOT NULL);
    """,
    # v5: user roles (admin, switch, infer), keys owned by a user (NULL: a system key made with the CLI),
    # a temporary password to change at the next login, and the user on each request
    """
    ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin';
    UPDATE users SET role = CASE WHEN is_admin THEN 'admin' ELSE 'switch' END;
    ALTER TABLE users ADD COLUMN must_change_pw INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE api_keys ADD COLUMN user_id INTEGER;
    UPDATE api_keys SET user_id = (SELECT id FROM users WHERE 'user:' || users.username = api_keys.created_by);
    ALTER TABLE activity ADD COLUMN user_id INTEGER;
    ALTER TABLE activity ADD COLUMN user_name TEXT;
    UPDATE activity SET user_id = (SELECT user_id FROM api_keys WHERE api_keys.id = activity.key_id);
    UPDATE activity SET user_name = (SELECT username FROM users WHERE users.id = activity.user_id);
    CREATE INDEX activity_user ON activity (user_id, id);
    """,
    # v6: recipes imported for the Custom mode (custom.py)
    """
    CREATE TABLE recipes (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL, created_by TEXT,
        source TEXT NOT NULL, yaml TEXT NOT NULL, spec TEXT NOT NULL, facts TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', approved_at REAL, approved_by TEXT,
        download TEXT, measured TEXT, update_info TEXT, parent_id INTEGER);
    """,
    # v7: sparkrun mods pinned with a recipe, their files' text included
    """
    ALTER TABLE recipes ADD COLUMN mods TEXT;
    """,
    # v8: benchmark runs (bench.py); numbers only, no prompts or answers
    """
    CREATE TABLE bench_runs (
        id INTEGER PRIMARY KEY, status TEXT NOT NULL, mode TEXT NOT NULL, recipe_id INTEGER, model_label TEXT,
        fingerprint TEXT, suites TEXT NOT NULL, internet INTEGER NOT NULL DEFAULT 0, start_at REAL NOT NULL,
        est_s REAL NOT NULL, created_at REAL NOT NULL, created_by TEXT, started_at REAL, finished_at REAL,
        key_id INTEGER, bench_version TEXT, progress TEXT, results TEXT, error TEXT);
    CREATE INDEX bench_runs_start ON bench_runs (start_at);
    """,
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "desired_mode": "fast",
    "activity_window_s": 300,       # a client counts as active for this long after its last request
    "drain_quiet_s": 120,           # drain: switch once nobody sent a request for this long
    "drain_timeout_s": 900,         # drain: give up after this long
    "key_max_concurrency": 4,       # default per-key cap on concurrent requests
    "queue_timeout_s": 600,         # how long a request may wait for its key's slot
    "load_timeout_s": 1500,         # one model load
    "retry_delay_s": 20,            # wait before the second load attempt
    "activity_retention_days": 90,
    "watchdog": True,               # reload a crashed model once
    "ntfy_enabled": True,           # notifications (hub pushes and Grafana alerts, see notify.py)
    "ntfy_high_only": False,        # only high-priority pushes and alerts; drops started/ready/model back
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",               # secret; set on the Settings page
    "chat_enabled": True,           # chat-ui at /chat/, when installed (see main.Hub.apply_chat)
    "embed_mode": "on-demand",      # embedding service: always | on-demand | off (see embeddings.py)
    "embed_idle_minutes": 10,       # on-demand: stop after this long without a request
    "embed_rerank": True,           # the reranker (always on-demand)
    "embed_hold_s": 60,             # chat first: a large embedding request waits at most this long for chat
    "update_check": True,           # once a day: is there a newer SparkHub release on GitHub (updates.py)
    "custom_recipe": None,          # the recipe id assigned to the Custom mode (custom.py)
    "recipe_registries": [          # browsed on the Models page (Custom); off until an admin turns one on
        {"url": "https://github.com/spark-arena/recipe-registry", "name": "Spark Arena official", "enabled": False},
        {"url": "https://github.com/spark-arena/community-recipe-registry", "name": "Spark Arena community",
         "enabled": False},
        {"url": "https://github.com/spark-arena/eugr-recipes", "name": "eugr (sparkrun's mods source)", "enabled": False},
    ],
    "registry_cache": {},           # url -> {fetched_at, commit, recipes}
}


class SchemaTooNew(RuntimeError):
    pass


def migrate(conn: sqlite3.Connection, path: str = "", upto: int | None = None) -> int:
    """Bring the database to the newest version (or `upto`); returns the version it is at."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(SCHEMA):
        raise SchemaTooNew(
            f"the database {path} is at schema v{version}, but this SparkHub knows only up to v{len(SCHEMA)}: a "
            f"newer release used it. Move to that release (./install.sh upgrade), or stop the hub and restore the "
            f"backup made before the upgrade (data/backups/pre-upgrade-*.db)")
    for i, step in enumerate(SCHEMA[version:upto], start=version + 1):
        try:
            if callable(step):
                conn.execute("BEGIN")
                step(conn)
                conn.execute(f"PRAGMA user_version={i}")
                conn.execute("COMMIT")
            else:
                conn.executescript("BEGIN;" + step + f"PRAGMA user_version={i}; COMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        version = i
    return version


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self._migrate()

    def _migrate(self) -> None:
        migrate(self.conn, self.path)

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    # settings -----------------------------------------------------------------
    def get_setting(self, key: str) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return DEFAULT_SETTINGS.get(key)
        return json.loads(row["value"])

    def set_setting(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def all_settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for row in self.all("SELECT key, value FROM settings"):
            out[row["key"]] = json.loads(row["value"])
        return out

    # events -------------------------------------------------------------------
    def event(self, kind: str, actor: str | None = None, **detail: Any) -> None:
        self.execute("INSERT INTO events(ts, kind, actor, detail) VALUES(?, ?, ?, ?)",
                     (time.time(), kind, actor, json.dumps(detail) if detail else None))

    def prune(self, days: int) -> None:
        cutoff = time.time() - days * 86400
        self.execute("DELETE FROM activity WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
