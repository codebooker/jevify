"""Entry point for every jev hook: python3 hookmain.py <HookEvent>, or hookmain.py cli <args>."""
from __future__ import annotations

import json
import os
import subprocess
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_ROOT)

from jev import apps, codex_route, commands, config, gate, route, state, trim  # noqa: E402


def _start_index_build(cwd: str) -> None:
    python = config.venv_python()
    if not cwd or python is None:
        return
    log_path = config.home() / "log" / "index.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log:
        subprocess.Popen([str(python), os.path.join(PLUGIN_ROOT, "jevsearch", "index.py"), "build", cwd],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def handle(event_name: str, event: dict):
    platform = apps.detect(event)
    os.environ["JEV_PLATFORM"] = platform
    settings = config.load_settings()
    sid = event.get("session_id") or ""
    if event_name == "UserPromptSubmit":
        prompt = event.get("prompt") or ""
        args = commands.match(prompt)
        if args is not None:
            output = {"decision": "block", "reason": commands.run(args, event)}
            if platform == "claude":
                output["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}
            return output
        if not settings["enabled"]:
            return None
        if platform == "claude":
            return route.on_prompt(event, settings, prompt)
        return codex_route.on_prompt(event, settings, prompt)
    if not settings["enabled"]:
        return None
    if event_name == "SessionStart":
        with state.session(sid) as s:
            s["cwd"] = event.get("cwd") or s["cwd"]
            s["model"] = event.get("model") or s["model"]
        state.cleanup()
        if settings["search"]:
            _start_index_build(event.get("cwd") or "")
        return None
    if event_name == "PreToolUse":
        return gate.pre_tool_use(event, settings)
    if event_name == "PostToolUse":
        output = gate.response_text(event)
        gate.record(event, output)
        return trim.post_tool_use(event, settings, output) if platform == "codex" else None
    if event_name in ("PreCompact", "PostCompact"):
        gate.compacted(event)
    return None


def main(argv) -> int:
    event_name = argv[1] if len(argv) > 1 else ""
    if event_name == "cli":
        print(commands.run(argv[2:], {"session_id": "", "cwd": os.getcwd()}))
        return 0
    try:
        output = handle(event_name, json.load(sys.stdin))
        if output:
            sys.stdout.write(json.dumps(output))
    except Exception as exc:  # a broken plugin must never block Codex
        try:
            state.log({"kind": "error", "event": event_name, "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])})
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
