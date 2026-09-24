import json
import os
import subprocess
import sys

from helpers import PLUGIN, FakeJev, JevTestCase
from jev import state

HOOKMAIN = str(PLUGIN / "jev" / "hookmain.py")


class ClaudeHookMainTest(JevTestCase):
    def setUp(self):
        super().setUp()
        os.environ["JEV_PLATFORM"] = "claude"

    def call(self, event_name, payload):
        done = subprocess.run([sys.executable, HOOKMAIN, event_name], input=json.dumps(payload),
                              capture_output=True, text=True, env=dict(os.environ), timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if done.stdout.strip() else None

    def test_jev_command_suppresses_prompt(self):
        out = self.call("UserPromptSubmit", {"session_id": "c1", "prompt": "/jev help", "cwd": "/tmp"})
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["hookSpecificOutput"]["suppressOriginalPrompt"])
        self.assertIn("/jev route off|shadow|on", out["reason"])

    def test_prompt_goes_through_router(self):
        transcript = os.path.join(self.home, "t.jsonl")
        with open(transcript, "w") as handle:
            handle.write(json.dumps({"type": "assistant", "message": {"id": "m", "model": "claude-opus-5",
                                                                      "usage": {"input_tokens": 5000}}}) + "\n")
        with FakeJev(lambda body: {"tier": {"choice": "mid", "confidence": 0.9}, "followup": {"noul": 0.0}}):
            out = self.call("UserPromptSubmit", {"session_id": "c1", "prompt": "add a flag", "cwd": "/tmp",
                                                 "transcript_path": transcript})
        self.assertIn("jev:worker", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("jev → Sonnet", out["systemMessage"])

    def test_session_start_records_model_and_no_trim_on_claude(self):
        self.call("SessionStart", {"session_id": "c2", "cwd": "/tmp", "source": "startup", "model": "claude-opus-5"})
        self.assertEqual(state.read("c2")["model"], "claude-opus-5")
        log = "\n".join("ok test %d" % i for i in range(900)) + "\nFAIL x\n1 failed"
        post = {"session_id": "c2", "cwd": "/tmp", "tool_name": "Bash", "tool_input": {"command": "pytest"},
                "tool_response": {"stdout": log, "stderr": ""}, "tool_use_id": "t1"}
        self.assertIsNone(self.call("PostToolUse", post))
