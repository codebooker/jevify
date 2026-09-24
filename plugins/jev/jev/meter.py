"""Usage meter: credits and plan windows from the Codex transcript; savings from the jev log."""
from __future__ import annotations

import glob
import json
import os
import time

from . import apps, state

RULE_LABELS = {"duplicate_read": "Duplicate read (unchanged file)", "huge_read": "Huge whole-file read",
               "duplicate_search": "Duplicate search", "jev_unnecessary": "Jev: unnecessary for task",
               "redundant_batch": "Batched command, every part redundant"}


def _k(n) -> str:
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(int(n))


def rate_for(model, rates):
    for name in sorted(rates, key=len, reverse=True):
        if model and model.startswith(name):
            return rates[name]
    return None


def scan(transcript_path: str, rates: dict) -> dict:
    result = {"credits": 0.0, "requests": 0, "unpriced": 0, "models": {}, "windows": {}}
    model = None
    try:
        handle = open(transcript_path)
    except OSError:
        return result
    with handle:
        for line in handle:
            if '"turn_context"' not in line and '"token_usage_record"' not in line and '"token_count"' not in line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            kind, payload = item.get("type"), item.get("payload") or {}
            if kind == "turn_context":
                model = payload.get("model") or model
            elif kind == "token_usage_record":
                usage = payload.get("usage") or {}
                result["requests"] += 1
                rate = rate_for(model, rates)
                if rate is None:
                    result["unpriced"] += 1
                    continue
                total = usage.get("input_tokens", 0)
                cached = min(usage.get("cached_input_tokens", 0), total)
                credits = ((total - cached) * rate[0] + cached * rate[1] + usage.get("output_tokens", 0) * rate[2]) / 1e6
                result["credits"] += credits
                result["models"][model] = result["models"].get(model, 0.0) + credits
            elif kind == "event_msg" and payload.get("type") == "token_count":
                # Codex reports several limit buckets; keep the newest snapshot of each one that has a window.
                limits = payload.get("rate_limits") or {}
                if limits.get("primary") or limits.get("secondary"):
                    result["windows"][limits.get("limit_id") or "codex"] = limits
    return result


def _format_bucket(limits) -> str:
    parts = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        minutes = window.get("window_minutes")
        label = {300: "5h", 10080: "weekly"}.get(minutes, "%dmin" % minutes if minutes else key)
        text = "%s %.0f%% used" % (label, window["used_percent"])
        if window.get("resets_at"):
            text += " (resets %s)" % time.strftime("%a %H:%M", time.localtime(window["resets_at"]))
        parts.append(text)
    return " · ".join(parts)


def format_windows(buckets) -> str:
    """Format {limit_id: rate_limits}; the main codex bucket is shown without a prefix."""
    parts = []
    for limit_id, limits in sorted((buckets or {}).items(), key=lambda kv: kv[0] != "codex"):
        text = _format_bucket(limits) if isinstance(limits, dict) else ""
        if text:
            parts.append(text if limit_id == "codex" else "%s: %s" % (limit_id, text))
    return " | ".join(parts)


def savings_line(session_id: str) -> str:
    events = state.events(session_id)
    trims = [e for e in events if e.get("kind") == "trim"]
    gates = [e for e in events if e.get("kind") == "gate"]
    suggested = [e for e in events if e.get("kind") == "skill"]
    read_paths = {e.get("path") for e in events if e.get("kind") in ("skill_read", "skill_used")}
    parts = ["trim ~%s tokens (%d outputs)" % (_k(sum(e["before"] - e["after"] for e in trims)), len(trims))]
    blocked = sum(e.get("tokens") or 0 for e in gates if e.get("mode") == "on")
    shadow = sum(e.get("tokens") or 0 for e in gates if e.get("mode") == "shadow")
    if blocked:
        parts.append("gate ~%s tokens" % _k(blocked))
    if shadow:
        parts.append("gate would have saved ~%s tokens (shadow)" % _k(shadow))
    parts.append("skills %d suggested, %d followed"
                 % (len(suggested), sum(1 for e in suggested if e.get("path") in read_paths)))
    return "Savings this session: " + " · ".join(parts)


