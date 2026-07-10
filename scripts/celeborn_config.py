"""celeborn config — read/write the board-settings knobs behind the t144 Settings sections.

The Settings page (board/app/settings/SettingsView.tsx) surfaces a curated set of project + fleet
configuration values. This module is the single read/write seam behind them (CELE-t355):

  celeborn config --json            → the resolved settings state (grouped, with per-key metadata)
  celeborn config set <key> <val>   → write one key to .celebornrc (project) or, with --fleet,
                                      to ~/.config/celeborn/fleet.json's `settings` block (machine-global)

Two stores, because the threat/scope model differs by knob:
  • rc    — the project's `.celebornrc` (context bands, board display toggles, integration flags). Merged
            via celeborn._update_config, exactly like every other project setting.
  • fleet — the machine-global fleet registry's `settings` object, for knobs that are inherently
            cross-project (liveness TTLs, blackboard retention). Shared by every repo on the machine.

"Wired to real config" means the value round-trips to the real file and — where a live consumer already
exists — is honored end-to-end. The SCHEMA below marks each key's consumer as "live" (a running code path
reads it today) or "persist" (stored authoritatively; a downstream renderer honors it in a follow-up card).
Nothing here is a localStorage stub: every write lands in the same file the CLI and hooks read.
"""

from __future__ import annotations

import json
from pathlib import Path

import celeborn as cb


# --------------------------------------------------------------------------- schema
#
# Each entry: (group, type, default, store, consumer, label, extra)
#   type     — "bool" | "int" | "str" | "int_list"
#   store    — "rc" (project .celebornrc) | "fleet" (machine-global fleet.json settings)
#   consumer — "live" (a running path reads this key today) | "persist" (stored; consumer is a follow-up)
#   extra    — dict with optional {min, max, choices, note}; for int_list, {len, ascending}
#
# The four "context_bands" thresholds are the band.ts clear-nudge boundaries (fresh/mid/clear-soon/
# clear-now, in k tokens). Writing them ALSO syncs context_soft_tokens / context_hard_tokens (the live
# context-pressure consumer, CELE-t207) so the pressure warnings track the edited bands — that's why the
# key is marked "live" even though band.ts itself keeps its own visual defaults.

SCHEMA: dict[str, dict] = {
    # ---- Context & clearing ---------------------------------------------------------------
    "context_bands": {
        "group": "context", "type": "int_list", "default": [50, 75, 100, 125], "store": "rc",
        "consumer": "live", "label": "Clear-nudge bands (k tokens)",
        "extra": {"len": 4, "ascending": True,
                  "note": "fresh < b0 · mid < b1 · clear soon < b2 · clear now < b3 · ≥ b3 clear urgent. "
                          "Writing syncs context_soft_tokens=b2·1000, context_hard_tokens=b3·1000."},
    },
    "checkpoint_first_rebirth": {
        "group": "context", "type": "bool", "default": True, "store": "rc", "consumer": "persist",
        "label": "Checkpoint-first rebirth",
    },
    "land_clears_on_stop": {
        "group": "context", "type": "bool", "default": True, "store": "rc", "consumer": "persist",
        "label": "Land clears on Stop conditions",
    },

    # ---- Fleet & blackboard (machine-global) ----------------------------------------------
    "fleet_working_minutes": {
        "group": "fleet", "type": "int", "default": 10, "store": "fleet", "consumer": "persist",
        "label": "Working window", "extra": {"min": 1, "max": 240, "unit": "min"},
    },
    "fleet_dead_minutes": {
        "group": "fleet", "type": "int", "default": 30, "store": "fleet", "consumer": "persist",
        "label": "Presumed dead after", "extra": {"min": 1, "max": 1440, "unit": "min"},
    },
    "fleet_roster_ageout_hours": {
        "group": "fleet", "type": "int", "default": 24, "store": "fleet", "consumer": "persist",
        "label": "Roster age-out", "extra": {"min": 1, "max": 720, "unit": "h"},
    },
    "blackboard_keep_notes": {
        "group": "fleet", "type": "int", "default": 200, "store": "fleet", "consumer": "persist",
        "label": "Blackboard retention", "extra": {"min": 10, "max": 5000, "unit": "notes"},
    },
    "blackboard_ttl_days": {
        "group": "fleet", "type": "int", "default": 7, "store": "fleet", "consumer": "persist",
        "label": "Note TTL", "extra": {"min": 1, "max": 365, "unit": "days"},
    },
    "intent_ttl_hours": {
        "group": "fleet", "type": "int", "default": 2, "store": "rc", "consumer": "live",
        "label": "Commit intent TTL", "extra": {"min": 1, "max": 72, "unit": "h",
                 "note": "Read by intent expiry (CELE-t303). Per-project in .celebornrc."},
    },
    "intent_overlap_warnings": {
        "group": "fleet", "type": "bool", "default": True, "store": "rc", "consumer": "persist",
        "label": "Overlap warnings",
    },

    # ---- Board (this viewer) --------------------------------------------------------------
    "board_night_watch": {
        "group": "board", "type": "str", "default": "22:00-06:30", "store": "rc", "consumer": "persist",
        "label": "Night Watch schedule",
        "extra": {"choices": ["22:00-06:30", "system-dark", "manual"]},
    },
    "board_savings_bar": {
        "group": "board", "type": "bool", "default": True, "store": "rc", "consumer": "persist",
        "label": "Savings bar",
    },
    "board_code_ticker": {
        "group": "board", "type": "bool", "default": True, "store": "rc", "consumer": "persist",
        "label": "Code ticker on Stage cards",
    },
    "auto_archive": {
        "group": "board", "type": "bool", "default": True, "store": "rc", "consumer": "live",
        "label": "Done column archive",
        "extra": {"note": "Read by the done-archive sweep (celeborn.py). Older Done cards fold into the drawer."},
    },

    # ---- Hosted sync ----------------------------------------------------------------------
    "hosted_autopush": {
        "group": "sync", "type": "bool", "default": True, "store": "rc", "consumer": "persist",
        "label": "Auto-push",
    },
    "hosted_delete_propagation": {
        "group": "sync", "type": "bool", "default": False, "store": "rc", "consumer": "persist",
        "label": "Delete propagation",
        "extra": {"note": "Off today: no tombstones — a card removed on one side is re-adopted from the other."},
    },

    # ---- Integrations ---------------------------------------------------------------------
    "jira_autopush": {
        "group": "integrations", "type": "bool", "default": True, "store": "rc", "consumer": "live",
        "label": "Jira mirror",
        "extra": {"note": "Read by tasks add/move/edit/claim to push linked cards downstream to Jira."},
    },
    "github_app_enabled": {
        "group": "integrations", "type": "bool", "default": True, "store": "rc", "consumer": "persist",
        "label": "GitHub App",
    },
}

