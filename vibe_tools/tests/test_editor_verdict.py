import copy
import unittest

from vibe_tools.editor_verdict import aggregate


class VerdictTests(unittest.TestCase):
    def verdict(self, logs="", visual="passed", process="passed", complete=True):
        result = {"status": "passed", "complete": complete, "observations": [
            {"step": {"op": "assert"}, "status": "passed"},
            {"step": {"op": "visual"}, "status": visual,
             "visual": {"status": visual, "summary": "visual evidence"}}]}
        return aggregate(result, {"status": process}, logs)

    def test_exit_cleanup_is_warning_only_after_normal_completion(self):
        logs = "ERROR: 2 resources still in use at exit.\n   at: ResourceCache::clear (core/io/resource.cpp:814)\n"
        for verbose in ("", "Loading resource: res://__vibe_test/bridge.gd\n"):
            result = self.verdict(verbose + logs)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["warning_count"], 1)
            self.assertEqual(result["repairable_defects"], [])
        self.assertEqual(self.verdict(logs, complete=False)["status"], "blocked")
        self.assertEqual(self.verdict(logs, process="failed")["status"], "blocked")

    def test_warning_does_not_hide_visual_failure_or_block(self):
        logs = "ERROR: 1 resources still in use at exit.\n"
        for state in ("failed", "blocked"):
            result = self.verdict(logs, visual=state)
            self.assertEqual(result["status"], state)
            self.assertEqual(bool(result["repairable_defects"]), state == "failed")

    def test_diagnostic_ownership_and_unknown_error(self):
        for path, expected in (("res://player.gd", "failed"), ("res://__vibe_test/bridge.gd", "blocked")):
            result = self.verdict("SCRIPT ERROR: Invalid call\n   at: run (" + path + ":12)\nLoading resource: res://__vibe_test/bridge.gd\n")
            self.assertEqual(result["status"], expected)
        self.assertEqual(self.verdict("ERROR: unknown error\n")["status"], "blocked")
        self.assertEqual(self.verdict("SCRIPT ERROR: bad call\n   at: player (res://player.gd:2)\n   at: input (res://__vibe_test/bridge.gd:4)\n")["status"], "failed")
        self.assertEqual(self.verdict("ERROR: 1 resources still in use at exit.\n   at: fake (res://player.gd:2)\n")["status"], "failed")

    def test_failure_wins_over_block_but_cancel_wins_over_failure(self):
        result = self.verdict("ERROR: unknown\n", visual="failed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["diagnostics"][0]["status"], "blocked")
        self.assertEqual(len(result["repairable_defects"]), 1)
        self.assertEqual(self.verdict(visual="failed", process="cancelled")["status"], "cancelled")
