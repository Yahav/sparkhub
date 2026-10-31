#!/usr/bin/env python3
"""SparkHub installer. Run it through ./install.sh.

  ./install.sh                 install or repair: asks the questions once (saved to sparkhub.conf), then runs every
                               step that is not done yet; a re-run on a finished install changes nothing
  ./install.sh --check         report every step without changing anything (exit 1 if something is missing)
  ./install.sh configure       ask the questions again (current answers are the defaults)
  ./install.sh render          regenerate compose.yaml, the nginx and Prometheus files from sparkhub.conf
  ./install.sh models [--verify]   download / build / prepare the enabled modes (resumable); --verify hashes them
  ./install.sh tls             obtain or renew the certificate (Let's Encrypt or the local CA)
  ./install.sh selftest [--switch]  check the running install end to end (--switch: switch modes and back, ~10 min)
  ./install.sh update          re-render, pull pinned images, rebuild the hub, restart what changed
  ./install.sh upgrade [vX.Y.Z]  move to the newest release (or the one given): release notes, database backup,
                               then that release's `update` (a model the release changed reloads)
  ./install.sh uninstall [--purge]  stop and remove the containers; keeps data/ and the weights unless --purge

Standard library only (DGX OS ships python3). Commands that need root are shown and confirmed one by one; --check
never runs them. Nothing here opens a port to the internet by itself: which addresses nginx listens on is a question.
"""
from __future__ import annotations

import argparse
import ast
import getpass
import grp
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import termios
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
CONF = ROOT / "sparkhub.conf"
VERSION_FILE = ROOT / "VERSION"
TEMPLATES = ROOT / "templates"
MODELS = json.loads((ROOT / "ops" / "models" / "models.json").read_text())
# generated file -> template (all under ROOT); listen-public.conf only with a trusted (Let's Encrypt/Tailscale) one
RENDERED = {
    "compose.yaml": "compose.yaml",
    "nginx/nginx.conf": "nginx/nginx.conf",
    "nginx/listen-sparkhub.conf": "nginx/listen-sparkhub.conf",
    "nginx/listen-public.conf": "nginx/listen-public.conf",
    "nginx/sparkhub-locations.inc": "nginx/sparkhub-locations.inc",
    "observability/prometheus.yml": "observability/prometheus.yml",
    "observability/grafana/provisioning/datasources/prometheus.yml": "observability/grafana-datasource.yml",
}
FALSY = ("", "0", "no", "false", "off")
# chat-ui (CHATUI): built from this exact commit; APP_BASE is fixed at build time
CHATUI_PIN = {"repo": "https://github.com/huggingface/chat-ui", "ref": "v0.10.0",
              "commit": "cac5329f4c7f95cffc2988546c34a12b1730969f"}
CHATUI_DEFAULT_IMAGE = "chatui-sparkhub:" + CHATUI_PIN["ref"]
# Arcane with base-path support (serves under /arcane), until upstream ships it; built when ARCANE_SOURCE is empty
ARCANE_PIN = {"repo": "https://github.com/Yahav/arcane", "ref": "base-path",
              "commit": "d8fc8b0e29cde71ceb398f45644c1238bf4ca0ab"}

# ----------------------------------------------------------------------------------------------- output helpers
TTY = sys.stdout.isatty()


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


def say(s: str = "") -> None:
    print(s, flush=True)


def head(s: str) -> None:
    say("\n" + c("1", f"== {s}"))


def ok(s: str) -> None:
    say(f"  {c('32', 'ok')}    {s}")


def todo(s: str) -> None:
    say(f"  {c('33', 'todo')}  {s}")


def bad(s: str) -> None:
    say(f"  {c('31', 'FAIL')}  {s}")


def info(s: str) -> None:
    say(f"  {c('36', '..')}    {s}")


class Abort(Exception):
    pass


def run(cmd: list[str] | str, *, check: bool = True, capture: bool = False, cwd: Path | None = None,
        env: dict | None = None, timeout: float | None = None, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        info("$ " + (cmd if isinstance(cmd, str) else shlex.join(cmd)))
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=check, text=True, cwd=cwd or ROOT,
                          capture_output=capture, env={**os.environ, **(env or {})}, timeout=timeout)


def out(cmd: list[str], cwd: Path | None = None, timeout: float = 60) -> str:
    """stdout of a command, '' when it fails (for checks)."""
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True, cwd=cwd or ROOT, timeout=timeout
                              ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def flush_typeahead() -> None:
    """Drop keys pressed during a long step: an Enter while weights download would answer the next question."""
    if sys.stdin.isatty():
        try:
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except termios.error:
            pass


def ask(q: str, default: str = "", choices: tuple[str, ...] = ()) -> str:
    hint = f" [{default}]" if default else ""
    if choices:
        hint = f" ({'/'.join(choices)}){hint}"
    while True:
        flush_typeahead()
        try:
            a = input(f"  {q}{hint}: ").strip()
        except EOFError:
            raise Abort("no answer (stdin closed); run interactively or write sparkhub.conf first")
        a = a or default
        if not choices or a in choices:
            return a
        say(f"  please answer one of: {', '.join(choices)}")


def yes(q: str, default: bool = False) -> bool:
    return ask(q, "y" if default else "n", ("y", "n")) == "y"


def sudo(cmd: list[str] | str, why: str, ctx: "Ctx") -> bool:
    """Run one root command after showing it and asking. False if declined."""
    shown = cmd if isinstance(cmd, str) else shlex.join(cmd)
    say(f"\n  {c('35', 'root')}  {why}\n        sudo {shown}")
    if ctx.check:
        return False
    if not yes("Run it?", False):
        todo("skipped (declined)")
        return False
    run(["sudo", "sh", "-c", shown] if isinstance(cmd, str) else ["sudo", *cmd], quiet=True)
    return True


# ----------------------------------------------------------------------------------------------- configuration
QUESTIONS = [
    # key, comment written to sparkhub.conf
    ("INSTANCE", "names the compose project, containers and hub image; another name = a separate install"),
    ("RUN_UID", "the user the stack runs as (owns data/)"),
    ("RUN_GID", ""),
    ("DOCKER_GID", "group id of /var/run/docker.sock (the hub starts the model containers)"),
    ("TZ", ""),
    ("HF_CACHE", "Hugging Face cache (the Quality weights live here)"),
    ("MODELS_DIR", "the Fast weights live in MODELS_DIR/qwen36"),
    ("MODES", "fast, quality or both"),
    ("ACCESS", "who reaches the hub: local (this machine and the LAN), tailscale (the tailnet; LAN devices optionally "
               "for the dashboard and API) or public (the internet, under your own domain)"),
    ("PUBLIC_HOST", "the name clients use: NAME.local (local), the tailnet name (tailscale) or your domain (public); "
                    "chat, Arcane and Grafana work under this name only"),
    ("EXTRA_NAMES", "more names nginx answers with the local-CA certificate (tailnet name, short host name)"),
    ("LISTEN_ADDRS", "addresses nginx listens on for HTTPS; 127.0.0.1 is always added. A LAN address the router "
                     "forwards 443 to makes the dashboard public (password + TOTP)"),
    ("HTTPS_PORT", ""),
    ("HTTP_PORT", "ACME challenge + redirect to https; 0 = off"),
    ("TLS", "certificate for PUBLIC_HOST: letsencrypt (public), tailscale (tailscale cert for the tailnet name) or "
            "local-ca (clients trust data/tls/ca.crt). The other names always use the local CA"),
    ("TLS_CHALLENGE", "letsencrypt only: http (port 80 must reach this machine) or dns (manual TXT record)"),
    ("LETSENCRYPT_ISSUED", "set by the installer once the certificate exists"),
    ("TAILSCALE_CERT_ISSUED", "set by the installer once the Tailscale certificate exists"),
    ("HUB_PORT", "local ports (127.0.0.1); change them for a second install on the same machine"),
    ("PROMETHEUS_PORT", ""),
    ("NODE_EXPORTER_PORT", ""),
    ("GRAFANA_PORT", ""),
    ("FAST_PORT", ""),
    ("QUALITY_PORT", ""),
    ("AUTOSTART", "load the last mode when the hub starts (0 for a test install that must not load models)"),
    ("ARCANE", "Docker UI at /arcane, signed in through the hub"),
    ("ARCANE_IMAGE", ""),
    ("ARCANE_SOURCE", "empty = build the pinned fork commit (" + ARCANE_PIN["repo"] + " @ " + ARCANE_PIN["commit"][:12]
                      + "); or a local checkout or git URL to build instead"),
    ("ARCANE_REF", "git ref to build when ARCANE_SOURCE is a URL"),
    ("ARCANE_PORT", ""),
    ("ARCANE_SUBNET", "Arcane's own bridge network"),
    ("ISSUER_ADDR", "address Arcane and chat-ui use to reach nginx (OIDC back channel, /v1): a LISTEN_ADDRS entry, "
                    "not 127.0.0.1"),
    ("CHATUI", "web chat (Hugging Face chat-ui) at /chat for the hub's users, signed in through the hub"),
    ("CHATUI_IMAGE", "built here from chat-ui " + CHATUI_PIN["ref"]),
    ("CHATUI_PORT", ""),
    ("CHATUI_SUBNET", "chat-ui's own bridge network"),
    ("EMBEDDINGS", "embedding service at /v1/embeddings and /v1/rerank (Qwen3-Embedding-0.6B + Qwen3-Reranker-0.6B "
                   "on llama.cpp, 1.8 GiB of weights); run mode on Settings → Models"),
    ("EMBED_PORT", ""),
    ("RERANK_PORT", ""),
    ("BENCH", "the Benchmarks page (compare models: speed, Terminal-Bench, knowledge tests): builds the runner image "
              "and fetches the test data into data/bench (~3 GB)"),
    ("NVIDIA_DASHBOARD", "proxy the DGX Dashboard (127.0.0.1:NVIDIA_DASHBOARD_PORT) at /nvidia"),
    ("NVIDIA_DASHBOARD_PORT", ""),
    ("FLASH_LICENSE", "license id and date the Quality weights' license was accepted (e.g. qwen-community-1.0 2026-10-28)"),
    ("SSH_GEO", "host: allow SSH only from one country + LAN + Tailscale (ops/ssh-geo)"),
    ("SSH_GEO_COUNTRY", "two-letter registry country code"),
    ("LAN_CIDR", "the LAN, always allowed by SSH_GEO and PORT_GUARD"),
    ("PORT_GUARD", "host: drop these ports from the internet, keep them for LAN/Tailscale (ops/port-guard)"),
    ("PORT_GUARD_PORTS", ""),
    ("HEADLESS", "host: boot without the desktop (multi-user.target); frees ~2 GB"),
]


ACCESS_MODES = ("local", "tailscale", "public")


def access(conf: dict[str, str]) -> str:
    """The access mode; a sparkhub.conf from before ACCESS gets it from its certificate and name."""
    if conf.get("ACCESS") in ACCESS_MODES:
        return conf["ACCESS"]
    if conf.get("TLS") == "letsencrypt":
        return "public"
    return "tailscale" if conf.get("PUBLIC_HOST", "").endswith(".ts.net") else "local"


def tailscale_status() -> dict:
    try:
        return json.loads(out(["tailscale", "status", "--json"]) or "{}")
    except ValueError:
        return {}


@dataclass
class Net:
    lan: str          # this machine's LAN address (the route to the internet)
    ts_ip: str        # Tailscale address, "" when Tailscale is not up
    ts_name: str      # tailnet name (MagicDNS)
    short: str        # host name

    @property
    def mdns(self) -> str:
        return f"{self.short}.local"


def net_facts() -> Net:
    st = tailscale_status()
    up = st.get("BackendState") == "Running"
    ts_ip = next((a for a in (st.get("Self", {}).get("TailscaleIPs") or []) if "." in a), "") if up else ""
    m = re.search(r"\bsrc (\S+)", out(["ip", "-4", "route", "get", "1.1.1.1"]))
    return Net(lan=m.group(1) if m else "", ts_ip=ts_ip,
               ts_name=st.get("Self", {}).get("DNSName", "").rstrip(".") if up else "",
               short=socket.gethostname().split(".")[0])


def detect() -> dict[str, str]:
    home = Path.home()
    n = net_facts()
    lan, short = n.lan, n.short
    try:
        dgid = str(os.stat("/var/run/docker.sock").st_gid)
    except OSError:
        dgid = str(grp.getgrnam("docker").gr_gid) if "docker" in [g.gr_name for g in grp.getgrall()] else ""
    tz = Path("/etc/timezone").read_text().strip() if Path("/etc/timezone").exists() else "UTC"
    lan_cidr = ""
    if lan:
        for line in out(["ip", "-4", "-o", "addr", "show"]).splitlines():
            m = re.search(rf"inet ({re.escape(lan)}/\d+)", line)
            if m:
                lan_cidr = str(ipaddress.ip_interface(m.group(1)).network)
    nv = "1" if _http_ok("http://127.0.0.1:11000/") else "0"
    return {
        "INSTANCE": "sparkhub", "RUN_UID": str(os.getuid()), "RUN_GID": str(os.getgid()), "DOCKER_GID": dgid,
        "TZ": tz, "HF_CACHE": str(home / ".cache" / "huggingface"), "MODELS_DIR": str(home / "models"),
        # the network answers are set by configure() from the access mode; these are its starting points
        "MODES": "fast quality", "ACCESS": "tailscale" if n.ts_ip else "local",
        "PUBLIC_HOST": n.ts_name or n.mdns, "EXTRA_NAMES": "", "LISTEN_ADDRS": n.ts_ip or lan or "127.0.0.1",
        "HTTPS_PORT": "443", "HTTP_PORT": "80", "TLS": "local-ca", "TLS_CHALLENGE": "http", "LETSENCRYPT_ISSUED": "0",
        "TAILSCALE_CERT_ISSUED": "0",
        "HUB_PORT": "8090", "PROMETHEUS_PORT": "9090", "NODE_EXPORTER_PORT": "9100", "GRAFANA_PORT": "3000",
        "FAST_PORT": "8001", "QUALITY_PORT": "18300", "AUTOSTART": "1",
        "ARCANE": "1", "ARCANE_IMAGE": "arcane-sparkhub:runtime-basepath", "ARCANE_SOURCE": "", "ARCANE_REF": "",
        "ARCANE_PORT": "3552", "ARCANE_SUBNET": "172.31.250.0/24", "ISSUER_ADDR": n.ts_ip or lan,
        "CHATUI": "1", "CHATUI_IMAGE": CHATUI_DEFAULT_IMAGE, "CHATUI_PORT": "3090", "CHATUI_SUBNET": "172.31.251.0/24",
        "EMBEDDINGS": "1", "EMBED_PORT": "18310", "RERANK_PORT": "18311", "BENCH": "1",
        "NVIDIA_DASHBOARD": nv, "NVIDIA_DASHBOARD_PORT": "11000", "FLASH_LICENSE": "",
        "SSH_GEO": "0", "SSH_GEO_COUNTRY": "", "LAN_CIDR": lan_cidr, "PORT_GUARD": "0", "PORT_GUARD_PORTS": "8000",
        "HEADLESS": "0",
    }


