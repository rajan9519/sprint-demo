import datetime as dt
import json
import os
import tempfile
import unittest

from sprintreport.codex_collect import collect_codex_sessions


UTC = dt.timezone.utc


def record(when, kind, payload):
    return {"timestamp": when, "type": kind, "payload": payload}


class CodexCollectorTest(unittest.TestCase):
    def test_extracts_work_without_internal_or_tool_output(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = os.path.join(directory, "sessions", "2026", "09", "26")
            os.makedirs(sessions)
            path = os.path.join(sessions, "rollout-test.jsonl")
            rows = [
                record("2026-09-26T10:00:00Z", "session_meta",
                       {"id": "root", "cwd": "/work/APP-123", "source": "cli"}),
                record("2026-09-26T10:00:01Z", "response_item",
                       {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "private policy"}]}),
                record("2026-09-26T10:00:02Z", "response_item",
                       {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix APP-123"}]}),
                record("2026-09-26T10:00:03Z", "response_item",
                       {"type": "reasoning", "summary": "private reasoning"}),
                record("2026-09-26T10:00:04Z", "response_item",
                       {"type": "custom_tool_call", "name": "exec",
                        "input": 'await tools.exec_command({"cmd":"git commit -m fix"})'}),
                record("2026-09-26T10:00:05Z", "response_item",
                       {"type": "custom_tool_call_output", "output": "private file contents"}),
                record("2026-09-26T10:00:06Z", "response_item",
                       {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Fixed it."}]}),
            ]
            with open(path, "w", encoding="utf-8") as out:
                for row in rows:
                    out.write(json.dumps(row) + "\n")
            result = collect_codex_sessions(directory, dt.datetime(2026, 9, 26, tzinfo=UTC),
                                            dt.datetime(2026, 9, 27, tzinfo=UTC), UTC)
            self.assertEqual(len(result), 1)
            session = result[0]
            self.assertEqual((session.n_user_prompts, session.n_assistant_msgs, session.n_tool_uses), (1, 1, 1))
            self.assertEqual(session.tickets, ["APP-123"])
            self.assertEqual(session.git_commands, ["git commit -m fix"])
            digest = " ".join(turn.text for turn in session.turns)
            for excluded in ("private policy", "private reasoning", "private file contents"):
                self.assertNotIn(excluded, digest)


if __name__ == "__main__":
    unittest.main()
