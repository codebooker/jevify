"""PreToolUse gate for read-only shell commands, plus the PostToolUse bookkeeping it relies on."""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import config, jevclient, shellparse, state

TRUNCATED = re.compile(r"…\d+ (?:tokens|chars) truncated…")
PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)
EDIT_TOOLS = ("apply_patch", "Edit", "Write", "MultiEdit", "NotebookEdit")
GATED = ("Bash", "Read", "Grep", "Glob")
JEV_QUESTION = ("This read-only shell command is unnecessary for accomplishing the task: the information it "
                "would return was already gathered by the recent commands, or it is irrelevant to the task. "
                "Commands that inspect relevant code or output not yet seen are necessary.")


def response_text(event) -> str:
    response = event.get("tool_response")
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("output", "stdout", "text"):
            if isinstance(response.get(key), str):
                return response[key]
    return json.dumps(response) if response is not None else ""


def intents_of(event) -> list:
    """Every intent in one tool call: Codex batches reads into a single shell command."""
    if event.get("tool_name") == "Bash":
        return shellparse.parse_all(shellparse.command_of(event), event.get("cwd") or "")
    return [intent_of(event)]


def _call_key(event) -> str:
    """One key for the whole call, so an override repeats the command the model actually ran."""
    return (event.get("cwd") or "") + "\0" + shellparse.command_text(shellparse.command_of(event))[:2000]


def intent_of(event):
    """Turn a gated tool call into a read, search or other intent."""
    tool, cwd = event.get("tool_name"), event.get("cwd") or ""
    tool_input = event.get("tool_input") or {}
    if tool == "Bash":
        return shellparse.parse(shellparse.command_of(event), cwd)
    if not isinstance(tool_input, dict):
        return shellparse.OTHER
    if tool == "Read" and tool_input.get("file_path"):
        offset, limit = tool_input.get("offset"), tool_input.get("limit")
        start = offset if isinstance(offset, int) and offset > 0 else None
        end = (start or 1) + limit - 1 if isinstance(limit, int) and limit > 0 else None
        return shellparse.Intent("read", os.path.normpath(os.path.join(cwd, tool_input["file_path"])), start, end,
                                 cwd + "\0Read " + json.dumps(tool_input, sort_keys=True), tool="Read")
    if tool in ("Grep", "Glob"):
        return shellparse.Intent("search", key=cwd + "\0" + tool + " " + json.dumps(tool_input, sort_keys=True),
                                 tool=tool)
    return shellparse.OTHER


def _read_span(event):
    response = event.get("tool_response")
    file = response.get("file") if isinstance(response, dict) else None
    if not isinstance(file, dict) or file.get("truncatedByTokenCap"):
        return None
    start, count = file.get("startLine") or 1, file.get("numLines") or 0
    return (start, start + count - 1) if count > 0 else None


def _file_info(path):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 20_000_000:
            return None
        stat = os.stat(path)
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return {"size": stat.st_size, "mtime": stat.st_mtime, "lines": lines, "data": data}


def _span(intent, lines):
    start = 1 if intent.start is None else intent.start
    if start < 0:
        start = max(1, lines + start + 1)
    end = lines if intent.end is None or intent.end > lines else intent.end
    return start, end


def _span_tokens(data: bytes, start: int, end: int) -> int:
    return config.tokens(b"\n".join(data.split(b"\n")[start - 1:end]).decode("utf-8", "replace"))


