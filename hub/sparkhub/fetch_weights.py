"""Download and verify model files into a Hugging Face cache (Custom recipes).

Runs in a short-lived helper container (the hub's own image) with the host's HF cache mounted read-write, started
by the hub for a Custom recipe. The manifest (a JSON list of files with their pinned revision and expected sha256
or git blob id, from the Hugging Face API) comes in the SPARKHUB_FETCH environment variable.

Files land in the standard cache layout (models--org--name/blobs/<hash>, snapshots/<revision>/<path> as relative
symlinks), so they are shared with anything else using the cache. A blob that is already there is re-hashed
instead of downloaded; a partial download resumes. Every file is verified; a mismatch fails the whole run.

Progress goes to stdout as JSON lines; the hub reads them from the container log.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

CHUNK = 8 * 1024 * 1024


def say(**kw) -> None:
    print(json.dumps(kw), flush=True)


def git_blob_hasher(size: int):
    h = hashlib.sha1()
    h.update(f"blob {size}\0".encode())
    return h


class Run:
    def __init__(self, root: Path, files: list[dict], endpoint: str, transport=None):
        self.root = root
        self.files = files
        self.endpoint = endpoint.rstrip("/")
        self.total = sum(int(f["size"]) for f in files)
        self.done = 0
        self.last_say = 0.0
        self.http = httpx.Client(transport=transport, timeout=httpx.Timeout(60.0), follow_redirects=True,
                                 headers={"User-Agent": "sparkhub-fetch"})

    def progress(self, current: str, force: bool = False) -> None:
        now = time.time()
        if force or now - self.last_say > 2:
            self.last_say = now
            say(progress=True, done=self.done, total=self.total, file=current)

    def hasher(self, f: dict):
        return hashlib.sha256() if f.get("sha256") else git_blob_hasher(int(f["size"]))

    def expected(self, f: dict) -> str:
        return f.get("sha256") or f["git_oid"]

    def hash_file(self, path: Path, f: dict, count: bool = True) -> str:
        h = self.hasher(f)
        with path.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
                if count:
                    self.done += len(chunk)
                    self.progress(f["path"])
        return h.hexdigest()

    def one(self, f: dict) -> str:
        """'present', 'downloaded'; raises on a mismatch."""
        if not f.get("sha256") and not f.get("git_oid"):
            raise ValueError(f"{f['path']}: no checksum in the manifest")
        repo_dir = self.root / "hub" / ("models--" + f["repo"].replace("/", "--"))
        blob = repo_dir / "blobs" / self.expected(f)
        link = repo_dir / "snapshots" / f["revision"] / f["path"]
        size = int(f["size"])
        how = "present"
        if blob.exists() and blob.stat().st_size == size:
            got = self.hash_file(blob, f)
            if got != self.expected(f):
                raise ValueError(f"{f['path']}: the cached file's checksum does not match ({got[:12]}…); "
                                 f"remove {blob} and retry")
        else:
            blob.parent.mkdir(parents=True, exist_ok=True)
            part = blob.with_name(blob.name + ".incomplete")
            h = self.hasher(f)
            have = part.stat().st_size if part.exists() else 0
            if have > size:
                part.unlink()
                have = 0
            if have:
                with part.open("rb") as fh:
                    while chunk := fh.read(CHUNK):
                        h.update(chunk)
                self.done += have
            url = f"{self.endpoint}/{f['repo']}/resolve/{f['revision']}/{f['path']}"
            headers = {"Range": f"bytes={have}-"} if have else {}
            with self.http.stream("GET", url, headers=headers) as r:
                if r.status_code == 416 and have == size:
                    pass
                elif r.status_code not in (200, 206):
                    raise ValueError(f"{f['path']}: download failed ({r.status_code})")
                else:
                    if have and r.status_code == 200:     # the server ignored the range: start over
                        h, self.done = self.hasher(f), self.done - have
                        mode = "wb"
                    else:
                        mode = "ab"
                    with part.open(mode) as out:
                        for chunk in r.iter_bytes(CHUNK):
                            out.write(chunk)
                            h.update(chunk)
                            self.done += len(chunk)
                            self.progress(f["path"])
            if part.stat().st_size != size:
                raise ValueError(f"{f['path']}: got {part.stat().st_size} bytes, expected {size}")
            got = h.hexdigest()
            if got != self.expected(f):
                part.unlink()
                raise ValueError(f"{f['path']}: checksum mismatch after download ({got[:12]}… ≠ {self.expected(f)[:12]}…)")
            os.replace(part, blob)
            how = "downloaded"
        link.parent.mkdir(parents=True, exist_ok=True)
        target = os.path.relpath(blob, link.parent)
        if link.is_symlink() or link.exists():
            if link.is_symlink() and os.readlink(link) == target:
                return how
            link.unlink()
        link.symlink_to(target)
        return how

    def run(self) -> int:
        say(start=True, files=len(self.files), total=self.total)
        counts = {"present": 0, "downloaded": 0}
        try:
            for f in self.files:
                counts[self.one(f)] += 1
                self.progress(f["path"])
        except Exception as e:
            say(error=f"{type(e).__name__}: {e}" if not isinstance(e, ValueError) else str(e))
            return 1
        self.progress("", force=True)
        say(ok=True, verified=len(self.files), **counts)
        return 0


def main() -> int:
    files = json.loads(os.environ["SPARKHUB_FETCH"])
    root = Path(os.environ.get("SPARKHUB_FETCH_ROOT", "/hf"))
    return Run(root, files, os.environ.get("HF_ENDPOINT", "https://huggingface.co")).run()


if __name__ == "__main__":
    sys.exit(main())
