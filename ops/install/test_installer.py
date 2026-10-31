"""Installer checks that need no Docker: templates render for every TLS/feature combination, nothing is left
unresolved, and the generated files follow the answers. Run: .venv/bin/pytest -q ops/install"""
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import installer as I  # noqa: E402

BASE = {**I.detect_defaults_only(), **{
    "INSTANCE": "sparkhub", "RUN_UID": "1000", "RUN_GID": "1000", "DOCKER_GID": "988", "TZ": "UTC",
    "HF_CACHE": "/h/hf", "MODELS_DIR": "/h/models", "MODES": "fast quality", "ACCESS": "tailscale",
    "PUBLIC_HOST": "spark.example.net",
    "EXTRA_NAMES": "spark.tail1.ts.net spark", "LISTEN_ADDRS": "100.64.1.2", "HTTPS_PORT": "443", "HTTP_PORT": "80",
    "TLS": "local-ca", "TLS_CHALLENGE": "http", "LETSENCRYPT_ISSUED": "0", "HUB_PORT": "8090",
    "PROMETHEUS_PORT": "9090", "NODE_EXPORTER_PORT": "9100", "GRAFANA_PORT": "3000", "FAST_PORT": "8001",
    "QUALITY_PORT": "18300", "AUTOSTART": "1", "ARCANE": "1", "ISSUER_ADDR": "100.64.1.2", "NVIDIA_DASHBOARD": "1"}}


def test_render_text_blocks():
    t = "a\n{{#X}}\nx={{V}}\n{{^nothing}}\n{{/X}}\n{{#Y}}\ny\n{{/Y}}\nb\n"
    assert I.render_text(t.replace("{{^nothing}}\n", ""), {"X": "1", "Y": "0", "V": "v"}) == "a\nx=v\nb\n"
    with pytest.raises(I.Abort):
        I.render_text("{{MISSING}}", {})


@pytest.mark.parametrize("tls,issued,arcane,nvidia,chatui", [
    ("local-ca", "0", "1", "1", "1"), ("letsencrypt", "0", "1", "0", "0"), ("letsencrypt", "1", "0", "1", "1"),
    ("letsencrypt", "1", "1", "1", "0"), ("local-ca", "0", "0", "0", "0")])
def test_every_combination_renders(tls, issued, arcane, nvidia, chatui):
    conf = {**BASE, "TLS": tls, "LETSENCRYPT_ISSUED": issued, "ARCANE": arcane, "NVIDIA_DASHBOARD": nvidia,
            "CHATUI": chatui}
    files = I.rendered(conf)
    for p, txt in files.items():
        if txt is not None:
            assert "{{" not in txt and "}}" not in txt.replace("}}}", ""), p
    compose = files["compose.yaml"]
    assert ("arcane:" in compose) == (arcane == "1")
    assert ("/etc/letsencrypt" in compose) == (tls == "letsencrypt" and issued == "1")
    assert (files["nginx/listen-public.conf"] is None) == (not (tls == "letsencrypt" and issued == "1"))
    assert ("/nvidia/" in files["nginx/sparkhub-locations.inc"]) == (nvidia == "1")
    if arcane == "1":
        assert ("SSL_CERT_FILE" in compose) == (files["nginx/listen-public.conf"] is None)
    doc = yaml.safe_load(compose)
    assert ("chatui" in doc["services"]) == (chatui == "1")
    assert set(doc.get("networks") or {}) == {n for n, on in (("arcane", arcane), ("chatui", chatui)) if on == "1"}
    hub_env = doc["services"]["hub"]["environment"]
    assert (hub_env.get("SPARKHUB_CHAT_CONTAINER") == "sparkhub-chatui") == (chatui == "1")
    locs = files["nginx/sparkhub-locations.inc"]
    assert ("location /chat/" in locs) == (chatui == "1")
    if chatui == "1":
        env = doc["services"]["chatui"]["environment"]
        assert env["OPENAI_BASE_URL"] == "https://spark.example.net/v1"
        assert env["OPENID_PROVIDER_URL"] == "https://spark.example.net/oidc"
        models = json.loads(env["MODELS"])
        assert [m["id"] for m in models if not m.get("unlisted")] == ["auto", "auto-thinking"]
        assert ("NODE_EXTRA_CA_CERTS" in env) == (files["nginx/listen-public.conf"] is None)
        assert "Content-Security-Policy" in locs and "auth_request /_auth_chat;" in locs


