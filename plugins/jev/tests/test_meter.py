import json
import os

from helpers import JevTestCase
from jev import config, meter, state


def line(kind, payload):
    return json.dumps({"type": kind, "payload": payload}) + "\n"


class MeterTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.settings = config.load_settings()
        self.transcript = os.path.join(self.home, "rollout.jsonl")
        with open(self.transcript, "w") as handle:
            handle.write(line("turn_context", {"model": "gpt-6-astra", "effort": "xhigh"}))
            handle.write(line("token_usage_record", {"usage": {"input_tokens": 10000, "cached_input_tokens": 8000,
                                                               "output_tokens": 1000}}))
            handle.write(line("event_msg", {"type": "token_count", "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 38.4, "window_minutes": 300, "resets_at": 1790000000},
                "secondary": {"used_percent": 12.0, "window_minutes": 10080, "resets_at": None}}}))
            handle.write(line("event_msg", {"type": "token_count", "rate_limits": {
                "limit_id": "premium", "primary": None, "secondary": None}}))
            handle.write(line("turn_context", {"model": "gpt-5.6-luna"}))
            handle.write(line("token_usage_record", {"usage": {"input_tokens": 1000, "cached_input_tokens": 0,
                                                               "output_tokens": 100}}))
            handle.write(line("turn_context", {"model": "mystery-model"}))
            handle.write(line("token_usage_record", {"usage": {"input_tokens": 5, "cached_input_tokens": 0,
                                                               "output_tokens": 5}}))

    def test_scan_credits_and_windows(self):
        result = meter.scan(self.transcript, self.settings["rates"])
        # astra: (2000*250 + 8000*25 + 1000*1250)/1e6 = 1.95; luna: (1000*5 + 100*30)/1e6 = 0.008
        self.assertAlmostEqual(result["credits"], 1.958, places=6)
        self.assertEqual(result["requests"], 3)
        self.assertEqual(result["unpriced"], 1)
        self.assertEqual(list(result["windows"]), ["codex"])
        text = meter.format_windows(result["windows"])
        self.assertIn("5h 38% used", text)
        self.assertIn("weekly 12% used", text)
        self.assertNotIn("premium", text)
        other = meter.format_windows({"codex": result["windows"]["codex"],
                                      "gpt-6-astra": {"primary": {"used_percent": 50, "window_minutes": 300}}})
        self.assertIn("gpt-6-astra: 5h 50% used", other)

    def test_savings_line(self):
        state.log({"kind": "trim", "session": "s1", "before": 3000, "after": 500})
        state.log({"kind": "gate", "session": "s1", "mode": "shadow", "tokens": 1200, "rule": "huge_read"})
        state.log({"kind": "skill", "session": "s1", "skill": "imagegen", "path": "/p/SKILL.md"})
        state.log({"kind": "skill_read", "session": "s1", "path": "/p/SKILL.md"})
        text = meter.savings_line("s1")
        self.assertIn("trim ~2.5k tokens (1 outputs)", text)
        self.assertIn("gate would have saved ~1.2k tokens", text)
        self.assertIn("skills 1 suggested, 1 followed", text)

    def test_gate_report(self):
        state.log({"kind": "gate", "session": "a", "mode": "shadow", "tokens": 3000, "rule": "duplicate_read",
                   "tool_use_id": "t1"})
        state.log({"kind": "gate", "session": "a", "mode": "shadow", "tokens": 500, "rule": "jev_unnecessary",
                   "tool_use_id": "t2"})
        state.log({"kind": "flag", "session": "a", "ref": "t2"})
        state.log({"kind": "override", "session": "a"})
        report = meter.gate_report(self.settings)
        self.assertIn("Duplicate read", report)
        self.assertIn("1 flagged", report)
        self.assertIn("Overridden by repeating the command: 1", report)

    def test_summary_mentions_credits(self):
        text = meter.summary({"session_id": "s1", "transcript_path": self.transcript}, self.settings)
        self.assertIn("~2.0 credits over 3 model requests", text)

    def test_router_line(self):
        self.assertEqual(meter.router_line("s1", self.settings["rates"]), "")
        state.log({"kind": "proxy", "session": "s1", "routed": True, "requested": "gpt-6-astra",
                   "model": "gpt-5.6-luna", "input": 10000, "cached": 8000, "output": 500})
        state.log({"kind": "proxy", "session": "s1", "routed": False, "requested": "gpt-6-astra",
                   "model": "gpt-6-astra", "input": 20000, "cached": 18000, "output": 1000})
        state.log({"kind": "proxy", "session": "s1", "routed": False, "requested": "gpt-6-astra",
                   "model": "gpt-6-astra", "status": 500})
        # actual: luna 0.029 + astra 2.2 = 2.229; all on astra: 1.325 + 2.2 = 3.525 -> 37% saved
        self.assertEqual(meter.router_line("s1", self.settings["rates"]),
                         "Router: 1 of 2 requests routed · ~2.2 credits (~3.5 at the model you picked, 37% saved)"
                         " · prompt cache hits 87% of input")

    def test_quality_line(self):
        self.assertEqual(meter.quality_line("s1"), "")
        for applied in (True, True, True, False):
            state.log({"kind": "route", "session": "s1", "applied": applied})
        state.log({"kind": "route_skipped", "session": "s1", "reason": "low_confidence"})
        state.log({"kind": "correction", "session": "s1", "after_routed": True})
        self.assertEqual(meter.quality_line("s1"),
                         "Corrections after: 1 of 3 routed turns (33%) · 0 of 2 your own model turns (0%)")