def _codex_summary(event, settings) -> str:
    result = scan(event.get("transcript_path") or "", settings["rates"])
    lines = []
    windows = format_windows(result["windows"])
    if windows:
        lines.append("Plan: " + windows)
    if result["requests"]:
        models = ", ".join("%s %.1f" % (m, c) for m, c in sorted(result["models"].items(), key=lambda kv: -kv[1]))
        text = "This session: ~%.1f credits over %d model requests" % (result["credits"], result["requests"])
        if models:
            text += " (%s)" % models
        if result["unpriced"]:
            text += "; %d requests on models without a rate" % result["unpriced"]
        lines.append(text)
    lines.append(router_line(event.get("session_id") or "", settings["rates"]))
    lines.append(quality_line(event.get("session_id") or ""))
    lines.append(savings_line(event.get("session_id") or ""))
    return "\n".join(line for line in lines if line)


def gate_coverage(session_id: str) -> str:
    """What the gate actually saw this session, so an idle gate can be told from an unused one."""
    data = state.read(session_id)
    seen, kinds = data.get("gate_seen") or {}, data.get("gate_kinds") or {}
    if not seen:
        return "  Tool calls seen this session: none (the gate's hook has not run)."
    tools = ", ".join("%s %d" % item for item in sorted(seen.items(), key=lambda kv: -kv[1])[:6])
    read = ", ".join("%s %d" % item for item in sorted(kinds.items(), key=lambda kv: -kv[1])[:6])
    return "  Tool calls seen this session: %d (%s) · understood as: %s" % (sum(seen.values()), tools, read)


def gate_report(settings, days: int = 7, session_id: str = "") -> str:
    cutoff = time.time() - days * 86400
    events = [e for e in state.events() if e.get("ts", 0) >= cutoff]
    flagged = {e.get("ref") for e in events if e.get("kind") == "flag"}
    rows = {}
    for event in events:
        if event.get("kind") == "gate":
            row = rows.setdefault(event.get("rule"), [0, 0, 0])
            row[0] += 1
            row[1] += event.get("tokens") or 0
            row[2] += event.get("tool_use_id") in flagged
    verb = "Would have blocked" if settings["gate"] != "on" else "Blocked or would have blocked"
    lines = ["Gate (%s), last %d days. %s:" % (settings["gate"], days, verb)]
    if not rows:
        lines.append("  No commands matched a rule yet.")
    for rule, (count, tokens, flags) in sorted(rows.items()):
        line = "  %-34s %4d  ~%s tokens" % (RULE_LABELS.get(rule, rule), count, _k(tokens))
        if flags:
            line += "  ⚠ %d flagged (file was later edited)" % flags
        lines.append(line)
    lines.append("  Overridden by repeating the command: %d"
                 % sum(1 for e in events if e.get("kind") == "override"))
    lines.append(gate_coverage(session_id))
    return "\n".join(lines)


def quality_line(session_id: str) -> str:
    """How often a turn was followed by a complaint, split by whether that turn was routed."""
    events = state.events(session_id)
    turns = {True: 0, False: 0}
    for event in events:
        if event.get("kind") == "route":
            turns[bool(event.get("applied"))] += 1
        elif event.get("kind") == "route_skipped":
            turns[False] += 1
    if not (turns[True] or turns[False]):
        return ""
    complaints = {True: 0, False: 0}
    for event in events:
        if event.get("kind") == "correction":
            complaints[bool(event.get("after_routed"))] += 1
    parts = []
    for routed, label in ((True, "routed"), (False, "your own model")):
        if turns[routed]:
            parts.append("%d of %d %s turns (%.0f%%)" % (complaints[routed], turns[routed], label,
                                                         100.0 * complaints[routed] / turns[routed]))
    return "Corrections after: " + " · ".join(parts)


def _proxy_credits(event, model, rates):
    rate = rate_for(model, rates)
    if rate is None:
        return None
    total = event.get("input") or 0
    hit = min(event.get("cached") or 0, total)
    return ((total - hit) * rate[0] + hit * rate[1] + (event.get("output") or 0) * rate[2]) / 1e6


def router_line(session_id: str, rates: dict) -> str:
    """What the router proxy did this session: requests rewritten, credits spent, and cache hits."""
    events = [e for e in state.events(session_id) if e.get("kind") == "proxy" and e.get("input")]
    if not events:
        return ""
    spent = baseline = 0.0
    total = cached = 0
    for event in events:
        total += event.get("input") or 0
        cached += min(event.get("cached") or 0, event.get("input") or 0)
        used, asked = _proxy_credits(event, event.get("model"), rates), _proxy_credits(event, event.get("requested"), rates)
        if used is not None and asked is not None:
            spent, baseline = spent + used, baseline + asked
    text = "Router: %d of %d requests routed · ~%.1f credits" % (
        sum(1 for e in events if e.get("routed")), len(events), spent)
    if baseline > spent:
        text += " (~%.1f at the model you picked, %.0f%% saved)" % (baseline, 100.0 * (baseline - spent) / baseline)
    if total:
        text += " · prompt cache hits %.0f%% of input" % (100.0 * cached / total)
    return text