def test_listen_addresses_never_wildcard_443():
    files = I.rendered(BASE)
    listen = files["nginx/listen-sparkhub.conf"]
    assert re.findall(r"listen (\S+) ssl", listen) == ["100.64.1.2:443", "127.0.0.1:443"]
    # without Let's Encrypt HTTP-01, port 80 is bound to the listen addresses too, not to every interface
    assert "listen 80;" not in listen and "listen 100.64.1.2:80;" in listen
    le = I.rendered({**BASE, "TLS": "letsencrypt"})["nginx/listen-sparkhub.conf"]
    assert "listen 80;" in le


def test_side_by_side_ports_and_names():
    conf = {**BASE, "INSTANCE": "sh2", "HUB_PORT": "18090", "HTTPS_PORT": "18443", "PROMETHEUS_PORT": "19090",
            "GRAFANA_PORT": "13000", "FAST_PORT": "28001"}
    files = I.rendered(conf)
    c = files["compose.yaml"]
    assert "name: sh2" in c and "container_name: sh2-hub" in c and '"--port", "18090"' in c
    assert "SPARKHUB_OIDC_ISSUER: https://spark.example.net:18443/oidc" in c
    assert "127.0.0.1:18090" in files["nginx/nginx.conf"]
    assert '"127.0.0.1:28001"' in files["observability/prometheus.yml"]
    assert "return 301 https://$host:18443$request_uri" in files["nginx/listen-sparkhub.conf"]


def test_conf_round_trip(tmp_path):
    p = tmp_path / "sparkhub.conf"
    I.save_conf(BASE, p)
    assert I.load_conf(p) == {k: v for k, v in BASE.items()}
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_access_inferred_for_older_conf():
    assert I.access({"TLS": "letsencrypt", "PUBLIC_HOST": "llm.example.com"}) == "public"
    assert I.access({"TLS": "local-ca", "PUBLIC_HOST": "spark.tail1.ts.net"}) == "tailscale"
    assert I.access({"TLS": "local-ca", "PUBLIC_HOST": "spark.local"}) == "local"
    assert I.access({"ACCESS": "local", "TLS": "letsencrypt"}) == "local"


@pytest.mark.parametrize("mode,tls,issued", [
    ("local", "local-ca", "0"), ("tailscale", "local-ca", "0"), ("tailscale", "tailscale", "0"),
    ("tailscale", "tailscale", "1"), ("public", "letsencrypt", "1")])
def test_access_modes_render(mode, tls, issued):
    host = {"local": "spark.local", "tailscale": "spark.tail1.ts.net", "public": "llm.example.com"}[mode]
    conf = {**BASE, "ACCESS": mode, "TLS": tls, "PUBLIC_HOST": host, "CHATUI": "1",
            "LETSENCRYPT_ISSUED": issued if tls == "letsencrypt" else "0",
            "TAILSCALE_CERT_ISSUED": issued if tls == "tailscale" else "0"}
    files = I.rendered(conf)
    trusted = issued == "1"
    pub = files["nginx/listen-public.conf"]
    assert (pub is not None) == trusted
    if trusted:
        want = "/etc/nginx/tls/tailscale.crt" if tls == "tailscale" else f"/etc/letsencrypt/live/{host}/fullchain.pem"
        assert f"ssl_certificate     {want};" in pub and f"server_name {host};" in pub
    doc = yaml.safe_load(files["compose.yaml"])
    assert doc["services"]["hub"]["environment"]["SPARKHUB_PUBLIC_CERT_TRUSTED"] == ("1" if trusted else "0")
    assert ("NODE_EXTRA_CA_CERTS" in doc["services"]["chatui"]["environment"]) == (not trusted)
    assert ("/etc/letsencrypt:/etc/letsencrypt:ro" in doc["services"]["nginx"]["volumes"]) == (tls == "letsencrypt")
    # ca.crt over plain http only where port 80 does not face the internet
    assert ("location = /ca.crt" in files["nginx/listen-sparkhub.conf"]) == (mode != "public")