GROUPS = [
    ("context", "Context & clearing"),
    ("fleet", "Fleet & blackboard"),
    ("board", "Board"),
    ("sync", "Hosted sync"),
    ("integrations", "Integrations"),
]


# --------------------------------------------------------------------------- fleet settings store

def _load_fleet_settings() -> dict:
    """The machine-global `settings` block inside ~/.config/celeborn/fleet.json (a sibling of the
    project registry). Absent → {}. Reuses celeborn's registry loader so we never corrupt the file."""
    data = cb._load_fleet_registry()
    s = data.get("settings")
    return dict(s) if isinstance(s, dict) else {}


def _save_fleet_setting(key: str, value) -> None:
    """Merge one key into fleet.json's `settings` block, preserving the project registry alongside it."""
    data = cb._load_fleet_registry()
    settings = data.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    settings[key] = value
    data["settings"] = settings
    cb._save_fleet_registry(data)


# --------------------------------------------------------------------------- resolution

def _resolved(ctx: Path, key: str, spec: dict):
    """The live value for `key`: the store's persisted value if present, else the schema default."""
    if spec["store"] == "fleet":
        store = _load_fleet_settings()
    else:
        store = cb.load_config(ctx)
    return store.get(key, spec["default"])


def state_json(ctx: Path) -> dict:
    """The full resolved settings state for the board — grouped, each key carrying its value + metadata.
    Also embeds the read-only hosted-sync registration (project id/name/repo) for the Hosted sync card."""
    groups = []
    for gkey, glabel in GROUPS:
        keys = []
        for key, spec in SCHEMA.items():
            if spec["group"] != gkey:
                continue
            keys.append({
                "key": key,
                "value": _resolved(ctx, key, spec),
                "default": spec["default"],
                "type": spec["type"],
                "store": spec["store"],
                "consumer": spec["consumer"],
                "label": spec["label"],
                "extra": spec.get("extra", {}),
            })
        groups.append({"key": gkey, "label": glabel, "keys": keys})

    out = {"groups": groups}

    # Read-only hosted-sync registration (best-effort; never fails the whole read).
    try:
        sync = __import__("celeborn_sync")
        cfg = sync.sync_config(ctx)
        out["hosted"] = {
            "project_id": cfg.get("project_id"),
            "project_name": cfg.get("project_name"),
            "github_repo": cfg.get("github_repo"),
            "registered": bool(cfg.get("project_id")),
        }
    except Exception as e:  # pragma: no cover - defensive; the toggles must still render offline
        out["hosted"] = {"registered": False, "error": str(e)}

    return out


