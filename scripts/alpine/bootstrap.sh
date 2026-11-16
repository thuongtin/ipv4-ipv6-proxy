#!/bin/sh
# Standalone Alpine installer template. Build with build_setup.py.
set -eu
umask 077

usage() {
    cat <<'HELP'
Usage: sh setup-alpine.sh [manager options]
       sh setup-alpine.sh --operation COMMAND [options]
       sh setup-alpine.sh --extract-only DIRECTORY

Install 3proxy 1.0.3 on Alpine and configure an authenticated IPv4-to-IPv6 pool.
Default pool size: 2000. Credentials: PROXY_USERNAME and PROXY_PASSWORD, or TTY prompts.
Manager options: --count N --start-port N --ipv4 ADDRESS
                 --ipv6-prefix PREFIX --interface NAME
                 --rollback SNAPSHOT_DIRECTORY
Operations (no package installation or build):
  preflight [--ipv4 ADDRESS --ipv6-prefix PREFIX --interface NAME] [--probe-ipv6]
  status [--json]
  verify [--sample N | --all] [--timeout SECONDS] [--output NEW_FILE]
  restart
  credentials          Read PROXY_USERNAME/PROXY_PASSWORD or prompt on a TTY.
  export --format txt|csv|json --output NEW_FILE
  rollback --list | --snapshot SNAPSHOT_DIRECTORY
--help                Show this help without changing the system.
--extract-only DIR    Extract Python tools without root, Alpine, or package changes.
HELP
}

extract_modules() {
    mkdir -p "$TOOLS"
__EMBEDDED_MODULES__
}

# Help is side-effect free even when combined with other options.
for argument in "$@"; do
    case "$argument" in
        --help|-h) usage; exit 0 ;;
    esac
done
if [ "${1:-}" = '--extract-only' ]; then
    [ "$#" -eq 2 ] && [ -n "$2" ] || { echo 'Usage: --extract-only DIRECTORY' >&2; exit 2; }
    TOOLS=$2
    extract_modules
    printf 'Extracted Python tools to %s\n' "$TOOLS"
    exit 0
fi

[ "$(id -u)" -eq 0 ] && [ -f /etc/alpine-release ] || {
    echo 'Root on Alpine is required' >&2
    exit 1
}
BASE=/var/lib/3proxy-setup
BINARY=/usr/local/bin/3proxy
VERSION=1.0.3
SOURCE_SHA256=71acfaf75cdabe6c486aa182eb2ecb8ec731bdb05d52d3f4efe68ecdc1898492

# Operations never install packages or persist replacement tools. An older
# installation without ops.py can use temporary embedded modules instead.
if [ "${1:-}" = '--operation' ]; then
    [ "$#" -ge 2 ] || { echo 'Specify an operation; use --help for the list' >&2; exit 2; }
    shift
    OPERATION=$1
    shift
    case "$OPERATION" in
        preflight|status|verify|restart|credentials|export|rollback) ;;
        *) echo 'Unknown operation; use --help for the list' >&2; exit 2 ;;
    esac
    command -v python3 >/dev/null || { echo 'Operations require Python 3; the setup command installs dependencies' >&2; exit 1; }
    if [ -f "$BASE/tools/ops.py" ]; then
        exec python3 "$BASE/tools/ops.py" "$OPERATION" "$@"
    fi
    TOOLS=$(mktemp -d /tmp/3proxy-operation.XXXXXX)
    trap 'rm -rf "$TOOLS"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    extract_modules
    python3 "$TOOLS/ops.py" "$OPERATION" "$@"
    exit $?
fi

# Rollback uses the installed manager without package, binary, or tool upgrades.
for argument in "$@"; do
    case "$argument" in
        --rollback|--rollback=*)
            command -v python3 >/dev/null || {
                echo 'Rollback requires the Python interpreter from the managed installation' >&2
                exit 1
            }
            for module in manage.py config.py ipv6_pool.py; do
                [ -f "$BASE/tools/$module" ] || {
                    echo 'Rollback requires the installed managed Python tools' >&2
                    exit 1
                }
            done
            exec python3 "$BASE/tools/manage.py" "$@"
            ;;
    esac
done

# Reuse only the exact binary recorded by this installer or the prior managed installer.
if [ -e "$BINARY" ] || [ -L "$BINARY" ]; then
    MATCH=0
    for manifest in "$BASE/binary.sha256" "$BASE/installed.sha256"; do
        if [ -f "$manifest" ]; then
            expected=$(awk -v binary="$BINARY" '$2 == binary {print $1; exit}' "$manifest")
            actual=$(sha256sum "$BINARY" | awk '{print $1}')
            if [ -n "$expected" ] && [ "$expected" = "$actual" ]; then MATCH=1; break; fi
        fi
    done
    [ "$MATCH" -eq 1 ] && [ -x "$BINARY" ] || {
        echo 'Existing 3proxy binary is not a verified managed installation; leaving it unchanged' >&2
        exit 1
    }
fi

apk add --no-cache python3 curl ca-certificates iproute2 openrc ufw
mkdir -p "$BASE" /usr/local/bin
if [ ! -e "$BINARY" ]; then
    BUILD=$(mktemp -d /tmp/3proxy-build.XXXXXX)
    BUILD_PACKAGE=".3proxy-setup-build-$$"
    BUILD_DEPS=0
    cleanup() {
        rm -rf "$BUILD"
        if [ "$BUILD_DEPS" -eq 1 ]; then apk del "$BUILD_PACKAGE" >/dev/null; fi
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    apk add --no-cache --virtual "$BUILD_PACKAGE" build-base linux-headers
    BUILD_DEPS=1
    ARCHIVE="$BUILD/3proxy-$VERSION.tar.gz"
    if [ -n "${SOURCE_ARCHIVE:-}" ]; then
        cp "$SOURCE_ARCHIVE" "$ARCHIVE"
    else
        curl --fail --location --retry 2 --proto '=https' --tlsv1.2 \
            -o "$ARCHIVE" "https://github.com/3proxy/3proxy/releases/download/$VERSION/3proxy-$VERSION.tar.gz"
    fi
    printf '%s  %s\n' "$SOURCE_SHA256" "$ARCHIVE" | sha256sum -c -
    tar -xzf "$ARCHIVE" -C "$BUILD"
    make -C "$BUILD/3proxy-$VERSION" -f Makefile.Linux -j1 HTTPSRV=false PLUGINS= \
        PAM_CHECK=false PCRE_CHECK=false OPENSSL_CHECK=false WOLFSSL_CHECK=false > "$BASE/build.log" 2>&1
    BUILT_BINARY="$BUILD/3proxy-$VERSION/bin/3proxy"
    [ -s "$BUILT_BINARY" ] && [ -x "$BUILT_BINARY" ] || {
        echo 'Build did not produce an executable 3proxy binary' >&2
        exit 1
    }
    # strip rejects invalid object files before anything is installed.
    strip "$BUILT_BINARY"
    install -m 755 "$BUILT_BINARY" "$BINARY"
    sha256sum "$BINARY" > "$BASE/binary.sha256"
    cleanup
    trap - EXIT INT TERM
fi
TOOLS="$BASE/tools"
extract_modules
exec python3 "$TOOLS/manage.py" "$@"
