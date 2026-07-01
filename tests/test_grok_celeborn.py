#!/usr/bin/env python3
"""Test suite for the Grok Build adapter (`grok/scripts/grok_celeborn.py`).

Stdlib `unittest` only — same constraint as test_celeborn.py. The adapter shells out to
the real `celeborn` CLI via `run_celeborn()`; these tests monkeypatch that boundary so they
stay hermetic (no celeborn install, no Grok, no network).

    python -m unittest tests.test_grok_celeborn
    python tests/test_grok_celeborn.py
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

# Import the adapter from ../grok/scripts/grok_celeborn.py without installing it.
REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT / "grok" / "scripts"))
import grok_celeborn as gc  # noqa: E402


def _fake_cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["celeborn"], returncode=returncode, stdout=stdout, stderr="")


class GrokTestCase(unittest.TestCase):
    """Base: a temp dir, GROK_HOME redirected into it, run_celeborn stubbed to a recorder."""

    def setUp(self):
        # .resolve() so paths match the adapter, which resolves (/tmp → /private/tmp on macOS).
        self.tmp = Path(tempfile.mkdtemp(prefix="grok-test-")).resolve()
        self.grok_home = self.tmp / "grok_home"
        self.grok_home.mkdir()
        # Redirect ~/.grok at the env layer so nothing touches the real home dir.
        self._orig_env = dict(__import__("os").environ)
        __import__("os").environ["GROK_HOME"] = str(self.grok_home)
        # Record every celeborn invocation instead of executing it.
        self.calls: list[list[str]] = []
        self._orig_run = gc.run_celeborn

        def _recorder(*args, project=None, cwd=None, check=False):
            self.calls.append(list(args))
            # `status` is the only call whose stdout the adapter consumes.
            if args and args[0] == "status":
                return _fake_cp("## Hot tier\nfocus: testing\n")
            return _fake_cp("")

        gc.run_celeborn = _recorder

    def tearDown(self):
        gc.run_celeborn = self._orig_run
        import os
        os.environ.clear()
        os.environ.update(self._orig_env)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx_project(self) -> Path:
        proj = self.tmp / "proj"
        (proj / ".context").mkdir(parents=True)
        return proj


# --------------------------------------------------------------------------- convert

class TestConvert(GrokTestCase):
    def _write_grok(self, rows: list[dict]) -> Path:
        src = self.tmp / "chat_history.jsonl"
        src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return src

    def test_converts_user_assistant_tool_to_claude_shape(self):
        rows = [
            {"type": "system", "content": "ignored"},
            {"type": "reasoning", "content": "ignored too"},
            {"type": "user", "content": "fix the bug"},
            {"type": "assistant", "content": "on it",
             "tool_calls": [{"id": "t1", "name": "Shell", "arguments": {"command": "ls"}}]},
            {"type": "tool_result", "tool_call_id": "t1", "content": "file.py"},
        ]
        src = self._write_grok(rows)
        dest = self.tmp / "out.jsonl"
        n = gc.convert_grok_transcript(src, "sess1", dest)

        lines = [json.loads(l) for l in dest.read_text().splitlines() if l.strip()]
        self.assertEqual(n, len(lines))
        # system + reasoning dropped → user, assistant, tool_result remain.
        self.assertEqual([l["type"] for l in lines], ["user", "assistant", "user"])
        # every line carries the session id.
        self.assertTrue(all(l["sessionId"] == "sess1" for l in lines))

        # assistant: text block + tool_use, with the Grok→Claude tool-name mapping applied.
        asst = lines[1]["message"]["content"]
        self.assertEqual(asst[0]["type"], "text")
        tool_use = next(b for b in asst if b["type"] == "tool_use")
        self.assertEqual(tool_use["name"], "Bash")  # Shell → Bash
        self.assertEqual(tool_use["input"]["command"], "ls")

        # tool_result becomes a Claude user/tool_result with the matching id.
        tr = lines[2]["message"]["content"][0]
        self.assertEqual(tr["type"], "tool_result")
        self.assertEqual(tr["tool_use_id"], "t1")

    def test_skips_empty_and_malformed_lines(self):
        src = self.tmp / "h.jsonl"
        src.write_text("\n{ not json }\n" + json.dumps({"type": "user", "content": "hi"}) + "\n",
                       encoding="utf-8")
        dest = self.tmp / "o.jsonl"
        self.assertEqual(gc.convert_grok_transcript(src, "s", dest), 1)

    def test_assistant_with_no_text_or_tools_is_dropped(self):
        src = self._write_grok([{"type": "assistant", "content": ""}])
        dest = self.tmp / "o.jsonl"
        self.assertEqual(gc.convert_grok_transcript(src, "s", dest), 0)

    def test_string_arguments_are_parsed(self):
        src = self._write_grok([
            {"type": "assistant", "content": "x",
             "tool_calls": [{"id": "a", "name": "read_file", "arguments": '{"path": "/tmp/f"}'}]},
        ])
        dest = self.tmp / "o.jsonl"
        gc.convert_grok_transcript(src, "s", dest)
        block = [b for b in json.loads(dest.read_text())["message"]["content"] if b["type"] == "tool_use"][0]
        self.assertEqual(block["name"], "Read")             # read_file → Read
        self.assertEqual(block["input"]["file_path"], "/tmp/f")  # path → file_path


# --------------------------------------------------------------------------- pure helpers

class TestHelpers(GrokTestCase):
    def test_map_tool_name(self):
        self.assertEqual(gc.map_tool_name("Shell"), "Bash")
        self.assertEqual(gc.map_tool_name("search_replace"), "Edit")
        self.assertEqual(gc.map_tool_name("Unknown"), "Unknown")
        self.assertEqual(gc.map_tool_name(""), "Tool")

    def test_grok_user_text_handles_blocks(self):
        self.assertEqual(gc.grok_user_text("plain"), "plain")
        self.assertEqual(
            gc.grok_user_text([{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]),
            "a\nb",
        )
        self.assertEqual(gc.grok_user_text(42), "")

    def test_find_context_root_walks_up(self):
        proj = self._ctx_project()
        nested = proj / "a" / "b"
        nested.mkdir(parents=True)
        self.assertEqual(gc.find_context_root(nested), proj)
        self.assertIsNone(gc.find_context_root(self.tmp / "nope"))

    def test_read_context_tokens_reads_signals_keys(self):
        sd = self.tmp / "sess"
        sd.mkdir()
        (sd / "signals.json").write_text(json.dumps({"context_tokens_used": 1234}))
        self.assertEqual(gc.read_context_tokens(sd), 1234)
        self.assertIsNone(gc.read_context_tokens(None))
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIsNone(gc.read_context_tokens(empty))

    def test_active_session_and_bootstrap_payload(self):
        proj = self._ctx_project()
        (self.grok_home / "active_sessions.json").write_text(
            json.dumps([{"cwd": str(proj), "session_id": "abc123"}])
        )
        active = gc.active_session_for(proj)
        self.assertIsNotNone(active)
        self.assertEqual(active["session_id"], "abc123")

        payload = gc.bootstrap_payload(proj)
        self.assertEqual(payload["session_id"], "abc123")
        self.assertEqual(payload["sessionId"], "abc123")
        self.assertEqual(Path(payload["cwd"]), proj)

        # No active session → payload still has the root but no session id.
        other = self.tmp / "other"
        (other / ".context").mkdir(parents=True)
        self.assertIsNone(gc.active_session_for(other))
        self.assertNotIn("session_id", gc.bootstrap_payload(other))


# --------------------------------------------------------------------------- bootstrap / hooks

class TestBootstrap(GrokTestCase):
    def test_bootstrap_writes_orient_pending_without_active_session(self):
        proj = self._ctx_project()
        rc = gc.cmd_bootstrap(types.SimpleNamespace(path=str(proj)))
        self.assertEqual(rc, 0)
        pending = proj / ".context" / gc.ORIENT_FILE
        self.assertTrue(pending.is_file())
        body = pending.read_text()
        self.assertIn("Celeborn Orient", body)
        self.assertIn("focus: testing", body)  # the stubbed `status` output landed in the file
        # It recorded an orient is optional (no session id), but status MUST have been called.
        self.assertIn(["status"], self.calls)

    def test_bootstrap_without_context_root_errors(self):
        rc = gc.cmd_bootstrap(types.SimpleNamespace(path=str(self.tmp / "no-context")))
        self.assertEqual(rc, 1)

    def test_session_start_records_orient_with_session_id(self):
        proj = self._ctx_project()
        gc.hook_session_start({"cwd": str(proj), "session_id": "S9"})
        self.assertIn(["record", "orient", "--session", "S9"], self.calls)
        self.assertTrue((proj / ".context" / gc.ORIENT_FILE).is_file())

    def test_session_end_removes_orient_pending(self):
        proj = self._ctx_project()
        pending = proj / ".context" / gc.ORIENT_FILE
        pending.write_text("stale")
        gc.hook_session_end({"cwd": str(proj)})
        self.assertIn(["handoff"], self.calls)
        self.assertFalse(pending.exists())

    def test_user_prompt_submit_nudges_when_orient_pending(self):
        proj = self._ctx_project()
        pending = proj / ".context" / gc.ORIENT_FILE
        pending.write_text("orient body")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gc.hook_user_prompt_submit({"cwd": str(proj), "session_id": "S1"})
        out = buf.getvalue()
        self.assertIn("Celeborn orient", out)
        self.assertIn(str(proj), out)
        self.assertIn("wire tN", out)

    def test_session_start_syncs_grok_rules(self):
        proj = self._ctx_project()
        gc.hook_session_start({"cwd": str(proj), "session_id": "S9"})
        self.assertIn(["grok", "sync-rules"], self.calls)

    def test_advisor_recommendation_rides_orient_injection(self):
        """Contract validation (deliverable #6): the pending-file injection carries the advisor
        notice as well as the Hot tier. `celeborn advise --json` recs land in the orient file."""
        proj = self._ctx_project()

        def _recorder(*args, project=None, cwd=None, check=False):
            self.calls.append(list(args))
            if args and args[0] == "status":
                return _fake_cp("## Hot tier\nfocus: testing\n")
            if args and args[0] == "advise":
                return _fake_cp(json.dumps({"harness": "claude", "recommendations": [
                    {"intent": "reduce-permission-friction", "text": "Run `celeborn permissions --suggest`."}]}))
            return _fake_cp("")

        gc.run_celeborn = _recorder
        gc.hook_session_start({"cwd": str(proj), "session_id": "S10"})
        body = (proj / ".context" / gc.ORIENT_FILE).read_text()
        self.assertIn("Celeborn advisor", body)
        self.assertIn("celeborn permissions --suggest", body)

    def test_advisor_block_silent_when_no_recommendations(self):
        proj = self._ctx_project()
        # default recorder returns "" for advise → invalid JSON → degrades to ""
        self.assertEqual(gc.advisor_block(proj), "")


if __name__ == "__main__":
    unittest.main()