@pytest.mark.parametrize("mode,answers,want", [
    ("local", ["local", "", ""],
     {"TLS": "local-ca", "PUBLIC_HOST": "spark.local", "LISTEN_ADDRS": "192.168.1.5", "EXTRA_NAMES": "spark",
      "ISSUER_ADDR": "192.168.1.5"}),
    ("tailscale", ["tailscale", "y", "y", ""],
     {"TLS": "tailscale", "PUBLIC_HOST": "spark.tail1.ts.net", "LISTEN_ADDRS": "100.64.1.2 192.168.1.5",
      "EXTRA_NAMES": "spark spark.local", "ISSUER_ADDR": "100.64.1.2"}),
    ("tailscale", ["tailscale", "n", "n", ""],
     {"TLS": "local-ca", "PUBLIC_HOST": "spark.tail1.ts.net", "LISTEN_ADDRS": "100.64.1.2", "EXTRA_NAMES": "spark",
      "ISSUER_ADDR": "100.64.1.2"}),
    ("public", ["public", "llm.example.com", "http", ""],
     {"TLS": "letsencrypt", "PUBLIC_HOST": "llm.example.com", "LISTEN_ADDRS": "192.168.1.5 100.64.1.2",
      "EXTRA_NAMES": "spark.tail1.ts.net spark spark.local", "ISSUER_ADDR": "192.168.1.5"}),
])
def test_configure_access(monkeypatch, mode, answers, want):
    monkeypatch.setattr(I, "net_facts", lambda: I.Net("192.168.1.5", "100.64.1.2", "spark.tail1.ts.net", "spark"))
    monkeypatch.setattr(I, "ensure_tailscale", lambda ctx: None)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    d = {**BASE, "ACCESS": "local", "TLS": "local-ca", "TLS_CHALLENGE": "http"}
    I.configure_access(d, {})
    assert d["ACCESS"] == mode
    assert {k: d[k] for k in want} == want
    assert I.rendered(d)       # the answers render


def test_configure_access_keeps_answers_in_the_same_mode(monkeypatch):
    monkeypatch.setattr(I, "net_facts", lambda: I.Net("192.168.1.5", "100.64.1.2", "spark.tail1.ts.net", "spark"))
    old = {**BASE, "ACCESS": "", "TLS": "letsencrypt", "PUBLIC_HOST": "llm.example.com", "LISTEN_ADDRS": "192.168.1.5 100.64.1.2",
           "EXTRA_NAMES": "spark.tail1.ts.net spark", "ISSUER_ADDR": "192.168.1.5"}
    it = iter(["", "", "", ""])          # Enter everywhere: mode public (inferred), same name, check, addresses
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    d = dict(old)
    I.configure_access(d, old)
    assert d["ACCESS"] == "public"
    assert {k: d[k] for k in ("PUBLIC_HOST", "LISTEN_ADDRS", "EXTRA_NAMES", "ISSUER_ADDR", "TLS")} == \
        {k: old[k] for k in ("PUBLIC_HOST", "LISTEN_ADDRS", "EXTRA_NAMES", "ISSUER_ADDR", "TLS")}


@pytest.mark.parametrize("mode,tls,want", [
    ("local", "local-ca", ["trusts the SparkHub certificate", "spark → 192.168.1.5"]),
    ("tailscale", "tailscale", ["HTTPS Certificates", "installs Tailscale", "spark.tail1.ts.net → 192.168.1.5"]),
    ("tailscale", "local-ca", ["installs Tailscale", "trusts the SparkHub certificate"]),
    ("public", "letsencrypt", ["A record", "forward TCP 443 and 80 to 192.168.1.5", "hairpin"]),
])
def test_manual_steps_listed_before_install(capsys, mode, tls, want):
    host = {"local": "spark.local", "tailscale": "spark.tail1.ts.net", "public": "llm.example.com"}[mode]
    I.manual_steps({**BASE, "ACCESS": mode, "TLS": tls, "PUBLIC_HOST": host, "LISTEN_ADDRS": "100.64.1.2 192.168.1.5"})
    text = capsys.readouterr().out
    for w in want:
        assert w in text, w
    assert ("HTTPS Certificates" in text) == (tls == "tailscale")


