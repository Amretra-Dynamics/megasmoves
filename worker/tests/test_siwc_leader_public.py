"""Public ChatGPT Agent Leader: allowlisted, review-only proposals."""
import json
import unittest
from unittest.mock import patch
from megas.siwc_oss import ChatGPTConnectionError
from megas.siwc_leader import connected_agents, preview

AGENTS = [{"kind": "agent", "target": "codex", "name": "Codex Agent"}]


class FakePlanProvider:
    def __init__(self, steps=None, text=None):
        self.steps = steps if steps is not None else [
            {"kind": "agent", "target": "codex", "task": "Inspect tests only"}]
        self.text = text
        self.calls = []

    def infer(self, **kwargs):
        self.calls.append(kwargs)
        return {"provider": "chatgpt_plan", "model": kwargs["model"],
                "text": self.text if self.text is not None else json.dumps({
                    "summary": "Review first", "steps": self.steps})}


class AgentLeaderTests(unittest.TestCase):
    def proposal(self, provider=None, inventory=AGENTS):
        fake = provider or FakePlanProvider()
        return preview(account_email="owner@example.test", model="gpt-fixture",
                       goal="Inspect repository", client=fake, inventory=inventory), fake

    def test_only_connected_agents_discovered(self):
        rows = [
            {"client": "codex", "detected": True, "megas_configured": True,
             "catalog": {"display_name": "Codex Agent"}},
            {"client": "cursor", "detected": True, "megas_configured": False},
            {"client": "unknown", "detected": False, "megas_configured": True},
            {"client": "bad target", "detected": True, "megas_configured": True},
        ]
        with patch("megas.agent_management.status", return_value=rows):
            self.assertEqual(connected_agents(), AGENTS)

    def test_plan_is_review_only_and_never_assigns_tasks(self):
        with patch("megas.agent_management.create_task") as create, \
             patch("megas.agent_management.assign_task") as assign:
            result, source = self.proposal()
            self.assertFalse(result["dispatched"])
            self.assertTrue(result["review_required"])
            self.assertFalse(result["webmcp_available"])
            self.assertEqual(result["steps"][0]["state"], "awaiting_review")
            create.assert_not_called()
            assign.assert_not_called()
        self.assertEqual(source.calls[0]["email"], "owner@example.test")
        self.assertIn('"codex"', source.calls[0]["prompt"])

    def test_hallucinated_target_rejected(self):
        fake = FakePlanProvider(steps=[
            {"kind": "agent", "target": "invented", "task": "Deploy"}])
        with self.assertRaisesRegex(ChatGPTConnectionError, "invented"):
            self.proposal(fake)

    def test_webmcp_not_claimed_without_discovery(self):
        fake = FakePlanProvider(steps=[
            {"kind": "webmcp", "target": "webmcp:unseen",
             "task": "Make website changes"}])
        with self.assertRaisesRegex(ChatGPTConnectionError, "invented"):
            self.proposal(fake)

    def test_disconnected_inventory_rejected_before_inference(self):
        source = FakePlanProvider()
        with self.assertRaisesRegex(ChatGPTConnectionError, "No connected"):
            self.proposal(source, inventory=[])
        self.assertEqual(source.calls, [])

    def test_invalid_model_plan_shapes_rejected(self):
        for payload in ("not json", '{"summary":"x","steps":[]}',
                        '{"summary":"x","steps":{}}'):
            with self.subTest(payload=payload):
                with self.assertRaises(ChatGPTConnectionError):
                    self.proposal(FakePlanProvider(text=payload))


if __name__ == "__main__":
    unittest.main()
