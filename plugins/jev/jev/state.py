"""Per-session state shared by hooks (file-locked, since tool calls can run in parallel) and the event log."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import time

from . import config

EMPTY = {"cwd": "", "task": "", "reads": [], "searches": [], "denied": [], "recent": [],
         "edits": 0, "step": 0, "skills": None, "model": "", "tier": "", "tiers": [], "prompts": 0,
         "advised_at": 0, "codex_tier": 0, "codex_model": "", "codex_routes": {},
         "codex_last_model": "", "codex_last_routed": False,
         "gate_seen": {}, "gate_kinds": {}, "gate_unparsed": []}
LOG_LIMIT = 5_000_000


def _fresh(data) -> dict:
    merged = json.loads(json.dumps(EMPTY))
    if isinstance(data, dict):
        merged.update(data)
    return merged


def path(session_id: str):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")[:128]
    return config.home() / "sessions" / (safe + ".json")


@contextlib.contextmanager
def session(session_id: str):
    """Yield the session's state dict under an exclusive lock and save it on normal exit."""
    target = path(session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _fresh(config.read_json(target, {}))
        yield data
        config.write_json(target, data)


def read(session_id: str) -> dict:
    return _fresh(config.read_json(path(session_id), {}))


def _log_path():
    return config.home() / "log" / "events.jsonl"


def log(event: dict) -> None:
    target = _log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.stat().st_size > LOG_LIMIT:
            os.replace(str(target), str(target.with_name("events.1.jsonl")))
    except OSError:
        pass
    with open(target, "a") as handle:
        handle.write(json.dumps(dict(event, ts=round(time.time(), 3))) + "\n")


def events(session_id=None) -> list:
    try:
        lines = _log_path().read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if session_id is None or event.get("session") == session_id:
            found.append(event)
    return found


def cleanup(days: int = 7) -> None:
    cutoff = time.time() - days * 86400
    for folder in (config.home() / "sessions", config.home() / "outputs"):
        if not folder.is_dir():
            continue
        for item in folder.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
            except OSError:
                pass