def _command_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _rules(intent, s, settings):
    """Deterministic rules. Returns (rule, reason, tokens, path) or None."""
    if intent.kind == "search":
        for past in s["searches"]:
            if past["key"] == intent.key and past["edits"] == s["edits"]:
                return ("duplicate_search", "This exact search already ran at step %d and nothing has changed "
                        "since. Use that output." % past["step"], past.get("tokens", 0), None)
        return None
    info = _file_info(intent.path)
    if info is None:
        return None
    start, end = _span(intent, info["lines"])
    estimate = _span_tokens(info["data"], start, end)
    if intent.tool != "Read":  # Claude Code already dedupes its own Read of unchanged files
        for past in s["reads"]:
            if (past["path"] == intent.path and past["size"] == info["size"] and past["mtime"] == info["mtime"]
                    and past["start"] <= start and past["end"] >= end):
                return ("duplicate_read", "%s lines %d-%d are already in your context from step %d and the file is "
                        "unchanged. Use that output." % (intent.path, start, end, past["step"]), estimate, intent.path)
    whole = intent.start is None and intent.end is None
    if whole and (info["lines"] > settings["huge_file_lines"] or estimate > settings["huge_file_tokens"]):
        return ("huge_read", "%s has %d lines (~%d tokens). Read only the part you need with sed -n "
                "'START,ENDp', or find it first with rg or jev_search." % (intent.path, info["lines"], estimate),
                estimate, intent.path)
    return None


def _jev_rule(intent, snapshot, settings):
    command = intent.key.split("\0", 1)[1]
    answers = jevclient.ask({"task": snapshot["task"][:2000], "recent_commands": snapshot["recent"],
                             "proposed_command": command[:500]},
                            {"unnecessary": {"type": "noul", "instructions": JEV_QUESTION}})
    p = jevclient.noul(answers, "unnecessary")
    if p is None or p < settings["gate_jev_threshold"]:
        return None
    estimate = 0
    if intent.kind == "read":
        info = _file_info(intent.path)
        if info:
            estimate = _span_tokens(info["data"], *_span(intent, info["lines"]))
    return ("jev_unnecessary", "Jev judged this command unnecessary for the task (p=%.2f): the information "
            "looks already gathered or irrelevant." % p, estimate, intent.path)


def _count(s, tool, kind):
    """Tally what the gate was shown. Without it, 'saw nothing' and 'matched nothing' look identical."""
    for field, value in (("gate_seen", tool), ("gate_kinds", kind)):
        tally = dict(s.get(field) or {})
        tally[value] = tally.get(value, 0) + 1
        s[field] = tally


def pre_tool_use(event, settings):
    if not settings["enabled"] or settings["gate"] == "off":
        return None
    tool = event.get("tool_name") or "?"
    intents = intents_of(event) if tool in GATED else []
    actionable = [i for i in intents if i.kind != "other"]
    sid = event.get("session_id") or ""
    key = _call_key(event) if tool == "Bash" else (actionable[0].key if actionable else "")
    with state.session(sid) as s:
        _count(s, tool, _label(intents, actionable))
        if not actionable:
            # Keep a few unrecognised commands so the parser can be fixed against what actually arrives.
            if intents:  # keep the most recent, so the samples track the parser as it improves
                samples = list(s.get("gate_unparsed") or [])
                s["gate_unparsed"] = (samples + [shellparse.command_text(shellparse.command_of(event))[:200]])[-5:]
            return None
        s["step"] += 1
        if key in s["denied"]:
            s["denied"].remove(key)
            state.log({"kind": "override", "session": sid, "cmd": _command_hash(key)})
            return None
        verdicts = [_rules(intent, s, settings) for intent in actionable]
        snapshot = {"task": s["task"], "recent": s["recent"][-15:]}
    verdict = _combine(verdicts)
    if verdict is None and len(actionable) == 1 and snapshot["task"]:
        verdict = _jev_rule(actionable[0], snapshot, settings)  # one Jev call per call, never per batch part
    if verdict is None:
        return None
    rule, reason, estimate, path = verdict
    state.log({"kind": "gate", "session": sid, "tool_use_id": event.get("tool_use_id"), "rule": rule,
               "mode": settings["gate"], "tokens": estimate, "path": path, "cmd": _command_hash(key),
               "parts": len(actionable)})
    if settings["gate"] != "on":
        return None
    with state.session(sid) as s:
        s["denied"] = (s["denied"] + [key])[-50:]
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "[jev gate] " + reason + " If you really need it, run the same command again.",
    }}


