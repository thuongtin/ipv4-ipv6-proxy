#!/bin/sh
# Local-only checks. No VPS, provider API, or public network calls.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT/scripts/alpine${PYTHONPATH:+:$PYTHONPATH}"
python3 scripts/alpine/build_setup.py --check
python3 -m unittest discover -s tests -p 'test_*.py' -v
command -v shellcheck >/dev/null || { echo 'Install ShellCheck to complete local validation' >&2; exit 1; }
for script in setup-alpine.sh scripts/*.sh scripts/alpine/bootstrap.sh; do
    sh -n "$script"
    shellcheck -x "$script"
done
git diff --check
printf 'LOCAL_CHECKS_PASS\n'
