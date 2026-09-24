import contextlib
import importlib.util
import io
import os
import tempfile
from pathlib import Path

from helpers import PLUGIN, JevTestCase


def load_installer():
    spec = importlib.util.spec_from_file_location("codex_router", str(PLUGIN / "scripts" / "codex_router.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORIGINAL = ('model = "gpt-6-astra"\nopenai_base_url = "https://example.test"\n\n[features]\nfoo = true\n\n'
            '[plugins."jev@personal"]\nenabled = true\n')


class InstallerTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.installer = load_installer()
        self.codex_home = tempfile.mkdtemp()
        os.environ["CODEX_HOME"] = self.codex_home
        self.config = Path(self.codex_home, "config.toml")
        self.config.write_text(ORIGINAL)

    def run_installer(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.installer.main(list(args))
        return out.getvalue()

    def test_toml_edits(self):
        text, previous = self.installer.set_top_level("a = 1\n[t]\nb = 2\n", "openai_base_url", '"x"')
        self.assertEqual((text, previous), ('a = 1\nopenai_base_url = "x"  # jev-router\n[t]\nb = 2\n', None))
        text, previous = self.installer.set_in_table("[features]\nfoo = true\n", "features",
                                                     "enable_request_compression", "false")
        self.assertEqual(text, "[features]\nenable_request_compression = false  # jev-router\nfoo = true\n")
        text, _ = self.installer.set_in_table("a = 1\n", "features", "enable_request_compression", "false")
        self.assertEqual(text, "a = 1\n\n[features]\nenable_request_compression = false  # jev-router\n")

    def test_install_then_uninstall_restores_config(self):
        self.run_installer(*["install", "--no-launchd", "--port", "47999"])
        text = self.config.read_text()
        self.assertIn('openai_base_url = "http://127.0.0.1:47999/backend-api/codex"  # jev-router', text)
        self.assertIn("enable_request_compression = false  # jev-router", text)
        self.assertNotIn("https://example.test", text)
        self.assertTrue(Path(self.home, "app", "router", "proxy.py").exists())
        self.assertTrue(Path(self.home, "app", "jev", "proxy_rules.py").exists())
        self.run_installer(*["uninstall", "--no-launchd"])
        self.assertEqual(self.config.read_text(), ORIGINAL)

    def test_api_key_mode_uses_v1(self):
        self.run_installer(*["install", "--no-launchd", "--api-key"])
        self.assertIn("/v1\"  # jev-router", self.config.read_text())

    def test_plist(self):
        data = self.installer.plist(47821, Path("/x/app"))
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", "/x/app/router/proxy.py", "--port", "47821"])
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(data["EnvironmentVariables"]["JEV_PLATFORM"], "codex")
