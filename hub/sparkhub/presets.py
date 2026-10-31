"""Mode presets: fully resolved container specs with a few editable settings.

A preset file (presets/<mode>.json) holds the image, env, binds and args captured from the benchmark recipe.
Args may contain ${placeholders}; the editable settings fill them in. Any other "$" (shell variables in a
`bash -c` wrapper) is passed through untouched. Settings changed in the dashboard are
stored in the database and apply on the next load.

Fast and Quality come from these files. The third mode, Custom, has no file: its spec is the approved recipe the
admin assigned to it (custom.py), and it is absent until there is one.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from .config import Config
from .db import DB

MODES = ("fast", "quality", "custom")
BUILTIN = ("fast", "quality")           # presets/<mode>.json; custom comes from the database
# the parts of a preset file a release can change that make the running container out of date (not titles or notes)
PIN_KEYS = ("image", "weights", "required_files", "env", "ipc", "shm_size", "gpus", "ulimits", "binds", "args",
            "speculative", "entrypoint", "network", "container_port")
PLACEHOLDER = re.compile(r"\$\{(\w+)\}")   # ${setting}; a KeyError for an unknown name, plain $VAR untouched

# name -> (type, validator description, check)
SETTING_RULES: dict[str, tuple[type, Any]] = {
    "max_model_len": (int, (4096, 262144)),
    "max_num_seqs": (int, (1, 16)),
    "gpu_memory_utilization": (float, (0.30, 0.85)),
    "mtp": (int, (0, 5)),
    "prefix_caching": (bool, None),
    "kv_cache_dtype": (str, ("auto", "fp8", "fp8_e4m3")),
    "tool_call_parser": (str, ("qwen3_xml", "qwen3_coder")),
    "default_thinking": (bool, None),
    "default_max_tokens": (int, (256, 262144)),
}
SETTING_HELP = {
    "max_model_len": "Context window (tokens)",
    "max_num_seqs": "Concurrent sequences",
    "gpu_memory_utilization": "Memory share (gpu-memory-utilization)",
    "mtp": "Speculative tokens (MTP, 0 = off)",
    "prefix_caching": "Prefix caching",
    "kv_cache_dtype": "KV cache type",
    "tool_call_parser": "Tool-call parser",
    "default_thinking": "Thinking on by default",
    "default_max_tokens": "Default max_tokens when the client sends none",
}


def validate_settings(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in values.items():
        if k not in SETTING_RULES:
            raise ValueError(f"unknown setting {k}")
        typ, rule = SETTING_RULES[k]
        if typ is bool:
            if isinstance(v, str):
                v = v.lower() in ("1", "true", "on", "yes")
            v = bool(v)
        elif typ is int:
            if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
                raise ValueError(f"{k} must be an integer")
            v = int(v)
        elif typ is float:
            v = round(float(v), 3)
        else:
            v = str(v)
        if isinstance(rule, tuple) and typ in (int, float):
            lo, hi = rule
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
        elif isinstance(rule, tuple) and v not in rule:
            raise ValueError(f"{k} must be one of {', '.join(rule)}")
        out[k] = v
    if "default_max_tokens" in out and "max_model_len" in out and out["default_max_tokens"] >= out["max_model_len"]:
        raise ValueError("default_max_tokens must be smaller than max_model_len")
    return out


@dataclass
class Preset:
    name: str
    raw: dict[str, Any]

    @property
    def title(self) -> str:
        return self.raw.get("title", self.name)

    @property
    def served_name(self) -> str:
        return self.raw["served_name"]

    @property
    def container(self) -> str:
        return self.raw["container"]

    @property
    def host_port(self) -> int:
        return int(self.raw["host_port"])

    @property
    def pin(self) -> str:
        """Hash of what the container runs; a model container carries it, so an upgrade that changes it is noticed."""
        blob = json.dumps({k: self.raw.get(k) for k in PIN_KEYS}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self.raw["defaults"])

    @property
    def expected(self) -> dict[str, Any]:
        return dict(self.raw.get("expected", {}))

    @property
    def origin(self) -> str:
        return self.raw.get("origin", "built-in")

    @property
    def unused_settings(self) -> list[str]:
        """Settings this recipe has no flag for (shown as not applicable instead of an input)."""
        return list(self.raw.get("unused_settings", []))


class Presets:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self.presets: dict[str, Preset] = {}
        for m in BUILTIN:
            p = cfg.presets_dir / f"{m}.json"
            raw = json.loads(p.read_text())
            raw["container"] = f"{cfg.instance}-{m}"
            if m in cfg.mode_ports:
                raw["host_port"] = cfg.mode_ports[m]
            self.presets[m] = Preset(m, raw)

    def __getitem__(self, mode: str) -> Preset:
        return self.presets[mode]

    def __contains__(self, mode: str) -> bool:
        return mode in self.presets

    @property
    def available(self) -> list[str]:
        """Modes that have a spec, in order (custom only once a recipe is assigned)."""
        return [m for m in MODES if m in self.presets]

    def set_custom(self, raw: dict[str, Any] | None, recipe_id: int | None = None) -> None:
        """Assign (or clear) the Custom mode's spec: the translated, pinned recipe (custom.py)."""
        if raw is None:
            self.presets.pop("custom", None)
            return
        raw = dict(raw)
        raw["container"] = f"{self.cfg.instance}-custom"
        raw["host_port"] = self.cfg.mode_ports.get("custom", 18320)   # unused on the isolated network (tests)
        raw["recipe_id"] = recipe_id
        self.presets["custom"] = Preset("custom", raw)

    def editable(self, mode: str) -> list[str]:
        unused = self.presets[mode].unused_settings
        return [k for k in SETTING_RULES if k not in unused]

    def by_served_name(self, served: str) -> str | None:
        for m, p in self.presets.items():
            if p.served_name == served:
                return m
        return None

    def settings(self, mode: str) -> dict[str, Any]:
        s = self.presets[mode].defaults
        row = self.db.one("SELECT value FROM mode_settings WHERE mode=?", (mode,))
        if row:
            s.update(json.loads(row["value"]))
        return s

    def overrides(self, mode: str) -> dict[str, Any]:
        row = self.db.one("SELECT value FROM mode_settings WHERE mode=?", (mode,))
        return json.loads(row["value"]) if row else {}

    def update_settings(self, mode: str, values: dict[str, Any], actor: str) -> dict[str, Any]:
        merged = self.settings(mode)
        merged.update(validate_settings(values))
        editable = self.editable(mode)
        validate_settings({k: v for k, v in merged.items() if k in editable})   # cross-field checks on the result
        if merged["default_max_tokens"] >= merged["max_model_len"]:     # also when the window is fixed (Custom)
            raise ValueError("default_max_tokens must be smaller than max_model_len")
        defaults = self.presets[mode].defaults
        overrides = {k: v for k, v in merged.items() if defaults.get(k) != v}
        self.db.execute(
            "INSERT INTO mode_settings(mode, value, updated_at, updated_by) VALUES(?, ?, ?, ?)"
            " ON CONFLICT(mode) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at,"
            " updated_by=excluded.updated_by", (mode, json.dumps(overrides), time.time(), actor))
        self.db.event("settings_changed", actor, mode=mode, values=validate_settings(values))
        return merged

    def reset_settings(self, mode: str, actor: str) -> None:
        self.db.execute("DELETE FROM mode_settings WHERE mode=?", (mode,))
        self.db.event("settings_reset", actor, mode=mode)

    def _paths(self, mode: str | None = None) -> dict[str, str]:
        paths = {"hf_cache": self.cfg.host_hf_cache, "models_dir": self.cfg.host_models_dir,
                 "presets_dir": self.cfg.host_presets_dir}
        if mode == "custom":
            # a Docker volume per recipe for its runtime caches (compile, autotune): writable, nothing on the host
            paths["runtime_volume"] = self.runtime_volume(self.presets[mode].raw.get("recipe_id"))
        return paths

    def runtime_volume(self, recipe_id: Any) -> str:
        return f"{self.cfg.instance}-custom-{recipe_id}-runtime"

    def weights_dir(self, mode: str) -> Path | None:
        w = self.presets[mode].raw.get("weights")
        return Path(Template(w).substitute(self._paths())) if w else None

    def weights_problems(self, mode: str) -> list[str]:
        """What is missing on disk for this mode's weights. The hub sees the host's model directories at the
        same paths (read-only mounts), so the paths match the ones the model container gets."""
        if self.presets[mode].origin == "recipe":
            # every file of the verified download (custom.py lists them all, including other repos' files)
            files = [Path(Template(f).substitute(self._paths())) for f in self.presets[mode].raw.get("required_files", [])]
            missing = [f for f in files if not f.exists()]
            return [f"weights incomplete: {len(missing)} of {len(files)} files missing (e.g. {missing[0]}); "
                    f"download again on the Models page"] if missing else []
        d = self.weights_dir(mode)
        if d is None:
            return []
        if not d.is_dir():
            return [f"weights not found ({d})"]
        idx = d / "model.safetensors.index.json"
        if not idx.is_file():
            return [f"weights incomplete: no model.safetensors.index.json in {d}"]
        try:
            shards = sorted(set(json.loads(idx.read_text())["weight_map"].values()))
        except (ValueError, KeyError) as e:
            return [f"weights index unreadable ({type(e).__name__})"]
        missing = [x for x in shards if not (d / x).exists()]
        if missing:
            return [f"weights incomplete: {len(missing)} of {len(shards)} files missing in {d}"]
        extra = [Path(Template(f).substitute(self._paths())) for f in self.presets[mode].raw.get("required_files", [])]
        return [f"weights incomplete: {f} missing" for f in extra if not f.exists()]

    def render(self, mode: str) -> dict[str, Any]:
        """The container spec for the next load: image, env, binds, args, port."""
        p = self.presets[mode]
        s = self.settings(mode)
        paths = self._paths(mode)
        lenient = bool(p.raw.get("lenient_placeholders"))   # a recipe's script: unknown ${NAME} is shell syntax
        spec_cfg = None
        if s["mtp"] > 0:
            extra = {k: v for k, v in p.raw.get("speculative", {}).items() if k != "method"}
            spec_cfg = json.dumps({"method": p.raw.get("speculative", {}).get("method", "mtp"),
                                   "num_speculative_tokens": s["mtp"], **extra}, separators=(",", ":"))
        values = {k: str(v) for k, v in s.items()}
        values["gpu_memory_utilization"] = f"{s['gpu_memory_utilization']:g}"
        args: list[str] = []
        for a in p.raw["args"]:
            if a == "${prefix_caching}":
                args.append("--enable-prefix-caching" if s["prefix_caching"] else "--no-enable-prefix-caching")
            elif a == "${speculative}":
                if spec_cfg:
                    args += ["--speculative-config", spec_cfg]
            elif lenient:
                args.append(PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)) if m.group(1) in SETTING_RULES
                                            else m.group(0), a))
            else:
                args.append(PLACEHOLDER.sub(lambda m: values[m.group(1)], a))
        binds = [Template(b).substitute(paths) for b in p.raw["binds"]]
        return {
            "mode": mode, "container": p.container, "pin": p.pin, "image": p.raw["image"],
            "env": dict(p.raw.get("env", {})),
            "binds": binds, "args": args, "ipc": p.raw.get("ipc"), "shm_size": p.raw.get("shm_size"),
            "gpus": p.raw.get("gpus"), "ulimits": dict(p.raw.get("ulimits", {})), "host_port": p.host_port,
            "container_port": int(p.raw.get("container_port", 8000)),
            **({"network": p.raw["network"]} if p.raw.get("network") else {}),
            **({"entrypoint": p.raw["entrypoint"]} if "entrypoint" in p.raw else {}),
            **({"uploads": p.raw["uploads"]} if p.raw.get("uploads") else {}),
        }
