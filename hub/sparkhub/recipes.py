"""sparkrun recipes → a Custom mode spec.

A recipe is YAML (sparkrun `recipe_version` 1 or 2): model, container image, defaults, env and a shell `command`
with `{placeholders}`. This module reads it without any sparkrun code and translates it into the same kind of
spec as presets/<mode>.json, with sparkrun's conventions (HF cache at /cache/huggingface, runtime cache at
/cache/runtime, memlock/stack ulimits, --ipc=host). It also gathers the facts the review screen shows: the image
digest and its build, the model revision, files and license, flags that run downloaded code, a memory estimate.

Recipes may name sparkrun mods: directories with a run.sh that patch the container before the
server starts. They are resolved like sparkrun does, pinned to a commit, stored with the recipe and scanned; a mod
that downloads anything at launch is rejected, since the container has no network.

Nothing here runs a recipe. The network lookups (GitHub, Hugging Face, the image registry) read metadata only.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import yaml

from .presets import SETTING_RULES, validate_settings

HF = "https://huggingface.co"
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
UA = {"User-Agent": "sparkhub-recipes"}

# settings a recipe's defaults may expose as editable hub settings (same names in sparkrun and SETTING_RULES)
EDITABLE = ("max_model_len", "max_num_seqs", "gpu_memory_utilization", "kv_cache_dtype", "tool_call_parser")
GATEWAY_SETTINGS = ("default_thinking", "default_max_tokens")   # applied by the hub, valid for any model
CONTAINER_PORT = 8000
SHA40 = re.compile(r"^[0-9a-f]{40}$")
HF_SNAPSHOT = re.compile(r"models--([\w.\-]+)--([\w.\-]+)/snapshots/([0-9a-f]{40})")
VLLM_START = re.compile(r"^(exec\s+)?(vllm\s+serve|python3?\s+-m\s+vllm\.entrypoints\.openai\.api_server)\b")
WEIGHT_PICKLES = (".bin", ".pt", ".pth", ".pkl", ".ckpt", ".pickle")
# command lines that fetch or run something beyond the server itself
RISKY = [
    (re.compile(r"\b(curl|wget)\b"), "downloads with curl/wget"),
    (re.compile(r"\b(pip|uv)\s+(install|pip)\b"), "installs Python packages"),
    (re.compile(r"\bgit\s+clone\b"), "clones a git repository"),
    (re.compile(r"snapshot_download|hf_hub_download|huggingface-cli\s+download|\bhf\s+download"),
     "downloads from Hugging Face"),
    (re.compile(r"HF_HUB_OFFLINE=0"), "turns Hugging Face offline mode off"),
    (re.compile(r"\bpython3?\s+-c\b"), "runs inline Python"),
    (re.compile(r"\b(bash|sh)\s+-c\b|\beval\b|\bsource\b|^\.\s"), "runs a nested shell"),
    (re.compile(r"\bapt(-get)?\s+install\b"), "installs system packages"),
    (re.compile(r"\bchmod\b.*\+x|\./\S+"), "runs a local script"),
]


# mods: where sparkrun finds them, and what they may not do here
EUGR_MODS = ("spark-arena/eugr-recipes", "main", "mods")      # sparkrun's fallback registry for unscoped mods
KNOWN_MOD_REGISTRIES = {                                      # sparkrun's built-in names for @name/mod references
    "eugr": EUGR_MODS,
    "official": ("spark-arena/recipe-registry", "main", "official-mods"),
    "experimental": ("spark-arena/recipe-registry", "main", "experimental-mods"),
}
MOD_LIMITS = {"files": 200, "file_bytes": 1024 * 1024, "mod_bytes": 4 * 1024 * 1024, "total_bytes": 8 * 1024 * 1024}
MOD_UPLOAD_DIR = "/tmp/sparkhub-mods"       # the hub copies the stored files here before the container starts
MOD_DIR = "/workspace/mods"                 # where sparkrun puts them, and where run.sh expects to be
MOD_SCANNED = (".sh", ".bash", ".py")
# anything in a mod that would reach the network at launch (the container has none)
NETWORK = [
    (re.compile(r"\b(curl|wget)\b"), "downloads with curl/wget"),
    (re.compile(r"\b(pip3?|uv)\s+(install|pip\s+install)\b|-m\s+pip\s+install\b|\bpip\.main\("),
     "installs Python packages"),
    (re.compile(r"\b(apt|apt-get|dnf|yum|apk)\s+(install|update|add)\b"), "installs system packages"),
    (re.compile(r"\bgit\s+(clone|fetch|pull|submodule)\b"), "fetches from a git repository"),
    (re.compile(r"snapshot_download|hf_hub_download|huggingface-cli\s+download|\bhf\s+download"),
     "downloads from Hugging Face"),
    (re.compile(r"^\s*(import|from)\s+(requests|httpx|aiohttp|urllib3)\b|urllib\.request|\burlopen\(|"
                r"\bsocket\.(create_connection|socket)\("),
     "opens network connections"),
    (re.compile(r"https?://"), "uses a URL"),
]


MANIFEST_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                  "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")


class RecipeError(ValueError):
    """The recipe can't be used; the message says why (shown to the admin)."""


