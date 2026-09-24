"""Skill router: have Jev pick the one listed skill that fits the prompt, if any."""
from __future__ import annotations

import json
import os
import re

from . import jevclient, state

ROOT_LINE = re.compile(r"^- `(r\d+)` = `(.+)`$")
ENTRY_LINE = re.compile(r"^- (\S+): (.*) \(file: (r\d+)/(.+SKILL\.md)\)$")
QUESTION = "Which skill should be used to carry out this request? Choose none unless one skill clearly applies."
SCAN_LINES = 400


def parse_block(block: str) -> list:
    roots, found = {}, []
    for line in block.splitlines():
        line = line.strip()
        match = ROOT_LINE.match(line)
        if match:
            roots[match.group(1)] = match.group(2)
            continue
        match = ENTRY_LINE.match(line)
        if match and match.group(3) in roots:
            found.append({"name": match.group(1), "description": match.group(2).strip(),
                          "path": os.path.join(roots[match.group(3)], match.group(4))})
    return found


def catalog_from_transcript(path: str) -> list:
    try:
        handle = open(path)
    except OSError:
        return []
    with handle:
        for number, line in enumerate(handle):
            if number >= SCAN_LINES:
                break
            if "<skills_instructions>" not in line:
                continue
            try:
                content = (json.loads(line).get("payload") or {}).get("content") or []
            except ValueError:
                continue
            for part in content:
                text = part.get("text", "") if isinstance(part, dict) else ""
                start = text.find("<skills_instructions>")
                if start >= 0:
                    end = text.find("</skills_instructions>", start)
                    return parse_block(text[start:end if end > 0 else len(text)])
    return []


def route(event, settings, prompt: str):
    if not (settings["enabled"] and settings["skills"]):
        return None
    sid = event.get("session_id") or ""
    catalog = state.read(sid)["skills"]
    if not catalog:
        catalog = catalog_from_transcript(event.get("transcript_path") or "")
        if not catalog:
            return None
        with state.session(sid) as s:
            s["skills"] = catalog
    if mentioned(prompt, [skill["name"] for skill in catalog]):
        return None
    answers = jevclient.ask({"request": prompt[:3000]},
                            {"skill": {"type": "choice", "criteria": criteria(catalog), "instructions": QUESTION}})
    return pick(answers, catalog, settings, sid)


def criteria(catalog) -> dict:
    found = {skill["name"]: (skill["description"] or skill["name"])[:300] for skill in catalog[:254]}
    found["none"] = "No listed skill clearly applies to this request."
    return found


def pick(answers, catalog, settings, sid):
    """Turn Jev's skill answer into a Codex hint (and a log entry), or None."""
    picked, confidence = jevclient.choice(answers, "skill")
    names = [skill["name"] for skill in catalog]
    if picked not in names or confidence < settings["skill_threshold"]:
        return None
    path = next(skill["path"] for skill in catalog if skill["name"] == picked)
    state.log({"kind": "skill", "session": sid, "skill": picked, "confidence": round(confidence, 3), "path": path})
    return ("[jev] The `%s` skill matches this request (confidence %.2f): read %s and follow it unless it "
            "clearly does not fit." % (picked, confidence, path))


CLAUDE_ENTRY = re.compile(r"^- (\S+?)(?:: (.*))?$")
OWN_SKILLS = {"jev", "jev:jev", "jev:lite", "jev:mid"}


def mentioned(prompt: str, names) -> bool:
    return any(re.search(r"[$/@]" + re.escape(name) + r"(?![\w:-])", prompt) for name in names)


def catalog_from_claude_transcript(path: str) -> list:
    """Parse the newest skill_listing attachment (Claude Code transcripts): '- name: description' lines."""
    latest = None
    try:
        handle = open(path)
    except OSError:
        return []
    with handle:
        for line in handle:
            if '"skill_listing"' not in line:
                continue
            try:
                attachment = json.loads(line).get("attachment") or {}
            except ValueError:
                continue
            if isinstance(attachment, dict) and attachment.get("type") == "skill_listing":
                latest = attachment.get("content") or ""
    found = []
    for line in (latest or "").splitlines():
        match = CLAUDE_ENTRY.match(line.strip())
        if match and match.group(1) not in OWN_SKILLS:
            found.append({"name": match.group(1), "description": (match.group(2) or "").strip(), "path": None})
    return found
