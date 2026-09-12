#!/usr/bin/env python3
"""Re-check the incompatible list.

Wrapper around `dsh_upgrade.py recheck`; every argument is passed through.
Run it with DSH switched off. Example: python3 scripts/recheck.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsh_upgrade import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["recheck", *sys.argv[1:]]))
