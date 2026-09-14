import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from vibe_tools.editor_test_review import review_case
from vibe_tools.editor_test_plan import TestCase
from vibe_tools.editor_harness import AcceptanceHarness
from vibe_tools.tests.test_editor_behavior import fixture, move_case


class TestReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = TestCase.model_validate(move_case()).model_dump()
        self.replacement = copy.deepcopy(self.case)
        self.replacement["steps"][2]["frames"] = 18
        self.proposal = {"decision": "revise", "reason": "timing assumption", "evidence": "script and step 2",
                         "replacement": self.replacement}

    def run_review(self, responses):
        provider = Mock()
        provider.invoke.side_effect = [json.dumps(r) for r in responses]
        return review_case(provider, "player must move", {"sources": {}}, self.case, threading.Event()), provider

    def test_revision_requires_independent_coverage_approval(self):
        result, provider = self.run_review([self.proposal, {"approved": True, "reason": "original behavior remains tested"}])
        self.assertEqual(result["decision"], "revise")
        self.assertEqual(provider.invoke.call_count, 2)
        self.assertEqual(result["replacement"]["id"], self.case["id"])
        result, _ = self.run_review([self.proposal, {"approved": False, "reason": "weakens requirement"}])
        self.assertEqual(result["decision"], "blocked")

    def test_cannot_disable_or_remove_assertions(self):
        for field, value in (("enabled", False), ("id", "another"), ("steps", [])):
            proposal = copy.deepcopy(self.proposal)
            proposal["replacement"][field] = value
            result, provider = self.run_review([proposal])
            self.assertEqual(result["decision"], "blocked")
            provider.invoke.assert_called_once()

    def test_valid_ambiguous_invalid_and_cancel(self):
        for state in ("valid", "blocked"):
            result, _ = self.run_review([{"decision": state, "reason": "reason", "evidence": "source"}])
            self.assertEqual(result["decision"], state)
        result, _ = self.run_review([{"decision": "valid"}])
        self.assertEqual(result["decision"], "blocked")
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            review_case(Mock(), "requirement", {}, self.case, event)

    def test_empty_output_retries_without_whole_suite_prompt(self):
        provider = Mock()
        provider.invoke.side_effect = [None, json.dumps({"decision": "valid", "reason": "reasonable", "evidence": "source"})]
        result = review_case(provider, "move", {}, self.case, threading.Event())
        self.assertEqual(result["decision"], "valid")
        self.assertEqual(provider.invoke.call_count, 2)
        self.assertNotIn("生成 3~6", provider.invoke.call_args_list[0].args[0][0]["content"])
        self.assertEqual(provider.invoke.call_args.kwargs["max_tokens"], 16000)

    def test_empty_and_bad_json_have_distinct_bounded_errors(self):
        for raw, expected in ((None, "输出为空"), ("broken json", "非法 JSON")):
            provider = Mock()
            provider.invoke.return_value = raw
            result = review_case(provider, "move", {}, self.case, threading.Event())
            self.assertEqual(result["decision"], "blocked")
            self.assertIn(expected, result["reason"])
            self.assertEqual(provider.invoke.call_count, 2)

    def test_previous_reviews_are_not_resent(self):
        self.case["test_review"] = {"replacement": {"old": "PREVIOUS_REVIEW"}}
        result, provider = self.run_review([{"decision": "valid", "reason": "reason", "evidence": "source"}])
        self.assertEqual(result["decision"], "valid")
        self.assertNotIn("PREVIOUS_REVIEW", provider.invoke.call_args.args[0][1]["content"])

    def test_history_preserves_original_plan_and_marks_revision_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            fixture(project)
            harness = AcceptanceHarness(project, "")
            harness.save_plan({"cases": [self.case]})
            result = harness.apply_reviews({self.case["id"]: self.proposal}, "task", 1)
            self.assertEqual(result["cases"][0]["status"], "pending")
            self.assertEqual(result["cases"][0]["verified_fingerprint"], "")
            archive = next((harness.agent_dir / "plan_history").glob("*.json"))
            old = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual(old["cases"][0]["steps"], self.case["steps"])
            self.assertEqual(result["plan_revision"], 1)
