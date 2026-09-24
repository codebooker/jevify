"""Claude edition: one batched Jev call per prompt for tier routing, skill choice and the two advisors."""
from __future__ import annotations

import json
import re

from . import apps, config, jevclient, skills, state

TIERS = {
    "direct": "A question, or a small change needing only a few tool calls in one or two files: quickest to just do.",
    "lite": "Mechanical work spanning several files or many steps: bulk renames, formatting, moving code, running a "
            "series of commands.",
    "mid": "Routine engineering spanning several files or steps: a contained feature, a bug fix with a clear cause, "
           "or tests.",
    "scout": "A question about where or how something works in this codebase or its docs that needs broad "
             "exploration: reading many files or following code across modules, more than a quick search; no edits.",
    "full": "Hard, ambiguous or high-stakes: design, tricky debugging, multi-system changes, careful reasoning.",
}
TIER_QUESTION = "Which cost tier fits the work this request needs?"
FOLLOWUP_QUESTION = ("The request is a short follow-up, confirmation or continuation of the previous request "
                     "(for example 'yes', 'go ahead', 'do that').")
UNRELATED_QUESTION = "The request starts a new task unrelated to the previous request and the conversation so far."
TIER_MODEL = {"lite": "haiku", "mid": "sonnet", "scout": "haiku"}
TIER_LABEL = {"lite": "Haiku helper", "mid": "Sonnet worker", "scout": "Haiku scout", "direct": "direct"}
ROUTINE = {"direct", "lite", "mid", "scout"}
# Subagents with a pinned model are the only verified way to move work to a cheaper model inside the Claude app:
# a skill's `model` is ignored there, whether the model or the user invokes it (checked 2026-09-22, app 2.1.275).
_DELEGATE = ("[jev] %s task: delegate it to the `%s` subagent (%s) with a complete, self-contained brief (files, "
             "the exact change, how to verify), then report its result briefly. Trust its report; only re-check if "
             "it reports a problem. Do it yourself only if it depends on context from this conversation that won't "
             "fit in a brief.")
HINTS = {
    "lite": _DELEGATE % ("Multi-step mechanical", "jev:helper", "Haiku"),
    "mid": _DELEGATE % ("Multi-step routine", "jev:worker", "Sonnet"),
    "scout": "[jev] This needs broad searching or reading. Delegate it to the `jev:scout` subagent (Haiku) with a "
             "precise brief of what to find, then answer from its report. Only read files yourself if the report "
             "leaves a specific gap.",
    "direct": "[jev] Quick task: answer or do it directly with as few tool calls as possible.",
}
MANUAL_TIER = re.compile(r"^\s*/(?:jev:)?(lite|mid)\b")
ADVISOR_MIN_CONTEXT = 60_000
ADVICE_RUN, ADVICE_GAP = 5, 10  # suggest a model switch after 5 routine prompts, at most once per 10 prompts


def _k(n) -> str:
    return "%.0fK" % (n / 1000.0) if n >= 1000 else str(int(n))


def conversation_info(path: str):
    """(context tokens, model) of the newest main-thread response in the transcript."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 1_000_000))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0, ""
    for line in reversed(lines):
        if '"assistant"' not in line or '"usage"' not in line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("type") != "assistant" or item.get("isSidechain"):
            continue
        message = item.get("message") or {}
        usage = message.get("usage") or {}
        total = sum(int(usage.get(k) or 0) for k in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if total and not (message.get("model") or "").startswith("<"):
            return total, message.get("model") or ""
    return 0, ""


def choose_tier(answers, previous_tier, session_model, settings):
    """Returns (tier to act on, confidence, routine class for the model advisor)."""
    tier, confidence = jevclient.choice(answers, "tier")
    if tier not in TIERS:
        return None, None, None
    if (jevclient.noul(answers, "followup") or 0.0) >= 0.7 and previous_tier in TIERS:
        tier = previous_tier
    if confidence < settings["route_threshold"]:
        return "full", confidence, tier  # too unsure to hint, but the kind of work still informs model advice
    target = TIER_MODEL.get(tier)
    if target and apps.model_rank(target) >= apps.model_rank(session_model):
        return "full", confidence, tier
    return tier, confidence, tier


def repo_nodes(cwd: str) -> int:
    """Size of the session repository's code graph (0 when it has not been indexed yet)."""
    if not cwd:
        return 0
    meta = config.read_json(config.index_dir(config.repo_root(cwd)) / "meta.json", {}) or {}
    return int(meta.get("nodes") or 0)


