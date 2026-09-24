#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Concurrent sidecar writes never tear: write_sidecar is atomic
(tmp + os.replace), so N racing writers must leave one whole payload
— never a mix of two."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INV = load(ROOT / "skills" / "jev-consult" / "scripts" / "inventory.py", "jev_inventory")

WRITERS = 8
ROUNDS = 50


class ConcurrentSidecarTests(unittest.TestCase):
    def test_parallel_writes_leave_one_whole_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / ".jev-tools.json"
            for rnd in range(ROUNDS):
                threads = []
                for w in range(WRITERS):
                    picked = [
                        {"kind": "skill", "name": "w%d-r%d" % (w, rnd),
                         "id": "id%d" % w,
                         "description": "payload %d round %d" % (w, rnd),
                         "path": str(sidecar)},
                    ]
                    threads.append(
                        threading.Thread(
                            target=INV.write_sidecar,
                            args=(sidecar, "claude-code", "task-%d" % w, picked),
                        )
                    )
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                tasks = {p.get("task") for p in [payload]}
                self.assertEqual(len(tasks), 1)
                self.assertRegex(payload["task"], r"^task-\d$")
                names = [n["name"] for n in payload["names"]]
                self.assertTrue(
                    all(n.endswith("r%d" % rnd) for n in names),
                    "torn payload: %r" % names,
                )
                self.assertFalse(
                    sidecar.with_name(sidecar.name + ".tmp").exists()
                )


if __name__ == "__main__":
    unittest.main()
