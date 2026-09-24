import os
import tempfile
from pathlib import Path

from helpers import FakeJev, JevTestCase, noul_all
from jev import config, gate, state


class GateTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.realpath(tempfile.mkdtemp())
        Path(self.repo, "a.py").write_text("".join("line %d\n" % i for i in range(1, 101)))
        Path(self.repo, "big.py").write_text("".join("x = %d\n" % i for i in range(700)))
        self.settings = config.load_settings()

    def event(self, command, tool="Bash", tid="t1"):
        return {"session_id": "s1", "cwd": self.repo, "tool_name": tool,
                "tool_input": {"command": command}, "tool_use_id": tid}

    def executed(self, command, output="ok"):
        result = gate.pre_tool_use(self.event(command), self.settings)
        if result is None:
            gate.record(self.event(command), output)
        return result

    def decision(self, result):
        return result and result["hookSpecificOutput"]["permissionDecision"]

    def test_duplicate_read_denied_when_on(self):
        self.settings["gate"] = "on"
        self.assertIsNone(self.executed("sed -n '1,50p' a.py"))
        result = gate.pre_tool_use(self.event("sed -n '10,20p' a.py"), self.settings)
        self.assertEqual(self.decision(result), "deny")
        self.assertIn("already in your context", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_repeat_after_deny_is_allowed_and_logged(self):
        self.settings["gate"] = "on"
        self.executed("sed -n '1,50p' a.py")
        self.assertEqual(self.decision(gate.pre_tool_use(self.event("sed -n '1,50p' a.py"), self.settings)), "deny")
        self.assertIsNone(gate.pre_tool_use(self.event("sed -n '1,50p' a.py"), self.settings))
        self.assertEqual([e["kind"] for e in state.events("s1")][-1], "override")

    def test_shadow_logs_but_allows(self):
        self.executed("sed -n '1,50p' a.py")
        self.assertIsNone(gate.pre_tool_use(self.event("sed -n '1,50p' a.py", tid="t2"), self.settings))
        event = state.events("s1")[-1]
        self.assertEqual((event["kind"], event["rule"], event["mode"]), ("gate", "duplicate_read", "shadow"))
        self.assertGreater(event["tokens"], 0)

    def test_changed_file_is_not_a_duplicate(self):
        self.settings["gate"] = "on"
        self.executed("cat a.py")
        Path(self.repo, "a.py").write_text("changed\n")
        self.assertIsNone(gate.pre_tool_use(self.event("cat a.py"), self.settings))

    def test_truncated_output_is_not_recorded(self):
        self.settings["gate"] = "on"
        self.executed("cat a.py", output="line 1\n…512 tokens truncated…\nline 100")
        self.assertIsNone(gate.pre_tool_use(self.event("cat a.py"), self.settings))

    def test_huge_whole_file_read(self):
        self.settings["gate"] = "on"
        result = gate.pre_tool_use(self.event("cat big.py"), self.settings)
        self.assertEqual(self.decision(result), "deny")
        self.assertIn("700 lines", result["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(gate.pre_tool_use(self.event("sed -n '1,100p' big.py"), self.settings))

    def test_duplicate_search_until_something_changes(self):
        self.settings["gate"] = "on"
        self.executed("rg -n foo", output="a.py:1:foo")
        self.assertEqual(self.decision(gate.pre_tool_use(self.event("rg -n foo"), self.settings)), "deny")
        gate.pre_tool_use(self.event("rg -n foo"), self.settings)  # override consumes the denial
        gate.record(self.event("*** Begin Patch\n*** Update File: a.py\n", tool="apply_patch"), "")
        self.assertIsNone(gate.pre_tool_use(self.event("rg -n foo"), self.settings))

    def test_compaction_clears_memory(self):
        self.settings["gate"] = "on"
        self.executed("cat a.py")
        gate.compacted({"session_id": "s1"})
        self.assertIsNone(gate.pre_tool_use(self.event("cat a.py"), self.settings))

    def test_other_commands_and_gate_off(self):
        self.assertIsNone(gate.pre_tool_use(self.event("go test ./..."), self.settings))
        self.settings["gate"] = "off"
        self.assertIsNone(gate.pre_tool_use(self.event("cat big.py"), self.settings))
        self.assertEqual(state.events("s1"), [])

    def test_jev_rule_uses_threshold(self):
        self.settings["gate"] = "on"
        with state.session("s1") as s:
            s["task"] = "fix login"
        with FakeJev(noul_all(0.95)) as fake:
            result = gate.pre_tool_use(self.event("sed -n '1,5p' a.py"), self.settings)
        self.assertEqual(self.decision(result), "deny")
        self.assertEqual(fake.requests[0]["body"]["state"]["task"], "fix login")
        with FakeJev(noul_all(0.5)):
            self.assertIsNone(gate.pre_tool_use(self.event("sed -n '6,9p' a.py"), self.settings))
        with FakeJev(lambda body: None):
            self.assertIsNone(gate.pre_tool_use(self.event("sed -n '9,12p' a.py"), self.settings))

    def test_later_edit_flags_a_would_deny(self):
        gate.pre_tool_use(self.event("cat big.py", tid="t9"), self.settings)
        gate.record(self.event("*** Begin Patch\n*** Update File: big.py\n", tool="apply_patch"), "")
        flags = [e for e in state.events("s1") if e["kind"] == "flag"]
        self.assertEqual([f["ref"] for f in flags], ["t9"])

    def test_skill_file_reads_are_logged(self):
        Path(self.repo, "SKILL.md").write_text("# skill\n")
        self.executed("cat SKILL.md")
        self.assertEqual(state.events("s1")[-1]["kind"], "skill_read")


class CoverageTest(JevTestCase):
    def test_every_tool_call_is_tallied_even_when_no_rule_matches(self):
        from jev import meter
        settings = config.load_settings()
        for tool, tool_input in (("Bash", {"command": "echo hi"}), ("Bash", {"command": "echo hi"}),
                                 ("exec", {"input": "tools.exec_command({cmd:'ls'})"})):
            gate.pre_tool_use({"session_id": "s1", "tool_name": tool, "tool_input": tool_input,
                               "cwd": "/tmp", "tool_use_id": "t"}, settings)
        report = meter.gate_coverage("s1")
        self.assertIn("Tool calls seen this session: 3", report)
        self.assertIn("exec 1", report)
        self.assertIn("not gated 1", report)


class BatchGateTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.realpath(tempfile.mkdtemp())
        Path(self.repo, "a.py").write_text("".join("line %d\n" % i for i in range(1, 101)))
        Path(self.repo, "b.py").write_text("".join("y = %d\n" % i for i in range(1, 51)))
        self.settings = config.save_settings({"gate": "on"})

    def event(self, command):
        return {"session_id": "s1", "cwd": self.repo, "tool_name": "Bash",
                "tool_input": {"command": command}, "tool_use_id": "t1"}

    def executed(self, command, output="ok"):
        result = gate.pre_tool_use(self.event(command), self.settings)
        if result is None:
            gate.record(self.event(command), output)
        return result

    def decision(self, result):
        return result and result["hookSpecificOutput"]["permissionDecision"]

    def test_a_batch_is_denied_only_when_every_part_is_redundant(self):
        batch = "sed -n '1,20p' a.py && sed -n '1,10p' b.py"
        self.assertIsNone(self.executed(batch))                      # first time: both new
        self.assertEqual(self.decision(self.executed(batch)), "deny")  # both already read
        mixed = "sed -n '1,20p' a.py && sed -n '30,40p' b.py"
        self.assertIsNone(self.executed(mixed))  # one repeat, one new part: must not be blocked

    def test_every_read_in_a_batch_is_remembered(self):
        self.executed("sed -n '1,20p' a.py && sed -n '1,10p' b.py")
        paths = {r["path"] for r in state.read("s1")["reads"]}
        self.assertEqual(paths, {os.path.join(self.repo, "a.py"), os.path.join(self.repo, "b.py")})

    def test_an_unknown_part_still_lets_the_known_reads_record(self):
        self.executed("npm test && sed -n '1,5p' a.py")
        self.assertTrue(any(r["path"].endswith("a.py") for r in state.read("s1")["reads"]))