# ---------------------------------------------------------------------------------------------- parsing
def parse(text: str) -> dict[str, Any]:
    if len(text) > 256 * 1024:
        raise RecipeError("the recipe is larger than 256 KiB")
    try:
        r = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RecipeError(f"not valid YAML: {str(e).splitlines()[0]}")
    if not isinstance(r, dict):
        raise RecipeError("not a recipe (the YAML is not a mapping)")
    if r.get("include"):
        raise RecipeError(f"builds on another recipe (include: {r['include']}); import that one instead")
    if str(r.get("recipe_version", "")) not in ("1", "2"):
        raise RecipeError(f"unsupported recipe_version {r.get('recipe_version')!r} (supported: 1, 2)")
    for k in ("model", "container"):
        if not isinstance(r.get(k), str) or not r[k].strip():
            raise RecipeError(f"the recipe has no {k}")
    if r.get("command") is not None and not isinstance(r["command"], str):
        raise RecipeError("command must be text")
    for k in ("defaults", "env"):
        if r.get(k) is not None and not isinstance(r[k], dict):
            raise RecipeError(f"{k} must be a mapping")
    return r


def description(r: dict[str, Any]) -> str:
    meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
    return str(r.get("description") or meta.get("description") or "").strip()


def recipe_name(r: dict[str, Any], fallback: str = "") -> str:
    name = r.get("name") or fallback or r["model"].split("/")[-1]
    return re.sub(r"[^\w.\-]+", "-", str(name)).strip("-")[:80] or "recipe"


def rejections(r: dict[str, Any]) -> list[str]:
    """Why this hub can't run the recipe (empty: it can)."""
    out = []
    d = r.get("defaults") or {}
    runtime = str(r.get("runtime") or "vllm").lower()
    if runtime != "vllm":
        out.append(f"runtime {runtime}: only vLLM recipes are supported")
    if r.get("solo_only") is False or int(r.get("min_nodes") or 1) > 1:
        out.append(f"needs {r.get('min_nodes') or 'several'} Sparks (this hub runs on one)")
    for k in ("tensor_parallel", "pipeline_parallel"):
        try:
            if int(d.get(k) or 1) > 1:
                out.append(f"{k} {d[k]}: needs more than one GPU")
        except (TypeError, ValueError):
            out.append(f"{k} {d.get(k)!r} is not a number")
    if r.get("build_args"):
        out.append("needs a local image build (build_args)")
    if not str(r.get("command") or "").strip():
        out.append("has no command: sparkrun builds the server command from its defaults itself, and this hub "
                   "only runs recipes that spell the command out")
    mods = r.get("mods")
    if mods is not None and (not isinstance(mods, list) or not all(isinstance(m, str) and m.strip() for m in mods)):
        out.append("mods must be a list of mod names")
    hooks = [k for k in ("pre_exec", "post_exec", "post_commands") if r.get(k)]
    if hooks:
        out.append(f"runs hook commands ({', '.join(hooks)}) that this hub does not run")
    return out


# ---------------------------------------------------------------------------------------------- translation
@dataclass
class Translation:
    spec: dict[str, Any]
    lines: list[dict[str, Any]]              # the command, line by line, with what each risky line does
    extra_repos: list[dict[str, Any]]        # other HF snapshots the command or env refers to
    fixed: dict[str, Any]                    # recipe defaults baked in (not editable here)
    editable: list[str]
    flags: list[str] = field(default_factory=list)


def _fill(command: str, values: dict[str, str]) -> str:
    """sparkrun placeholders: {name} → value, {{ and }} → literal braces. Unknown names raise RecipeError."""
    out, i, n = [], 0, len(command)
    while i < n:
        c = command[i]
        if command.startswith("{{", i):
            out.append("{")
            i += 2
        elif command.startswith("}}", i):
            out.append("}")
            i += 2
        elif c == "{":
            j = command.find("}", i)
            name = command[i + 1:j] if j > 0 else ""
            if not re.fullmatch(r"\w+", name):
                raise RecipeError(f"unbalanced brace in the command near {command[i:i + 30]!r}")
            if name not in values and i and command[i - 1] == "$":
                out.append(command[i:j + 1])      # ${VAR}: shell syntax, not a placeholder
                i = j + 1
                continue
            if name not in values:
                raise RecipeError(f"the command uses {{{name}}}, which has no default")
            out.append(values[name])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_flag(line: str, flag: str, multi: bool = False) -> str:
    """Remove `--flag value` / `--flag=value` (multi: several values) from a command line."""
    val = r"(?:'[^']*'|\"[^\"]*\"|[^\s'\"-]\S*)"
    pat = rf"\s{re.escape(flag)}(?:=\S+|(?:\s+{val}){'+' if multi else ''})"
    return re.sub(pat, "", line)


def _squeeze(line: str) -> str:
    """Runs of spaces outside quotes → one (left behind by joined continuation lines and removed flags)."""
    out, q = [], None
    for ch in line:
        if q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif ch in " \t" and out and out[-1] == " ":
            continue
        out.append(" " if ch == "\t" and not q else ch)
    return "".join(out).strip()


def served_name_of(line: str) -> str | None:
    m = re.search(r"--served-model-name(?:=|\s+)([^\s'\"-]\S*)", line)
    return m.group(1) if m else None