def model_advice(history, tier, confidence, session_model, prompts, advised_at):
    """User-facing advice to switch the session model in the app's picker; the model itself can't switch."""
    if advised_at and prompts - advised_at < ADVICE_GAP:
        return None
    rank = apps.model_rank(session_model)
    if rank >= 3 and len(history) >= ADVICE_RUN and all(t in ROUTINE for t in history[-ADVICE_RUN:]):
        return ("sonnet", "[jev] Your last %d prompts were routine: switching this session to Sonnet in the model "
                "picker would cost about %s less per request." % (ADVICE_RUN, "60%" if rank == 3 else "80%"))
    if rank <= 2 and tier == "full" and confidence is not None and confidence >= 0.8:
        return ("opus", "[jev] This looks like hard work for %s: consider Opus in the model picker for it."
                % ("Sonnet" if rank == 2 else "Haiku"))
    return None


def on_prompt(event, settings, prompt: str):
    sid = event.get("session_id") or ""
    transcript = event.get("transcript_path") or ""
    tokens, answering_model = conversation_info(transcript)
    session = state.read(sid)
    session_model = answering_model or session["model"] or "claude-opus-5"
    catalog = session["skills"]
    if not catalog and settings["skills"]:
        catalog = skills.catalog_from_claude_transcript(transcript)
    names = [entry["name"] for entry in catalog or []]
    command = prompt.lstrip().startswith("/")  # an explicit slash command already says what to do
    if MANUAL_TIER.match(prompt):
        state.log({"kind": "manual_tier", "session": sid, "tier": MANUAL_TIER.match(prompt).group(1)})
    questions = {}
    if settings["route"] != "off" and not command:
        questions["tier"] = {"type": "choice", "criteria": TIERS, "instructions": TIER_QUESTION}
        questions["followup"] = {"type": "noul", "instructions": FOLLOWUP_QUESTION}
    if settings["advisor"] and tokens >= ADVISOR_MIN_CONTEXT and session["task"] and not command:
        questions["unrelated"] = {"type": "noul", "instructions": UNRELATED_QUESTION}
    if settings["skills"] and names and not command and not skills.mentioned(prompt, names):
        criteria = {entry["name"]: (entry["description"] or entry["name"])[:200] for entry in catalog[:254]}
        criteria["none"] = "No listed skill clearly applies to this request."
        questions["skill"] = {"type": "choice", "criteria": criteria, "instructions": skills.QUESTION}
    answers = None
    if questions:
        answers = jevclient.ask({"request": prompt[:3000], "previous_request": session["task"][:1500],
                                 "previous_tier": session["tier"] or "none", "conversation_tokens": tokens},
                                questions, timeout=3)
    hints, notices = [], []
    tier, confidence, routine = (None, None, None)
    if "tier" in questions:
        tier, confidence, routine = choose_tier(answers, session["tier"], session_model, settings)
    if tier == "scout" and repo_nodes(event.get("cwd") or session["cwd"]) < settings["scout_min_nodes"]:
        tier = "direct"  # measured 2026-09-22: a scout's ~$0.12 startup costs more than it saves on small repos
    if tier:
        applied = settings["route"] == "on" and tier in HINTS
        state.log({"kind": "route", "session": sid, "tier": tier, "choice": jevclient.choice(answers, "tier")[0],
                   "confidence": round(confidence, 3), "mode": settings["route"], "tokens": tokens,
                   "model": session_model})
        if applied:
            hints.append(HINTS[tier])
        if tier in TIER_LABEL:
            notices.append("jev → %s (%s, %.2f)%s" % (TIER_LABEL[tier], tier, confidence,
                                                     "" if applied else " [shadow]"))
    picked, skill_confidence = jevclient.choice(answers, "skill")
    if picked in names and skill_confidence >= settings["skill_threshold"] and tier != "direct":
        hints.append("[jev] The `%s` skill matches this request (confidence %.2f): invoke it with the Skill tool "
                     "unless it clearly does not fit." % (picked, skill_confidence))
        state.log({"kind": "skill", "session": sid, "skill": picked, "confidence": round(skill_confidence, 3),
                   "path": picked})
    unrelated = jevclient.noul(answers, "unrelated")
    if unrelated is not None and unrelated >= 0.8:
        notices.append("[jev] New topic with ~%s tokens of earlier context. A new session would skip re-reading "
                       "that on every request." % _k(tokens))
        state.log({"kind": "advisor", "session": sid, "tokens": tokens})
    with state.session(sid) as s:
        s["task"] = prompt[:4000]
        s["cwd"] = event.get("cwd") or s["cwd"]
        s["skills"] = catalog or s["skills"]
        s["prompts"] = s.get("prompts", 0) + 1
        if tier:
            s["tier"] = tier
        if routine:
            s["tiers"] = (s.get("tiers", []) + [routine])[-20:]
            advice = model_advice(s["tiers"], routine, confidence, session_model, s["prompts"], s.get("advised_at"))
            if advice:
                s["advised_at"] = s["prompts"]
                notices.append(advice[1])
                state.log({"kind": "model_advice", "session": sid, "to": advice[0], "from": session_model})
    output = {}
    if hints:
        output["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(hints)}
    if notices:
        output["systemMessage"] = " · ".join(notices)
    return output or None
