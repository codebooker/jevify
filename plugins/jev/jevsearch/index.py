"""Build and refresh the Graphify graph for one repository. The graph lives outside the repository."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev import config  # noqa: E402

EXCLUDE = {".git", "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__", ".cache",
           "target", ".next", "graphify-out", ".idea", ".vscode", "coverage"}
MAX_FILES, MAX_BYTES = 20000, 1_000_000
MAX_WALK = 100_000  # names gathered by the non-git fallback walk before giving up on completeness


def _extensions():
    from graphify.detect import CODE_EXTENSIONS
    return set(CODE_EXTENSIONS) | {".md", ".mdx"}


def list_files(root: str) -> list:
    # A session started in the home folder or at / is not a project; walking it would be slow and useless.
    if os.path.realpath(root) in {os.path.realpath(os.path.expanduser("~")), "/"}:
        return []
    extensions = _extensions()
    names = None
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            names = out.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        pass
    if names is None:
        names = []
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in EXCLUDE]
            names.extend(os.path.relpath(os.path.join(folder, f), root) for f in files)
            if len(names) >= MAX_WALK:
                names = names[:MAX_WALK]
                break
    kept = []
    for name in sorted(names):
        path = os.path.join(root, name)
        if EXCLUDE.intersection(Path(name).parts) or Path(name).suffix not in extensions:
            continue
        try:
            if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_BYTES:
                continue
            with open(path, "rb") as handle:
                head = handle.read(1024)
        except OSError:
            continue
        if b"Code generated" in head or b"AUTO-GENERATED" in head:
            continue
        kept.append(name)
        if len(kept) >= MAX_FILES:
            break
    return kept


def fingerprint(root: str, files: list) -> str:
    digest = hashlib.sha256()
    for name in files:
        try:
            stat = os.stat(os.path.join(root, name))
        except OSError:
            continue
        digest.update(("%s\0%d\0%d\n" % (name, stat.st_size, stat.st_mtime_ns)).encode())
    return digest.hexdigest()


def read_meta(root: str) -> dict:
    return config.read_json(config.index_dir(root) / "meta.json", {}) or {}


def is_stale(root: str) -> bool:
    meta = read_meta(root)
    return meta.get("fingerprint") != fingerprint(root, list_files(root))


def is_building(root: str) -> bool:
    lock_path = config.index_dir(root) / "build.lock"
    if not lock_path.exists():
        return False
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def build(root: str) -> str:
    root = config.repo_root(root)
    target = config.index_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    with open(target / "build.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        files = list_files(root)
        current = fingerprint(root, files)
        meta = read_meta(root)
        if meta.get("fingerprint") == current and (target / "graph.json").exists():
            return "fresh"
        config.write_json(target / "meta.json", dict(meta, state="building", started_at=time.time(), root=root))
        import networkx as nx
        from graphify.build import build as build_graph
        from graphify.extract import extract
        started = time.time()
        extraction = extract([Path(root) / name for name in files], root=Path(root),
                             cache_root=target / "extraction-cache", max_workers=4)
        graph = build_graph([extraction], root=Path(root))
        tmp = target / "graph.json.tmp"
        tmp.write_text(json.dumps(nx.node_link_data(graph)))
        os.replace(str(tmp), str(target / "graph.json"))
        config.write_json(target / "meta.json", {
            "state": "ready", "root": root, "fingerprint": current, "built_at": time.time(),
            "seconds": round(time.time() - started, 2), "files": len(files),
            "nodes": graph.number_of_nodes(), "edges": graph.number_of_edges()})
        return "built"


_cache = {}


def load_graph(root: str):
    import networkx as nx
    path = config.index_dir(root) / "graph.json"
    mtime = path.stat().st_mtime
    cached = _cache.get(root)
    if cached and cached[0] == mtime:
        return cached[1]
    graph = nx.node_link_graph(json.loads(path.read_text()))
    _cache[root] = (mtime, graph)
    return graph


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "build":
        print(json.dumps({"root": sys.argv[2], "result": build(sys.argv[2]), "ts": time.time()}), flush=True)
