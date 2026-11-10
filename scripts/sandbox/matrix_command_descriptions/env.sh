#!/usr/bin/env bash
# Source from the worktree root; no activation or provisioning.
set -euo pipefail
MCD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
[ "$PWD" = "$MCD_ROOT" ] || { echo 'Run from the worktree root.' >&2; return 1; }
export SB="$PWD/.hermes-sandbox/matrix-commands"
[ ! -L "$PWD/.hermes-sandbox" ] && [ ! -L "$SB" ]
export HERMES_HOME="$SB/home" HERMES_RUNTIME_DIR="$SB/home/tools"
export HERMES_PYTHON="$PWD/.venv/bin/python"
[ -x "$HERMES_PYTHON" ] || { echo "Missing worktree .venv/bin/python" >&2; return 1; }
export PATH="$SB/bin:$(dirname "$HERMES_PYTHON"):$PATH"
export RUFF_CACHE_DIR="$SB/cache/ruff" XDG_CACHE_HOME="$SB/cache"
export TMPDIR="$SB/tmp" TMP="$SB/tmp" TEMP="$SB/tmp"
export PYTHONPYCACHEPREFIX="$SB/cache/pycache" PNPM_HOME="$SB/cache/pnpm"
export COREPACK_HOME="$SB/cache/corepack" npm_config_cache="$SB/cache/npm"
export PNPM_STORE_DIR="$SB/cache/pnpm-store" NX_DAEMON=false
[ "$HERMES_HOME" = "$PWD/.hermes-sandbox/matrix-commands/home" ]
[ "$HERMES_RUNTIME_DIR" = "$HERMES_HOME/tools" ]
[ "$(command -v python)" = "$HERMES_PYTHON" ]
umask 077
mkdir -p "$SB/bin" "$SB/tmp" "$SB/cache" "$SB/evidence"
