"""celeborn_secrets — encrypted secrets manager for Pro (Infisical). CELE-t224 / SCRUM-157.

Design: docs/plans/cele-t224-infisical-secrets.md. Model A (CLI-first): Celeborn wraps the
**pinned** `infisical` binary — auto-provisioned like CMM's (download, sha256-verify the release
tarball against references/infisical-pin.json, extract, cache) — so secrets get Infisical's own
offline cache and keyring-backed login token, and Celeborn never stores an Infisical secret on
disk. Auto-provisioning of the *project* follows Model 3a: the coder logs in once in a browser
(`infisical login`, signup included); Celeborn then uses that session token against Infisical's
REST API to create the project and writes `.infisical.json` — the coder never sees the dashboard.
Any REST failure degrades to Infisical's own interactive `infisical init`, never a dead end.

The whole `celeborn secrets` family is Pro-gated (operator decision 2, 2026-07-07), mirroring
hosted sync: the vault itself is Infisical's free tier — Celeborn Pro gates the convenience
wrapper + discipline enforcement. The gate checks the hosted entitlements row once and caches
the answer for a day so `secrets run` keeps working offline.

Config split (mirrors celeborn_jira):
  • non-secret config → .celebornrc "secrets" object (provider/host/default_env/path)
  • .infisical.json (committable, no sensitive data) → project id + default environment
  • NOTHING secret is ever written by this module — the login token lives in the OS keyring,
    managed by the `infisical` binary itself.

Imported lazily by celeborn.py so the free, local core stays network- and dependency-free.
"""

from __future__ import annotations

import getpass
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import celeborn as cb

DEFAULT_HOST = "https://app.infisical.com"
INFISICAL_JSON = ".infisical.json"
PIN_FILENAME = "infisical-pin.json"
TIER_CACHE_TTL = 24 * 3600  # re-verify the Pro entitlement at most daily; offline runs ride the cache


# --------------------------------------------------------------------------- config (rc + project file)

def _ctx(args) -> Path:
    ctx = cb.find_context_root(Path(getattr(args, "path", ".") or "."))
    if not ctx:
        cb.die("no .context/ found — run `celeborn init` first.")
    return ctx


def secrets_config(ctx: Path) -> dict:
    """The non-secret "secrets" object from .celebornrc, with defaults filled in."""
    cfg = dict(cb.load_config(ctx).get("secrets", {}) or {})
    cfg.setdefault("provider", "infisical")
    cfg.setdefault("host", DEFAULT_HOST)
    cfg.setdefault("default_env", "dev")
    cfg.setdefault("path", "/")
    return cfg


def _set_rc_secrets(ctx: Path, key: str, value) -> None:
    """Persist a non-secret value under the "secrets" object in .celebornrc (e.g. host, default_env)."""
    rc = ctx / cb.RC_NAME
    data = {}
    if rc.is_file():
        try:
            data = json.loads(rc.read_text())
        except json.JSONDecodeError:
            data = {}
    data.setdefault("secrets", {})[key] = value
    rc.write_text(json.dumps(data, indent=2) + "\n")


def _project_file(ctx: Path) -> Path:
    return ctx.parent / INFISICAL_JSON


