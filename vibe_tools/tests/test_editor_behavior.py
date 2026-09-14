from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vibe_tools.editor_behavior import BehaviorRunner
from vibe_tools.editor_harness import AcceptanceHarness, project_fingerprint
from vibe_tools.editor_test_plan import TestCase, change_context, generate_cases


def fixture(project: Path, speed=120):
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.godot").write_text('''config_version=5
[application]
config/name="Acceptance fixture"
run/main_scene="res://main.tscn"
[display]
window/size/viewport_width=640
window/size/viewport_height=360
[rendering]
renderer/rendering_method="gl_compatibility"
[input]
move_right={"deadzone":0.5,"events":[]}
jump={"deadzone":0.5,"events":[]}
''', encoding="utf-8")
    (project / "main.tscn").write_text('''[gd_scene load_steps=2 format=3]
[ext_resource type="Script" path="res://game.gd" id="1"]
[node name="Game" type="Node2D"]
script = ExtResource("1")
[node name="Player" type="Node2D" parent="."]
[node name="Next" type="Button" parent="."]
offset_left = 20.0
offset_top = 30.0
offset_right = 180.0
offset_bottom = 90.0
text = "Next scene"
[connection signal="pressed" from="Next" to="." method="_next"]
''', encoding="utf-8")
    (project / "second.tscn").write_text('''[gd_scene format=3]
[node name="Second" type="Node2D"]
''', encoding="utf-8")
    (project / "game.gd").write_text(f'''extends Node2D
func _physics_process(delta: float) -> void:
    if Input.is_action_pressed("move_right"):
        $Player.position.x += {speed} * delta
func _input(event: InputEvent) -> void:
    if event.is_action_pressed("jump"):
        $Player.position.y -= 20
    if event is InputEventKey and event.pressed and event.keycode == KEY_SPACE:
        $Player.position.y -= 10
func _next() -> void:
    get_tree().change_scene_to_file("res://second.tscn")
