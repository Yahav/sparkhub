"""Custom mode: recipe translation, import, review and approval, download, assignment, loading,
smoke checks, registries and update notices. GitHub, Hugging Face and the image registry are answered in-process."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

import httpx
import pyotp
import pytest

from sparkhub import fetch_weights
from sparkhub import recipes as rx

from .test_auth import csrf_of, enrolled_client, secret

AUX_SHA = "a" * 40
MODEL_SHA = "b" * 40
COMMIT1 = "1" * 40
COMMIT2 = "2" * 40
DIGEST = "sha256:" + "d" * 64

RECIPE = f'''recipe_version: "1"
name: test-model-1x
description: "A test recipe for one Spark"
model: org/Test-Model-NVFP4
model_revision: main
container: ghcr.io/someone/vllm-test:v1
runtime: vllm
solo_only: true
defaults:
  port: 8000
  host: 0.0.0.0
  tensor_parallel: 1
  max_model_len: 131072
  max_num_seqs: 8
  kv_cache_memory_bytes: 15032385536
  gpu_memory_utilization: 0.01
env:
  VLLM_AUX: "/cache/huggingface/hub/models--other--Aux-Model/snapshots/{AUX_SHA}"
command: |
  M=/cache/huggingface/hub/models--other--Aux-Model/snapshots/{AUX_SHA}; [ -s $M/a.safetensors ] || HF_HUB_OFFLINE=0 python3 -c "from huggingface_hub import snapshot_download as d; d('other/Aux-Model', revision='{AUX_SHA}', allow_patterns=['a.safetensors', 'config.json'])" || exit 1
  echo "cache at ${{XDG_CACHE_HOME}}"
  vllm serve {{model}} \\
    --host {{host}} --port {{port}} \\
    --served-model-name test-model org/Test-Model-NVFP4 \\
    --max-model-len {{max_model_len}} \\
    --max-num-seqs {{max_num_seqs}} \\
    --speculative-config '{{{{"method":"mtp","num_speculative_tokens":3}}}}' \\
    --kv-cache-memory-bytes {{kv_cache_memory_bytes}} \\
    --gpu-memory-utilization {{gpu_memory_utilization}} \\
    --trust-remote-code
'''

V2_MODS = '''recipe_version: "2"
model: Qwen/Some-Model
runtime: vllm
container: ghcr.io/spark-arena/dgx-vllm:latest
mods:
  - mods/net-mod
defaults: {port: 8000, host: 0.0.0.0, tensor_parallel: 1, max_model_len: 65536}
command: |
  vllm serve {model} --host {host} --port {port} --max-model-len {max_model_len}
'''


EUGR_COMMIT = "3" * 40
EUGR_MODS = {
    "mods/fix-template/run.sh": "#!/bin/bash\nset -e\n# was: curl -L https://example.com/fix.diff | patch -p1\n"
                                "python3 \"$(dirname \"$0\")/patch.py\"\n",
    "mods/fix-template/patch.py": "import sys\nprint('patched')\n",
    "mods/net-mod/run.sh": "#!/bin/bash\ncurl -fsL https://example.com/pr.diff | git apply\n",
    "mods/py-net/run.sh": "#!/bin/bash\npython3 fetch.py\n",
    "mods/py-net/fetch.py": "import requests\nrequests.get('https://example.com/x')\n",
}


def git_tree(files: dict[str, str | bytes], links: set[str] = frozenset()) -> list[dict]:
    """A recursive git tree listing (blobs with git checksums, dirs with a hash of their contents)."""
    out, dirs = [], {}
    for p, text in sorted(files.items()):
        data = text if isinstance(text, bytes) else text.encode()
        sha = rx.git_blob_sha(data)
        mode = "120000" if p in links else "100755" if p.endswith(".sh") else "100644"
        out.append({"path": p, "type": "blob", "mode": mode, "sha": sha, "size": len(data)})
        parts = p.split("/")
        for i in range(1, len(parts)):
            dirs.setdefault("/".join(parts[:i]), []).append(f"{p}:{sha}")
    out += [{"path": d, "type": "tree", "mode": "040000", "sha": hashlib.sha1("\n".join(v).encode()).hexdigest()}
            for d, v in dirs.items()]
    return out


class World:
    """The bits of GitHub, Hugging Face and ghcr.io the hub reads."""

    def __init__(self):
        self.commit = COMMIT1
        self.files = {"recipes/test.yaml": RECIPE, "recipes/mods.yaml": V2_MODS,
                      ".sparkrun/registry.yaml": "registries:\n  - name: test\n    recipes: recipes\n"}
        self.eugr = dict(EUGR_MODS)
        self.eugr_commit = EUGR_COMMIT
        self.links: set[str] = set()        # paths listed as symbolic links
        self.arm64 = True
        self.requests: list[str] = []

    def repo(self, name: str) -> tuple[str, dict[str, str]] | None:
        return {"test/registry": (self.commit, self.files),
                "spark-arena/eugr-recipes": (self.eugr_commit, self.eugr)}.get(name)

    def hf(self, repo: str) -> dict:
        if repo == "org/Test-Model-NVFP4":
            sib = [{"rfilename": "config.json", "size": 120, "blobId": "c" * 40},
                   {"rfilename": "model-00001-of-00002.safetensors", "size": 40 * 2**30,
                    "lfs": {"sha256": "e" * 64, "size": 40 * 2**30}},
                   {"rfilename": "model-00002-of-00002.safetensors", "size": 30 * 2**30,
                    "lfs": {"sha256": "f" * 64, "size": 30 * 2**30}}]
            return {"sha": MODEL_SHA, "siblings": sib, "cardData": {"license": "apache-2.0"},
                    "lastModified": "2026-10-01T00:00:00Z"}
        if repo == "other/Aux-Model":
            sib = [{"rfilename": "a.safetensors", "size": 2**30, "lfs": {"sha256": "1" * 64, "size": 2**30}},
                   {"rfilename": "b.safetensors", "size": 2**30, "lfs": {"sha256": "2" * 64, "size": 2**30}},
                   {"rfilename": "config.json", "size": 50, "blobId": "9" * 40}]
            return {"sha": AUX_SHA, "siblings": sib, "tags": ["license:other"]}
        if repo == "Qwen/Some-Model":
            return {"sha": "5" * 40, "siblings": [], "cardData": {"license": "apache-2.0"}}
        return {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = req.url
        self.requests.append(str(u))
        host, path = u.host, u.path
        if host == "api.github.com":
            m = re.match(r"/repos/([^/]+/[^/]+)(/.*)?$", path)
            repo = self.repo(m.group(1)) if m else None
            if repo and m.group(2) == "/commits":
                return httpx.Response(200, json=[{"sha": repo[0]}])
            if repo and (m.group(2) or "").startswith("/git/trees/"):
                return httpx.Response(200, json={"tree": git_tree(repo[1], self.links)})
            if repo and not m.group(2):
                return httpx.Response(200, json={"default_branch": "main"})
        if host == "raw.githubusercontent.com":
            m = re.match(r"/([^/]+/[^/]+)/([0-9a-f]{40})/(.+)", path)
            repo = self.repo(m.group(1)) if m else None
            if repo and m.group(3) in repo[1]:
                body = repo[1][m.group(3)]
                return httpx.Response(200, content=body if isinstance(body, bytes) else body.encode())
        if host == "huggingface.co":
            m = re.match(r"/api/models/([^/]+/[^/]+)/revision/", path)
            if m and self.hf(m.group(1)):
                return httpx.Response(200, json=self.hf(m.group(1)))
            if path.endswith("/config.json"):
                return httpx.Response(200, json={"architectures": ["X"]})
        if host == "ghcr.io":
            if path == "/token":
                return httpx.Response(200, json={"token": "t"})
            if req.headers.get("authorization") != "Bearer t":
                return httpx.Response(401, headers={"www-authenticate":
                                                    'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})
            if path == "/v2/someone/vllm-test/manifests/v1":
                arch = "arm64" if self.arm64 else "amd64"
                return httpx.Response(200, headers={"docker-content-digest": DIGEST}, json={
                    "manifests": [{"digest": "sha256:" + "7" * 64, "platform": {"os": "linux", "architecture": arch}}]})
            if path == "/v2/someone/vllm-test/manifests/sha256:" + "7" * 64:
                return httpx.Response(200, json={"config": {"digest": "sha256:" + "8" * 64},
                                                 "layers": [{"size": 5 * 2**30}, {"size": 2**30}]})
            if path == "/v2/someone/vllm-test/blobs/sha256:" + "8" * 64:
                return httpx.Response(200, json={"architecture": "arm64", "created": "2026-10-17T00:00:00Z",
                                                 "config": {"Labels": {"org.opencontainers.image.source": "x"}},
                                                 "history": [{"created_by": "RUN pip install vllm"}]})
        return httpx.Response(404, text="not found")


@pytest.fixture
def world(hub):
    w = World()
    hub.hub.custom.transport = httpx.MockTransport(w.handler)
    return w


def fresh_code(hub, ahead=30):
    return pyotp.TOTP(secret(hub)).at(time.time() + ahead)


def wait(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not reached")


# ------------------------------------------------------------------------------------------------- translation
def test_translate():
    r = rx.parse(RECIPE)
    r["model_revision"] = MODEL_SHA
    t = rx.translate(r, {"qwen3.8-flash-next"})
    script = t.spec["args"][2]
    last = script.splitlines()[-1]
    assert t.spec["args"][:2] == ["bash", "-c"] and t.spec["served_name"] == "test-model"
    assert last.startswith("exec vllm serve org/Test-Model-NVFP4 ")
    assert last.endswith(f"--revision {MODEL_SHA} --served-model-name test-model --host 0.0.0.0 --port 8000")
    assert "org/Test-Model-NVFP4 --max" not in last.replace("serve org/Test-Model-NVFP4 ", "")  # old names gone
    assert last.count("--served-model-name") == 1 and last.count("--port") == 1
    assert "--max-model-len ${max_model_len}" in last and "--max-num-seqs ${max_num_seqs}" in last
    assert "'{\"method\":\"mtp\",\"num_speculative_tokens\":3}'" in last          # {{ }} → literal braces
    assert "--gpu-memory-utilization 0.01" in last and "--kv-cache-memory-bytes 15032385536" in last
    assert t.editable == ["max_model_len", "max_num_seqs"]          # 0.01 is outside the hub's range: fixed
    assert "gpu_memory_utilization" in t.spec["unused_settings"] and t.spec["defaults"]["max_model_len"] == 131072
    assert t.extra_repos == [{"repo": "other/Aux-Model", "revision": AUX_SHA,
                              "patterns": ["a.safetensors", "config.json"]}]
    risky = [ln for ln in t.lines if ln["risky"]]
    assert len(risky) == 1 and "downloads from Hugging Face" in risky[0]["risky"]
    assert t.flags and "trust-remote-code" in t.flags[0]
    assert t.spec["env"]["HF_HUB_OFFLINE"] == "1" and t.spec["network"] == "internal"
    assert t.spec["ulimits"] == {"memlock": -1, "stack": 67108864}
    # a served name another mode uses gets a suffix
    assert rx.translate(r, {"test-model"}).spec["served_name"] == "test-model-custom"


@pytest.mark.parametrize("change, why", [
    (lambda r: r.update(runtime="sglang"), "only vLLM"),
    (lambda r: r.update(min_nodes=2), "needs 2 Sparks"),
    (lambda r: r["defaults"].update(tensor_parallel=2), "more than one GPU"),
    (lambda r: r.update(build_args=["X=1"]), "local image build"),
    (lambda r: r.update(mods="mods/x"), "list of mod names"),
    (lambda r: r.update(pre_exec=["pip install x"]), "hook commands (pre_exec)"),
    (lambda r: r.update(post_commands=["echo"]), "hook commands (post_commands)"),
    (lambda r: r.update(command="vllm serve {model} --foo {nope}"), "{nope}"),
    (lambda r: r.update(command="vllm serve {model}\necho done"), "must end with the vllm serve"),
    (lambda r: r.pop("command"), "has no command"),
])
def test_rejections(change, why):
    r = rx.parse(RECIPE)
    change(r)
    with pytest.raises(rx.RecipeError, match=re.escape(why)):
        rx.translate(r, set())


def test_docker_spec(tmp_path):
    """The container a recipe gets: internal network, nothing published, the entrypoint override."""
    from sparkhub.runtime import DockerRuntime
    r = rx.parse(RECIPE)
    r["model_revision"] = MODEL_SHA
    r["executor_config"] = {"entrypoint": ""}
    t = rx.translate(r, set())
    assert t.spec["entrypoint"] == ""
    rt = DockerRuntime(str(tmp_path / "no.sock"), "img", tmp_path / "logs", instance="sparkhub")
    body = rt._create_body({"mode": "custom", "container": "sparkhub-custom", "image": "x@sha256:1", "env": {},
                            "binds": [], "args": ["bash", "-c", "true"], "host_port": 18320, "container_port": 8000,
                            "network": "internal", "entrypoint": "", "ulimits": {"memlock": -1}})
    hc = body["HostConfig"]
    assert hc["NetworkMode"] == "sparkhub-isolated" and "PortBindings" not in hc and body["Entrypoint"] == [""]
    assert hc["Ulimits"] == [{"Name": "memlock", "Soft": -1, "Hard": -1}]
    body = rt._create_body({"mode": "fast", "container": "c", "image": "i", "env": {}, "binds": [], "args": [],
                            "host_port": 8001, "container_port": 8000})
    assert "NetworkMode" not in body["HostConfig"] and "Entrypoint" not in body
    assert body["HostConfig"]["PortBindings"] == {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}]}


def test_parse_errors():
    for text, why in (("a: [", "not valid YAML"), ("- 1", "not a mapping"), ("recipe_version: '9'", "unsupported"),
                      ('include: other', "builds on another recipe")):
        with pytest.raises(rx.RecipeError, match=why):
            rx.parse(text)
    assert rx.parse_github_url("https://github.com/o/r/blob/main/a/b.yaml") == ("o/r", "main", "a/b.yaml")
    assert rx.parse_github_url("https://raw.githubusercontent.com/o/r/abc/x.yaml") == ("o/r", "abc", "x.yaml")
    assert rx.parse_github_url("https://github.com/o/r") == ("o/r", "", "")
    with pytest.raises(rx.RecipeError):
        rx.parse_github_url("http://github.com/o/r/blob/main/x.yaml")
    assert rx.Lookups.split_image("vllm/vllm-openai:v1") == ("registry-1.docker.io", "vllm/vllm-openai", "v1", "")
    assert rx.Lookups.split_image("ubuntu") == ("registry-1.docker.io", "library/ubuntu", "latest", "")
    assert rx.Lookups.split_image("ghcr.io/a/b@sha256:1") == ("ghcr.io", "a/b", "", "sha256:1")


# ------------------------------------------------------------------------------------------------- the whole flow
def import_test_recipe(c, url="https://github.com/test/registry/blob/main/recipes/test.yaml"):
    r = c.post("/custom/import", data={"csrf": csrf_of(c), "url": url}, follow_redirects=False)
    assert r.status_code == 303 and re.match(r"/custom/\d+\?msg=imported", r.headers["location"]), r.headers
    return int(re.search(r"/custom/(\d+)", r.headers["location"]).group(1))


def test_custom_flow(hub, world):
    c, _ = enrolled_client(hub)
    # nothing assigned: no third model anywhere, and a clear refusal
    with hub.client(hub.admin_key) as k:
        ids = [m["id"] for m in k.get("/v1/models").json()["data"]]
        assert "custom" not in ids and "fast" in ids
        r = k.post("/v1/chat/completions", json={"model": "custom", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 404 and "no recipe assigned" in r.json()["error"]["message"]
        r = k.post("/api/v1/mode", json={"mode": "custom"})
        assert r.status_code == 409 and "no recipe assigned" in r.text
    assert "No recipe assigned" in c.get("/switch").text and 'href="#custom"' in c.get("/switch").text
    r = c.get("/custom?msg=hi")                                       # old address: the section on the Models page
    assert r.status_code == 303 and r.headers["location"] == "/switch?msg=hi#custom"

    rid = import_test_recipe(c)
    d = hub.hub.custom.row(rid)
    assert d["state"] == "pending" and d["status"] == "pending review"
    assert d["spec"]["image"] == f"ghcr.io/someone/vllm-test@{DIGEST}"                  # pinned
    assert d["spec"]["revision"] == MODEL_SHA and d["facts"]["model"]["resolved"]      # "main" → commit
    assert d["source"]["commit"] == COMMIT1 and d["facts"]["download_gib"] == 71.0
    assert [f["path"] for f in d["facts"]["files"] if f["repo"] == "other/Aux-Model"] == ["a.safetensors", "config.json"]
    assert d["facts"]["memory"]["estimate_gib"] == pytest.approx(70 + 1 + 14 + 4, abs=0.2)
    page = c.get(f"/custom/{rid}").text
    for s in ("pending review", DIGEST, MODEL_SHA, "apache-2.0", "trust-remote-code", "downloads from Hugging Face",
              "Review checklist", "Approve", "custom</code> or <code>test-model"):
        assert s in page, s
    # not loadable, not assignable, no download before approval
    for action in ("download", "assign"):
        r = c.post(f"/custom/{rid}/{action}", data={"csrf": csrf_of(c)}, follow_redirects=False)
        assert "error=" in r.headers["location"]
    # approval needs the box and a fresh code
    r = c.post(f"/custom/{rid}/approve", data={"csrf": csrf_of(c), "code": fresh_code(hub)}, follow_redirects=False)
    assert "error=" in r.headers["location"] and hub.hub.custom.row(rid)["state"] == "pending"
    r = c.post(f"/custom/{rid}/approve", data={"csrf": csrf_of(c), "code": "000000", "ack": "on"}, follow_redirects=False)
    assert "Wrong%20code" in r.headers["location"]
    r = c.post(f"/custom/{rid}/approve", data={"csrf": csrf_of(c), "code": fresh_code(hub), "ack": "on"},
               follow_redirects=False)
    assert "msg=Approved" in r.headers["location"]
    ev = hub.db.one("SELECT * FROM events WHERE kind='recipe_approved'")
    assert DIGEST in ev["detail"] and MODEL_SHA in ev["detail"] and ev["actor"] == "user:admin"

    # download and verify
    r = c.post(f"/custom/{rid}/download", data={"csrf": csrf_of(c)}, follow_redirects=False)
    assert "msg=Download" in r.headers["location"]
    wait(lambda: hub.hub.custom.row(rid)["status"] == "ready")
    assert hub.runtime.pulled == [f"ghcr.io/someone/vllm-test@{DIGEST}"]
    files = hub.runtime.fetched[-1]
    assert {(f["repo"], f["path"]) for f in files} >= {("org/Test-Model-NVFP4", "config.json"),
                                                      ("other/Aux-Model", "a.safetensors")}
    assert all(f.get("sha256") or f.get("git_oid") for f in files)

    # assign: Custom appears for clients and on the Model page
    r = c.post(f"/custom/{rid}/assign", data={"csrf": csrf_of(c)}, follow_redirects=False)
    assert "msg=Assigned" in r.headers["location"]
    with hub.client(hub.infer_key) as k:
        ids = [m["id"] for m in k.get("/v1/models").json()["data"]]
        assert "custom" in ids and "custom-thinking" in ids
    assert "Switch to custom" in c.get("/switch").text
    settings = c.get("/settings?tab=models").text
    assert "imported recipe" in settings and 'action="/settings/custom"' in settings
    r = c.post("/settings/custom", data={"csrf": csrf_of(c), "max_model_len": "65536", "max_num_seqs": "4",
                                         "default_max_tokens": "8000"}, follow_redirects=False)
    assert "saved=custom" in r.headers["location"]

    # load it: isolated network, the recipe's command with the hub's values
    with hub.client(hub.admin_key) as k:
        assert k.post("/api/v1/mode", json={"mode": "custom", "force": True}).status_code == 202
    hub.wait_state("ready", "custom")
    spec = hub.runtime.started[-1]
    assert spec["mode"] == "custom" and spec["network"] == "internal" and spec["image"].endswith(DIGEST)
    script = spec["args"][2]
    assert "--max-model-len 65536" in script and "--max-num-seqs 4" in script and "${XDG_CACHE_HOME}" in script
    assert f"sparkhub-custom-{rid}-runtime:/cache/runtime" in spec["binds"]
    assert spec["binds"][0].endswith(":/cache/huggingface:ro") and spec["env"]["HF_HUB_OFFLINE"] == "1"
    with hub.client(hub.infer_key) as k:
        r = k.post("/v1/chat/completions", json={"model": "custom", "messages": [{"role": "user", "content": "hi"}],
                                                 "max_tokens": 5})
        assert r.status_code == 200, r.text
        r = k.post("/v1/chat/completions", json={"model": "test-model", "messages": [{"role": "user", "content": "x"}],
                                                 "max_tokens": 5})
        assert r.status_code == 200
    assert hub.runtime.states["custom"].bodies[-1]["model"] == "test-model"
    m = hub.hub.custom.row(rid)["measured"]
    assert m["kv_tokens"] == 320000 and m["load_seconds"] is not None

    # while loaded: no reassigning, unassigning or deleting
    for action, data in (("unassign", {}), ("delete", {"confirm": "test-model-1x"})):
        r = c.post(f"/custom/{rid}/{action}", data={"csrf": csrf_of(c), **data}, follow_redirects=False)
        assert "error=" in r.headers["location"], action
    # smoke checks against the loaded model
    assert "Run smoke checks" in c.get(f"/custom/{rid}").text
    r = c.post(f"/custom/{rid}/smoke", data={"csrf": csrf_of(c)}, follow_redirects=False)
    assert "msg=Smoke" in r.headers["location"], r.headers["location"]
    smoke = hub.hub.custom.row(rid)["measured"]["smoke"]
    assert smoke["tool_call"]["ok"] and smoke["truncation"]["ok"] and smoke["prefix_cache"]["ok"]
    page = c.get(f"/custom/{rid}").text
    assert "Measured on this Spark" in page and "320,000" in page

    # back to Fast; unassign; delete
    with hub.client(hub.admin_key) as k:
        assert k.post("/api/v1/mode", json={"mode": "fast", "force": True}).status_code == 202
    hub.wait_state("ready", "fast")
    r = c.post(f"/custom/{rid}/unassign", data={"csrf": csrf_of(c)}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    with hub.client(hub.admin_key) as k:
        assert "custom" not in [m["id"] for m in k.get("/v1/models").json()["data"]]
        assert k.post("/api/v1/mode", json={"mode": "custom"}).status_code == 409
    r = c.post(f"/custom/{rid}/delete", data={"csrf": csrf_of(c), "confirm": "wrong"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = c.post(f"/custom/{rid}/delete", data={"csrf": csrf_of(c), "confirm": "test-model-1x"}, follow_redirects=False)
    assert hub.hub.custom.row(rid) is None and hub.runtime.volumes_removed == [f"sparkhub-custom-{rid}-runtime"]


def test_assignment_comes_back_from_the_database(hub, world):
    import asyncio
    rid = asyncio.run_coroutine_threadsafe(hub.hub.custom.import_url(
        "https://github.com/test/registry/blob/main/recipes/test.yaml", "test"), hub.loop).result(10)
    hub.hub.custom.approve(rid, "test")
    hub.loop.call_soon_threadsafe(hub.hub.custom.start_download, rid, "test")
    wait(lambda: hub.hub.custom.row(rid)["status"] == "ready")
    hub.hub.custom.assign(rid, "test")
    # what a restart does: fresh presets, then CustomManager.load() before the mode manager starts
    from sparkhub.presets import Presets
    fresh = Presets(hub.cfg, hub.db)
    assert "custom" not in fresh
    hub.hub.custom.presets = fresh
    hub.hub.custom.load()
    assert fresh["custom"].served_name == "test-model" and fresh["custom"].raw["recipe_id"] == rid
    # a failed download on the assigned recipe takes Custom away
    hub.db.execute("UPDATE recipes SET download=? WHERE id=?", (json.dumps({"state": "failed", "error": "x"}), rid))
    hub.hub.custom.load()
    assert "custom" not in fresh


def test_imports_refused(hub, world):
    c, _ = enrolled_client(hub)
    r = c.post("/custom/import", data={"csrf": csrf_of(c), "yaml": V2_MODS}, follow_redirects=False)
    assert "error=Import%20refused" in r.headers["location"] and "net-mod%20needs%20the%20network" in r.headers["location"]
    world.arm64 = False
    r = c.post("/custom/import", data={"csrf": csrf_of(c), "url": "https://github.com/test/registry/blob/main/recipes/"
                                                                   "test.yaml"}, follow_redirects=False)
    assert "arm64" in r.headers["location"]
    r = c.post("/custom/import", data={"csrf": csrf_of(c), "url": "https://example.com/x.yaml"}, follow_redirects=False)
    assert "GitHub" in r.headers["location"]
    assert hub.db.one("SELECT COUNT(*) n FROM recipes")["n"] == 0
    # a pasted recipe works (no source, no update notices)
    world.arm64 = True
    r = c.post("/custom/import", data={"csrf": csrf_of(c), "yaml": RECIPE}, follow_redirects=False)
    rid = int(re.search(r"/custom/(\d+)", r.headers["location"]).group(1))
    assert hub.hub.custom.row(rid)["source"]["kind"] == "pasted"
    assert "pasted or uploaded" in c.get(f"/custom/{rid}").text


def test_updates_and_registry(hub, world):
    c, _ = enrolled_client(hub)
    sent = []
    hub.hub.notify = lambda *a, **k: sent.append(a)
    hub.hub.custom.notify = hub.hub.notify
    rid = import_test_recipe(c)
    r = c.post("/custom/check-updates", data={"csrf": csrf_of(c)}, follow_redirects=False)
    assert "none" in r.headers["location"]
    # upstream changes the file: one notice with the diff; nothing else changes
    world.commit = COMMIT2
    world.files["recipes/test.yaml"] = RECIPE.replace("max_num_seqs: 8", "max_num_seqs: 6")
    r = c.post("/custom/check-updates", data={"csrf": csrf_of(c)}, follow_redirects=False)
    assert "1%20new" in r.headers["location"]
    info = hub.hub.custom.row(rid)["update_info"]
    assert info["commit"] == COMMIT2 and "-  max_num_seqs: 8" in info["diff"] and "+  max_num_seqs: 6" in info["diff"]
    assert sent and "update" in sent[0][0]
    assert "update available" in c.get("/switch").text and "Import this version" in c.get(f"/custom/{rid}").text
    import asyncio
    n = asyncio.run_coroutine_threadsafe(hub.hub.custom.check_updates(), hub.loop).result(10)
    assert n == 0 and len(sent) == 1                      # the same commit is not reported twice
    r = c.post(f"/custom/{rid}/update", data={"csrf": csrf_of(c)}, follow_redirects=False)
    new = int(re.search(r"/custom/(\d+)", r.headers["location"]).group(1))
    d = hub.hub.custom.row(new)
    assert d["parent_id"] == rid and d["state"] == "pending" and d["source"]["commit"] == COMMIT2
    assert hub.hub.custom.row(rid)["update_info"] is None

    # registry: off by default; a repo added by URL is listed with what can run here
    assert all(not g["enabled"] for g in hub.hub.custom.registries())
    r = c.post("/custom/registries", data={"csrf": csrf_of(c), "action": "add",
                                           "url": "https://github.com/test/registry"}, follow_redirects=False)
    assert r.status_code == 303
    g = next(g for g in hub.hub.custom.registries() if g["url"] == "https://github.com/test/registry")
    byp = {x["path"]: x for x in g["recipes"]}
    assert byp["recipes/test.yaml"]["problems"] == [] and byp["recipes/test.yaml"]["license"] == "apache-2.0"
    assert "net-mod needs the network" in byp["recipes/mods.yaml"]["problems"][0]
    # a listing made by an older hub is fetched again on start; a current one isn't
    import asyncio
    from sparkhub import custom as cm
    cache = hub.db.get_setting("registry_cache")
    assert cache["https://github.com/test/registry"]["v"] == cm.REGISTRY_CACHE_V
    before = len(world.requests)
    asyncio.run_coroutine_threadsafe(hub.hub.custom.refresh_registries(outdated_only=True), hub.loop).result(10)
    assert len(world.requests) == before
    cache["https://github.com/test/registry"]["v"] = 1
    hub.db.set_setting("registry_cache", cache)
    asyncio.run_coroutine_threadsafe(hub.hub.custom.refresh_registries(outdated_only=True), hub.loop).result(10)
    assert len(world.requests) > before
    assert hub.db.get_setting("registry_cache")["https://github.com/test/registry"]["v"] == cm.REGISTRY_CACHE_V
    page = c.get("/switch").text
    assert "1 of 2 can run here" in page and "recipes/test.yaml" in page and "recipes/mods.yaml" not in page
    assert "recipes/mods.yaml" in c.get("/switch?all=1").text
    r = c.post("/custom/import", data={"csrf": csrf_of(c), "registry": "https://github.com/test/registry",
                                       "path": "recipes/test.yaml"}, follow_redirects=False)
    assert "msg=imported" in r.headers["location"]


def test_custom_is_admin_only(hub, world):
    from .test_users import add_user, first_login
    admin, _ = enrolled_client(hub)
    add_user(admin, "sam", "switch")
    sam = first_login(hub, "sam")
    assert sam.get("/custom").status_code == 403
    assert 'id="custom"' not in sam.get("/switch").text                # switchers see Fast/Quality/Custom, not recipes
    assert sam.post("/custom/import", data={"csrf": csrf_of(sam), "yaml": RECIPE}).status_code == 403
    assert 'href="/custom"' not in sam.get("/switch").text and "An admin can set one up" in sam.get("/switch").text


# ------------------------------------------------------------------------------------------------- the downloader
def test_fetch_weights(tmp_path):
    big = os.urandom(3 * 1024 * 1024 + 17)
    small = b'{"a": 1}'
    oid = hashlib.sha1(f"blob {len(small)}\0".encode() + small).hexdigest()
    files = [{"repo": "o/m", "revision": MODEL_SHA, "path": "w/model.safetensors", "size": len(big),
              "sha256": hashlib.sha256(big).hexdigest(), "git_oid": None},
             {"repo": "o/m", "revision": MODEL_SHA, "path": "config.json", "size": len(small), "sha256": None,
              "git_oid": oid}]
    served = {"model.safetensors": big, "config.json": small}
    hits = []

    def handler(req):
        name = req.url.path.rsplit("/", 1)[-1]
        hits.append((name, req.headers.get("range")))
        data = served[name]
        rng = req.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data)
    t = httpx.MockTransport(handler)
    assert fetch_weights.Run(tmp_path, files, "https://hf.test", t).run() == 0
    snap = tmp_path / "hub" / "models--o--m" / "snapshots" / MODEL_SHA
    assert (snap / "w" / "model.safetensors").read_bytes() == big and (snap / "config.json").read_bytes() == small
    assert os.readlink(snap / "config.json") == f"../../blobs/{oid}"
    assert os.readlink(snap / "w" / "model.safetensors").startswith("../../../blobs/")
    # again: checked, not downloaded
    hits.clear()
    assert fetch_weights.Run(tmp_path, files, "https://hf.test", t).run() == 0 and hits == []
    # a partial download resumes with a range request
    blob = tmp_path / "hub" / "models--o--m" / "blobs" / files[0]["sha256"]
    blob.rename(blob.with_name(blob.name + ".incomplete"))
    blob.with_name(blob.name + ".incomplete").write_bytes(big[:1000])
    assert fetch_weights.Run(tmp_path, files, "https://hf.test", t).run() == 0
    assert ("model.safetensors", "bytes=1000-") in hits and blob.read_bytes() == big
    # a wrong checksum fails the run and leaves nothing behind
    bad = [dict(files[0], path="other.safetensors", sha256="0" * 64)]
    served["other.safetensors"] = big
    assert fetch_weights.Run(tmp_path, bad, "https://hf.test", t).run() == 1
    assert not (tmp_path / "hub" / "models--o--m" / "blobs" / ("0" * 64)).exists()
    # a corrupted cached file is reported, not trusted
    blob.write_bytes(b"x" * len(big))
    assert fetch_weights.Run(tmp_path, files[:1], "https://hf.test", t).run() == 1


def test_lenient_placeholders(hub):
    # a recipe's shell ${VAR} survives rendering; only the hub's setting names are filled in
    r = rx.parse(RECIPE)
    r["model_revision"] = MODEL_SHA
    t = rx.translate(r, set())
    t.spec["image"] = "x@sha256:1"
    hub.hub.presets.set_custom(t.spec, 7)
    spec = hub.hub.presets.render("custom")
    assert "${XDG_CACHE_HOME}" in spec["args"][2] and "--max-model-len 131072" in spec["args"][2]
    assert spec["container_port"] == 8000 and spec["network"] == "internal"
    hub.hub.presets.set_custom(None)
    assert hub.hub.presets.available == ["fast", "quality"]


# ------------------------------------------------------------------------------------------------- mods
RECIPE_MODS = RECIPE.replace("solo_only: true\n", "solo_only: true\nmods:\n  - mods/local-fix\n  - fix-template\n")
LOCAL_FIX = {"recipes/mods/local-fix/run.sh": "#!/bin/bash\necho local fix\n",
             "recipes/mods/local-fix/data/template.jinja": "{{ messages }}\n"}


def lookups(world):
    return rx.Lookups(httpx.MockTransport(world.handler))


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_mod_resolution(world):
    world.files.update(LOCAL_FIX)
    src = rx.Source("github", "", "test/registry", "main", "recipes/withmods.yaml", COMMIT1)

    async def go(ref, s=src):
        lk = lookups(world)
        try:
            return await lk.resolve_mod(ref, s)
        finally:
            await lk.close()
    m = run(go("mods/local-fix"))                          # next to the recipe, in its own repo and commit
    assert (m["repo"], m["commit"], m["dir"]) == ("test/registry", COMMIT1, "recipes/mods/local-fix")
    assert {f["path"] for f in m["files"]} == {"run.sh", "data/template.jinja"} and m["network"] == []
    assert next(f for f in m["files"] if f["path"] == "run.sh")["exec"]
    m = run(go("fix-template"))                            # the eugr fallback, pinned to its head
    assert (m["repo"], m["commit"], m["name"]) == ("spark-arena/eugr-recipes", EUGR_COMMIT, "fix-template")
    assert m["network"] == []                              # a curl in a comment doesn't count
    assert run(go("@eugr/fix-template"))["dir"] == "mods/fix-template"
    assert run(go("fix-template", rx.Source("pasted")))["repo"] == "spark-arena/eugr-recipes"
    hits = run(go("net-mod"))["network"]
    assert hits[0]["file"] == "run.sh" and hits[0]["line"] == 2 and "downloads with curl/wget" in hits[0]["why"]
    assert "opens network connections" in run(go("py-net"))["network"][0]["why"]
    with pytest.raises(rx.RecipeError, match=r"mod nope not found \(looked in test/registry/recipes/nope, "):
        run(go("nope"))
    with pytest.raises(rx.RecipeError, match="unknown registry"):
        run(go("@elsewhere/x"))
    with pytest.raises(rx.RecipeError, match="not a mod name"):
        run(go("../../etc"))
    # a file that doesn't match its git checksum, a symlink, a binary file
    real = world.handler

    def tamper(req):
        if req.url.path.endswith("/patch.py"):
            return httpx.Response(200, text="import os\nos.system('x')\n")
        return real(req)
    world.handler = tamper
    with pytest.raises(rx.RecipeError, match="does not match its git checksum"):
        run(go("fix-template"))
    world.handler = real
    world.eugr["mods/bin-mod/run.sh"] = "#!/bin/bash\n"
    world.eugr["mods/bin-mod/x.so"] = b"\x7fELF\xff\xfe"
    with pytest.raises(rx.RecipeError, match="x.so is a binary file"):
        run(go("bin-mod"))
    del world.eugr["mods/bin-mod/x.so"]
    world.links.add("mods/bin-mod/run.sh")
    with pytest.raises(rx.RecipeError, match="symbolic link"):
        run(go("bin-mod"))


def test_mod_prelude_and_translation():
    r = rx.parse(RECIPE_MODS)
    r["model_revision"] = MODEL_SHA
    t = rx.translate(r, set(), ["local-fix", "fix-template"])
    lines = t.spec["args"][2].splitlines()
    assert lines[0].startswith("mkdir -p /workspace/mods && cp -a /tmp/sparkhub-mods/local-fix /workspace/mods/ && ")
    assert "cd /workspace/mods/local-fix && chmod +x run.sh && ./run.sh) || { echo" in lines[0]
    assert "mod fix-template failed" in lines[1] and lines[-1].startswith("exec vllm serve")
    assert t.lines[0]["risky"] == ["runs a mod (shown below)"]
    from sparkhub.runtime import upload_tar
    import io
    import tarfile
    tar = tarfile.open(fileobj=io.BytesIO(upload_tar([{"path": "local-fix/run.sh", "data": "x", "exec": True},
                                                      {"path": "local-fix/d/t.j", "data": "y", "exec": False}])))
    members = {m.name: m for m in tar.getmembers()}
    assert set(members) == {"sparkhub-mods", "sparkhub-mods/local-fix", "sparkhub-mods/local-fix/run.sh",
                            "sparkhub-mods/local-fix/d", "sparkhub-mods/local-fix/d/t.j"}
    assert members["sparkhub-mods/local-fix/run.sh"].mode == 0o755 and members["sparkhub-mods/local-fix/d/t.j"].mode == 0o644


def test_mods_flow(hub, world):
    world.files.update(LOCAL_FIX)
    world.files["recipes/withmods.yaml"] = RECIPE_MODS
    c, _ = enrolled_client(hub)
    rid = import_test_recipe(c, "https://github.com/test/registry/blob/main/recipes/withmods.yaml")
    d = hub.hub.custom.row(rid)
    assert [m["name"] for m in d["mods"]] == ["local-fix", "fix-template"]
    assert [m["name"] for m in d["facts"]["mods"]] == ["local-fix", "fix-template"]
    assert "text" not in d["facts"]["mods"][0]["files"][0]                    # the text lives in the mods column
    assert d["spec"]["mods_digest"] == rx.mods_digest(d["mods"])
    page = c.get(f"/custom/{rid}").text
    for s in ("Mods", "spark-arena/eugr-recipes", EUGR_COMMIT[:12], "python3 &#34;$(dirname", "echo local fix",
              "runs a mod", "template.jinja"):
        assert s in page, s
    ev = json.loads(hub.db.one("SELECT detail FROM events WHERE kind='recipe_imported'")["detail"])
    assert ev["mods"] == [f"test/registry/recipes/mods/local-fix@{COMMIT1[:12]}",
                          f"spark-arena/eugr-recipes/mods/fix-template@{EUGR_COMMIT[:12]}"]
    r = c.post(f"/custom/{rid}/approve", data={"csrf": csrf_of(c), "code": fresh_code(hub), "ack": "on"},
               follow_redirects=False)
    assert "msg=Approved" in r.headers["location"]
    ev = json.loads(hub.db.one("SELECT detail FROM events WHERE kind='recipe_approved'")["detail"])
    assert ev["mods_digest"] == d["spec"]["mods_digest"] and len(ev["mods"]) == 2

    # download: the mods run once, offline, in the recipe's own container setup
    hub.runtime.run_once_result = {"exit_code": 1, "log": "[fix-template] unsupported vLLM layout\n"}
    c.post(f"/custom/{rid}/download", data={"csrf": csrf_of(c)}, follow_redirects=False)
    wait(lambda: hub.hub.custom.row(rid)["status"] == "download failed")
    err = hub.hub.custom.row(rid)["download"]["error"]
    assert "offline test run (exit 1)" in err and "unsupported vLLM layout" in err
    hub.runtime.run_once_result = None
    c.post(f"/custom/{rid}/download", data={"csrf": csrf_of(c)}, follow_redirects=False)
    wait(lambda: hub.hub.custom.row(rid)["status"] == "ready")
    assert hub.hub.custom.row(rid)["download"]["mods_tested"] == 2
    spec = hub.runtime.ran_once[-1]
    assert spec["container"] == "sparkhub-modcheck" and spec["network"] == "internal"
    assert spec["image"] == f"ghcr.io/someone/vllm-test@{DIGEST}" and spec["labels"] == {"sparkhub.helper": "modcheck"}
    assert "vllm serve" not in spec["args"][2] and spec["args"][2].endswith("echo '[sparkhub] mods applied'")
    assert {u["path"] for u in spec["uploads"]} == {"local-fix/run.sh", "local-fix/data/template.jinja",
                                                     "fix-template/run.sh", "fix-template/patch.py"}
    assert f"sparkhub-custom-{rid}-runtime:/cache/runtime" in spec["binds"][1]

    # assign and load: the files go into the container, the mods run before the server
    c.post(f"/custom/{rid}/assign", data={"csrf": csrf_of(c)}, follow_redirects=False)
    with hub.client(hub.admin_key) as k:
        assert k.post("/api/v1/mode", json={"mode": "custom", "force": True}).status_code == 202
        hub.wait_state("ready", "custom")
        spec = hub.runtime.started[-1]
        assert spec["mode"] == "custom" and len(spec["uploads"]) == 4
        script = spec["args"][2].splitlines()
        assert "local-fix" in script[0] and "fix-template" in script[1] and script[-1].startswith("exec vllm serve")
        api = k.get("/api/v1/modes/custom/spec").json()
        assert {"path": "local-fix/run.sh", "bytes": len("#!/bin/bash\necho local fix\n")} in api["uploads"]
        assert "text" not in api["uploads"][0] and "data" not in api["uploads"][0]
        assert k.post("/api/v1/mode", json={"mode": "fast", "force": True}).status_code == 202
    hub.wait_state("ready", "fast")

    # a stored mod file edited behind the approval's back: Custom is withdrawn
    mods = hub.hub.custom.row(rid)["mods"]
    mods[1]["files"][0]["text"] += "curl https://evil.example\n"
    hub.db.execute("UPDATE recipes SET mods=? WHERE id=?", (json.dumps(mods), rid))
    hub.hub.custom.load()
    assert "custom" not in hub.hub.presets


def test_mods_in_registry_listing(hub, world):
    world.files.update(LOCAL_FIX)
    world.files["recipes/withmods.yaml"] = RECIPE_MODS
    c, _ = enrolled_client(hub)
    c.post("/custom/registries", data={"csrf": csrf_of(c), "action": "add", "url": "https://github.com/test/registry"},
           follow_redirects=False)
    c.post("/custom/registries", data={"csrf": csrf_of(c), "action": "refresh"}, follow_redirects=False)
    g = next(g for g in hub.hub.custom.registries() if g["url"] == "https://github.com/test/registry")
    r = next(x for x in g["recipes"] if x["path"] == "recipes/withmods.yaml")
    assert r["problems"] == [] and r["mods"] == ["local-fix", "fix-template"]
    page = c.get("/switch").text
    assert "2 of 3 can run here" in page and "needs 2 mods" in page and "mods: local-fix, fix-template" in page


def test_locally_built_images_are_refused(hub, world):
    world.files["recipes/local.yaml"] = RECIPE.replace("container: ghcr.io/someone/vllm-test:v1", "container: vllm-node")
    c, _ = enrolled_client(hub)
    c.post("/custom/registries", data={"csrf": csrf_of(c), "action": "add", "url": "https://github.com/test/registry"},
           follow_redirects=False)
    c.post("/custom/registries", data={"csrf": csrf_of(c), "action": "refresh"}, follow_redirects=False)
    g = next(g for g in hub.hub.custom.registries() if g["url"] == "https://github.com/test/registry")
    byp = {x["path"]: x for x in g["recipes"]}
    assert "vllm-node is not published in any registry" in byp["recipes/local.yaml"]["problems"][0]
    assert byp["recipes/test.yaml"]["problems"] == []
    r = c.post("/custom/import", data={"csrf": csrf_of(c), "yaml": world.files["recipes/local.yaml"]},
               follow_redirects=False)
    assert "built%20locally" in r.headers["location"]


def test_mod_updates(hub, world):
    world.files["recipes/withmods.yaml"] = RECIPE_MODS.replace("  - mods/local-fix\n", "")
    c, _ = enrolled_client(hub)
    sent = []
    hub.hub.custom.notify = lambda *a, **k: sent.append(a)
    rid = import_test_recipe(c, "https://github.com/test/registry/blob/main/recipes/withmods.yaml")
    import asyncio
    check = lambda: asyncio.run_coroutine_threadsafe(hub.hub.custom.check_updates(), hub.loop).result(10)
    assert check() == 0
    # the eugr mod changes; the recipe file doesn't
    world.eugr_commit = "4" * 40
    world.eugr["mods/fix-template/patch.py"] = "import sys\nprint('patched v2')\n"
    assert check() == 1
    info = hub.hub.custom.row(rid)["update_info"]
    assert info["mods_changed"] == ["fix-template"] and not info["recipe_changed"]
    assert "+print('patched v2')" in info["diff"] and "-print('patched')" in info["diff"]
    assert "changed mods (fix-template)" in sent[0][1]
    page = c.get(f"/custom/{rid}").text
    assert "fix-template" in page and "Import this version" in page
    assert check() == 0 and len(sent) == 1                     # reported once
    # another commit in eugr that doesn't touch the mod: nothing new
    world.eugr_commit = "5" * 40
    world.eugr["mods/other/run.sh"] = "#!/bin/bash\n"
    assert check() == 0
    r = c.post(f"/custom/{rid}/update", data={"csrf": csrf_of(c)}, follow_redirects=False)
    new = int(re.search(r"/custom/(\d+)", r.headers["location"]).group(1))
    d = hub.hub.custom.row(new)
    assert d["state"] == "pending" and d["parent_id"] == rid and d["mods"][0]["commit"] == "5" * 40
    assert "patched v2" in d["mods"][0]["files"][0]["text"] + d["mods"][0]["files"][1]["text"]
