"""Codex edition: Jev picks a model tier for each turn (the local router proxy applies it) and a matching skill."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from . import config, jevclient, meter, skills, state

TIERS = {
    "1": "Trivial: a question answerable from context, a rename, a one-line change, or running a command.",
    "2": "Routine: a small feature, tests, or a clear bug fix in one or two files.",
    "3": "Moderate: multi-file changes or refactors that need some design judgment.",
    "4": "Hard: tricky debugging, cross-cutting design, or unfamiliar code.",
    "5": "Hardest: novel algorithms, deep multi-system reasoning, or high-stakes changes.",
}
TIER_QUESTION = "How capable a model does the work in this request need?"
CORRECTION_QUESTION = ("The request says the last answer was wrong, incomplete or missed the point, or asks for "
                       "work that was just done to be redone.")
FOLLOWUP_QUESTION = ("The request is a short follow-up, confirmation or continuation of the previous request "
                     "(for example 'yes', 'go ahead', 'do that').")
MARK = "# jev-router"
HORIZON, OUTPUT_TOKENS = 4, 2000  # cache-guard assumptions: requests per turn, output tokens per request


def codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"


def router_installed() -> bool:
    try:
        return MARK in codex_config().read_text()
    except OSError:
        return False


def router_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def router_status(settings) -> str:
    record = config.read_json(config.home() / "router-install.json", {}) or {}
    port = record.get("port") or settings["router_port"]
    if not router_installed():
        return ("Router: not installed, so Jev's model picks are only shown "
                "(install: python3 scripts/codex_router.py install)")
    return "Router: installed %s on :%d" % ("and running" if router_running(port) else "but not responding", port)


def last_input_tokens(path: str) -> int:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 1_000_000))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            info = (json.loads(line).get("payload") or {}).get("info") or {}
        except ValueError:
            continue
        tokens = (info.get("last_token_usage") or {}).get("input_tokens")
        if tokens:
            return int(tokens)
    return 0


def switch_pays(current_rate, new_rate, tokens: int) -> bool:
    """Is moving to a cheaper model worth re-reading the conversation without a cache hit?"""
    stay = HORIZON * (tokens * current_rate[1] + OUTPUT_TOKENS * current_rate[2])
    switch = (tokens * new_rate[0] + (HORIZON - 1) * tokens * new_rate[1]
              + HORIZON * OUTPUT_TOKENS * new_rate[2])
    return switch < stay


def choose(answers, session, requested, tokens, settings):
    key, confidence = jevclient.choice(answers, "tier")
    followup = jevclient.noul(answers, "followup") or 0.0
    if followup >= 0.7 and session.get("codex_tier"):
        tier, confidence = session["codex_tier"], max(confidence or 0.0, followup)
    elif key in TIERS and confidence >= settings["route_threshold"]:
        tier = int(key)
    else:
        return None, confidence
    tier = max(settings["codex_min_tier"], min(settings["codex_max_tier"], tier))
    model, effort = settings["codex_tiers"][tier - 1]
    current = session.get("codex_model") or requested
    rates = settings["rates"]
    current_rate, new_rate = meter.rate_for(current, rates), meter.rate_for(model, rates)
    requested_rate = meter.rate_for(requested, rates)
    if (new_rate and requested_rate and new_rate[0] > requested_rate[0]
            and confidence < settings["route_up_threshold"]):
        state.log({"kind": "route_skipped", "reason": "upgrade_confidence", "model": model, "tier": tier,
                   "confidence": round(confidence, 3)})
        return None, confidence
    if current_rate is None and model != current and (tier > 3 or new_rate is None):
        # A model with no rate is one we have never priced (newly released). We cannot tell whether the
        # tier's model is cheaper or weaker, so only routine work moves, and never onto an older flagship.
        state.log({"kind": "route_skipped", "reason": "unpriced_model", "model": current, "tier": tier})
        return None, confidence
    guarded = bool(current and model != current and current_rate and new_rate and new_rate[0] < current_rate[0]
                   and not switch_pays(current_rate, new_rate, tokens))
    if guarded:
        model = current
    return {"tier": tier, "model": model, "effort": effort, "confidence": round(confidence, 3),
            "guarded": guarded}, confidence


def on_prompt(event, settings, prompt: str):
    sid, turn = event.get("session_id") or "", event.get("turn_id") or ""
    requested = event.get("model") or ""
    transcript = event.get("transcript_path") or ""
    session = state.read(sid)
    catalog = session["skills"] or (skills.catalog_from_transcript(transcript) if settings["skills"] else [])
    names = [entry["name"] for entry in catalog or []]
    command = prompt.lstrip()[:1] in ("/", "$")
    questions = {}
    if settings["route"] != "off" and not command and not event.get("agent_id"):  # the proxy skips subagents
        questions["tier"] = {"type": "choice", "criteria": TIERS, "instructions": TIER_QUESTION}
        questions["followup"] = {"type": "noul", "instructions": FOLLOWUP_QUESTION}
        questions["correction"] = {"type": "noul", "instructions": CORRECTION_QUESTION}
    if settings["skills"] and names and not command and not skills.mentioned(prompt, names):
        questions["skill"] = {"type": "choice", "criteria": skills.criteria(catalog), "instructions": skills.QUESTION}
    tokens = last_input_tokens(transcript) if "tier" in questions else 0
    answers = None
    if questions:
        answers = jevclient.ask({"request": prompt[:3000], "previous_request": session["task"][:1500],
                                 "previous_tier": str(session.get("codex_tier") or "none")}, questions, timeout=3)
    if questions and answers is None:
        # Jev could not be reached or refused: say so in the log, or a silent no-route looks like a no-op.
        state.log({"kind": "route_skipped", "reason": "no_answer", "session": sid, "turn": turn,
                   "asked": sorted(questions), "keys": sorted(config.credentials())})
    decision, confidence = choose(answers, session, requested, tokens, settings) if "tier" in questions else (None, None)
    if "tier" in questions and answers is not None and not decision:
        # Jev answered but we are not acting on it. Without this the turn looks identical to one
        # where nothing was ever asked, which makes a quiet router impossible to diagnose.
        raw, raw_confidence = jevclient.choice(answers, "tier")
        state.log({"kind": "route_skipped", "reason": "low_confidence", "session": sid, "turn": turn,
                   "tier": raw, "confidence": round(raw_confidence or 0.0, 3),
                   "threshold": settings["route_threshold"], "requested": requested})
    # Did the previous turn's model earn a complaint? The honest quality signal for routing down.
    correction = jevclient.noul(answers, "correction") or 0.0
    if correction >= 0.6 and session.get("codex_last_model"):
        state.log({"kind": "correction", "session": sid, "turn": turn, "confidence": round(correction, 3),
                   "after_model": session["codex_last_model"], "after_tier": session.get("codex_tier") or 0,
                   "after_routed": bool(session.get("codex_last_routed"))})
    applied = bool(decision) and settings["route"] == "on" and router_installed()
    notice = None
    if decision:
        state.log({"kind": "route", "session": sid, "turn": turn, "tier": decision["tier"],
                   "model": decision["model"], "effort": decision["effort"], "confidence": decision["confidence"],
                   "requested": requested, "guarded": decision["guarded"], "applied": applied, "tokens": tokens})
        suffix = "" if applied else (" [shadow]" if settings["route"] != "on" else " [router not installed]")
        notice = "jev → %s · %s (tier %d, %.2f)%s%s" % (
            decision["model"], decision["effort"], decision["tier"], confidence,
            " · kept for cache" if decision["guarded"] else "", suffix)
    hint = skills.pick(answers, catalog, settings, sid) if "skill" in questions else None
    with state.session(sid) as s:
        s["task"] = prompt[:4000]
        s["cwd"] = event.get("cwd") or s["cwd"]
        s["skills"] = catalog or s["skills"]
        if decision:
            s["codex_tier"] = decision["tier"]
        if "tier" in questions:
            s["codex_model"] = decision["model"] if applied else requested
            s["codex_last_model"] = s["codex_model"]
            s["codex_last_routed"] = applied
        if applied:
            routes = dict(s.get("codex_routes") or {})
            routes[turn] = {"model": decision["model"], "effort": decision["effort"], "tier": decision["tier"]}
            s["codex_routes"] = dict(list(routes.items())[-50:])
    output = {}
    if hint:
        output["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": hint}
    if notice:
        output["systemMessage"] = notice
    return output or None
