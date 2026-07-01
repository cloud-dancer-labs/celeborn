#!/usr/bin/env python3
"""Test suite for the Codex CLI adapter (`codex/scripts/codex_celeborn.py`).

Stdlib `unittest` only — same constraint as test_celeborn.py / test_grok_celeborn.py. The adapter
shells out to the real `celeborn` CLI via `run_celeborn()`; these tests monkeypatch that boundary
so they stay hermetic (no celeborn install, no Codex, no network).

    python -m unittest tests.test_codex_celeborn
    python tests/test_codex_celeborn.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT / "codex" / "scripts"))
import codex_celeborn as cc  # noqa: E402


def _fake_cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["celeborn"], returncode=returncode, stdout=stdout, stderr="")


class CodexTestCase(unittest.TestCase):
    """Base: a temp dir, CODEX_HOME redirected into it, run_celeborn stubbed to a recorder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-test-")).resolve()
        self.codex_home = self.tmp / "codex_home"
        self.codex_home.mkdir()
        self._orig_env = dict(os.environ)
        os.environ["CODEX_HOME"] = str(self.codex_home)
        self.calls: list[list[str]] = []
        self._orig_run = cc.run_celeborn

        def _recorder(*args, project=None, cwd=None, check=False):
            self.calls.append(list(args))
            if args and args[0] == "status":
                return _fake_cp("## Hot tier\nfocus: testing\n")
            return _fake_cp("")

        cc.run_celeborn = _recorder

    def tearDown(self):
        cc.run_celeborn = self._orig_run
        os.environ.clear()
        os.environ.update(self._orig_env)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx_project(self) -> Path:
        proj = self.tmp / "proj"
        (proj / ".context").mkdir(parents=True)
        return proj

    def _write_config(self, body: str):
        (self.codex_home / "config.toml").write_text(body, encoding="utf-8")

    def _write_rollout(self, rows: list[dict], sid: str = "uuid-1") -> Path:
        d = self.codex_home / "sessions" / "2026" / "06" / "14"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"rollout-2026-06-14T10-00-00-{sid}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path


# --------------------------------------------------------------------------- transcript convert

