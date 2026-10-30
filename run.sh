#!/bin/sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    echo 'Execute primeiro: sh install.sh' >&2
    exit 1
fi
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" "$@"
