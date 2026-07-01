#!/usr/bin/env bash
# Celeborn PreCompact hook — collapsed (executable-app.md §3). Thin shim that execs the in-process
# `celeborn hook pre-compact` entry point. Reads the host's event JSON on stdin (inherited via exec) and
# runs THIS clone's celeborn.py directly — no _resolve, no inline python3, no $CELEBORN_HOME on PATH.
#
# Kept only so installs wired to this script PATH keep working; `celeborn wire` now points hooks at
# the `celeborn hook <event>` CLI command instead. Re-run `celeborn wire` to migrate.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$DIR/scripts/celeborn.py" hook pre-compact
