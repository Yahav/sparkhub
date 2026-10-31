"""Installs: the readiness check (image + weights before a switch), installed modes, and the instance label that keeps
two installs on one machine from touching each other's model containers."""
from __future__ import annotations

import asyncio
import json

import httpx

from sparkhub import runtime as rt
from sparkhub.presets import Presets


def fake_weights(d, shards=("a.safetensors", "b.safetensors"), skip=()):
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {f"w{i}": s for i, s in
                                                                              enumerate(shards)}}))
    for s in shards:
        if s not in skip:
            (d / s).write_text("x")


def test_weights_problems(hub, tmp_path):
    p: Presets = hub.hub.presets
    hub.cfg.host_models_dir = str(tmp_path / "models")
    assert "weights not found" in p.weights_problems("fast")[0]
    fake_weights(tmp_path / "models" / "qwen36", skip=("b.safetensors",))
    assert "1 of 2 files missing" in p.weights_problems("fast")[0]
    (tmp_path / "models" / "qwen36" / "b.safetensors").write_text("x")
    assert p.weights_problems("fast") == []


def test_switch_refused_until_installed(hub, tmp_path):
    hub.cfg.check_install = True
    hub.cfg.host_hf_cache = str(tmp_path / "hf")
    modes = hub.hub.modes
    asyncio.run_coroutine_threadsafe(modes.check_install(), hub.loop).result(5)
    assert modes.install["quality"] and "weights not found" in modes.install["quality"][0]
    with hub.client(hub.admin_key) as c:
        r = c.post("/api/v1/mode", json={"mode": "quality"})
        assert r.status_code == 409 and "cannot be loaded" in r.json()["error"]
        assert c.get("/api/v1/status").json()["install"]["quality"]
        fake_weights(hub.hub.presets.weights_dir("quality"))
        asyncio.run_coroutine_threadsafe(modes.check_install(), hub.loop).result(5)
        # Quality (b12x) also needs one shard of the base repo next to its own checkpoint
        assert len(modes.install["quality"]) == 1 and "model-00035-of-00036.safetensors missing" in \
            modes.install["quality"][0]
        mx = (tmp_path / "hf/hub/models--local-inference-lab--Qwen3.8-Flash-Next-NVFP4/snapshots"
              "/7c4f1bc1a2d6847e0cbc01ac6b823f00251de8dd")
        mx.mkdir(parents=True)
        (mx / "model-00035-of-00036.safetensors").write_text("x")
        asyncio.run_coroutine_threadsafe(modes.check_install(), hub.loop).result(5)
        assert modes.install["quality"] == []
        assert c.post("/api/v1/mode", json={"mode": "quality"}).status_code == 202
    hub.wait_state("ready", "quality")


def test_missing_weights_never_stop_the_running_model(hub, tmp_path):
    """The cached check said yes, the disk says no: the switch aborts before the loaded model is touched."""
    hub.cfg.check_install = True
    hub.cfg.host_hf_cache = str(tmp_path / "hf")
    modes = hub.hub.modes
    modes.install["quality"] = []          # stale "ok"
    with hub.client(hub.admin_key) as c:
        assert c.post("/api/v1/mode", json={"mode": "quality"}).status_code == 202
    st = hub.wait_state("ready", "fast")
    assert st["mode"] == "fast" and "cannot be loaded" in (modes.last_switch["error"] or "")
    assert "sparkhub-fast" not in hub.runtime.stopped


def test_mode_not_enabled(make_hub):
    h = make_hub()
    h.cfg.modes_enabled = ("fast",)
    h.wait_state("ready", "fast")
    asyncio.run_coroutine_threadsafe(h.hub.modes.check_install(), h.loop).result(5)
    with h.client(h.admin_key) as c:
        r = c.post("/api/v1/mode", json={"mode": "quality"})
        assert r.status_code == 409 and "not installed on this machine" in r.json()["error"]


def test_instance_names_and_ports(tmp_path):
    from sparkhub.config import Config
    from sparkhub.db import DB
    from .conftest import REPO
    cfg = Config(data_dir=tmp_path, presets_dir=REPO / "presets", instance="sh2", mode_ports={"fast": 28001})
    p = Presets(cfg, DB(":memory:"))
    assert p["fast"].container == "sh2-fast" and p["fast"].host_port == 28001
    assert p["quality"].container == "sh2-quality" and p["quality"].host_port == 18300


def test_runtime_only_sees_its_own_instance(tmp_path):
    listing = [
        {"State": "running", "Names": ["/sparkhub-quality"], "Labels": {rt.LABEL: "quality"}},   # pre-Phase-7
        {"State": "running", "Names": ["/sh2-fast"], "Labels": {rt.LABEL: "fast", rt.INSTANCE_LABEL: "sh2"}},
    ]

    def handler(request):
        return httpx.Response(200, json=listing)

    async def run(instance):
        r = rt.DockerRuntime("/nonexistent.sock", "x", tmp_path / "logs", instance)
        await r.client.aclose()
        r.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://docker/v1.43")
        try:
            return await r.running()
        finally:
            await r.client.aclose()
    assert asyncio.run(run("sparkhub")) == {"quality": "sparkhub-quality"}
    assert asyncio.run(run("sh2")) == {"fast": "sh2-fast"}
    body = rt.DockerRuntime("/nonexistent.sock", "x", tmp_path / "logs", "sh2")._create_body(
        {"container_port": 8000, "binds": [], "host_port": 1, "image": "i", "args": [], "env": {}, "mode": "fast"})
    assert body["Labels"] == {rt.LABEL: "fast", rt.INSTANCE_LABEL: "sh2"}


def test_switch_page_shows_what_is_missing(hub):
    from .test_auth import enrolled_client
    hub.cfg.modes_enabled = ("fast",)
    c, _ = enrolled_client(hub)
    r = c.get("/switch")
    assert "Not ready to load: not installed on this machine" in r.text
    assert 'value="quality"' not in r.text          # no switch button for it