def _http_ok(url: str, timeout: float = 2) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True        # it answered
    except Exception:
        return False


def load_conf(path: Path = CONF) -> dict[str, str]:
    conf: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        conf[k.strip()] = v
    return conf


def save_conf(conf: dict[str, str], path: Path = CONF) -> None:
    lines = ["# SparkHub install settings, written by ./install.sh (re-run it after editing). Not in git.", ""]
    known = {k for k, _ in QUESTIONS}
    for k, comment in QUESTIONS:
        if comment:
            lines.append(f"# {comment}")
        lines.append(f'{k}="{conf.get(k, "")}"')
    for k in sorted(set(conf) - known):
        lines.append(f'{k}="{conf[k]}"')
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def configure(conf: dict[str, str]) -> dict[str, str]:
    d = {**detect(), **conf}
    head("Questions (Enter keeps the value in brackets)")
    say("  Answers go to sparkhub.conf; later runs, repairs and updates use them without asking.")
    d["MODES"] = ask("Which modes? fast = qwen36 (22 GiB), quality = Flash (140 GiB incl. prep), or both",
                     d["MODES"], ("fast quality", "fast", "quality"))
    d["HF_CACHE"] = ask("Hugging Face cache directory", d["HF_CACHE"])
    d["MODELS_DIR"] = ask("Directory for the Fast weights", d["MODELS_DIR"])
    configure_access(d, conf)
    d["ARCANE"] = "1" if yes(f"Docker UI (Arcane) at /arcane? (built from {_arcane_pin_label()}, ~10 min)",
                             d["ARCANE"] not in FALSY) else "0"
    if d["ARCANE"] == "1" and re.match(r"^(https?|git|ssh)://|^git@", d.get("ARCANE_SOURCE", "")):
        d["ARCANE_REF"] = ask("Branch, tag or commit to build (empty = the default branch)", d.get("ARCANE_REF", ""))
    d["CHATUI"] = "1" if yes("Web chat (chat-ui) at /chat for the hub's users?", d["CHATUI"] not in FALSY) else "0"
    d["EMBEDDINGS"] = "1" if yes("Embedding service (/v1/embeddings, /v1/rerank) for clients that search their own "
                                 "documents?", d["EMBEDDINGS"] not in FALSY) else "0"
    d["BENCH"] = "1" if yes("Benchmarks page, to compare models (speed, Terminal-Bench, knowledge tests; ~3 GB "
                            "of test data, the runner image builds in ~5 min)?", d.get("BENCH", "1") not in FALSY) \
        else "0"
    d["NVIDIA_DASHBOARD"] = "1" if yes("Proxy the NVIDIA DGX Dashboard at /nvidia?",
                                       d["NVIDIA_DASHBOARD"] not in FALSY) else "0"
    head("Optional host pieces (each needs root; every command is shown before it runs)")
    d["SSH_GEO"] = "1" if yes("Allow SSH only from one country, the LAN and Tailscale?", d["SSH_GEO"] not in FALSY) else "0"
    if d["SSH_GEO"] == "1":
        d["SSH_GEO_COUNTRY"] = ask("Country code (RIPE/registry, e.g. IL, DE)", d["SSH_GEO_COUNTRY"]).upper()
        d["LAN_CIDR"] = ask("Your LAN", d["LAN_CIDR"])
    d["PORT_GUARD"] = "1" if yes("Keep some forwarded ports closed to the internet (port guard)?",
                                 d["PORT_GUARD"] not in FALSY) else "0"
    if d["PORT_GUARD"] == "1":
        d["PORT_GUARD_PORTS"] = ask("Ports", d["PORT_GUARD_PORTS"])
        d["LAN_CIDR"] = ask("Your LAN", d["LAN_CIDR"])
    d["HEADLESS"] = "1" if yes("Boot without the desktop (headless)?", d["HEADLESS"] not in FALSY) else "0"
    return d


def configure_access(d: dict[str, str], conf: dict[str, str]) -> None:
    """Who reaches the hub, and from that: the name, certificate, listen addresses and the other names."""
    head("Access")
    say("  local      this machine and the LAN. Devices open https://NAME.local and trust the SparkHub certificate once.")
    say("  tailscale  devices on your tailnet (Tailscale app). With a Tailscale certificate they install nothing.")
    say("             LAN devices without Tailscale can still get the dashboard and the API.")
    say("  public     the internet, under your own domain (Let's Encrypt). Your router forwards 443 and 80 here.")
    prev = access(conf) if conf else ""
    d["ACCESS"] = ask("Who should reach the hub?", prev or d.get("ACCESS") or "local", ACCESS_MODES)
    mode, same = d["ACCESS"], d["ACCESS"] == prev

    def kept(key: str, new: str) -> str:
        """Same mode as before: the saved answer stays; otherwise the value that fits the mode."""
        return conf[key] if same and conf.get(key) else new

    if mode == "tailscale":
        ensure_tailscale(Ctx(d))
    n = net_facts()
    if mode == "local":
        d["TLS"] = "local-ca"
        d["PUBLIC_HOST"] = ask("Name LAN devices use (NAME.local needs no DNS setup)", kept("PUBLIC_HOST", n.mdns))
        listen, issuer = [n.lan], n.lan
        names = [n.short, n.mdns]
    elif mode == "tailscale":
        if not n.ts_name:
            raise Abort("Tailscale is not up (tailscale status); start it and run ./install.sh configure again")
        d["PUBLIC_HOST"] = n.ts_name
        say(f"  Name: {n.ts_name}")
        lan_too = yes(f"Also serve LAN devices without Tailscale (dashboard and API at https://{n.mdns}; "
                      "chat and Arcane need Tailscale)?", (n.lan in conf.get("LISTEN_ADDRS", "").split()) if same else True)
        say("  A Tailscale certificate is publicly trusted, so devices install nothing. It is Let's Encrypt's, which")
        say(f"  publishes every certificate: {n.ts_name} becomes visible in those public logs.")
        d["TLS"] = "tailscale" if yes("Use a Tailscale certificate?", d["TLS"] != "local-ca" if same else True) \
            else "local-ca"
        listen, issuer = [n.ts_ip] + ([n.lan] if lan_too else []), n.ts_ip
        names = [n.short] + ([n.mdns] if lan_too else [])
    else:
        d["TLS"] = "letsencrypt"
        d["PUBLIC_HOST"] = ask("Your domain name for the hub (its DNS record points at your router's public address)",
                               kept("PUBLIC_HOST", ""))
        while "." not in d["PUBLIC_HOST"]:
            d["PUBLIC_HOST"] = ask("A domain name like llm.example.com, please", "")
        d["TLS_CHALLENGE"] = ask("Let's Encrypt check: http (port 80 reaches this machine; renews by itself) or dns "
                                 "(you add a TXT record by hand; no automatic renewal)", d["TLS_CHALLENGE"],
                                 ("http", "dns"))
        listen, issuer = [n.lan] + ([n.ts_ip] if n.ts_ip else []), n.lan
        names = [n.ts_name, n.short, n.mdns]
        say(f"\n  On your router, forward TCP 443{' and 80' if d['TLS_CHALLENGE'] == 'http' else ''} to {n.lan}.")
        say("  The dashboard then answers from the internet (password + authenticator app; the API needs a key).")
    listen = [a for a in listen if a] or ["127.0.0.1"]
    say("\n  nginx listens only on these addresses (127.0.0.1 is always added).")
    d["LISTEN_ADDRS"] = ask("HTTPS listen addresses (space-separated)", kept("LISTEN_ADDRS", " ".join(listen)))
    addrs = d["LISTEN_ADDRS"].split()
    d["EXTRA_NAMES"] = kept("EXTRA_NAMES", " ".join(x for x in dict.fromkeys(names) if x and x != d["PUBLIC_HOST"]))
    issuer = kept("ISSUER_ADDR", issuer)
    d["ISSUER_ADDR"] = issuer if issuer in addrs else next((a for a in addrs if a != "127.0.0.1"), "127.0.0.1")
    manual_steps(d)


def lan_addr(conf: dict[str, str]) -> str:
    """The LAN listen address (not Tailscale, not loopback), "" if nginx listens on none."""
    for a in conf["LISTEN_ADDRS"].split():
        ip = ipaddress.ip_address(a)
        if not ip.is_loopback and ip not in ipaddress.ip_network("100.64.0.0/10"):
            return a
    return ""


def manual_steps(d: dict[str, str]) -> None:
    """What the installer cannot do from this machine: shown before anything is installed."""
    mode, host, lan = access(d), d["PUBLIC_HOST"], lan_addr(d)
    steps: list[str] = []
    if mode == "local":
        steps.append("Each device trusts the SparkHub certificate once (where to get it and how: at the end).")
        if host.endswith(".local"):
            steps.append(f"Only if a device cannot open {host}: a DNS entry on your router, "
                         f"{host.removesuffix('.local')} → {lan}.")
        else:
            steps.append(f"A DNS entry on your router: {host} → {lan}.")
    elif mode == "tailscale":
        if d["TLS"] == "tailscale":
            steps.append("Turn on HTTPS Certificates once: https://login.tailscale.com/admin/dns (the installer "
                         "waits for it).")
        steps.append("Each user installs Tailscale (https://tailscale.com/download) and joins your tailnet "
                     "(admin console → Users → Invite).")
        if d["TLS"] != "tailscale":
            steps.append("Each device trusts the SparkHub certificate once (where to get it and how: at the end).")
        if lan:
            steps.append(f"Optional, so LAN devices without Tailscale get everything (chat and Arcane too): a DNS "
                         f"entry on your router, {host} → {lan}. Without it they get the dashboard and the API.")
    else:
        steps.append(f"A DNS A record at your DNS provider: {host} → your router's public address.")
        ports = "443 and 80" if d["TLS_CHALLENGE"] == "http" else "443"
        steps.append(f"On your router, forward TCP {ports} to {lan or d['ISSUER_ADDR']}.")
        if d["TLS_CHALLENGE"] == "dns":
            steps.append("certbot shows a TXT record to add at your DNS provider (and again at each renewal).")
        steps.append(f"Only if LAN devices cannot open {host} (router without hairpin NAT): a DNS entry on your "
                     f"router, {host} → {lan or d['ISSUER_ADDR']}.")
    head("You will need to do")
    for i, st in enumerate(steps, 1):
        say(f"  {i}. {st}")
    say("  The installer checks what it can from here at the end (self-test) and repeats what is still missing.")


def ensure_tailscale(ctx: "Ctx") -> None:
    """Tailscale installed, running and logged in (asks before each root command)."""
    if not shutil.which("tailscale"):
        if not sudo("curl -fsSL https://tailscale.com/install.sh | sh", "install Tailscale (its official script)", ctx):
            raise Abort("Tailscale is needed for access mode tailscale")
    if tailscale_status().get("BackendState") != "Running":
        say("  Tailscale prints a login link; open it and sign in to your tailnet.")
        if not sudo(["tailscale", "up"], "connect this machine to your tailnet", ctx):
            raise Abort("Tailscale is not connected")


def derive(conf: dict[str, str]) -> dict[str, str]:
    """Template values: the answers plus what follows from them."""
    v = dict(conf)
    v["ROOT"] = str(ROOT)
    addrs = conf["LISTEN_ADDRS"].split()
    if "127.0.0.1" not in addrs:
        addrs.append("127.0.0.1")
    port = conf["HTTPS_PORT"]
    v["PUBLIC_URL"] = f"https://{conf['PUBLIC_HOST']}" + ("" if port == "443" else f":{port}")
    v["HTTPS_PORT_SUFFIX"] = "" if port == "443" else f":{port}"
    names = [conf["PUBLIC_HOST"], *[n for n in conf["EXTRA_NAMES"].split() if n != conf["PUBLIC_HOST"]]]
    v["SERVER_NAMES"] = " ".join(names)
    v["TLS_NAMES"] = ",".join(n for n in names if "." in n)
    v["LISTEN"] = "\n".join(f"    listen {a}:{port} ssl;" for a in addrs)
    v["LISTEN_DEFAULT"] = "\n".join(f"    listen {a}:{port} ssl default_server;" for a in addrs)
    hp = conf["HTTP_PORT"]
    v["HTTP"] = "" if hp in FALSY else "1"
    acme_http = conf["TLS"] == "letsencrypt" and conf["TLS_CHALLENGE"] == "http"
    # Let's Encrypt connects to port 80 from the internet; otherwise :80 stays on the listen addresses
    v["HTTP_LISTEN"] = f"    listen {hp};" if acme_http else "\n".join(f"    listen {a}:{hp};" for a in addrs)
    v["HTTP_LISTEN_DEFAULT"] = (f"    listen {hp} default_server;" if acme_http else
                                "\n".join(f"    listen {a}:{hp} default_server;" for a in addrs))
    v["LETSENCRYPT"] = "1" if conf["TLS"] == "letsencrypt" and conf["LETSENCRYPT_ISSUED"] not in FALSY else ""
    v["TAILSCALE_CERT"] = "1" if conf["TLS"] == "tailscale" and conf.get("TAILSCALE_CERT_ISSUED") not in FALSY else ""
    # TRUSTED: PUBLIC_HOST has a publicly trusted certificate (its own server block, listen-public.conf)
    v["TRUSTED"] = "1" if v["LETSENCRYPT"] or v["TAILSCALE_CERT"] else ""
    v["PUBLIC_CERT_TRUSTED"] = "1" if v["TRUSTED"] else "0"
    if v["LETSENCRYPT"]:
        v["PUBLIC_CERT"] = f"/etc/letsencrypt/live/{conf['PUBLIC_HOST']}/fullchain.pem"
        v["PUBLIC_KEY"] = f"/etc/letsencrypt/live/{conf['PUBLIC_HOST']}/privkey.pem"
        v["PUBLIC_CERT_FROM"] = "its Let's Encrypt certificate from the host's /etc/letsencrypt (read-only mount)"
    else:
        v["PUBLIC_CERT"], v["PUBLIC_KEY"] = "/etc/nginx/tls/tailscale.crt", "/etc/nginx/tls/tailscale.key"
        v["PUBLIC_CERT_FROM"] = "its Tailscale certificate (data/tls, renewed by sparkhub-tailscale-cert.timer)"
    v["ARCANE_LOCAL_CA"] = v["LOCAL_CA"] = "1" if not v["TRUSTED"] else ""
    # ca.crt over plain http on the LAN/tailnet, so a new device can fetch it before it trusts anything; not on a
    # public install, whose port 80 faces the internet
    v["CA_HTTP"] = "1" if access(conf) != "public" else ""
    net = ipaddress.ip_network(conf["ARCANE_SUBNET"])
    v["ARCANE_GATEWAY"] = str(next(net.hosts()))
    ipaddress.ip_network(conf["CHATUI_SUBNET"])        # fails early on a typo
    v["MODES"] = " ".join(m for m in ("fast", "quality") if m in conf["MODES"].split())
    v["AUTOSTART"] = "1" if conf["AUTOSTART"] not in FALSY else "0"
    for k in ("ARCANE", "NVIDIA_DASHBOARD", "CHATUI"):
        v[k] = "" if conf[k] in FALSY else "1"
    v["BENCH"] = "1" if conf.get("BENCH") not in FALSY else ""
    v["VERSION"] = version()
    v["NETWORKS"] = "1" if v["ARCANE"] or v["CHATUI"] else ""
    v["EMBEDDINGS"] = "1" if conf.get("EMBEDDINGS", "") not in FALSY else ""
    return v