def mod_prelude(names: list[str]) -> list[str]:
    """The lines that run each mod before the recipe's command, as sparkrun's pre_exec does: the files go to
    /workspace/mods/<name>, then `./run.sh` runs there with WORKSPACE_DIR set (a subshell each, like sparkrun's
    separate docker exec). A failing mod stops the load."""
    out = []
    for n in names:
        q = shlex.quote(n)
        out.append(f"mkdir -p {MOD_DIR} && cp -a {MOD_UPLOAD_DIR}/{q} {MOD_DIR}/ && (export WORKSPACE_DIR=$PWD && "
                   f"cd {MOD_DIR}/{q} && chmod +x run.sh && ./run.sh) || {{ echo \"[sparkhub] mod {n} failed\" >&2; exit 1; }}")
    return out


def mod_name(ref: str) -> str:
    return ref.strip().removeprefix("mods/").rstrip("/").rsplit("/", 1)[-1]


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def scan_mod_file(path: str, text: str) -> list[dict[str, Any]]:
    """Lines of a mod's shell or Python file that would reach the network at launch (comments skipped)."""
    if not path.endswith(MOD_SCANNED) and not text.startswith("#!"):
        return []
    shell = not path.endswith(".py")
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        code = line.strip()
        if not code or code.startswith("#"):
            continue
        why = [w for rx, w in NETWORK if rx.search(code) and (shell or w != "uses a URL")]
        if why:
            hits.append({"file": path, "line": i, "text": line.strip()[:200], "why": why})
    return hits


def mod_problems(mods: list[dict[str, Any]]) -> list[str]:
    """Mods that would reach the network at launch: rejected, since the Custom container has none."""
    out = []
    for m in mods:
        if m["network"]:
            h = m["network"][0]
            more = f" and {len(m['network']) - 1} more line(s)" if len(m["network"]) > 1 else ""
            out.append(f"mod {m['name']} needs the network at launch, which the Custom container doesn't have: "
                       f"{h['file']} line {h['line']} {', '.join(h['why'])} ({h['text'][:100]}){more}")
    return out


def mods_digest(mods: list[dict[str, Any]]) -> str:
    """One hash over every mod file (name, path, git sha): recorded at approval, checked before each load."""
    h = hashlib.sha256()
    for m in mods:
        for f in m["files"]:
            h.update(f"{m['name']}/{f['path']}:{f['sha']}\n".encode())
    return h.hexdigest()


