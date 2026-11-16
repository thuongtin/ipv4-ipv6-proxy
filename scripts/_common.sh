#!/bin/sh
# Shared operation launcher; never installs dependencies or changes credentials.
set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
run_operation() {
    operation=$1
    shift
    command -v python3 >/dev/null || { echo 'Python 3 is required; use the setup entry point to install dependencies' >&2; exit 1; }
    if [ -f /var/lib/3proxy-setup/tools/ops.py ]; then
        exec python3 /var/lib/3proxy-setup/tools/ops.py "$operation" "$@"
    fi
    exec python3 "$SCRIPT_DIR/alpine/ops.py" "$operation" "$@"
}
