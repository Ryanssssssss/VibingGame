from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vibe_tools.editor_workspace import Conflict, Workspace, rollback, safe_path


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.project = base / "中文 project"
        self.state = base / "state"
        self.project.mkdir()
        (self.project / "project.godot").write_text("config_version=5\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_commit_and_rollback(self):
        workspace = Workspace(self.project, self.state, "task")
        (workspace.stage / "main.gd").write_text("extends Node\n", encoding="utf-8")
        changed = workspace.commit([])
        self.assertEqual(changed, ["main.gd"])
        workspace.close()
        restored = rollback(self.state / "transactions/task/journal.json", self.project, [])
        self.assertEqual(restored, ["main.gd"])
        self.assertFalse((self.project / "main.gd").exists())

    def test_external_change_blocks_commit(self):
        workspace = Workspace(self.project, self.state, "task")
        (workspace.stage / "project.godot").write_text("staged", encoding="utf-8")
        (self.project / "project.godot").write_text("user", encoding="utf-8")
        with self.assertRaises(Conflict):
            workspace.commit([])

    def test_dirty_file_blocks_commit(self):
        workspace = Workspace(self.project, self.state, "task")
        (workspace.stage / "project.godot").write_text("staged", encoding="utf-8")
        with self.assertRaises(Conflict):
            workspace.commit(["res://project.godot"])

    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            safe_path(self.project, "../outside.txt")

    def test_interrupted_journal_is_not_rollbackable(self):
        journal = self.state / "transactions/task/journal.json"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps({"root": str(self.project), "status": "interrupted"}), encoding="utf-8")
        with self.assertRaises(Conflict):
            rollback(journal, self.project, [])


if __name__ == "__main__":
    unittest.main()