# --------------------------------------------------------------------------- coercion + validation

def _coerce(spec: dict, raw: str):
    """Turn a CLI string into the schema type, raising ValueError with a clear message on bad input."""
    t = spec["type"]
    extra = spec.get("extra", {})
    if t == "bool":
        v = raw.strip().lower()
        if v in ("true", "1", "on", "yes"):
            return True
        if v in ("false", "0", "off", "no"):
            return False
        raise ValueError(f"expected a boolean (true/false), got {raw!r}")
    if t == "int":
        try:
            n = int(raw)
        except ValueError:
            raise ValueError(f"expected an integer, got {raw!r}")
        lo, hi = extra.get("min"), extra.get("max")
        if lo is not None and n < lo:
            raise ValueError(f"below minimum {lo}")
        if hi is not None and n > hi:
            raise ValueError(f"above maximum {hi}")
        return n
    if t == "int_list":
        parts = [p for p in raw.replace(",", " ").split() if p]
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            raise ValueError(f"expected comma-separated integers, got {raw!r}")
        want = extra.get("len")
        if want is not None and len(nums) != want:
            raise ValueError(f"expected exactly {want} values, got {len(nums)}")
        if extra.get("ascending") and any(nums[i] >= nums[i + 1] for i in range(len(nums) - 1)):
            raise ValueError("values must be strictly ascending")
        return nums
    # str
    choices = extra.get("choices")
    if choices and raw not in choices:
        raise ValueError(f"expected one of {choices}, got {raw!r}")
    return raw


def _apply_side_effects(ctx: Path, key: str, value) -> list[str]:
    """Keep dependent live consumers honest. Editing the context bands re-derives the soft/hard
    context-pressure thresholds (CELE-t207) so the ⚠/⛔ warnings track the edited bands."""
    notes: list[str] = []
    if key == "context_bands" and isinstance(value, list) and len(value) == 4:
        cb._update_config(ctx, context_soft_tokens=value[2] * 1000, context_hard_tokens=value[3] * 1000)
        notes.append(f"context_soft_tokens={value[2] * 1000}, context_hard_tokens={value[3] * 1000}")
    return notes


def set_value(ctx: Path, key: str, raw: str) -> dict:
    """Validate + coerce + persist one key to its store. Returns the write report (for --json)."""
    spec = SCHEMA.get(key)
    if not spec:
        cb.die(f"unknown config key: {key}\n  known: {', '.join(sorted(SCHEMA))}")
    try:
        value = _coerce(spec, raw)
    except ValueError as e:
        cb.die(f"invalid value for {key}: {e}")
    if spec["store"] == "fleet":
        _save_fleet_setting(key, value)
        where = str(cb._fleet_registry_path())
    else:
        cb._update_config(ctx, **{key: value})
        where = str(ctx / cb.RC_NAME)
    synced = _apply_side_effects(ctx, key, value)
    return {"ok": True, "key": key, "value": value, "store": spec["store"], "path": where, "synced": synced}


# --------------------------------------------------------------------------- command

def cmd_config(args):
    """`celeborn config` — read (`--json`) or write (`set <key> <value> [--fleet]`) the Settings knobs."""
    ctx = cb.require_context(args)
    sub = getattr(args, "config_cmd", None)

    if sub == "set":
        key = getattr(args, "key", None)
        raw = getattr(args, "value", None)
        if key is None or raw is None:
            cb.die("usage: celeborn config set <key> <value> [--fleet]")
        # --fleet is a caller hint; the schema's store is authoritative, but reject a mismatch so a
        # fleet knob can't be silently written to the project rc (or vice-versa).
        spec = SCHEMA.get(key)
        if spec and getattr(args, "fleet", False) and spec["store"] != "fleet":
            cb.die(f"{key} is a project (.celebornrc) key — drop --fleet")
        rep = set_value(ctx, key, raw)
        if getattr(args, "json", False):
            print(json.dumps(rep, indent=2))
        else:
            cb.ok(f"set {rep['key']} = {rep['value']}  ->  {rep['path']}"
                  + (f"  (synced {', '.join(rep['synced'])})" if rep["synced"] else ""))
        return

    # default: read
    state = state_json(ctx)
    if getattr(args, "json", False):
        print(json.dumps(state, indent=2))
        return
    for g in state["groups"]:
        print(f"\n{g['label']}")
        for k in g["keys"]:
            flag = "" if k["consumer"] == "live" else "  (persist)"
            print(f"  {k['key']:28} = {k['value']}{flag}")