def test_dns_a_parses_answers():
    import socket
    import threading
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def answer():
        q, addr = srv.recvfrom(512)
        qend = 12 + q[12:].index(b"\x00") + 5
        r = q[:2] + b"\x81\x80\x00\x01\x00\x02\x00\x00\x00\x00" + q[12:qend]
        r += b"\xc0\x0c\x00\x05\x00\x01\x00\x00\x00\x3c\x00\x02\xc0\x0c"          # a CNAME, skipped
        r += b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + bytes([192, 168, 1, 5])
        srv.sendto(r, addr)
    t = threading.Thread(target=answer)
    t.start()
    assert I.dns_a("127.0.0.1", "spark.tail1.ts.net", port=port) == ["192.168.1.5"]
    t.join()
    srv.close()
    assert I.dns_a("127.0.0.1", "x.example", timeout=0.2, port=port) is None     # nobody answers


def test_preflight(monkeypatch):
    ctx = I.Ctx(BASE)

    def fake(cmd, **kw):
        if "nvidia-smi" in cmd:
            return "NVIDIA GB10"
        if "info" in cmd:
            return '{"nvidia": {}}'
        return "v2" if "compose" in cmd or "buildx" in cmd else ""
    monkeypatch.setattr(I, "out", fake)
    monkeypatch.setattr(I.os, "access", lambda p, m: True)
    monkeypatch.setattr(I, "model_problems", lambda ctx, m: [])
    monkeypatch.setattr(I, "embed_problems", lambda ctx: [])
    good, msg = I.chk_preflight(ctx)
    assert good and "GB10" in msg, msg
    monkeypatch.setattr(I, "out", lambda cmd, **kw: "" if "buildx" in cmd else fake(cmd))
    good, msg = I.chk_preflight(ctx)                               # legacy builder only: chat-ui can't build
    assert not good and "docker buildx (BuildKit) missing" in msg, msg
    monkeypatch.setattr(I, "out", lambda cmd, **kw: "Some GPU" if "nvidia-smi" in cmd else fake(cmd))
    good, msg = I.chk_preflight(ctx)
    assert not good and "GPU is Some GPU" in msg, msg
    hub = yaml.safe_load(I.rendered(BASE)["compose.yaml"])["services"]["hub"]
    assert hub["deploy"]["resources"]["reservations"]["devices"][0]["driver"] == "nvidia"


def test_embeddings_step(monkeypatch, tmp_path):
    # an older sparkhub.conf has no EMBEDDINGS: off until asked for, like chat-ui
    hub = yaml.safe_load(I.rendered(BASE)["compose.yaml"])["services"]["hub"]
    assert hub["environment"]["SPARKHUB_EMBEDDINGS"] == ""
    conf = {**BASE, "EMBEDDINGS": "1", "MODELS_DIR": str(tmp_path)}
    hub = yaml.safe_load(I.rendered(conf)["compose.yaml"])["services"]["hub"]
    assert hub["environment"]["SPARKHUB_EMBEDDINGS"] == "1" and hub["environment"]["SPARKHUB_PORT_EMBED"] == "18310"
    ctx = I.Ctx(conf)
    step = next(s for s in I.STEPS if s.name == "embedding models")
    assert step.when(ctx) and not step.when(I.Ctx(BASE))
    # the manifest pins every file models.json downloads, by size and sha256
    man = I._manifest("embed")["files"]
    assert sorted(f["file"] for f in I.MODELS["embed"]["files"]) == sorted(man)
    assert all(len(m["sha256"]) == 64 and m["size"] > 100_000_000 for m in man.values())
    # the preset loads exactly those files, from the directory the installer fills
    preset = json.loads((I.ROOT / "presets" / "embed.json").read_text())
    assert preset["image"] == I.MODELS["embed"]["image"]
    assert {Path(s["weights"]).name for s in preset["services"].values()} == set(man)
    assert preset["binds"] == ["${models_dir}/embed:/models:ro"] and I.MODELS["embed"]["dir"] == "${MODELS_DIR}/embed"
    monkeypatch.setattr(I, "image_present", lambda ref: True)
    good, msg = I.chk_embed(ctx)
    assert not good and "missing" in msg
    d = tmp_path / "embed"
    d.mkdir()
    for name, m in man.items():
        with (d / name).open("wb") as f:
            f.truncate(m["size"])
    assert I.chk_embed(ctx)[0]                                     # sizes match (hashes only with --verify)
    assert "sha256 mismatch" in I.embed_problems(ctx, verify=True)[0]


