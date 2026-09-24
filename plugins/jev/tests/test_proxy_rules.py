import json
import unittest

import helpers  # noqa: F401
from jev import proxy_rules

TURN_HEADERS = {"x-codex-turn-metadata": json.dumps({"session_id": "s1", "turn_id": "t2", "request_kind": "turn"})}


def request(model="gpt-6-astra", effort="xhigh", items=None, **extra):
    body = {"model": model, "reasoning": {"effort": effort, "summary": "auto"}, "stream": True,
            "input": items if items is not None else [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]},
                {"type": "reasoning", "encrypted_content": "old"},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second"}]},
            ]}
    body.update(extra)
    return json.dumps(body).encode()


def decide(model, effort):
    return lambda session, turn: {"model": model, "effort": effort} if (session, turn) == ("s1", "t2") else None


class ProxyRulesTest(unittest.TestCase):
    def setUp(self):
        self.memory = proxy_rules.Memory()

    def sent(self, plan, label="primary"):
        return json.loads(dict(plan.attempts)[label])

    def test_other_request_kinds_and_subagents_are_untouched(self):
        for headers in ({"x-codex-turn-metadata": json.dumps({"session_id": "s1", "turn_id": "t2",
                                                               "request_kind": "compaction"})},
                        dict(TURN_HEADERS, **{"x-openai-subagent": "review"}), {}):
            plan = proxy_rules.plan(request(), headers, decide("gpt-5.6-luna", "low"), self.memory)
            self.assertFalse(plan.routed)
            self.assertEqual([label for label, _ in plan.attempts], ["original"])

    def test_no_decision_or_bad_json_is_untouched(self):
        self.assertFalse(proxy_rules.plan(request(), TURN_HEADERS, lambda s, t: None, self.memory).routed)
        self.assertFalse(proxy_rules.plan(b"not json", TURN_HEADERS, decide("x", "low"), self.memory).routed)

    def test_model_route_rewrites_model_and_effort_with_ordered_fallbacks(self):
        plan = proxy_rules.plan(request(), TURN_HEADERS, decide("gpt-5.6-luna", "low"), self.memory)
        self.assertTrue(plan.routed)
        body = self.sent(plan)
        self.assertEqual((body["model"], body["reasoning"]["effort"], body["reasoning"]["summary"]),
                         ("gpt-5.6-luna", "low", "auto"))
        self.assertIn({"type": "reasoning", "encrypted_content": "old"}, body["input"])  # keep, for cache reuse
        self.assertEqual([label for label, _ in plan.attempts], ["primary", "stripped", "original"])
        stripped = self.sent(plan, "stripped")
        self.assertNotIn({"type": "reasoning", "encrypted_content": "old"}, stripped["input"])
        self.assertEqual(self.sent(plan, "original")["model"], "gpt-6-astra")

    def test_strip_keeps_current_turn_reasoning(self):
        items = json.loads(request())["input"] + [{"type": "reasoning", "encrypted_content": "now"},
                                                  {"type": "function_call_output", "output": "x"}]
        kept = proxy_rules.strip_previous_reasoning(items)
        self.assertIn({"type": "reasoning", "encrypted_content": "now"}, kept)
        self.assertNotIn({"type": "reasoning", "encrypted_content": "old"}, kept)

    def test_remembered_strip_is_applied_first(self):
        self.memory.strip.add("s1")
        plan = proxy_rules.plan(request(), TURN_HEADERS, decide("gpt-5.6-luna", "low"), self.memory)
        self.assertNotIn({"type": "reasoning", "encrypted_content": "old"}, self.sent(plan)["input"])
        self.assertEqual([label for label, _ in plan.attempts], ["primary", "original"])

    def test_astra_effort_change_uses_configuration_update_and_stays_in_place(self):
        plan = proxy_rules.plan(request(effort="xhigh"), TURN_HEADERS, decide("gpt-6-astra", "high"), self.memory)
        body = self.sent(plan)
        self.assertEqual(body["reasoning"]["effort"], "xhigh")  # request-level effort untouched: cache kept
        self.assertEqual(body["input"][4], {"type": "configuration_update", "reasoning": {"effort": "high"}})
        self.assertEqual([label for label, _ in plan.attempts], ["primary", "no_update", "original"])
        self.assertEqual(self.sent(plan, "no_update")["reasoning"]["effort"], "high")
        proxy_rules.remember(self.memory, plan, "primary")
        # Next request in the same turn: tool output appended; the update stays after the same user message.
        later = json.loads(request())["input"] + [{"type": "function_call_output", "output": "x"}]
        plan2 = proxy_rules.plan(request(items=later), TURN_HEADERS, decide("gpt-6-astra", "high"), self.memory)
        body2 = self.sent(plan2)
        self.assertEqual(body2["input"][4], {"type": "configuration_update", "reasoning": {"effort": "high"}})
        self.assertEqual(sum(1 for i in body2["input"] if i["type"] == "configuration_update"), 1)

    def test_next_turn_back_to_default_effort_adds_a_second_update(self):
        first = proxy_rules.plan(request(), TURN_HEADERS, decide("gpt-6-astra", "high"), self.memory)
        proxy_rules.remember(self.memory, first, "primary")
        items = json.loads(request())["input"] + [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "third"}]}]
        headers = {"x-codex-turn-metadata": json.dumps({"session_id": "s1", "turn_id": "t3", "request_kind": "turn"})}
        plan = proxy_rules.plan(request(items=items), headers, lambda s, t: {"model": "gpt-6-astra", "effort": "xhigh"},
                                self.memory)
        updates = [(i, item["reasoning"]["effort"]) for i, item in enumerate(self.sent(plan)["input"])
                   if item["type"] == "configuration_update"]
        self.assertEqual(updates, [(4, "high"), (7, "xhigh")])

    def test_turn_without_decision_keeps_earlier_insertions_and_restores_effort(self):
        first = proxy_rules.plan(request(), TURN_HEADERS, decide("gpt-6-astra", "high"), self.memory)
        proxy_rules.remember(self.memory, first, "primary")
        items = json.loads(request())["input"] + [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "third"}]}]
        headers = {"x-codex-turn-metadata": json.dumps({"session_id": "s1", "turn_id": "t3", "request_kind": "turn"})}
        plan = proxy_rules.plan(request(items=items), headers, lambda s, t: None, self.memory)
        updates = [item["reasoning"]["effort"] for item in self.sent(plan)["input"]
                   if item["type"] == "configuration_update"]
        self.assertEqual(updates, ["high", "xhigh"])  # prefix unchanged, then back to the requested effort

    def test_turn_after_a_model_route_gets_a_stripped_fallback(self):
        first = proxy_rules.plan(request(), TURN_HEADERS, decide("gpt-5.6-luna", "low"), self.memory)
        proxy_rules.remember(self.memory, first, "primary")
        headers = {"x-codex-turn-metadata": json.dumps({"session_id": "s1", "turn_id": "t3", "request_kind": "turn"})}
        plan = proxy_rules.plan(request(), headers, lambda s, t: None, self.memory)
        self.assertEqual([label for label, _ in plan.attempts], ["original", "stripped"])
        self.assertEqual(dict(plan.attempts)["original"], request())
        proxy_rules.remember(self.memory, plan, "stripped")
        self.assertIn("s1", self.memory.strip)

    def test_compaction_forgets_insertion_points(self):
        first = proxy_rules.plan(request(), TURN_HEADERS, decide("gpt-6-astra", "high"), self.memory)
        proxy_rules.remember(self.memory, first, "primary")
        compaction = {"x-codex-turn-metadata": json.dumps({"session_id": "s1", "request_kind": "compaction"})}
        proxy_rules.plan(request(), compaction, lambda s, t: None, self.memory)
        self.assertNotIn("s1", self.memory.updates)

    def test_disabled_updates_fall_back_to_request_level_effort(self):
        self.memory.no_updates = True
        plan = proxy_rules.plan(request(), TURN_HEADERS, decide("gpt-6-astra", "high"), self.memory)
        self.assertEqual(self.sent(plan)["reasoning"]["effort"], "high")
        self.assertFalse(any(i["type"] == "configuration_update" for i in self.sent(plan)["input"]))

    def test_usage_from_sse(self):
        tail = (b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta"}\n\n'
                b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":'
                b'{"input_tokens":1000,"input_tokens_details":{"cached_tokens":800},"output_tokens":50}}}\n\n')
        self.assertEqual(proxy_rules.usage_from_sse(tail), {"input": 1000, "cached": 800, "output": 50})
        self.assertIsNone(proxy_rules.usage_from_sse(b"data: nothing\n\n"))