''', encoding="utf-8")


def move_case():
    return {"id": "move", "title": "玩家向右移动", "steps": [
        {"op": "read", "node": "Player", "property": "position:x", "store": "start"},
        {"op": "action", "action": "move_right", "pressed": True},
        {"op": "wait", "frames": 12},
        {"op": "action", "action": "move_right", "pressed": False},
        {"op": "assert", "node": "Player", "property": "position:x", "compare": "gt", "reference": "start"}]}


class PlanTests(unittest.TestCase):
    def test_invalid_steps_and_path_ids_are_rejected(self):
        for value in ({"id": "../../escape", "title": "x"},
                      {"title": "x", "steps": [{"op": "shell"}]},
                      {"title": "x", "steps": [{"op": "wait"}]},
                      {"title": "x", "requires_visual": True, "steps": move_case()["steps"]}):
            with self.assertRaises(ValueError):
                TestCase.model_validate(value)

    def test_generation_uses_context_and_demands_visual_evidence(self):
        provider = Mock()
        provider.invoke.return_value = json.dumps({"visual_change": True, "cases": [move_case()] * 3})
        with self.assertRaises(ValueError):
            generate_cases(provider, {"prompt": "修改 UI"}, threading.Event())
        visual = {"title": "画面", "steps": [{"op": "visual", "criteria": "按钮可见且文字不被裁切"}]}
        provider.invoke.return_value = json.dumps({"visual_change": True, "cases": [move_case(), move_case(), visual]})
        cases = generate_cases(provider, {"prompt": "修改 UI", "changes": [{"path": "main.tscn"}]}, threading.Event())
        self.assertEqual(len({case["id"] for case in cases}), 3)
        self.assertTrue(cases[-1]["requires_visual"])
        self.assertIn("main.tscn", provider.invoke.call_args.args[0][1]["content"])

    def test_legacy_and_client_verdicts_cannot_forge_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            fixture(project)
            harness = AcceptanceHarness(project, "")
            harness._persist({"version": 1, "cases": [{"id": "old", "title": "legacy", "status": "passed"}], "verified_fingerprint": "forged"})
            self.assertEqual(harness.load_plan()["cases"][0]["status"], "needs_generation")
            plan = harness.save_plan({"cases": [{**move_case(), "status": "passed", "artifacts": ["forged"], "verified_fingerprint": "forged"}], "verified_fingerprint": "forged"})
            self.assertEqual(plan["cases"][0]["status"], "pending")
            self.assertEqual(plan["verified_fingerprint"], "")
            self.assertEqual(plan["cases"][0]["artifacts"], [])

    def test_content_fingerprint_and_context_ignore_history(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            fixture(project)
            old = project_fingerprint(project)
            (project / "chat_history.json").write_text("history")
            self.assertEqual(project_fingerprint(project), old)
            path = project / "game.gd"
            before = path.read_bytes()
            metadata = path.stat()
            path.write_bytes(before.replace(b"120", b"999"))
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            self.assertNotEqual(project_fingerprint(project), old)
            context = change_context(project, {"game.gd": before}, "change speed")
            self.assertIn("999", context["changes"][0]["diff"])
            self.assertNotIn("chat_history.json", context["sources"])

    def test_old_rule_result_invalidates_verification_without_rewriting_report(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            fixture(project)
            harness = AcceptanceHarness(project, "")
            fingerprint = project_fingerprint(project)
            harness._persist({"version": 2, "verified_fingerprint": fingerprint, "cases": [
                {**move_case(), "status": "passed", "verified_fingerprint": fingerprint,
                 "result": {"status": "passed"}}]})
            original = harness.plan_file.read_bytes()
            plan = harness.load_plan()
            self.assertEqual(plan["cases"][0]["status"], "stale")
            self.assertIn("旧判定规则", plan["cases"][0]["failure"])
            self.assertEqual(plan["verified_fingerprint"], "")
            self.assertEqual(harness.plan_file.read_bytes(), original)

    def test_partial_pass_edit_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            fixture(project)
            harness = AcceptanceHarness(project, "")
            harness.save_plan({"cases": [move_case(), {**move_case(), "id": "second"}]})
            with patch.object(harness, "_preflight", return_value={"status": "passed", "summary": "ok"}), patch.object(harness.behavior, "run_case", return_value={"status": "passed", "summary": "ok"}):
                summary = harness.run(["move"], lambda *args: None, threading.Event())
                self.assertFalse(summary["acceptance_ok"])
                summary = harness.run([], lambda *args: None, threading.Event())
                self.assertTrue(summary["acceptance_ok"])
                plan = harness.load_plan()
                plan["cases"][0]["then"] = "new standard"
                self.assertEqual(harness.save_plan(plan)["cases"][0]["status"], "pending")
                cancel = threading.Event()
                cancel.set()
                summary = harness.run([], lambda *args: None, cancel)
                self.assertTrue(summary["cancelled"])
                self.assertFalse(summary["acceptance_ok"])
                self.assertTrue(all(case["status"] == "cancelled" for case in summary["plan"]["cases"]))

    def test_visual_three_way_judgement(self):
        with tempfile.TemporaryDirectory() as directory:
            picture = Path(directory) / "shot.png"
            picture.write_bytes(b"test-image")
            provider = Mock()
            runner = BehaviorRunner("", provider)
            for status in ("passed", "failed", "blocked"):
                provider.invoke.return_value = json.dumps({"status": status, "reason": "evidence"})
                self.assertEqual(runner.judge(picture, "criterion", threading.Event())["status"], status)
            runner.judge(picture, "criterion", threading.Event(), [{"step": {"op": "mouse_button"}, "status": "passed"}])
            self.assertIn("mouse_button", provider.invoke.call_args.args[0][1]["content"][0]["text"])
            provider.invoke.side_effect = RuntimeError("unavailable")
            self.assertEqual(runner.judge(picture, "criterion", threading.Event())["status"], "blocked")


@unittest.skipUnless(os.getenv("GODOT_TEST_EXE"), "Set GODOT_TEST_EXE to run real Godot integration tests")
class GodotBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "中文 game"
        fixture(self.project)
        self.runner = BehaviorRunner(os.environ["GODOT_TEST_EXE"], headless=True)

    def tearDown(self):
        self.temp.cleanup()

    def run_case(self, case, name="case", timeout=60):
        result = self.runner.run_case(self.project, case, self.root / name, threading.Event(), timeout)
        return result

    def test_real_movement_and_valid_script_with_wrong_behavior(self):
        result = self.run_case(move_case(), "correct")
        self.assertEqual(result["status"], "passed", result)
        self.assertGreater(result["observations"][-1]["actual"], 0)
        self.assertNotIn("GDScript backtrace", result["log_tail"])
        fixture(self.project, speed=0)
        result = self.run_case(move_case(), "wrong")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["observations"][-1]["actual"], 0)
        self.assertFalse((self.project / "__vibe_test").exists())

    def test_real_jump_key_and_scene_loading(self):
        case = {"title": "jump and key", "steps": [
            {"op": "action", "action": "jump"}, {"op": "action", "action": "jump", "pressed": False},
            {"op": "key", "key": "Space"}, {"op": "key", "key": "Space", "pressed": False},
            {"op": "assert", "node": "Player", "property": "position:y", "value": -30},
            {"op": "load_scene", "scene": "res://second.tscn"},
            {"op": "wait_until", "node": ".", "property": "scene_file_path", "value": "res://second.tscn", "frames": 30}]}
        result = self.run_case(case)
        self.assertEqual(result["status"], "passed", result)

    def test_real_mouse_click_and_rendered_screenshot(self):
        self.runner.headless = False
        self.runner.vision = Mock()
        self.runner.vision.invoke.return_value = '{"status":"passed","reason":"mock judge; actual screenshot captured"}'
        case = {"title": "click", "steps": [
            {"op": "wait", "frames": 10}, {"op": "visual", "criteria": "Next scene button visible"},
            {"op": "mouse_move", "position": [80, 60]},
            {"op": "mouse_button", "pressed": True}, {"op": "mouse_button", "pressed": False},
            {"op": "wait_until", "node": ".", "property": "scene_file_path", "value": "res://second.tscn", "frames": 30}]}
        result = self.run_case(case)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue((self.root / "case" / "step_001.png").read_bytes().startswith(b"\x89PNG"))
        self.assertNotIn("GDScript backtrace", result["log_tail"])

    def test_runtime_error_cannot_pass_and_cancel_cleans_process(self):
        script = self.project / "game.gd"
        script.write_text(script.read_text(encoding="utf-8") + '\nfunc _ready():\n    push_error("fixture error")\n', encoding="utf-8")
        result = self.run_case(move_case(), "runtime_error")
        self.assertEqual(result["status"], "failed", result)
        fixture(self.project)
        cancel = threading.Event()
        output = self.root / "cancelled"

        def stop_game():
            for _ in range(200):
                if (output / "game.log").exists():
                    break
                if cancel.wait(0.05):
                    return
            cancel.set()
        watcher = threading.Thread(target=stop_game)
        watcher.start()
        case = {"title": "cancel", "steps": [{"op": "wait", "frames": 3600},
                {"op": "assert", "node": "Player", "compare": "exists", "value": True}]}
        result = self.runner.run_case(self.project, case, output, cancel)
        watcher.join(12)
        self.assertEqual(result["status"], "cancelled", result)

    def test_timeout_and_premature_exit(self):
        case = {"title": "timeout", "steps": [{"op": "wait", "frames": 3600},
            {"op": "assert", "node": "Player", "compare": "exists", "value": True}]}
        result = self.run_case(case, timeout=0.2)
        self.assertEqual(result["status"], "blocked", result)
        (self.project / "game.gd").write_text('extends Node2D\nfunc _ready():\n    get_tree().quit()\n', encoding="utf-8")
        result = self.run_case(move_case(), "exit")
        self.assertEqual(result["status"], "blocked", result)


if __name__ == "__main__":
    unittest.main()