def render_text(tmpl: str, v: dict[str, str]) -> str:
    """{{NAME}} is replaced; lines between {{#NAME}} and {{/NAME}} stay only if NAME is set (a tiny mustache)."""
    lines, keep = [], [True]
    for line in tmpl.splitlines(keepends=True):
        m = re.fullmatch(r"\{\{([#/])([A-Z_]+)\}\}\n?", line)
        if m:
            if m.group(1) == "#":
                keep.append(keep[-1] and v.get(m.group(2), "") not in FALSY)
            else:
                keep.pop()
            continue
        if keep[-1]:
            lines.append(line)

    def sub(mt: re.Match) -> str:
        if mt.group(1) not in v:
            raise Abort(f"template uses {{{{{mt.group(1)}}}}}, which sparkhub.conf does not define")
        return v[mt.group(1)]
    return re.sub(r"\{\{([A-Z_]+)\}\}", sub, "".join(lines))


def rendered(conf: dict[str, str]) -> dict[str, str | None]:
    """generated path -> content (None = must not exist)."""
    v = derive(conf)
    res: dict[str, str | None] = {}
    for dst, src in RENDERED.items():
        if dst == "nginx/listen-public.conf" and not v["TRUSTED"]:
            res[dst] = None
            continue
        res[dst] = render_text((TEMPLATES / src).read_text(), v)
    return res


# ----------------------------------------------------------------------------------------------- context
@dataclass
class Ctx:
    conf: dict[str, str]
    check: bool = False
    verify: bool = False

    @property
    def v(self) -> dict[str, str]:
        return derive(self.conf)

    def modes(self) -> list[str]:
        """The modes whose weights and images the installer sets up."""
        return [m for m in ("fast", "quality") if m in self.conf["MODES"].split()]

    def embeddings(self) -> bool:
        return self.on("EMBEDDINGS")

    def on(self, key: str) -> bool:
        return self.conf.get(key, "") not in FALSY

    def on_access(self, mode: str) -> bool:
        return access(self.conf) == mode

    def name(self, svc: str) -> str:
        return f"{self.conf['INSTANCE']}-{svc}"

    def compose(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return run(["docker", "compose", *args], **kw)

    def path(self, key: str, sub: str = "") -> Path:
        return Path(MODELS_TEMPLATE(self, key)) / sub if sub else Path(MODELS_TEMPLATE(self, key))


def MODELS_TEMPLATE(ctx: Ctx, s: str) -> str:
    return s.replace("${HF_CACHE}", ctx.conf["HF_CACHE"]).replace("${MODELS_DIR}", ctx.conf["MODELS_DIR"])


# ----------------------------------------------------------------------------------------------- steps
@dataclass
class Step:
    name: str
    check: Callable[[Ctx], tuple[bool, str]]
    apply: Callable[[Ctx], None] | None
    when: Callable[[Ctx], bool] = lambda ctx: True


def image_present(ref: str) -> bool:
    return bool(out(["docker", "image", "inspect", "--format", "{{.Id}}", ref]))


# --- preflight
def chk_preflight(ctx: Ctx) -> tuple[bool, str]:
    problems = []
    gpu = out(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if "GB10" not in gpu:
        problems.append(f"GPU is {gpu or 'not found'} (SparkHub's recipes are for the DGX Spark's GB10)")
    if not out(["docker", "compose", "version", "--short"]):
        problems.append("docker compose v2 missing (apt: docker-compose-v2 with Ubuntu's docker.io, "
                        "docker-compose-plugin with Docker's docker-ce)")
    # the legacy builder creates WORKDIR as root, which breaks chat-ui's Dockerfile (it writes there as a user)
    if not out(["docker", "buildx", "version"]):
        problems.append("docker buildx (BuildKit) missing (apt: docker-buildx with Ubuntu's docker.io, "
                        "docker-buildx-plugin with Docker's docker-ce)")
    if not os.access("/var/run/docker.sock", os.W_OK):
        problems.append("this user cannot use Docker (add it to the docker group, log in again)")
    elif "nvidia" not in out(["docker", "info", "--format", "{{json .Runtimes}}"]):
        problems.append("Docker has no nvidia runtime (NVIDIA Container Toolkit)")
    if os.getuid() == 0:
        problems.append("run as your normal user, not root (sudo is asked for where needed)")
    need = sum(MODELS[m]["size_gib"] for m in ctx.modes() if model_problems(ctx, m))
    need += MODELS["embed"]["size_gib"] if ctx.embeddings() and embed_problems(ctx) else 0
    if need:
        base = Path(ctx.conf["HF_CACHE"])
        while not base.exists():
            base = base.parent
        free = shutil.disk_usage(base).free / 2**30
        if free < need + 30:
            problems.append(f"{free:.0f} GiB free under {base}, the missing weights need ~{need} GiB (+30 spare)")
    return (not problems), "; ".join(problems) or f"{gpu}, Docker with the NVIDIA runtime"


def chk_network(ctx: Ctx) -> tuple[bool, str]:
    urls = ["https://huggingface.co", "https://registry-1.docker.io/v2/", "https://github.com"]
    down = [u for u in urls if not _http_ok(u, 10)]
    return (not down), ("cannot reach " + ", ".join(down)) if down else "Hugging Face, Docker Hub, GitHub reachable"


# --- licenses and Hugging Face token (asked up front, so the downloads need nobody at the keyboard)
HF_TOKEN_FILE = ROOT / "data" / "hf-token"      # empty file = asked, skipped (anonymous downloads)


def hf_token() -> str:
    return os.environ.get("HF_TOKEN") or (HF_TOKEN_FILE.read_text().strip() if HF_TOKEN_FILE.exists() else "")


def _hf_whoami(token: str) -> str | None:
    """The account name for a token; '' when Hugging Face can't be asked; None when it refuses the token."""
    req = urllib.request.Request("https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {token}"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=15)).get("name", "")
    except urllib.error.HTTPError as e:
        return None if e.code in (401, 403) else ""
    except Exception:
        return ""


def chk_licenses(ctx: Ctx) -> tuple[bool, str]:
    missing = [MODELS[m]["license"]["name"] + f" ({m})" for m in ctx.modes() if not license_ok(ctx, m)]
    asked = bool(os.environ.get("HF_TOKEN")) or HF_TOKEN_FILE.exists()
    tok = ("Hugging Face token set" if hf_token() else "no Hugging Face token (anonymous downloads)") if asked \
        else "Hugging Face token not asked yet"
    lic = ("not accepted: " + ", ".join(missing)) if missing else \
        ("Quality license accepted " + ctx.conf["FLASH_LICENSE"].split(" ", 1)[-1] if ctx.conf.get("FLASH_LICENSE")
         and "quality" in ctx.modes() else "no license to accept")
    return (not missing and asked), f"{lic}; {tok}"


def do_licenses(ctx: Ctx) -> None:
    for m in ctx.modes():
        if license_ok(ctx, m):
            continue
        spec = MODELS[m]
        say(f"  The {m} weights are under the {spec['license']['name']}. Please read it:\n    {spec['license']['url']}\n"
            "  It is an agreement between you and the licensor; Hugging Face needs no account for these files.")
        if yes("Have you read it, and do you accept it?", True):
            ctx.conf["FLASH_LICENSE"] = f"{spec['license']['id']} {time.strftime('%Y-%m-%d')}"
            save_conf(ctx.conf)
        else:
            todo(f"{m} will be skipped (answer again on the next run, or drop it with ./install.sh configure)")
    if os.environ.get("HF_TOKEN") or HF_TOKEN_FILE.exists():
        return
    say("  A Hugging Face token is optional. Without one, downloads are anonymous: they work, at lower rate limits\n"
        "  (and the download tool warns about it). A read-only token is enough:\n"
        "    https://huggingface.co/settings/tokens\n"
        "  It is kept in data/hf-token (mode 600) and used only for the installer's downloads.")
    token = ""
    while sys.stdin.isatty():
        flush_typeahead()
        token = getpass.getpass("  Hugging Face token (Enter to skip, input hidden): ").strip()
        if not token:
            break
        who = _hf_whoami(token)
        if who is None:
            say("  Hugging Face refused that token; try again or press Enter to skip.")
            continue
        ok(f"token for {who}" if who else "token saved (Hugging Face could not be asked to check it)")
        break
    HF_TOKEN_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    HF_TOKEN_FILE.touch(mode=0o600)
    os.chmod(HF_TOKEN_FILE, 0o600)
    HF_TOKEN_FILE.write_text(token + "\n" if token else "")


# --- config files
def chk_render(ctx: Ctx) -> tuple[bool, str]:
    diff = []
    for p, content in rendered(ctx.conf).items():
        f = ROOT / p
        if content is None:
            if f.exists():
                diff.append(f"{p} (should not exist)")
        elif not f.exists() or f.read_text() != content:
            diff.append(p)
    alerts = ROOT / "observability/grafana/provisioning/alerting/contact-points.yaml"
    if f"127.0.0.1:{ctx.conf['HUB_PORT']}/internal/grafana-alert" not in (alerts.read_text() if alerts.exists() else ""):
        diff.append(str(alerts.relative_to(ROOT)))
    return (not diff), ("differs from sparkhub.conf: " + ", ".join(diff)) if diff else "generated files match sparkhub.conf"


def _listen_lines() -> list[str]:
    return sorted(ln.strip() for f in (ROOT / "nginx").glob("*.conf")
                  for ln in f.read_text().splitlines() if ln.strip().startswith("listen "))


def do_render(ctx: Ctx) -> None:
    changed = []
    listen_before = _listen_lines() if (ROOT / "nginx").is_dir() else []
    for p, content in rendered(ctx.conf).items():
        f = ROOT / p
        if content is None:
            if f.exists():
                f.unlink()
                changed.append(p)
                info(f"removed {p}")
            continue
        if not f.exists() or f.read_text() != content:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
            changed.append(p)
            info(f"wrote {p}")
    alerting = ROOT / "observability/grafana/provisioning/alerting"
    before = {q.name: q.read_text() for q in alerting.glob("*.yaml")}
    run([sys.executable, "observability/grafana/build_alerts.py"], env={"SPARKHUB_HUB_PORT": ctx.conf["HUB_PORT"]},
        capture=True)
    if {q.name: q.read_text() for q in alerting.glob("*.yaml")} != before:
        changed.append("observability/grafana/provisioning/alerting")
    if listen_before and _listen_lines() != listen_before:
        changed.append(NGINX_LISTEN)
    if changed and _containers(ctx):
        if not chk_data(ctx)[0]:             # files a newly enabled piece needs (e.g. data/chatui.env) before compose
            do_data(ctx)
        apply_changes(ctx, changed)


NGINX_LISTEN = "nginx: listen addresses"      # a pseudo-path in `changed`: restart nginx, a reload is not enough


def apply_changes(ctx: Ctx, changed: list[str]) -> None:
    """Bring a running stack in line with regenerated files, restarting only what they feed."""
    if "compose.yaml" in changed:
        n = active_requests(ctx)
        if n and not yes(f"compose.yaml changed and {n} request(s) are running; recreating the hub cuts them. "
                         f"Continue?", False):
            todo("not applied yet: run ./install.sh again when the model is idle")
            return
        ctx.compose("up", "-d", "--remove-orphans")
        wait_hub(ctx)
    if NGINX_LISTEN in changed:
        # A reload can't move a port from 0.0.0.0 to one address (the new bind() collides with the old socket);
        # nginx then logs the error and keeps the whole old config, certificate included. A restart applies it.
        run(["docker", "exec", ctx.name("nginx"), "nginx", "-t"], capture=True)
        n = active_requests(ctx)
        if n and not yes(f"nginx listens on new addresses and {n} request(s) are running; restarting nginx cuts "
                         f"them. Continue?", False):
            todo("nginx not restarted yet: run ./install.sh again when the model is idle")
        else:
            run(["docker", "restart", ctx.name("nginx")])
    elif any(p.startswith("nginx/") for p in changed):
        run(["docker", "exec", ctx.name("nginx"), "nginx", "-t"], capture=True)
        run(["docker", "exec", ctx.name("nginx"), "nginx", "-s", "reload"])
    if "observability/prometheus.yml" in changed:
        run(["docker", "kill", "-s", "HUP", ctx.name("prometheus")])
    if any(p.startswith("observability/grafana") for p in changed):
        run(["docker", "restart", ctx.name("grafana")])


# --- data directories and secrets
DATA_DIRS = ("grafana", "prometheus", "arcane", "chatui", "acme/.well-known/acme-challenge", "tls", "backups",
             "model-logs")


def chk_data(ctx: Ctx) -> tuple[bool, str]:
    missing = [d for d in DATA_DIRS if not (ROOT / "data" / d).is_dir()]
    if ctx.on("ARCANE"):
        env = _read_env(ROOT / "data" / "arcane.env")
        missing += [f"arcane.env:{k}" for k in ("ENCRYPTION_KEY", "JWT_SECRET") if not env.get(k)]
    if ctx.on("CHATUI") and not (ROOT / "data" / "chatui.env").exists():
        missing.append("chatui.env")
    return (not missing), ("missing: " + ", ".join(missing)) if missing else "data/ ready"


def _read_env(p: Path) -> dict[str, str]:
    if not p.exists():
        return {}
    return dict(line.split("=", 1) for line in p.read_text().splitlines() if "=" in line and not line.startswith("#"))


def _write_env(p: Path, env: dict[str, str]) -> None:
    p.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    os.chmod(p, 0o600)


def do_data(ctx: Ctx) -> None:
    for d in DATA_DIRS:
        (ROOT / "data" / d).mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT / "data", 0o700)
    if ctx.on("ARCANE"):
        p = ROOT / "data" / "arcane.env"
        env = _read_env(p)
        env.setdefault("ENCRYPTION_KEY", secrets.token_hex(32))
        env.setdefault("JWT_SECRET", secrets.token_hex(32))
        _write_env(p, env)
    if ctx.on("CHATUI"):
        p = ROOT / "data" / "chatui.env"     # compose needs the file; its secrets come from the hub (step chat-ui sign-in)
        _write_env(p, _read_env(p))


# --- local CA certificate (always: the default server and the tailnet/LAN names use it)
def _cert_names(ctx: Ctx) -> list[str]:
    names = [ctx.conf["PUBLIC_HOST"], *ctx.conf["EXTRA_NAMES"].split(), "localhost"]
    ips = [a for a in ctx.conf["LISTEN_ADDRS"].split() if a] + ["127.0.0.1"]
    seen: list[str] = []
    for n in names + ips:
        if n and n not in seen:
            seen.append(n)
    return seen


