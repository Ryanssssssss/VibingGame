from __future__ import annotations

import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from vibe_tools.editor_harness import AcceptanceHarness
from vibe_tools.editor_sidecar import Runtime, app
from vibe_tools.editor_workspace import Workspace
from vibe_tools.tests.test_editor_behavior import move_case


class EditorSidecarTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "中文 project"
        self.project.mkdir()
        (self.project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        Runtime.project = self.project.resolve()
        Runtime.token = "sidecar-token"
        Runtime.parent_pid = 123
        Runtime.godot_exe = ""
        Runtime.runs = {}
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer sidecar-token"}
        self.credentials = patch("vibe_tools.editor_sidecar.credential", return_value="")
        self.credentials.start()
        self.preflight = patch.object(AcceptanceHarness, "_preflight", return_value={"status": "blocked", "summary": "Godot unavailable in unit test"})
        self.preflight.start()

    def tearDown(self):
        self.preflight.stop()
        self.credentials.stop()
        self.temp.cleanup()

    def test_project_bound_health_and_authentication(self):
        self.assertEqual(self.client.get("/v1/health").status_code, 401)
        response = self.client.get("/v1/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.json()["project"]), self.project.resolve())
        self.assertEqual(response.json()["protocol_version"], 1)

    def test_attachment_is_deduplicated_outside_export_tree(self):
        image = Path(self.temp.name) / "截图.png"
        image.write_bytes(b"fake-png")
        body = {"paths": [str(image), str(image)]}
        response = self.client.post("/v1/attachments", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        values = response.json()["attachments"]
        self.assertEqual(values[0]["id"], values[1]["id"])
        self.assertTrue(values[0]["project_path"].startswith(".godot/agent/attachments/"))

    def test_asset_import_uses_res_assets(self):
        image = Path(self.temp.name) / "hero.png"
        image.write_bytes(b"fake-png")
        response = self.client.post("/v1/assets/import", headers=self.headers, json={"paths": [str(image)]})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["assets"][0]
        self.assertTrue(item["res_path"].startswith("res://assets/"))
        self.assertTrue((self.project / item["project_path"]).is_file())

    def test_legacy_chat_history_is_returned_without_migration(self):
        history = {"messages": [{"role": "user", "content": "旧消息", "steps": []}]}
        (self.project / "chat_history.json").write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        response = self.client.get("/v1/chat/history", headers=self.headers)
        self.assertEqual(response.json()["messages"][0]["content"], "旧消息")

    def test_harness_marks_results_stale_after_project_change(self):
        harness = AcceptanceHarness(self.project, "")
        plan = harness.save_plan({"cases": [move_case()]})
        plan["cases"][0]["status"] = "passed"
        plan["cases"][0]["verified_fingerprint"] = "old"
        plan["verified_fingerprint"] = "old"
        harness._persist(plan)
        loaded = harness.load_plan()
        self.assertEqual(loaded["cases"][0]["status"], "stale")

    def test_last_agent_transaction_can_be_rolled_back(self):
        run_id = uuid.uuid4().hex
        workspace = Workspace(self.project, self.project / ".godot/agent", run_id)
        (workspace.stage / "created.gd").write_text("extends Node\n", encoding="utf-8")
        workspace.commit([])
        workspace.close()
        health = self.client.get("/v1/health", headers=self.headers).json()
        self.assertEqual(health["last_revertible_run_id"], run_id)
        response = self.client.post(
            "/v1/agent/rollback", headers=self.headers,
            json={"run_id": run_id, "dirty_files": []},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["changed_files"], ["created.gd"])
        self.assertFalse((self.project / "created.gd").exists())

    def test_rollback_refuses_to_overwrite_a_later_user_change(self):
        run_id = uuid.uuid4().hex
        workspace = Workspace(self.project, self.project / ".godot/agent", run_id)
        (workspace.stage / "project.godot").write_text("agent\n", encoding="utf-8")
        workspace.commit([])
        workspace.close()
        (self.project / "project.godot").write_text("user\n", encoding="utf-8")
        response = self.client.post(
            "/v1/agent/rollback", headers=self.headers,
            json={"run_id": run_id, "dirty_files": []},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual((self.project / "project.godot").read_text(encoding="utf-8"), "user\n")

    def test_visual_case_blocks_cleanly_and_artifact_escape_is_rejected(self):
        harness = AcceptanceHarness(self.project, "")
        harness.save_plan({"cases": [{"title": "画面可见", "requires_visual": True,
                                     "steps": [{"op": "visual", "criteria": "画面可见"}]}]})
        events = []
        summary = harness.run([], lambda name, payload: events.append((name, payload)), threading.Event())
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertTrue(any(name == "harness_result" for name, _ in events))
        with self.assertRaises((ValueError, FileNotFoundError)):
            harness.artifact("../../project.godot")

    def test_harness_stream_uses_versioned_sse_events(self):
        AcceptanceHarness(self.project, "").save_plan({"cases": [move_case()]})
        with self.client.stream(
            "POST", "/v1/harness/run-stream", headers=self.headers,
            json={"case_ids": []},
        ) as response:
            body = "".join(response.iter_text())
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: protocol_version", body)
        self.assertIn('"protocol_version": 1', body)
        self.assertIn("event: harness_observation", body)
        self.assertIn("event: harness_result", body)
        self.assertIn("event: done", body)


if __name__ == "__main__":
    unittest.main()
