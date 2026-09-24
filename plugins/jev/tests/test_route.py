import json
import os

from helpers import FakeJev, JevTestCase
from jev import config, route, state


def answers(tier="mid", conf=0.85, followup=0.1, unrelated=None, skill=None, skill_conf=0.9):
    def respond(body):
        out = {}
        questions = body["questions"]
        if "tier" in questions:
            out["tier"] = {"choice": tier, "confidence": conf}
        if "followup" in questions:
            out["followup"] = {"noul": followup}
        if "unrelated" in questions and unrelated is not None:
            out["unrelated"] = {"noul": unrelated}
        if "skill" in questions:
            out["skill"] = {"choice": skill or "none", "confidence": skill_conf}
        return out
    return respond


class RouteTest(JevTestCase):
    def setUp(self):
        super().setUp()
        os.environ["JEV_PLATFORM"] = "claude"
        self.settings = config.load_settings()
        self.transcript = os.path.join(self.home, "t.jsonl")
        self.write_transcript(20000)

    def write_transcript(self, context, skills="- simplify: Review changed code\n- pdf: Work with PDFs\n",
                         model="claude-opus-5"):
        lines = [{"type": "attachment", "attachment": {"type": "skill_listing", "content": skills}},
                 {"type": "assistant", "message": {"id": "m1", "model": model, "usage": {
                     "input_tokens": 10, "cache_read_input_tokens": context - 10, "cache_creation_input_tokens": 0,
                     "output_tokens": 50}}}]
        with open(self.transcript, "w") as handle:
            handle.write("".join(json.dumps(line) + "\n" for line in lines))

    def run_prompt(self, prompt="add a --verbose flag to the CLI", **kw):
        with FakeJev(answers(**kw)) as fake:
            out = route.on_prompt({"session_id": "r1", "cwd": "/tmp", "transcript_path": self.transcript},
                                  self.settings, prompt)
        return out, fake

    def context(self, out):
        return out["hookSpecificOutput"]["additionalContext"]

    def test_mid_tier_delegates_to_sonnet_worker(self):
        out, fake = self.run_prompt()
        self.assertIn("`jev:worker`", self.context(out))
        self.assertIn("delegate", self.context(out))
        self.assertIn("Trust its report", self.context(out))
        self.assertIn("jev → Sonnet worker (mid, 0.85)", out["systemMessage"])
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(state.read("r1")["tier"], "mid")
        self.assertEqual(state.read("r1")["task"], "add a --verbose flag to the CLI")

    def test_low_confidence_gives_no_tier_hint(self):
        out, _ = self.run_prompt(conf=0.4)
        self.assertIsNone(out)

    def test_followup_keeps_previous_tier(self):
        with state.session("r1") as s:
            s["tier"], s["task"] = "lite", "rename foo to bar everywhere"
        out, _ = self.run_prompt(prompt="yes do it", tier="full", followup=0.9)
        self.assertIn("`jev:helper`", self.context(out))

    def test_lite_delegates_to_haiku_even_with_big_context(self):
        self.write_transcript(700000)  # the helper starts with a fresh context, so conversation size doesn't matter
        out, _ = self.run_prompt(tier="lite")
        self.assertIn("`jev:helper`", self.context(out))
        self.assertIn("Haiku helper", out["systemMessage"])

    def test_no_downgrade_hint_when_session_is_already_cheaper(self):
        self.write_transcript(20000, model="claude-sonnet-5")  # the model actually answering, from the transcript
        out, _ = self.run_prompt(tier="mid")
        self.assertIsNone(out)

    def index_nodes(self, nodes):
        config.write_json(config.index_dir(config.repo_root("/tmp")) / "meta.json", {"state": "ready", "nodes": nodes})

    def test_scout_only_on_large_codebases(self):
        out, _ = self.run_prompt(tier="scout")  # no index yet: size unknown, so no scout
        self.assertIn("as few tool calls as possible", self.context(out))
        self.index_nodes(1375)
        out, _ = self.run_prompt(tier="scout")
        self.assertNotIn("jev:scout", self.context(out))
        self.assertIn("jev → direct", out["systemMessage"])

    def test_scout_and_direct_hints(self):
        self.index_nodes(19734)
        out, _ = self.run_prompt(tier="scout")
        self.assertIn("`jev:scout`", self.context(out))
        self.assertIn("Only read files yourself if", self.context(out))
        out, _ = self.run_prompt(tier="direct")
        self.assertIn("as few tool calls as possible", self.context(out))

    def test_shadow_logs_without_hint(self):
        self.settings["route"] = "shadow"
        out, _ = self.run_prompt()
        self.assertNotIn("hookSpecificOutput", out or {})
        self.assertIn("[shadow]", out["systemMessage"])
        self.assertEqual(state.events("r1")[-1]["mode"], "shadow")

    def test_skill_hint_uses_claude_catalog(self):
        out, fake = self.run_prompt(tier="full", skill="simplify")
        self.assertIn("`simplify` skill", self.context(out))
        self.assertIn("none", fake.requests[0]["body"]["questions"]["skill"]["criteria"])

    def test_advisor_only_with_big_context_and_previous_task(self):
        self.write_transcript(90000)
        with state.session("r1") as s:
            s["task"] = "fix the parser"
        out, fake = self.run_prompt(tier="full", unrelated=0.95)
        self.assertIn("New topic", out["systemMessage"])
        self.assertIn("unrelated", fake.requests[0]["body"]["questions"])
        self.write_transcript(20000)
        _, fake = self.run_prompt(tier="full", unrelated=0.95)
        self.assertNotIn("unrelated", fake.requests[0]["body"]["questions"])

    def test_jev_failure_still_saves_task(self):
        with FakeJev(lambda body: None):
            out = route.on_prompt({"session_id": "r2", "transcript_path": self.transcript}, self.settings, "hello")
        self.assertIsNone(out)
        self.assertEqual(state.read("r2")["task"], "hello")

    def test_route_off_skips_tier_question(self):
        self.settings["route"] = "off"
        _, fake = self.run_prompt()
        self.assertNotIn("tier", fake.requests[0]["body"]["questions"])

    def test_slash_command_prompts_skip_routing(self):
        out, fake = self.run_prompt(prompt="/jev:lite create notes/x.md from the README")
        self.assertIsNone(out)
        self.assertEqual(fake.requests, [])
        self.assertEqual(state.read("r1")["task"], "/jev:lite create notes/x.md from the README")
        self.assertEqual(state.events("r1")[-1], dict(state.events("r1")[-1], kind="manual_tier", tier="lite"))

    def test_model_advisor_suggests_sonnet_after_a_routine_run(self):
        notices = []
        for i, tier in enumerate(["mid", "direct", "lite", "direct", "mid", "direct"]):
            out, _ = self.run_prompt(prompt="routine task %d" % i, tier=tier)
            notices.append((out or {}).get("systemMessage") or "")
        self.assertNotIn("model picker", " ".join(notices[:4]))
        self.assertIn("switching this session to Sonnet in the model picker", notices[4])
        self.assertNotIn("model picker", notices[5])  # rate limited
        self.assertEqual([e["to"] for e in state.events("r1") if e["kind"] == "model_advice"], ["sonnet"])

    def test_model_advisor_suggests_opus_for_a_hard_prompt_on_sonnet(self):
        self.write_transcript(20000, model="claude-sonnet-5")
        out, _ = self.run_prompt(prompt="redesign the sync protocol", tier="full", conf=0.9)
        self.assertIn("Opus in the model picker", out["systemMessage"])

    def test_low_confidence_routine_prompts_still_count_for_model_advice(self):
        out = None
        for i in range(5):
            out, _ = self.run_prompt(prompt="small thing %d" % i, tier="direct", conf=0.5)
        self.assertIn("Sonnet in the model picker", out["systemMessage"])

    def test_no_skill_hint_for_direct_tasks(self):
        out, _ = self.run_prompt(tier="direct", skill="simplify")
        self.assertNotIn("skill matches", self.context(out))