def chk_local_cert(ctx: Ctx) -> tuple[bool, str]:
    crt = ROOT / "data" / "tls" / "server.crt"
    if not crt.exists():
        return False, "no local certificate yet"
    txt = out(["openssl", "x509", "-in", str(crt), "-noout", "-enddate", "-ext", "subjectAltName"])
    m = re.search(r"notAfter=(.+)", txt)
    days = (ssl.cert_time_to_seconds(m.group(1)) - time.time()) / 86400 if m else -1
    have = set(re.findall(r"(?:DNS|IP Address):([^,\s]+)", txt))     # exact entries: "spark" is not "spark-28.x"
    missing = [n for n in _cert_names(ctx) if n not in have]
    if missing:
        return False, f"does not cover {', '.join(missing)}"
    if days < 30:
        return False, f"expires in {days:.0f} days"
    return True, f"local CA certificate valid for {days:.0f} more days"


def do_local_cert(ctx: Ctx) -> None:
    run(["bash", "ops/tls/make-local-cert.sh", *_cert_names(ctx)], capture=True)
    if _containers(ctx).get("nginx") == "running":
        run(["docker", "exec", ctx.name("nginx"), "nginx", "-s", "reload"])


# --- images
def compose_images(ctx: Ctx) -> list[str]:
    txt = (ROOT / "compose.yaml").read_text() if (ROOT / "compose.yaml").exists() else ""
    imgs = re.findall(r"^\s+image:\s*(\S+)", txt, re.M)
    return [i for i in imgs if "@sha256:" in i]


def chk_images(ctx: Ctx) -> tuple[bool, str]:
    want = compose_images(ctx) + ([MODELS["fast"]["image"]] if ctx.modes() else [])
    missing = [i for i in dict.fromkeys(want) if not image_present(i)]
    if not image_present(f"{ctx.conf['INSTANCE']}-hub:dev"):
        missing.append(f"{ctx.conf['INSTANCE']}-hub:dev (built here)")
    return (not missing), ("missing: " + ", ".join(m.split("@")[0] for m in missing)) if missing else \
        f"{len(set(want)) + 1} pinned images present"


def do_images(ctx: Ctx) -> None:
    want = compose_images(ctx) + ([MODELS["fast"]["image"]] if ctx.modes() else [])
    for i in dict.fromkeys(want):
        if not image_present(i):
            run(["docker", "pull", i])
    ctx.compose("build", "hub")


def _arcane_pin_label() -> str:
    return f"{ARCANE_PIN['repo'].removeprefix('https://github.com/')}@{ARCANE_PIN['commit'][:12]}"


def chk_arcane_image(ctx: Ctx) -> tuple[bool, str]:
    img, src = ctx.conf["ARCANE_IMAGE"], ctx.conf.get("ARCANE_SOURCE", "")
    labels = out(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", img])
    if not labels:
        return False, f"{img} missing; will build from {src or _arcane_pin_label()}"
    if not src and (json.loads(labels) or {}).get("sparkhub.arcane-commit") != ARCANE_PIN["commit"]:
        return False, f"{img} is not {_arcane_pin_label()}; will rebuild"
    return True, f"{img} present" + ("" if src else f" ({_arcane_pin_label()})")


def do_arcane_image(ctx: Ctx) -> None:
    src = ctx.conf.get("ARCANE_SOURCE", "")
    if not src:                              # the pinned fork commit, fetched like chat-ui
        dst, commit = ROOT / "vendor" / "arcane-pinned", ARCANE_PIN["commit"]
        if not dst.exists() or out(["git", "rev-parse", "HEAD"], cwd=dst) != commit:
            dst.mkdir(parents=True, exist_ok=True)
            if not (dst / ".git").exists():
                run(["git", "init", "-q"], cwd=dst)
            run(["git", "fetch", "--depth", "1", ARCANE_PIN["repo"], commit], cwd=dst)
            run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=dst)
        if out(["git", "rev-parse", "HEAD"], cwd=dst) != commit:
            raise Abort(f"vendor/arcane-pinned is not at {commit}")
        run(["docker", "build", "-f", "docker/Dockerfile", "--label", f"sparkhub.arcane-commit={commit}",
             "-t", ctx.conf["ARCANE_IMAGE"], "."], cwd=dst)
        return
    if re.match(r"^(https?|git|ssh)://|^git@", src):
        dst = ROOT / "vendor" / "arcane"
        if not dst.exists():
            run(["git", "clone", src, str(dst)])
        run(["git", "fetch", "--all", "--tags"], cwd=dst)
        if ref := ctx.conf.get("ARCANE_REF"):
            # a branch name means the remote's branch (a fresh clone has no local branch but the default one)
            remote = subprocess.run(["git", "rev-parse", "--verify", "-q", f"origin/{ref}"], cwd=dst,
                                    capture_output=True).returncode == 0
            run(["git", "checkout", "--detach", f"origin/{ref}" if remote else ref], cwd=dst)
        src = str(dst)
    run(["docker", "build", "-f", "docker/Dockerfile", "-t", ctx.conf["ARCANE_IMAGE"], "."], cwd=Path(src))


# --- benchmarks: the runner image (bench/) and the test data in data/bench
TB2_PIN = {"repo": "https://github.com/laude-institute/terminal-bench-2",
           "commit": "2fd12b88aafdd04a52c298e3940bcb189f9766d6"}
BENCH_PREPARED = "2"        # bump when the lm-eval suites need other datasets (2: NLTK data kept)


def bench_on(ctx: Ctx) -> bool:
    return ctx.on("BENCH")


def _bench_hash() -> str:
    h = hashlib.sha256()
    for f in sorted((ROOT / "bench").rglob("*")):
        if f.is_file():
            h.update(f.relative_to(ROOT).as_posix().encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:16]


def chk_bench_image(ctx: Ctx) -> tuple[bool, str]:
    img = f"{ctx.conf['INSTANCE']}-bench:dev"
    labels = out(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", img])
    if not labels:
        return False, f"{img} missing; will build it from bench/ (~5 min)"
    if (json.loads(labels) or {}).get("sparkhub.bench-hash") != _bench_hash():
        return False, f"{img} is older than bench/; will rebuild"
    return True, f"{img} present"


def do_bench_image(ctx: Ctx) -> None:
    run(["docker", "build", "--label", f"sparkhub.bench-hash={_bench_hash()}", "-t",
         f"{ctx.conf['INSTANCE']}-bench:dev", "."], cwd=ROOT / "bench")


def chk_bench_data(ctx: Ctx) -> tuple[bool, str]:
    d = ROOT / "data" / "bench"
    probs = []
    tb2_head = out(["git", "rev-parse", "HEAD"], cwd=d / "tb2") if (d / "tb2" / ".git").exists() else ""
    if tb2_head != TB2_PIN["commit"]:
        probs.append(f"Terminal-Bench 2.0 @ {TB2_PIN['commit'][:7]} missing")
    marker = d / "hf" / ".sparkhub-prepared"
    if not marker.exists() or marker.read_text().strip() != BENCH_PREPARED:
        probs.append("knowledge and reasoning datasets not downloaded")
    return (not probs), "; ".join(probs) or f"Terminal-Bench 2.0 @ {TB2_PIN['commit'][:7]}, lm-eval datasets ready"


def do_bench_data(ctx: Ctx) -> None:
    d = ROOT / "data" / "bench"
    tb2 = d / "tb2"
    tb2.mkdir(parents=True, exist_ok=True)
    if not (tb2 / ".git").exists():
        run(["git", "init", "-q"], cwd=tb2)
    if out(["git", "rev-parse", "HEAD"], cwd=tb2) != TB2_PIN["commit"]:
        run(["git", "fetch", "--depth", "1", TB2_PIN["repo"], TB2_PIN["commit"]], cwd=tb2)
        run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=tb2)
    marker = d / "hf" / ".sparkhub-prepared"
    if not marker.exists() or marker.read_text().strip() != BENCH_PREPARED:
        bench = str(d)
        run(["docker", "run", "--rm", "--user", f"{ctx.conf['RUN_UID']}:{ctx.conf['RUN_GID']}", "-e", "HOME=/tmp",
             "-e", f"HF_HOME={bench}/hf", "-e", f"NLTK_DATA={bench}/nltk", "-v", f"{bench}:{bench}",
             f"{ctx.conf['INSTANCE']}-bench:dev", "prepare"])
        marker.write_text(BENCH_PREPARED + "\n")


def chk_chatui_image(ctx: Ctx) -> tuple[bool, str]:
    img = ctx.conf["CHATUI_IMAGE"]
    labels = out(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", img])
    if not labels:
        return False, f"{img} missing; will build chat-ui {CHATUI_PIN['ref']} (~10 min)"
    if (json.loads(labels) or {}).get("sparkhub.chatui-commit") != CHATUI_PIN["commit"]:
        return False, f"{img} is not chat-ui {CHATUI_PIN['ref']}; will rebuild"
    return True, f"{img} present (chat-ui {CHATUI_PIN['ref']})"


def do_chatui_image(ctx: Ctx) -> None:
    dst, commit = ROOT / "vendor" / "chat-ui", CHATUI_PIN["commit"]
    if not dst.exists() or out(["git", "rev-parse", "HEAD"], cwd=dst) != commit:
        dst.mkdir(parents=True, exist_ok=True)
        if not (dst / ".git").exists():
            run(["git", "init", "-q"], cwd=dst)
        run(["git", "fetch", "--depth", "1", CHATUI_PIN["repo"], commit], cwd=dst)
        run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=dst)
    if out(["git", "rev-parse", "HEAD"], cwd=dst) != commit:
        raise Abort(f"vendor/chat-ui is not at {commit}")
    # APP_BASE: the /chat prefix is compiled in; INCLUDE_DB: MongoDB runs inside the container
    run(["docker", "build", "--build-arg", "INCLUDE_DB=true", "--build-arg", "APP_BASE=/chat",
         "--build-arg", f"PUBLIC_COMMIT_SHA={commit}", "--label", f"sparkhub.chatui-commit={commit}",
         "-t", ctx.conf["CHATUI_IMAGE"], "."], cwd=dst)


# --- models
def _manifest(mode: str) -> dict:
    return json.loads((ROOT / "ops" / "models" / MODELS[mode]["manifest"]).read_text())


def _git_sha1(p: Path) -> str:
    h = hashlib.sha1(b"blob %d\0" % p.stat().st_size)
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def files_problems(d: Path, files: dict, verify: bool) -> list[str]:
    if not d.is_dir():
        return [f"{d} missing"]
    probs = []
    for name, meta in files.items():
        p = d / name
        if not p.exists():
            probs.append(f"{name} missing")
        elif p.stat().st_size != meta["size"]:
            probs.append(f"{name}: {p.stat().st_size} bytes, expected {meta['size']}")
        elif verify and "sha256" in meta and _sha256(p) != meta["sha256"]:
            probs.append(f"{name}: sha256 mismatch")
        elif verify and "git-sha1" in meta and _git_sha1(p) != meta["git-sha1"]:
            probs.append(f"{name}: content differs from the pinned revision")
    return probs


def license_ok(ctx: Ctx, mode: str) -> bool:
    """FLASH_LICENSE holds "<license id> <date>"; accepting an earlier Quality model's license does not count."""
    lic = MODELS[mode]["license"]
    return not lic.get("must_accept") or str(ctx.conf.get("FLASH_LICENSE", "")).split(" ")[0] == lic["id"]


def model_problems(ctx: Ctx, mode: str, verify: bool = False) -> list[str]:
    spec, man = MODELS[mode], _manifest(mode)
    probs = files_problems(ctx.path(spec["dir"]), man["files"], verify)
    if "extra" in spec:
        probs += [f"{spec['extra']['repo']}: {x}" for x in
                  files_problems(ctx.path(spec["extra"]["dir"]), man["extra"], verify)]
    if "runtime_dir" in spec and not ctx.path(spec["runtime_dir"]).is_dir():
        probs.append(f"runtime cache {ctx.path(spec['runtime_dir'])} missing")
    if not license_ok(ctx, mode):
        probs.insert(0, f"{spec['license']['name']} not accepted yet")
    return probs


def model_image_problems(ctx: Ctx, mode: str) -> list[str]:
    img = MODELS[mode]["image"]
    return [] if image_present(img) else [f"{img.split('@')[0]} not pulled"]


def chk_models(ctx: Ctx) -> tuple[bool, str]:
    msgs, good = [], True
    for m in ctx.modes():
        p = model_image_problems(ctx, m) + model_problems(ctx, m, ctx.verify)
        good &= not p
        msgs.append(f"{m}: " + ("; ".join(p[:3]) + (f" (+{len(p) - 3} more)" if len(p) > 3 else "") if p else
                                "complete" + (" (hashes verified)" if ctx.verify else "")))
    return good, " | ".join(msgs) or "no modes enabled"


def _hf(ctx: Ctx, image: str, args: list[str], mounts: list[str]) -> None:
    tok = hf_token()       # passed by name only, so the value never shows in the echoed command or `ps`
    vols = sum((["-v", m] for m in mounts), [])
    run(["docker", "run", "--rm", "--user", f"{ctx.conf['RUN_UID']}:{ctx.conf['RUN_GID']}", "-e", "HF_HOME=/hf",
         "-e", "HF_HUB_DISABLE_XET=0", "-e", "HF_XET_HIGH_PERFORMANCE=1", *(["-e", "HF_TOKEN"] if tok else []), *vols,
         "--entrypoint", "hf", image, *args], env={"HF_TOKEN": tok} if tok else None)


def do_models(ctx: Ctx) -> None:
    hf = Path(ctx.conf["HF_CACHE"])
    hf.mkdir(parents=True, exist_ok=True)
    for m in ctx.modes():
        spec = MODELS[m]
        head(f"Model: {spec['title']}")
        if not license_ok(ctx, m):
            todo(f"{m} skipped: {spec['license']['name']} not accepted (asked in step \"licenses\")")
            continue
        fast_img = MODELS["fast"]["image"]
        if not image_present(fast_img):
            run(["docker", "pull", fast_img])
        if m == "fast":
            d = ctx.path(spec["dir"])
            d.mkdir(parents=True, exist_ok=True)
            if files_problems(d, _manifest(m)["files"], False):
                _hf(ctx, fast_img, ["download", spec["repo"], "--revision", spec["revision"], "--local-dir", "/models"],
                    [f"{hf}:/hf", f"{d}:/models"])
        else:
            _prepare_flash(ctx, spec)
        p = model_problems(ctx, m)
        (ok if not p else bad)(f"{m}: " + ("complete" if not p else "; ".join(p[:3])))


