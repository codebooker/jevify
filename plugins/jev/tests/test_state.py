import os
import threading
import time

from helpers import JevTestCase
from jev import state


class StateTest(JevTestCase):
    def test_roundtrip_with_defaults(self):
        with state.session("abc") as s:
            s["task"] = "fix the bug"
            s["reads"].append({"path": "/x"})
        again = state.read("abc")
        self.assertEqual(again["task"], "fix the bug")
        self.assertEqual(again["reads"], [{"path": "/x"}])
        self.assertEqual(again["edits"], 0)

    def test_exception_discards_changes(self):
        with self.assertRaises(RuntimeError):
            with state.session("abc") as s:
                s["task"] = "lost"
                raise RuntimeError("boom")
        self.assertEqual(state.read("abc")["task"], "")

    def test_parallel_updates_are_serialized(self):
        def bump():
            for _ in range(25):
                with state.session("p") as s:
                    s["step"] += 1
        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(state.read("p")["step"], 100)

    def test_session_id_is_sanitized(self):
        self.assertEqual(state.path("../x").name, ".._x.json")

    def test_log_and_filter(self):
        state.log({"kind": "trim", "session": "a"})
        state.log({"kind": "trim", "session": "b"})
        self.assertEqual([e["session"] for e in state.events("a")], ["a"])
        self.assertEqual(len(state.events()), 2)
        self.assertIn("ts", state.events()[0])

    def test_cleanup_removes_old_sessions_only(self):
        with state.session("old") as s:
            s["task"] = "x"
        with state.session("new") as s:
            s["task"] = "y"
        past = time.time() - 10 * 86400
        os.utime(state.path("old"), (past, past))
        state.cleanup(days=7)
        self.assertFalse(state.path("old").exists())
        self.assertTrue(state.path("new").exists())
