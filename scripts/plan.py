#!/usr/bin/env python3
"""Show the upgrade plan for every new core version.

Wrapper around `dsh_upgrade.py plan`; the arguments are passed through.
Run it with DSH switched off. Example: python3 scripts/plan.py --limit 8
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsh_upgrade import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["plan", *sys.argv[1:]]))
