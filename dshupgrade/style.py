"""Terminal styling: ANSI colors, terminal width and text wrapping.

Everything here is optional decoration. When colors are disabled the output is
plain text; every width computation strips escape sequences first, so a colored
cell can never break the alignment of a table.

Color policy (same as most CLI tools):

* ``auto`` (default) — colors only when stdout is a terminal, unless ``NO_COLOR``
  is set or ``TERM`` is ``dumb``;
* ``always`` — colors even when redirected (useful for logs);
* ``never`` — never.

The mode can be forced by the ``DSH_UPGRADE_COLOR`` environment variable or by
the ``--color`` command line flag; the flag wins.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap

RESET = "\033[0m"

CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "italic": "\033[3m",
    "underline": "\033[4m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "grey": "\033[90m",
}

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

MODES = ("auto", "always", "never")

# "auto" | "always" | "never"; changed by set_mode() from the CLI/env.
_MODE = "auto"


def normalize_mode(mode: str | None) -> str:
    """Coerce a user supplied value to one of the known modes."""
    value = (mode or "").strip().lower()
    if value in MODES:
        return value
    aliases = {"on": "always", "yes": "always", "true": "always", "1": "always",
               "off": "never", "no": "never", "false": "never", "0": "never"}
    return aliases.get(value, "auto")


def set_mode(mode: str | None = None) -> str:
    """Set the global color mode; ``None`` falls back to the environment."""
    global _MODE
    _MODE = normalize_mode(mode if mode is not None else os.environ.get("DSH_UPGRADE_COLOR"))
    return _MODE


def mode() -> str:
    return _MODE


def enabled(stream=None) -> bool:
    """Are colors on for this stream right now?"""
    if _MODE == "always":
        return True
    if _MODE == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if (os.environ.get("TERM") or "").strip().lower() == "dumb":
        return False
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


def paint(text: object, *styles: str) -> str:
    """Wrap text in ANSI styles (a no-op when colors are disabled)."""
    value = "" if text is None else str(text)
    if not styles or not enabled() or not value:
        return value
    prefix = "".join(CODES.get(style, "") for style in styles)
    if not prefix:
        return value
    return f"{prefix}{value}{RESET}"


def strip(text: object) -> str:
    """Remove ANSI sequences from text."""
    return _ANSI_RE.sub("", "" if text is None else str(text))


def width(text: object) -> int:
    """Visible width of text: escape sequences do not count."""
    return len(strip(text))


def terminal_width(default: int = 100, *, minimum: int = 40) -> int:
    """Current terminal width in columns (``COLUMNS`` is honored)."""
    try:
        columns = shutil.get_terminal_size((default, 24)).columns
    except (OSError, ValueError):
        columns = default
    return max(minimum, columns or default)


def wrap(text: object, limit: int) -> list[str]:
    """Wrap one paragraph to ``limit`` columns; long tokens are broken.

    ANSI sequences are removed first: they occupy no columns, and a naive break
    would cut an escape sequence in half. Styling belongs outside the wrapping
    step (or the text must already fit).
    """
    value = strip(text)
    if limit <= 0:
        return [value]
    if not value.strip():
        return [value]
    return textwrap.wrap(
        value,
        width=limit,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=True,
        drop_whitespace=True,
    ) or [""]


def wrap_lines(text: object, limit: int) -> list[str]:
    """Wrap every line of a possibly multi-line text."""
    value = "" if text is None else str(text)
    lines: list[str] = []
    for line in value.split("\n"):
        lines.extend(wrap(line, limit))
    return lines or [""]


def fits(text: object, limit: int) -> bool:
    """Does a single-line rendering of text fit into ``limit`` columns?"""
    return width(text) <= limit


# --------------------------------------------------------------------------- #
# Ready-made styles for the reports
# --------------------------------------------------------------------------- #

def heading(text: object) -> str:
    """Section title (``=== Like this ===``)."""
    return paint(text, "bold", "cyan")


def subheading(text: object) -> str:
    return paint(text, "bold")


def dim(text: object) -> str:
    return paint(text, "dim")


def good(text: object) -> str:
    return paint(text, "green")


def bad(text: object) -> str:
    return paint(text, "bold", "red")


def warn(text: object) -> str:
    return paint(text, "yellow")


def info(text: object) -> str:
    return paint(text, "cyan")


def path(text: object) -> str:
    return paint(text, "underline", "cyan")


def command(text: object) -> str:
    return paint(text, "bold", "magenta")


def rule(width_hint: int = 0, *, character: str = "─") -> str:
    """A horizontal rule of the terminal width (or of ``width_hint``)."""
    columns = width_hint or terminal_width()
    return paint(character * columns, "dim")
