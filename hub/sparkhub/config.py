"""Runtime configuration, from environment variables (set in compose.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path = Path("/srv/sparkhub/data")
    presets_dir: Path = Path("/srv/sparkhub/presets")
    # Host paths, used in the bind mounts of the model containers (the Docker daemon resolves them on the host).
    # compose.yaml (rendered by ./install.sh from sparkhub.conf) always sets them.
    host_presets_dir: str = ""
    host_hf_cache: str = ""
    host_models_dir: str = ""
    docker_socket: str = "/var/run/docker.sock"
    runtime: str = "docker"            # docker | fake (tests)
    upstream_host: str = "127.0.0.1"   # where the model containers' published ports are reached
    helper_image: str = "sparkhub-hub:dev"  # image for the drop_caches helper (the hub's own image)
    autostart: bool = True             # restore the last mode when the hub starts
    prometheus_url: str = "http://127.0.0.1:9090"
    gpu_collector: bool = True
    cookie_secure: bool = True
    clients_dir: Path = Path("/srv/sparkhub/clients")
    public_host: str = ""                      # the name clients use (installer: PUBLIC_HOST)
    public_cert_trusted: bool = True           # PUBLIC_HOST has a publicly trusted certificate (else the local CA's)
    tls_names: str = ""                        # names whose certificate the hub monitors, comma-separated
    tls_port: int = 443                        # nginx's HTTPS port on 127.0.0.1 (certificate checks)
    instance: str = "sparkhub"                 # names and labels the model containers; two installs never share them
    mode_ports: dict = field(default_factory=dict)   # mode -> host port, overriding the preset (side-by-side installs)
    modes_enabled: tuple = ("fast", "quality")       # modes installed on this machine (installer: MODES)
    check_install: bool = False                # verify image + weights before a load (on in the container, off in tests)
    oidc_issuer_url: str = ""                  # OIDC provider for Arcane; default https://<public_host>/oidc
    chat_container: str = ""                   # chat-ui's container (installer: CHATUI); "" = not installed
    embeddings: bool = False                   # the embedding service is installed (installer: EMBEDDINGS)
    arcane: bool = True                        # Arcane is installed at /arcane/ (installer: ARCANE); header link
    nvidia_dashboard: bool = True              # the DGX Dashboard is proxied at /nvidia/ (installer: NVIDIA_DASHBOARD)
    bench: bool = False                        # the benchmark runner is installed (installer: BENCH)
    bench_image: str = "sparkhub-bench:dev"
    host_data_dir: str = ""                    # data/ as the host sees it (bind mounts for the benchmark runner)
    gateway_addr: str = "127.0.0.1"            # the address containers reach nginx on (installer: ISSUER_ADDR)
    docker_socket_host: str = "/var/run/docker.sock"
    version: str = "dev"                       # the release this hub was built from (VERSION, via the installer)
    update_repo: str = "Yahav/sparkhub"        # GitHub owner/name whose latest release the update check reads
    embed_ports: dict = field(default_factory=dict)  # embed / rerank -> host port, overriding presets/embed.json
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sparkhub.db"

    def tls_check_names(self) -> list[tuple[str, str | None]]:
        """(name, CA file): a trusted public name verifies against the system CAs, others against the local CA."""
        local_ca = self.data_dir / "tls" / "ca.crt"
        out = []
        for n in (x.strip() for x in self.tls_names.split(",") if x.strip()):
            trusted = n == self.public_host and self.public_cert_trusted
            out.append((n, None if trusted or not local_ca.exists() else str(local_ca)))
        return out

    @property
    def oidc_issuer(self) -> str:
        return self.oidc_issuer_url or f"https://{self.public_host}/oidc"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "model-logs"


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_config() -> Config:
    e = os.environ
    c = Config()
    c.data_dir = Path(e.get("SPARKHUB_DATA_DIR", c.data_dir))
    c.presets_dir = Path(e.get("SPARKHUB_PRESETS_DIR", c.presets_dir))
    c.host_presets_dir = e.get("SPARKHUB_HOST_PRESETS_DIR", c.host_presets_dir)
    c.host_hf_cache = e.get("SPARKHUB_HOST_HF_CACHE", c.host_hf_cache)
    c.host_models_dir = e.get("SPARKHUB_HOST_MODELS_DIR", c.host_models_dir)
    c.docker_socket = e.get("SPARKHUB_DOCKER_SOCKET", c.docker_socket)
    c.runtime = e.get("SPARKHUB_RUNTIME", c.runtime)
    c.upstream_host = e.get("SPARKHUB_UPSTREAM_HOST", c.upstream_host)
    c.helper_image = e.get("SPARKHUB_HELPER_IMAGE", c.helper_image)
    c.autostart = _bool(e.get("SPARKHUB_AUTOSTART"), c.autostart)
    c.prometheus_url = e.get("SPARKHUB_PROMETHEUS_URL", c.prometheus_url)
    c.gpu_collector = _bool(e.get("SPARKHUB_GPU_COLLECTOR"), c.gpu_collector)
    c.cookie_secure = _bool(e.get("SPARKHUB_COOKIE_SECURE"), c.cookie_secure)
    c.clients_dir = Path(e.get("SPARKHUB_CLIENTS_DIR", c.clients_dir))
    c.public_host = e.get("SPARKHUB_PUBLIC_HOST", c.public_host)
    c.public_cert_trusted = _bool(e.get("SPARKHUB_PUBLIC_CERT_TRUSTED"), c.public_cert_trusted)
    c.tls_names = e.get("SPARKHUB_TLS_NAMES", c.tls_names)
    c.oidc_issuer_url = e.get("SPARKHUB_OIDC_ISSUER", c.oidc_issuer_url)
    c.tls_port = int(e.get("SPARKHUB_TLS_PORT", c.tls_port))
    c.instance = e.get("SPARKHUB_INSTANCE", c.instance)
    c.chat_container = e.get("SPARKHUB_CHAT_CONTAINER", c.chat_container)
    for m in ("fast", "quality"):
        if e.get(f"SPARKHUB_PORT_{m.upper()}"):
            c.mode_ports[m] = int(e[f"SPARKHUB_PORT_{m.upper()}"])
    c.embeddings = _bool(e.get("SPARKHUB_EMBEDDINGS"), c.embeddings)
    c.arcane = _bool(e.get("SPARKHUB_ARCANE"), c.arcane)
    c.nvidia_dashboard = _bool(e.get("SPARKHUB_NVIDIA_DASHBOARD"), c.nvidia_dashboard)
    c.bench = _bool(e.get("SPARKHUB_BENCH"), c.bench)
    c.bench_image = e.get("SPARKHUB_BENCH_IMAGE", c.bench_image)
    c.host_data_dir = e.get("SPARKHUB_HOST_DATA_DIR", c.host_data_dir) or str(c.data_dir)
    c.gateway_addr = e.get("SPARKHUB_GATEWAY_ADDR", c.gateway_addr)
    c.version = e.get("SPARKHUB_VERSION") or c.version
    c.update_repo = e.get("SPARKHUB_UPDATE_REPO") or c.update_repo
    for svc in ("embed", "rerank"):
        if e.get(f"SPARKHUB_PORT_{svc.upper()}"):
            c.embed_ports[svc] = int(e[f"SPARKHUB_PORT_{svc.upper()}"])
    if e.get("SPARKHUB_MODES"):
        c.modes_enabled = tuple(m for m in e["SPARKHUB_MODES"].replace(",", " ").split() if m in ("fast", "quality"))
    c.check_install = _bool(e.get("SPARKHUB_CHECK_INSTALL"), True)
    return c
