from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from vibe_tools import editor_sidecar as sidecar
from vibe_tools.editor_harness import AcceptanceHarness, project_fingerprint
from vibe_tools.editor_test_plan import TestCase
from vibe_tools.editor_workspace import Conflict, Workspace, rollback
from vibe_tools.tests.test_editor_behavior import fixture, move_case


def summary(status="failed", stale=False):
    return {"passed": int(status == "passed"), "failed": int(status == "failed"), "blocked": int(status == "blocked"),
            "cancelled": False, "stale": stale, "acceptance_ok": status == "passed",
            "preflight": {"status": "passed", "summary": "ok"}, "repair_round": 0,
            "plan": {"cases": [{**move_case(), "status": status, "result": {"actual": 0, "expected": 120, "repairable_defects": [{"actual": 0, "expected": 120}] if status == "failed" else []}}]}}


class ProviderWiringTests(unittest.TestCase):
    def test_text_and_vision_providers_actually_dispatch_requests(self):
        for visual in (False, True):
            with self.subTest(visual=visual), \
                    patch.object(sidecar, "credential", return_value="offline-test"), \
                    patch.dict(sidecar.Runtime.config, model="text-test", vision_model="vision-test", base_url=""), \
                    patch("vibe_tools.llm_transfer.SimpleLLMProvider._init_client") as init:
                client = init.return_value
                client.chat.completions.create.return_value = Mock(
                    choices=[Mock(message=Mock(content="ok"))])
                provider = sidecar._provider(visual)
                self.assertEqual(provider.invoke([{"role": "user", "content": "test"}]), "ok")
                client.chat.completions.create.assert_called_once()
                self.assertEqual(client.chat.completions.create.call_args.kwargs["model"],
                                 "vision-test" if visual else "text-test")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "game"
        fixture(self.project)
        sidecar.Runtime.project = self.project
        sidecar.Runtime.godot_exe = ""
        sidecar.Runtime.runs = {}
        sidecar.Runtime.token = "test-token"
        sidecar.Runtime.run_lock = threading.Lock()
        self.client = TestClient(sidecar.app)
        self.headers = {"Authorization": "Bearer test-token"}
        self.credentials = patch.object(sidecar, "credential", return_value="offline-test")
        self.credentials.start()
        self.provider = patch.object(sidecar, "_provider", return_value=Mock())
        self.provider.start()
        self.review = patch.object(sidecar, "review_case", return_value={"decision": "valid", "reason": "valid test", "evidence": "source and observations"})
        self.review.start()

    def tearDown(self):
        self.review.stop()
        self.provider.stop()
        self.credentials.stop()
        self.temp.cleanup()

    def new_run(self):
        _, run = sidecar._new_run("agent", {})
        run["expected_fingerprint"] = project_fingerprint(self.project)
        run["changed_files"] = []
        return run

    def test_failed_suite_repairs_three_times_and_freezes_test_definitions(self):
        run = self.new_run()
        harness = Mock()
        harness.apply_reviews.return_value = summary()["plan"]
        harness.run.side_effect = [copy.deepcopy(summary()) for _ in range(4)]
        repair = Mock()
        sidecar._repair_cycle(run, sidecar.HarnessRunRequest(case_ids=["move"]), harness, repair, "make player move")
        self.assertEqual(harness.run.call_count, 4)
        self.assertEqual(repair.call_count, 3)
        self.assertEqual(harness.run.call_args_list[0].args[0], ["move"])
        self.assertTrue(all(call.args[0] == [] for call in harness.run.call_args_list[1:]))
        self.assertIn('"expected": 120', repair.call_args.args[0])
        self.assertIn("make player move", repair.call_args.args[0])

    def test_pass_block_and_stale_do_not_trigger_repairs(self):
        for status, stale in (("passed", False), ("blocked", False), ("failed", True)):
            harness, repair = Mock(), Mock()
            harness.run.return_value = summary(status, stale)
            sidecar._repair_cycle(self.new_run(), sidecar.HarnessRunRequest(), harness, repair, "requirement")
            repair.assert_not_called()

    def test_warning_or_unclassified_failure_never_repairs(self):
        for state in ("passed", "failed", "blocked"):
            harness, repair = Mock(), Mock()
            report = summary(state)
            report["plan"]["cases"][0]["result"] = {"warning_count": 2, "repairable_defects": []}
            harness.run.return_value = report
            sidecar._repair_cycle(self.new_run(), sidecar.HarnessRunRequest(), harness, repair, "requirement")
            repair.assert_not_called()

    def test_repair_prompt_excludes_unconfirmed_diagnostics(self):
        harness, repair = Mock(), Mock()
        harness.apply_reviews.return_value = summary()["plan"]
        report = summary()
        report["plan"]["cases"][0]["result"]["diagnostics"] = [{"summary": "UNCONFIRMED_RUNTIME_ERROR"}]
        harness.run.side_effect = [report, summary("passed")]
        sidecar._repair_cycle(self.new_run(), sidecar.HarnessRunRequest(), harness, repair, "requirement")
        self.assertNotIn("UNCONFIRMED_RUNTIME_ERROR", repair.call_args.args[0])
        self.assertIn('"expected": 120', repair.call_args.args[0])

    def test_changed_project_stops_repair(self):
        run = self.new_run()
        (self.project / "game.gd").write_text("user changed", encoding="utf-8")
        with self.assertRaises(Conflict):
            sidecar._repair_cycle(run, sidecar.HarnessRunRequest(), Mock(), Mock(), "requirement")

    def test_invalid_test_is_revised_and_rerun_without_game_repair(self):
        harness, repair = Mock(), Mock()
        harness.run.side_effect = [summary(), summary("passed")]
        harness.apply_reviews.return_value = summary()["plan"]
        with patch.object(sidecar, "review_case", return_value={"decision": "revise", "reason": "timing race", "evidence": "source", "replacement": move_case()}):
            result = sidecar._repair_cycle(self.new_run(), sidecar.HarnessRunRequest(case_ids=["move"]), harness, repair, "requirement")
        self.assertEqual(harness.run.call_count, 2)
        self.assertEqual(result["plan_revision"], 1)
        self.assertEqual(result["repair_round"], 0)
        repair.assert_not_called()

    def test_single_run_does_not_review_another_cases_old_failure(self):
        harness, repair = Mock(), Mock()
        report = summary("passed")
        other = summary()["plan"]["cases"][0]
        other["id"] = "old_failure"
        report["plan"]["cases"].append(other)
        harness.run.return_value = report
        sidecar._repair_cycle(self.new_run(), sidecar.HarnessRunRequest(case_ids=["move"]), harness, repair, "requirement")
        sidecar.review_case.assert_not_called()
        repair.assert_not_called()

    def test_uncertain_test_blocks_and_revision_loop_is_bounded(self):
        for decision, count in (("blocked", 1), ("revise", 4)):
            harness, repair = Mock(), Mock()
            harness.run.side_effect = lambda *a, **kw: summary()
            def apply(reviews, task, revision):
                plan = summary()["plan"]
                plan["cases"][0]["status"] = "blocked" if reviews["move"]["decision"] == "blocked" else "pending"
                return plan
            harness.apply_reviews.side_effect = apply
            with patch.object(sidecar, "review_case", return_value={"decision": decision, "reason": "race", "evidence": "source", "replacement": move_case()}):
                result = sidecar._repair_cycle(self.new_run(), sidecar.HarnessRunRequest(), harness, repair, "requirement")
            self.assertEqual(harness.run.call_count, count)
            self.assertEqual(result["blocked"], 1)
            repair.assert_not_called()

    def test_latest_unsaved_editor_state_blocks_commit(self):
        run = self.new_run()
        workspace = Workspace(self.project, self.project / ".godot/agent", run["id"])
        original = (self.project / "game.gd").read_bytes()
        (workspace.stage / "game.gd").write_text("agent change")
        failures = []

        def commit():
            try:
                sidecar._commit_workspace(run, workspace, sidecar.AgentRequest(prompt="change"))
            except Exception as error:
                failures.append(error)
        worker = threading.Thread(target=commit)
        worker.start()
        name, event = run["events"].get(timeout=3)
        self.assertEqual(name, "editor_state_required")
        response = self.client.post("/v1/editor/state", headers=self.headers,
            json={"run_id": run["id"], "nonce": event["nonce"], "dirty_files": ["res://game.gd"]})
        self.assertEqual(response.status_code, 200)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(failures[0], Conflict)
        self.assertEqual((self.project / "game.gd").read_bytes(), original)
        response = self.client.post("/v1/editor/state", headers=self.headers,
            json={"run_id": run["id"], "nonce": event["nonce"], "dirty_files": []})
        self.assertEqual(response.status_code, 409)
        workspace.close()

    def test_running_suite_locks_plan_edits(self):
        sidecar.Runtime.run_lock.acquire()
        try:
            response = self.client.put("/v1/harness/plan", headers=self.headers, json={"cases": [move_case()]})
            self.assertEqual(response.status_code, 409)
        finally:
            sidecar.Runtime.run_lock.release()

    def test_no_change_conversation_does_not_generate_or_run_tests(self):
        generator = Mock()
        generator.agent_generate.return_value = {"reply": "explanation", "ok": True}
        with patch.object(sidecar, "GameGenerator", return_value=generator), patch.object(sidecar, "_generate_plan") as generate, patch.object(sidecar, "_repair_cycle") as cycle:
            run = self.new_run()
            sidecar._run_agent(run, sidecar.AgentRequest(prompt="explain", editor_sync=False))
        generate.assert_not_called()
        cycle.assert_not_called()
        self.assertEqual(run["status"], "done")

    def test_cancel_stops_before_next_repair(self):
        run = self.new_run()
        harness = Mock()
        harness.apply_reviews.return_value = summary()["plan"]
        value = summary()
        value["cancelled"] = True
        harness.run.return_value = value
        repair = Mock()
        sidecar._repair_cycle(run, sidecar.HarnessRunRequest(), harness, repair, "requirement")
        repair.assert_not_called()

    @unittest.skipUnless(os.getenv("GODOT_TEST_EXE"), "Requires real Godot")
    def test_real_respawn_damage_race_revises_test_without_editing_game(self):
        script = '''extends Node2D
var health: int = 0
var max_health: int = 3
var is_dead: bool = true
var frames_alive: int = 0
func _next() -> void:
    is_dead = false
    health = max_health
func _physics_process(_delta: float) -> void:
    if not is_dead:
        frames_alive += 1
        if frames_alive > 2:
            health = 2
'''
        path = self.project / "game.gd"
        path.write_text(script, encoding="utf-8")
        case = TestCase.model_validate({"id": "respawn", "title": "点击复活后存活", "steps": [
            {"op": "wait", "frames": 30},
            {"op": "mouse_move", "position": [80, 60]},
            {"op": "mouse_button", "pressed": True},
            {"op": "mouse_button", "pressed": False},
            {"op": "wait", "frames": 10},
            {"op": "assert", "node": ".", "property": "is_dead", "value": False},
            {"op": "assert", "node": ".", "property": "health", "value": 3}]}).model_dump()
        replacement = copy.deepcopy(case)
        replacement["steps"][-1].update(compare="gt", value=0)
        harness = AcceptanceHarness(self.project, os.environ["GODOT_TEST_EXE"])
        harness.save_plan({"cases": [case]})
        repair = Mock()
        with patch.object(sidecar, "review_case", return_value={"decision": "revise", "reason": "复活后两帧会受到攻击，稍后血量不保证等于上限", "evidence": "frames_alive > 2: health = 2", "replacement": replacement}):
            result = sidecar._repair_cycle(self.new_run(), sidecar.HarnessRunRequest(), harness, repair, "点击按钮后复活")
        repair.assert_not_called()
        self.assertTrue(result["acceptance_ok"], result)
        self.assertEqual(result["plan_revision"], 1)
        self.assertEqual(result["repair_round"], 0)
        self.assertEqual(path.read_text(encoding="utf-8"), script)
        reports = [json.loads(p.read_text(encoding="utf-8")) for p in harness.artifact_root.glob("*/respawn/report.json")]
        self.assertEqual({r["result"]["status"] for r in reports}, {"failed", "passed"})

    @unittest.skipUnless(os.getenv("GODOT_TEST_EXE"), "Requires real Godot")
    def test_real_game_failure_repair_retest_and_whole_task_rollback(self):
        sidecar.Runtime.godot_exe = os.environ["GODOT_TEST_EXE"]
        original = (self.project / "game.gd").read_bytes()
        calls = []
        generator = Mock()

        def modify(prompt, stage, callback, images):
            calls.append(prompt)
            fixture(Path(stage), speed=0 if len(calls) == 1 else 120)
            callback(Mock(to_dict=lambda: {"type": "edit", "description": "offline fixture change"}))
            return {"reply": "fixture change", "ok": True}
        generator.agent_generate.side_effect = modify
        cases = [TestCase.model_validate({**move_case(), "id": f"move_{i}", "generated": True}).model_dump() for i in range(3)]
        with patch.object(sidecar, "GameGenerator", return_value=generator), patch.object(sidecar, "generate_cases", return_value=cases):
            run = self.new_run()
            sidecar._run_agent(run, sidecar.AgentRequest(prompt="moving works", editor_sync=False))
        events = []
        while not run["events"].empty():
            events.append(run["events"].get())
        self.assertEqual(run["status"], "done", events[-1])
        self.assertEqual(len(calls), 2)
        self.assertEqual(sum(name == "done" for name, _ in events), 1)
        self.assertEqual(events[-1][0], "done")
        self.assertTrue(events[-1][1]["acceptance_ok"])
        self.assertEqual(events[-1][1]["harness"]["repair_round"], 1)
        self.assertIn('"actual": 0', calls[1])
        journal = self.project / ".godot/agent/transactions" / run["id"] / "journal.json"
        rollback(journal, self.project, [])
        self.assertEqual((self.project / "game.gd").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
