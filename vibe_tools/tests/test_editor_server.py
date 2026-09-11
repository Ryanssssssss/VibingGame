from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vibe_tools.editor_server import Runtime, TemplateRequest, _new_task, _run_template, app


class EditorServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        Runtime.project = root / "current"
        Runtime.project.mkdir()
        Runtime.state = root / "state"
        Runtime.token = "test-token"
        Runtime.tasks = {}
        Runtime.locks = {}
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer test-token"}

    def tearDown(self):
        self.temp.cleanup()

    def test_health_requires_instance_token(self):
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        self.assertEqual(self.client.get("/api/health", headers=self.headers).status_code, 200)

    def test_template_task_creates_project_and_journal(self):
        destination = Path(self.temp.name) / "new games" / "演示"
        response = self.client.post("/api/templates", headers=self.headers, json={
            "template": "blank", "name": "演示", "project_dir": str(destination), "dirty_files": [],
        })
        task_id = response.json()["task_id"]
        for _ in range(100):
            status = self.client.get(f"/api/tasks/{task_id}", headers=self.headers).json()
            if status["status"] in ("done", "error"):
                break
            time.sleep(0.02)
        self.assertEqual(status["status"], "done", status.get("error"))
        self.assertTrue((destination / "project.godot").is_file())
        journal = Runtime.state / "transactions" / task_id / "journal.json"
        self.assertTrue(journal.is_file())

    def test_web_preview_uses_random_capability_path(self):
        export = Path(self.temp.name) / "export"
        export.mkdir()
        (export / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
        Runtime.play_tokens = {"random-token": export}
        response = self.client.get("/play/random-token/index.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>ok</h1>", response.text)
        self.assertEqual(self.client.get("/play/wrong/index.html").status_code, 404)

    def test_pre_cancelled_template_does_not_write_project(self):
        destination = Path(self.temp.name) / "cancelled-template"
        _, task = _new_task(destination)
        task["cancel"].set()
        _run_template(task, TemplateRequest(
            template="blank", name="取消测试", project_dir=str(destination), dirty_files=[]))
        self.assertEqual(task["status"], "cancelled")
        self.assertFalse((destination / "project.godot").exists())


if __name__ == "__main__":
    unittest.main()
