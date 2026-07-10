#!/usr/bin/env python3
"""Tests for the GitHub Issues board mirror (CELE-t214) — Celeborn-first, idempotent, offline.

Network is always mocked (`gh_request` / `_list_mirror_issues`). Mirrors the jira test suite shape.
"""
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
import celeborn_github as cg  # noqa: E402


class GithubMirrorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = self.root / ".context"
        self.ctx.mkdir(parents=True)
        (self.ctx / cb.RC_NAME).write_text(
            json.dumps({"github": {"repo": "o/r"}}) + "\n"
        )
        cb._tasks_path(self.ctx).write_text(
            "## [t1] Linked card\n- state: doing\n- owner: \n- tags: \n- blocked-by: \n- phase: \n"
            "- github: 7\n- created: 2026-06-09T00:00:00\n- updated: 2026-06-09T00:00:00\n\n"
            "## [t2] Local only\n- state: todo\n- owner: \n- tags: \n- blocked-by: \n- phase: \n"
            "- created: 2026-06-09T00:00:00\n- updated: 2026-06-09T00:00:00\n\n"
            "## [t3] Stale link\n- state: done\n- owner: \n- tags: \n- blocked-by: \n- phase: \n"
            "- github: 99\n- created: 2026-06-09T00:00:00\n- updated: 2026-06-09T00:00:00\n\n"
        )

    def tearDown(self):
        self._tmp.cleanup()

    # --- reconcile audit (Celeborn wins; no orphan import) ---

    def _fake_issues(self):
        return [
            {"number": 7, "title": "Linked card", "state": "open",
             "labels": [{"name": "celeborn:doing"}], "body": cg._marker("t1")},
            {"number": 42, "title": "GitHub orphan", "state": "open",
             "labels": [{"name": "celeborn:todo"}], "body": cg._marker("t999")},
        ]

    def test_analyze_finds_orphans_and_stale_without_import(self):
        with mock.patch.object(cg, "_resolve_token", return_value="tok"), \
             mock.patch.object(cg, "_list_mirror_issues", return_value=self._fake_issues()):
            report = cg.analyze_reconcile(self.ctx)
        self.assertTrue(report.get("celeborn_truth"))
        self.assertEqual(report["linked_count"], 1)
        self.assertEqual(len(report["github_orphans"]), 1)
        self.assertEqual(report["github_orphans"][0]["number"], "42")
        self.assertEqual(len(report["stale_links"]), 1)
        self.assertEqual(report["stale_links"][0]["id"], "t3")
        self.assertEqual(len(report["celeborn_unlinked"]), 1)
        self.assertEqual(report["celeborn_unlinked"][0]["id"], "t2")

    # --- push: create + update + done→closed, links stored back ---

    def test_push_creates_updates_and_closes_done(self):
        tasks = cb._load_tasks(self.ctx)
        # creates = t2 (todo), updates = t1 (doing, #7) + t3 (done, #99)
        calls = []

        def fake_gh(token, method, path, body=None):
            calls.append((method, path, body))
            if method == "POST" and path.endswith("/issues"):
                return (201, {"number": 100})
            return (200, {})

        with mock.patch.object(cg, "_resolve_token", return_value="tok"), \
             mock.patch.object(cg, "_ensure_labels"), \
             mock.patch.object(cg, "gh_request", side_effect=fake_gh):
            tasks2, result = cg._push_tasks_apply(self.ctx, tasks, tasks, quiet=True)

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(sorted(result["pushed"]), ["t1", "t2", "t3"])
        self.assertFalse(result["errors"])
        t2 = next(t for t in tasks2 if t["id"] == "t2")
        self.assertEqual(t2["github"], "100")  # new link stored back
        # t3 is done → its update PATCH must set state=closed
        t3_patch = next(b for m, p, b in calls if m == "PATCH" and p.endswith("/issues/99"))
        self.assertEqual(t3_patch["state"], "closed")
        # t1 is doing → open, with the doing label
        t1_patch = next(b for m, p, b in calls if m == "PATCH" and p.endswith("/issues/7"))
        self.assertEqual(t1_patch["state"], "open")
        self.assertEqual(t1_patch["labels"], ["celeborn:doing"])

    def test_reconcile_is_idempotent(self):
        """Second push creates zero — the stored github number turns creates into updates."""
        tasks = cb._load_tasks(self.ctx)

        def fake_gh(token, method, path, body=None):
            if method == "POST" and path.endswith("/issues"):
                return (201, {"number": 100})
            return (200, {})

        with mock.patch.object(cg, "_resolve_token", return_value="tok"), \
             mock.patch.object(cg, "_ensure_labels"), \
             mock.patch.object(cg, "gh_request", side_effect=fake_gh):
            tasks, r1 = cg._push_tasks_apply(self.ctx, tasks, tasks, quiet=True)
            self.assertEqual(r1["created"], 1)
            tasks, r2 = cg._push_tasks_apply(self.ctx, tasks, tasks, quiet=True)
        self.assertEqual(r2["created"], 0)  # nothing new to create on re-run
        self.assertEqual(r2["updated"], 3)

    def test_push_relinks_stripped_field_instead_of_duplicating(self):
        """If the `github` field was stripped (older celeborn wrote tasks.md), push re-links by the
        body marker and UPDATEs the existing issue rather than creating a duplicate."""
        # t2 (todo) has no github field, but issue #55 already mirrors it (marker t2).
        existing = [{"number": 55, "title": "Local only", "state": "open",
                     "labels": [{"name": "celeborn:todo"}], "body": cg._marker("t2")}]
        tasks = cb._load_tasks(self.ctx)
        t2 = next(t for t in tasks if t["id"] == "t2")
        posted = []

        def fake_gh(token, method, path, body=None):
            if method == "POST" and path.endswith("/issues"):
                posted.append(path)
                return (201, {"number": 999})
            return (200, {})

        with mock.patch.object(cg, "_resolve_token", return_value="tok"), \
             mock.patch.object(cg, "_ensure_labels"), \
             mock.patch.object(cg, "_list_mirror_issues", return_value=existing), \
             mock.patch.object(cg, "gh_request", side_effect=fake_gh):
            tasks2, result = cg._push_tasks_apply(self.ctx, tasks, [t2], quiet=True)

        self.assertEqual(result["relinked"], 1)
        self.assertEqual(result["created"], 0)     # no duplicate created
        self.assertEqual(result["updated"], 1)
        self.assertEqual(posted, [])               # POST /issues never called
        self.assertEqual(next(t for t in tasks2 if t["id"] == "t2")["github"], "55")

    # --- pull: links by marker, applies GitHub state, never imports orphans ---

    def test_pull_links_by_marker_and_updates_state(self):
        # An unlinked local card (t2) whose issue #55 carries its marker and is now closed → done.
        issues = [
            {"number": 55, "title": "Local only", "state": "closed",
             "labels": [], "body": cg._marker("t2")},
            {"number": 77, "title": "Truly orphan", "state": "open",
             "labels": [{"name": "celeborn:todo"}], "body": cg._marker("t404")},
        ]

        class A:
            path = str(self.root)
            dry_run = False

        with mock.patch.object(cg, "_resolve_token", return_value="tok"), \
             mock.patch.object(cg, "_list_mirror_issues", return_value=issues):
            cg._cmd_pull(A())

        t2 = next(t for t in cb._load_tasks(self.ctx) if t["id"] == "t2")
        self.assertEqual(t2["github"], "55")   # linked via marker
        self.assertEqual(t2["state"], "done")  # closed issue → done
        # #77 (marker t404) matches no card → not imported
        self.assertNotIn("t404", {t["id"] for t in cb._load_tasks(self.ctx)})
        # CELE-t216: a real pull delta wakes the PM.
        self.assertEqual([e["source"] for e in cb._pm_wake_peek(self.ctx)], ["github"])

    def test_pull_no_delta_does_not_wake_pm(self):
        # #7 already linked to t1 with matching title+state → no change → no wake (CELE-t216).
        issues = [{"number": 7, "title": "Linked card", "state": "open",
                   "labels": [{"name": "celeborn:doing"}], "body": cg._marker("t1")}]

        class A:
            path = str(self.root)
            dry_run = False

        with mock.patch.object(cg, "_resolve_token", return_value="tok"), \
             mock.patch.object(cg, "_list_mirror_issues", return_value=issues):
            cg._cmd_pull(A())
        self.assertEqual(cb._pm_wake_peek(self.ctx), [])

    # --- connect: validates repo + write scope, ensures labels, reconciles on first connect ---

    def test_perform_connect_validates_and_reconciles(self):
        (self.ctx / cb.RC_NAME).write_text("{}\n")

        def fake_gh(token, method, path, body=None):
            if path == "/user":
                return (200, {"login": "octocat"})
            if path.startswith("/repos/"):
                return (200, {"has_issues": True, "permissions": {"push": True}})
            return (200, {})

        with mock.patch.object(cg, "github_connected", return_value=False), \
             mock.patch.object(cg, "_ensure_labels") as lbl, \
             mock.patch.object(cg, "save_github_creds"), \
             mock.patch.object(cg, "analyze_reconcile", return_value={"linked_count": 0}) as ar, \
             mock.patch.object(cg, "gh_request", side_effect=fake_gh):
            result = cg.perform_connect(self.ctx, "o/r", "tok", token_explicit=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["first_connect"])
        self.assertEqual(result["login"], "octocat")
        self.assertIn("reconcile", result)
        lbl.assert_called_once()
        ar.assert_called_once()

    def test_connect_rejects_repo_without_write_access(self):
        def fake_gh(token, method, path, body=None):
            if path == "/user":
                return (200, {"login": "octocat"})
            if path.startswith("/repos/"):
                return (200, {"has_issues": True, "permissions": {"pull": True}})  # read-only
            return (200, {})

        with mock.patch.object(cg, "github_connected", return_value=False), \
             mock.patch.object(cg, "gh_request", side_effect=fake_gh):
            result = cg.perform_connect(self.ctx, "o/r", "tok", token_explicit=True)
        self.assertFalse(result["ok"])
        self.assertIn("write access", result["error"])


if __name__ == "__main__":
    unittest.main()
