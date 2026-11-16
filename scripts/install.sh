#!/bin/sh
# Install or reconfigure only Alpine through the single generated entry point.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
exec sh "$ROOT/setup-alpine.sh" "$@"