def _label(intents, actionable) -> str:
    if not intents:
        return "not gated"
    if not actionable:
        return "other"
    return "batch" if len(actionable) > 1 else actionable[0].kind


def _combine(verdicts):
    """A call is only redundant when every recognised part of it is."""
    if not verdicts or any(verdict is None for verdict in verdicts):
        return None
    rules = {verdict[0] for verdict in verdicts}
    rule = verdicts[0][0] if len(rules) == 1 else "redundant_batch"
    reason = verdicts[0][1]
    if len(verdicts) > 1:
        reason += " Every one of the %d parts of this command is already covered." % len(verdicts)
    return rule, reason, sum(verdict[2] for verdict in verdicts), verdicts[0][3]


def _edited_paths(event):
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or ""
    if event.get("tool_name") == "apply_patch":
        names = PATCH_FILE.findall(tool_input.get("command") or "")
    else:
        names = [tool_input.get("file_path") or tool_input.get("notebook_path") or ""]
    return {os.path.normpath(os.path.join(cwd, name.strip())) for name in names if name.strip()}


def _flag(sid, paths):
    if not paths:
        return
    events = state.events(sid)
    flagged = {e.get("ref") for e in events if e.get("kind") == "flag"}
    for event in events:
        if event.get("kind") == "gate" and event.get("path") in paths and event.get("tool_use_id") not in flagged:
            state.log({"kind": "flag", "session": sid, "ref": event.get("tool_use_id"), "path": event["path"]})


def _log_use(sid, event):
    tool_input = event.get("tool_input") or {}
    if event.get("tool_name") == "Skill":
        name = tool_input.get("skill") or tool_input.get("name") or ""
        state.log({"kind": "skill_used", "session": sid, "skill": name, "path": name})
    else:
        state.log({"kind": "delegated", "session": sid, "agent": tool_input.get("subagent_type") or "",
                   "model": tool_input.get("model") or ""})


def record(event, output: str) -> None:
    """PostToolUse bookkeeping: what the model has seen, and whether anything changed."""
    sid = event.get("session_id") or ""
    tool = event.get("tool_name")
    if tool in EDIT_TOOLS:
        with state.session(sid) as s:
            s["edits"] += 1
        _flag(sid, _edited_paths(event))
        return
    if tool in ("Skill", "Agent", "Task"):
        _log_use(sid, event)
        return
    if tool not in GATED:
        return
    intents = intents_of(event)
    actionable = [intent for intent in intents if intent.kind != "other"]
    skill_read = None
    with state.session(sid) as s:
        if len(actionable) < len(intents):
            s["edits"] += 1  # an unrecognised part of the call may change files
        if not actionable:
            return
        for intent in actionable:
            s["recent"] = (s["recent"] + [intent.key.split("\0", 1)[1][:300]])[-15:]
            if intent.kind == "search":
                s["searches"] = (s["searches"] + [{"key": intent.key, "edits": s["edits"], "step": s["step"],
                                                   "tokens": config.tokens(output) // len(actionable)}])[-100:]
                continue
            if intent.path.endswith("SKILL.md"):
                skill_read = intent.path
            info = _file_info(intent.path)
            span = None
            if info and intent.tool == "Read":
                span = _read_span(event)
            elif info and not TRUNCATED.search(output):
                span = _span(intent, info["lines"])
            if span:
                s["reads"] = (s["reads"] + [{"path": intent.path, "start": span[0], "end": span[1],
                                             "size": info["size"], "mtime": info["mtime"],
                                             "step": s["step"]}])[-200:]
    if skill_read:
        state.log({"kind": "skill_read", "session": sid, "path": skill_read})


def compacted(event) -> None:
    with state.session(event.get("session_id") or "") as s:
        s["reads"] = []
        s["searches"] = []
