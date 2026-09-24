import json
import os
import tempfile
from pathlib import Path

from helpers import FakeJev, JevTestCase
from jev import codex_route, config, state


def answers(tier="2", conf=0.9, followup=0.0, skill=None):
    def respond(body):
        out = {}
        if "tier" in body["questions"]:
            out["tier"] = {"choice": tier, "confidence": conf}
            out["followup"] = {"noul": followup}
        if "skill" in body["questions"]:
            out["skill"] = {"choice": skill or "none", "confidence": 0.9}
        return out
    return respond


class CodexRouteTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.codex_home = tempfile.mkdtemp()
        os.environ["CODEX_HOME"] = self.codex_home
        Path(self.codex_home, "config.toml").write_text(
            'openai_base_url = "http://127.0.0.1:47821/backend-api/codex"  # jev-router\n')
        self.transcript = os.path.join(self.home, "t.jsonl")
        self.context(20000)
        self.settings = config.load_settings()

    def context(self, tokens):
        Path(self.transcript).write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {"input_tokens": tokens}}}}) + "\n")

    def prompt(self, text="add a --verbose flag", turn="t1", model="gpt-6-astra", **kw):
        with FakeJev(answers(**kw)) as fake:
            out = codex_route.on_prompt({"session_id": "s1", "turn_id": turn, "model": model, "cwd": "/tmp",
                                         "transcript_path": self.transcript}, self.settings, text)
        return out, fake

    def routes(self):
        return state.read("s1").get("codex_routes") or {}

    def test_tier_is_stored_for_the_turn_and_shown(self):
        out, _ = self.prompt(tier="2")
        self.assertEqual(self.routes()["t1"], {"model": "gpt-6-luna", "effort": "high", "tier": 2})
        self.assertIn("jev → gpt-6-luna · high (tier 2, 0.90)", out["systemMessage"])

    def test_followup_keeps_tier_and_low_confidence_leaves_model_alone(self):
        self.prompt(tier="1", turn="t1")
        self.prompt(tier="5", followup=0.9, turn="t2", text="yes do it")
        self.assertEqual(self.routes()["t2"]["model"], "gpt-6-luna")
        out, _ = self.prompt(tier="1", conf=0.4, turn="t3")
        self.assertNotIn("t3", self.routes())
        self.assertIsNone(out)

    def test_max_tier_clamp(self):
        config.save_settings({"codex_max_tier": 4})
        self.settings = config.load_settings()
        self.prompt(tier="5")
        self.assertEqual(self.routes()["t1"], {"model": "gpt-6-astra", "effort": "high", "tier": 4})

    def test_cache_guard_keeps_the_cached_model_on_long_contexts(self):
        # A model only slightly cheaper than Astra: below 12.5K tokens the switch pays, above it the
        # uncached re-read costs more than it saves. (The real gpt-6 ladder is cheap enough that every
        # downgrade pays, so this uses a synthetic rung to exercise the guard itself.)
        config.save_settings({"rates": dict(config.DEFAULTS["rates"], **{"almost-astra": [200, 20, 1000]}),
                              "codex_tiers": [["almost-astra", "high"]] * 5})
        self.settings = config.load_settings()
        self.context(300000)
        out, _ = self.prompt(tier="3")
        self.assertEqual(self.routes()["t1"]["model"], "gpt-6-astra")
        self.assertIn("kept for cache", out["systemMessage"])
        self.context(5000)
        self.prompt(tier="3", turn="t2")
        self.assertEqual(self.routes()["t2"]["model"], "almost-astra")

    def test_every_gpt6_downgrade_now_pays_for_itself(self):
        rates = self.settings["rates"]
        for tokens in (20000, 300000, 900000):
            self.assertTrue(codex_route.switch_pays(rates["gpt-6-astra"], rates["gpt-6-sol"], tokens))
            self.assertTrue(codex_route.switch_pays(rates["gpt-6-sol"], rates["gpt-6-luna"], tokens))

    def test_shadow_and_missing_router_store_nothing(self):
        config.save_settings({"route": "shadow"})
        self.settings = config.load_settings()
        out, _ = self.prompt()
        self.assertEqual(self.routes(), {})
        self.assertIn("[shadow]", out["systemMessage"])
        config.save_settings({"route": "on"})
        self.settings = config.load_settings()
        Path(self.codex_home, "config.toml").write_text("")
        out, _ = self.prompt(turn="t2")
        self.assertEqual(self.routes(), {})
        self.assertIn("[router not installed]", out["systemMessage"])

    def test_commands_skip_routing(self):
        out, fake = self.prompt(text="$jev status")
        self.assertIsNone(out)
        self.assertEqual(fake.requests, [])

    def test_an_unpriced_new_model_only_gives_up_routine_work(self):
        out, _ = self.prompt(tier="5", model="gpt-7-nova")  # hard work stays on the model the user chose
        self.assertEqual(self.routes(), {})
        self.assertIsNone(out)
        out, _ = self.prompt(tier="1", turn="t2", model="gpt-7-nova")
        self.assertEqual(self.routes()["t2"]["model"], "gpt-6-luna")

    def test_upgrades_follow_jev_but_the_bar_is_tunable(self):
        """By default an upgrade needs no more confidence than a downgrade; the knob can raise it."""
        self.prompt(tier="4", conf=0.7, model="gpt-6-sol")
        self.assertEqual(self.routes()["t1"]["model"], "gpt-6-astra")
        config.save_settings({"route_up_threshold": 0.8})
        self.settings = config.load_settings()
        out, _ = self.prompt(tier="4", conf=0.7, turn="t9", model="gpt-6-sol")
        self.assertNotIn("t9", self.routes())
        self.assertIsNone(out)
        self.prompt(tier="4", conf=0.85, turn="t2", model="gpt-6-sol")
        self.assertEqual(self.routes()["t2"]["model"], "gpt-6-astra")
        self.prompt(tier="1", conf=0.7, turn="t3", model="gpt-6-sol")
        self.assertEqual(self.routes()["t3"]["model"], "gpt-6-luna")

    def test_a_low_confidence_answer_is_logged_not_silent(self):
        from jev import state
        self.prompt(tier="2", conf=0.4)
        skipped = [e for e in state.events("s1") if e["kind"] == "route_skipped"]
        self.assertEqual((skipped[-1]["reason"], skipped[-1]["tier"], skipped[-1]["confidence"]),
                         ("low_confidence", "2", 0.4))