def test_licenses_and_token_asked_up_front(monkeypatch, tmp_path):
    monkeypatch.setattr(I, "HF_TOKEN_FILE", tmp_path / "hf-token")
    monkeypatch.setattr(I, "save_conf", lambda conf: None)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    ctx = I.Ctx({**BASE, "MODES": "fast quality", "FLASH_LICENSE": ""})
    good, msg = I.chk_licenses(ctx)
    assert not good and "not accepted: Qwen Community License 1.0 (quality)" in msg and "not asked yet" in msg
    monkeypatch.setattr(I, "yes", lambda q, default=False: True)
    monkeypatch.setattr(I.sys.stdin, "isatty", lambda: False)        # unattended: the token is skipped, not asked
    I.do_licenses(ctx)
    assert ctx.conf["FLASH_LICENSE"].startswith("qwen-community-1.0 ")
    assert (tmp_path / "hf-token").read_text() == "" and (tmp_path / "hf-token").stat().st_mode & 0o777 == 0o600
    good, msg = I.chk_licenses(ctx)
    assert good and "anonymous downloads" in msg, msg
    # a token reaches the download container by name only, never on the command line
    (tmp_path / "hf-token").write_text("hf_secret\n")
    calls = []
    monkeypatch.setattr(I, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    I._hf(ctx, "img", ["download", "x/y"], [])
    cmd, kw = calls[0]
    assert "HF_TOKEN" in cmd and not any("hf_secret" in a for a in cmd) and kw["env"] == {"HF_TOKEN": "hf_secret"}


def test_local_cert_names_match_exactly(monkeypatch, tmp_path):
    crt = tmp_path / "data" / "tls" / "server.crt"
    crt.parent.mkdir(parents=True)
    crt.write_text("x")
    monkeypatch.setattr(I, "ROOT", tmp_path)
    san = ("notAfter=Apr 28 13:11:32 2099 GMT\nX509v3 Subject Alternative Name: \n    DNS:a1.spark-28.ssh.example.dev, "
           "DNS:vm1, DNS:vm1.local, DNS:localhost, IP Address:10.66.193.239, IP Address:127.0.0.1")
    monkeypatch.setattr(I, "out", lambda cmd, **kw: san)
    conf = {**BASE, "PUBLIC_HOST": "spark", "EXTRA_NAMES": "vm1 vm1.local", "LISTEN_ADDRS": "10.66.193.239"}
    good, msg = I.chk_local_cert(I.Ctx(conf))
    assert not good and msg == "does not cover spark", msg              # "spark" is inside "spark-28", not a name
    good, msg = I.chk_local_cert(I.Ctx({**conf, "PUBLIC_HOST": "vm1.local"}))
    assert good, msg


def test_new_listen_addresses_restart_nginx(monkeypatch):
    """A reload can't move :80 from 0.0.0.0 to one address (bind fails, nginx keeps the old config): restart."""
    calls = []
    monkeypatch.setattr(I, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(I, "active_requests", lambda ctx: 0)
    ctx = I.Ctx(dict(BASE))
    I.apply_changes(ctx, ["nginx/listen-sparkhub.conf", I.NGINX_LISTEN])
    assert ["docker", "restart", ctx.name("nginx")] in calls and not any("reload" in c for c in calls)
    calls.clear()
    I.apply_changes(ctx, ["nginx/sparkhub.conf"])                    # other nginx changes: a graceful reload
    assert calls[-1][-2:] == ["-s", "reload"] and not any(c[:2] == ["docker", "restart"] for c in calls)


def test_hub_learns_which_pieces_are_installed():
    env = lambda conf: yaml.safe_load(I.rendered(conf)["compose.yaml"])["services"]["hub"]["environment"]
    on = env({**BASE, "ARCANE": "1", "NVIDIA_DASHBOARD": "1"})
    off = env({**BASE, "ARCANE": "0", "NVIDIA_DASHBOARD": "0"})
    assert on["SPARKHUB_ARCANE"] == "1" and on["SPARKHUB_NVIDIA_DASHBOARD"] == "1"
    assert off["SPARKHUB_ARCANE"] == "" and off["SPARKHUB_NVIDIA_DASHBOARD"] == ""     # the hub reads "" as off


def test_arcane_built_from_the_pinned_fork_by_default(monkeypatch):
    labels = {}
    monkeypatch.setattr(I, "out", lambda cmd, **kw: json.dumps(labels["v"]) if labels.get("v") is not None else "")
    ctx = I.Ctx({**BASE, "ARCANE": "1", "ARCANE_SOURCE": ""})
    good, msg = I.chk_arcane_image(ctx)
    assert not good and "will build from Yahav/arcane@" + I.ARCANE_PIN["commit"][:12] in msg
    labels["v"] = {"sparkhub.arcane-commit": "0" * 40}                 # an older pin: rebuilt
    assert "will rebuild" in I.chk_arcane_image(ctx)[1]
    labels["v"] = {"sparkhub.arcane-commit": I.ARCANE_PIN["commit"]}
    assert I.chk_arcane_image(ctx)[0]
    labels["v"] = {}                                                   # own source: any image of it is fine
    assert I.chk_arcane_image(I.Ctx({**BASE, "ARCANE": "1", "ARCANE_SOURCE": "/src/arcane"}))[0]


def test_benchmarks_piece(monkeypatch, tmp_path):
    env = lambda conf: yaml.safe_load(I.rendered(conf)["compose.yaml"])["services"]["hub"]["environment"]
    on = env({**BASE, "BENCH": "1"})
    assert on["SPARKHUB_BENCH"] == "1" and on["SPARKHUB_BENCH_IMAGE"].endswith("-bench:dev")
    assert on["SPARKHUB_HOST_DATA_DIR"].endswith("/data") and on["SPARKHUB_GATEWAY_ADDR"] == BASE["ISSUER_ADDR"]
    assert I.detect_defaults_only()["BENCH"] == "0"                                  # older installs: off until asked
    # the runner image is rebuilt when bench/ changes
    monkeypatch.setattr(I, "out", lambda cmd, **kw: json.dumps({"sparkhub.bench-hash": "old"}))
    good, msg = I.chk_bench_image(I.Ctx({**BASE, "BENCH": "1"}))
    assert not good and "older than bench/" in msg
    monkeypatch.setattr(I, "out", lambda cmd, **kw: json.dumps({"sparkhub.bench-hash": I._bench_hash()}))
    assert I.chk_bench_image(I.Ctx({**BASE, "BENCH": "1"}))[0]


def test_version_and_changelog_sections():
    assert I.vtuple("v1.2.3") == (1, 2, 3) == I.vtuple("1.2.3") and I.vtuple("dev") is None
    log = "# Changelog\nintro\n\n## 1.2.0 (2026-12-01)\n- c\n\n## 1.1.0 (2026-11-10)\n- b\n\n## 1.0.0 (2026-10-31)\n- a\n"
    notes = I.changelog_between(log, "1.0.0", "v1.2.0")
    assert "## 1.2.0" in notes and "## 1.1.0" in notes and "## 1.0.0" not in notes and "intro" not in notes
    assert 'SPARKHUB_VERSION: "' + I.version() + '"' in I.rendered(BASE)["compose.yaml"]


def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def release_repo(tmp_path, at="v1.0.0"):
    """An origin with releases 1.0.0 (2 migrations), 1.1.0 (3) and 1.2.0 (4), and a clone of it at `at` whose
    database (bench_runs, a marker table) is at schema v2."""
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "hub" / "sparkhub").mkdir(parents=True)
    for v, note, n in (("1.0.0", "- first", 2), ("1.1.0", "- Fast model pin changed: reloads", 3),
                       ("1.2.0", "- grafana", 4)):
        (origin / "VERSION").write_text(v + "\n")
        old = (origin / "CHANGELOG.md").read_text() if (origin / "CHANGELOG.md").exists() else "# Changelog\n"
        (origin / "CHANGELOG.md").write_text(old.replace("# Changelog\n", f"# Changelog\n\n## {v} (d)\n{note}\n"))
        (origin / "hub" / "sparkhub" / "db.py").write_text("SCHEMA: list = [\n" + '    "",\n' * n + "]\n")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-q", "-m", v)
        _git(origin, "tag", "v" + v)
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "reset", "-q", "--hard", at)
    (clone / "data").mkdir()
    import sqlite3
    with sqlite3.connect(clone / "data" / "sparkhub.db") as c:
        c.execute("CREATE TABLE bench_runs (id INTEGER, status TEXT)")
        c.execute("CREATE TABLE marker (v TEXT)")
        c.execute("INSERT INTO marker VALUES ('before')")
        c.execute("PRAGMA user_version=2")
    return clone


