#!/usr/bin/env bash
# Install Celeborn Grok Build hooks into ~/.grok/hooks/ and bootstrap project memory.
set -euo pipefail

# Every celeborn call from here on resolves GrokAdapter (the bridge hooks export this too at runtime).
export CELEBORN_HARNESS=grok

MODULE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADAPTER="${MODULE}/scripts/grok_celeborn.py"
DEST="${GROK_HOME:-$HOME/.grok}/hooks"
PROJECT=""
RUN_INIT=0
INIT_PRIVATE=0
INIT_PUBLIC=0
NO_HARNESS_PIN=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Install Celeborn Grok hooks (global, auto-load on every new Grok session).

Options:
  --project PATH   Project root: run celeborn init + bootstrap there
  --init           Run celeborn init in cwd when .context/ is missing
  --private        Pass --private to celeborn init
  --public         Pass --public to celeborn init
  --no-init        Skip celeborn init even if .context/ is missing
  --no-harness-pin Don't pin harness=grok in .celebornrc (used by core's speculative wiring)
  -h, --help       Show this help

One-liner (hooks + memory + orient):
  python3 ~/.grok/skills/celeborn-grok/scripts/grok_celeborn.py install --project .
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --init) RUN_INIT=1; shift ;;
    --private) INIT_PRIVATE=1; shift ;;
    --public) INIT_PUBLIC=1; shift ;;
    --no-init) RUN_INIT=-1; shift ;;
    --no-harness-pin) NO_HARNESS_PIN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v celeborn >/dev/null 2>&1; then
  echo "Error: celeborn is not on PATH. Install with:" >&2
  echo "  uv tool install --editable /path/to/celeborn" >&2
  exit 1
fi

SKILL_DEST="${GROK_HOME:-$HOME/.grok}/skills/celeborn-grok"
mkdir -p "$DEST" "$SKILL_DEST/hooks" "$SKILL_DEST/scripts" "$SKILL_DEST/cache"
cp "$MODULE/hooks/celeborn.json" "$DEST/celeborn.json"
cp "$MODULE/SKILL.md" "$SKILL_DEST/"
cp "$MODULE/hooks/celeborn.json" "$SKILL_DEST/hooks/"
cp "$MODULE/scripts/grok_celeborn.py" "$SKILL_DEST/scripts/"
cp "$MODULE/scripts/install.sh" "$SKILL_DEST/scripts/"
chmod +x "$SKILL_DEST/scripts/grok_celeborn.py" "$SKILL_DEST/scripts/install.sh"
ADAPTER="$SKILL_DEST/scripts/grok_celeborn.py"

TARGET="${PROJECT:-$PWD}"
if [[ -z "$PROJECT" && "$RUN_INIT" -eq 0 && ! -d "$TARGET/.context" ]]; then
  RUN_INIT=1
fi

if [[ "$RUN_INIT" -ge 0 && ! -d "$TARGET/.context" ]]; then
  INIT_ARGS=(--path "$TARGET" init --no-claude-md)
  if [[ "$INIT_PRIVATE" -eq 1 ]]; then
    INIT_ARGS+=(--private)
  elif [[ "$INIT_PUBLIC" -eq 1 ]]; then
    INIT_ARGS+=(--public)
  fi
  celeborn "${INIT_ARGS[@]}"
fi

if [[ -d "$TARGET/.context" ]]; then
  BOOTSTRAP_ARGS=(bootstrap --path "$TARGET")
  if [[ "$NO_HARNESS_PIN" -eq 1 ]]; then
    BOOTSTRAP_ARGS+=(--no-harness-pin)
  fi
  python3 "$ADAPTER" "${BOOTSTRAP_ARGS[@]}"
fi

python3 "$ADAPTER" doctor

echo ""
echo "Installed skill → $SKILL_DEST"
echo "Installed Grok hooks → $DEST/celeborn.json"
echo "Hooks load automatically on every new Grok session — no manual reload."

if [[ -f "${GROK_HOME:-$HOME/.grok}/active_sessions.json" ]] && \
   python3 -c "
import json, sys
from pathlib import Path
target = Path('${TARGET}').resolve()
active = Path('${GROK_HOME:-$HOME/.grok}/active_sessions.json')
try:
    sessions = json.loads(active.read_text())
except Exception:
    sys.exit(1)
for s in sessions:
    try:
        if Path(s.get('cwd','')).resolve() == target:
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)
" 2>/dev/null; then
  echo "Grok is open on this project — type /clear once to activate hooks in this session."
fi