def _prepare_flash(ctx: Ctx, spec: dict) -> None:
    """Quality: the recipe author's prebuilt image (pinned digest), the checkpoint in the HF cache, the few files
    of the base repo that b12x reads, and the writable runtime cache (compile and autotune results)."""
    if not image_present(spec["image"]):
        run(["docker", "pull", spec["image"]])
    hf, man, fast_img = Path(ctx.conf["HF_CACHE"]), _manifest("quality"), MODELS["fast"]["image"]
    if files_problems(ctx.path(spec["dir"]), man["files"], False):
        _hf(ctx, fast_img, ["download", spec["repo"], "--revision", spec["revision"], "--max-workers", "8"],
            [f"{hf}:/hf"])
    ex = spec["extra"]
    if files_problems(ctx.path(ex["dir"]), man["extra"], False):
        _hf(ctx, fast_img, ["download", ex["repo"], *man["extra"], "--revision", ex["revision"]], [f"{hf}:/hf"])
    ctx.path(spec["runtime_dir"]).mkdir(parents=True, exist_ok=True)


def embed_problems(ctx: Ctx, verify: bool = False) -> list[str]:
    spec = MODELS["embed"]
    probs = files_problems(ctx.path(spec["dir"]), _manifest("embed")["files"], verify)
    return probs + ([] if image_present(spec["image"]) else [f"{spec['image'].split('@')[0]} not pulled"])


def chk_embed(ctx: Ctx) -> tuple[bool, str]:
    p = embed_problems(ctx, ctx.verify)
    return not p, "; ".join(p[:3]) if p else "complete" + (" (hashes verified)" if ctx.verify else "")


def do_embed(ctx: Ctx) -> None:
    """The embedding service: llama.cpp's image (pinned digest) and two GGUF files, hash-checked after download."""
    spec = MODELS["embed"]
    if not image_present(spec["image"]):
        run(["docker", "pull", spec["image"]])
    d, hf, fast_img = ctx.path(spec["dir"]), Path(ctx.conf["HF_CACHE"]), MODELS["fast"]["image"]
    d.mkdir(parents=True, exist_ok=True)
    if not image_present(fast_img):
        run(["docker", "pull", fast_img])
    man = _manifest("embed")["files"]
    for f in spec["files"]:
        if files_problems(d, {f["file"]: man[f["file"]]}, False):
            _hf(ctx, fast_img, ["download", f["repo"], f["file"], "--revision", f["revision"], "--local-dir", "/models"],
                [f"{hf}:/hf", f"{d}:/models"])
    p = embed_problems(ctx, verify=True)
    (ok if not p else bad)("embeddings: " + ("complete (hashes verified)" if not p else "; ".join(p[:3])))


# --- TLS (Let's Encrypt; the local CA is its own step)
def chk_letsencrypt(ctx: Ctx) -> tuple[bool, str]:
    host = ctx.conf["PUBLIC_HOST"]
    if ctx.conf["LETSENCRYPT_ISSUED"] in FALSY:
        return False, f"no certificate for {host} yet"
    days = _served_cert_days(ctx, host, None)
    if days is None:
        return False, f"nginx does not serve a publicly trusted certificate for {host} (stack down?)"
    if days < 21:
        return False, f"{host}: expires in {days:.0f} days" + (" (DNS-01 has no automatic renewal)" if
                                                                ctx.conf["TLS_CHALLENGE"] == "dns" else "")
    hook = Path("/etc/letsencrypt/renewal-hooks/deploy/sparkhub-nginx-reload")
    if ctx.conf["TLS_CHALLENGE"] == "http" and not hook.exists():
        return False, "certbot deploy hook (nginx reload) missing"
    return True, f"{host}: Let's Encrypt, {days:.0f} days left"


def _served_cert_days(ctx: Ctx, name: str, cafile: str | None) -> float | None:
    sctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    try:
        with socket.create_connection(("127.0.0.1", int(ctx.conf["HTTPS_PORT"])), timeout=5) as s:
            with sctx.wrap_socket(s, server_hostname=name) as t:
                return (ssl.cert_time_to_seconds(t.getpeercert()["notAfter"]) - time.time()) / 86400
    except (OSError, ssl.SSLError):
        return None


def do_letsencrypt(ctx: Ctx) -> None:
    host = ctx.conf["PUBLIC_HOST"]
    if not shutil.which("certbot"):
        if not sudo(["apt-get", "install", "-y", "certbot"], "install certbot (Let's Encrypt client)", ctx):
            return
    acme = ROOT / "data" / "acme"
    if ctx.conf["TLS_CHALLENGE"] == "http":
        probe = acme / ".well-known" / "acme-challenge" / "sparkhub-probe"
        probe.write_text("ok")
        reach = _http_ok(f"http://{host}/.well-known/acme-challenge/sparkhub-probe", 10)
        probe.unlink()
        if not reach:
            bad(f"http://{host}/.well-known/acme-challenge/ does not reach this machine's nginx (is the stack up, "
                f"does port 80 reach it?). Use TLS_CHALLENGE=dns, or fix port 80 and re-run ./install.sh tls")
            return
        if not sudo(["certbot", "certonly", "--webroot", "-w", str(acme), "-d", host, "--cert-name", host,
                     "--agree-tos", "--register-unsafely-without-email", "-n", "--keep-until-expiring"],
                    f"get or renew the Let's Encrypt certificate for {host} (HTTP-01 via nginx's webroot)", ctx):
            return
        hook = (f"#!/bin/sh\n# SparkHub: reload nginx after certbot renews a certificate\n"
                f"docker exec {ctx.name('nginx')} nginx -s reload\n")
        sudo(f"install -d /etc/letsencrypt/renewal-hooks/deploy && printf %s {shlex.quote(hook)} > "
             f"/etc/letsencrypt/renewal-hooks/deploy/sparkhub-nginx-reload && chmod 755 "
             f"/etc/letsencrypt/renewal-hooks/deploy/sparkhub-nginx-reload",
             "reload nginx after each automatic renewal (certbot deploy hook)", ctx)
    else:
        say("  certbot will print a TXT record to add at your DNS provider, then wait for you.")
        if not sudo(["certbot", "certonly", "--manual", "--preferred-challenges", "dns", "-d", host, "--cert-name",
                     host, "--agree-tos", "--register-unsafely-without-email"],
                    f"get the Let's Encrypt certificate for {host} (DNS-01, manual; renew the same way)", ctx):
            return
    ctx.conf["LETSENCRYPT_ISSUED"] = "1"
    save_conf(ctx.conf)
    do_render(ctx)
    ctx.compose("up", "-d", "--remove-orphans")     # nginx mounts /etc/letsencrypt; the apps stop trusting the local CA


# --- Tailscale (access mode tailscale) and the .local name (mDNS)
def chk_tailscale(ctx: Ctx) -> tuple[bool, str]:
    st = tailscale_status()
    if not shutil.which("tailscale"):
        return False, "Tailscale not installed"
    if st.get("BackendState") != "Running":
        return False, f"Tailscale not connected ({st.get('BackendState') or 'not running'})"
    name = st.get("Self", {}).get("DNSName", "").rstrip(".")
    if ctx.on_access("tailscale") and name != ctx.conf["PUBLIC_HOST"]:
        return False, f"the tailnet name is {name}, sparkhub.conf has {ctx.conf['PUBLIC_HOST']} (./install.sh configure)"
    return True, f"connected as {name}"


def do_tailscale(ctx: Ctx) -> None:
    ensure_tailscale(ctx)


def lan_gateway(addr: str) -> str:
    """The default gateway on the interface that holds addr: the router, which DHCP clients use for DNS."""
    m = re.search(rf"^\d+:\s+(\S+)\s+inet {re.escape(addr)}/", out(["ip", "-4", "-o", "addr", "show"]), re.M)
    if not m:
        return ""
    g = re.search(r"default via (\S+)", out(["ip", "-4", "route", "show", "default", "dev", m.group(1)]))
    return g.group(1) if g else ""


def dns_a(server: str, name: str, timeout: float = 3, port: int = 53) -> list[str] | None:
    """A records for name from one DNS server (a minimal stub query); None when the server does not answer."""
    qid = secrets.randbits(16)
    q = qid.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    q += b"".join(bytes([len(x)]) + x.encode() for x in name.rstrip(".").split(".")) + b"\x00\x00\x01\x00\x01"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.settimeout(timeout)
            sk.sendto(q, (server, port))
            r = sk.recv(1500)
    except OSError:
        return None
    if len(r) < 12 or int.from_bytes(r[:2], "big") != qid:
        return None

    def skip(i: int) -> int:                     # past a (possibly compressed) name
        while r[i]:
            if r[i] & 0xC0 == 0xC0:
                return i + 2
            i += r[i] + 1
        return i + 1

    i, res = skip(12) + 4, []
    for _ in range(int.from_bytes(r[6:8], "big")):
        i = skip(i)
        typ, ln = int.from_bytes(r[i:i + 2], "big"), int.from_bytes(r[i + 8:i + 10], "big")
        if typ == 1 and ln == 4:
            res.append(socket.inet_ntoa(r[i + 10:i + 14]))
        i += 10 + ln
    return res


def _resolves(name: str) -> list[str]:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)})
    except OSError:
        return []


def mdns_names(ctx: Ctx) -> list[str]:
    return [n for n in [ctx.conf["PUBLIC_HOST"], *ctx.conf["EXTRA_NAMES"].split()] if n.endswith(".local")]


AVAHI_CONF = Path("/etc/avahi/avahi-daemon.conf")


def _lan_iface(ctx: Ctx) -> str:
    """The interface of the LAN listen address."""
    a = lan_addr(ctx.conf)
    m = re.search(rf"^\d+:\s+(\S+)\s+inet {re.escape(a)}/", out(["ip", "-4", "-o", "addr", "show"]), re.M) if a \
        else None
    return m.group(1) if m else ""


def _avahi_allowed() -> list[str]:
    txt = AVAHI_CONF.read_text() if AVAHI_CONF.exists() else ""
    m = re.search(r"^allow-interfaces=(.*)$", txt, re.M)
    return [x.strip() for x in m.group(1).split(",")] if m else []


def chk_mdns(ctx: Ctx) -> tuple[bool, str]:
    names = ", ".join(mdns_names(ctx))
    if _systemd("avahi-daemon.service", "is-active") != "active":
        return False, f"avahi-daemon (announces {names} on the LAN) is not running"
    if any(n != f"{socket.gethostname()}.local" for n in mdns_names(ctx)):
        return False, f"avahi announces only {socket.gethostname()}.local; use that name (./install.sh configure)"
    iface = _lan_iface(ctx)
    if not iface:
        return False, "no LAN address in LISTEN_ADDRS to announce"
    # otherwise avahi also announces the Docker bridges' addresses, and churns with every container start
    if _avahi_allowed() != [iface]:
        return False, f"avahi should announce on {iface} only (it uses: {', '.join(_avahi_allowed()) or 'every interface'})"
    return True, f"{names} announced on {iface} (mDNS)"


def do_mdns(ctx: Ctx) -> None:
    iface = _lan_iface(ctx)
    if not iface:
        raise Abort("no LAN address in LISTEN_ADDRS; add it (./install.sh configure)")
    pkgs = [p for p in ("avahi-daemon", "libnss-mdns")       # libnss-mdns: this machine resolves .local names too
            if "ok installed" not in out(["dpkg-query", "-W", "-f=${Status}", p])]
    conf = (f"sed -i -E '/^#?allow-interfaces=/d; s/^\\[server\\]$/[server]\\nallow-interfaces={iface}/' "
            f"{AVAHI_CONF}")
    cmd = (f"apt-get install -y {' '.join(pkgs)} && " if pkgs else "") + \
        f"{conf} && systemctl enable avahi-daemon && systemctl restart avahi-daemon"
    sudo(cmd, f"announce this machine as {socket.gethostname()}.local on the LAN ({iface} only; avahi, mDNS)", ctx)


# --- Tailscale certificate (TLS=tailscale)
TS_CERT = ROOT / "data" / "tls" / "tailscale.crt"


def chk_ts_cert(ctx: Ctx) -> tuple[bool, str]:
    host = ctx.conf["PUBLIC_HOST"]
    if ctx.conf.get("TAILSCALE_CERT_ISSUED") in FALSY or not TS_CERT.exists():
        return False, f"no Tailscale certificate for {host} yet"
    if _systemd("sparkhub-tailscale-cert.timer") != "enabled":
        return False, "renewal timer (sparkhub-tailscale-cert.timer) not installed"
    days = _served_cert_days(ctx, host, None)
    if days is None:
        return False, f"nginx does not serve a publicly trusted certificate for {host} (stack down?)"
    if days < 14:
        return False, f"{host}: expires in {days:.0f} days (journalctl -u sparkhub-tailscale-cert)"
    return True, f"{host}: Tailscale certificate, {days:.0f} days left, renewed by itself"


def do_ts_cert(ctx: Ctx) -> None:
    host = ctx.conf["PUBLIC_HOST"]
    while host not in (tailscale_status().get("CertDomains") or []):
        say("  HTTPS certificates are off on your tailnet. Turn them on once in the Tailscale admin console:")
        say("    https://login.tailscale.com/admin/dns  →  HTTPS Certificates  →  Enable HTTPS")
        if ctx.check:
            return
        if ask("Press Enter when done, or type local to use the SparkHub certificate instead", "") == "local":
            ctx.conf["TLS"] = "local-ca"
            save_conf(ctx.conf)
            do_render(ctx)
            ctx.compose("up", "-d", "--remove-orphans")
            return
    o = ROOT / "ops" / "tailscale-cert"
    env = (f"NAME={host}\nDIR={ROOT / 'data' / 'tls'}\nOWNER={ctx.conf['RUN_UID']}:{ctx.conf['RUN_GID']}\n"
           f"NGINX={ctx.name('nginx')}\n")
    if not sudo(f"install -m 755 {o}/sparkhub-tailscale-cert /usr/local/sbin/ && printf %s {shlex.quote(env)} > "
                f"/etc/default/sparkhub-tailscale-cert && install -m 644 {o}/sparkhub-tailscale-cert.service "
                f"{o}/sparkhub-tailscale-cert.timer /etc/systemd/system/ && systemctl daemon-reload && "
                f"systemctl enable --now sparkhub-tailscale-cert.timer && systemctl start sparkhub-tailscale-cert",
                f"get the Tailscale certificate for {host} into data/tls and renew it daily when due", ctx):
        return
    if not TS_CERT.exists():
        raise Abort("tailscale cert did not write a certificate; see: journalctl -u sparkhub-tailscale-cert")
    ctx.conf["TAILSCALE_CERT_ISSUED"] = "1"
    save_conf(ctx.conf)
    do_render(ctx)
    ctx.compose("up", "-d", "--remove-orphans")     # the apps stop trusting the local CA for this name
    run(["docker", "exec", ctx.name("nginx"), "nginx", "-s", "reload"], check=False)


# --- host pieces (each optional)
def _systemd(unit: str, what: str = "is-enabled") -> str:
    return out(["systemctl", what, unit])


