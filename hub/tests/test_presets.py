"""Presets render to the benchmark's exact containers, and the Docker body is safe."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from sparkhub.config import Config
from sparkhub.db import DB
from sparkhub.presets import Presets
from sparkhub.runtime import DockerRuntime

REPO = Path(__file__).resolve().parents[2]


def presets(tmp_path):
    return Presets(Config(presets_dir=REPO / "presets", data_dir=tmp_path), DB(tmp_path / "t.db"))


def benchmark_run():
    """The `docker run` argument list serve.sh produced for the benchmark (captured 2026-10-28): docker options,
    image, then `bash -c SCRIPT` whose last line is `exec vllm serve ...`."""
    argv = json.loads((REPO / "presets/quality/benchmark-docker-run-args.json").read_text())["argv"]
    img = next(i for i, a in enumerate(argv) if a.startswith("ghcr.io/ursuciprian/spark-vllm-b12x@"))
    opts, cmd = argv[:img], argv[img + 1:]
    env = dict(opts[i + 1].split("=", 1) for i in range(len(opts)) if opts[i] == "-e")
    binds = [opts[i + 1] for i in range(len(opts)) if opts[i] == "-v"]
    ulimits = [opts[i + 1] for i in range(len(opts)) if opts[i] == "--ulimit"]
    assert cmd[:2] == ["bash", "-c"]
    script = cmd[2]
    seed, serve = script.split("exec vllm serve", 1)
    return argv[img], opts, env, binds, ulimits, seed, [t for t in shlex.split(serve) if t.strip()]   # minus the line breaks


def test_quality_matches_benchmark(tmp_path):
    # the benchmark ran with the HF cache in the user's home; render with the same host path
    cfg = Config(presets_dir=REPO / "presets", data_dir=tmp_path, host_hf_cache="~/.cache/huggingface",
                 host_models_dir="~/models")
    spec = Presets(cfg, DB(tmp_path / "t.db")).render("quality")
    image, opts, env, binds, ulimits, seed, serve = benchmark_run()
    assert spec["image"] == image
    assert spec["env"] == env
    assert spec["ulimits"] == {"memlock": -1, "stack": 67108864} and ulimits == ["memlock=-1", "stack=67108864"]
    assert spec["ipc"] == "host" and spec["gpus"] == "all"
    # same seeding of the compile caches, then exec of the same vllm serve command line
    assert spec["args"][:2] == ["bash", "-c"] and spec["args"][3:6] == ["b12x", "vllm", "serve"]
    norm = lambda t: " ".join(t.replace(";", " ").split())    # the preset joins the script's lines with "; "
    assert norm(spec["args"][2]) == norm(seed + ' exec "$@"')
    args = spec["args"][6:]
    # what differs on purpose: the served name (clients keep using qwen3.8-flash-next) and the effort-alias chat
    # template (same template as the checkpoint's, plus high/max -> xhigh, minimal -> low, which the gateway relies on)
    i = serve.index("--served-model-name")
    serve[i + 1] = "qwen3.8-flash-next"
    assert args == serve + ["--chat-template", "/qwen38/chat_template.jinja"]
    # the HF cache read-only at sparkrun's path, a writable runtime cache, plus the chat template
    assert spec["binds"][0] == binds[0] == "~/.cache/huggingface:/cache/huggingface:ro"
    assert spec["binds"][1] == "~/models/.runtime/quality:/cache/runtime" and binds[1].endswith(":/cache/runtime")
    assert spec["binds"][2].endswith("/quality/chat_template.jinja:/qwen38/chat_template.jinja:ro")
    # what differs on purpose: no isolated network (SparkHub publishes the port on 127.0.0.1 instead)
    assert "--network" in opts and "spark-bench-isolated" in opts


def test_quality_template_is_the_checkpoints_plus_effort_aliases():
    tpl = (REPO / "presets/quality/chat_template.jinja").read_text().splitlines()
    assert "high/max -> xhigh" in tpl[0]
    assert "{'high': 'xhigh', 'max': 'xhigh', 'minimal': 'low'}" in "\n".join(tpl)


def test_fast_matches_benchmark(tmp_path):
    args = presets(tmp_path).render("fast")["args"]
    s = " ".join(args)
    for flag in ("--kv-cache-dtype fp8", "--attention-backend flashinfer", "--moe-backend marlin",
                 "--gpu-memory-utilization 0.55", "--max-model-len 131072", "--max-num-seqs 4",
                 "--max-num-batched-tokens 8192", "--enable-chunked-prefill", "--async-scheduling",
                 "--enable-prefix-caching", "--enable-prompt-tokens-details",
                 '--speculative-config {"method":"mtp","num_speculative_tokens":3,"moe_backend":"triton"}',
                 "--reasoning-parser qwen3", "--tool-call-parser qwen3_xml", "--enable-auto-tool-choice",
                 "--trust-remote-code"):
        assert flag in s, flag


def test_docker_body_is_local_and_no_restart(tmp_path):
    rt = DockerRuntime("/nonexistent.sock", "img", tmp_path / "logs")
    for mode in ("fast", "quality"):
        spec = presets(tmp_path).render(mode)
        body = rt._create_body(spec)
        hc = body["HostConfig"]
        assert hc["RestartPolicy"] == {"Name": "no"}                       # never restarted by Docker
        bindings = hc["PortBindings"]["8000/tcp"]
        assert bindings == [{"HostIp": "127.0.0.1", "HostPort": str(spec["host_port"])}]   # 127.0.0.1 only
        assert "NetworkMode" not in hc and not hc.get("Privileged")
        assert body["Labels"]["sparkhub.mode"] == mode
        # Quality's b12x loader mlock()s its weights: memlock unlimited, like the benchmark's --ulimit
        assert hc.get("Ulimits", []) == ([{"Name": "memlock", "Soft": -1, "Hard": -1},
                                          {"Name": "stack", "Soft": 67108864, "Hard": 67108864}] if mode == "quality" else [])
