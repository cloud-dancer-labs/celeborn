#!/usr/bin/env bash
# Build a standalone `celeborn` binary on macOS/Linux. Output: dist/celeborn
#
# Builds inside an isolated venv so it works on PEP 668 "externally managed" interpreters too
# (e.g. Homebrew Python on macOS), where a bare `pip install` is refused. CI's setup-python is
# unaffected either way.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="${CELEBORN_BUILD_VENV:-build/venv}"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip pyinstaller
"$VENV/bin/pyinstaller" --clean --noconfirm packaging/celeborn.spec

echo "✓ built dist/celeborn"
./dist/celeborn version || true
