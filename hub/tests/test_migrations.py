"""Database migrations (db.migrate): every older version, with rows in every table, migrates to the newest without
losing a row; a Python migration backfills inside the transaction; a failing one changes nothing; a database from
a newer release is refused."""
from __future__ import annotations

import sqlite3

import pytest

from sparkhub import db as dbmod
from sparkhub.db import DB, SchemaTooNew, migrate

DUMMY = {"INTEGER": 1, "REAL": 1.0, "TEXT": "{}"}


def tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def fill(conn) -> dict[str, int]:
    """One row in every table: every NOT NULL column without a default gets a dummy of its type."""
    counts = {}
    for t in tables(conn):
        cols = conn.execute(f"PRAGMA table_info({t})").fetchall()     # cid, name, type, notnull, default, pk
        need = [c for c in cols if c[3] and c[4] is None and not c[5]]
        names = ", ".join(c[1] for c in need)
        marks = ", ".join("?" for _ in need)
        sql = f"INSERT INTO {t}({names}) VALUES({marks})" if need else f"INSERT INTO {t} DEFAULT VALUES"
        pk_text = [c for c in cols if c[5] and c[2] == "TEXT"]          # sessions.id, oidc_clients.id, settings.key
        if pk_text and pk_text[0] not in need:
            need, sql = [pk_text[0], *need], (f"INSERT INTO {t}({', '.join(c[1] for c in [pk_text[0], *need])}) "
                                              f"VALUES({', '.join('?' for _ in range(len(need) + 1))})")
        conn.execute(sql, [("k1" if c in pk_text else DUMMY.get(c[2].split("(")[0], "{}")) for c in need])
        counts[t] = 1
    return counts


@pytest.mark.parametrize("start", range(0, len(dbmod.SCHEMA)))
def test_every_version_migrates_to_the_newest(tmp_path, start):
    path = tmp_path / f"v{start}.db"
    conn = sqlite3.connect(path, isolation_level=None)
    assert migrate(conn, upto=start) == start
    counts = fill(conn)
    conn.close()
    db = DB(path)                                                       # what the hub does at start
    assert db.one("PRAGMA user_version")[0] == len(dbmod.SCHEMA)
    assert db.one("PRAGMA integrity_check")[0] == "ok"
    for t, n in counts.items():
        assert db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] == n, t
    assert db.get_setting("watchdog") is True                          # the hub's code reads it


def test_python_migration_backfills_and_a_failure_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "h.db"
    db = DB(path)
    db.execute("INSERT INTO events(ts, kind, actor) VALUES(1, 'switch', 'user:alice'), (2, 'switch', 'boot')")
    db.conn.close()
    newest = len(dbmod.SCHEMA)

    def broken(conn):
        conn.execute("ALTER TABLE events ADD COLUMN by_user INTEGER")
        raise ValueError("bug in the backfill")
    monkeypatch.setattr(dbmod, "SCHEMA", [*dbmod.SCHEMA, broken])
    with pytest.raises(ValueError):
        DB(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == newest  # rolled back: old version, no new column
    assert "by_user" not in [r[1] for r in conn.execute("PRAGMA table_info(events)")]
    conn.close()

    def backfill(conn):
        conn.execute("ALTER TABLE events ADD COLUMN by_user INTEGER")
        for rid, actor in conn.execute("SELECT id, actor FROM events").fetchall():
            conn.execute("UPDATE events SET by_user=? WHERE id=?", (int(actor.startswith("user:")), rid))
    monkeypatch.setattr(dbmod, "SCHEMA", [*dbmod.SCHEMA[:newest], backfill])
    db = DB(path)
    assert db.one("PRAGMA user_version")[0] == newest + 1
    assert [r["by_user"] for r in db.all("SELECT by_user FROM events ORDER BY id")] == [1, 0]


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "n.db"
    DB(path).execute(f"PRAGMA user_version={len(dbmod.SCHEMA) + 1}")
    with pytest.raises(SchemaTooNew, match="newer release used it"):
        DB(path)
