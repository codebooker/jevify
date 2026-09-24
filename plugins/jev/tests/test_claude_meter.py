import json
import os

from helpers import JevTestCase
from jev import config, meter, state


def assistant(mid, model, usage, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "message": {"id": mid, "model": model, "usage": usage}}


class ClaudeMeterTest(JevTestCase):
    def setUp(self):
        super().setUp()
        os.environ["JEV_PLATFORM"] = "claude"
        self.transcript = os.path.join(self.home, "sess.jsonl")
        opus = {"input_tokens": 100, "output_tokens": 1000, "cache_read_input_tokens": 100000,
                "cache_creation_input_tokens": 10000,
                "cache_creation": {"ephemeral_1h_input_tokens": 10000, "ephemeral_5m_input_tokens": 0}}
        sonnet = {"input_tokens": 0, "output_tokens": 2000, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 50000}
        rows = [assistant("a", "claude-opus-5", opus), assistant("a", "claude-opus-5", opus),
                assistant("b", "claude-sonnet-5", sonnet), assistant("c", "<synthetic>", {"output_tokens": 0})]
        with open(self.transcript, "w") as handle:
            handle.write("".join(json.dumps(r) + "\n" for r in rows))
        sub = os.path.join(self.home, "sess", "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-1.jsonl"), "w") as handle:
            handle.write(json.dumps(assistant("d", "claude-haiku-4-5", {"input_tokens": 1000, "output_tokens": 1000},
                                              sidechain=True)) + "\n")

    def test_scan_dedupes_and_prices_each_model(self):
        result = meter.scan_claude(self.transcript)
        # opus: 100*5 + 1000*25 + 100000*0.5 + 10000*10 = 175,500 / 1e6
        # sonnet: 2000*10 + 50000*2.5 (5-minute writes when no breakdown) = 145,000 / 1e6
        # haiku: 1000*1 + 1000*5 = 6,000 / 1e6
        self.assertAlmostEqual(result["models"]["claude-opus-5"], 0.1755, places=6)
        self.assertAlmostEqual(result["models"]["claude-sonnet-5"], 0.145, places=6)
        self.assertAlmostEqual(result["models"]["claude-haiku-4-5"], 0.006, places=6)
        self.assertEqual(result["requests"], 3)
        self.assertEqual(result["subagent_requests"], 1)

    def test_summary_and_route_line(self):
        state.log({"kind": "route", "session": "s", "tier": "mid", "mode": "on", "confidence": 0.8})
        state.log({"kind": "route", "session": "s", "tier": "scout", "mode": "on", "confidence": 0.8})
        state.log({"kind": "skill_used", "session": "s", "skill": "jev:mid", "path": "jev:mid"})
        state.log({"kind": "delegated", "session": "s", "agent": "jev:scout"})
        state.log({"kind": "delegated", "session": "s", "agent": "jev:worker"})
        text = meter.summary({"session_id": "s", "transcript_path": self.transcript}, config.load_settings())
        self.assertIn("~$0.33 at API prices over 3 requests", text)
        self.assertIn("cheaper tiers $0.15 (46%)", text)
        self.assertIn("Routing: mid 1, scout 1 · delegated: scout 1, worker 1 · manual tiers 1", text)
