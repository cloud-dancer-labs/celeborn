# PyInstaller spec — one-file `celeborn` binary for macOS and Windows.
#
#   pip install pyinstaller
#   pyinstaller packaging/celeborn.spec        # → dist/celeborn  (or dist/celeborn.exe on Windows)
#
# The binary bundles BOTH flat modules (celeborn.py + the lazily-imported celeborn_sync.py) and the
# entire references/ tree under `celeborn_refs/` so the frozen build resolves templates + schema.sql
# exactly like a pip/uv install (see celeborn.py:_data_dir, which checks sys._MEIPASS when frozen).
# A VERSION file is baked alongside the data so `celeborn version` works without package metadata.

import os
import re

block_cipher = None

ROOT = os.path.abspath(os.path.join(os.getcwd()))
SCRIPTS = os.path.join(ROOT, "scripts")
REFS = os.path.join(ROOT, "references")

# Bake the version (regex — no toml dep, matching celeborn.py's own approach).
_pyproject = open(os.path.join(ROOT, "pyproject.toml")).read()
_version = (re.search(r'^version\s*=\s*"([^"]+)"', _pyproject, re.M) or [None, "0.0.0"])[1]
_version_file = os.path.join(ROOT, "packaging", ".version")
with open(_version_file, "w") as fh:
    fh.write(_version + "\n")

a = Analysis(
    [os.path.join(SCRIPTS, "celeborn.py")],
    pathex=[SCRIPTS],
    binaries=[],
    datas=[
        (REFS, "celeborn_refs"),                 # templates/, schema.sql, *.md → _MEIPASS/celeborn_refs
        (_version_file, "celeborn_refs/VERSION") # → _MEIPASS/celeborn_refs/VERSION
    ],
    hiddenimports=["celeborn_sync"],             # imported lazily via __import__, so declare it
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "setuptools", "pip"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="celeborn",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                # it's a CLI
    disable_windowed_traceback=False,
    target_arch=None,            # set per-build (arm64 / x86_64) via the build script
    codesign_identity=None,      # signing/notarization handled in CI with the org's certs
    entitlements_file=None,
)
