import os

from helpers import FakeJev, JevTestCase, noul_all
from jev import jevclient, state

Q = {"q": {"type": "noul", "instructions": "x"}}


class JevClientTest(JevTestCase):
    def test_no_credentials_means_no_call(self):
        self.assertIsNone(jevclient.ask({}, Q))

    def test_typesafe_request_and_answer(self):
        with FakeJev(noul_all(0.75)) as fake:
            answers = jevclient.ask({"a": 1}, Q)
        self.assertEqual(jevclient.noul(answers, "q"), 0.75)
        sent = fake.requests[0]
        self.assertEqual(sent["body"]["model"], "jev-latest")
        self.assertEqual(sent["body"]["state"], {"a": 1})
        self.assertEqual(sent["auth"], "Bearer test-key")

    def test_openrouter_model_name(self):
        with FakeJev(noul_all(0.5)) as fake:
            os.environ.pop("TYPESAFE_API_KEY")
            os.environ["OPENROUTER_API_KEY"] = "or-key"
            jevclient.ask({}, Q)
        self.assertEqual(fake.requests[0]["body"]["model"], "~typesafe/jev-latest")
        self.assertEqual(fake.requests[0]["auth"], "Bearer or-key")

    def test_http_error_returns_none(self):
        with FakeJev(lambda body: None):
            self.assertIsNone(jevclient.ask({}, Q))

    def test_answer_validation(self):
        self.assertEqual(jevclient.choice({"s": {"choice": "a", "confidence": 0.8}}, "s"), ("a", 0.8))
        self.assertEqual(jevclient.choice({"s": {"choice": "a", "confidence": 3}}, "s"), (None, None))
        self.assertEqual(jevclient.choice(None, "s"), (None, None))
        self.assertIsNone(jevclient.noul({"q": {"noul": float("nan")}}, "q"))
        self.assertIsNone(jevclient.noul({"q": {"noul": True}}, "q"))
        self.assertIsNone(jevclient.noul({}, "q"))


class FailureLoggingTest(JevTestCase):
    def test_transport_failure_and_empty_answers_are_both_logged(self):
        os.environ["TYPESAFE_API_KEY"] = "k"
        os.environ["JEV_API_URL"] = "http://127.0.0.1:1/nope"
        self.assertIsNone(jevclient.ask({"request": "x"}, {"q": {"type": "noul"}}, timeout=1))
        with FakeJev(lambda body: None) as fake:  # replies 500
            os.environ["JEV_API_URL"] = fake.url
            self.assertIsNone(jevclient.ask({"request": "x"}, {"q": {"type": "noul"}}, timeout=2))
        errors = [e for e in state.events() if e["kind"] == "jev_error"]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e["python"] and "host" in e for e in errors))
