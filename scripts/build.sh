#!/bin/sh
# Rebuild the standalone Alpine bundle from its source modules.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$ROOT/scripts/alpine/build_setup.py" "$@"