def test_upgrade_moves_to_the_newest_release_and_hands_over(monkeypatch, tmp_path, capsys):
    clone = release_repo(tmp_path)
    import sqlite3
    monkeypatch.setattr(I, "ROOT", clone)
    monkeypatch.setattr(I, "VERSION_FILE", clone / "VERSION")
    monkeypatch.setattr(I, "ask", lambda q, default="", choices=(): "y")
    execs = []
    monkeypatch.setattr(I.os, "execve", lambda exe, argv, env: execs.append(argv))
    # a running benchmark blocks it
    with sqlite3.connect(clone / "data" / "sparkhub.db") as c:
        c.execute("INSERT INTO bench_runs VALUES (4, 'running')")
    with pytest.raises(I.Abort, match="benchmark #4"):
        I.upgrade(None)
    with sqlite3.connect(clone / "data" / "sparkhub.db") as c:
        c.execute("UPDATE bench_runs SET status='done'")
    I.upgrade(None)
    out = capsys.readouterr().out
    assert "1.0.0 -> v1.2.0" in out and "Fast model pin changed" in out and "- grafana" in out and "- first" not in out
    assert (clone / "VERSION").read_text().strip() == "1.2.0"                     # fast-forwarded on main
    assert execs and execs[0][-1] == "update" and execs[0][-2].endswith("ops/install/installer.py")
    assert list((clone / "data" / "backups").glob("pre-upgrade-1.0.0-to-1.2.0-*.db"))
    # already there: nothing happens; a given older release is checked out detached (with a warning)
    execs.clear()
    I.upgrade(None)
    assert not execs and "newest release" in capsys.readouterr().out
    I.upgrade("1.1.0")
    assert (clone / "VERSION").read_text().strip() == "1.1.0" and execs
    assert "does not undo database changes" in capsys.readouterr().out
    # local edits to tracked files stop it before anything changes
    (clone / "CHANGELOG.md").write_text("edited")
    with pytest.raises(I.Abort, match="tracked files were changed"):
        I.upgrade(None)


