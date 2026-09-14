import copy
import unittest
from unittest.mock import Mock
from pydantic import ValidationError
from vibe_tools.editor_language import LanguageProvider, INSTRUCTIONS
from vibe_tools.editor_sidecar import ConfigureRequest


class LanguageTests(unittest.TestCase):
    def test_default_and_allowed_settings(self):
        self.assertEqual(ConfigureRequest().response_language, "zh-CN")
        for language in INSTRUCTIONS:
            self.assertEqual(ConfigureRequest(response_language=language).response_language, language)
        with self.assertRaises(ValidationError):
            ConfigureRequest(response_language="unknown")

    def test_each_request_reapplies_language_without_changing_history(self):
        original = [{"role": "system", "content": "base"}, {"role": "user", "content": "make a game"}]
        before = copy.deepcopy(original)
        for language in INSTRUCTIONS:
            backend = Mock()
            provider = LanguageProvider(backend, language)
            provider.invoke(original, max_tokens=42)
            self.assertIn(INSTRUCTIONS[language], backend.invoke.call_args.args[0][0]["content"])
            self.assertEqual(backend.invoke.call_args.kwargs["max_tokens"], 42)
            provider.invoke_with_tools(original, [{"type": "function"}])
            self.assertIn(INSTRUCTIONS[language], backend.invoke_with_tools.call_args.args[0][0]["content"])
            self.assertEqual(original, before)

    def test_summary_without_system_gets_language(self):
        backend = Mock()
        LanguageProvider(backend, "en").invoke([{"role": "user", "content": "summarize"}])
        self.assertEqual(backend.invoke.call_args.args[0][0]["role"], "system")
        self.assertIn(INSTRUCTIONS["en"], backend.invoke.call_args.args[0][0]["content"])
