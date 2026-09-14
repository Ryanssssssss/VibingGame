import json
import threading
import unittest
from unittest.mock import Mock

from vibe_tools.editor_test_plan import generate_cases


class PlanRecoveryTests(unittest.TestCase):
    def valid(self):
        return json.dumps({"cases": [{"title": str(i), "steps": [
            {"op": "assert", "node": ".", "compare": "exists", "value": True}
        ]} for i in range(3)]})

    def test_empty_and_invalid_json_are_corrected(self):
        provider = Mock()
        provider.invoke.side_effect = [None, '{"cases": [}', self.valid()]
        self.assertEqual(len(generate_cases(provider, {"prompt": "move"}, threading.Event())), 3)
        self.assertEqual(provider.invoke.call_count, 3)
        self.assertIn("未通过校验", provider.invoke.call_args.args[0][-1]["content"])

    def test_invalid_schema_and_missing_visual_are_corrected(self):
        provider = Mock()
        valid = json.loads(self.valid())
        valid["cases"][0]["steps"].append({"op": "visual", "criteria": "按钮可见"})
        provider.invoke.side_effect = ['{"cases":null}', self.valid(), json.dumps(valid)]
        cases = generate_cases(provider, {"prompt": "修改界面"}, threading.Event())
        self.assertTrue(cases[0]["requires_visual"])

    def test_exhaustion_is_readable_and_bounded(self):
        provider = Mock()
        provider.invoke.return_value = "invalid"
        with self.assertRaisesRegex(ValueError, "连续 3 次"):
            generate_cases(provider, {"prompt": "move"}, threading.Event())
        self.assertEqual(provider.invoke.call_count, 3)

    def test_cancel_prevents_retry(self):
        provider, cancel = Mock(), threading.Event()
        def response(*args, **kwargs):
            cancel.set()
            return None
        provider.invoke.side_effect = response
        with self.assertRaises(InterruptedError):
            generate_cases(provider, {"prompt": "move"}, cancel)
        provider.invoke.assert_called_once()