def test_code_schema_version_reads_the_hub_without_running_it():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hubdb", I.ROOT / "hub" / "sparkhub" / "db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert I.code_schema_version() == len(mod.SCHEMA) >= 8


def fake_stack(monkeypatch, calls, wait_hub=None, prepared=True):
    """update() without Docker: records docker commands, runs git for real."""
    real_run = I.run

    def run(cmd, **kw):
        if cmd[0] == "docker":
            calls.append(cmd)
            return I.subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kw)
    monkeypatch.setattr(I, "run", run)
    monkeypatch.setattr(I, "active_requests", lambda ctx: 0)
    monkeypatch.setattr(I, "do_render", lambda ctx: calls.append(["render"]))
    monkeypatch.setattr(I, "compose_images", lambda ctx: [])
    monkeypatch.setattr(I, "run_steps", lambda ctx, only=None: prepared)
    monkeypatch.setattr(I, "chk_data", lambda ctx: (True, ""))
    monkeypatch.setattr(I, "wait_hub", wait_hub or (lambda ctx: None))
    monkeypatch.setattr(I, "ask", lambda q, default="", choices=(): "y")


def _version(clone):
    import sqlite3
    with sqlite3.connect(clone / "data" / "sparkhub.db") as c:
        return c.execute("PRAGMA user_version").fetchone()[0], [r[0] for r in c.execute("SELECT v FROM marker")]


