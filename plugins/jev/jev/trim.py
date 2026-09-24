"""PostToolUse trimmer: condense large command output to the lines that matter."""
from __future__ import annotations

import json
import re

from . import config, jevclient, shellparse, state

KEEP = re.compile(r"error|fail|panic|exception|traceback|assert|warn|fatal|denied|not found|✗|✘", re.I)
NO_TRIM = re.compile(r"^\s*git\s+(?:diff|show)\b|^\s*git\s+log\b.*\s-p\b")
CHUNK, HEAD, TAIL, CONTEXT = 20, 10, 30, 2
MAX_CHUNKS, CHUNK_CHARS, MIN_SCORE = 60, 800, 0.3
QUESTION = ("For chunk id {i}: this part of the command output contains information needed to act on the "
            "result for the task, such as errors, failing tests, requested values or key results.")


def _is_json(output: str) -> bool:
    text = output.strip()
    if text[:1] not in ("{", "["):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _cost(lines, indexes) -> int:
    return sum(config.tokens(lines[i]) + 1 for i in indexes)


def _render(lines, keep) -> str:
    out, previous = [], -1
    for i in sorted(keep):
        if i > previous + 1:
            out.append("… %d lines omitted …" % (i - previous - 1))
        out.append(lines[i])
        previous = i
    if previous < len(lines) - 1:
        out.append("… %d lines omitted …" % (len(lines) - 1 - previous))
    return "\n".join(out)


def _safe(name) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in (name or "unknown"))[:128]


def _save(event, output) -> str:
    target = config.home() / "outputs" / _safe(event.get("session_id")) / (_safe(event.get("tool_use_id")) + ".txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(output)
    return str(target)


def post_tool_use(event, settings, output: str):
    if not (settings["enabled"] and settings["trim"]) or event.get("tool_name") != "Bash":
        return None
    total = config.tokens(output)
    command = shellparse.command_of(event)
    text = shellparse.command_text(command)
    if (total < settings["trim_min_tokens"] or NO_TRIM.search(text) or _is_json(output)
            or shellparse.parse(command, event.get("cwd") or "").kind == "read"):
        return None
    lines = output.splitlines()
    keep = set(range(min(HEAD, len(lines)))) | set(range(max(0, len(lines) - TAIL), len(lines)))
    for i, line in enumerate(lines):
        if KEEP.search(line):
            keep.update(range(max(0, i - CONTEXT), min(len(lines), i + CONTEXT + 1)))
    budget = settings["trim_budget_tokens"] - _cost(lines, keep)
    chunks = []
    for start in range(0, len(lines), CHUNK):
        rest = [i for i in range(start, min(start + CHUNK, len(lines))) if i not in keep]
        if rest:
            chunks.append(rest)
    chunks = chunks[:MAX_CHUNKS]
    if chunks and budget > 0:
        task = state.read(event.get("session_id") or "")["task"]
        answers = jevclient.ask(
            {"task": task[:2000], "command": text[:500],
             "chunks": [{"id": str(n), "text": "\n".join(lines[i] for i in c)[:CHUNK_CHARS]}
                        for n, c in enumerate(chunks)]},
            {str(n): {"type": "noul", "instructions": QUESTION.format(i=n)} for n in range(len(chunks))},
            timeout=5)
        if answers is None:
            return None
        ranked = sorted(((jevclient.noul(answers, str(n)) or 0.0, n) for n in range(len(chunks))), reverse=True)
        for score, n in ranked:
            cost = _cost(lines, chunks[n])
            if score >= MIN_SCORE and cost <= budget:
                keep.update(chunks[n])
                budget -= cost
    shown = _render(lines, keep)
    shown_tokens = config.tokens(shown)
    if shown_tokens >= total * 0.8:
        return None
    saved = _save(event, output)
    state.log({"kind": "trim", "session": event.get("session_id"), "tool_use_id": event.get("tool_use_id"),
               "before": total, "after": shown_tokens})
    header = ("[jev trim] The command ran; its output was condensed from ~%d to ~%d tokens. Exit status is not "
              "available here, so rely on the summary lines. Captured output: %s (read ranges with sed -n)."
              % (total, shown_tokens, saved))
    return {"decision": "block", "reason": header + "\n" + shown}
