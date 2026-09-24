import os
import tempfile
from pathlib import Path

from helpers import JevTestCase
from jev import config, gate, state


class ClaudeGateTest(JevTestCase):
    def setUp(self):
        super().setUp()
        os.environ["JEV_PLATFORM"] = "claude"
        self.repo = os.path.realpath(tempfile.mkdtemp())
        Path(self.repo, "a.py").write_text("".join("line %d\n" % i for i in range(1, 101)))
        Path(self.repo, "big.py").write_text("".join("x = %d\n" % i for i in range(700)))
        self.settings = dict(config.load_settings(), gate="on")

    def event(self, tool, tool_input, tid="t1", response=None):
        event = {"session_id": "c1", "cwd": self.repo, "tool_name": tool, "tool_input": tool_input,
                 "tool_use_id": tid}
        if response is not None:
            event["tool_response"] = response
        return event

    def read_response(self, start=1, count=100, truncated=False):
        return {"type": "text", "file": {"filePath": os.path.join(self.repo, "a.py"), "startLine": start,
                                         "numLines": count, "totalLines": 100, "truncatedByTokenCap": truncated}}

    def denied(self, result):
        return bool(result) and result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_read_is_left_to_claudes_own_dedupe(self):
        read = {"file_path": os.path.join(self.repo, "a.py")}
        gate.record(self.event("Read", read, response=self.read_response()), "")
        self.assertIsNone(gate.pre_tool_use(self.event("Read", read), self.settings))

    def test_cat_after_read_is_a_duplicate(self):
        gate.record(self.event("Read", {"file_path": "a.py"}, response=self.read_response()), "")
        self.assertTrue(self.denied(gate.pre_tool_use(self.event("Bash", {"command": "cat a.py"}), self.settings)))

    def test_truncated_read_is_not_recorded(self):
        gate.record(self.event("Read", {"file_path": "a.py"}, response=self.read_response(truncated=True)), "")
        self.assertIsNone(gate.pre_tool_use(self.event("Bash", {"command": "cat a.py"}), self.settings))

    def test_huge_whole_file_read(self):
        self.assertTrue(self.denied(gate.pre_tool_use(self.event("Read", {"file_path": "big.py"}), self.settings)))
        ranged = {"file_path": "big.py", "offset": 100, "limit": 50}
        self.assertIsNone(gate.pre_tool_use(self.event("Read", ranged), self.settings))

    def test_grep_and_glob_duplicates(self):
        grep = {"pattern": "foo", "path": self.repo}
        gate.record(self.event("Grep", grep, response={"mode": "files_with_matches", "filenames": []}), "")
        self.assertTrue(self.denied(gate.pre_tool_use(self.event("Grep", grep), self.settings)))
        self.assertIsNone(gate.pre_tool_use(self.event("Grep", {"pattern": "bar"}), self.settings))
        gate.record(self.event("Glob", {"pattern": "*.py"}, response={"filenames": ["a.py"]}), "")
        self.assertTrue(self.denied(gate.pre_tool_use(self.event("Glob", {"pattern": "*.py"}), self.settings)))

    def test_multiedit_resets_search_memory(self):
        grep = {"pattern": "foo"}
        gate.record(self.event("Grep", grep, response={}), "")
        gate.record(self.event("MultiEdit", {"file_path": os.path.join(self.repo, "a.py"), "edits": []}), "")
        self.assertIsNone(gate.pre_tool_use(self.event("Grep", grep), self.settings))

    def test_skill_and_agent_use_are_logged(self):
        gate.record(self.event("Skill", {"skill": "jev:mid"}), "")
        gate.record(self.event("Agent", {"subagent_type": "jev:scout", "prompt": "find x"}), "")
        kinds = [(e["kind"], e.get("skill") or e.get("agent")) for e in state.events("c1")]
        self.assertEqual(kinds, [("skill_used", "jev:mid"), ("delegated", "jev:scout")])
