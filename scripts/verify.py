#!/usr/bin/env python3
"""Check that the INSTALLED plugins actually load; import each one with node.

Wrapper around `dsh_upgrade.py verify`; every argument is passed through.
Run it with DSH switched off. Example: python3 scripts/verify.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsh_upgrade import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["verify", *sys.argv[1:]]))
