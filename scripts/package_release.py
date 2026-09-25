#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build dist/jev-setup.pyz — a one-file jev-consult installer (zipapp).

The .pyz carries payload/skills/jev-consult (including scripts/doctor.py) and
payload/scripts/install.py plus a __main__.py that extracts the payload to a
temp dir and runs install.py --source <payload>. No git clone needed:

    python jev-setup.pyz            # interactive setup menu in a TTY
    python jev-setup.pyz --check-key
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "jev-consult"
INSTALLER = ROOT / "scripts" / "install.py"
DEFAULT_OUT = ROOT / "dist" / "jev-setup.pyz"

BOOTSTRAP = '''#!/usr/bin/env python
"""jev-setup bootstrap: unpack the bundled payload, run install.py."""
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def _extract() -> Path:
    dest = Path(tempfile.mkdtemp(prefix="jev-setup-"))
    with zipfile.ZipFile(sys.argv[0]) as zf:
        for name in zf.namelist():
            if name.startswith("payload/"):
                zf.extract(name, dest)
    return dest / "payload"


def main() -> int:
    payload = _extract()
    args = sys.argv[1:]
    cmd = [
        sys.executable,
        str(payload / "scripts" / "install.py"),
        "--source",
        str(payload),
    ]
    if not args:
        cmd.append("--setup")
    cmd.extend(args)
    try:
        return subprocess.call(cmd)
    finally:
        shutil.rmtree(payload.parent, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
'''


def build(out: Path) -> Path:
    """Stage payload/ + __main__.py in a temp dir and zipapp it."""
    with tempfile.TemporaryDirectory(prefix="jev-setup-build-") as tmp:
        app = Path(tmp) / "app"
        payload = app / "payload"
        (payload / "skills").mkdir(parents=True)
        shutil.copytree(SKILL, payload / "skills" / "jev-consult")
        (payload / "scripts").mkdir(parents=True)
        shutil.copy2(INSTALLER, payload / "scripts" / "install.py")
        (app / "__main__.py").write_text(BOOTSTRAP, encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(
            app,
            target=str(out),
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the single-file jev-setup.pyz installer (zipapp)."
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        metavar="PATH",
        help="Output .pyz path (default: dist/jev-setup.pyz).",
    )
    args = parser.parse_args(argv)
    for need in (SKILL / "SKILL.md", INSTALLER):
        if not need.is_file():
            sys.stderr.write("package_release: missing %s\n" % need)
            return 2
    out = build(Path(args.out))
    sys.stdout.write("wrote %s\n" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
