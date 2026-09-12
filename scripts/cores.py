#!/usr/bin/env python3
"""List the available core versions and tags.

Wrapper around `dsh_upgrade.py core-versions`; every argument is passed through.
Run it with DSH switched off. Example: python3 scripts/cores.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsh_upgrade import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["core-versions", *sys.argv[1:]]))
