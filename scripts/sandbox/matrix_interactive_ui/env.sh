#!/usr/bin/env bash
# Source from the worktree root; no activation or provisioning.
set -euo pipefail
MUI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
[ "$PWD" = "$MUI_ROOT" ] || { echo 'Run from the worktree root.' >&2; return 1; }
export SB="$PWD/.hermes-sandbox/matrix-ui"
[ ! -L "$PWD/.hermes-sandbox" ] && [ ! -L "$SB" ]
export HERMES_HOME="$SB/home" HERMES_RUNTIME_DIR="$SB/home/tools"
export HERMES_PYTHON="$SB/home/installs/865670c36e681bef/test-environment/gen-a030d2f93353456a9de98107c9975f81/venv/bin/python"
export PATH="$(dirname "$HERMES_PYTHON"):$PATH"
export RUFF_CACHE_DIR="$SB/cache/ruff" XDG_CACHE_HOME="$SB/cache"
export TMPDIR="$SB/tmp" TMP="$SB/tmp" TEMP="$SB/tmp"
export PYTHONPYCACHEPREFIX="$SB/cache/pycache" PNPM_HOME="$SB/cache/pnpm"
export COREPACK_HOME="$SB/cache/corepack" npm_config_cache="$SB/cache/npm"
export PNPM_STORE_DIR="$SB/cache/pnpm-store" NX_DAEMON=false
[ "$HERMES_HOME" = "$PWD/.hermes-sandbox/matrix-ui/home" ]
[ "$HERMES_RUNTIME_DIR" = "$HERMES_HOME/tools" ]
[ "$(command -v python)" = "$HERMES_PYTHON" ]
umask 077
mkdir -p "$SB/tmp" "$SB/cache" "$SB/evidence"
