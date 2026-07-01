#!/usr/bin/env bash
# Install Celeborn Codex CLI hooks into ~/.codex/ and bootstrap project memory.
# Mirrors grok/scripts/install.sh. Does not modify Celeborn core.
set -euo pipefail

# Every celeborn call from here on resolves CodexAdapter (the bridge hooks export this too at runtime).
export CELEBORN_HARNESS=codex

MODULE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
DEST="${CODEX_DIR}/hooks"
PROJECT=""
RUN_INIT=0
INIT_PRIVATE=0
INIT_PUBLIC=0
NO_HARNESS_PIN=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Install Celeborn Codex hooks (~/.codex/hooks/celeborn.json) + AGENTS.md orient block.

Options:
  --project PATH   Project root: run celeborn init + bootstrap there
  --init           Run celeborn init in cwd when .context/ is missing
  --private        Pass --private to celeborn init
  --public         Pass --public to celeborn init
  --no-init        Skip celeborn init even if .context/ is missing
  --no-harness-pin Don't pin harness=codex in .celebornrc (for speculative wiring; parity with grok)
  -h, --help       Show this help

Note: Codex loads hooks from a [hooks] table in ~/.codex/config.toml on some builds, and from
~/.codex/hooks/*.json on others. This installs the JSON form; if your Codex ignores it, copy the
[hooks] entries from hooks/celeborn.json into ~/.codex/config.toml (same shape). The AGENTS.md
orient block works regardless — Codex auto-loads AGENTS.md every session.
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

SKILL_DEST="${CODEX_DIR}/skills/celeborn-codex"
mkdir -p "$DEST" "$SKILL_DEST/hooks" "$SKILL_DEST/scripts" "$SKILL_DEST/cache"
cp "$MODULE/hooks/celeborn.json" "$DEST/celeborn.json"
cp "$MODULE/SKILL.md" "$SKILL_DEST/"
cp "$MODULE/hooks/celeborn.json" "$SKILL_DEST/hooks/"
cp "$MODULE/scripts/codex_celeborn.py" "$SKILL_DEST/scripts/"
cp "$MODULE/scripts/install.sh" "$SKILL_DEST/scripts/"
chmod +x "$SKILL_DEST/scripts/codex_celeborn.py" "$SKILL_DEST/scripts/install.sh"
ADAPTER="$SKILL_DEST/scripts/codex_celeborn.py"

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
echo "Installed Codex hooks → $DEST/celeborn.json"
echo "AGENTS.md in the project carries the orient block — Codex loads it every session."
echo "If hooks don't fire, copy the [hooks] table from hooks/celeborn.json into ~/.codex/config.toml."
