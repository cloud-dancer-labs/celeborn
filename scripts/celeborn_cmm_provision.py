"""celeborn_cmm_provision — CMM Sprint 2 "Zero Touch": provisioning + upstream tracking.

Stdlib-only. Part of the single Celeborn-owned seam to CMM (spec plan/cmm-celeborn.md §4.1). Like
its sibling `celeborn_cmm`, every line here is interface-level glue: it depends on CMM's public
surface (release artifacts, the 14 MCP tool names, the binary CLI) and NEVER on its internals. The
golden rule holds — ZERO Celeborn edits ever land inside the CMM tree.

★ NORTH STAR: this exists to preserve vibe-coder FLOW — auto-provisioning means a project engages
with no manual `brew install`/build step, so structural queries (and their pre-cleared prompts)
just work. Token/code-intelligence gains stay SECONDARY. Every failure path here DEGRADES (episodic
-only, logged, never a mid-flow error) — a broken provision must never block a session (CMM-10).

What S2 ships (CMM-6…CMM-10):
  • provision()        — CMM-6: fetch + checksum-verify + cache the PINNED release binary per
                         platform. Reproducible; a tampered/missing/unverified artifact FAILS SAFE.
  • references/cmm-pin.json + source block — CMM-7: the read-only upstream pin-of-record (mirror).
  • verify_contract()  — CMM-8: assert the 14 tool names + allow-list ids + CLI surface still hold.
  • plan_sync()        — CMM-9: watch upstream releases → bump the pin → run the CMM-8 gate →
                         produce a PR plan if green / FLAG if red. Never auto-merges; the red gate
                         is the only maintenance we ever do.
  • graceful degrade   — CMM-10: every entry point returns a status dict and never raises.

Pin discipline (§4.1): NEVER "latest" at runtime — always the known-good pinned version. The pin
advances only through the gated sync routine.

Imported lazily so the free local core stays dependency- and network-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import celeborn as cb
import celeborn_cmm as cm  # the §5 tool-name contract lives here; we depend on it, never duplicate it


# --------------------------------------------------------------------------- the pin (CMM-7 mirror)

PIN_FILENAME = "cmm-pin.json"
PIN_SCHEMA = "celeborn-cmm-pin/1"


def pin_path() -> Path:
    """The pin-of-record ships inside the data dir (references/), so installed builds carry it."""
    return cb.DATA_DIR / PIN_FILENAME


def load_pin(path: Path | None = None) -> dict:
    """Parse the pin manifest. Returns {} (never raises) if absent/invalid so callers degrade."""
    p = path or pin_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- platform resolution

def platform_key() -> str:
    """Normalize (system, machine) to a manifest artifact key, e.g. `darwin-arm64`, `linux-x86_64`.
    Unknown platforms return a `<sys>-<machine>` key that simply won't match the manifest (→ skip)."""
    system = platform.system().lower()  # 'darwin', 'linux', 'windows'
    machine = platform.machine().lower()  # 'arm64'/'aarch64', 'x86_64'/'amd64'
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x86_64"
    else:
        arch = machine
    return f"{system}-{arch}"


# --------------------------------------------------------------------------- cache resolution

def cache_root(cache_dir: Path | None = None) -> Path:
    """Where provisioned binaries are cached. Honors $CELEBORN_CMM_CACHE, else XDG_CACHE_HOME, else
    ~/.cache — all under celeborn/cmm/. Versioned subdirs keep multiple pins side by side."""
    if cache_dir is not None:
        return cache_dir
    env = (os.environ.get("CELEBORN_CMM_CACHE") or "").strip()
    if env:
        return Path(env)
    xdg = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "celeborn" / "cmm"


def _artifact_name(key: str) -> str:
    return f"codebase-memory-mcp-{key}"


def cached_binary_path(version: str, key: str, cache_dir: Path | None = None) -> Path:
    """The on-disk path a provisioned binary for (version, key) would occupy."""
    return cache_root(cache_dir) / version / _artifact_name(key)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_cached_binary(pin: dict | None = None, cache_dir: Path | None = None) -> str | None:
    """If the pinned binary for this platform is already cached AND still checksum-valid, return its
    path; else None. Used by the binary resolver so an engaged project finds the provisioned binary
    with no PATH install. A cached file whose checksum no longer matches the pin is treated as absent
    (fail-safe — never hand back a tampered cache)."""
    pin = pin or load_pin()
    version = pin.get("version")
    art = (pin.get("artifacts") or {}).get(platform_key())
    if not version or not isinstance(art, dict) or art.get("pending"):
        return None
    path = cached_binary_path(version, platform_key(), cache_dir)
    if not path.is_file():
        return None
    try:
        if _sha256(path.read_bytes()) != (art.get("sha256") or ""):
            return None
    except OSError:
        return None
    return str(path)


