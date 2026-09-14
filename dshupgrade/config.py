"""Persistent settings: the same options as the flags, remembered between runs.

The settings screen of the menu writes here, so a choice made once — the profile,
the target core, the colour mode, where the version checkouts live — survives a
restart instead of being retyped as flags every time.

Priority for every option: **an explicit command-line flag > an environment
override > this file > the built-in default**. The file is written only when the
user changes something in the settings screen, never as a side effect of a normal
run: a check, a plan or an upgrade leaves it untouched.

Location: ``$DSH_UPGRADE_CONFIG``, else
``$XDG_CONFIG_HOME/dsh-upgrade/config.json``, else
``~/.config/dsh-upgrade/config.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import style
from .paths import read_json

#: Options that can be remembered, with their built-in defaults. The keys are
#: exactly the ``--flag`` names of the CLI, so a saved file reads like the long
#: version of the command line that would produce it.
DEFAULTS = {
    "profile": "web",
    "core": None,
    "offline": False,
    "no_clone": False,
    "json": False,
    "verbose": False,
    "color": None,
    "state_dir": None,
    "checkouts": "temp",
    #: The Termux correction. ``None`` means auto-detect (the layer is used on
    #: Termux and ignored elsewhere), ``True``/``False`` force it on or off.
    "termux": None,
    #: Where the Termux patch layer lives; None lets it be discovered.
    "termux_dir": None,
}

#: Keys that hold a truth value (toggled in the settings screen).
FLAGS = ("offline", "no_clone", "json", "verbose")


def config_path() -> Path:
    """Where the settings file lives (it need not exist)."""
    override = os.environ.get("DSH_UPGRADE_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "dsh-upgrade" / "config.json"


def _clean(name: str, value):
    """``value`` if it is usable for the option ``name``, else None (drop it).

    A settings file is user-editable, so every value is validated on the way in:
    a typo must degrade to the built-in default, never crash a run.
    """
    if name in ("profile", "core", "state_dir", "checkouts", "termux_dir"):
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    if name == "color":
        return value if isinstance(value, str) and value in style.MODES else None
    # The Termux correction is tri-state on purpose: None is "detect it", and only a
    # real boolean may override the detection.
    if name == "termux":
        return value if isinstance(value, bool) else None
    if name in FLAGS:
        return value if isinstance(value, bool) else None
    return None


def load() -> dict:
    """The saved options, ignoring unknown keys and unusable values."""
    data = read_json(config_path())
    if not isinstance(data, dict):
        return {}
    saved = {}
    for name in DEFAULTS:
        if name not in data:
            continue
        cleaned = _clean(name, data[name])
        if cleaned is not None:
            saved[name] = cleaned
    return saved


def save(updates: dict) -> Path:
    """Merge ``updates`` into the settings file (a value of None removes the key)."""
    merged = load()
    for name, value in updates.items():
        if name not in DEFAULTS:
            continue
        cleaned = _clean(name, value)
        if cleaned is None:
            merged.pop(name, None)
        else:
            merged[name] = cleaned

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: an interrupted save must not leave a half-written file.
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def forget() -> Path | None:
    """Delete the settings file; returns the removed path, or None if there was none."""
    path = config_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return path
