"""Which app is calling the plugin (Codex or Claude) and what differs between them."""
from __future__ import annotations

import os
from pathlib import Path

PLATFORMS = ("codex", "claude")

# Claude API prices per 1M tokens: [input, output, cache read, 5-minute cache write, 1-hour cache write].
# Source: claude-api skill model table (cached 2026-06-24), read 2026-09-22. Cache writes are 1.25x / 2x input.
CLAUDE_PRICES = {
    "claude-fable-5-1": [10, 50, 0.25, 12.5, 20],
    "claude-opus-5": [5, 25, 0.5, 6.25, 10],
    "claude-sonnet-5": [2, 10, 0.2, 2.5, 4],
    "claude-haiku-4-5": [1, 5, 0.1, 1.25, 2],
}
_RANKS = (("haiku", 1), ("sonnet", 2), ("opus", 3), ("fable", 4), ("mythos", 4))


def from_path(path: str):
    if "/.codex/" in path:
        return "codex"
    if "/.claude/" in path:
        return "claude"
    return None


def detect(event=None) -> str:
    forced = os.environ.get("JEV_PLATFORM")
    if forced in PLATFORMS:
        return forced
    if os.environ.get("PLUGIN_ROOT") or (isinstance(event, dict) and "turn_id" in event):
        return "codex"
    return from_path(os.path.abspath(__file__)) or "claude"


def other(platform: str) -> str:
    return "codex" if platform == "claude" else "claude"


def home_for(platform: str) -> Path:
    if platform == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "jev"
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "jev"


def model_rank(model) -> int:
    name = (model or "").lower()
    for key, rank in _RANKS:
        if key in name:
            return rank
    return 3  # unknown models are treated like the Opus tier


def price_for(model):
    for name in sorted(CLAUDE_PRICES, key=len, reverse=True):
        if model and model.startswith(name):
            return CLAUDE_PRICES[name]
    return None
