import os
import tempfile
from pathlib import Path

from helpers import JevTestCase
from jev import commands, config

EVENT = {"session_id": "s1", "cwd": "/tmp", "transcript_path": "/nope"}


class CommandsTest(JevTestCase):
    def test_match(self):
        self.assertEqual(commands.match("/jev"), [])
        self.assertEqual(commands.match("  /jev gate on "), ["gate", "on"])
        self.assertEqual(commands.match("$jev off"), ["off"])
        self.assertEqual(commands.match("[$jev](/x/skills/jev/SKILL.md) status"), ["status"])
        self.assertIsNone(commands.match("/jevx"))
        self.assertIsNone(commands.match("please run /jev"))
        self.assertIsNone(commands.match(None))
        self.assertEqual(commands.match("/jev:jev route on"), ["route", "on"])

    def test_route_and_advisor(self):
        self.assertIn("Tier routing: shadow.", commands.run(["route", "shadow"], EVENT))
        self.assertEqual(config.load_settings()["route"], "shadow")
        commands.run(["advisor", "off"], EVENT)
        self.assertFalse(config.load_settings()["advisor"])

    def test_settings_changes(self):
        self.assertIn("gate: on", commands.run(["gate", "on"], EVENT))
        self.assertEqual(config.load_settings()["gate"], "on")
        commands.run(["off"], EVENT)
        self.assertFalse(config.load_settings()["enabled"])
        commands.run(["on"], EVENT)
        self.assertTrue(config.load_settings()["enabled"])
        commands.run(["trim", "off"], EVENT)
        commands.run(["skills", "off"], EVENT)
        settings = config.load_settings()
        self.assertFalse(settings["trim"] or settings["skills"])

    def test_status_help_report_and_unknown(self):
        status = commands.run([], EVENT)
        self.assertIn("Jev: on · gate: shadow · trim: on · skills: on · search: on", status)
        self.assertIn("No Jev key found", status)
        self.assertIn("/jev gate off|shadow|on", commands.run(["help"], EVENT))
        self.assertIn("Gate (shadow)", commands.run(["gate"], EVENT))
        self.assertIn("Unknown /jev command", commands.run(["bogus"], EVENT))

    def test_codex_status_shows_router_state(self):
        os.environ["CODEX_HOME"] = tempfile.mkdtemp()
        config.save_settings({"router_port": 1})
        self.assertIn("Router: not installed", commands.run([], EVENT))
        Path(os.environ["CODEX_HOME"], "config.toml").write_text(
            'openai_base_url = "http://127.0.0.1:1/backend-api/codex"  # jev-router\n')
        status = commands.run([], EVENT)
        self.assertIn("route: on", status)
        self.assertIn("Router: installed but not responding on :1", status)

    def test_viewer_must_be_loopback(self):
        self.assertIn("go to http://127.0.0.1:4777/codex/v1/systemone",
                      commands.run(["viewer", "http://127.0.0.1:4777/codex/v1/systemone"], EVENT))
        self.assertEqual(config.load_settings()["jev_api_url"], "http://127.0.0.1:4777/codex/v1/systemone")
        self.assertIn("must be a loopback address", commands.run(["viewer", "https://evil.example/v1"], EVENT))
        self.assertEqual(config.load_settings()["jev_api_url"], "http://127.0.0.1:4777/codex/v1/systemone")
        commands.run(["viewer", "off"], EVENT)
        self.assertEqual(config.load_settings()["jev_api_url"], "")

    def test_plugin_chip_forms(self):
        self.assertEqual(commands.match("[@Jev](plugin://jev@personal) /jev\n"), [])
        self.assertEqual(commands.match("[@Jev](plugin://jev@personal) /jev route shadow"), ["route", "shadow"])
        self.assertEqual(commands.match("[$jev:jev](/x/skills/jev/SKILL.md) "), [])
        self.assertEqual(commands.match("[$jev](/x/SKILL.md) gate on"), ["gate", "on"])
        self.assertIsNone(commands.match("[@Other](plugin://x) do a thing"))
