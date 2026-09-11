from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from vibe_tools.agent import AgentSession, ChatHistoryStore


class EditorAgentStateTest(unittest.TestCase):
    def test_history_persists_per_project(self):
        with tempfile.TemporaryDirectory() as directory:
            history_file = str(Path(directory) / "history.json")
            first = ChatHistoryStore(history_file)
            first.append(directory, {"role": "user", "content": "hello"})
            second = ChatHistoryStore(history_file)
            self.assertEqual(second.get(directory)[0]["content"], "hello")

    def test_pre_cancelled_session_never_calls_model(self):
        with tempfile.TemporaryDirectory() as directory:
            event = threading.Event()
            event.set()
            session = AgentSession(object(), ChatHistoryStore(), cancel_event=event)
            with self.assertRaises(InterruptedError):
                session.agent_generate("change this project", directory)


if __name__ == "__main__":
    unittest.main()
