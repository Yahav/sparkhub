"""Housekeeping: nightly backups and certificate-expiry checks, both exported as metrics for Grafana alerts."""
from __future__ import annotations

import io
import logging
import shutil
import socket
import sqlite3
import ssl
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from prometheus_client import Gauge

from . import metrics
from .config import Config
from .db import DB
from .runtime import demux, docker_base_url

log = logging.getLogger("sparkhub.maintenance")

BACKUP_LAST = Gauge("sparkhub_backup_last_success_timestamp_seconds", "Time of the last successful backup",
                    registry=metrics.REGISTRY)
BACKUP_SIZE = Gauge("sparkhub_backup_size_bytes", "Size of the last backup", registry=metrics.REGISTRY)
CERT_EXPIRY = Gauge("sparkhub_tls_cert_expiry_timestamp_seconds", "notAfter of the certificate nginx serves",
                    ["name"], registry=metrics.REGISTRY)
CERT_OK = Gauge("sparkhub_tls_check_ok", "1 if the certificate nginx serves for this name verifies", ["name"],
                registry=metrics.REGISTRY)

KEEP_DAYS = 14


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite database (online backup API; the source is opened read-only)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _chat_backup(cfg: Config, dest: Path) -> None:
    """chat-ui's MongoDB (everyone's chat history): a mongodump archive while the container runs (restore:
    docker exec -i <instance>-chatui mongorestore --archive --gzip --drop < chatui.archive.gz), else a copy of
    data/chatui (MongoDB shut down cleanly, so the files are consistent)."""
    name = cfg.chat_container
    with httpx.Client(transport=httpx.HTTPTransport(uds=cfg.docker_socket), base_url=docker_base_url(cfg.docker_socket),
                      timeout=httpx.Timeout(60.0, read=900.0)) as d:
        r = d.get(f"/containers/{name}/json")
        running = r.status_code == 200 and r.json()["State"]["Running"]
        if running:
            r = d.post(f"/containers/{name}/exec", json={"AttachStdout": True, "AttachStderr": True,
                                                         "Cmd": ["mongodump", "--archive", "--gzip", "--quiet"]})
            r.raise_for_status()
            eid = r.json()["Id"]
            r = d.post(f"/exec/{eid}/start", json={"Detach": False, "Tty": False})
            r.raise_for_status()
            stdout, stderr = demux(r.content)
            code = d.get(f"/exec/{eid}/json").json().get("ExitCode")
            if code != 0 or not stdout:
                raise RuntimeError(f"mongodump in {name} exited {code}: {stderr.decode(errors='replace')[:300]}")
            (dest / "chatui.archive.gz").write_bytes(stdout)
            return
    src = cfg.data_dir / "chatui"
    if src.is_dir():
        with tarfile.open(dest / "chatui-db.tar.gz", "w:gz") as tar:
            tar.add(src, arcname="chatui")


def run_backup(cfg: Config, db: DB, when: datetime | None = None) -> Path:
    """Backup into data/backups/YYYY-MM-DD/: sparkhub.db (includes the ntfy topic), grafana.db, Arcane's two
    databases, chat-ui's history (see _chat_backup), and secrets.tar.gz (TLS CA and server certificate/key, the OIDC
    signing key, Arcane's and chat-ui's keys, the old data/ntfy-topic file if present). Configuration, presets and
    dashboards are in git; Prometheus history is not backed up (re-creatable, large)."""
    when = when or datetime.now(timezone.utc)
    root = cfg.data_dir / "backups"
    dest = root / when.strftime("%Y-%m-%d")
    tmp = root / (dest.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        _sqlite_copy(cfg.db_path, tmp / "sparkhub.db")
        for src, name in ((cfg.data_dir / "grafana" / "grafana.db", "grafana.db"),
                          (cfg.data_dir / "arcane" / "arcane.db", "arcane.db"),
                          (cfg.data_dir / "arcane" / "arcane.francis.db", "arcane.francis.db")):
            if src.exists():
                _sqlite_copy(src, tmp / name)
        if cfg.chat_container:
            _chat_backup(cfg, tmp)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for rel in ("tls", "oidc", "ntfy-topic", "arcane.env", "chatui.env"):
                p = cfg.data_dir / rel
                if p.exists():
                    tar.add(p, arcname=rel)
        (tmp / "secrets.tar.gz").write_bytes(buf.getvalue())
        for f in tmp.iterdir():
            f.chmod(0o600)
        tmp.chmod(0o700)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    size = sum(f.stat().st_size for f in dest.iterdir())
    # prune: keep the newest KEEP_DAYS daily backups (and anything that isn't a dated backup directory)
    dated = sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 10 and p.name[4] == "-")
    for old in dated[:-KEEP_DAYS]:
        shutil.rmtree(old, ignore_errors=True)
    BACKUP_LAST.set(time.time())
    BACKUP_SIZE.set(size)
    db.event("backup", None, path=str(dest.relative_to(cfg.data_dir)), bytes=size)
    log.info("backup written to %s (%d bytes)", dest, size)
    return dest


def last_backup_time(cfg: Config) -> float | None:
    root = cfg.data_dir / "backups"
    if not root.exists():
        return None
    dated = sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 10 and p.name[4] == "-")
    return dated[-1].stat().st_mtime if dated else None


def check_cert(name: str, cafile: str | None, host: str = "127.0.0.1", port: int = 443) -> tuple[bool, float | None]:
    """Connect to nginx with SNI `name`, verify against the system CAs (or `cafile`), return (ok, notAfter)."""
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=name) as tls:
                cert = tls.getpeercert()
        return True, ssl.cert_time_to_seconds(cert["notAfter"])
    except (OSError, ssl.SSLError, KeyError, ValueError) as e:
        log.warning("certificate check for %s failed: %s", name, e)
        return False, None


def check_certs(cfg: Config) -> dict[str, dict]:
    out = {}
    for name, cafile in cfg.tls_check_names():
        ok, exp = check_cert(name, cafile, port=cfg.tls_port)
        CERT_OK.labels(name).set(1 if ok else 0)
        if exp:
            CERT_EXPIRY.labels(name).set(exp)
        out[name] = {"ok": ok, "expires": exp, "days_left": round((exp - time.time()) / 86400, 1) if exp else None}
    return out
