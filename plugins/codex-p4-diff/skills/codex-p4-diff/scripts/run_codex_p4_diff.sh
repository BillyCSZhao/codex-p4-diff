#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
        export PYTHONUTF8=1
        exec "$candidate" "$SCRIPT_DIR/codex_p4_diff.py" "$@"
    fi
done

echo 'Codex P4 Diff requires Python 3.10+. Install Python, then restart Codex.' >&2
exit 2