def chk_ssh_geo(ctx: Ctx) -> tuple[bool, str]:
    en = _systemd("sparkhub-ssh-geo.timer")
    want = f"COUNTRY={ctx.conf['SSH_GEO_COUNTRY']}\nTRUSTED4={ctx.conf['LAN_CIDR']} 100.64.0.0/10\n"
    cur = Path("/etc/default/sparkhub-ssh-geo").read_text() if Path("/etc/default/sparkhub-ssh-geo").exists() else ""
    if en != "enabled":
        return False, "not installed"
    if cur and cur != want:
        return False, "installed with other settings"
    return True, f"SSH from {ctx.conf['SSH_GEO_COUNTRY']}, {ctx.conf['LAN_CIDR']} and Tailscale only"


def do_ssh_geo(ctx: Ctx) -> None:
    if not ctx.conf.get("SSH_GEO_COUNTRY") or not ctx.conf.get("LAN_CIDR"):
        raise Abort("SSH_GEO needs SSH_GEO_COUNTRY and LAN_CIDR in sparkhub.conf")
    o = ROOT / "ops" / "ssh-geo"
    env = f"COUNTRY={ctx.conf['SSH_GEO_COUNTRY']}\nTRUSTED4={ctx.conf['LAN_CIDR']} 100.64.0.0/10\n"
    say("  Keep a second session open while this is applied: a wrong country or LAN locks SSH from outside.")
    sudo(f"install -m 755 {o}/sparkhub-ssh-geo /usr/local/sbin/ && install -D -m 644 {o}/build.py "
         f"/usr/local/lib/sparkhub-ssh-geo/build.py && printf %s {shlex.quote(env)} > /etc/default/sparkhub-ssh-geo && "
         f"install -m 644 {o}/sparkhub-ssh-geo.service {o}/sparkhub-ssh-geo.timer /etc/systemd/system/ && "
         f"systemctl daemon-reload && systemctl enable --now sparkhub-ssh-geo.service sparkhub-ssh-geo.timer",
         "install the SSH country allowlist (nftables table inet ssh_geo, refreshed weekly)", ctx)


def _port_guard_nft(ctx: Ctx) -> str:
    src = (ROOT / "ops" / "port-guard" / "port-guard.nft").read_text()
    ports = ", ".join(ctx.conf["PORT_GUARD_PORTS"].replace(",", " ").split())
    src = re.sub(r"(set guarded \{[^}]*elements = \{ )[^}]*( \})", rf"\g<1>{ports}\g<2>", src, flags=re.S)
    return re.sub(r"(set trusted4 \{[^}]*elements = \{ )[^}]*( \})",
                  rf"\g<1>{ctx.conf['LAN_CIDR']}, 100.64.0.0/10, 127.0.0.0/8\g<2>", src, flags=re.S)


def chk_port_guard(ctx: Ctx) -> tuple[bool, str]:
    inst = Path("/usr/local/lib/sparkhub-port-guard/port-guard.nft")
    if _systemd("sparkhub-port-guard.service") != "enabled" or not inst.exists():
        return False, "not installed"
    if inst.read_text() != _port_guard_nft(ctx):
        return False, "installed with other ports or LAN"
    return True, f"ports {ctx.conf['PORT_GUARD_PORTS']} closed to the internet"


def do_port_guard(ctx: Ctx) -> None:
    tmp = ROOT / "data" / "port-guard.nft"
    tmp.write_text(_port_guard_nft(ctx))
    o = ROOT / "ops" / "port-guard"
    sudo(f"install -D -m 644 {tmp} /usr/local/lib/sparkhub-port-guard/port-guard.nft && install -m 644 "
         f"{o}/sparkhub-port-guard.service /etc/systemd/system/ && systemctl daemon-reload && "
         f"systemctl enable sparkhub-port-guard && systemctl restart sparkhub-port-guard",
         "install the port guard (nftables table inet port_guard)", ctx)


def chk_headless(ctx: Ctx) -> tuple[bool, str]:
    t = out(["systemctl", "get-default"])
    return t == "multi-user.target", f"boots to {t}"


def do_headless(ctx: Ctx) -> None:
    sudo(["systemctl", "set-default", "multi-user.target"], "boot without the desktop from the next boot on "
         "(undo: sudo systemctl set-default graphical.target)", ctx)


# --- the running stack
def _containers(ctx: Ctx) -> dict[str, str]:
    txt = out(["docker", "compose", "ps", "--all", "--format", "{{.Service}} {{.State}}"])
    return dict(line.split(" ", 1) for line in txt.splitlines() if " " in line)


def _services(ctx: Ctx) -> list[str]:
    return ["hub", "prometheus", "node-exporter", "grafana", "nginx"] + (["arcane"] if ctx.on("ARCANE") else []) + \
        (["chatui"] if ctx.on("CHATUI") else [])


def chat_turned_off(ctx: Ctx) -> bool:
    """Turned off on the hub's Settings page: then the hub keeps chat-ui's container stopped."""
    return out(["docker", "exec", ctx.name("hub"), "sparkhub-admin", "chat", "status"]) == "off"


def chk_stack(ctx: Ctx) -> tuple[bool, str]:
    st = _containers(ctx)
    down = [s for s in _services(ctx) if st.get(s) != "running"]
    if "chatui" in down and st.get("chatui") and chat_turned_off(ctx):
        down.remove("chatui")
    return (not down), ("not running: " + ", ".join(down)) if down else f"{len(_services(ctx))} containers running"


def do_stack(ctx: Ctx) -> None:
    ctx.compose("up", "-d", "--remove-orphans")
    wait_hub(ctx)


def wait_hub(ctx: Ctx) -> None:
    for _ in range(60):
        if _http_ok(f"http://127.0.0.1:{ctx.conf['HUB_PORT']}/login"):
            return
        time.sleep(1)
    raise Abort("the hub did not come up; see: docker compose logs hub")


def hub_admin(ctx: Ctx, *args: str, tty: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "exec", *(["-it"] if tty else []), ctx.name("hub"), "sparkhub-admin", *args],
               capture=not tty, check=check, quiet=not tty)


def _oidc_client(ctx: Ctx, client: str) -> str | None:
    """The `oidc list` line of a client, None if it is not registered."""
    lst = out(["docker", "exec", ctx.name("hub"), "sparkhub-admin", "oidc", "list"])
    return next((line for line in lst.splitlines() if line.split(" ", 1)[0] == client), None)


def chk_oidc(ctx: Ctx) -> tuple[bool, str]:
    env = _read_env(ROOT / "data" / "arcane.env")
    line = _oidc_client(ctx, "arcane")
    if line is None:
        return False, "OIDC client for Arcane not registered"
    if f"{derive(ctx.conf)['PUBLIC_URL']}/arcane/auth/oidc/callback" not in line:
        return False, "Arcane's OIDC redirect URI differs from PUBLIC_HOST"
    if not env.get("OIDC_CLIENT_SECRET"):
        return False, "OIDC_CLIENT_SECRET missing from data/arcane.env"
    return True, "Arcane signs in through the hub"


def do_oidc(ctx: Ctx) -> None:
    r = hub_admin(ctx, "oidc", "add-client", "arcane", "--redirect-uri",
                  f"{derive(ctx.conf)['PUBLIC_URL']}/arcane/auth/oidc/callback")
    secret = r.stdout.strip().splitlines()[-1]
    p = ROOT / "data" / "arcane.env"
    env = _read_env(p)
    env["OIDC_CLIENT_SECRET"] = secret
    _write_env(p, env)
    ctx.compose("up", "-d", "arcane")


def chk_chatui(ctx: Ctx) -> tuple[bool, str]:
    env = _read_env(ROOT / "data" / "chatui.env")
    line = _oidc_client(ctx, "chatui")
    if line is None:
        return False, "OIDC client for chat-ui not registered"
    if f"{derive(ctx.conf)['PUBLIC_URL']}/chat/login/callback" not in line.split():
        return False, "chat-ui's OIDC redirect URI differs from PUBLIC_HOST"
    if not env.get("OPENID_CLIENT_SECRET"):
        return False, "OPENID_CLIENT_SECRET missing from data/chatui.env"
    keys = out(["docker", "exec", ctx.name("hub"), "sparkhub-admin", "key", "list"])
    kid = env.get("SPARKHUB_KEY_ID", "")
    if not env.get("OPENAI_API_KEY") or not any(line.split()[:2] == [kid, "chatui"] and " active " in line
                                                    for line in keys.splitlines() if line.strip()):
        return False, "chat-ui has no active API key"
    return True, "chat-ui signs in through the hub and has its API key"


def do_chatui(ctx: Ctx) -> None:
    p = ROOT / "data" / "chatui.env"
    env = _read_env(p)
    r = hub_admin(ctx, "oidc", "add-client", "chatui", "--redirect-uri",
                  f"{derive(ctx.conf)['PUBLIC_URL']}/chat/login/callback")
    env["OPENID_CLIENT_SECRET"] = r.stdout.strip().splitlines()[-1]
    if env.get("SPARKHUB_KEY_ID"):           # replacing a key: the old one goes
        hub_admin(ctx, "key", "revoke", env["SPARKHUB_KEY_ID"], check=False)
    # infer only: chat-ui can chat, not switch modes; every chat request shows as key "chatui" in Activity
    r = hub_admin(ctx, "key", "create", "chatui", "--scope", "infer")
    env["OPENAI_API_KEY"] = r.stdout.strip().splitlines()[-1]
    env["SPARKHUB_KEY_ID"] = re.search(r"key (\d+)", r.stdout).group(1)
    _write_env(p, env)
    ctx.compose("up", "-d", "--force-recreate", "chatui")


def chk_admin(ctx: Ctx) -> tuple[bool, str]:
    users = out(["docker", "exec", ctx.name("hub"), "sparkhub-admin", "user", "list"])
    n = len([u for u in users.splitlines() if u.strip()])
    return n > 0, f"{n} dashboard user(s)" if n else "no dashboard user yet"


def do_admin(ctx: Ctx) -> None:
    # unattended installs: SPARKHUB_ADMIN_USER + SPARKHUB_ADMIN_PASSWORD (at least 12 characters)
    if os.environ.get("SPARKHUB_ADMIN_USER") and os.environ.get("SPARKHUB_ADMIN_PASSWORD"):
        name = os.environ["SPARKHUB_ADMIN_USER"]
        hub_admin(ctx, "user", "create", name, "--password", os.environ["SPARKHUB_ADMIN_PASSWORD"])
    else:
        name = ask("Username for the first dashboard admin", getpass.getuser())
        hub_admin(ctx, "user", "create", name, tty=True)
    say(f"  Log in at {derive(ctx.conf)['PUBLIC_URL']}/ and enrol the authenticator app (TOTP) on first login.")


def _status(ctx: Ctx) -> dict:
    """Mode and state from the hub's /metrics (localhost only, no key needed)."""
    try:
        txt = urllib.request.urlopen(f"http://127.0.0.1:{ctx.conf['HUB_PORT']}/metrics", timeout=5).read().decode()
    except Exception:
        return {}
    m = re.search(r'^sparkhub_mode\{mode="(\w+)",state="(\w+)"\} 1\.0', txt, re.M)
    return {"mode": m.group(1) if m and m.group(1) != "none" else None, "state": m.group(2) if m else None}


def chk_model_loaded(ctx: Ctx) -> tuple[bool, str]:
    st = _status(ctx)
    if not ctx.on("AUTOSTART"):
        return True, f"autostart off ({st.get('state') or 'hub not answering'})"
    if st.get("state") == "ready":
        return True, f"{st['mode']} loaded"
    return False, f"state {st.get('state') or 'unknown'}"


def do_model_loaded(ctx: Ctx) -> None:
    say("  The hub loads the last mode by itself (first load ~4-5 min). Waiting up to 25 min...")
    t0 = time.time()
    while time.time() - t0 < 1500:
        st = _status(ctx)
        if st.get("state") == "ready":
            return
        if st.get("state") == "failed":
            raise Abort("the model failed to load; see the dashboard's Logs page or docker logs "
                        f"{ctx.name('fast')} / {ctx.name('quality')}")
        time.sleep(10)
    raise Abort("no model after 25 min")


STEPS = [
    Step("preflight", chk_preflight, None),
    Step("network", chk_network, None, lambda ctx: not ctx.check),
    Step("licenses and Hugging Face token", chk_licenses, do_licenses, lambda ctx: bool(ctx.modes() or ctx.embeddings())),
    Step("Tailscale", chk_tailscale, do_tailscale,
         lambda ctx: ctx.on_access("tailscale") or ctx.conf["TLS"] == "tailscale"),
    Step("NAME.local on the LAN", chk_mdns, do_mdns, lambda ctx: bool(mdns_names(ctx)) and not ctx.on_access("public")),
    Step("config files", chk_render, do_render),
    Step("data and secrets", chk_data, do_data),
    Step("local certificate", chk_local_cert, do_local_cert),
    Step("images", chk_images, do_images),
    Step("Arcane image", chk_arcane_image, do_arcane_image, lambda ctx: ctx.on("ARCANE")),
    Step("chat-ui image", chk_chatui_image, do_chatui_image, lambda ctx: ctx.on("CHATUI")),
    Step("benchmark runner image", chk_bench_image, do_bench_image, lambda ctx: bench_on(ctx)),
    Step("benchmark test data", chk_bench_data, do_bench_data, lambda ctx: bench_on(ctx)),
    Step("models", chk_models, do_models, lambda ctx: bool(ctx.modes())),
    Step("embedding models", chk_embed, do_embed, lambda ctx: ctx.embeddings()),
    Step("stack running", chk_stack, do_stack),
    Step("Let's Encrypt", chk_letsencrypt, do_letsencrypt, lambda ctx: ctx.conf["TLS"] == "letsencrypt"),
    Step("Tailscale certificate", chk_ts_cert, do_ts_cert, lambda ctx: ctx.conf["TLS"] == "tailscale"),
    Step("Arcane sign-in (OIDC)", chk_oidc, do_oidc, lambda ctx: ctx.on("ARCANE")),
    Step("chat-ui sign-in (OIDC) and API key", chk_chatui, do_chatui, lambda ctx: ctx.on("CHATUI")),
    Step("dashboard admin", chk_admin, do_admin),
    Step("first model load", chk_model_loaded, do_model_loaded, lambda ctx: bool(ctx.modes())),
    Step("host: SSH allowlist", chk_ssh_geo, do_ssh_geo, lambda ctx: ctx.on("SSH_GEO")),
    Step("host: port guard", chk_port_guard, do_port_guard, lambda ctx: ctx.on("PORT_GUARD")),
    Step("host: headless boot", chk_headless, do_headless, lambda ctx: ctx.on("HEADLESS")),
]


