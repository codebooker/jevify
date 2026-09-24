import os
import tempfile
import unittest
from pathlib import Path

from helpers import JevTestCase

try:
    import graphify  # noqa: F401
    import networkx  # noqa: F401
except ImportError:
    raise unittest.SkipTest("graphify not installed in this interpreter")

from jevsearch import index, pipeline

AUTH = ("def login(user, password):\n    return check_password(user, password)\n\n\n"
        "def check_password(user, password):\n    return password == 'secret'\n")
BILLING = "def charge(card, amount):\n    return amount > 0\n"


class SearchTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.realpath(tempfile.mkdtemp())
        Path(self.repo, "auth.py").write_text(AUTH)
        Path(self.repo, "billing.py").write_text(BILLING)
        Path(self.repo, "node_modules").mkdir()
        Path(self.repo, "node_modules", "dep.js").write_text("function x() {}\n")

    def test_list_files_skips_excluded_directories(self):
        self.assertEqual(sorted(index.list_files(self.repo)), ["auth.py", "billing.py"])

    def test_home_folder_is_never_indexed(self):
        os.environ["HOME"] = self.repo
        self.assertEqual(index.list_files(self.repo), [])

    def test_fallback_walk_is_capped(self):
        original = index.MAX_WALK
        index.MAX_WALK = 1
        try:
            self.assertLessEqual(len(index.list_files(self.repo)), 1)
        finally:
            index.MAX_WALK = original

    def test_build_is_incremental(self):
        self.assertEqual(index.build(self.repo), "built")
        self.assertEqual(index.build(self.repo), "fresh")
        meta = index.read_meta(self.repo)
        self.assertEqual(meta["state"], "ready")
        self.assertGreater(meta["nodes"], 0)
        Path(self.repo, "billing.py").write_text(BILLING + "\ndef refund(card):\n    return True\n")
        self.assertTrue(index.is_stale(self.repo))
        self.assertEqual(index.build(self.repo), "built")

    def test_search_seeds_follow_jev_scores(self):
        index.build(self.repo)
        graph = index.load_graph(self.repo)

        def ask(state, questions, timeout=10):
            return {str(i): {"noul": 0.99 if "check_password" in c["label"] else 0.01}
                    for i, c in enumerate(state["candidates"])}

        text = pipeline.search(graph, self.repo, "password check", ask)
        self.assertTrue(text.startswith("Selected graph entry points: check_password"), text[:200])
        self.assertIn("auth.py", text)

    def test_search_falls_back_to_lexical_order(self):
        index.build(self.repo)
        text = pipeline.search(index.load_graph(self.repo), self.repo, "charge card", lambda *a, **k: None)
        self.assertIn("(Jev unavailable: lexical order)", text)
        self.assertIn("billing.py", text)

    def test_no_candidates(self):
        index.build(self.repo)
        text = pipeline.search(index.load_graph(self.repo), self.repo, "zzzqqq", lambda *a, **k: None)
        self.assertIn("No matching graph candidates", text)
