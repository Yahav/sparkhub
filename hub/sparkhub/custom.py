"""The Custom mode: sparkrun recipes imported, reviewed, downloaded and assigned by an admin.

A recipe's life:
1. import (URL, registry pick or pasted YAML): translated to a spec (recipes.py), the image pinned to a digest and
   the model to a commit, the review facts gathered. State `pending`: listed, never loadable.
2. approve: an admin with a fresh 2FA code (web.py), recorded in the event log with the digest and revision.
3. download: the image pulled by digest (RepoDigest confirmed), every model file fetched and verified against the
   Hugging Face checksums (fetch_weights.py in a helper container). A failed check blocks loading.
4. assign: Custom gets this recipe's spec; it loads through the normal switch, on an internal network with no route
   out. The first load records the real memory, load time and KV size; smoke checks can run from the review page.
Mods: the mods a recipe names are pinned at import and stored with it (their files' text, in the
`mods` column), shown on the review and covered by the approval. Ones that need the network are rejected. The
download step runs them once in a throwaway container on the isolated network; each load copies the stored files
into the new container and runs them before the server (recipes.mod_prelude).
Updates (a newer commit of the source file, or of a mod) are checked daily and shown as a diff; importing one
makes a new pending recipe and leaves the assigned one alone.

Everything lives in the database (data/sparkhub.db, in the nightly backup); nothing is written to presets/.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import time
from string import Template
from typing import Any

import httpx

from . import recipes as rx
from .config import Config
from .db import DB
from .presets import Presets
from .runtime import Runtime, meminfo

log = logging.getLogger("sparkhub.custom")
UPDATE_CHECK_S = 24 * 3600
REGISTRY_TTL_S = 6 * 3600
REGISTRY_CACHE_V = 3        # bump when what a listing says changes (2: mods scanned, 3: images checked); refetched on start


class CustomError(Exception):
    pass


def _j(v: str | None, default: Any = None) -> Any:
    return json.loads(v) if v else default


class CustomManager:
    def __init__(self, cfg: Config, db: DB, presets: Presets, runtime: Runtime, modes, notify=None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.db = db
        self.presets = presets
        self.runtime = runtime
        self.modes = modes
        self.notify = notify or (lambda *a, **k: None)
        self.transport = transport          # tests answer GitHub / Hugging Face / registry lookups
        self.jobs: dict[int, asyncio.Task] = {}
        self.progress: dict[int, dict[str, Any]] = {}
        self._loop_task: asyncio.Task | None = None
        self.update_check_s = UPDATE_CHECK_S

    def lookups(self) -> rx.Lookups:
        total = meminfo().get("total_gib") or 121.0
        return rx.Lookups(self.transport, total_mem_gib=total)

    # ------------------------------------------------------------------ rows
    def row(self, rid: int) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM recipes WHERE id=?", (rid,))
        if r is None:
            return None
        d = dict(r)
        for k, default in (("source", {}), ("spec", {}), ("facts", {}), ("download", None), ("measured", None),
                           ("update_info", None), ("mods", [])):
            d[k] = _j(d[k], default)
        d["assigned"] = self.assigned_id() == rid
        d["status"] = self.status_of(d)
        if rid in self.progress and d["status"] == "downloading":
            d["progress"] = self.progress[rid]
        return d

    def rows(self) -> list[dict[str, Any]]:
        return [self.row(r["id"]) for r in self.db.all("SELECT id FROM recipes ORDER BY id DESC")]

    def status_of(self, d: dict[str, Any]) -> str:
        if d["state"] == "pending":
            return "pending review"
        dl = d.get("download") or {}
        if d["id"] in self.jobs and not self.jobs[d["id"]].done():
            return "downloading"
        if dl.get("state") == "done":
            return "ready"
        if dl.get("state") == "failed":
            return "download failed"
        return "approved, not downloaded"

    def assigned_id(self) -> int | None:
        v = self.db.get_setting("custom_recipe")
        return int(v) if v else None

    def assigned(self) -> dict[str, Any] | None:
        rid = self.assigned_id()
        return self.row(rid) if rid else None

    def taken_names(self) -> set[str]:
        return {self.presets[m].served_name for m in ("fast", "quality") if m in self.presets}

    # ------------------------------------------------------------------ startup
    def load(self) -> None:
        """Give Custom its assigned recipe's spec (or none)."""
        d = self.assigned()
        if d and d["state"] == "approved" and (d.get("download") or {}).get("state") == "done":
            try:
                spec = {**d["spec"], "uploads": self.mod_uploads(d)}
            except CustomError as e:
                log.error("recipe %s: %s", d["id"], e)
                self.presets.set_custom(None)
                return
            self.presets.set_custom(spec, d["id"])
        else:
            self.presets.set_custom(None)

    @staticmethod
    def mod_uploads(d: dict[str, Any]) -> list[dict[str, Any]]:
        """The approved mod files, checked against the approval, as files to copy into the model container."""
        mods = d.get("mods") or []
        if rx.mods_digest(mods) != d["spec"].get("mods_digest", rx.mods_digest([])):
            raise CustomError("the stored mod files don't match the approved list; import the recipe again")
        out = []
        for m in mods:
            for f in m["files"]:
                data = f["text"].encode()
                if rx.git_blob_sha(data) != f["sha"]:
                    raise CustomError(f"mod {m['name']}: {f['path']} changed since approval; import the recipe again")
                out.append({"path": f"{m['name']}/{f['path']}", "data": f["text"], "exec": f["exec"]})
        return out

    async def start(self) -> None:
        self.load()
        self._loop_task = asyncio.create_task(self._update_loop())
        self._refresh_task = asyncio.create_task(self._refresh_outdated())

    async def _refresh_outdated(self) -> None:
        """Listings made by an older hub (another REGISTRY_CACHE_V) are fetched again after an update."""
        try:
            await self.refresh_registries(outdated_only=True)
        except Exception:
            log.exception("registry refresh after an update")

    async def stop(self) -> None:
        for t in [self._loop_task, getattr(self, "_refresh_task", None), *self.jobs.values()]:
            if t and not t.done():
                t.cancel()

    # ------------------------------------------------------------------ import
    async def import_url(self, url: str, actor: str, parent_id: int | None = None) -> int:
        lk = self.lookups()
        try:
            text, src = await lk.fetch_recipe(url)
            return await self._import(lk, text, src, actor, parent_id)
        finally:
            await lk.close()

    async def import_registry(self, registry_url: str, path: str, actor: str) -> int:
        repo, ref, _ = rx.parse_github_url(registry_url)
        cache = (self.db.get_setting("registry_cache") or {}).get(registry_url) or {}
        ref = cache.get("ref") or ref or "main"
        return await self.import_url(f"https://github.com/{repo}/blob/{ref}/{path}", actor)

    async def import_text(self, text: str, actor: str) -> int:
        lk = self.lookups()
        try:
            return await self._import(lk, text, rx.Source("pasted"), actor, None)
        finally:
            await lk.close()

    async def _import(self, lk: rx.Lookups, text: str, src: rx.Source, actor: str, parent_id: int | None) -> int:
        r = rx.parse(text)
        problems = rx.rejections(r)
        if problems:
            raise rx.RecipeError("; ".join(problems))
        mods = await lk.resolve_mods(r["mods"], src) if r.get("mods") else []
        problems = rx.mod_problems(mods)
        if problems:
            raise rx.RecipeError("; ".join(problems))
        given = str(r.get("model_revision") or "")
        info = await lk.hf_model(r["model"].strip(), given)
        r["model_revision"] = info["sha"]            # pinned: the command gets --revision <commit>
        t = rx.translate(r, self.taken_names(), [m["name"] for m in mods])
        facts = await lk.facts(r, t)
        facts["model"]["given_revision"] = given
        facts["model"]["resolved"] = given != info["sha"]
        spec = t.spec
        spec["image"] = facts["image"]["pinned"]
        spec["revision"] = info["sha"]
        repo_dir = "${hf_cache}/hub/models--" + spec["hf_repo"].replace("/", "--")
        spec["weights"] = f"{repo_dir}/snapshots/{info['sha']}"
        spec["required_files"] = [f"${{hf_cache}}/hub/models--{f['repo'].replace('/', '--')}/snapshots/"
                                  f"{f['revision']}/{f['path']}" for f in facts["files"]]
        spec["expected"]["mem_used_gib"] = facts["memory"]["estimate_gib"]
        spec["mods_digest"] = rx.mods_digest(mods)
        facts["mods"] = [{k: v for k, v in m.items() if k not in ("files", "network")}
                         | {"files": [{k: v for k, v in f.items() if k != "text"} for f in m["files"]]} for m in mods]
        facts["lines"] = t.lines
        facts["fixed"] = {k: v for k, v in t.fixed.items() if k not in ("port", "host")}   # the hub sets those
        facts["editable"] = t.editable
        facts["description"] = rx.description(r)
        name = rx.recipe_name(r, src.path.rsplit("/", 1)[-1].rsplit(".", 1)[0] if src.path else "")
        cur = self.db.execute(
            "INSERT INTO recipes(name, created_at, created_by, source, yaml, spec, facts, state, parent_id, mods)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (name, time.time(), actor, json.dumps(src.as_dict()), text, json.dumps(spec), json.dumps(facts),
             parent_id, json.dumps([{k: v for k, v in m.items() if k != "network"} for m in mods])))
        rid = cur.lastrowid
        self.db.event("recipe_imported", actor, id=rid, name=name, source=src.url or src.kind, commit=src.commit,
                      image=spec["image"], revision=info["sha"], mods=self._mod_refs(mods))
        if parent_id:
            self.db.execute("UPDATE recipes SET update_info=NULL WHERE id=?", (parent_id,))
        return rid

    # ------------------------------------------------------------------ review
    def approve(self, rid: int, actor: str) -> None:
        d = self.row(rid)
        if not d:
            raise CustomError("no such recipe")
        if d["state"] != "pending":
            raise CustomError("already approved")
        self.db.execute("UPDATE recipes SET state='approved', approved_at=?, approved_by=? WHERE id=?",
                        (time.time(), actor, rid))
        self.db.event("recipe_approved", actor, id=rid, name=d["name"], image=d["spec"]["image"],
                      revision=d["spec"]["revision"], source=d["source"].get("url") or "pasted",
                      commit=d["source"].get("commit"), mods=self._mod_refs(d["mods"]),
                      mods_digest=d["spec"].get("mods_digest"))

    @staticmethod
    def _mod_refs(mods: list[dict[str, Any]]) -> list[str]:
        return [f"{m['repo']}/{m['dir']}@{m['commit'][:12]}" for m in mods]

    async def delete(self, rid: int, actor: str) -> None:
        d = self.row(rid)
        if not d:
            raise CustomError("no such recipe")
        if d["assigned"]:
            if self.custom_busy():
                raise CustomError("Custom is loaded with this recipe; switch to another mode first")
            self.unassign(actor)
        job = self.jobs.pop(rid, None)
        if job and not job.done():
            job.cancel()
        self.db.execute("DELETE FROM recipes WHERE id=?", (rid,))
        self.db.event("recipe_deleted", actor, id=rid, name=d["name"])
        try:
            await self.runtime.remove_volume(self.presets.runtime_volume(rid))
        except Exception as e:
            log.warning("removing the runtime volume of recipe %s: %s", rid, e)

    # ------------------------------------------------------------------ download and verify
    def start_download(self, rid: int, actor: str) -> None:
        d = self.row(rid)
        if not d or d["state"] != "approved":
            raise CustomError("approve the recipe first")
        if rid in self.jobs and not self.jobs[rid].done():
            raise CustomError("already downloading")
        if any(not j.done() for j in self.jobs.values()):
            raise CustomError("another recipe is downloading; one at a time")
        self.db.event("recipe_download", actor, id=rid, name=d["name"])
        self.progress[rid] = {"step": "starting", "done": 0, "total": 0}
        self.jobs[rid] = asyncio.create_task(self._download(rid))

    def _set_download(self, rid: int, **kw: Any) -> None:
        self.db.execute("UPDATE recipes SET download=? WHERE id=?", (json.dumps(kw), rid))

    async def _download(self, rid: int) -> None:
        d = self.row(rid)
        spec, facts = d["spec"], d["facts"]
        t0 = time.time()
        prog = self.progress[rid]
        try:
            prog.update(step="image", done=0, total=0)

            def img_progress(done: int, total: int) -> None:
                prog.update(done=done, total=total)
            await self.runtime.pull_image(spec["image"], img_progress)
            digests = await self.runtime.image_digests(spec["image"])
            want = spec["image"].split("@", 1)[1]
            if self.cfg.check_install and not any(x.endswith("@" + want) for x in digests):
                raise CustomError(f"the pulled image's digest does not match {want[:19]}… (RepoDigests: {digests})")
            prog.update(step="weights", done=0, total=sum(f["size"] for f in facts["files"]), file="")

            def w_progress(done: int, total: int, file: str = "") -> None:
                prog.update(done=done, total=total, file=file)
            res = await self.runtime.run_fetch(facts["files"], self.cfg.host_hf_cache, w_progress)
            if not res.get("ok"):
                raise CustomError(res.get("error") or "the download failed")
            mods_ok = None
            if d["mods"]:
                prog.update(step="mods", done=0, total=0, file="")
                out = await self.runtime.run_once(self._modcheck_spec(d), timeout=900)
                if out.get("exit_code") != 0:
                    tail = " | ".join(out.get("log", "").strip().splitlines()[-4:])
                    raise CustomError(f"the mods failed in the offline test run (exit {out.get('exit_code')}): "
                                      f"{tail[:400]}")
                mods_ok = len(d["mods"])
            took = round(time.time() - t0)
            self._set_download(rid, state="done", at=time.time(), seconds=took, verified=res.get("verified"),
                               downloaded=res.get("downloaded"), present=res.get("present"), digest=want,
                               mods_tested=mods_ok)
            self.db.event("recipe_downloaded", None, id=rid, name=d["name"], seconds=took,
                          verified=res.get("verified"))
            self.notify("SparkHub: Custom recipe downloaded",
                        f"{d['name']}: image and {res.get('verified')} model files verified in {took // 60} min.")
            if d["assigned"]:
                self.load()
                await self.modes.check_install("custom")
        except asyncio.CancelledError:
            self._set_download(rid, state="failed", at=time.time(), error="cancelled")
            raise
        except Exception as e:
            msg = str(e) if isinstance(e, CustomError) else f"{type(e).__name__}: {e}"
            log.warning("download of recipe %s failed: %s", rid, msg)
            self._set_download(rid, state="failed", at=time.time(), error=msg[:600])
            self.db.event("recipe_download_failed", None, id=rid, name=d["name"], error=msg[:300])
            self.notify("SparkHub: Custom download failed", f"{d['name']}: {msg[:300]}", "high")
        finally:
            self.progress.pop(rid, None)

    def _modcheck_spec(self, d: dict[str, Any]) -> dict[str, Any]:
        """The recipe's container as it loads (image, entrypoint, env, mounts, isolated network), running only
        the mods and then exiting."""
        sp = d["spec"]
        paths = {**self.presets._paths(), "runtime_volume": self.presets.runtime_volume(d["id"])}
        script = "\n".join([*rx.mod_prelude([m["name"] for m in d["mods"]]), "echo '[sparkhub] mods applied'"])
        return {"mode": "custom", "container": f"{self.cfg.instance}-modcheck", "image": sp["image"],
                "env": dict(sp.get("env") or {}), "binds": [Template(b).substitute(paths) for b in sp["binds"]],
                "args": ["bash", "-c", script], "ipc": sp.get("ipc"), "gpus": sp.get("gpus"),
                "ulimits": dict(sp.get("ulimits") or {}), "host_port": 0, "container_port": rx.CONTAINER_PORT,
                "network": "internal", "labels": {"sparkhub.helper": "modcheck"},
                **({"entrypoint": sp["entrypoint"]} if "entrypoint" in sp else {}),
                "uploads": self.mod_uploads(d)}

    # ------------------------------------------------------------------ assignment
    def custom_busy(self) -> bool:
        m = self.modes
        return m.loaded == "custom" or m.target == "custom" or (m.busy and m.previous == "custom")

    def assign(self, rid: int, actor: str) -> None:
        d = self.row(rid)
        if not d:
            raise CustomError("no such recipe")
        if d["status"] != "ready":
            raise CustomError(f"only a downloaded and verified recipe can be assigned (this one: {d['status']})")
        if d["assigned"]:
            return
        if self.custom_busy():
            raise CustomError("Custom is loaded; switch to Fast or Quality before assigning another recipe")
        self.db.set_setting("custom_recipe", rid)
        self.db.execute("DELETE FROM mode_settings WHERE mode='custom'")   # the old recipe's settings don't apply
        self.load()
        self.modes.install["custom"] = []
        self.db.event("custom_assigned", actor, id=rid, name=d["name"], image=d["spec"]["image"],
                      revision=d["spec"]["revision"])

    def unassign(self, actor: str) -> None:
        if self.custom_busy():
            raise CustomError("Custom is loaded; switch to Fast or Quality first")
        rid = self.assigned_id()
        self.db.set_setting("custom_recipe", None)
        self.db.execute("DELETE FROM mode_settings WHERE mode='custom'")
        self.load()
        self.modes.install["custom"] = ["no recipe assigned (an admin sets one up on the Models page)"]
        if self.db.get_setting("desired_mode") == "custom":
            self.db.set_setting("desired_mode", "fast")
        self.db.event("custom_unassigned", actor, id=rid)

    # ------------------------------------------------------------------ after a load
    async def record_load(self, mode: str, seconds: float, kv_tokens: int | None) -> None:
        if mode != "custom" or "custom" not in self.presets:
            return
        rid = self.presets["custom"].raw.get("recipe_id")
        d = self.row(rid) if rid else None
        if not d:
            return
        m = d.get("measured") or {}
        m.update(load_seconds=round(seconds), mem_used_gib=meminfo().get("used_gib"), kv_tokens=kv_tokens,
                 at=time.time())
        self.db.execute("UPDATE recipes SET measured=? WHERE id=?", (json.dumps(m), rid))
        # the switch progress bar and the memory check use the real numbers from now on
        spec = d["spec"]
        spec["expected"].update(load_seconds=round(seconds), mem_used_gib=m["mem_used_gib"], measured=True)
        self.db.execute("UPDATE recipes SET spec=? WHERE id=?", (json.dumps(spec), rid))
        self.presets["custom"].raw["expected"] = spec["expected"]

    async def smoke(self, rid: int, actor: str) -> dict[str, Any]:
        """Smoke checks against the loaded Custom model: tool call, truncation, prefix cache."""
        if not (self.modes.loaded == "custom" and self.modes.state == "ready"
                and self.presets["custom"].raw.get("recipe_id") == rid):
            raise CustomError("load Custom with this recipe first")
        url = self.modes.upstream("custom") + "/v1/chat/completions"
        name = self.presets["custom"].served_name
        out: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0)) as http:
            async def chat(**body) -> dict[str, Any]:
                body.setdefault("chat_template_kwargs", {"enable_thinking": False})
                r = await http.post(url, json={"model": name, **body})
                if r.status_code != 200:
                    raise CustomError(f"{r.status_code} {r.text[:200]}")
                return r.json()

            tool = {"type": "function", "function": {
                "name": "get_weather", "description": "Current weather for a city",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
            try:
                res = await chat(messages=[{"role": "user", "content": "What is the weather in Paris right now? "
                                                                        "Use the tool."}],
                                 tools=[tool], tool_choice="auto", max_tokens=400)
                calls = res["choices"][0]["message"].get("tool_calls") or []
                ok = bool(calls) and calls[0]["function"]["name"] == "get_weather" and \
                    "city" in json.loads(calls[0]["function"]["arguments"] or "{}")
                out["tool_call"] = {"ok": ok, "detail": f"{len(calls)} tool call(s)" if calls
                                    else "answered without calling the tool"}
            except Exception as e:
                out["tool_call"] = {"ok": False, "detail": str(e)[:200]}
            try:
                res = await chat(messages=[{"role": "user", "content": "Count from 1 to 200, one number per line."}],
                                 max_tokens=8)
                fr = res["choices"][0].get("finish_reason")
                n = (res.get("usage") or {}).get("completion_tokens")
                out["truncation"] = {"ok": fr == "length" and (n is None or n <= 8),
                                     "detail": f"finish_reason {fr}, {n} tokens"}
            except Exception as e:
                out["truncation"] = {"ok": False, "detail": str(e)[:200]}
            try:
                long = "Reference notes (ignore): " + " ".join(f"item-{i} is filed under shelf {i % 17}."
                                                             for i in range(400))
                msgs = [{"role": "system", "content": long}, {"role": "user", "content": "Say OK."}]
                await chat(messages=msgs, max_tokens=1)
                res = await chat(messages=msgs, max_tokens=1)
                details = (res.get("usage") or {}).get("prompt_tokens_details")
                cached = (details or {}).get("cached_tokens")
                if details is None:
                    out["prefix_cache"] = {"ok": None, "detail": "not reported (the recipe doesn't pass "
                                                                 "--enable-prompt-tokens-details)"}
                else:
                    out["prefix_cache"] = {"ok": bool(cached), "detail": f"{cached or 0} of "
                                           f"{res['usage'].get('prompt_tokens')} prompt tokens cached on the repeat"}
            except Exception as e:
                out["prefix_cache"] = {"ok": False, "detail": str(e)[:200]}
        d = self.row(rid)
        m = d.get("measured") or {}
        m["smoke"] = {"at": time.time(), "by": actor, **out}
        self.db.execute("UPDATE recipes SET measured=? WHERE id=?", (json.dumps(m), rid))
        self.db.event("recipe_smoke", actor, id=rid, **{k: v["ok"] for k, v in out.items()})
        return out

    # ------------------------------------------------------------------ registries
    def registries(self) -> list[dict[str, Any]]:
        cache = self.db.get_setting("registry_cache") or {}
        out = []
        for reg in self.db.get_setting("recipe_registries") or []:
            c = cache.get(reg["url"]) or {}
            out.append({**reg, "fetched_at": c.get("fetched_at"), "error": c.get("error"),
                        "recipes": c.get("recipes") or [], "commit": c.get("commit")})
        return out

    def set_registries(self, regs: list[dict[str, Any]], actor: str) -> None:
        self.db.set_setting("recipe_registries", regs)
        self.db.event("recipe_registries", actor, urls=[r["url"] for r in regs if r.get("enabled")])

    def add_registry(self, url: str, actor: str) -> None:
        repo, _, _ = rx.parse_github_url(url)
        regs = self.db.get_setting("recipe_registries") or []
        norm = f"https://github.com/{repo}"
        if any(r["url"].rstrip("/") == norm for r in regs):
            raise CustomError("that registry is already listed")
        regs.append({"url": norm, "name": repo, "enabled": True})
        self.set_registries(regs, actor)

    async def refresh_registry(self, url: str) -> None:
        cache = dict(self.db.get_setting("registry_cache") or {})
        lk = self.lookups()
        try:
            data = await lk.registry(url)
            cache[url] = {"fetched_at": time.time(), "v": REGISTRY_CACHE_V, **data}
        except Exception as e:
            cache[url] = {**(cache.get(url) or {}), "fetched_at": time.time(),
                          "error": str(e)[:300] if isinstance(e, rx.RecipeError) else f"{type(e).__name__}: {e}"[:300]}
        finally:
            await lk.close()
        self.db.set_setting("registry_cache", cache)

    async def refresh_registries(self, force: bool = False, outdated_only: bool = False) -> None:
        cache = self.db.get_setting("registry_cache") or {}
        for reg in self.db.get_setting("recipe_registries") or []:
            c = cache.get(reg["url"]) or {}
            outdated = bool(c) and c.get("v") != REGISTRY_CACHE_V
            stale = time.time() - c.get("fetched_at", 0) > REGISTRY_TTL_S
            if reg.get("enabled") and (force or outdated or (stale and not outdated_only)):
                await self.refresh_registry(reg["url"])

    # ------------------------------------------------------------------ updates
    async def check_updates(self) -> int:
        """Compare each recipe with its sources: the recipe file's head (GitHub imports) and each mod's folder.
        A change is stored as a diff on the newest import of that source; nothing is applied."""
        found = 0
        lk = self.lookups()
        try:
            newest: dict[tuple[str, ...], int] = {}
            for d in self.rows():          # only the newest import of each source is checked
                s = d["source"]
                if s.get("kind") == "github" and not rx.SHA40.match(s.get("ref") or ""):
                    newest.setdefault(("github", s["repo"], s.get("ref") or "", s["path"]), d["id"])
                elif d["mods"]:            # pasted, or pinned to a commit: only the mods can change
                    newest.setdefault(("text", hashlib.sha256(d["yaml"].encode()).hexdigest()), d["id"])
            for rid in newest.values():
                d = self.row(rid)
                try:
                    info = await self._find_update(lk, d)
                    if not info or info["sig"] == (d.get("update_info") or {}).get("sig"):
                        continue
                    self.db.execute("UPDATE recipes SET update_info=? WHERE id=?", (json.dumps(info), rid))
                    self.db.event("recipe_update_available", None, id=rid, name=d["name"], commit=info["commit"],
                                  mods=info["mods_changed"])
                    what = "changed upstream" + (f" ({info['commit'][:8]})" if info["recipe_changed"] else "")
                    if info["mods_changed"]:
                        what = (what + " and" if info["recipe_changed"] else "") + \
                            f" has changed mods ({', '.join(info['mods_changed'])})"
                    self.notify("SparkHub: Custom recipe update", f"{d['name']} {what}. Review the diff on the "
                                f"Custom page; nothing changes until you import it.")
                    found += 1
                except Exception as e:
                    log.info("update check of recipe %s: %s", rid, e)
        finally:
            await lk.close()
        self.db.set_setting("recipe_update_checked", time.time())
        return found

    async def _find_update(self, lk: rx.Lookups, d: dict[str, Any]) -> dict[str, Any] | None:
        s = d["source"]
        text, commit, diffs = d["yaml"], s.get("commit") or "", []
        gh = s.get("kind") == "github"
        if gh and not rx.SHA40.match(s.get("ref") or ""):
            head = await lk.github_commit(s["repo"], s.get("ref") or "", s["path"])
            if head != s["commit"]:
                new = await lk.github_file(s["repo"], head, s["path"])
                if new != d["yaml"]:
                    diffs.append("".join(difflib.unified_diff(
                        d["yaml"].splitlines(True), new.splitlines(True),
                        f"{s['path']} @ {s['commit'][:8]}", f"{s['path']} @ {head[:8]}")))
                    text = new
                commit = head
        recipe_changed = bool(diffs)
        r = rx.parse(text)
        changed: list[str] = []
        pins: list[tuple[str, str, str, str]] = []      # what the changed mods are now (repo, dir, tree)
        if r.get("mods") and not rx.rejections(r):
            src = rx.Source("github", s["url"], s["repo"], s.get("ref") or "", s["path"], commit) if gh \
                else rx.Source("pasted")
            old = {m["name"]: m for m in d["mods"]}
            for m in await lk.resolve_mods(r["mods"], src):
                o = old.get(m["name"])
                if o and (o["repo"], o["dir"], o["tree"]) == (m["repo"], m["dir"], m["tree"]):
                    continue
                changed.append(m["name"])
                pins.append((m["name"], m["repo"], m["dir"], m["tree"]))
                before = {f["path"]: f["text"] for f in (o or {}).get("files", [])}
                after = {f["path"]: f["text"] for f in m["files"]}
                for path in sorted(set(before) | set(after)):
                    if before.get(path) != after.get(path):
                        diffs.append("".join(difflib.unified_diff(
                            (before.get(path) or "").splitlines(True), (after.get(path) or "").splitlines(True),
                            f"mod {m['name']}/{path} @ {(o or {}).get('commit', '')[:8] or '(new)'}",
                            f"mod {m['name']}/{path} @ {m['commit'][:8]}")))
        if not diffs:
            return None
        # the same change is reported once, however many unrelated commits the mods' repo gets meanwhile
        sig = hashlib.sha256(json.dumps([commit if recipe_changed else "", pins]).encode()).hexdigest()
        return {"commit": commit, "checked_at": time.time(), "diff": "\n".join(diffs)[:60000], "sig": sig,
                "recipe_changed": recipe_changed, "mods_changed": changed,
                "url": f"https://github.com/{s['repo']}/blob/{commit}/{s['path']}" if gh else ""}

    async def import_update(self, rid: int, actor: str) -> int:
        d = self.row(rid)
        info = (d or {}).get("update_info")
        if not info:
            raise CustomError("no update to import")
        s = d["source"]
        lk = self.lookups()
        try:
            if s.get("kind") == "github":
                text = await lk.github_file(s["repo"], info["commit"], s["path"])
                src = rx.Source("github", s["url"], s["repo"], s["ref"], s["path"], info["commit"])
            else:
                text, src = d["yaml"], rx.Source("pasted")
            return await self._import(lk, text, src, actor, rid)
        finally:
            await lk.close()

    async def _update_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            last = self.db.get_setting("recipe_update_checked") or 0
            if time.time() - last < self.update_check_s:
                continue
            try:
                await self.check_updates()
                await self.refresh_registries()
            except Exception:
                log.exception("recipe update check")
