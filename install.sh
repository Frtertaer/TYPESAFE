#!/bin/sh
set -e
cd "$(dirname "$0")"
exec python scripts/install.py "$@"