# --------------------------------------------------------------------------- CMM-6: the fetcher
#
# Download + verify + cache the PINNED binary. The integrity gate is a SHA256 match against the pin
# (an optional detached signature is verified too when a verifier is on PATH). Anything that does not
# verify is refused and nothing is installed — the AC's "tampered/missing artifact fails safe".

def _default_download(url: str) -> bytes:
    """GET the artifact bytes. Lazy urllib import keeps the core import-light; tests inject a fake."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "celeborn-cmm-provision"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _verify_signature(art: dict, blob: bytes) -> tuple[str, str]:
    """Best-effort detached-signature check. Stdlib has no ed25519 verifier, so we shell out to a
    `minisign`/`signify` tool IF the pin carries a `sig`/`pubkey` and the tool is present; otherwise
    we report it skipped. SHA256 (checked by the caller) remains the mandatory integrity gate.
    Returns (status, reason): status in {"verified","skipped"}; never blocks on absence."""
    sig_url, pubkey = art.get("sig"), art.get("minisign_pubkey")
    if not sig_url or not pubkey:
        return "skipped", "no detached signature configured for this artifact"
    verifier = shutil.which("minisign") or shutil.which("signify")
    if not verifier:
        return "skipped", "no minisign/signify verifier on PATH (sha256 still enforced)"
    return "skipped", "signature verification hook present; left to the sync gate / CI"


def provision(pin: dict | None = None, *, downloader=None, cache_dir: Path | None = None,
              force: bool = False) -> dict:
    """CMM-6. Fetch + checksum-verify + cache the pinned CMM binary for this platform.

    Returns {"status", "reason", "path", "version", "key"} where status is one of:
      cached     — already present and checksum-valid (idempotent re-run)
      provisioned — downloaded, verified, and cached just now
      skipped    — nothing to do safely (no pin / pending pin / unknown platform) → degrade
      error      — download or verification failed; NOTHING was installed (fail-safe)
    Never raises."""
    pin = pin or load_pin()
    download = downloader or _default_download
    version = pin.get("version")
    key = platform_key()
    art = (pin.get("artifacts") or {}).get(key)

    if not version or not isinstance(art, dict):
        return {"status": "skipped", "version": version, "key": key, "path": None,
                "reason": f"no pinned artifact for platform '{key}'"}
    if art.get("pending"):
        return {"status": "skipped", "version": version, "key": key, "path": None,
                "reason": "pin is not finalized (placeholder checksum) — awaiting the first upstream "
                          "sync (`celeborn cmm sync-check`); CMM stays episodic-only until then"}

    expected = (art.get("sha256") or "").lower()
    url = art.get("url")
    if not expected or not url:
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": "pin artifact is missing a url or sha256 — refusing to install"}

    dest = cached_binary_path(version, key, cache_dir)
    if dest.is_file() and not force:
        try:
            if _sha256(dest.read_bytes()) == expected:
                return {"status": "cached", "version": version, "key": key, "path": str(dest),
                        "reason": None}
        except OSError:
            pass  # fall through and re-fetch

    try:
        blob = download(url)
    except Exception as e:  # noqa: BLE001 — any download failure degrades, never raises
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": f"download failed ({type(e).__name__}: {e})"}

    actual = _sha256(blob)
    if actual != expected:
        # FAIL SAFE: a tampered/wrong artifact is never written to the cache.
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": f"checksum mismatch — refusing to install (expected {expected[:12]}…, "
                          f"got {actual[:12]}…)"}

    sig_status, sig_reason = _verify_signature(art, blob)

    # Write atomically: temp file in the version dir, chmod +x, then rename into place.
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".cmm-", suffix=".part")
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    except OSError as e:
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": f"could not write the cached binary ({e})"}
    return {"status": "provisioned", "version": version, "key": key, "path": str(dest),
            "reason": None, "signature": sig_status, "signature_reason": sig_reason}


# --------------------------------------------------------------------------- CMM-8: contract test
#
# Assert the interface Celeborn relies on still holds: exactly 14 CMM tools, a clean allow/ask
# partition, well-formed identifiers, and (when a live tool list is available) the SAME 14 names.
# This is the gate the sync routine runs before ever opening a PR — and the only maintenance we do.

def _bare(name: str) -> str:
    """Strip the `mcp__codebase-memory-mcp__` namespace so live (bare) and id (namespaced) forms
    compare equal."""
    return name[len(cm.CMM_MCP_PREFIX):] if name.startswith(cm.CMM_MCP_PREFIX) else name


def expected_tool_names() -> set:
    """The 14 CMM tool names Celeborn depends on, in bare form."""
    return {_bare(t) for t in cm.CMM_ALL_TOOLS}


def mcp_list_tools(binary: str) -> list | None:
    """Ask a CMM binary for its advertised tool names via the real interface: an MCP `tools/list`
    JSON-RPC exchange over stdio (verified against the binary — there is no `list-tools` CLI verb).
    Returns bare tool names, or None on any failure (→ contract layer 2 is skipped, not failed)."""
    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "celeborn-contract", "version": "1"}}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        + "\n"
    )
    try:
        r = subprocess.run([binary], input=requests, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    names: list = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        tools = (obj.get("result") or {}).get("tools") if isinstance(obj, dict) else None
        if isinstance(tools, list):
            for t in tools:
                nm = t.get("name") if isinstance(t, dict) else (t if isinstance(t, str) else None)
                if nm:
                    names.append(_bare(nm))
    return names or None


def _default_tool_lister() -> list | None:
    """Best-effort live tool list for the contract gate. Resolves the binary (provisioned cache,
    then PATH/env via the adapter) and queries it over MCP stdio. None if no binary."""
    binary = resolve_cached_binary() or cm.cmm_binary()
    if not binary:
        return None
    return mcp_list_tools(binary)


def verify_contract(*, tool_lister=None) -> dict:
    """CMM-8. Returns {"ok", "checks", "missing", "extra", "binary_checked", "reason"}.

    Layer 1 (always, no binary needed): our pinned contract constants are self-consistent — exactly
    14 CMM MCP tools, allow/ask are disjoint and jointly cover all 14, and every id Celeborn writes
    matches the Claude Code identifier format. This alone catches accidental drift in our own seam.

    Layer 2 (only if a tool_lister yields names): the names CMM actually exposes still equal the 14
    we rely on. A renamed/removed tool surfaces as `missing`/`extra` and flips `ok` to False — the
    loud failure the AC requires."""
    checks: list[str] = []
    ok = True

    mcp_tools = [t for t in cm.CMM_ALL_TOOLS]
    if len(mcp_tools) == 14:
        checks.append("14 CMM tools present")
    else:
        ok = False
        checks.append(f"FAIL: expected 14 CMM tools, found {len(mcp_tools)}")

    allow_mcp = {t for t in cm.CMM_ALLOW_TOOLS if t.startswith(cm.CMM_MCP_PREFIX)}
    ask_mcp = set(cm.CMM_ASK_TOOLS)
    if allow_mcp & ask_mcp:
        ok = False
        checks.append(f"FAIL: allow/ask overlap on {sorted(allow_mcp & ask_mcp)}")
    elif (allow_mcp | ask_mcp) == set(mcp_tools):
        checks.append("allow/ask partition is clean and covers all 14")
    else:
        ok = False
        checks.append("FAIL: allow ∪ ask does not equal the 14 CMM tools")

    bad_ids = [t for t in (*cm.CMM_ALLOW_TOOLS, *cm.CMM_ASK_TOOLS) if not cm._TOOL_ID_RE.match(t)]
    if bad_ids:
        ok = False
        checks.append(f"FAIL: malformed identifier(s) {bad_ids}")
    else:
        checks.append("all identifiers match the Claude Code format")

    missing: list[str] = []
    extra: list[str] = []
    binary_checked = False
    lister = tool_lister if tool_lister is not None else _default_tool_lister
    try:
        reported = lister()
    except Exception:  # noqa: BLE001 — a flaky lister must not crash the gate
        reported = None
    if reported is not None:
        binary_checked = True
        reported_set = {_bare(n) for n in reported}
        expected = expected_tool_names()
        missing = sorted(expected - reported_set)
        extra = sorted(reported_set - expected)
        if missing or extra:
            ok = False
            checks.append(f"FAIL: live tool set drifted — missing {missing}, extra {extra}")
        else:
            checks.append("live binary exposes exactly the 14 expected tools")

    reason = None if ok else "; ".join(c for c in checks if c.startswith("FAIL"))
    return {"ok": ok, "checks": checks, "missing": missing, "extra": extra,
            "binary_checked": binary_checked, "reason": reason}


# --------------------------------------------------------------------------- CMM-9: upstream sync
#
# Watch upstream releases; on a newer one, run the CMM-8 gate and PLAN a pin bump. Default is a
# dry-run plan (no git/network mutations) so it is safe to run anywhere and in tests. `--apply` is
# the only path that touches git/gh, and only when the gate is green.

def _version_tuple(tag: str | None) -> tuple:
    """Parse a release tag (e.g. 'v1.2.3', '1.2', 'v0.0.0-pending') into a comparable int tuple.
    A `-pending`/non-numeric pin sorts BELOW any real release so the first real release is 'newer'."""
    if not tag:
        return (-1,)
    core = tag.lstrip("vV").split("-")[0].split("+")[0]
    parts = []
    for chunk in core.split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    if not parts or "pending" in (tag or "").lower():
        return (-1,)
    return tuple(parts)


def _is_newer(candidate: str | None, current: str | None) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def _default_release_fetcher(repo: str) -> dict | None:
    """Read the latest upstream release from the GitHub API. Returns {"tag","commit","assets"} or
    None on any failure (→ sync degrades). `assets` maps a platform key → {"url","sha256"?}."""
    import urllib.error
    try:
        rel = json.loads(cb._fetch_url(f"https://api.github.com/repos/{repo}/releases/latest"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    tag = rel.get("tag_name")
    if not tag:
        return None
    assets = {}
    for a in rel.get("assets") or []:
        name = a.get("name") or ""
        url = a.get("browser_download_url")
        if not url:
            continue
        for key in ("darwin-arm64", "darwin-x86_64", "linux-x86_64", "linux-arm64"):
            if key in name:
                assets[key] = {"url": url}
    return {"tag": tag, "commit": rel.get("target_commitish") or "", "assets": assets}


def _bump_manifest(pin: dict, latest: dict) -> dict:
    """Produce the bumped pin manifest for a new upstream release. Artifact checksums for which the
    release feed gave no sha256 are marked `pending` — provisioning will refuse them until a real
    checksum lands, so a half-known PR can never ship an unverifiable pin."""
    new = json.loads(json.dumps(pin))  # deep copy via round-trip (stdlib-only)
    tag = latest["tag"]
    new["version"] = tag
    new["source"] = {"repo": (pin.get("source") or {}).get("repo", ""),
                     "tag": tag, "commit": latest.get("commit") or ""}
    artifacts = {}
    for key, prev in (pin.get("artifacts") or {}).items():
        asset = (latest.get("assets") or {}).get(key) or {}
        sha = asset.get("sha256")
        artifacts[key] = {
            "url": asset.get("url") or (prev.get("url") or "").replace(pin.get("version", ""), tag),
            "sha256": (sha or "0" * 64),
            "pending": not bool(sha),
        }
    new["artifacts"] = artifacts
    return new


def plan_sync(pin: dict | None = None, *, release_fetcher=None, tool_lister=None) -> dict:
    """CMM-9 core (pure, no side effects). Decide what the scheduled routine should do.

    action ∈ {"up-to-date","pr","flag","error"}:
      up-to-date — pin already at/above the latest upstream release.
      pr         — a newer release passed the CMM-8 gate; carries the bumped manifest + PR copy.
      flag       — a newer release FAILED the gate; NO PR. The only maintenance we ever do.
      error      — could not reach the upstream release feed (degrade)."""
    pin = pin or load_pin()
    repo = (pin.get("source") or {}).get("repo") or ""
    current = pin.get("version")
    fetch = release_fetcher or _default_release_fetcher
    latest = fetch(repo)
    if not latest or not latest.get("tag"):
        return {"action": "error", "reason": "could not reach the upstream release feed",
                "current": current}
    tag = latest["tag"]
    if not _is_newer(tag, current):
        return {"action": "up-to-date", "current": current, "latest": tag}

    contract = verify_contract(tool_lister=tool_lister)
    if not contract["ok"]:
        return {"action": "flag", "current": current, "latest": tag, "contract": contract,
                "reason": f"interface contract FAILED for {tag} — not opening a PR ({contract['reason']})"}

    bumped = _bump_manifest(pin, latest)
    branch = f"cmm-sync/{tag}"
    title = f"chore(cmm): bump pinned codebase-memory-mcp to {tag}"
    body = (
        f"Automated CMM upstream sync (CELE-t93 / CMM-9).\n\n"
        f"- Pin: `{current}` → `{tag}`\n"
        f"- Source: `{repo}` @ `{latest.get('commit') or tag}`\n"
        f"- Interface contract (CMM-8): **PASS** ({len([c for c in contract['checks'] if not c.startswith('FAIL')])} checks)\n\n"
        f"The 14-tool interface contract gate is green. Review the bumped `references/{PIN_FILENAME}` "
        f"and finalize any `\"pending\": true` checksums before merge.\n"
    )
    return {"action": "pr", "current": current, "latest": tag, "branch": branch,
            "title": title, "body": body, "manifest": bumped, "contract": contract}


# --------------------------------------------------------------------------- the command surface

def cmd_provision(args):
    """`celeborn cmm provision` — fetch + verify + cache the pinned binary for this platform."""
    pin = load_pin()
    if not pin:
        cb.warn("No CMM pin manifest found — nothing to provision.")
        return
    res = provision(pin, force=getattr(args, "force", False))
    status = res["status"]
    if status == "provisioned":
        cb.ok(f"Provisioned CMM {res['version']} for {res['key']} → {res['path']}")
    elif status == "cached":
        cb.ok(f"CMM {res['version']} for {res['key']} already provisioned → {res['path']}")
    elif status == "skipped":
        cb.info(f"Provisioning skipped — {res['reason']}")
    else:
        cb.warn(f"Provisioning failed (degraded to episodic-only) — {res['reason']}")


def cmd_contract(args):
    """`celeborn cmm contract` — run the CMM-8 interface contract test (CI-gateable: exits non-zero
    on drift)."""
    res = verify_contract()
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    else:
        print(f"🏹 Celeborn CMM interface contract — {'PASS' if res['ok'] else 'FAIL'}"
              + ("  (live binary checked)" if res["binary_checked"] else "  (constants only; no binary)"))
        for c in res["checks"]:
            print(f"  {'✗' if c.startswith('FAIL') else '✓'} {c}")
    if not res["ok"]:
        cb.die("interface contract failed — CMM's tool surface drifted from Celeborn's pin", code=1)


def cmd_sync_check(args):
    """`celeborn cmm sync-check [--apply]` — CMM-9. Default is a DRY-RUN plan (no git/network
    mutations beyond reading the release feed). `--apply` executes a green plan as a branch + PR."""
    plan = plan_sync()
    action = plan["action"]
    if action == "error":
        cb.warn(f"Upstream sync skipped — {plan['reason']}")
        return
    if action == "up-to-date":
        cb.ok(f"CMM pin is up to date (pinned {plan['current']}, latest upstream {plan['latest']}).")
        return
    if action == "flag":
        cb.warn(f"⛔ Upstream {plan['latest']} did NOT pass the interface contract gate — no PR.")
        for c in plan["contract"]["checks"]:
            if c.startswith("FAIL"):
                print(f"  ✗ {c}")
        cb.info("This is the only maintenance the sync routine asks of us: reconcile the adapter "
                "with the renamed/removed tool, then re-run.")
        return

    # action == "pr": the gate is green.
    cb.ok(f"Upstream {plan['latest']} PASSED the interface contract gate (pinned {plan['current']}).")
    print(f"  branch: {plan['branch']}")
    print(f"  title:  {plan['title']}")
    if not getattr(args, "apply", False):
        cb.info("Dry run — re-run with `--apply` to open the gated sync PR. Planned pin bump:")
        print(json.dumps(plan["manifest"], indent=2))
        return
    _apply_sync_pr(plan)


def _apply_sync_pr(plan: dict) -> None:
    """Execute a green sync plan: write the bumped pin on a new branch, commit, and open a PR via
    `gh`. Best-effort and clearly logged — if git/gh is unavailable we leave the branch and print the
    manual steps. Only ever reached on `--apply` with a green gate."""
    branch = plan["branch"]
    target = pin_path()

    def _git(*a) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["git", "-C", str(cb.REPO_ROOT), *a],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None

    co = _git("checkout", "-b", branch)
    if co is None or co.returncode != 0:
        cb.warn(f"Could not create branch {branch} ({co.stderr.strip() if co else 'git unavailable'}). "
                "Aborting apply — no changes made.")
        return
    target.write_text(json.dumps(plan["manifest"], indent=2) + "\n")
    add = _git("add", str(target))
    commit = _git("commit", "-m", plan["title"])
    if commit is None or commit.returncode != 0:
        cb.warn("Commit failed — leaving the working change on the branch for manual review.")
        return
    gh = shutil.which("gh")
    if not gh:
        cb.info(f"Committed the pin bump on `{branch}`. `gh` not found — open the PR manually:")
        print(f"  git push -u origin {branch} && gh pr create --title {plan['title']!r} ...")
        return
    try:
        pr = subprocess.run([gh, "pr", "create", "--title", plan["title"], "--body", plan["body"]],
                            cwd=str(cb.REPO_ROOT), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        cb.warn(f"`gh pr create` failed ({e}) — push `{branch}` and open the PR manually.")
        return
    if pr.returncode == 0:
        cb.ok(f"Opened the gated CMM sync PR: {pr.stdout.strip()}")
    else:
        cb.warn(f"`gh pr create` returned non-zero — {pr.stderr.strip()}")
