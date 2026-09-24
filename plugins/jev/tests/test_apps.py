import os
import shutil
import tempfile
from pathlib import Path

from helpers import JevTestCase
from jev import apps, config


class AppsTest(JevTestCase):
    def temp(self):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_detect(self):
        os.environ.pop("JEV_PLATFORM")
        self.assertEqual(apps.detect({"turn_id": "t"}), "codex")
        os.environ["PLUGIN_ROOT"] = "/x"
        self.assertEqual(apps.detect({}), "codex")
        os.environ["JEV_PLATFORM"] = "claude"
        self.assertEqual(apps.detect({"turn_id": "t"}), "claude")

    def test_path_hint(self):
        self.assertEqual(apps.from_path("/Users/a/.codex/plugins/cache/personal/jev/1/jev/apps.py"), "codex")
        self.assertEqual(apps.from_path("/Users/a/.claude/plugins/cache/x/jev/1/jev/apps.py"), "claude")
        self.assertIsNone(apps.from_path("/Users/a/Documents/plugins/jev/jev/apps.py"))

    def test_homes_share_key_and_venv(self):
        os.environ.pop("JEV_HOME")
        codex_home, claude_home = self.temp(), self.temp()
        os.environ["CODEX_HOME"] = codex_home
        os.environ["CLAUDE_CONFIG_DIR"] = claude_home
        os.environ["JEV_PLATFORM"] = "claude"
        self.assertEqual(config.home(), Path(claude_home) / "jev")
        python = Path(codex_home) / "jev" / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("")
        (Path(codex_home) / "jev" / "credentials").write_text("OPENROUTER_API_KEY=shared\n")
        self.assertEqual(config.credentials(), {"OPENROUTER_API_KEY": "shared"})
        self.assertEqual(config.venv_python(), python)
        (Path(claude_home) / "jev").mkdir()
        (Path(claude_home) / "jev" / "credentials").write_text("OPENROUTER_API_KEY=own\n")
        self.assertEqual(config.credentials()["OPENROUTER_API_KEY"], "own")

    def test_jev_home_disables_fallback(self):
        self.assertEqual(config.homes(), [Path(self.home)])

    def test_model_rank_and_prices(self):
        self.assertLess(apps.model_rank("claude-haiku-4-5"), apps.model_rank("claude-sonnet-5"))
        self.assertLess(apps.model_rank("claude-sonnet-5"), apps.model_rank("claude-opus-5"))
        self.assertEqual(apps.model_rank("haiku"), 1)
        self.assertEqual(apps.model_rank(None), 3)
        self.assertEqual(apps.price_for("claude-sonnet-5")[0], 2)
        self.assertIsNone(apps.price_for("gpt-6-astra"))
