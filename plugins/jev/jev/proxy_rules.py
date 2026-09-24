"""Pure request-rewrite rules for the Codex router proxy (no I/O; the proxy commits memory after success)."""
from __future__ import annotations

import json

# Models that accept a positional `configuration_update` item, so effort can change without breaking the
# cached prefix (openai/codex#42996).
CONFIG_UPDATE_MODELS = ("gpt-6-astra",)


class Memory:
    """What the proxy remembers per session between requests."""

    def __init__(self):
        self.updates = {}      # session -> [(user message ordinal, effort)] configuration_update insertions
        self.routed = set()    # sessions with a turn sent to another model (their history holds its reasoning)
        self.strip = set()     # sessions whose requests needed earlier reasoning items removed
        self.no_updates = False


class Plan:
    def __init__(self, attempts, routed=False, session=None, turn=None, requested=None, model=None, effort=None,
                 via=None, updates=None, kind=None):
        self.attempts, self.routed, self.session, self.turn, self.kind = attempts, routed, session, turn, kind
        self.requested, self.model, self.effort, self.via, self.updates = requested, model, effort, via, updates


def turn_metadata(body, headers):
    client = body.get("client_metadata") if isinstance(body.get("client_metadata"), dict) else {}
    raw = headers.get("x-codex-turn-metadata") or client.get("x-codex-turn-metadata") or "{}"
    try:
        meta = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        meta = {}
    return (meta.get("session_id") or client.get("session_id"), meta.get("turn_id") or client.get("turn_id"),
            meta.get("request_kind"), meta)


def _users(items):
    return [i for i, item in enumerate(items)
            if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user"]


def strip_previous_reasoning(items):
    users = _users(items)
    last = users[-1] if users else -1
    return [item for i, item in enumerate(items)
            if not (i < last and isinstance(item, dict) and item.get("type") == "reasoning")]


def with_updates(items, updates):
    """Insert configuration_update items right after the user message at each recorded ordinal."""
    users = _users(items)
    after = {users[ordinal]: effort for ordinal, effort in updates if ordinal < len(users)}
    out = []
    for i, item in enumerate(items):
        out.append(item)
        if i in after:
            out.append({"type": "configuration_update", "reasoning": {"effort": after[i]}})
    return out


def _encode(body):
    return json.dumps(body).encode()


def plan(raw: bytes, headers: dict, decision_for, memory: Memory) -> Plan:
    passthrough = Plan([("original", raw)])
    try:
        body = json.loads(raw)
    except ValueError:
        return passthrough
    if not isinstance(body, dict):
        return passthrough
    session, turn, kind, meta = turn_metadata(body, headers)
    requested = body.get("model")
    if kind == "compaction" and session:
        memory.updates.pop(session, None)  # history is rewritten, so recorded insertion points are gone
    if headers.get("x-openai-subagent") or meta.get("subagent_kind"):
        kind = "subagent"
    passthrough = Plan([("original", raw)], session=session, turn=turn, requested=requested, model=requested,
                       kind=kind)
    if kind != "turn" or not session or not turn:
        return passthrough
    items = body.get("input") if isinstance(body.get("input"), list) else []
    reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
    decision = decision_for(session, turn)
    if not decision:
        if session not in memory.routed and not (requested in CONFIG_UPDATE_MODELS and memory.updates.get(session)):
            return passthrough
        decision = {"model": requested, "effort": reasoning.get("effort")}  # keep earlier rewrites consistent
    model, effort = decision["model"], decision["effort"]
    users = _users(items)
    updates = [u for u in memory.updates.get(session, []) if u[0] < len(users)]
    primary, via = dict(body), None
    if model == requested and model in CONFIG_UPDATE_MODELS and users and not memory.no_updates:
        current = updates[-1][1] if updates else reasoning.get("effort")
        if effort != current:
            updates = [u for u in updates if u[0] != len(users) - 1] + [(len(users) - 1, effort)]
        if updates:
            primary["input"] = with_updates(items, updates)
            via = "configuration_update"
    else:
        primary["model"] = model
        primary["reasoning"] = dict(reasoning, effort=effort)
        if model in CONFIG_UPDATE_MODELS and updates and not memory.no_updates:
            primary["input"] = with_updates(items, updates)  # keep earlier insertions so the prefix matches
        via = "model" if model != requested else ("effort" if effort != reasoning.get("effort") else None)
    if session in memory.strip:
        primary["input"] = strip_previous_reasoning(primary.get("input", items))
    strippable = session not in memory.strip and (model != requested or session in memory.routed)
    stripped = ("stripped", _encode(dict(primary, input=strip_previous_reasoning(primary.get("input", items)))))
    info = dict(session=session, turn=turn, requested=requested, model=model, effort=effort, via=via, updates=updates,
                kind=kind)
    if primary == body:
        return Plan([("original", raw)] + ([stripped] if strippable else []), **info)
    attempts = [("primary", _encode(primary))] + ([stripped] if strippable else [])
    if via == "configuration_update":
        attempts.append(("no_update", _encode(dict(body, reasoning=dict(reasoning, effort=effort)))))
    return Plan(attempts + [("original", raw)], routed=True, **info)


def remember(memory: Memory, plan_: Plan, label: str) -> None:
    """Commit what the successful attempt implies for later requests in the session."""
    if not plan_.session:
        return
    if label == "stripped":
        memory.strip.add(plan_.session)
    if label == "no_update":
        memory.no_updates = True
    if not plan_.routed or label in ("original", "no_update"):
        return
    if plan_.model != plan_.requested:
        memory.routed.add(plan_.session)
    if plan_.via == "configuration_update":
        memory.updates[plan_.session] = plan_.updates


def usage_from_sse(tail: bytes):
    """Token usage from the response.completed event near the end of a Responses SSE stream."""
    for line in reversed(tail.decode("utf-8", "replace").splitlines()):
        if not line.startswith("data:") or "response.completed" not in line:
            continue
        try:
            usage = (json.loads(line[5:].strip()).get("response") or {}).get("usage") or {}
        except ValueError:
            return None
        return {"input": usage.get("input_tokens") or 0,
                "cached": (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0,
                "output": usage.get("output_tokens") or 0}
    return None