class TestConvert(CodexTestCase):
    def _ri(self, payload: dict) -> dict:
        return {"timestamp": "t", "type": "response_item", "payload": payload}

    def test_converts_message_call_output_to_claude_shape(self):
        rows = [
            {"type": "session_meta", "payload": {"id": "uuid-1", "cwd": "/x"}},
            self._ri({"type": "reasoning", "summary": "ignored"}),
            self._ri({"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": "fix the bug"}]}),
            self._ri({"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": "on it"}]}),
            self._ri({"type": "function_call", "name": "shell", "call_id": "c1",
                      "arguments": json.dumps({"command": ["ls", "-a"]})}),
            self._ri({"type": "function_call_output", "call_id": "c1",
                      "output": {"content": "file.py", "success": True}}),
        ]
        src = self._write_rollout(rows)
        dest = self.tmp / "out.jsonl"
        n = cc.convert_codex_transcript(src, "uuid-1", dest)
        lines = [json.loads(l) for l in dest.read_text().splitlines() if l.strip()]

        # session_meta + reasoning are dropped; 4 substantive lines remain.
        self.assertEqual(n, 4)
        self.assertEqual(lines[0]["type"], "user")
        self.assertEqual(lines[0]["message"]["content"], "fix the bug")
        self.assertEqual(lines[1]["type"], "assistant")
        self.assertEqual(lines[1]["message"]["content"][0]["text"], "on it")
        # function_call → assistant tool_use, name mapped, array command joined.
        tu = lines[2]["message"]["content"][0]
        self.assertEqual(tu["type"], "tool_use")
        self.assertEqual(tu["name"], "Bash")
        self.assertEqual(tu["input"]["command"], "ls -a")
        # function_call_output → user tool_result joined on call_id.
        tr = lines[3]["message"]["content"][0]
        self.assertEqual(tr["type"], "tool_result")
        self.assertEqual(tr["tool_use_id"], "c1")
        self.assertIn("file.py", tr["content"])

    def test_apply_patch_create_maps_to_write_update_to_edit(self):
        create = self._ri({"type": "function_call", "name": "apply_patch", "call_id": "a1",
                           "arguments": json.dumps({"patch": "*** Add File: new.py\n+print(1)\n"})})
        update = self._ri({"type": "function_call", "name": "apply_patch", "call_id": "a2",
                           "arguments": json.dumps({"patch": "*** Update File: old.py\n+x\n"})})
        src = self._write_rollout([create, update])
        dest = self.tmp / "out.jsonl"
        cc.convert_codex_transcript(src, "uuid-1", dest)
        lines = [json.loads(l) for l in dest.read_text().splitlines() if l.strip()]
        self.assertEqual(lines[0]["message"]["content"][0]["name"], "Write")
        self.assertEqual(lines[0]["message"]["content"][0]["input"]["file_path"], "new.py")
        self.assertEqual(lines[1]["message"]["content"][0]["name"], "Edit")
        self.assertEqual(lines[1]["message"]["content"][0]["input"]["file_path"], "old.py")

    def test_local_shell_call_maps_to_bash(self):
        row = self._ri({"type": "local_shell_call", "call_id": "s1",
                        "action": {"command": "echo hi"}})
        src = self._write_rollout([row])
        dest = self.tmp / "out.jsonl"
        cc.convert_codex_transcript(src, "uuid-1", dest)
        tu = json.loads(dest.read_text().splitlines()[0])["message"]["content"][0]
        self.assertEqual(tu["name"], "Bash")
        self.assertEqual(tu["input"]["command"], "echo hi")

    def test_bare_response_items_without_wrapper(self):
        # Some exports drop the response_item wrapper — handle a bare ResponseItem too.
        rows = [{"type": "message", "role": "user", "content": "hello"}]
        src = self._write_rollout(rows)
        dest = self.tmp / "out.jsonl"
        n = cc.convert_codex_transcript(src, "uuid-1", dest)
        self.assertEqual(n, 1)


# --------------------------------------------------------------------------- AGENTS.md inject

class TestAgentsMd(CodexTestCase):
    def test_writes_managed_block(self):
        proj = self._ctx_project()
        self.assertTrue(cc.ensure_agents_md(proj))
        body = (proj / "AGENTS.md").read_text()
        self.assertIn(cc.AGENTS_BEGIN, body)
        self.assertIn(cc.AGENTS_END, body)
        self.assertIn("wire tN", body)
        self.assertIn(".codex-orient-pending.md", body)

    def test_refresh_is_idempotent_and_preserves_other_content(self):
        proj = self._ctx_project()
        (proj / "AGENTS.md").write_text("# My project rules\n\nDo the thing.\n")
        self.assertTrue(cc.ensure_agents_md(proj))           # appended
        body = (proj / "AGENTS.md").read_text()
        self.assertIn("My project rules", body)
        self.assertIn(cc.AGENTS_BEGIN, body)
        self.assertFalse(cc.ensure_agents_md(proj))          # second call: no change


# --------------------------------------------------------------------------- permission lever

