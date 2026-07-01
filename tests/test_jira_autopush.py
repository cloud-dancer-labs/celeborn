#!/usr/bin/env python3
"""Tests for Jira auto-push (t31) — celeborn_jira.schedule_auto_push / flush_auto_push."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import celeborn as cb  # noqa: E402
import celeborn_jira as cj  # noqa: E402


class JiraAutopushTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = self.root / ".context"
        self.ctx.mkdir(parents=True)
        (self.ctx / cb.RC_NAME).write_text(
            json.dumps({
                "jira_autopush": True,
                "jira_autopush_debounce_seconds": 0,
                "jira": {"site": "https://test.atlassian.net", "project_key": "CEL"},
            })
            + "\n"
        )
        creds_dir = Path.home() / ".config" / "celeborn"
        self._creds_backup = None
        self._creds_path = creds_dir / "credentials.json"
        if self._creds_path.is_file():
            self._creds_backup = self._creds_path.read_text()
        creds_dir.mkdir(parents=True, exist_ok=True)
        self._creds_path.write_text(
            json.dumps({"jira": {"email": "a@b.com", "token": "tok"}}) + "\n"
        )
        cb._tasks_path(self.ctx).write_text(
            "## [t1] Ship it\n- state: doing\n- owner: \n- tags: \n- blocked-by: \n- phase: \n"
            "- jira: SCRUM-1\n- created: 2026-06-09T00:00:00\n- updated: 2026-06-09T00:00:00\n\n"
        )
        cb._write_tasks_json(self.ctx, cb._load_tasks(self.ctx))

    def tearDown(self):
        if self._creds_backup is not None:
            self._creds_path.write_text(self._creds_backup)
        self._tmp.cleanup()

    def test_schedule_pushes_linked_task(self):
        tasks = cb._load_tasks(self.ctx)
        with mock.patch.object(cj, "_push_tasks_apply") as push:
            push.return_value = (tasks, {"pushed": ["t1"], "errors": [], "created": 0, "updated": 1})
            cj.schedule_auto_push(self.ctx, tasks, ["t1"])
            push.assert_called_once()
            args, kwargs = push.call_args
            self.assertEqual(args[2][0]["id"], "t1")

    def test_skips_when_disconnected(self):
        (self.ctx / cb.RC_NAME).write_text(json.dumps({"jira_autopush": True}) + "\n")
        tasks = cb._load_tasks(self.ctx)
        with mock.patch.object(cj, "_push_tasks_apply") as push:
            cj.schedule_auto_push(self.ctx, tasks, ["t1"])
            push.assert_not_called()

    def test_debounce_blocks_repeat(self):
        tasks = cb._load_tasks(self.ctx)
        (self.ctx / cb.RC_NAME).write_text(
            json.dumps({
                "jira_autopush": True,
                "jira_autopush_debounce_seconds": 9999,
                "jira": {"site": "https://test.atlassian.net", "project_key": "CEL"},
            })
            + "\n"
        )
        state = {
            "pending": {},
            "last": {"t1": {"fingerprint": cj._task_fingerprint(tasks[0]), "at": __import__("time").time()}},
        }
        cj._save_autopush_state(self.ctx, state)
        with mock.patch.object(cj, "_push_tasks_apply") as push:
            result = cj.flush_auto_push(self.ctx, tasks, quiet=True)
            push.assert_not_called()
            self.assertEqual(result.get("flushed"), 0)

    def test_tasks_move_triggers_autopush_hook(self):
        with mock.patch("celeborn_jira.schedule_auto_push") as sched:
            cb.main(["--path", str(self.root), "tasks", "move", "t1", "done"])
            sched.assert_called_once()
            self.assertEqual(sched.call_args[0][2], ["t1"])


if __name__ == "__main__":
    unittest.main()