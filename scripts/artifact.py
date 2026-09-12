#!/usr/bin/env python3
"""Check an artifact that is NOT installed in the profile yet.

Wrapper around `dsh_upgrade.py inspect`; every argument is passed through.
The artifact is a plugin directory or a `.tgz`, and the verdict is checked
against the INSTALLED core unless `--core` says otherwise. Nothing is installed
and the profile is not read.

The file is deliberately NOT named `inspect.py`: the script's own directory is
prepended to sys.path, so that name would shadow the stdlib `inspect` module and
break every import that needs it (`dataclasses`).

Run it with DSH switched off. Example: python3 scripts/artifact.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsh_upgrade import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["inspect", *sys.argv[1:]]))
