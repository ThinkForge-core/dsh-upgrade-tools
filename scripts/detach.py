#!/usr/bin/env python3
"""Snapshot the profile and detach every plugin.

Wrapper around `dsh_upgrade.py detach`; every argument is passed through.
Run it with DSH switched off. Example: python3 scripts/detach.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsh_upgrade import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["detach", *sys.argv[1:]]))
