import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from helpers import PLUGIN, FakeJev, JevTestCase, noul_all
from jev import config, state

HOOKMAIN = str(PLUGIN / "jev" / "hookmain.py")
SCHEMAS = PLUGIN / "tests" / "fixtures" / "codex-hook-schemas"
try:
    import jsonschema
except ImportError:
    jsonschema = None


class HookMainTest(JevTestCase):
    def call(self, event_name, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        done = subprocess.run([sys.executable, HOOKMAIN, event_name], input=raw, capture_output=True,
                              text=True, env=dict(os.environ), timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if done.stdout.strip() else None

    def check(self, schema_name, output):
        if jsonschema is None:
            self.skipTest("jsonschema not installed; run with: uv run --no-project --with jsonschema ...")
        schema = json.loads((SCHEMAS / (schema_name + ".command.output.schema.json")).read_text())
        jsonschema.validate(output, schema)

    def test_jev_command_blocks_prompt(self):
        out = self.call("UserPromptSubmit", {"session_id": "s1", "prompt": "/jev help", "cwd": "/tmp"})
        self.assertEqual(out["decision"], "block")
        self.assertIn("/jev commands", out["reason"])
        self.check("user-prompt-submit", out)

    def test_normal_prompt_is_saved_as_task(self):
        self.assertIsNone(self.call("UserPromptSubmit", {"session_id": "s1", "prompt": "fix login", "cwd": "/tmp",
                                                         "transcript_path": "/nope"}))
        self.assertEqual(state.read("s1")["task"], "fix login")

    def test_skill_hint_matches_schema(self):
        transcript = os.path.join(self.home, "t.jsonl")
        block = "<skills_instructions>\n- `r0` = `/s`\n- logo: Make logos. (file: r0/logo/SKILL.md)\n</skills_instructions>"
        Path(transcript).write_text(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "developer", "content": [{"type": "input_text", "text": block}]}}) + "\n")
        with FakeJev(lambda body: {"skill": {"choice": "logo", "confidence": 0.9}}):
            out = self.call("UserPromptSubmit", {"session_id": "s1", "prompt": "design a logo",
                                                 "transcript_path": transcript, "cwd": "/tmp"})
        self.assertIn("`logo`", out["hookSpecificOutput"]["additionalContext"])
        self.check("user-prompt-submit", out)

    def test_gate_deny_and_trim_block_match_schemas(self):
        config.save_settings({"gate": "on"})
        repo = tempfile.mkdtemp()
        Path(repo, "big.py").write_text("x = 1\n" * 700)
        event = {"session_id": "s1", "cwd": repo, "tool_name": "Bash", "tool_input": {"command": "cat big.py"},
                 "tool_use_id": "t1", "hook_event_name": "PreToolUse"}
        out = self.call("PreToolUse", event)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.check("pre-tool-use", out)
        log = "\n".join("ok test %d" % i for i in range(900)) + "\nFAIL test 5\n1 failed"
        post = dict(event, tool_input={"command": "pytest"}, tool_response=log, hook_event_name="PostToolUse")
        with FakeJev(noul_all(0.1)):
            out = self.call("PostToolUse", post)
        self.assertEqual(out["decision"], "block")
        self.check("post-tool-use", out)

    def test_bad_input_never_fails(self):
        self.assertIsNone(self.call("PreToolUse", "not json"))
        self.assertEqual(state.events()[-1]["kind"], "error")

    def test_disabled_is_a_no_op_except_commands(self):
        config.save_settings({"enabled": False, "gate": "on"})
        repo = tempfile.mkdtemp()
        Path(repo, "big.py").write_text("x = 1\n" * 700)
        self.assertIsNone(self.call("PreToolUse", {"session_id": "s1", "cwd": repo, "tool_name": "Bash",
                                                   "tool_input": {"command": "cat big.py"}, "tool_use_id": "t"}))
        out = self.call("UserPromptSubmit", {"session_id": "s1", "prompt": "/jev on", "cwd": repo})
        self.assertEqual(out["reason"], "Jev is now on.")

    def test_session_start_records_cwd(self):
        self.assertIsNone(self.call("SessionStart", {"session_id": "s7", "cwd": "/tmp/project", "source": "startup"}))
        self.assertEqual(state.read("s7")["cwd"], "/tmp/project")

    def test_cli_mode(self):
        done = subprocess.run([sys.executable, HOOKMAIN, "cli", "gate", "on"], capture_output=True, text=True,
                              env=dict(os.environ), timeout=30)
        self.assertEqual(done.stdout.strip(), "Tool-call gate: on.")
