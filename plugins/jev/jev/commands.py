"""/jev (app) and $jev (CLI) commands, answered by the prompt hook without any model call."""
from __future__ import annotations

import re
import time

from . import apps, codex_route, config, meter, state

# The app may put a chip before the command: "[$jev:jev](/path/SKILL.md) status" or
# "[@Jev](plugin://jev@personal) /jev". Either way what follows is the command.
CHIP = re.compile(r"^\s*\[[@$][Jj]ev(?::jev)?\]\([^)]*\)\s*")
PATTERN = re.compile(r"^\s*[/$]jev(?::jev)?(?=\s|$)(.*)$", re.S)
HELP = """/jev commands ($jev in the Codex CLI):
  /jev                      status and savings
  /jev on | off             turn everything on or off
  /jev gate                 gate report (last 7 days)
  /jev gate off|shadow|on   tool-call gate mode
  /jev trim on|off          output trimmer (Codex)
  /jev skills on|off        skill router
  /jev route off|shadow|on  model routing (Codex proxy, Claude tier hints)
  /jev advisor on|off       new-topic advice (Claude)
  /jev viewer <url>|off     send Jev calls through a local viewer
  /jev help                 this list"""


def match(prompt):
    text = prompt or ""
    chip = CHIP.match(text)
    if chip:
        rest = text[chip.end():]
        found = PATTERN.match(rest)
        return found.group(1).split() if found else rest.split()
    found = PATTERN.match(text)
    return found.group(1).split() if found else None


def _on(value) -> str:
    return "on" if value else "off"


def _index_line(event) -> str:
    cwd = event.get("cwd") or state.read(event.get("session_id") or "")["cwd"]
    if not cwd:
        return ""
    root = config.repo_root(cwd)
    meta = config.read_json(config.index_dir(root) / "meta.json", {}) or {}
    if meta.get("state") == "ready":
        return "Search index: ready for %s (%s nodes, built %s)" % (
            root, meta.get("nodes"), time.strftime("%a %H:%M", time.localtime(meta.get("built_at", 0))))
    if meta.get("state") == "building":
        return "Search index: building for %s" % root
    return "Search index: not built yet for %s (builds at the next session start)" % root


def status(event, settings) -> str:
    lines = ["Jev: %s · gate: %s · trim: %s · skills: %s · search: %s" % (
        _on(settings["enabled"]), settings["gate"], _on(settings["trim"]), _on(settings["skills"]),
        _on(settings["search"]))]
    if apps.detect(event) == "claude":
        lines[0] += " · route: %s · advisor: %s" % (settings["route"], _on(settings["advisor"]))
    else:
        lines[0] += " · route: %s" % settings["route"]
        lines.append(codex_route.router_status(settings))
    if not config.credentials():
        lines.append("No Jev key found: run scripts/setup.py. Jev features stay idle until then.")
    lines.append(_index_line(event))
    lines.append(meter.summary(event, settings))
    return "\n".join(line for line in lines if line)


def run(args, event) -> str:
    settings = config.load_settings()
    words = [a.lower() for a in args]
    if not words or words == ["status"]:
        return status(event, settings)
    head, rest = words[0], words[1:]
    if head in ("on", "off") and not rest:
        config.save_settings({"enabled": head == "on"})
        return "Jev is now %s." % head
    if head == "gate" and not rest:
        return meter.gate_report(settings, session_id=event.get("session_id") or "")
    if head == "gate" and len(rest) == 1 and rest[0] in config.GATE_MODES:
        config.save_settings({"gate": rest[0]})
        return "Tool-call gate: %s." % rest[0]
    if head in ("trim", "skills") and rest in (["on"], ["off"]):
        config.save_settings({head: rest[0] == "on"})
        return "%s: %s." % ("Output trimmer" if head == "trim" else "Skill router", rest[0])
    if head == "route" and len(rest) == 1 and rest[0] in config.ROUTE_MODES:
        config.save_settings({"route": rest[0]})
        return "Tier routing: %s." % rest[0]
    if head == "advisor" and rest in (["on"], ["off"]):
        config.save_settings({"advisor": rest[0] == "on"})
        return "New-topic advisor: %s." % rest[0]
    if head == "viewer" and len(rest) == 1:
        url = "" if rest[0] == "off" else args[-1]
        if url and not url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            return "The viewer must be a loopback address, so the prompts in Jev calls stay on this machine."
        config.save_settings({"jev_api_url": url})
        return "Jev calls now go to %s." % (url or "TypeSafe directly")
    if head == "help":
        return HELP
    return "Unknown /jev command.\n" + HELP
