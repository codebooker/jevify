"""Paths, settings, credentials and small helpers shared by every jev component."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import apps

DEFAULTS = {
    "enabled": True,
    "gate": "shadow",
    "trim": True,
    "search": True,
    "skills": True,
    "gate_jev_threshold": 0.9,
    "huge_file_lines": 600,
    "huge_file_tokens": 8000,
    "trim_min_tokens": 2000,
    "trim_budget_tokens": 1500,
    "skill_threshold": 0.6,
    "route": "on",
    "route_threshold": 0.6,
    # Same bar in both directions: Jev's job is to pick the model, up or down. Raise this to make
    # upgrades onto a pricier model rarer if the meter shows it reaching too often.
    "route_up_threshold": 0.6,
    "advisor": True,
    "scout_min_nodes": 5000,
    # Codex router tiers 1-5: [model, effort]. The local proxy applies Jev's pick per turn.
    "codex_tiers": [["gpt-6-luna", "low"], ["gpt-6-luna", "high"], ["gpt-6-sol", "high"],
                    ["gpt-6-astra", "high"], ["gpt-6-astra", "xhigh"]],
    "codex_min_tier": 1,
    "codex_max_tier": 5,
    "router_port": 47821,
    # Send Jev calls through a local viewer (e.g. Jeview on http://127.0.0.1:4777/codex/v1/systemone)
    # instead of straight to TypeSafe. Empty means straight there.
    "jev_api_url": "",
    # Credits per 1M tokens: [input, cached input, output].
    # Source: https://learn.chatgpt.com/docs/pricing, read 2026-09-22.
    # The gpt-6 Sol and Luna releases cost half their 5.6 namesakes, which makes gpt-5.6-terra,
    # gpt-5.6-sol and gpt-5.6-luna dominated; they stay here only to price older sessions.
    "rates": {
        "gpt-6-astra": [250, 25, 1250],
        "gpt-6-sol": [50, 5, 250],
        "gpt-6-luna": [2.5, 0.25, 12.5],
        "gpt-5.6-sol": [100, 10, 500],
        "gpt-5.6-terra": [50, 5, 300],
        "gpt-5.6-luna": [5, 0.5, 30],
    },
}
GATE_MODES = ("off", "shadow", "on")
ROUTE_MODES = GATE_MODES
KEY_NAMES = ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY")


def home() -> Path:
    override = os.environ.get("JEV_HOME")
    if override:
        return Path(override)
    return apps.home_for(apps.detect())


def homes() -> list:
    """This app's home first, then the other app's, so both apps share one key and one search venv."""
    if os.environ.get("JEV_HOME"):
        return [home()]
    current = apps.detect()
    return [apps.home_for(current), apps.home_for(apps.other(current))]


def venv_python():
    for base in homes():
        candidate = base / "venv" / "bin" / "python"
        if candidate.exists():
            return candidate
    return None


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle)
    os.replace(tmp, str(path))


def load_settings() -> dict:
    settings = json.loads(json.dumps(DEFAULTS))
    stored = read_json(home() / "settings.json", {})
    if isinstance(stored, dict):
        settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return settings


def save_settings(changes: dict) -> dict:
    settings = load_settings()
    settings.update(changes)
    write_json(home() / "settings.json", {k: v for k, v in settings.items() if v != DEFAULTS[k]})
    return settings


def credentials() -> dict:
    found = {}
    for base in reversed(homes()):
        try:
            text = (base / "credentials").read_text()
        except OSError:
            continue
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in KEY_NAMES and value.strip():
                found[key.strip()] = value.strip()
    # The file wins: it is the key the user configured on purpose. The environment is only a fallback,
    # because an app's hook environment may carry an unrelated (or stale) key of the same name.
    for key in KEY_NAMES:
        if not found.get(key) and os.environ.get(key):
            found[key] = os.environ[key]
    return found


def tokens(text: str) -> int:
    return (len(text) + 3) // 4


def repo_root(path: str) -> str:
    path = os.path.realpath(os.path.expanduser(path or "."))
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return os.path.realpath(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return path


def index_dir(root: str) -> Path:
    return home() / "index" / hashlib.sha256(root.encode()).hexdigest()[:16]
