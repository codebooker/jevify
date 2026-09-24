"""Minimal Jev (TypeSafe System One) client. Every failure returns None."""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.parse
import urllib.request

from . import config
from . import state as eventlog  # `ask(state=...)` shadows the module name, so it is imported under another

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"


def ask(state: dict, questions: dict, timeout: float = 2.5):
    """Send one System One request and return its answers map, or None."""
    creds = config.credentials()
    if creds.get("TYPESAFE_API_KEY"):
        url, model, key = TYPESAFE_URL, "jev-latest", creds["TYPESAFE_API_KEY"]
    elif creds.get("OPENROUTER_API_KEY"):
        url, model, key = OPENROUTER_URL, "~typesafe/jev-latest", creds["OPENROUTER_API_KEY"]
    else:
        return None
    # Which key was used, for the log only: a length and its last four cannot rebuild a key, and a 401
    # is otherwise impossible to tell from a wrong key supplied by the environment.
    name = "TYPESAFE_API_KEY" if url == TYPESAFE_URL else "OPENROUTER_API_KEY"
    whose = {"key_source": "env" if os.environ.get(name) else "file",
             "key": "%s len=%d ...%s" % (name, len(key), key[-4:])}
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    # A local viewer may stand in front of Jev; it supplies its own key and drops ours before calling out.
    where = os.environ.get("JEV_API_URL") or config.load_settings()["jev_api_url"] or url
    request = urllib.request.Request(where, data=body, method="POST",
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, payload = response.status, json.loads(response.read())
    except Exception as exc:
        # Never fail a turn over this, but never fail silently either: a swallowed error here looks
        # exactly like routing being switched off.
        _log_failure(where, timeout, dict(whose, error="%s: %s" % (type(exc).__name__, str(exc)[:200])))
        return None
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        # A 200 that carries no answers (a provider error in the body, say) is a failure too.
        _log_failure(where, timeout, dict(whose, **{
            "error": "no answers in response", "status": status,
            "fields": sorted(payload)[:10] if isinstance(payload, dict) else type(payload).__name__,
            "detail": json.dumps(payload.get("error"))[:300] if isinstance(payload, dict) else ""}))
        return None
    return answers


def _log_failure(where, timeout, detail):
    try:
        # Names only: a proxy URL can carry credentials, so the values never go in the log.
        proxies = sorted(name for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                           "http_proxy", "https_proxy", "all_proxy") if os.environ.get(name))
        eventlog.log(dict(detail, kind="jev_error", host=urllib.parse.urlsplit(where).hostname,
                          timeout=timeout, python=sys.executable, proxy_env=proxies))
    except Exception:
        pass


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= 1 else None


def noul(answers, qid):
    answer = (answers or {}).get(qid)
    return _number(answer.get("noul")) if isinstance(answer, dict) else None


def choice(answers, qid):
    answer = (answers or {}).get(qid)
    if not isinstance(answer, dict) or not isinstance(answer.get("choice"), str):
        return None, None
    confidence = _number(answer.get("confidence"))
    return (answer["choice"], confidence) if confidence is not None else (None, None)
