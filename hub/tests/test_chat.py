"""chat-ui at /chat/: the on/off setting, the nginx gate (/auth/check-chat), the container and the backup."""
from __future__ import annotations

import asyncio
import struct
import tarfile
import threading
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response

from sparkhub import admin_cli, maintenance

from .test_auth import csrf_of, enrolled_client

CHAT = "sparkhub-chatui"


def wait(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_not_installed(hub):
    c, _ = enrolled_client(hub)
    assert c.get("/auth/check-chat").status_code == 403
    assert 'action="/settings-chat"' not in c.get("/settings").text
    assert 'href="/chat/"' not in c.get("/").text
    r = c.get("/chat-off")
    assert r.status_code == 503 and "not installed" in r.text
    r = c.post("/settings-chat", data={"csrf": csrf_of(c), "enabled": "on"}, follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_toggle_starts_stops_and_gates(make_hub):
    h = make_hub(chat_container=CHAT, apps={CHAT: False})
    h.wait_state("ready", "fast")
    assert wait(lambda: h.runtime.apps[CHAT])            # on by default: the hub starts the stopped container
    with h.client() as anon:
        assert anon.get("/auth/check-chat").status_code == 401
    c, _ = enrolled_client(h)
    r = c.get("/auth/check-chat")
    assert r.status_code == 204 and r.headers["x-auth-user"] == "admin"
    assert 'href="/chat/"' in c.get("/").text
    assert "container: running" in c.get("/settings").text

    r = c.post("/settings-chat", data={"csrf": csrf_of(c)}, follow_redirects=False)    # checkbox unticked = off
    assert r.status_code == 303 and "saved=chat%20off" in r.headers["location"]
    assert h.runtime.apps[CHAT] is False
    assert c.get("/auth/check-chat").status_code == 403
    assert 'href="/chat/"' not in c.get("/").text
    r = c.get("/chat-off")
    assert r.status_code == 503 and "turned off" in r.text
    ev = h.db.one("SELECT actor FROM events WHERE kind='chat_disabled'")
    assert ev["actor"] == "user:admin"

    # compose (install/update) starting it again while it is off: the hub stops it
    h.runtime.apps[CHAT] = True
    asyncio.run_coroutine_threadsafe(h.hub.apply_chat(), h.loop).result(5)
    assert h.runtime.apps[CHAT] is False

    # CSRF is required
    assert c.post("/settings-chat", data={"enabled": "on"}, follow_redirects=False).headers["location"] == "/login"
    c.post("/settings-chat", data={"csrf": csrf_of(c), "enabled": "on"})
    assert h.runtime.apps[CHAT] is True and c.get("/auth/check-chat").status_code == 204


def test_admin_cli_chat(hub, monkeypatch, capsys):
    monkeypatch.setenv("SPARKHUB_DATA_DIR", str(hub.cfg.data_dir))
    assert admin_cli.main(["chat", "off"]) == 0
    assert hub.db.get_setting("chat_enabled") is False
    admin_cli.main(["chat", "status"])
    assert capsys.readouterr().out.strip().endswith("off")
    admin_cli.main(["chat", "on"])
    assert hub.db.get_setting("chat_enabled") is True


def _frame(stream: int, data: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(data)) + data


def _fake_docker(sock: str, running: dict):
    """The few Docker Engine endpoints the backup uses, on a unix socket."""
    app = FastAPI()

    @app.get("/v1.43/containers/{name}/json")
    async def inspect(name: str):
        return {"State": {"Running": running["on"]}}

    @app.post("/v1.43/containers/{name}/exec")
    async def exec_create(name: str, body: dict):
        assert body["Cmd"][0] == "mongodump"
        return {"Id": "e1"}

    @app.post("/v1.43/exec/e1/start")
    async def exec_start():
        return Response(_frame(1, b"ARCHIVE-") + _frame(2, b"log line") + _frame(1, b"BYTES"),
                        media_type="application/vnd.docker.raw-stream")

    @app.get("/v1.43/exec/e1/json")
    async def exec_json():
        return {"ExitCode": 0}

    server = uvicorn.Server(uvicorn.Config(app, uds=sock, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    assert wait(lambda: server.started)
    return server


def test_backup_includes_chat_history(hub, tmp_path):
    cfg = hub.cfg
    cfg.chat_container = CHAT
    cfg.docker_socket = str(tmp_path / "docker.sock")
    (cfg.data_dir / "chatui").mkdir()
    (cfg.data_dir / "chatui" / "collection-0.wt").write_bytes(b"wiredtiger")
    (cfg.data_dir / "chatui.env").write_text("OPENAI_API_KEY=x\n")
    running = {"on": True}
    server = _fake_docker(cfg.docker_socket, running)
    try:
        dest = maintenance.run_backup(cfg, hub.db)
        assert (dest / "chatui.archive.gz").read_bytes() == b"ARCHIVE-BYTES"       # stdout only
        assert not (dest / "chatui-db.tar.gz").exists()
        with tarfile.open(dest / "secrets.tar.gz") as t:
            assert "chatui.env" in t.getnames()
        running["on"] = False                     # stopped: the data directory itself is consistent
        dest = maintenance.run_backup(cfg, hub.db)
        assert not (dest / "chatui.archive.gz").exists()
        with tarfile.open(dest / "chatui-db.tar.gz") as t:
            assert "chatui/collection-0.wt" in t.getnames()
    finally:
        server.should_exit = True
