#!/usr/bin/env python3
"""Tests for Jira reconcile (t21) — Celeborn-first audit, no orphan import."""
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


class JiraReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = self.root / ".context"
        self.ctx.mkdir(parents=True)
        (self.ctx / cb.RC_NAME).write_text(
            json.dumps({
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
            "## [t1] Linked card\n- state: doing\n- owner: \n- tags: \n- blocked-by: \n- phase: \n"
            "- jira: CEL-1\n- created: 2026-06-09T00:00:00\n- updated: 2026-06-09T00:00:00\n\n"
            "## [t2] Local only\n- state: todo\n- owner: \n- tags: \n- blocked-by: \n- phase: \n"
            "- created: 2026-06-09T00:00:00\n- updated: 2026-06-09T00:00:00\n\n"
            "## [t3] Stale link\n- state: done\n- owner: \n- tags: \n- blocked-by: \n- phase: \n"
            "- jira: CEL-99\n- created: 2026-06-09T00:00:00\n- updated: 2026-06-09T00:00:00\n\n"
        )

    def tearDown(self):
        if self._creds_backup is not None:
            self._creds_path.write_text(self._creds_backup)
        self._tmp.cleanup()

    def _fake_issues(self):
        return [
            {"key": "CEL-1", "fields": {
                "summary": "Linked card",
                "status": {"statusCategory": {"key": "indeterminate"}},
                "issuetype": {"name": "Task"},
                "parent": None,
            }},
            {"key": "CEL-5", "fields": {
                "summary": "Jira orphan",
                "status": {"statusCategory": {"key": "new"}},
                "issuetype": {"name": "Task"},
                "parent": None,
            }},
        ]

    def test_analyze_finds_orphans_and_stale_without_import(self):
        with mock.patch.object(cj, "_search_issues", return_value=self._fake_issues()):
            report = cj.analyze_reconcile(self.ctx)
        self.assertTrue(report.get("celeborn_truth"))
        self.assertEqual(report["linked_count"], 1)
        self.assertEqual(len(report["jira_orphans"]), 1)
        self.assertEqual(report["jira_orphans"][0]["key"], "CEL-5")
        self.assertEqual(len(report["stale_links"]), 1)
        self.assertEqual(report["stale_links"][0]["id"], "t3")
        self.assertEqual(len(report["celeborn_unlinked"]), 1)
        self.assertEqual(report["celeborn_unlinked"][0]["id"], "t2")

    def test_perform_connect_includes_reconcile_before_first_apply(self):
        (self.ctx / cb.RC_NAME).write_text("{}\n")
        with mock.patch.object(cj, "jira_request") as req:
            req.side_effect = [
                (200, {"displayName": "Test User", "emailAddress": "a@b.com"}),
                (200, {"name": "Celeborn"}),
            ]
            with mock.patch.object(cj, "analyze_reconcile", return_value={"linked_count": 0}) as ar:
                result = cj.perform_connect(self.ctx, "test.atlassian.net", "a@b.com", "CEL", "tok2")
        self.assertTrue(result["ok"])
        self.assertTrue(result["first_connect"])
        self.assertIn("reconcile", result)
        ar.assert_called_once()

    def test_reconcile_apply_marks_rc_flag(self):
        tasks = cb._load_tasks(self.ctx)
        with mock.patch.object(cj, "_push_tasks_apply", return_value=(tasks, {"pushed": ["t2"]})):
            with mock.patch.object(cj, "analyze_reconcile", return_value={"project_key": "CEL", "linked_count": 1,
                                                                           "jira_orphans": [], "state_drift": [],
                                                                           "stale_links": [], "celeborn_unlinked": []}):
                class A:
                    path = str(self.root)
                    apply = True
                    json = False
                cj._cmd_reconcile(A())
        cfg = cb.load_config(self.ctx)
        self.assertTrue(cfg.get("jira", {}).get("reconcile_applied"))


if __name__ == "__main__":
    unittest.main()