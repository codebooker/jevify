import json
from pathlib import Path

from helpers import FakeJev, JevTestCase, noul_all
from jev import config, state, trim


def sample_log(n=600):
    lines = ["ok   test_case_%04d passed in 0.01s" % i for i in range(n)]
    lines[300] = "FAIL test_case_0300: expected 3, got 4"
    lines.append("1 failed, %d passed" % (n - 1))
    return "\n".join(lines)


class TrimTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.settings = config.load_settings()

    def event(self, command="pytest -q"):
        return {"session_id": "s1", "cwd": "/repo", "tool_name": "Bash",
                "tool_input": {"command": command}, "tool_use_id": "call-1"}

    def test_keeps_errors_and_summary_and_saves_full_output(self):
        output = sample_log()
        with FakeJev(noul_all(0.1)):
            result = trim.post_tool_use(self.event(), self.settings, output)
        self.assertEqual(result["decision"], "block")
        reason = result["reason"]
        self.assertTrue(reason.startswith("[jev trim] The command ran"))
        self.assertIn("FAIL test_case_0300", reason)
        self.assertIn("1 failed, 599 passed", reason)
        self.assertIn("lines omitted", reason)
        saved = Path(self.home, "outputs", "s1", "call-1.txt")
        self.assertEqual(saved.read_text(), output)
        self.assertIn(str(saved), reason)
        event = state.events("s1")[-1]
        self.assertEqual(event["kind"], "trim")
        self.assertLess(event["after"], event["before"])

    def test_budget_is_respected(self):
        with FakeJev(noul_all(0.9)):
            result = trim.post_tool_use(self.event(), self.settings, sample_log())
        body = result["reason"].split("\n", 1)[1]
        self.assertLessEqual(config.tokens(body), self.settings["trim_budget_tokens"] + 200)

    def test_untouched_cases(self):
        with FakeJev(noul_all(0.1)):
            self.assertIsNone(trim.post_tool_use(self.event(), self.settings, "short output"))
            self.assertIsNone(trim.post_tool_use(self.event(), self.settings, json.dumps(list(range(5000)))))
            self.assertIsNone(trim.post_tool_use(self.event("cat big.log"), self.settings, sample_log()))
            self.assertIsNone(trim.post_tool_use(self.event("git diff HEAD~1"), self.settings, sample_log()))
            off = dict(self.settings, trim=False)
            self.assertIsNone(trim.post_tool_use(self.event(), off, sample_log()))

    def test_jev_failure_leaves_output_alone(self):
        with FakeJev(lambda body: None):
            self.assertIsNone(trim.post_tool_use(self.event(), self.settings, sample_log()))