def translate(r: dict[str, Any], taken_names: set[str], mods: list[str] | None = None) -> Translation:
    """Recipe → Custom spec. `taken_names`: served names and aliases other modes use. `mods`: the resolved mod
    names, in the recipe's order (run before the command)."""
    problems = rejections(r)
    if problems:
        raise RecipeError("; ".join(problems))
    d = dict(r.get("defaults") or {})
    model = r["model"].strip()
    command = r["command"]
    used = set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", command))
    editable = []
    for k in EDITABLE:
        if k in d and k in used:
            try:
                validate_settings({k: d[k]})
                editable.append(k)
            except (ValueError, TypeError):
                pass       # out of the hub's range (e.g. a 1M window): baked in as the recipe has it
    values = {k: str(v) for k, v in d.items()}
    values.update({"model": model, "host": "0.0.0.0", "port": str(CONTAINER_PORT)})
    for k in editable:
        values[k] = "${" + k + "}"       # filled from the hub's settings at each load (presets.render)
    script = _fill(command, values)
    script = re.sub(r"\\\r?\n[ \t]*", " ", script)       # join continuation lines
    lines = [x.rstrip() for x in script.strip().splitlines() if x.strip() and not x.strip().startswith("#")]
    lines = mod_prelude(mods or []) + lines
    if not lines or not VLLM_START.match(lines[-1].strip()):
        raise RecipeError("the command must end with the vllm serve command (the server runs as the last step)")
    last = lines[-1].strip()
    name = served_name_of(last) or model.split("/")[-1].lower()
    if name in taken_names or name in ("auto", "fast", "quality", "custom"):
        name = f"{name}-custom"
    for flag, multi in (("--served-model-name", True), ("--port", False), ("--host", False)):
        last = _strip_flag(last, flag, multi)
    if not last.startswith("exec "):
        last = "exec " + last              # vLLM gets the stop signal, not the wrapping shell
    rev = str(r.get("model_revision") or "")
    if rev:                                    # the pinned commit, whatever the recipe wrote
        last = _strip_flag(last, "--revision")
        last += f" --revision {shlex.quote(rev)}"
    last += f" --served-model-name {shlex.quote(name)} --host 0.0.0.0 --port {CONTAINER_PORT}"
    lines[-1] = _squeeze(last)
    script = "\n".join(lines)

    # extra snapshots (other repos, or pinned files) the prelude or the env point at
    env = {str(k): str(v) for k, v in (r.get("env") or {}).items()}
    extra: dict[tuple[str, str], dict[str, Any]] = {}
    for text in [script, *env.values()]:
        for org, nm, sha in HF_SNAPSHOT.findall(text):
            repo = f"{org}/{nm}"
            if repo != model:
                extra.setdefault((repo, sha), {"repo": repo, "revision": sha, "patterns": []})
    for e in extra.values():
        # snapshot_download('<repo>', ..., allow_patterns=[...]) narrows what the prelude would have fetched
        m = re.search(re.escape(e["repo"]) + r"""['"][^)]*allow_patterns\s*=\s*\[([^\]]*)\]""", script)
        if m:
            e["patterns"] = re.findall(r"""['"]([^'"]+)['"]""", m.group(1))
    out_lines = []
    n_mods = len(mods or [])
    for i, ln in enumerate(lines):
        why = ["runs a mod (shown below)"] if i < n_mods else [w for rx, w in RISKY if rx.search(ln)]
        out_lines.append({"text": ln, "risky": why})
    flags = []
    if "--trust-remote-code" in script:
        flags.append("--trust-remote-code: the server runs Python code shipped with the model repo, if it has any")

    defaults: dict[str, Any] = {"max_model_len": 32768, "max_num_seqs": 4, "gpu_memory_utilization": 0.7, "mtp": 0,
                                "prefix_caching": True, "kv_cache_dtype": "auto", "tool_call_parser": "qwen3_xml",
                                "default_thinking": False}
    for k in editable:
        defaults[k] = validate_settings({k: d[k]})[k]
    ctx = _int(d.get("max_model_len"))
    if ctx:
        defaults["max_model_len"] = ctx
    defaults["default_max_tokens"] = min(16000, max(256, (defaults["max_model_len"] or 32768) // 2))
    unused = [k for k in SETTING_RULES if k not in editable and k not in GATEWAY_SETTINGS]
    hf_env = {"HF_HOME": "/cache/huggingface", "XDG_CACHE_HOME": "/cache/runtime", "HF_HUB_OFFLINE": "1"}
    ex = r.get("executor_config") if isinstance(r.get("executor_config"), dict) else {}
    spec = {
        "name": "custom", "title": "Custom", "origin": "recipe",
        "model": f"{recipe_name(r)} ({model})",
        "served_name": name,
        "image": r["container"].strip(),          # replaced by name@digest once resolved (pin)
        "image_ref": r["container"].strip(),
        "hf_repo": model, "revision": rev,
        "env": {**env, **hf_env},
        "ipc": "host", "gpus": "all",
        "ulimits": {"memlock": -1, "stack": 67108864},
        "network": "internal",
        "binds": ["${hf_cache}:/cache/huggingface:ro", "${runtime_volume}:/cache/runtime"],
        "args": ["bash", "-c", script],
        "container_port": CONTAINER_PORT,
        "lenient_placeholders": True,
        **({"entrypoint": str(ex["entrypoint"])} if "entrypoint" in ex else {}),   # "" = clear the image's
        "unused_settings": unused,
        "unused_note": "this recipe has no setting for it (its value is part of the command)",
        "defaults": defaults,
        "expected": {"load_seconds": 600},
        "reasoning_effort": False,
    }
    fixed = {k: v for k, v in d.items() if k not in editable}
    return Translation(spec, out_lines, list(extra.values()), fixed, editable, flags)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------- sources
@dataclass
class Source:
    kind: str                    # github | pasted
    url: str = ""
    repo: str = ""               # owner/name
    ref: str = ""                # branch or tag as given
    path: str = ""
    commit: str = ""

    def as_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


def parse_github_url(url: str) -> tuple[str, str, str]:
    """GitHub file URL (github.com/o/r/blob/ref/path or raw.githubusercontent.com/o/r/ref/path) → (repo, ref, path).
    Repo URLs (no file) return path ''."""
    u = urlparse(url.strip())
    parts = [p for p in u.path.split("/") if p]
    if u.scheme != "https":
        raise RecipeError("use an https URL")
    if u.netloc == "raw.githubusercontent.com" and len(parts) >= 4:
        return f"{parts[0]}/{parts[1]}", parts[2], "/".join(parts[3:])
    if u.netloc in ("github.com", "www.github.com") and len(parts) >= 2:
        repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
        if len(parts) >= 5 and parts[2] in ("blob", "raw"):
            return repo, parts[3], "/".join(parts[4:])
        if len(parts) == 2:
            return repo, "", ""
        if len(parts) >= 4 and parts[2] == "tree":
            return repo, parts[3], ""
    raise RecipeError("not a GitHub file or repository URL (github.com/<owner>/<repo>/blob/<branch>/<file>.yaml)")


class Lookups:
    """Read-only metadata lookups. `transport` lets tests answer instead of the internet."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, total_mem_gib: float = 121.0):
        self.http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(20.0), headers=UA,
                                      follow_redirects=True)
        self.total_mem_gib = total_mem_gib
        self._trees: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._heads: dict[tuple[str, str], str] = {}
        self._mods: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()      # one GitHub API call per tree or head (anonymous: 60 an hour)

    async def close(self) -> None:
        await self.http.aclose()

    async def _json(self, url: str, **kw) -> Any:
        r = await self.http.get(url, **kw)
        if r.status_code == 404:
            raise RecipeError(f"not found: {url}")
        if r.status_code in (401, 403):
            raise RecipeError(f"access denied ({r.status_code}) for {url}"
                              + (" (GitHub API rate limit? try again in an hour)" if "github" in url else ""))
        r.raise_for_status()
        return r.json()

    # GitHub ------------------------------------------------------------------
    async def github_commit(self, repo: str, ref: str, path: str = "") -> str:
        """Latest commit of `ref` (that touched `path`, if given)."""
        params = {"sha": ref, "per_page": "1"} if ref else {"per_page": "1"}
        if path:
            params["path"] = path
        data = await self._json(f"{GITHUB_API}/repos/{repo}/commits", params=params)
        if not data:
            raise RecipeError(f"no commit found for {path or ref} in {repo}")
        return data[0]["sha"]

    async def github_file(self, repo: str, commit: str, path: str) -> str:
        r = await self.http.get(f"{GITHUB_RAW}/{repo}/{commit}/{quote(path)}")
        if r.status_code != 200:
            raise RecipeError(f"could not read {path} from {repo} ({r.status_code})")
        return r.text

    async def github_head(self, repo: str, ref: str) -> str:
        async with self._lock:
            if (repo, ref) not in self._heads:
                self._heads[(repo, ref)] = await self.github_commit(repo, ref)
            return self._heads[(repo, ref)]

    async def github_tree(self, repo: str, commit: str) -> dict[str, dict[str, Any]]:
        """path -> git tree entry (type, mode, sha, size) for the whole repo at `commit`."""
        async with self._lock:
            if (repo, commit) not in self._trees:
                data = await self._json(f"{GITHUB_API}/repos/{repo}/git/trees/{commit}", params={"recursive": "1"})
                if data.get("truncated"):
                    raise RecipeError(f"{repo} is too large to list")
                self._trees[(repo, commit)] = {e["path"]: e for e in data.get("tree", [])}
            return self._trees[(repo, commit)]

    async def registry_manifest(self, repo: str, commit: str) -> list[dict[str, Any]]:
        """The registries in a repo's .sparkrun/registry.yaml ([] without one)."""
        if ".sparkrun/registry.yaml" not in await self.github_tree(repo, commit):
            return []
        try:
            reg = yaml.safe_load(await self.github_file(repo, commit, ".sparkrun/registry.yaml")) or {}
            return [x for x in reg.get("registries", []) if isinstance(x, dict)]
        except (yaml.YAMLError, AttributeError):
            return []

    # mods ---------------------------------------------------------------------
    async def resolve_mods(self, refs: list[str], src: Source) -> list[dict[str, Any]]:
        """Each mod the recipe names, found like sparkrun finds it, pinned to a commit and read (text only)."""
        mods = [await self.resolve_mod(ref, src) for ref in refs]
        names = [m["name"] for m in mods]
        if len(set(names)) != len(names):
            raise RecipeError(f"two mods share a name ({', '.join(names)})")
        if sum(f["size"] for m in mods for f in m["files"]) > MOD_LIMITS["total_bytes"]:
            raise RecipeError(f"the mods are larger than {MOD_LIMITS['total_bytes'] // 2**20} MiB together")
        return mods

    async def resolve_mod(self, ref: str, src: Source) -> dict[str, Any]:
        """sparkrun's order (core/mods.py): an @registry/name reference; next to the recipe file; the recipe's
        registry's mods folder; the eugr registry's mods folder."""
        ref = ref.strip()
        cands: list[tuple[str, str, str]] = []
        gh = src.kind == "github"
        manifest = await self.registry_manifest(src.repo, src.commit) if gh else []
        if ref.startswith("@"):
            reg, _, rel = ref[1:].partition("/")
            rel = rel.removeprefix("mods/")
            own = next((x for x in manifest if x.get("name") == reg and x.get("mods")), None)
            if own:
                cands.append((src.repo, src.commit, f"{own['mods']}/{rel}"))
            elif reg in KNOWN_MOD_REGISTRIES:
                repo, branch, sub = KNOWN_MOD_REGISTRIES[reg]
                cands.append((repo, await self.github_head(repo, branch), f"{sub}/{rel}"))
            else:
                raise RecipeError(f"mod {ref}: unknown registry {reg!r}")
        else:
            rel = ref.removeprefix("mods/")
            if gh:
                base = posixpath.dirname(src.path)
                cands += [(src.repo, src.commit, posixpath.join(base, rel)),
                          (src.repo, src.commit, posixpath.join(base, "mods", rel))]
                for x in manifest:
                    if x.get("mods") and x.get("recipes") and src.path.startswith(str(x["recipes"]).rstrip("/") + "/"):
                        cands.append((src.repo, src.commit, f"{str(x['mods']).rstrip('/')}/{rel}"))
            repo, branch, sub = EUGR_MODS
            cands.append((repo, await self.github_head(repo, branch), f"{sub}/{rel}"))
        if not rel or ".." in rel.split("/") or rel.startswith("/"):
            raise RecipeError(f"mod {ref}: not a mod name")
        tried = []
        for repo, commit, d in cands:
            d = posixpath.normpath(d).lstrip("/")
            tree = await self.github_tree(repo, commit)
            tried.append(f"{repo}/{d}")
            if (tree.get(f"{d}/run.sh") or {}).get("type") == "blob":
                return {"ref": ref, **await self._fetch_mod(repo, commit, d, tree)}
        raise RecipeError(f"mod {ref} not found (looked in {', '.join(tried)})")

    async def _fetch_mod(self, repo: str, commit: str, d: str, tree: dict[str, dict[str, Any]]) -> dict[str, Any]:
        key = (repo, commit, d)
        if key in self._mods:
            return self._mods[key]
        name = d.rsplit("/", 1)[-1]
        entries = [e for p, e in sorted(tree.items()) if p.startswith(d + "/")]
        for e in entries:
            if e["type"] == "commit":
                raise RecipeError(f"mod {name}: contains a git submodule ({e['path']})")
            if e.get("mode") == "120000":
                raise RecipeError(f"mod {name}: contains a symbolic link ({e['path']})")
        blobs = [e for e in entries if e["type"] == "blob"]
        if len(blobs) > MOD_LIMITS["files"]:
            raise RecipeError(f"mod {name}: more than {MOD_LIMITS['files']} files")
        big = [e["path"] for e in blobs if int(e.get("size") or 0) > MOD_LIMITS["file_bytes"]]
        if big:
            raise RecipeError(f"mod {name}: {big[0]} is larger than {MOD_LIMITS['file_bytes'] // 2**20} MiB")
        if sum(int(e.get("size") or 0) for e in blobs) > MOD_LIMITS["mod_bytes"]:
            raise RecipeError(f"mod {name}: larger than {MOD_LIMITS['mod_bytes'] // 2**20} MiB")
        sem = asyncio.Semaphore(8)

        async def one(e: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                r = await self.http.get(f"{GITHUB_RAW}/{repo}/{commit}/{quote(e['path'])}")
            if r.status_code != 200:
                raise RecipeError(f"mod {name}: could not read {e['path']} ({r.status_code})")
            if git_blob_sha(r.content) != e["sha"]:
                raise RecipeError(f"mod {name}: {e['path']} does not match its git checksum")
            try:
                text = r.content.decode("utf-8")
            except UnicodeDecodeError:
                raise RecipeError(f"mod {name}: {e['path']} is a binary file (only text can be reviewed)")
            return {"path": e["path"][len(d) + 1:], "size": len(r.content), "sha": e["sha"],
                    "exec": e.get("mode") == "100755", "text": text}
        files = list(await asyncio.gather(*(one(e) for e in blobs)))
        network = [h for f in files for h in scan_mod_file(f["path"], f["text"])]
        mod = {"name": name, "repo": repo, "commit": commit, "dir": d, "tree": tree[d]["sha"] if d in tree else "",
               "url": f"https://github.com/{repo}/tree/{commit}/{d}", "files": files, "network": network}
        self._mods[key] = mod
        return mod

    async def fetch_recipe(self, url: str) -> tuple[str, Source]:
        repo, ref, path = parse_github_url(url)
        if not path:
            raise RecipeError("that is a repository; add it as a registry (Browse) and pick a recipe from it")
        if not path.endswith((".yaml", ".yml")):
            raise RecipeError("a recipe is a .yaml file")
        commit = ref if SHA40.match(ref) else await self.github_commit(repo, ref, path)
        text = await self.github_file(repo, commit, path)
        return text, Source("github", url.strip(), repo, ref, path, commit)

    async def registry(self, url: str) -> dict[str, Any]:
        """A registry repo's recipes (YAML only, nothing else is fetched): {commit, recipes: [...]}."""
        repo, ref, _ = parse_github_url(url)
        if not ref:
            ref = (await self._json(f"{GITHUB_API}/repos/{repo}"))["default_branch"]
        commit = await self.github_commit(repo, ref)
        tree = await self.github_tree(repo, commit)
        paths = [p for p, e in tree.items() if e.get("type") == "blob"]
        dirs = [str(x["recipes"]) for x in await self.registry_manifest(repo, commit) if x.get("recipes")] or ["recipes"]
        files = [p for p in paths if p.endswith((".yaml", ".yml")) and any(p.startswith(d.rstrip("/") + "/") for d in dirs)]
        files = files[:200]
        sem = asyncio.Semaphore(8)

        async def one(p: str) -> dict[str, Any]:
            async with sem:
                try:
                    text = await self.github_file(repo, commit, p)
                    r = parse(text)
                except RecipeError as e:
                    return {"path": p, "error": str(e)}
            d = r.get("defaults") or {}
            out = {"path": p, "name": recipe_name(r, p.rsplit("/", 1)[-1].rsplit(".", 1)[0]),
                   "description": description(r)[:300], "model": r["model"], "image": r["container"],
                   "runtime": str(r.get("runtime") or "vllm"), "max_model_len": d.get("max_model_len"),
                   "problems": rejections(r)}
            if not out["problems"] and r.get("mods"):      # found, pinned and scanned as at import
                try:
                    mods = await self.resolve_mods(r["mods"], Source("github", "", repo, ref, p, commit))
                    out["mods"] = [m["name"] for m in mods]
                    out["problems"] = mod_problems(mods)
                except RecipeError as e:
                    out["problems"] = [str(e)]
                except httpx.HTTPError as e:
                    out["problems"] = [f"mod lookup failed: {e}"]
            if not out["problems"]:       # size, license and age from the model card (no download)
                try:
                    async with sem:
                        info = await self.hf_model(r["model"].strip(), str(r.get("model_revision") or ""))
                    w = self._file_facts(self._files(r["model"], info.get("sha", ""), info, []))["weights_gib"]
                    out.update(license=self._license(info), last_modified=info.get("lastModified"),
                               memory=self.memory_estimate(r, {"model": {"weights_gib": w}, "extra_repos": []}))
                except (RecipeError, httpx.HTTPError) as e:
                    out["problems"] = [f"model lookup failed: {e}"]
            return out
        recipes = list(await asyncio.gather(*(one(p) for p in files)))
        images = {r["image"] for r in recipes if not r.get("error") and not r["problems"]}

        async def check(img: str) -> tuple[str, str | None]:
            async with sem:
                try:
                    return img, await self.image_published(img)
                except httpx.HTTPError:
                    return img, None       # a registry hiccup is no verdict; the import checks again
        why = dict(await asyncio.gather(*(check(i) for i in images)))
        for r in recipes:
            if not r.get("error") and not r["problems"] and why.get(r["image"]):
                r["problems"] = [why[r["image"]]]
        return {"repo": repo, "ref": ref, "commit": commit, "recipes": recipes}

    # Hugging Face -------------------------------------------------------------
    async def hf_model(self, repo: str, revision: str) -> dict[str, Any]:
        rev = revision or "main"
        try:
            return await self._json(f"{HF}/api/models/{repo}/revision/{quote(rev, safe='')}",
                                    params={"blobs": "true"})
        except RecipeError as e:
            if "access denied" in str(e):
                raise RecipeError(f"{repo} is gated or private on Hugging Face; this hub downloads public models only")
            raise

    async def hf_text(self, repo: str, sha: str, path: str) -> str | None:
        r = await self.http.get(f"{HF}/{repo}/resolve/{sha}/{quote(path)}")
        return r.text if r.status_code == 200 and len(r.content) < 4 * 1024 * 1024 else None

    # image registry -----------------------------------------------------------
    @staticmethod
    def split_image(ref: str) -> tuple[str, str, str, str]:
        """image ref → (registry host, repository, tag, digest)."""
        name, digest = (ref.split("@", 1) + [""])[:2]
        tag = ""
        last = name.rsplit("/", 1)[-1]
        if ":" in last:
            name, tag = name.rsplit(":", 1)
        first = name.split("/", 1)[0]
        if "." in first or ":" in first or first == "localhost":
            host, repo = name.split("/", 1)
        else:
            host, repo = "registry-1.docker.io", name if "/" in name else f"library/{name}"
        return host, repo, tag or ("" if digest else "latest"), digest

    async def _registry_get(self, host: str, repo: str, path: str, accept: str = "",
                            method: str = "GET") -> httpx.Response:
        url = f"https://{host}/v2/{repo}/{path}"
        headers = {"Accept": accept} if accept else {}
        r = await self.http.request(method, url, headers=headers)
        if r.status_code == 401 and "bearer" in r.headers.get("www-authenticate", "").lower():
            ch = dict(re.findall(r'(\w+)="([^"]*)"', r.headers["www-authenticate"]))
            t = await self.http.get(ch["realm"], params={"service": ch.get("service", ""),
                                                         "scope": f"repository:{repo}:pull"})
            if t.status_code in (401, 403, 404):        # ghcr: no anonymous token for a missing or private image
                raise RecipeError(self._not_published(host, repo, t.status_code))
            t.raise_for_status()
            token = t.json().get("token") or t.json().get("access_token")
            r = await self.http.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"})
        if r.status_code in (401, 403, 404) and path.startswith("manifests/"):
            raise RecipeError(self._not_published(host, repo, r.status_code))
        if r.status_code != 200:
            raise RecipeError(f"image registry {host}: {path.split('/')[0]} {r.status_code} for {repo}")
        return r

    @staticmethod
    def _not_published(host: str, repo: str, status: int) -> str:
        if host == "registry-1.docker.io" and repo.startswith("library/") and "." not in repo:
            # a bare name like vllm-node: Docker Hub's official images don't have it, so it's a local build
            return (f"image {repo.removeprefix('library/')} is not published in any registry: it is built locally "
                    f"(e.g. by the author's build script), so this hub can't pull it, pin it or check how it was "
                    f"built. Only recipes with a published image can be imported")
        return f"image {host}/{repo} not found or private ({status}); this hub pulls public images only"

    async def image_published(self, ref: str) -> str | None:
        """Why a recipe's image can't be used here (None: it can be pulled). One manifest lookup, no download."""
        host, repo, tag, digest = self.split_image(ref)
        try:
            await self._registry_get(host, repo, f"manifests/{digest or tag}", ", ".join(MANIFEST_TYPES), "HEAD")
            return None
        except RecipeError as e:
            return str(e)

    async def image_facts(self, ref: str) -> dict[str, Any]:
        host, repo, tag, digest = self.split_image(ref)
        accept = ", ".join(MANIFEST_TYPES)
        r = await self._registry_get(host, repo, f"manifests/{digest or tag}", accept)
        top = r.headers.get("docker-content-digest") or digest
        m = r.json()
        platform_ok, arm = True, None
        if "manifests" in m:          # an index: pick linux/arm64
            arm = next((x for x in m["manifests"] if (x.get("platform") or {}).get("architecture") == "arm64"
                        and (x.get("platform") or {}).get("os") == "linux"), None)
            if not arm:
                platform_ok = False
                return {"pinned": None, "platform_ok": False, "registry": host, "repository": repo, "tag": tag}
            m = (await self._registry_get(host, repo, f"manifests/{arm['digest']}", accept)).json()
        cfg = (await self._registry_get(host, repo, f"blobs/{m['config']['digest']}")).json()
        if cfg.get("architecture") and cfg["architecture"] != "arm64":
            platform_ok = False
        labels = ((cfg.get("config") or {}).get("Labels")) or {}
        entrypoint = (cfg.get("config") or {}).get("Entrypoint")
        history = [h.get("created_by", "") for h in cfg.get("history", [])]
        display = "docker.io/" + repo if host == "registry-1.docker.io" else f"{host}/{repo}"
        return {
            "pinned": f"{display}@{top}" if top else None, "digest": top, "registry": host, "repository": repo,
            "tag": tag, "platform_ok": platform_ok, "created": cfg.get("created"),
            "size_gib": round(sum(x.get("size", 0) for x in m.get("layers", [])) / 2**30, 1),
            "labels": dict(list(labels.items())[:16]),
            "history_tail": [h[:300] for h in history[-8:]],
            "entrypoint": entrypoint,
            "built_by_ci": any(k in labels for k in ("org.opencontainers.image.revision", "org.opencontainers.image.source")),
        }

    # all facts for the review screen -------------------------------------------
    async def facts(self, r: dict[str, Any], t: Translation) -> dict[str, Any]:
        facts: dict[str, Any] = {"warnings": [], "flags": list(t.flags)}
        img, model = await asyncio.gather(self.image_facts(r["container"].strip()),
                                          self.hf_model(r["model"].strip(), str(r.get("model_revision") or "")),
                                          return_exceptions=True)
        if isinstance(img, Exception):
            raise RecipeError(f"image {r['container']}: {img}")
        if isinstance(model, Exception):
            raise RecipeError(f"model {r['model']}: {model}")
        if not img.get("platform_ok"):
            raise RecipeError(f"image {r['container']} has no linux/arm64 build (the DGX Spark is arm64)")
        facts["image"] = img
        sha = model["sha"]
        given = str(r.get("model_revision") or "")
        facts["model"] = {"repo": r["model"].strip(), "revision": sha, "given_revision": given,
                          "resolved": not SHA40.match(given)}
        files = self._files(r["model"].strip(), sha, model, [])
        facts["model"].update(self._file_facts(files))
        facts["model"]["license"] = self._license(model)
        facts["model"]["gated"] = bool(model.get("gated"))
        cfgtext = await self.hf_text(r["model"].strip(), sha, "config.json")
        try:
            auto_map = cfgtext and "auto_map" in json.loads(cfgtext)
        except ValueError:
            auto_map = False
        if auto_map:
            facts["flags"].append("config.json has auto_map: the model class is Python code from the model repo")
        if facts["model"]["py_files"]:
            facts["flags"].append(f"the model repo contains Python files: {', '.join(facts['model']['py_files'][:6])}")
        if facts["model"]["pickles"]:
            facts["flags"].append(f"pickle weight files (can run code when loaded): {', '.join(facts['model']['pickles'][:6])}")
        extras = []
        for e in t.extra_repos:
            info = await self.hf_model(e["repo"], e["revision"])
            ef = self._files(e["repo"], e["revision"], info, e["patterns"])
            extras.append({**e, **self._file_facts(ef), "license": self._license(info)})
            files += ef
        facts["extra_repos"] = extras
        facts["files"] = files
        facts["download_gib"] = round(sum(f["size"] for f in files) / 2**30, 1)
        facts["memory"] = self.memory_estimate(r, facts)
        return facts

    @staticmethod
    def _files(repo: str, sha: str, info: dict[str, Any], patterns: list[str]) -> list[dict[str, Any]]:
        out = []
        for s in info.get("siblings", []):
            p = s["rfilename"]
            if patterns and not any(fnmatch.fnmatch(p, pat) for pat in patterns):
                continue
            lfs = s.get("lfs") or {}
            out.append({"repo": repo, "revision": sha, "path": p, "size": int(lfs.get("size") or s.get("size") or 0),
                        "sha256": lfs.get("sha256"), "git_oid": None if lfs else s.get("blobId")})
        return out

    @staticmethod
    def _file_facts(files: list[dict[str, Any]]) -> dict[str, Any]:
        return {"n_files": len(files), "size_gib": round(sum(f["size"] for f in files) / 2**30, 1),
                "weights_gib": round(sum(f["size"] for f in files if f["path"].endswith(".safetensors")) / 2**30, 1),
                "py_files": [f["path"] for f in files if f["path"].endswith(".py")],
                "pickles": [f["path"] for f in files if f["path"].endswith(WEIGHT_PICKLES)]}

    @staticmethod
    def _license(info: dict[str, Any]) -> str:
        card = info.get("cardData") or {}
        lic = card.get("license_name") or card.get("license")
        if not lic:
            lic = next((t.split(":", 1)[1] for t in info.get("tags", []) if t.startswith("license:")), None)
        return str(lic or "not stated")

    def memory_estimate(self, r: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
        d = r.get("defaults") or {}
        weights = facts["model"]["weights_gib"] + sum(e["weights_gib"] for e in facts.get("extra_repos", []))
        kv_bytes = _int(d.get("kv_cache_memory_bytes"))
        util = d.get("gpu_memory_utilization")
        total = self.total_mem_gib
        if kv_bytes:
            est, how = weights + kv_bytes / 2**30 + 4, f"weights {weights:.0f} GiB + KV {kv_bytes / 2**30:.0f} GiB + ~4 GiB runtime"
        elif isinstance(util, (int, float)) and util > 0:
            est = max(weights + 4, float(util) * total)
            how = f"gpu_memory_utilization {util} × {total:.0f} GiB (weights {weights:.0f} GiB inside it)"
        else:
            est, how = weights * 1.3 + 4, f"weights {weights:.0f} GiB + ~30 % KV and runtime (the recipe sets no KV size)"
        room = total - 10        # the hub stack, the embedding service and the OS
        # rough: weights read through mmap (page cache) need not all stay resident, so above `room` is "tight",
        # not "no"; the first load measures the real number
        return {"estimate_gib": round(est, 1), "how": how, "total_gib": round(total, 1), "room_gib": round(room, 1),
                "fits": est <= total, "tight": room < est <= total}
