#!/bin/sh
set -e
case $0 in
  */*) cd "${0%/*}" ;;
esac

PY=
for cand in "py -3" python3 python; do
    if $cand -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1; then
        PY=$cand
        break
    fi
done
if [ -z "$PY" ]; then
    echo "install.sh: Python 3 not found on PATH." >&2
    echo "Get it from https://www.python.org/downloads/ - on Windows tick 'Add python.exe to PATH'." >&2
    exit 1
fi
exec $PY scripts/install.py --setup "$@"
