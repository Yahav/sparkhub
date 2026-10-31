"""Starting and stopping the model containers through the Docker Engine API (unix socket).

Model containers are created with restart policy "no" and their port published on 127.0.0.1 only.
A Custom recipe's container instead sits on an internal Docker network with no route out and no published port;
the hub (host network) reaches it at its container IP.
After a model stops, a one-second privileged helper container runs drop_caches so the memory comes back.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shlex
import struct
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("sparkhub.runtime")
LABEL = "sparkhub.mode"
PIN_LABEL = "sparkhub.pin"          # the preset's pin (Preset.pin) the container was started from
EMBED_LABEL = "sparkhub.embed"         # the embedding service's containers (embeddings.py); never stopped as strays
INSTANCE_LABEL = "sparkhub.instance"   # containers without it belong to the default instance (made before 2026-10-24)


def meminfo() -> dict[str, float]:
    """Host memory in GiB. /proc/meminfo inside a container is the host's."""
    vals: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, rest = line.split(":", 1)
            vals[k] = int(rest.split()[0])
    except OSError:
        return {}
    total = vals.get("MemTotal", 0) / 1048576
    avail = vals.get("MemAvailable", 0) / 1048576
    return {"total_gib": round(total, 1), "available_gib": round(avail, 1), "used_gib": round(total - avail, 1),
            "cached_gib": round(vals.get("Cached", 0) / 1048576, 1),
            "swap_used_gib": round((vals.get("SwapTotal", 0) - vals.get("SwapFree", 0)) / 1048576, 1)}


def cpu_times() -> tuple[int, int] | None:
    """(busy, total) jiffies of all CPUs since boot. /proc/stat inside a container is the host's."""
    try:
        f = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    except (OSError, ValueError):
        return None
    idle = f[3] + (f[4] if len(f) > 4 else 0)       # idle + iowait
    total = sum(f[:8])                               # guest time is already counted in user/nice
    return total - idle, total


def loadavg() -> float | None:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


DISK_RE = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)$")   # whole disks, not partitions


def disk_bytes() -> tuple[int, int] | None:
    """(read, written) bytes of all disks since boot. /proc/diskstats inside a container is the host's."""
    try:
        rd = wr = 0
        for line in Path("/proc/diskstats").read_text().splitlines():
            f = line.split()
            if len(f) > 9 and DISK_RE.match(f[2]):
                rd += int(f[5]) * 512
                wr += int(f[9]) * 512
        return rd, wr
    except (OSError, ValueError):
        return None


def _health(status: str) -> str | None:
    for h in ("unhealthy", "healthy", "health: starting"):
        if f"({h})" in status:
            return h.replace("health: ", "")
    return None


def _size_bytes(s: str | None) -> int | None:
    if not s:
        return None
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    s = s.strip().lower()
    if s[-1] in units:
        return int(float(s[:-1]) * units[s[-1]])
    return int(s)


