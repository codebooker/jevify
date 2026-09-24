import os
from pathlib import Path

from helpers import JevTestCase
from jev import config


class ConfigTest(JevTestCase):
    def test_defaults_without_file(self):
        settings = config.load_settings()
        self.assertEqual(settings["gate"], "shadow")
        self.assertTrue(settings["trim"] and settings["skills"] and settings["search"] and settings["enabled"])

    def test_save_persists_and_unknown_keys_are_ignored(self):
        config.save_settings({"gate": "on"})
        self.assertEqual(config.load_settings()["gate"], "on")
        (config.home() / "settings.json").write_text('{"gate": "off", "bogus": 1}')
        settings = config.load_settings()
        self.assertEqual(settings["gate"], "off")
        self.assertNotIn("bogus", settings)

    def test_corrupt_settings_fall_back_to_defaults(self):
        config.home().mkdir(parents=True, exist_ok=True)
        (config.home() / "settings.json").write_text("{not json")
        self.assertEqual(config.load_settings()["gate"], "shadow")

    def test_credentials_prefer_the_file_over_the_environment(self):
        config.home().mkdir(parents=True, exist_ok=True)
        (config.home() / "credentials").write_text("# comment\nOPENROUTER_API_KEY=file-key\nEMPTY=\n")
        self.assertEqual(config.credentials(), {"OPENROUTER_API_KEY": "file-key"})
        os.environ["OPENROUTER_API_KEY"] = "env-key"  # an app's hook env may carry someone else's key
        self.assertEqual(config.credentials()["OPENROUTER_API_KEY"], "file-key")

    def test_tokens_estimate(self):
        self.assertEqual(config.tokens("abcd" * 10), 10)
        self.assertEqual(config.tokens(""), 0)

    def test_index_dir_is_stable_per_root(self):
        self.assertEqual(config.index_dir("/a"), config.index_dir("/a"))
        self.assertNotEqual(config.index_dir("/a"), config.index_dir("/b"))

    def test_repo_root_without_git_is_the_path(self):
        self.assertEqual(config.repo_root(self.home), os.path.realpath(self.home))


class CredentialPrecedenceTest(JevTestCase):
    def test_a_configured_key_beats_one_from_the_environment(self):
        Path(self.home, "credentials").write_text("OPENROUTER_API_KEY=from-file\n")
        os.environ["OPENROUTER_API_KEY"] = "from-env"
        self.assertEqual(config.credentials()["OPENROUTER_API_KEY"], "from-file")
        os.environ["TYPESAFE_API_KEY"] = "only-in-env"
        self.assertEqual(config.credentials()["TYPESAFE_API_KEY"], "only-in-env")