def _load_project(ctx: Path) -> dict:
    try:
        data = json.loads(_project_file(ctx).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- the Pro gate

def _entitled_tier() -> str:
    """Live entitlement lookup against the hosted account (the same table `celeborn account` reads).
    Returns "free" when logged in with no active entitlement; dies with a login hint when there is
    no stored session at all. Tests monkeypatch this."""
    import celeborn_sync as cs
    cfg = cs.sync_config(None)
    if not cs.load_creds():
        cb.die("the encrypted secrets manager is part of Celeborn Pro — sign in first "
               "(`celeborn login`), then `celeborn upgrade` if you don't have a plan yet.", code=2)
    tok = cs._ensure_session(cfg)
    s, rows = cs._http("GET", f"{cfg['url']}/rest/v1/entitlements?select=*",
                       headers=cs._rest_headers(cfg, tok))
    if s == 200 and isinstance(rows, list) and rows:
        r = rows[0]
        if r.get("active", True) and (r.get("status") in (None, "active", "trialing")):
            return r.get("tier") or "paid"
    return "free"


def _tier_cache_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "celeborn" / "secrets-tier.json"


def require_pro() -> str:
    """Gate the whole `secrets` family on an active Pro/Team entitlement (operator decision 2).
    The verified tier is cached for TIER_CACHE_TTL so day-to-day (and offline) `secrets run` calls
    don't need the network; a failed live check falls back to a still-fresh cache."""
    p = _tier_cache_path()
    try:
        cached = json.loads(p.read_text())
        if time.time() - float(cached.get("checked_at", 0)) < TIER_CACHE_TTL:
            if cached.get("tier") not in (None, "", "free"):
                return cached["tier"]
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    tier = _entitled_tier()
    if tier == "free":
        cb.die("the encrypted secrets manager is part of Celeborn Pro — run `celeborn upgrade` "
               "when you're ready. (The vault itself is Infisical's free tier; Pro adds the "
               "Celeborn wrapper + secrets-discipline enforcement.)", code=2)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tier": tier, "checked_at": time.time()}) + "\n")
    return tier


# --------------------------------------------------------------------------- pinned-binary provisioning
#
# Same discipline as celeborn_cmm_provision: NEVER "latest" at runtime — always the pinned version,
# checksum-verified, cached, fail-safe. The one twist: Infisical ships tarballs, so we verify the
# ARCHIVE against the pin, then extract the `infisical` member and record the extracted binary's own
# sha256 in a sidecar for cheap revalidation on later runs.

def pin_path() -> Path:
    return cb.DATA_DIR / PIN_FILENAME


def load_pin(path: Path | None = None) -> dict:
    """Parse the pin manifest. Returns {} (never raises) if absent/invalid so callers degrade."""
    p = path or pin_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x86_64"
    else:
        arch = machine
    return f"{system}-{arch}"


def cache_root(cache_dir: Path | None = None) -> Path:
    if cache_dir is not None:
        return cache_dir
    env = (os.environ.get("CELEBORN_INFISICAL_CACHE") or "").strip()
    if env:
        return Path(env)
    xdg = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "celeborn" / "infisical"


def cached_binary_path(version: str, cache_dir: Path | None = None) -> Path:
    return cache_root(cache_dir) / version / "infisical"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "celeborn-secrets-provision"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _extract_binary(blob: bytes) -> bytes | None:
    """The `infisical` member out of the release tar.gz, or None if the archive has no such member."""
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile() and Path(m.name).name == "infisical":
                    f = tf.extractfile(m)
                    return f.read() if f else None
    except (tarfile.TarError, OSError):
        return None
    return None


def resolve_cached_binary(pin: dict | None = None, cache_dir: Path | None = None) -> str | None:
    """The provisioned binary for the pinned version, if present and still matching its provision-time
    sidecar checksum; else None (a tampered cache is treated as absent — fail-safe)."""
    pin = pin or load_pin()
    version = pin.get("version")
    if not version:
        return None
    path = cached_binary_path(version, cache_dir)
    sidecar = path.with_suffix(".sha256")
    if not path.is_file() or not sidecar.is_file():
        return None
    try:
        if _sha256(path.read_bytes()) != sidecar.read_text().strip():
            return None
    except OSError:
        return None
    return str(path)


def provision(pin: dict | None = None, *, downloader=None, cache_dir: Path | None = None,
              force: bool = False) -> dict:
    """Fetch + verify + cache the pinned `infisical` binary for this platform.

    Returns {"status", "reason", "path", "version", "key"} with status in
    {cached, provisioned, skipped, error} — same contract as CMM provisioning. Never raises;
    a tarball that fails the archive checksum (or carries no `infisical` member) installs NOTHING."""
    pin = pin or load_pin()
    download = downloader or _default_download
    version = pin.get("version")
    key = platform_key()
    art = (pin.get("artifacts") or {}).get(key)

    if not version or not isinstance(art, dict):
        return {"status": "skipped", "version": version, "key": key, "path": None,
                "reason": f"no pinned artifact for platform '{key}'"}
    expected = (art.get("sha256") or "").lower()
    url = art.get("url")
    if not expected or not url:
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": "pin artifact is missing a url or sha256 — refusing to install"}

    dest = cached_binary_path(version, cache_dir)
    if not force:
        cached = resolve_cached_binary(pin, cache_dir)
        if cached:
            return {"status": "cached", "version": version, "key": key, "path": cached, "reason": None}

    try:
        blob = download(url)
    except Exception as e:  # noqa: BLE001 — any download failure degrades, never raises
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": f"download failed ({type(e).__name__}: {e})"}

    actual = _sha256(blob)
    if actual != expected:
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": f"checksum mismatch — refusing to install (expected {expected[:12]}…, "
                          f"got {actual[:12]}…)"}

    binary = _extract_binary(blob)
    if binary is None:
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": "verified archive contains no `infisical` binary — refusing to install"}

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".infisical-", suffix=".part")
        with os.fdopen(fd, "wb") as fh:
            fh.write(binary)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
        dest.with_suffix(".sha256").write_text(_sha256(binary) + "\n")
    except OSError as e:
        return {"status": "error", "version": version, "key": key, "path": None,
                "reason": f"could not write the cached binary ({e})"}
    return {"status": "provisioned", "version": version, "key": key, "path": str(dest), "reason": None}