def demux(data: bytes) -> tuple[bytes, bytes]:
    """Docker's multiplexed stream (8-byte frame headers) to (stdout, stderr), as bytes."""
    out: dict[int, list[bytes]] = {1: [], 2: []}
    i = 0
    while i + 8 <= len(data):
        size = struct.unpack(">I", data[i + 4:i + 8])[0]
        out.setdefault(data[i], []).append(data[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out[1]), b"".join(out[2])


def upload_tar(files: list[dict[str, Any]]) -> bytes:
    """A tar of mod files under sparkhub-mods/ (extracted at /tmp in the container; recipes.MOD_UPLOAD_DIR)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        dirs: set[str] = set()
        for f in files:
            parts = ["sparkhub-mods", *f["path"].split("/")]
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    info = tarfile.TarInfo(d)
                    info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
                    tar.addfile(info)
            data = f["data"].encode()
            info = tarfile.TarInfo("/".join(parts))
            info.size, info.mode, info.mtime = len(data), 0o755 if f.get("exec") else 0o644, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def demux_logs(data: bytes) -> str:
    """Docker's multiplexed log stream (8-byte frame headers) to text; raw text if it isn't multiplexed."""
    out = []
    i = 0
    while i + 8 <= len(data) and data[i] in (0, 1, 2) and data[i + 1:i + 4] == b"\0\0\0":
        size = struct.unpack(">I", data[i + 4:i + 8])[0]
        out.append(data[i + 8:i + 8 + size])
        i += 8 + size
    if i == 0:
        return data.decode(errors="replace")
    return b"".join(out).decode(errors="replace")


class RuntimeError_(Exception):
    pass


class Runtime:
    """Interface; DockerRuntime is the real one, tests use a fake."""
    async def start(self, spec: dict[str, Any]) -> None: ...
    async def stop(self, mode: str, container: str) -> None: ...
    async def state(self, container: str) -> dict[str, Any] | None: ...
    async def logs(self, mode: str, container: str, tail: int = 300) -> str: ...
    async def free_memory(self) -> dict[str, Any]: ...
    async def running(self) -> dict[str, str]: ...      # mode -> container name, for every sparkhub model container
    async def inspect(self, container: str) -> dict[str, Any] | None: ...
    async def image_exists(self, image: str) -> bool:
        return True
    async def start_container(self, container: str) -> None: ...   # an existing container (chat-ui), not a model
    async def stop_container(self, container: str, timeout: int = 20) -> None: ...
    async def start_container_body(self, name: str, body: dict[str, Any]) -> None: ...   # create + start (helpers)
    async def remove_container(self, name: str) -> None: ...

    async def components(self) -> list[dict[str, Any]]:
        """This install's containers (the compose services and the model), for the overview."""
        return []

    async def address(self, container: str) -> str | None:
        """host:port of a container on the isolated network; None for a published port."""
        return None

    async def pull_image(self, ref: str, progress=None) -> None: ...
    async def image_digests(self, ref: str) -> list[str]:
        return []

    async def run_fetch(self, files: list[dict[str, Any]], hf_cache: str, progress=None) -> dict[str, Any]:
        """Download and verify model files into the host's HF cache (fetch_weights.py in a helper container)."""
        return {"ok": True}

    async def remove_volume(self, name: str) -> None: ...

    async def run_once(self, spec: dict[str, Any], timeout: float = 900) -> dict[str, Any]:
        """Run a container to completion (the mods test run): {exit_code, log}. Removed afterwards."""
        return {"exit_code": 0, "log": ""}

    async def close(self) -> None: ...


DOCKER_API = (1, 43)    # the API version this code was written against


def docker_base_url(socket: str) -> str:
    """Docker's API base URL at the version this code was written for, raised to the daemon's minimum when the
    daemon refuses older clients (Ubuntu's docker.io 29 accepts 1.44 and up)."""
    want = DOCKER_API
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=socket), timeout=5) as c:
            v = c.get("http://docker/version").json()
        lo, hi = (tuple(int(x) for x in v[k].split(".")) for k in ("MinAPIVersion", "ApiVersion"))
        want = max(lo, min(hi, want))
    except Exception:
        pass
    return f"http://docker/v{want[0]}.{want[1]}"


