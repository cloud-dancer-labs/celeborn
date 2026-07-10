"""Tests for `celeborn config` — the read/write seam behind the board Settings sections (CELE-t355).

Isolated: every test runs against a throwaway project .context/ and a throwaway XDG_CONFIG_HOME, so
neither the operator's real .celebornrc nor their fleet.json is ever touched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import celeborn as cb  # noqa: E402
import celeborn_config as cc  # noqa: E402


def _args(ctx: Path, **kw):
    ns = types.SimpleNamespace(path=str(ctx.parent), config_cmd=None, json=False, fleet=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ctx = root / "repo" / ".context"
        self.ctx.mkdir(parents=True)
        # Isolate the machine-global fleet.json under a temp XDG dir.
        self.xdg = root / "xdg"
        self.xdg.mkdir()
        self._prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.xdg)

    def tearDown(self):
        if self._prev_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev_xdg
        self.tmp.cleanup()

    # ---- resolution -----------------------------------------------------------------------
    def test_defaults_when_unset(self):
        state = cc.state_json(self.ctx)
        keys = {k["key"]: k for g in state["groups"] for k in g["keys"]}
        self.assertEqual(keys["context_bands"]["value"], [50, 75, 100, 125])
        self.assertEqual(keys["fleet_working_minutes"]["value"], 10)
        self.assertEqual(keys["jira_autopush"]["value"], True)
        # every schema key is surfaced in exactly one group
        self.assertEqual(len(keys), len(cc.SCHEMA))

    # ---- coercion + validation ------------------------------------------------------------
    def test_coerce_types(self):
        self.assertIs(cc._coerce(cc.SCHEMA["jira_autopush"], "false"), False)
        self.assertIs(cc._coerce(cc.SCHEMA["jira_autopush"], "ON"), True)
        self.assertEqual(cc._coerce(cc.SCHEMA["fleet_dead_minutes"], "45"), 45)
        self.assertEqual(cc._coerce(cc.SCHEMA["context_bands"], "40,70,95,120"), [40, 70, 95, 120])

    def test_bad_values_raise(self):
        with self.assertRaises(ValueError):
            cc._coerce(cc.SCHEMA["jira_autopush"], "maybe")
        with self.assertRaises(ValueError):
            cc._coerce(cc.SCHEMA["context_bands"], "100,50,25,10")  # not ascending
        with self.assertRaises(ValueError):
            cc._coerce(cc.SCHEMA["context_bands"], "1,2,3")  # wrong length
        with self.assertRaises(ValueError):
            cc._coerce(cc.SCHEMA["fleet_dead_minutes"], "0")  # below min
        with self.assertRaises(ValueError):
            cc._coerce(cc.SCHEMA["board_night_watch"], "whenever")  # not a choice

    # ---- writes land in the right store ---------------------------------------------------
    def test_rc_write(self):
        rep = cc.set_value(self.ctx, "board_savings_bar", "false")
        self.assertEqual(rep["store"], "rc")
        rc = json.loads((self.ctx / cb.RC_NAME).read_text())
        self.assertIs(rc["board_savings_bar"], False)

    def test_fleet_write_preserves_registry(self):
        # seed a project registry, then write a fleet setting; the registry must survive.
        cb._save_fleet_registry({"projects": [{"path": "/x", "slug": "x"}]})
        rep = cc.set_value(self.ctx, "fleet_working_minutes", "12")
        self.assertEqual(rep["store"], "fleet")
        data = json.loads(cb._fleet_registry_path().read_text())
        self.assertEqual(data["settings"]["fleet_working_minutes"], 12)
        self.assertEqual(len(data["projects"]), 1)  # registry not clobbered

    def test_context_bands_sync_side_effect(self):
        cc.set_value(self.ctx, "context_bands", "40,70,95,120")
        rc = json.loads((self.ctx / cb.RC_NAME).read_text())
        # the live context-pressure consumer (t207) tracks the edited bands
        self.assertEqual(rc["context_soft_tokens"], 95000)
        self.assertEqual(rc["context_hard_tokens"], 120000)

    def test_unknown_key_dies(self):
        with self.assertRaises(SystemExit):
            cc.set_value(self.ctx, "not_a_key", "1")


if __name__ == "__main__":
    unittest.main()
