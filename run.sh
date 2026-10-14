#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PLAYWRIGHT_SKIP_BROWSER_GC=1
exec .venv/bin/book-monitor serve "$@"
