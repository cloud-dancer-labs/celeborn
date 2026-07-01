# Celeborn — PyPI publishing runbook & the distribution trilemma

Operator-facing. Covers (1) the `celeborn` name on PyPI, (2) the one decision you must make before
publishing, and (3) the exact steps for whichever path you pick. Pairs with the native-binary path in
[`../packaging/`](../packaging) + [`../.github/workflows/release.yml`](../.github/workflows/release.yml).

---

## 1. Name availability (checked 2026-06-17)

| Name | PyPI | TestPyPI |
|------|------|----------|
| **`celeborn`** | **FREE** (404) | FREE |
| `celeborn-memory` | FREE | — |
| `celeborn-cli`, `celeborn-context`, `pyceleborn`, `apache-celeborn` | FREE | — |

`celeborn` is unclaimed on PyPI — there is **no** name collision with Apache Celeborn there (Apache ships
no `celeborn` distribution). The `[project] name = "celeborn"` in `pyproject.toml` would map 1:1.

> **Reserve it regardless of the decision below.** Names are first-come. A defensive reservation (even a
> stub that just prints the install instructions) stops a squatter from taking `pip install celeborn`. See
> §4 for the stub.

---

## 2. The decision you must make first — the trilemma

A stdlib-only **Python** CLI cannot be all three of these at once. Pick two:

```
        proprietary source
              /\
             /  \
            /    \
  no signing ---- public download
```

- **PyPI / `pip` / `uvx` / `pip install -e git+…`** → **no signing, public download, but source is exposed.**
  A wheel and an sdist are just zipped `.py` files. Anyone can `pip download celeborn && unzip` and read
  `celeborn.py` verbatim. A proprietary *license* restricts **use**, not **visibility** — publishing to PyPI
  is, in practice, publishing the source.
- **Native binaries** (PyInstaller via [`packaging/celeborn.spec`](../packaging/celeborn.spec), shipped through
  Homebrew/Scoop/winget/Releases) → **proprietary source stays hidden, public download — but needs signing.**
  `release.yml:12` says signing/notarization is *intentionally not wired yet* (needs the org's Apple Developer
  cert + a Windows Authenticode cert). Unsigned binaries trip Gatekeeper/SmartScreen.
- **Private repo + token installs** → proprietary + no-signing, but **not** a public download.

**This is exactly the tension behind "I wanted to avoid signing."** Avoiding signing for the CLI means
PyPI, and PyPI means the CLI source is readable. The GitHub App half of the product sidesteps this entirely
(it's a hosted webhook receiver — nothing to sign, nothing to download); but the *CLI* half can't escape the
triangle. Decide consciously:

| If you value most… | Ship the CLI via | Signing? | Source hidden? |
|---|---|---|---|
| Frictionless install, fastest launch | **PyPI** (`uvx celeborn` / `pip install celeborn`) | none | no |
| Keeping `celeborn.py` proprietary | **Native binaries** (brew/scoop/winget) | **required** | yes |
| Neither yet — just hold the name | **Stub on PyPI** (§4) + binaries later | none | yes (stub has no real code) |

**Recommendation:** the CLI is stdlib-only and its value is the *protocol + hosted sync*, not the 7k lines of
`celeborn.py`. If you're comfortable that the local CLI is effectively source-visible (it already ships as
readable `.py` to anyone you grant repo access, and `integrity`/`doctor` assume users can see it), then
**PyPI is the right launch channel** — it's the no-signing, host-agnostic path you wanted, and you keep the
moat where it actually is (Supabase sync + entitlements + the App). Reserve the name today either way.

---

## 3. Full PyPI publish — runbook

Only do this once you've accepted §2's source-exposure. Three `pyproject.toml` edits are required first:

1. **Remove the upload blocker.** PyPI **rejects** any distribution carrying the
   `"Private :: Do Not Upload"` classifier — it's there today as an intentional guard. Delete that line from
   `[project] classifiers`.
2. **License metadata.** `license = { text = "LicenseRef-Proprietary" }` + `"License :: Other/Proprietary
   License"` are accepted by PyPI (it hosts proprietary packages). Leave as-is, or move to SPDX
   `license = "LicenseRef-Proprietary"` if you bump setuptools.
3. **Long description.** `readme = "README.md"` renders as the project page — confirm it has no secrets/local
   paths (it doesn't today).

Then:

```bash
cd ~/Desktop/celeborn
python3 -m pip install --upgrade build twine
python3 -m build                       # → dist/celeborn-0.1.0.tar.gz + celeborn-0.1.0-py3-none-any.whl

# smoke-test the wheel in a throwaway venv BEFORE uploading
python3 -m venv /tmp/cbtest && /tmp/cbtest/bin/pip install dist/*.whl
/tmp/cbtest/bin/celeborn version       # entry points: celeborn + cel → celeborn:main

# dry run on TestPyPI first
python3 -m twine upload --repository testpypi dist/*
#   verify: pip install -i https://test.pypi.org/simple/ celeborn

# real upload
python3 -m twine upload dist/*
```

**Prefer Trusted Publishing (no API token).** Add a PyPI "trusted publisher" for the `cloud-dancer-labs/celeborn`
repo + a `publish.yml` GitHub Action with `permissions: id-token: write` using
`pypa/gh-action-pypi-publish`. Works from a **private** repo, and means no long-lived token in secrets. This
is the recommended production path; the manual `twine` flow above is for the first/dry run.

**Verify after publish:** `uvx celeborn version` and `pipx install celeborn` from a clean machine.
Then update the README Install block and **thot.ai/celeborn** to lead with the one-liner.

---

## 4. Defensive name-reservation stub (no real code shipped)

If you're not ready to expose source but want to hold `pip install celeborn`, publish a minimal stub whose
only behavior is to point at the real installer. Keep the real package in a branch; the stub is a separate
tiny `pyproject` + a one-file module that prints the brew/native-install instructions and exits non-zero.
Bump to the real package later (same name, higher version). This buys time without resolving §2.

---

## 5. How this sits next to the rest of distribution

- **GitHub App** → GitHub Marketplace (free listing). No download, no signing. See
  [`github-app-listing.md`](github-app-listing.md) + [`github-app-manifest.json`](github-app-manifest.json).
- **CLI** → this doc. PyPI (no signing, source visible) **or** native binaries (signing required, source hidden).
- **Host-neutrality** is automatic for the CLI (it works on any folder/repo). Only the App is GitHub-coupled,
  and that's fine — it *is* a GitHub integration. See the corrected
  [`../CELEBORN_PATH1_EVALUATION.md`](../CELEBORN_PATH1_EVALUATION.md).