class DockerRuntime(Runtime):
    def __init__(self, socket: str, helper_image: str, logs_dir: Path, instance: str = "sparkhub"):
        self.instance = instance
        self.client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket), base_url=docker_base_url(socket),
                                        timeout=httpx.Timeout(60.0))
        self.helper_image = helper_image
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    async def _req(self, method: str, url: str, ok: tuple[int, ...] = (200, 201, 204), **kw) -> httpx.Response:
        r = await self.client.request(method, url, **kw)
        if r.status_code not in ok:
            raise RuntimeError_(f"docker {method} {url}: {r.status_code} {r.text[:300]}")
        return r

    @property
    def isolated_net(self) -> str:
        return f"{self.instance}-isolated"

    async def ensure_isolated_net(self) -> None:
        r = await self.client.get(f"/networks/{self.isolated_net}")
        if r.status_code == 200:
            if not r.json().get("Internal"):
                raise RuntimeError_(f"network {self.isolated_net} exists but is not internal; remove it")
            return
        await self._req("POST", "/networks/create", json={
            "Name": self.isolated_net, "Driver": "bridge", "Internal": True,
            "Labels": {INSTANCE_LABEL: self.instance, "sparkhub.purpose": "isolated model containers"}})

    def _create_body(self, spec: dict[str, Any]) -> dict[str, Any]:
        cport = f"{spec['container_port']}/tcp"
        host: dict[str, Any] = {
            "Binds": spec["binds"],
            "PortBindings": {cport: [{"HostIp": "127.0.0.1", "HostPort": str(spec["host_port"])}]},
            "RestartPolicy": {"Name": "no"},
        }
        if spec.get("network") == "internal":
            # no route out and nothing published; the hub connects to the container IP
            host["NetworkMode"] = self.isolated_net
            host.pop("PortBindings")
        if spec.get("ipc"):
            host["IpcMode"] = spec["ipc"]
        if spec.get("shm_size"):
            host["ShmSize"] = _size_bytes(spec["shm_size"])
        if spec.get("ulimits"):
            # e.g. memlock -1: b12x mlock()s its weight storage; -1 = unlimited
            host["Ulimits"] = [{"Name": k, "Soft": v, "Hard": v} for k, v in spec["ulimits"].items()]
        if spec.get("gpus") == "all":
            host["DeviceRequests"] = [{"Driver": "", "Count": -1, "DeviceIDs": None, "Capabilities": [["gpu"]],
                                       "Options": {}}]
        return {
            "Image": spec["image"],
            "Cmd": spec["args"],
            "Env": [f"{k}={v}" for k, v in spec["env"].items()],
            "ExposedPorts": {cport: {}},
            "Labels": {**(spec.get("labels") or {LABEL: spec["mode"]}), INSTANCE_LABEL: self.instance,
                       **({PIN_LABEL: spec["pin"]} if spec.get("pin") else {})},
            "HostConfig": host,
            **({"User": spec["user"]} if spec.get("user") else {}),
            # a recipe's executor_config.entrypoint: "" clears the image's (e.g. ["vllm", "serve"])
            **({"Entrypoint": shlex.split(spec["entrypoint"]) or [""]} if "entrypoint" in spec else {}),
        }

    async def start(self, spec: dict[str, Any]) -> None:
        name = spec["container"]
        if spec.get("network") == "internal":
            await self.ensure_isolated_net()
        await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404))
        await self._req("POST", "/containers/create", params={"name": name}, json=self._create_body(spec))
        try:
            if spec.get("uploads"):
                # a recipe's mods: the approved files, copied in before the start (sparkrun uses docker cp)
                await self._req("PUT", f"/containers/{name}/archive", params={"path": "/tmp"},
                                content=upload_tar(spec["uploads"]), headers={"Content-Type": "application/x-tar"})
            await self._req("POST", f"/containers/{name}/start")
        except Exception:
            await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404))
            raise

    async def _save_logs(self, mode: str, container: str) -> None:
        r = await self.client.get(f"/containers/{container}/logs",
                                  params={"stdout": 1, "stderr": 1, "tail": 3000, "timestamps": 1})
        if r.status_code == 200:
            ts = time.strftime("%Y%m%d-%H%M%S")
            (self.logs_dir / f"{mode}-{ts}.log").write_text(demux_logs(r.content))
            old = sorted(self.logs_dir.glob(f"{mode}-*.log"))[:-20]
            for p in old:
                p.unlink(missing_ok=True)

    async def stop(self, mode: str, container: str) -> None:
        try:
            await self._save_logs(mode, container)
        except Exception as e:  # logs are nice to have, the stop is not optional
            log.warning("saving logs of %s failed: %s", container, e)
        await self._req("POST", f"/containers/{container}/stop", params={"t": 30}, ok=(204, 304, 404),
                        timeout=90)
        await self._req("DELETE", f"/containers/{container}", params={"force": "true"}, ok=(204, 404))

    async def inspect(self, container: str) -> dict[str, Any] | None:
        r = await self.client.get(f"/containers/{container}/json")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def image_exists(self, image: str) -> bool:
        r = await self.client.get(f"/images/{image}/json")
        return r.status_code == 200

    async def image_digests(self, ref: str) -> list[str]:
        r = await self.client.get(f"/images/{ref}/json")
        return list(r.json().get("RepoDigests") or []) if r.status_code == 200 else []

    async def pull_image(self, ref: str, progress=None) -> None:
        """Pull by reference (name@sha256:… for a recipe), reporting (done, total) bytes as layers arrive."""
        layers: dict[str, tuple[int, int]] = {}
        async with self.client.stream("POST", "/images/create", params={"fromImage": ref},
                                      timeout=httpx.Timeout(60.0, read=1800.0)) as r:
            if r.status_code != 200:
                raise RuntimeError_(f"pull {ref}: {r.status_code} {(await r.aread())[:300]!r}")
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise RuntimeError_(f"pull {ref}: {ev['error']}")
                d = ev.get("progressDetail") or {}
                if ev.get("id") and d.get("total") and ev.get("status") == "Downloading":
                    layers[ev["id"]] = (d.get("current", 0), d["total"])
                elif ev.get("id") and ev.get("status") in ("Download complete", "Pull complete", "Already exists") \
                        and ev["id"] in layers:
                    layers[ev["id"]] = (layers[ev["id"]][1], layers[ev["id"]][1])
                if progress:
                    progress(sum(c for c, _ in layers.values()), sum(t for _, t in layers.values()))

    async def address(self, container: str) -> str | None:
        info = await self.inspect(container)
        if not info:
            return None
        net = (info.get("NetworkSettings", {}).get("Networks") or {}).get(self.isolated_net)
        if not net or not net.get("IPAddress"):
            return None
        port = next(iter(info.get("Config", {}).get("ExposedPorts") or {"8000/tcp": {}})).split("/")[0]
        return f"{net['IPAddress']}:{port}"

    async def run_fetch(self, files: list[dict[str, Any]], hf_cache: str, progress=None) -> dict[str, Any]:
        name = f"{self.instance}-fetch"
        body = {"Image": self.helper_image, "User": "1000:1000",
                "Cmd": ["python", "-m", "sparkhub.fetch_weights"],
                "Env": [f"SPARKHUB_FETCH={json.dumps(files, separators=(',', ':'))}", "SPARKHUB_FETCH_ROOT=/hf"],
                "Labels": {"sparkhub.helper": "fetch", INSTANCE_LABEL: self.instance},
                "HostConfig": {"Binds": [f"{hf_cache}:/hf"], "RestartPolicy": {"Name": "no"},
                               "SecurityOpt": ["no-new-privileges"], "CapDrop": ["ALL"]}}
        await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404))
        await self._req("POST", "/containers/create", params={"name": name}, json=body)
        last: dict[str, Any] = {}
        try:
            await self._req("POST", f"/containers/{name}/start")
            while True:
                st = await self.state(name)
                r = await self.client.get(f"/containers/{name}/logs", params={"stdout": 1, "stderr": 1, "tail": 20})
                for line in demux_logs(r.content).splitlines():
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        last = ev if (ev.get("ok") or ev.get("error")) else last
                        if ev.get("progress") and progress:
                            progress(ev["done"], ev["total"], ev.get("file", ""))
                if not st or not st["running"]:
                    break
                await asyncio.sleep(2)
            if not last:
                tail = demux_logs(r.content).strip().splitlines()[-3:]
                last = {"error": f"the download helper exited (code {st and st['exit_code']}): {' | '.join(tail)[:400]}"}
        finally:
            await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404))
        return last

    async def remove_volume(self, name: str) -> None:
        await self._req("DELETE", f"/volumes/{name}", ok=(204, 404, 409))

    async def run_once(self, spec: dict[str, Any], timeout: float = 900) -> dict[str, Any]:
        name = spec["container"]
        await self.start(spec)
        try:
            deadline = time.time() + timeout
            st = await self.state(name)
            while st and st["running"] and time.time() < deadline:
                await asyncio.sleep(2)
                st = await self.state(name)
            r = await self.client.get(f"/containers/{name}/logs", params={"stdout": 1, "stderr": 1, "tail": 60})
            log_text = demux_logs(r.content) if r.status_code == 200 else ""
            if st and st["running"]:
                return {"exit_code": -1, "log": log_text + f"\n(still running after {timeout:.0f} s; stopped)"}
            return {"exit_code": st["exit_code"] if st else -1, "log": log_text}
        finally:
            await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404))

    async def start_container(self, container: str) -> None:
        await self._req("POST", f"/containers/{container}/start", ok=(204, 304))

    async def stop_container(self, container: str, timeout: int = 20) -> None:
        # kept (not removed), so compose and its restart policy still know it
        await self._req("POST", f"/containers/{container}/stop", params={"t": timeout}, ok=(204, 304, 404),
                        timeout=timeout + 40)

    async def start_container_body(self, name: str, body: dict[str, Any]) -> None:
        """A helper container from a full create body (the benchmark runner), replacing one of the same name."""
        body = {**body, "Labels": {**body.get("Labels", {}), INSTANCE_LABEL: self.instance}}
        await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404))
        await self._req("POST", "/containers/create", params={"name": name}, json=body)
        await self._req("POST", f"/containers/{name}/start")

    async def remove_container(self, name: str) -> None:
        await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404))

    async def state(self, container: str) -> dict[str, Any] | None:
        info = await self.inspect(container)
        if info is None:
            return None
        st = info["State"]
        return {"status": st["Status"], "running": st["Running"], "exit_code": st["ExitCode"],
                "oom_killed": st["OOMKilled"], "started_at": st["StartedAt"]}

    async def logs(self, mode: str, container: str, tail: int = 300) -> str:
        r = await self.client.get(f"/containers/{container}/logs",
                                  params={"stdout": 1, "stderr": 1, "tail": tail, "timestamps": 1})
        if r.status_code == 200:
            return demux_logs(r.content)
        saved = sorted(self.logs_dir.glob(f"{mode}-*.log"))
        if saved:
            lines = saved[-1].read_text().splitlines()[-tail:]
            return f"(container not running; last saved log {saved[-1].name})\n" + "\n".join(lines)
        return "(no logs)"

    async def running(self) -> dict[str, str]:
        r = await self._req("GET", "/containers/json",
                            params={"all": "true", "filters": json.dumps({"label": [LABEL]})})
        out = {}
        for c in r.json():
            if c["Labels"].get(INSTANCE_LABEL, "sparkhub") != self.instance:
                continue      # another install's model (side-by-side): never adopt or stop it
            if c["State"] in ("running", "restarting", "created"):
                out[c["Labels"][LABEL]] = c["Names"][0].lstrip("/")
        return out

    async def components(self) -> list[dict[str, Any]]:
        r = await self._req("GET", "/containers/json", params={"all": "true"})
        out = []
        for c in r.json():
            lab = c.get("Labels") or {}
            mine = lab.get(INSTANCE_LABEL, "sparkhub") == self.instance
            model = LABEL in lab and mine
            embed = EMBED_LABEL in lab and mine
            if not model and not embed and lab.get("com.docker.compose.project") != self.instance:
                continue
            status = c.get("Status") or ""
            svc = lab.get(LABEL) if model else lab.get(EMBED_LABEL) if embed else lab.get("com.docker.compose.service", "")
            out.append({"name": c["Names"][0].lstrip("/"), "service": svc, "model": model, "embed": embed,
                        "state": c.get("State", ""),
                        "health": _health(status), "status": status})
        return out

    async def free_memory(self) -> dict[str, Any]:
        """Drop the page cache through a privileged helper container, then wait for available memory to settle."""
        before = meminfo().get("available_gib")
        name = f"sparkhub-dropcaches-{int(time.time())}"
        body = {"Image": self.helper_image, "User": "0", "Entrypoint": ["sh", "-c"],
                "Cmd": ["sync; echo 3 > /proc/sys/vm/drop_caches"],
                "Labels": {"sparkhub.helper": "drop_caches"},
                "HostConfig": {"Privileged": True, "NetworkMode": "none", "RestartPolicy": {"Name": "no"}}}
        try:
            await self._req("POST", "/containers/create", params={"name": name}, json=body)
            await self._req("POST", f"/containers/{name}/start")
            r = await self._req("POST", f"/containers/{name}/wait", timeout=120)
            code = r.json().get("StatusCode")
            if code != 0:
                log.warning("drop_caches helper exited %s", code)
        finally:
            await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404))
        # wait until available memory stops rising (the old model's memory is returned gradually)
        last = meminfo().get("available_gib", 0)
        for _ in range(30):
            await asyncio.sleep(2)
            now = meminfo().get("available_gib", 0)
            if now - last < 0.3:
                break
            last = now
        return {"available_before_gib": before, "available_after_gib": meminfo().get("available_gib")}

    async def close(self) -> None:
        await self.client.aclose()