def test_update_backs_up_before_a_migration_and_refuses_a_newer_database(monkeypatch, tmp_path, capsys):
    clone = release_repo(tmp_path, at="v1.2.0")                        # code has 4 migrations, the database is at 2
    monkeypatch.setattr(I, "ROOT", clone)
    calls = []
    fake_stack(monkeypatch, calls)
    ctx = I.Ctx(dict(BASE))
    I.update(ctx)
    assert "migrates it from v2 to v4" in capsys.readouterr().out
    assert list((clone / "data" / "backups").glob("pre-update-v2-to-v4-*.db"))
    assert ["docker", "compose", "up", "-d", "--remove-orphans"] in calls
    # a database from a newer release: refused before anything changes
    import sqlite3
    with sqlite3.connect(clone / "data" / "sparkhub.db") as c:
        c.execute("PRAGMA user_version=5")
    calls.clear()
    with pytest.raises(I.Abort, match="newer than this checkout's hub knows"):
        I.update(ctx)
    assert calls == []


def _upgrade_then_fail(monkeypatch, tmp_path, *, hub_starts_then_dies):
    clone = release_repo(tmp_path)
    monkeypatch.setattr(I, "ROOT", clone)
    monkeypatch.setattr(I, "VERSION_FILE", clone / "VERSION")
    execs = []
    monkeypatch.setattr(I.os, "execve", lambda exe, argv, env: execs.append((argv, env)))
    monkeypatch.setattr(I, "ask", lambda q, default="", choices=(): "y")
    I.upgrade(None)                                                     # 1.0.0 -> 1.2.0, then hands over
    argv, env = execs.pop()
    assert argv[-1] == "update" and (clone / "VERSION").read_text().strip() == "1.2.0"
    monkeypatch.setenv("SPARKHUB_ROLLBACK", env["SPARKHUB_ROLLBACK"])
    calls = []

    def new_hub_migrates_then_dies(ctx):
        import sqlite3
        with sqlite3.connect(clone / "data" / "sparkhub.db") as c:    # the new hub got as far as v3 of 4
            c.execute("INSERT INTO marker VALUES ('after')")
            c.execute("PRAGMA user_version=3")
        raise I.Abort("the hub did not come up; see: docker compose logs hub")
    fake_stack(monkeypatch, calls, wait_hub=new_hub_migrates_then_dies, prepared=hub_starts_then_dies)
    with pytest.raises(I.Abort):
        I.update(I.Ctx(dict(BASE)))
    return clone, calls, execs


def test_failed_upgrade_goes_back_with_the_database(monkeypatch, tmp_path, capsys):
    clone, calls, execs = _upgrade_then_fail(monkeypatch, tmp_path, hub_starts_then_dies=True)
    assert "stopped: the hub did not come up" in capsys.readouterr().out
    assert ["docker", "compose", "stop", "hub"] in calls
    assert _version(clone) == (2, ["before"])                          # the pre-upgrade backup is back
    assert (clone / "VERSION").read_text().strip() == "1.0.0"          # main moved back to the old release
    argv, env = execs.pop()
    assert argv[-1] == "update" and "SPARKHUB_ROLLBACK" not in env      # the old installer restarts the stack


def test_upgrade_failing_before_the_restart_only_goes_back_in_code(monkeypatch, tmp_path):
    clone, calls, execs = _upgrade_then_fail(monkeypatch, tmp_path, hub_starts_then_dies=False)
    assert ["docker", "compose", "up", "-d", "--remove-orphans"] not in calls
    assert ["docker", "compose", "stop", "hub"] not in calls
    assert _version(clone) == (2, ["before"])                          # untouched (the old hub kept writing it)
    assert (clone / "VERSION").read_text().strip() == "1.0.0"
    assert execs.pop()[0][-1] == "render"                              # config back to the old templates, no restart


def test_update_refuses_during_a_benchmark(monkeypatch):
    monkeypatch.setattr(I, "running_benchmark", lambda: 7)
    with pytest.raises(I.Abort, match="benchmark #7 is running"):
        I.update(I.Ctx(dict(BASE), check=False, verify=False))


def test_version_matches_the_changelog():
    text = (I.ROOT / "CHANGELOG.md").read_text()
    top = next(line for line in text.splitlines() if line.startswith("## "))
    assert I.vtuple(I.version()) and top.split()[1] == I.version(), "bump VERSION and CHANGELOG.md together"