def run_steps(ctx: Ctx, only: list[str] | None = None) -> bool:
    all_ok = True
    for st in STEPS:
        if only and st.name not in only:
            continue
        if not st.when(ctx):
            continue
        head(st.name)
        good, detail = st.check(ctx)
        if good:
            ok(detail)
            continue
        if ctx.check or st.apply is None:
            (bad if st.apply is None else todo)(detail)
            all_ok = False
            if st.name == "preflight" and not ctx.check:
                raise Abort("preflight failed: fix the above, then run ./install.sh again")
            continue
        todo(detail)
        st.apply(ctx)
        good, detail = st.check(ctx)
        (ok if good else bad)(detail)
        all_ok &= good
    return all_ok


# ----------------------------------------------------------------------------------------------- self-test
def selftest(ctx: Ctx, switch: bool = False) -> bool:
    v = derive(ctx.conf)
    host, port = ctx.conf["PUBLIC_HOST"], int(ctx.conf["HTTPS_PORT"])
    cafile = None if v["TRUSTED"] else str(ROOT / "data" / "tls" / "ca.crt")
    sctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    results: list[bool] = []

    def chk(name: str, good: bool, detail: str = "") -> None:
        results.append(bool(good))
        (ok if good else bad)(name + (f"  [{detail}]" if detail else ""))

    import http.client

    class Conn(http.client.HTTPSConnection):
        """Connects to nginx on 127.0.0.1 but presents the public name (SNI and Host), as a client would."""
        def connect(self):
            self.sock = sctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=self.timeout),
                                         server_hostname=host)

    def req(method: str, path: str, body: dict | None = None, key: str | None = None) -> tuple[int, dict, str]:
        conn = Conn(host, port, timeout=300, context=sctx)
        h = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if key:
            h["Authorization"] = f"Bearer {key}"
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read().decode(errors="replace")
        finally:
            conn.close()

    head(f"Self-test against https://{host}{v['HTTPS_PORT_SUFFIX']} (via 127.0.0.1)")
    try:
        st, _, body = req("GET", "/login")
        chk("TLS verifies and the login page answers", st == 200 and "password" in body.lower(), str(st))
    except (OSError, ssl.SSLError) as e:
        chk("TLS verifies and the login page answers", False, f"{type(e).__name__}: {e}")
        return False
    gated = ["/grafana/"] + (["/arcane/"] if ctx.on("ARCANE") else []) + (["/chat/"] if ctx.on("CHATUI") else []) + \
        (["/nvidia/"] if ctx.on("NVIDIA_DASHBOARD") else [])
    for p in gated:
        st, h, _ = req("GET", p)
        loc = urllib.parse.urlsplit(h.get("Location", ""))
        chk(f"{p} needs the hub login", st == 302 and loc.path == "/login", f"{st} -> {loc.path}")
    for p in ("/metrics", "/internal/grafana-alert"):
        st, _, _ = req("GET", p)
        chk(f"{p} not reachable from outside", st == 404, str(st))
    if ctx.on("CHATUI"):
        if chat_turned_off(ctx):
            info("chat-ui is turned off on the Settings page: not tested")
        else:
            url = f"http://127.0.0.1:{ctx.conf['CHATUI_PORT']}/chat/healthcheck"
            good = False
            for _ in range(30):            # a container just (re)created needs a few seconds
                try:
                    good = urllib.request.urlopen(url, timeout=5).status == 200
                    break
                except Exception:
                    time.sleep(2)
            chk("chat-ui answers", good)
    st, _, body = req("GET", "/oidc/.well-known/openid-configuration")
    chk("OIDC discovery", st == 200 and json.loads(body).get("issuer") == f"{v['PUBLIC_URL']}/oidc", str(st))
    st, _, _ = req("GET", "/v1/models")
    chk("API refuses requests without a key", st == 401, str(st))
    r = hub_admin(ctx, "key", "create", "install-selftest", "--scope", "admin", "--expires-days", "0.02")
    key = r.stdout.strip().splitlines()[-1]
    kid = re.search(r"key (\d+)", r.stdout).group(1)
    try:
        st, _, body = req("GET", "/v1/models", key=key)
        ids = [m["id"] for m in json.loads(body).get("data", [])] if st == 200 else []
        chk("/v1/models lists fast, quality, auto", {"fast", "quality", "auto"} <= set(ids), ",".join(ids))
        st, _, body = req("GET", "/api/v1/status", key=key)
        status = json.loads(body) if st == 200 else {}
        missing = {m: p for m, p in status.get("install", {}).items() if p and m in ctx.modes()}
        chk("enabled modes are loadable (image + weights)", not missing, json.dumps(missing)[:200])
        loaded = status.get("mode") if status.get("state") == "ready" else None
        if loaded:
            st, _, body = req("POST", "/v1/chat/completions", key=key, body={
                "model": "auto", "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                "max_tokens": 20})
            txt = json.loads(body)["choices"][0]["message"]["content"] if st == 200 else body[:200]
            chk(f"chat completion ({loaded})", st == 200 and "pong" in (txt or "").lower(), (txt or "").strip()[:60])
            st, _, body = req("POST", "/v1/chat/completions", key=key, body={
                "model": "auto", "stream": True, "max_tokens": 20,
                "messages": [{"role": "user", "content": "Count from 1 to 5."}]})
            chk("streaming", st == 200 and body.count("data: ") > 2 and "[DONE]" in body,
                f"{body.count('data: ')} events")
            if switch:
                other = next((m for m in ctx.modes() if m != loaded), None)
                if other:
                    for target in (other, loaded):
                        info(f"switching to {target} (a few minutes)...")
                        st, _, body = req("POST", "/api/v1/mode", key=key, body={"mode": target})
                        t0 = time.time()
                        while time.time() - t0 < 1500:
                            time.sleep(10)
                            s2 = json.loads(req("GET", "/api/v1/status", key=key)[2])
                            if s2.get("state") in ("ready", "failed") and not s2.get("switch"):
                                break
                        chk(f"switch to {target}", s2.get("mode") == target and s2.get("state") == "ready",
                            f"{s2.get('state')} {s2.get('mode')}, {time.time() - t0:.0f} s")
        else:
            info(f"no model loaded ({status.get('state')}): chat and streaming not tested")
    finally:
        hub_admin(ctx, "key", "revoke", kid, check=False)
    say(f"\n  {sum(results)}/{len(results)} checks passed")
    lan_checks(ctx)
    return all(results)


def _tls_ok(addr: str, name: str, port: int) -> bool:
    try:
        with socket.create_connection((addr, port), timeout=5) as sk:
            with ssl.create_default_context().wrap_socket(sk, server_hostname=name):
                return True
    except (OSError, ssl.SSLError):
        return False


def lan_checks(ctx: Ctx) -> None:
    """The optional steps on the router (manual_steps): reported, never a failure."""
    mode, host, lan = access(ctx.conf), ctx.conf["PUBLIC_HOST"], lan_addr(ctx.conf)
    if not lan or (mode == "local" and host.endswith(".local")):      # mDNS has its own step
        return
    head("LAN devices")
    gw = lan_gateway(lan)
    ans = dns_a(gw, host) if gw else None
    if ans and lan in ans:
        ok(f"your router answers {host} → {lan}: LAN devices reach everything directly")
        return
    if mode == "public":
        pub = _resolves(host)
        if pub and _tls_ok(pub[0], host, int(ctx.conf["HTTPS_PORT"])):
            ok(f"hairpin NAT works: LAN devices reach https://{host} through the public address {pub[0]}")
        else:
            info(f"https://{host} through {pub[0] if pub else 'its public address'} does not answer from the LAN "
                 f"(no hairpin NAT?). Add a DNS entry on your router: {host} → {lan}")
        return
    what = "everything" if mode == "tailscale" else "the hub"
    if ans is None:
        info(f"could not ask your router ({gw or 'no gateway'}) for {host}. From a LAN device, `nslookup {host}` "
             f"should give {lan}; if not, add that DNS entry on the router so LAN devices get {what}")
    elif mode == "tailscale":
        info(f"optional: a DNS entry on your router, {host} → {lan}, gives LAN devices without Tailscale everything "
             f"(now: the dashboard and the API at the other names)")
    else:
        info(f"your router does not know {host}: add a DNS entry, {host} → {lan}")


# ----------------------------------------------------------------------------------------------- update / uninstall
def active_requests(ctx: Ctx) -> int:
    try:
        url = (f"http://127.0.0.1:{ctx.conf['PROMETHEUS_PORT']}/api/v1/query?query="
               "sum(vllm:num_requests_running)%2Bsum(vllm:num_requests_waiting)")
        res = json.load(urllib.request.urlopen(url, timeout=5))["data"]["result"]
        return int(float(res[0]["value"][1])) if res else 0
    except Exception:
        return 0


def running_benchmark() -> int | None:
    """The id of a benchmark that is running or about to: restarting the hub would fail it."""
    db = ROOT / "data" / "sparkhub.db"
    if not db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5) as c:
            row = c.execute("SELECT id FROM bench_runs WHERE status IN ('starting', 'running') LIMIT 1").fetchone()
        return row[0] if row else None
    except sqlite3.Error:                     # no bench_runs table yet (an older hub)
        return None


# what a release may need before anything restarts: built while the old stack (and model) keep serving
PREPARE_STEPS = ["Arcane image", "chat-ui image", "benchmark runner image", "benchmark test data", "models",
                 "embedding models"]


def db_schema_version() -> int | None:
    """The database's migration version (PRAGMA user_version), None without a database."""
    db = ROOT / "data" / "sparkhub.db"
    if not db.exists():
        return None
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def code_schema_version() -> int | None:
    """How many migrations this checkout's hub has (the SCHEMA list in hub/sparkhub/db.py, read without running it)."""
    try:
        tree = ast.parse((ROOT / "hub" / "sparkhub" / "db.py").read_text())
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) \
            else []
        if any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in targets) and isinstance(node.value, ast.List):
            return len(node.value.elts)
    return None


def update(ctx: Ctx) -> None:
    """Apply this checkout. After ./install.sh upgrade (SPARKHUB_ROLLBACK set), a failure offers to go back."""
    rb = json.loads(os.environ.get("SPARKHUB_ROLLBACK") or "null")
    stage = {"restarted": False}
    try:
        _update(ctx, rb, stage)
    except Abort as e:
        if rb and not ctx.check:
            offer_rollback(ctx, rb, stage["restarted"], str(e))     # goes back (no return), or the user stays
        raise


def _update(ctx: Ctx, rb: dict | None, stage: dict) -> None:
    db_v, code_v = db_schema_version(), code_schema_version()
    if db_v is not None and code_v is not None and db_v > code_v:
        raise Abort(f"the database is at schema v{db_v}, newer than this checkout's hub knows (v{code_v}): a newer "
                    "release used it. Move to that release (./install.sh upgrade), or restore a backup from before it "
                    "(data/backups/pre-upgrade-*.db) with the hub stopped")
    if rid := running_benchmark():
        raise Abort(f"benchmark #{rid} is running; restarting the hub would fail it. Update after it ends "
                    "(or cancel it on the Benchmarks page)")
    n = active_requests(ctx)
    if n and not yes(f"{n} request(s) are running on the model; restarting the hub cuts them. Continue?", False):
        raise Abort("update postponed")
    head("config files")
    do_render(ctx)
    head("images")
    for i in dict.fromkeys(compose_images(ctx)):
        if not image_present(i):
            run(["docker", "pull", i])
    ctx.compose("build", "hub")
    if not run_steps(ctx, PREPARE_STEPS):
        raise Abort("a step above failed; nothing was restarted. Fix it, then run ./install.sh update again")
    if not chk_data(ctx)[0]:                 # files a newly enabled piece needs (e.g. data/chatui.env)
        do_data(ctx)
    if db_v is not None and code_v is not None and db_v < code_v:
        head(f"database: the hub migrates it from v{db_v} to v{code_v} when it starts")
        if rb and rb.get("backup"):
            ok(f"backed up before the upgrade: {Path(rb['backup']).relative_to(ROOT)}")
        else:
            ok(f"backed up to {backup_db(f'pre-update-v{db_v}-to-v{code_v}').relative_to(ROOT)}")
    head("restart what changed (the model reloads only if this release changed it)")
    stage["restarted"] = True
    ctx.compose("up", "-d", "--remove-orphans")
    wait_hub(ctx)
    run(["docker", "exec", ctx.name("nginx"), "nginx", "-s", "reload"], check=False)


# ----------------------------------------------------------------------------------------------- versions
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def version() -> str:
    """The release this checkout is (VERSION), 'dev' without one."""
    try:
        return VERSION_FILE.read_text().strip() or "dev"
    except OSError:
        return "dev"


def vtuple(v: str) -> tuple[int, int, int] | None:
    m = TAG_RE.match(v if v.startswith("v") else "v" + v)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def changelog_between(text: str, old: str, new: str) -> str:
    """The CHANGELOG.md sections after `old` up to and including `new` (headings '## X.Y.Z ...')."""
    lo, hi = vtuple(old) or (0, 0, 0), vtuple(new)
    keep, out_lines = False, []
    for line in text.splitlines():
        if line.startswith("## "):
            v = vtuple(line[3:].split()[0]) if line[3:].split() else None
            keep = bool(v and hi and lo < v <= hi)
        if keep:
            out_lines.append(line)
    return "\n".join(out_lines).strip()


def backup_db(name: str) -> Path | None:
    """A consistent copy of the database (SQLite's backup API, safe while the hub writes) in data/backups."""
    db = ROOT / "data" / "sparkhub.db"
    if not db.exists():
        return None
    dest = ROOT / "data" / "backups" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
    os.chmod(dest, 0o600)
    return dest


def restore_db(backup: Path) -> None:
    """Put a backup back as the database (the hub must be stopped); its write-ahead log goes with the old file."""
    db = ROOT / "data" / "sparkhub.db"
    mode = db.stat().st_mode & 0o777 if db.exists() else 0o600
    for extra in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        extra.unlink(missing_ok=True)
    shutil.copyfile(backup, db)
    os.chmod(db, mode)


def offer_rollback(ctx: Ctx, rb: dict, restarted: bool, why: str) -> None:
    """After a failed upgrade: back to the previous checkout; if the new hub already started (and may have migrated
    the database), also the database from before the upgrade, then the old release's update restarts the stack."""
    say(f"\n  The upgrade from {rb['from']} to {rb['to']} stopped: {why}")
    what = "its code and the database from before the upgrade" if restarted else "its code; nothing was restarted"
    if not yes(f"Go back to {rb['from']} ({what})?", True):
        say(f"  Staying on {rb['to']}. Fix the problem and run ./install.sh update, or go back by hand:\n"
            f"  git checkout {rb['ref'][:12]}" + (f" and restore {rb['backup']}" if restarted and rb.get("backup") else ""))
        return
    head(f"back to {rb['from']}")
    if restarted and rb.get("backup"):
        ctx.compose("stop", "hub", check=False)
        restore_db(Path(rb["backup"]))
        ok(f"database restored from {Path(rb['backup']).relative_to(ROOT)}")
    if rb.get("branch"):
        run(["git", "reset", "--keep", rb["ref"]])          # moves the branch back (the tree was clean)
    else:
        run(["git", "checkout", "--detach", rb["ref"]])
    env = {k: v for k, v in os.environ.items() if k != "SPARKHUB_ROLLBACK"}
    # the old release's installer: update restarts the stack on the old code; render only rewrites the config
    os.execve(sys.executable, [sys.executable, "-I", str(ROOT / "ops" / "install" / "installer.py"),
                               "update" if restarted else "render"], env)


