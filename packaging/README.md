# Packaging & native installers

How the free Celeborn CLI ships to macOS and Windows users who don't have a Python toolchain. The
binary is a self-contained PyInstaller bundle of the two flat modules (`celeborn.py` +
`celeborn_sync.py`) plus the `references/` data tree (templates + `schema.sql`).

## Local build

```bash
# macOS / Linux
bash packaging/build-binary.sh        # → dist/celeborn

# Windows (PowerShell)
pwsh packaging/build-binary.ps1       # → dist\celeborn.exe
```

The spec (`celeborn.spec`) bundles `references/` under `celeborn_refs/` and bakes a `VERSION` file, so
the frozen binary resolves templates/schema and reports its version with no source tree or package
metadata present (see `celeborn.py:_data_dir` / `_local_version`, both `sys.frozen`-aware).

## Release (automated)

Tagging triggers `.github/workflows/release.yml`:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

It builds three assets and attaches them (plus `.sha256` sidecars) to the GitHub Release:

| Asset | Runner |
|---|---|
| `celeborn-macos-arm64` | macos-14 (Apple silicon) |
| `celeborn-macos-x86_64` | macos-13 (Intel) |
| `celeborn-windows-x86_64.exe` | windows-latest |

## Package managers

Each manifest points at the Release assets above. On every version bump, update the version + paste the
asset `sha256` (from the `.sha256` sidecars):

- **Homebrew** — `packaging/homebrew/celeborn.rb` → tap repo `cloud-dancer-labs/homebrew-celeborn`
  (`Formula/celeborn.rb`). Install: `brew install cloud-dancer-labs/celeborn/celeborn`.
- **Scoop** — `packaging/scoop/celeborn.json` → bucket repo `cloud-dancer-labs/scoop-celeborn`. Install:
  `scoop bucket add celeborn https://github.com/cloud-dancer-labs/scoop-celeborn; scoop install celeborn`.
- **winget** — `packaging/winget/*.yaml` → PR to `microsoft/winget-pkgs` under
  `manifests/t/ThotTechnologies/Celeborn/<version>/`. Install: `winget install ThotTechnologies.Celeborn`.

## Owner-side actions (one-time, can't be done from this repo)

- [ ] Create tap repo **`cloud-dancer-labs/homebrew-celeborn`** and Scoop bucket **`cloud-dancer-labs/scoop-celeborn`**.
- [ ] First winget submission: PR the three manifests to `microsoft/winget-pkgs` (needs the asset live).
- [ ] **Code signing / notarization** (otherwise users see "unidentified developer"):
  - macOS: Apple Developer ID cert → set `codesign_identity`/`entitlements_file` in the spec + add
    `xcrun notarytool` to the workflow (secrets: cert .p12 + notarization creds).
  - Windows: Authenticode cert → `signtool` step in the workflow (secret: cert + password).
- [ ] Optionally automate the manifest bump (a `release`-event job that opens PRs to the tap/bucket).
