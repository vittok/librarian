#!/bin/sh
# Bookroom installer: curl -fsSL https://raw.githubusercontent.com/vittok/librarian/main/install.sh | sh
set -eu

case "${1:-}" in
    --help|-h)
        cat <<'HELP'
Install Bookroom for the current user (Python 3.11+ and curl required).

Usage: sh install.sh [--prefix DIRECTORY] [--bin-dir DIRECTORY]
                     [--ref BRANCH_TAG_OR_SHA] [--no-pdf] [--run [-- APP_OPTIONS]]

Default: ~/.local/share/bookroom-app and ~/.local/bin/bookroom (Linux/macOS).
Use BOOKROOM_PYTHON=/path/to/python3 to choose the bootstrap interpreter.
Example: curl -fsSL https://raw.githubusercontent.com/vittok/librarian/main/install.sh | sh -s -- --run
HELP
        exit 0
        ;;
esac

if ! command -v curl >/dev/null 2>&1; then
    echo 'Bookroom setup requires curl. Install curl and retry.' >&2
    exit 1
fi

bookroom_python=''
if [ -n "${BOOKROOM_PYTHON:-}" ]; then
    if "$BOOKROOM_PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
        bookroom_python=$BOOKROOM_PYTHON
    else
        echo 'BOOKROOM_PYTHON must point to a working Python 3.11+ executable.' >&2
        exit 1
    fi
else
    for bookroom_candidate in python3 python3.14 python3.13 python3.12 python3.11 python; do
        if command -v "$bookroom_candidate" >/dev/null 2>&1 && "$bookroom_candidate" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
            bookroom_python=$(command -v "$bookroom_candidate")
            break
        fi
    done
fi
if [ -z "$bookroom_python" ]; then
    echo 'Python 3.11+ is required. Install Python from https://www.python.org/downloads/ (with venv support) and retry.' >&2
    exit 1
fi

bookroom_tmp=$(mktemp -d "${TMPDIR:-/tmp}/bookroom-bootstrap.XXXXXXXX")
trap 'rm -rf "$bookroom_tmp"' 0
trap 'exit 130' INT
trap 'exit 143' HUP TERM
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    'https://raw.githubusercontent.com/vittok/librarian/main/install.py' \
    --output "$bookroom_tmp/install.py"
"$bookroom_python" -I "$bookroom_tmp/install.py" "$@"
