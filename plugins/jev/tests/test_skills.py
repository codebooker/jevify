import json
import os

from helpers import FakeJev, JevTestCase
from jev import config, skills, state

BLOCK = ("<skills_instructions>\n## Skills\nIntro text.\n### Skill roots\n- `r0` = `/skills/sys`\n"
         "- `r1` = `/plugins/x`\n### Available skills\n"
         "- imagegen: Generate images. (file: r0/imagegen/SKILL.md)\n"
         "- sites:build: Build a site: fast. (file: r1/sites/build/SKILL.md)\n</skills_instructions>")


class SkillsTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.settings = config.load_settings()
        self.transcript = os.path.join(self.home, "rollout.jsonl")
        with open(self.transcript, "w") as handle:
            handle.write(json.dumps({"type": "session_meta", "payload": {}}) + "\n")
            handle.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "developer",
                "content": [{"type": "input_text", "text": "Preamble " + BLOCK}]}}) + "\n")

    def event(self):
        return {"session_id": "s1", "transcript_path": self.transcript}

    def test_parse_catalog(self):
        catalog = skills.catalog_from_transcript(self.transcript)
        self.assertEqual([s["name"] for s in catalog], ["imagegen", "sites:build"])
        self.assertEqual(catalog[1]["description"], "Build a site: fast.")
        self.assertEqual(catalog[1]["path"], "/plugins/x/sites/build/SKILL.md")

    def test_confident_choice_is_suggested_and_cached(self):
        with FakeJev(lambda body: {"skill": {"choice": "imagegen", "confidence": 0.9}}) as fake:
            hint = skills.route(self.event(), self.settings, "make me a logo")
        self.assertIn("`imagegen`", hint)
        self.assertIn("/skills/sys/imagegen/SKILL.md", hint)
        criteria = fake.requests[0]["body"]["questions"]["skill"]["criteria"]
        self.assertIn("none", criteria)
        self.assertEqual(state.read("s1")["skills"][0]["name"], "imagegen")
        self.assertEqual(state.events("s1")[-1]["skill"], "imagegen")

    def test_low_confidence_and_none_are_ignored(self):
        with FakeJev(lambda body: {"skill": {"choice": "imagegen", "confidence": 0.4}}):
            self.assertIsNone(skills.route(self.event(), self.settings, "make me a logo"))
        with FakeJev(lambda body: {"skill": {"choice": "none", "confidence": 0.99}}):
            self.assertIsNone(skills.route(self.event(), self.settings, "fix the tests"))

    def test_explicit_mention_skips_jev(self):
        with FakeJev(lambda body: {"skill": {"choice": "imagegen", "confidence": 0.9}}) as fake:
            self.assertIsNone(skills.route(self.event(), self.settings, "$imagegen a logo"))
            self.assertIsNone(skills.route(self.event(), self.settings, "use /sites:build now"))
        self.assertEqual(fake.requests, [])

    def test_missing_catalog_or_disabled(self):
        with FakeJev(lambda body: {"skill": {"choice": "imagegen", "confidence": 0.9}}) as fake:
            self.assertIsNone(skills.route({"session_id": "s2", "transcript_path": "/nope"}, self.settings, "logo"))
            self.assertIsNone(skills.route(self.event(), dict(self.settings, skills=False), "logo"))
        self.assertEqual(fake.requests, [])


class ClaudeCatalogTest(JevTestCase):
    def test_parse_latest_skill_listing(self):
        path = os.path.join(self.home, "claude.jsonl")
        first = {"type": "attachment", "attachment": {"type": "skill_listing", "content": "- old: gone"}}
        content = ("<system-reminder>\nThe following skills are available for use with the Skill tool:\n\n"
                   "- pdf-viewer:annotate: Collaboratively annotate a PDF: propose markup\n"
                   "- anthropic-skills:canvas-design\n- jev:mid: Run on Sonnet\n- simplify: Review changed code\n")
        second = {"type": "attachment", "attachment": {"type": "skill_listing", "content": content}}
        with open(path, "w") as handle:
            handle.write(json.dumps(first) + "\n" + json.dumps({"type": "user"}) + "\n" + json.dumps(second) + "\n")
        catalog = skills.catalog_from_claude_transcript(path)
        self.assertEqual([s["name"] for s in catalog], ["pdf-viewer:annotate", "anthropic-skills:canvas-design", "simplify"])
        self.assertEqual(catalog[0]["description"], "Collaboratively annotate a PDF: propose markup")
        self.assertEqual(catalog[1]["description"], "")

    def test_mentioned(self):
        self.assertTrue(skills.mentioned("please /simplify this", ["simplify"]))
        self.assertTrue(skills.mentioned("$pdf-viewer:annotate now", ["pdf-viewer:annotate"]))
        self.assertFalse(skills.mentioned("simplify the code", ["simplify"]))