def resolve_binary(ctx: Path) -> str | None:
    """The `infisical` binary Celeborn should run: rc override → PATH → provisioned cache."""
    override = (secrets_config(ctx).get("binary") or "").strip()
    if override and Path(override).is_file():
        return override
    on_path = shutil.which("infisical")
    if on_path:
        return on_path
    return resolve_cached_binary()


# --------------------------------------------------------------------------- infisical CLI seam

def _run(binary: str, argv: list, ctx: Path, *, capture: bool = True, input_: str | None = None,
         timeout: int = 300) -> subprocess.CompletedProcess:
    """Run the infisical CLI from the repo root (where .infisical.json lives). A non-default host
    is passed via --domain so self-hosters work without touching the CLI's own config. Tests
    monkeypatch this."""
    cfg = secrets_config(ctx)
    cmd = [binary, *argv]
    if cfg["host"] != DEFAULT_HOST and argv and argv[0] != "run":
        cmd += ["--domain", cfg["host"]]
    return subprocess.run(cmd, cwd=str(ctx.parent), capture_output=capture, text=True,
                          input=input_, timeout=timeout)


def _session_token(binary: str, ctx: Path) -> str | None:
    """The logged-in coder's session token (`infisical user get token`) for the Model 3a REST calls.
    None when not logged in or the verb is unavailable — callers degrade, never die."""
    try:
        r = _run(binary, ["user", "get", "token"], ctx, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    # Output looks like "Token: <jwt>" (possibly with extra lines) — take the last field that
    # looks token-shaped rather than pinning the exact label.
    for line in reversed((r.stdout or "").splitlines()):
        parts = line.strip().split()
        if parts and len(parts[-1]) > 20 and "." in parts[-1]:
            return parts[-1]
    return None


def _logged_in(binary: str, ctx: Path) -> bool:
    return _session_token(binary, ctx) is not None


# --------------------------------------------------------------------------- Model 3a REST provisioning

def _http(method: str, url: str, headers: dict | None = None, body=None, timeout: int = 30):
    """Tiny JSON HTTP against the Infisical REST API. Returns (status, parsed_or_None); network
    errors return (0, None) so setup can degrade to `infisical init`. Tests monkeypatch this."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="ignore")
        try:
            return e.code, (json.loads(raw) if raw.strip() else None)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}
    except (urllib.error.URLError, OSError):
        return 0, None


def _api(host: str, token: str, method: str, path: str, body=None):
    return _http(method, f"{host}{path}",
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                          "Accept": "application/json"},
                 body=body)


def provision_project(host: str, token: str, name: str) -> dict:
    """Create (or find) the Infisical project via REST with the coder's own session token — the
    Model 3a heart. Returns {"ok", "workspace_id", "reason"}; every failure is a reason, never a
    raise, so setup can fall back to interactive `infisical init`."""
    s, orgs = _api(host, token, "GET", "/api/v2/organizations")
    org_list = (orgs or {}).get("organizations") if isinstance(orgs, dict) else None
    if s != 200 or not org_list:
        return {"ok": False, "workspace_id": None,
                "reason": f"could not list organizations (HTTP {s})"}
    org_id = org_list[0].get("id") or org_list[0].get("_id")

    # Reuse an existing same-named project (idempotent re-run) before creating a duplicate.
    s, ws = _api(host, token, "GET", f"/api/v2/organizations/{org_id}/workspaces")
    for w in ((ws or {}).get("workspaces") or []) if s == 200 and isinstance(ws, dict) else []:
        if (w.get("name") or "").strip().lower() == name.strip().lower():
            return {"ok": True, "workspace_id": w.get("id") or w.get("_id"), "reason": "existing"}

    s, created = _api(host, token, "POST", "/api/v2/workspaces",
                      body={"projectName": name, "organizationId": org_id})
    wid = None
    if isinstance(created, dict):
        proj = created.get("project") or created.get("workspace") or created
        if isinstance(proj, dict):
            wid = proj.get("id") or proj.get("_id")
    if s not in (200, 201) or not wid:
        return {"ok": False, "workspace_id": None, "reason": f"project creation failed (HTTP {s})"}
    return {"ok": True, "workspace_id": wid, "reason": "created"}


# --------------------------------------------------------------------------- commands

def cmd_secrets(args):
    action = getattr(args, "secrets_cmd", None)
    handlers = {"setup": _cmd_setup, "set": _cmd_set, "get": _cmd_get, "list": _cmd_list,
                "run": _cmd_run, "status": _cmd_status, "doctor": _cmd_doctor}
    fn = handlers.get(action)
    if not fn:
        cb.die(f"unknown secrets command: {action!r}")
    return fn(args)


def _require_binary(ctx: Path) -> str:
    binary = resolve_binary(ctx)
    if not binary:
        cb.die("the `infisical` CLI isn't available yet — run `celeborn secrets setup` "
               "(it provisions the pinned binary automatically).")
    return binary


def _require_project(ctx: Path) -> dict:
    proj = _load_project(ctx)
    if not proj.get("workspaceId"):
        cb.die(f"this repo isn't linked to a vault project ({INFISICAL_JSON} missing) — "
               "run `celeborn secrets setup`.")
    return proj


def _cmd_setup(args):
    """One-command onboarding: Pro gate → pinned binary → browser login → Model 3a project
    auto-provision → .infisical.json + rc config. Idempotent — a re-run only fills gaps."""
    ctx = _ctx(args)
    require_pro()
    cfg = secrets_config(ctx)
    host = (getattr(args, "host", None) or cfg["host"]).rstrip("/")

    binary = resolve_binary(ctx)
    if not binary:
        print("  provisioning the pinned `infisical` CLI…")
        res = provision()
        if res["status"] in ("provisioned", "cached"):
            binary = res["path"]
            cb.ok(f"infisical {res['version']} ready ({res['status']}) → {binary}")
        else:
            cb.die(f"could not provision the infisical CLI — {res['reason']}\n"
                   "  Install it manually (https://infisical.com/docs/cli/overview) and re-run.")
    else:
        cb.ok(f"infisical CLI found → {binary}")

    if _logged_in(binary, ctx):
        cb.ok("already logged in to Infisical")
    else:
        print("  opening the Infisical browser login (signup happens right there if you're new)…")
        r = _run(binary, ["login"], ctx, capture=False, timeout=600)
        if r.returncode != 0:
            cb.die("`infisical login` did not complete — re-run `celeborn secrets setup` to retry.")

    proj = _load_project(ctx)
    if proj.get("workspaceId"):
        cb.ok(f"vault project already linked ({proj['workspaceId']})")
    else:
        name = getattr(args, "project", None) or ctx.parent.name or "celeborn-project"
        token = _session_token(binary, ctx)
        result = {"ok": False, "reason": "no session token available"}
        if token:
            result = provision_project(host, token, name)
        if result["ok"]:
            _project_file(ctx).write_text(json.dumps(
                {"workspaceId": result["workspace_id"], "defaultEnvironment": cfg["default_env"]},
                indent=2) + "\n")
            cb.ok(f"vault project '{name}' {result['reason']} → {INFISICAL_JSON} written "
                  "(no secrets in it — safe to commit)")
        else:
            # Degrade to Infisical's own interactive selector rather than dead-ending (§6a).
            cb.warn(f"hands-off project provisioning unavailable ({result['reason']}) — "
                    "falling back to Infisical's interactive `init`.")
            r = _run(binary, ["init"], ctx, capture=False, timeout=600)
            if r.returncode != 0 or not _load_project(ctx).get("workspaceId"):
                cb.die("project link did not complete — re-run `celeborn secrets setup` to retry.")
            cb.ok(f"vault project linked → {INFISICAL_JSON} written")

    _set_rc_secrets(ctx, "provider", "infisical")
    _set_rc_secrets(ctx, "host", host)
    _set_rc_secrets(ctx, "default_env", cfg["default_env"])
    print("\n  You're set. Put a key in the vault:   celeborn secrets set ANTHROPIC_API_KEY")
    print("  Run anything with secrets injected:  celeborn secrets run -- <command>")


def _env_of(args, ctx: Path) -> str:
    return getattr(args, "env", None) or secrets_config(ctx)["default_env"]


def _cmd_set(args):
    ctx = _ctx(args)
    require_pro()
    binary = _require_binary(ctx)
    _require_project(ctx)
    name = args.name.strip()
    if getattr(args, "stdin", False):
        value = sys.stdin.read().strip()
    else:
        value = getpass.getpass(f"value for {name} (input hidden): ").strip()
    if not value:
        cb.die("no value given — nothing stored.")
    r = _run(binary, ["secrets", "set", f"{name}={value}", "--env", _env_of(args, ctx)], ctx)
    if r.returncode != 0:
        cb.die(f"infisical refused the write: {(r.stderr or r.stdout).strip()}")
    cb.ok(f"{name} stored in the vault (env: {_env_of(args, ctx)}). It never touched this repo's disk.")


def _cmd_get(args):
    ctx = _ctx(args)
    require_pro()
    binary = _require_binary(ctx)
    _require_project(ctx)
    r = _run(binary, ["secrets", "get", args.name.strip(), "--plain", "--env", _env_of(args, ctx)], ctx)
    if r.returncode != 0:
        cb.die(f"could not read {args.name}: {(r.stderr or r.stdout).strip()}")
    print((r.stdout or "").strip())


def _cmd_list(args):
    """Secret NAMES in the current env — never values."""
    ctx = _ctx(args)
    require_pro()
    binary = _require_binary(ctx)
    _require_project(ctx)
    env = _env_of(args, ctx)
    r = _run(binary, ["export", "--format=dotenv", "--env", env], ctx)
    if r.returncode != 0:
        cb.die(f"could not list secrets: {(r.stderr or r.stdout).strip()}")
    names = [ln.partition("=")[0].strip() for ln in (r.stdout or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#") and "=" in ln]
    if not names:
        print(f"  (no secrets in env '{env}' yet — add one: celeborn secrets set <NAME>)")
        return
    print(f"  {len(names)} secret(s) in env '{env}' (names only):")
    for n in sorted(names):
        print(f"    {n}")


def _cmd_run(args):
    """Run a command with vault secrets injected as ephemeral env vars — the consumption path
    infra provisioning rides (§6). Exit code passes through."""
    ctx = _ctx(args)
    require_pro()
    binary = _require_binary(ctx)
    _require_project(ctx)
    cmd = list(getattr(args, "cmd", []) or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        cb.die("nothing to run — usage: celeborn secrets run -- <command …>")
    cfg = secrets_config(ctx)
    argv = ["run", "--env", _env_of(args, ctx)]
    if cfg["path"] != "/":
        argv += ["--path", cfg["path"]]
    r = _run(binary, [*argv, "--", *cmd], ctx, capture=False)
    sys.exit(r.returncode)


def _cmd_status(args):
    ctx = _ctx(args)
    cfg = secrets_config(ctx)
    binary = resolve_binary(ctx)
    proj = _load_project(ctx)
    logged = bool(binary) and _logged_in(binary, ctx)
    doc = {
        "provider": cfg["provider"],
        "host": cfg["host"],
        "default_env": cfg["default_env"],
        "binary": binary,
        "project_linked": bool(proj.get("workspaceId")),
        "workspace_id": proj.get("workspaceId"),
        "logged_in": logged,
    }
    if getattr(args, "json", False):
        print(json.dumps(doc, indent=2))
        return
    print("🏹 Celeborn secrets — Infisical")
    print(f"  host:     {doc['host']}")
    print(f"  binary:   {binary or 'not provisioned (run `celeborn secrets setup`)'}")
    print(f"  project:  {doc['workspace_id'] or 'not linked (run `celeborn secrets setup`)'}")
    print(f"  login:    {'yes (keyring)' if logged else 'no (run `celeborn secrets setup`)'}")
    print(f"  env:      {doc['default_env']}")


def _cmd_doctor(args):
    """The secrets-discipline check, standalone. The same scan also folds into `celeborn doctor`
    for everyone (free included) — this Pro verb exists so `secrets` users can run just it."""
    ctx = _ctx(args)
    require_pro()
    hits = cb._env_file_secret_hits(ctx.parent, cb.load_config(ctx).get("secret_patterns", []))
    if not hits:
        cb.ok("no live secret values in repo .env files")
        return
    for fname, key in hits:
        cb.warn(f"`{key}` in {fname} looks like a live secret")
    print("  Fix:  move each into the vault — `celeborn secrets set <NAME>` — then delete the line;")
    print("        consume at run time with `celeborn secrets run -- <command>`.")
    sys.exit(1)