class TestPermissionLever(CodexTestCase):
    def test_status_parses_policy_and_trust(self):
        proj = self._ctx_project()
        rp = str(proj.resolve())
        self._write_config(
            'approval_policy = "on-request"\n'
            'sandbox_mode = "workspace-write"\n\n'
            f'[projects."{rp}"]\n'
            'trust_level = "trusted"\n'
        )
        st = cc.codex_permission_status(proj)
        self.assertEqual(st["approval_policy"], "on-request")
        self.assertEqual(st["sandbox_mode"], "workspace-write")
        self.assertTrue(st["project_trusted"])

    def test_friction_when_interactive_and_untrusted(self):
        proj = self._ctx_project()
        self._write_config('approval_policy = "on-request"\n')
        self.assertIsNotNone(cc.codex_friction_signal(proj))

    def test_no_friction_when_trusted(self):
        proj = self._ctx_project()
        rp = str(proj.resolve())
        self._write_config(f'[projects."{rp}"]\ntrust_level = "trusted"\n')
        self.assertIsNone(cc.codex_friction_signal(proj))

    def test_no_friction_when_never(self):
        proj = self._ctx_project()
        self._write_config('approval_policy = "never"\n')
        self.assertIsNone(cc.codex_friction_signal(proj))

    def test_no_config_defaults_to_interactive_friction(self):
        proj = self._ctx_project()
        self.assertIsNotNone(cc.codex_friction_signal(proj))

    def test_permissions_apply_appends_trust_block(self):
        proj = self._ctx_project()
        self._write_config('approval_policy = "on-request"\n')
        args = type("A", (), {"path": str(proj), "apply": True, "suggest": False})()
        rc = cc.cmd_permissions(args)
        self.assertEqual(rc, 0)
        body = (self.codex_home / "config.toml").read_text()
        self.assertIn(f'[projects."{proj.resolve()}"]', body)
        self.assertIn('trust_level = "trusted"', body)
        # now trusted → no friction
        self.assertIsNone(cc.codex_friction_signal(proj))

    def test_permissions_apply_refuses_duplicate_header(self):
        proj = self._ctx_project()
        rp = str(proj.resolve())
        self._write_config(f'approval_policy = "on-request"\n\n[projects."{rp}"]\ntrust_level = "untrusted"\n')
        args = type("A", (), {"path": str(proj), "apply": True, "suggest": False})()
        rc = cc.cmd_permissions(args)
        self.assertEqual(rc, 1)                              # won't duplicate the table

    def test_advise_json_reports_codex_friction(self):
        proj = self._ctx_project()
        self._write_config('approval_policy = "on-request"\n')
        args = type("A", (), {"path": str(proj), "json": True})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_advise(args)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["harness"], "codex")
        self.assertEqual(data["recommendations"][0]["intent"], "reduce-permission-friction")


# --------------------------------------------------------------------------- hooks

class TestHooks(CodexTestCase):
    def test_session_start_writes_pending_and_agents_md(self):
        proj = self._ctx_project()
        self._write_config('approval_policy = "on-request"\n')   # produce an advisor recommendation
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.hook_session_start({"cwd": str(proj), "session_id": "uuid-1"})
        pending = proj / ".context" / cc.ORIENT_FILE
        self.assertTrue(pending.is_file())
        body = pending.read_text()
        self.assertIn("focus: testing", body)
        self.assertIn("Celeborn advisor", body)                  # advice rode the orient channel
        self.assertTrue((proj / "AGENTS.md").is_file())
        self.assertIn(["record", "orient", "--session", "uuid-1"], self.calls)

    def test_user_prompt_submit_nudges_when_orient_pending(self):
        proj = self._ctx_project()
        (proj / ".context" / cc.ORIENT_FILE).write_text("orient body")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.hook_user_prompt_submit({"cwd": str(proj), "session_id": "uuid-1"})
        out = buf.getvalue()
        self.assertIn("Celeborn orient", out)
        self.assertIn(str(proj), out)

    def test_stop_converts_and_captures(self):
        proj = self._ctx_project()
        rows = [{"type": "response_item", "payload": {"type": "message", "role": "user",
                                                      "content": "hi"}}]
        self._write_rollout(rows, sid="uuid-9")
        cc.hook_stop({"cwd": str(proj), "session_id": "uuid-9"})
        self.assertTrue(any(c and c[0] == "capture" for c in self.calls))

    def test_session_end_handoff_and_clears_pending(self):
        proj = self._ctx_project()
        pending = proj / ".context" / cc.ORIENT_FILE
        pending.write_text("x")
        cc.hook_session_end({"cwd": str(proj), "session_id": "uuid-1"})
        self.assertIn(["handoff"], self.calls)
        self.assertFalse(pending.exists())

    def test_find_rollout_file_by_conversation_id(self):
        self._write_rollout([{"type": "x"}], sid="abc-123")
        found = cc.find_rollout_file("abc-123")
        self.assertIsNotNone(found)
        self.assertIn("abc-123", found.name)


if __name__ == "__main__":
    unittest.main()