def scan_claude(transcript_path: str) -> dict:
    """API-price cost of a Claude Code session, including its subagent transcripts, deduped by message id."""
    result = {"cost": 0.0, "requests": 0, "unpriced": 0, "models": {}, "subagent_requests": 0}
    base = os.path.splitext(transcript_path)[0]
    paths = [transcript_path] + sorted(glob.glob(os.path.join(base, "subagents", "*.jsonl")))
    seen = set()
    for index, path in enumerate(paths):
        try:
            handle = open(path)
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                message = item.get("message") or {}
                model = message.get("model") or ""
                if item.get("type") != "assistant" or not message.get("usage") or model.startswith("<"):
                    continue
                key = message.get("id") or (path, item.get("uuid"))
                if key in seen:
                    continue
                seen.add(key)
                result["requests"] += 1
                if index > 0 or item.get("isSidechain"):
                    result["subagent_requests"] += 1
                price = apps.price_for(model)
                if price is None:
                    result["unpriced"] += 1
                    continue
                usage = message["usage"]
                creation = usage.get("cache_creation") or {}
                one_hour = creation.get("ephemeral_1h_input_tokens") or 0
                five_minute = creation.get("ephemeral_5m_input_tokens")
                if five_minute is None:
                    five_minute = max(0, (usage.get("cache_creation_input_tokens") or 0) - one_hour)
                cost = ((usage.get("input_tokens") or 0) * price[0] + (usage.get("output_tokens") or 0) * price[1]
                        + (usage.get("cache_read_input_tokens") or 0) * price[2]
                        + five_minute * price[3] + one_hour * price[4]) / 1e6
                result["cost"] += cost
                result["models"][model] = result["models"].get(model, 0.0) + cost
    return result


def route_line(session_id: str) -> str:
    events = state.events(session_id)
    tiers = {}
    for event in events:
        if event.get("kind") == "route" and event.get("tier") != "full":
            tiers[event["tier"]] = tiers.get(event["tier"], 0) + 1
    if not tiers:
        return ""
    manual = sum(1 for e in events if e.get("kind") == "manual_tier"
                 or (e.get("kind") == "skill_used" and e.get("skill") in ("jev:lite", "jev:mid")))
    delegated = {}
    for event in events:
        agent = (event.get("agent") or "").split(":")[-1]
        if event.get("kind") == "delegated" and agent in ("scout", "helper", "worker"):
            delegated[agent] = delegated.get(agent, 0) + 1
    text = "Routing: " + ", ".join("%s %d" % item for item in sorted(tiers.items()))
    if delegated:
        text += " · delegated: " + ", ".join("%s %d" % item for item in sorted(delegated.items()))
    if manual:
        text += " · manual tiers %d" % manual
    advice = sum(1 for e in events if e.get("kind") == "advisor")
    model_tips = sum(1 for e in events if e.get("kind") == "model_advice")
    return (text + (" · new-topic tips %d" % advice if advice else "")
            + (" · model tips %d" % model_tips if model_tips else ""))


def _claude_summary(event, settings) -> str:
    result = scan_claude(event.get("transcript_path") or "")
    lines = []
    if result["requests"]:
        cheaper = sum(c for m, c in result["models"].items() if apps.model_rank(m) < 3)
        models = ", ".join("%s $%.2f" % (m, c) for m, c in sorted(result["models"].items(), key=lambda kv: -kv[1]))
        text = "This session: ~$%.2f at API prices over %d requests (%s)" % (result["cost"], result["requests"], models)
        if result["cost"]:
            text += "; cheaper tiers $%.2f (%.0f%%)" % (cheaper, 100.0 * cheaper / result["cost"])
        if result["subagent_requests"]:
            text += "; %d requests in subagents" % result["subagent_requests"]
        lines.append(text)
    lines.append(savings_line(event.get("session_id") or ""))
    lines.append(route_line(event.get("session_id") or ""))
    return "\n".join(line for line in lines if line)


def summary(event, settings) -> str:
    return _claude_summary(event, settings) if apps.detect(event) == "claude" else _codex_summary(event, settings)
