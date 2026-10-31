"""Backups and certificate checks."""
from __future__ import annotations

import json
import sqlite3
import tarfile
from datetime import datetime, timedelta, timezone

from sparkhub import maintenance


def test_backup_contents_and_pruning(hub):
    cfg = hub.cfg
    (cfg.data_dir / "tls").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "tls" / "ca.key").write_text("secret")
    (cfg.data_dir / "ntfy-topic").write_text("topic")
    (cfg.data_dir / "grafana").mkdir(exist_ok=True)
    g = sqlite3.connect(cfg.data_dir / "grafana" / "grafana.db")
    g.execute("CREATE TABLE dashboard(id INTEGER)")
    g.commit()
    g.close()
    (cfg.data_dir / "arcane").mkdir(exist_ok=True)
    a = sqlite3.connect(cfg.data_dir / "arcane" / "arcane.db")
    a.execute("PRAGMA journal_mode=WAL")      # Arcane keeps its database in WAL mode
    a.execute("CREATE TABLE users(id INTEGER)")
    a.commit()
    (cfg.data_dir / "arcane.env").write_text("ENCRYPTION_KEY=x\n")
    day0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for i in range(16):
        dest = maintenance.run_backup(cfg, hub.db, when=day0 + timedelta(days=i))
    names = sorted(p.name for p in (cfg.data_dir / "backups").iterdir())
    # 14 kept (the hub's own startup backup, dated today, may be one of them); the oldest test days are gone
    assert len(names) == 14 and "2026-10-16" in names and "2026-10-01" not in names and "2026-10-02" not in names
    files = sorted(p.name for p in dest.iterdir())
    assert files == ["arcane.db", "grafana.db", "secrets.tar.gz", "sparkhub.db"]
    a.close()
    assert oct(dest.stat().st_mode)[-3:] == "700"
    # the database copy is a usable SQLite database with the live tables
    n = sqlite3.connect(dest / "sparkhub.db").execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
    assert n == 3
    with tarfile.open(dest / "secrets.tar.gz") as t:
        assert {"tls/ca.key", "oidc/es256.pem", "ntfy-topic", "arcane.env"} <= set(t.getnames())
    assert "sparkhub_backup_last_success_timestamp_seconds" in hub.client().get("/metrics").text


def test_cert_check_against_a_bad_target():
    ok, exp = maintenance.check_cert("example.invalid", None, host="127.0.0.1", port=1)
    assert ok is False and exp is None


def test_docker_api_version_follows_the_daemon(tmp_path):
    """Ubuntu's docker.io 29 refuses API 1.43 (minimum 1.44): use the daemon's minimum then, else 1.43."""
    import http.server
    import socketserver
    import threading
    from sparkhub.runtime import docker_base_url

    versions = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(versions).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def get_request(self):
            req, _ = super().get_request()
            return req, ("local", 0)

    sock = str(tmp_path / "docker.sock")
    srv = S(sock, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for lo, hi, want in (("1.44", "1.52", "v1.44"), ("1.40", "1.55", "v1.43"), ("1.24", "1.41", "v1.41")):
            versions.update(MinAPIVersion=lo, ApiVersion=hi)
            assert docker_base_url(sock) == f"http://docker/{want}"
    finally:
        srv.shutdown()
        srv.server_close()
    assert docker_base_url(str(tmp_path / "missing.sock")) == "http://docker/v1.43"