def upgrade(target: str | None) -> None:
    """Move this checkout to a release tag, then hand over to that release's installer (`update`)."""
    if not (ROOT / ".git").exists():
        raise Abort("not a git checkout: unpack the new release over this one, then run ./install.sh update")
    if dirty := out(["git", "status", "--porcelain", "--untracked-files=no"]):
        raise Abort("tracked files were changed here; keep them (git stash) or drop them (git checkout -- .) "
                    "first:\n" + dirty)
    head("releases")
    run(["git", "fetch", "--tags", "--force", "origin"])
    tags = sorted((t for t in out(["git", "tag", "-l", "v*"]).split() if vtuple(t)), key=vtuple)
    if not tags:
        raise Abort("origin has no release tags (vX.Y.Z)")
    want = ("v" + target.lstrip("v")) if target else tags[-1]
    if want not in tags:
        raise Abort(f"no release {want}; releases: {', '.join(tags[-8:])}")
    cur = version()
    if vtuple(cur) == vtuple(want):
        ok(f"this install is {want}, the newest release" if want == tags[-1] else f"this install is {want}")
        return
    newer = vtuple(cur) is None or vtuple(want) > vtuple(cur)
    say(f"  {cur} -> {want}" + ("" if newer else "   (an older release)"))
    notes = changelog_between(out(["git", "show", f"{want}:CHANGELOG.md"]), cur, want)
    if notes and newer:
        say("\n" + "\n".join("  " + line for line in notes.splitlines()) + "\n")
    if not newer:
        say("  Going back does not undo database changes; if the older hub refuses the database, restore the\n"
            "  backup made before the upgrade (data/backups/pre-upgrade-*.db) with the stack stopped.")
    if rid := running_benchmark():
        raise Abort(f"benchmark #{rid} is running; upgrade after it ends")
    if not yes(f"Upgrade to {want}? The hub restarts, and a model this release changed reloads (~4-5 min)", newer):
        raise Abort("upgrade cancelled")
    if dest := backup_db(f"pre-upgrade-{cur}-to-{want.lstrip('v')}"):
        ok(f"database backed up to {dest.relative_to(ROOT)}")
    branch = out(["git", "symbolic-ref", "-q", "--short", "HEAD"])
    prev = out(["git", "rev-parse", "HEAD"])
    if branch and newer:
        r = run(["git", "merge", "--ff-only", want], check=False)
        if r.returncode:
            raise Abort(f"{branch} has commits that are not in {want}; check out the release instead: "
                        f"git checkout {want} && ./install.sh update")
    else:
        run(["git", "checkout", "--detach", want])
    say(f"\n  Now on {want}; its installer applies it.")
    # the new release's installer runs the update: its steps, pins and new questions' defaults; if it fails, it
    # offers to come back here (SPARKHUB_ROLLBACK)
    rollback = {"ref": prev, "branch": branch, "from": cur, "to": want, "backup": str(dest) if dest else ""}
    os.execve(sys.executable, [sys.executable, "-I", str(ROOT / "ops" / "install" / "installer.py"), "update"],
              {**os.environ, "SPARKHUB_ROLLBACK": json.dumps(rollback)})


def uninstall(ctx: Ctx, purge: bool) -> None:
    head("stop and remove the containers")
    ctx.compose("down", "--remove-orphans", check=False)
    models = out(["docker", "ps", "-a", "--filter", "label=sparkhub.mode", "--format",
                  "{{.Names}} {{.Label \"sparkhub.instance\"}}"])
    for line in models.splitlines():
        name, _, inst = line.partition(" ")
        if (inst or "sparkhub") == ctx.conf["INSTANCE"]:
            run(["docker", "rm", "-f", name], check=False)
    say("  Kept: data/ (database, keys, Grafana, backups), the model weights, and the images.")
    if purge:
        say(f"  --purge deletes {ROOT / 'data'}: users, API keys, settings, history, backups, certificates.")
        if ask(f"Type the instance name ({ctx.conf['INSTANCE']}) to delete it", "") == ctx.conf["INSTANCE"]:
            r = run(["rm", "-rf", str(ROOT / "data")], check=False)
            if r.returncode:
                sudo(["rm", "-rf", str(ROOT / "data")], "some files in data/ are owned by root (Grafana, ACME)", ctx)
    # host pieces are machine-wide: only offered when this install's sparkhub.conf enabled them
    host = [("SSH_GEO", "sparkhub-ssh-geo.timer", "systemctl disable --now sparkhub-ssh-geo.timer "
             "sparkhub-ssh-geo.service && nft delete table inet ssh_geo", "remove the SSH allowlist"),
            ("PORT_GUARD", "sparkhub-port-guard.service", "systemctl disable --now sparkhub-port-guard && "
             "nft delete table inet port_guard", "remove the port guard"),
            ("TAILSCALE_CERT_ISSUED", "sparkhub-tailscale-cert.timer", "systemctl disable --now "
             "sparkhub-tailscale-cert.timer && rm -f /etc/systemd/system/sparkhub-tailscale-cert.* "
             "/usr/local/sbin/sparkhub-tailscale-cert /etc/default/sparkhub-tailscale-cert && systemctl daemon-reload",
             "remove the Tailscale certificate renewal")]
    for key, unit, cmd, why in host:
        if ctx.on(key) and _systemd(unit) == "enabled":
            sudo(cmd, why, ctx)
    say("  Not touched: certbot and its certificates, Tailscale, avahi, the boot target, Docker.")


# ----------------------------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="install.sh", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="install",
                    choices=("install", "configure", "render", "models", "tls", "selftest", "update", "upgrade", "uninstall"))
    ap.add_argument("release", nargs="?", help="upgrade: the release to move to (default: the newest)")
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--verify", action="store_true", help="models: hash every file against the pinned manifest")
    ap.add_argument("--switch", action="store_true", help="selftest: also switch modes and back")
    ap.add_argument("--purge", action="store_true", help="uninstall: also delete data/")
    a = ap.parse_args(argv)
    os.chdir(ROOT)
    try:
        if a.command == "configure" or (not CONF.exists() and not a.check):
            conf = configure(load_conf() if CONF.exists() else {})
            save_conf(conf)
            ok(f"saved {CONF}")
            if a.command == "configure":
                return 0
        if not CONF.exists():
            bad("no sparkhub.conf: run ./install.sh first")
            return 1
        conf = {**detect_defaults_only(), **load_conf()}
        conf["ACCESS"] = access(conf)            # written on the next save; older files lack it
        ctx = Ctx(conf, check=a.check, verify=a.verify)
        if a.command == "render":
            do_render(ctx)
            return 0
        if a.command == "models":
            return 0 if run_steps(ctx, ["models"]) else 1
        if a.command == "tls":
            if ctx.conf["TLS"] == "letsencrypt":
                do_letsencrypt(ctx)
            return 0 if run_steps(ctx, ["local certificate", "Let's Encrypt", "Tailscale certificate"]) else 1
        if a.command == "selftest":
            return 0 if selftest(ctx, a.switch) else 1
        if a.command == "upgrade":
            upgrade(a.release)
            return 0
        if a.command == "update":
            update(ctx)
            return 0 if run_steps(ctx) else 1   # then whatever else is missing (e.g. a newly enabled piece's sign-in)
        if a.command == "uninstall":
            uninstall(ctx, a.purge)
            return 0
        good = run_steps(ctx)
        if a.check:
            say("\n" + (c("32", "Everything is in place.") if good else c("33", "Some steps are not done (todo).")))
            return 0 if good else 1
        if good:
            selftest(ctx)
        summary(ctx)
        return 0 if good else 1
    except Abort as e:
        bad(str(e))
        return 2
    except subprocess.CalledProcessError as e:
        bad(f"command failed ({e.returncode}): {e.cmd if isinstance(e.cmd, str) else shlex.join(e.cmd)}")
        return 2
    except KeyboardInterrupt:
        say("\n  interrupted; re-run to continue where it stopped")
        return 130


def detect_defaults_only() -> dict[str, str]:
    """Defaults for keys a sparkhub.conf from an older installer lacks (no questions, no network)."""
    return {"AUTOSTART": "1", "NVIDIA_DASHBOARD": "0", "NVIDIA_DASHBOARD_PORT": "11000", "ARCANE": "0",
            "ARCANE_IMAGE": "arcane-sparkhub:runtime-basepath", "ARCANE_SOURCE": "", "ARCANE_REF": "",
            "ARCANE_PORT": "3552", "ARCANE_SUBNET": "172.31.250.0/24", "ISSUER_ADDR": "127.0.0.1",
            "CHATUI": "0", "CHATUI_IMAGE": CHATUI_DEFAULT_IMAGE, "CHATUI_PORT": "3090",
            "CHATUI_SUBNET": "172.31.251.0/24", "EMBEDDINGS": "0", "EMBED_PORT": "18310", "RERANK_PORT": "18311",
            "BENCH": "0", "LETSENCRYPT_ISSUED": "0", "TAILSCALE_CERT_ISSUED": "0", "TLS_CHALLENGE": "http", "SSH_GEO": "0", "PORT_GUARD": "0",
            "PORT_GUARD_PORTS": "8000", "HEADLESS": "0", "FLASH_LICENSE": "", "SSH_GEO_COUNTRY": "", "LAN_CIDR": ""}


def summary(ctx: Ctx) -> None:
    v = derive(ctx.conf)
    url = v["PUBLIC_URL"]
    head("Summary")
    say(f"  Dashboard   {url}/   (password + authenticator app)")
    say(f"  API         {url}/v1   (OpenAI-compatible; create keys on the Keys page)")
    say(f"  Grafana     {url}/grafana/" + (f"   Arcane {url}/arcane/" if ctx.on("ARCANE") else ""))
    if ctx.on("CHATUI"):
        say(f"  Chat        {url}/chat/   (hub users; on/off under Settings → Chat)")
    connect_card(ctx)
    say(f"\n  SparkHub {version()}. Check any time: ./install.sh --check    Upgrade: ./install.sh upgrade")


def connect_card(ctx: Ctx) -> None:
    """How users reach the hub, for the access mode: what each device needs."""
    v = derive(ctx.conf)
    url, mode, host = v["PUBLIC_URL"], access(ctx.conf), ctx.conf["PUBLIC_HOST"]
    port = v["HTTPS_PORT_SUFFIX"]
    others = [n for n in ctx.conf["EXTRA_NAMES"].split() if "." in n]
    ca = ROOT / "data" / "tls" / "ca.crt"
    fp = out(["openssl", "x509", "-in", str(ca), "-noout", "-fingerprint", "-sha256"]).split("=", 1)[-1]
    http80 = ctx.conf["HTTP_PORT"] not in FALSY
    def trust_ca(where: str, name: str = host) -> None:
        ca_url = f"http://{name}{'' if ctx.conf['HTTP_PORT'] == '80' else ':' + ctx.conf['HTTP_PORT']}/ca.crt"
        say(f"    {where}, each device trusts the SparkHub certificate once:")
        say(f"      get it: {ca_url if http80 and v['CA_HTTP'] else str(ca)}  (also on the dashboard's Connect page)")
        if fp:
            say(f"      check its SHA-256 fingerprint before trusting it: {fp}")
        say("      Windows: double-click → Install → Current User → Trusted Root Certification Authorities")
        say("      macOS: double-click → Keychain Access → the certificate → Trust → Always Trust")
        say("      iPhone/iPad: open it → Settings → Install; then General → About → Certificate Trust Settings → on")
        say("      Android: Settings → Security → Encryption & credentials → Install a certificate → CA certificate")
        say("      Linux: sudo cp ca.crt /usr/local/share/ca-certificates/sparkhub.crt && sudo update-ca-certificates")
        say("      Firefox (own store): Settings → Certificates → Authorities → Import → trust to identify websites")
        say(f"      Only ca.crt leaves this machine; {ca.parent / 'ca.key'} next to it is the signing secret.")

    head("How users connect")
    if mode == "local":
        say(f"  On the LAN, open {url}/")
        trust_ca("Before the first visit")
        if host.endswith(".local"):
            say(f"    {host} resolves by itself on Windows 10+, macOS, iOS, most Linux desktops and recent Android.")
            say(f"    Where it does not, add a DNS entry on your router: {host.removesuffix('.local')} → "
                f"{ctx.conf['ISSUER_ADDR']}")
        say("  Not on the same network (e.g. a cloud machine): tunnel over SSH and point the name at it.")
        say(f"    copy the certificate: scp {getpass.getuser()}@<this machine>:{ca} .")
        say(f"    ssh -L 443:{ctx.conf['ISSUER_ADDR']}:{ctx.conf['HTTPS_PORT']} {getpass.getuser()}@<this machine>"
            "   (local port 443 needs an admin shell)")
        say(f"    hosts file (Windows: C:\\Windows\\System32\\drivers\\etc\\hosts, else /etc/hosts): 127.0.0.1 {host}")
        say(f"    then open {url}/ ; port 443 matters: chat and Grafana sign-in return to exactly {url}.")
    elif mode == "tailscale":
        say("  Each device installs Tailscale (https://tailscale.com/download) and joins your tailnet")
        say("  (same account, or invite people from the admin console: Users → Invite).")
        say(f"  Then open {url}/" + ("  — the certificate is publicly trusted, nothing else to install."
                                     if v["TRUSTED"] else ""))
        if not v["TRUSTED"]:
            trust_ca("Also")
        lan = [n for n in others if n.endswith(".local")]
        if lan_addr(ctx.conf):
            say(f"  LAN devices without Tailscale: with a DNS entry on your router, {host} → {lan_addr(ctx.conf)},")
            say(f"  they open {url}/ too and get everything" + (" with nothing to install." if v["TRUSTED"] else "."))
        if lan:
            say(f"  Without that entry: https://{lan[0]}{port}/ for the dashboard and the API only")
            say("  (chat, Arcane and Grafana need the tailnet name).")
            trust_ca("There", lan[0])
    else:
        say(f"  From anywhere: {url}/")
        say(f"  Your router forwards TCP 443{' and 80' if ctx.conf['TLS_CHALLENGE'] == 'http' else ''} to "
            f"{ctx.conf['ISSUER_ADDR']}; check it from outside: open {url}/ on a phone with Wi-Fi off.")
        say("  On the LAN the same address works when your router supports hairpin NAT (most do); if it does not, add")
        say(f"  a DNS entry on the router: {host} → {ctx.conf['ISSUER_ADDR']}. Elsewhere it goes over the internet.")
        if others:
            say(f"  The local names ({', '.join(others)}) serve the dashboard and the API with the SparkHub")
            say("  certificate (dashboard → Connect explains it); chat, Arcane and Grafana use the domain only.")

if __name__ == "__main__":
    sys.exit(main())
