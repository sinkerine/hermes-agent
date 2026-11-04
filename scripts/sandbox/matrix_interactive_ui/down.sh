#!/usr/bin/env bash
set -euo pipefail
source scripts/sandbox/matrix_interactive_ui/env.sh
python scripts/sandbox/matrix_interactive_ui/lifecycle.py down "$@"
