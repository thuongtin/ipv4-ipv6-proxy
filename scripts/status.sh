#!/bin/sh
# Alpine status operation. See docs/OPERATIONS.md.
set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=scripts/_common.sh
. "$SCRIPT_DIR/_common.sh"
run_operation status "$@"
