#!/usr/bin/env python3
"""Write a pinned manifest of a model directory (file -> size and hash) from a known-good download.

  make_manifest.py local-dir DIR > manifests/x.json     (hf download --local-dir: hashes from .cache/huggingface)
  make_manifest.py hf-cache SNAPSHOT [HYBRID] > x.json   (HF cache: the blob names are the hashes)
  make_manifest.py hf-cache SNAPSHOT --extra SNAPSHOT2 FILE... > x.json   (plus a few files of a second repo)

Hashes are what the Hub itself uses: sha256 for large (LFS) files, the git blob sha1 for small ones. Files written
by prepare-hybrid.sh (the fp8 shards) are recorded by size only.
"""
import json
import os
import sys
from pathlib import Path


def kind(h: str) -> str:
    return "sha256" if len(h) == 64 else "git-sha1"


def local_dir(d: Path) -> dict:
    files = {}
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d).as_posix()
        if rel.startswith(".cache/") or not p.is_file():
            continue
        meta = d / ".cache" / "huggingface" / "download" / f"{rel}.metadata"
        h = meta.read_text().splitlines()[1].strip() if meta.exists() else None
        files[rel] = {"size": p.stat().st_size, **({kind(h): h} if h else {})}
    return files


def hf_cache(snap: Path, only: list[str] | None = None) -> dict:
    files = {}
    for p in sorted(snap.rglob("*")):
        rel = p.relative_to(snap).as_posix()
        if p.is_dir() or (only and rel not in only):
            continue
        target = os.readlink(p) if p.is_symlink() else None
        h = Path(target).name if target else None
        files[rel] = {"size": p.stat().st_size, **({kind(h): h} if h else {})}
    return files


def main() -> None:
    mode, d = sys.argv[1], Path(sys.argv[2])
    out = {"files": local_dir(d) if mode == "local-dir" else hf_cache(d)}
    if mode == "hf-cache" and len(sys.argv) > 4 and sys.argv[3] == "--extra":
        out["extra"] = hf_cache(Path(sys.argv[4]), sys.argv[5:])
    elif mode == "hf-cache" and len(sys.argv) > 3:
        hyb = {}
        for p in sorted(Path(sys.argv[3]).iterdir()):
            if p.name in (".prepared", "fp8_convert.log") or p.name.endswith(".bf16.bak"):
                continue
            hyb[p.name] = {"size": p.stat().st_size, **({"link": True} if p.is_symlink() else {})}
        out["hybrid"] = hyb
    print(json.dumps(out, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
