#!/usr/bin/env python3
"""Open the interactive menu.

Wrapper around `dsh_upgrade.py menu`; the arguments are passed through.
Example: python3 scripts/menu.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsh_upgrade import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["menu", *sys.argv[1:]]))
