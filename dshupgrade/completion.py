"""Terminal-grade path input: filename completion and shell-style unescaping.

The tool asks for paths (an artifact to inspect, a snapshot to restore from, the
directory the version checkouts should live in). Typing a path by hand invites
typos, so where the input really is a terminal the reader is wired to
``readline``: the line editing of an ordinary shell (arrows, Home/End, Ctrl+A/E),
Tab completion of files and directories, and a recall list of the paths entered
before.

``readline`` is a stdlib module but is not guaranteed to exist (some embedded
builds), so nothing here imports it eagerly: :func:`path_candidates` and
:func:`unescape` are pure and testable on their own, and :func:`install` only
wires them up when the module is actually importable.
"""

from __future__ import annotations

import os
import re

try:  # pragma: no cover - depends on the platform build
    import readline as _readline
except ImportError:  # pragma: no cover - depends on the platform build
    _readline = None

#: Path specifiers the tool accepts for a plugin — completed after the prefix.
SPECIFIERS = ("file:", "link:")

#: Characters that would otherwise be eaten by the shell, so completion escapes
#: them with a backslash (``unescape`` turns the line back into a plain path).
_SPECIAL = " \t\\'\"`!$&()*;<>?[]|{}"

_UNESCAPE_RE = re.compile(r"\\([" + re.escape(_SPECIAL) + r"])")

#: State of the current completion round (readline asks for one match at a time).
_matches: list[str] = []
_match_text: str = ""
_hooked = False


def escape(text: str) -> str:
    """Backslash-escape every shell special character in ``text``."""
    return "".join(("\\" + char if char in _SPECIAL else char) for char in text)


def unescape(text: str) -> str:
    """The plain path behind ``text``: quotes and backslash escapes are removed.

    Both spellings work, because a human may type either: ``/tmp/a\\ b.tgz``
    (what completion produces) and ``/tmp/a b.tgz`` (what a person types).
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    return _UNESCAPE_RE.sub(r"\1", text)


def prepare(text: str) -> str:
    """A typed path ready for the filesystem: unescaped and ``~`` expanded."""
    return os.path.expanduser(unescape(text))


def path_candidates(text: str) -> list[str]:
    """Bash-like completions for the path prefix ``text`` (the word being typed).

    Directories are completed with a trailing slash; a ``file:``/``link:``
    specifier is preserved so complete artifacts can be typed. Hidden entries are
    offered only when the name already starts with a dot, as in a shell.
    """
    prefix = ""
    for specifier in SPECIFIERS:
        if text.startswith(specifier):
            prefix, text = specifier, text[len(specifier):]
            break

    split = text.rfind("/")
    typed_dir = text[:split + 1] if split >= 0 else ""
    base = text[split + 1:]

    if typed_dir == "" and base.startswith("~"):
        # A bare ``~`` (or ``~user``) completes to the directory, as in a shell.
        expanded = os.path.expanduser(base)
        return [base + "/"] if expanded != base and os.path.isdir(expanded) else []

    scan = os.path.expanduser(typed_dir) or "."
    try:
        names = sorted(os.listdir(scan))
    except OSError:
        return []

    found = []
    for name in names:
        if not name.startswith(base):
            continue
        if name.startswith(".") and not base.startswith("."):
            continue
        candidate = typed_dir + name
        if os.path.isdir(os.path.join(scan, name)):
            candidate += "/"
        found.append(prefix + escape(candidate))
    return found


def _complete(text: str, state: int) -> str | None:
    """readline callback: hand back one match per call, then None."""
    global _matches, _match_text
    if state == 0 or text != _match_text:
        _match_text = text
        _matches = path_candidates(text)
    return _matches[state] if state < len(_matches) else None


def install() -> bool:
    """Wire Tab completion into readline; False when readline is unavailable."""
    global _hooked
    if _readline is None:
        return False
    if not _hooked:
        _readline.set_completer(_complete)
        # A space is part of a path here, so it must not split the completed word.
        _readline.set_completer_delims("\t\n\"'")
        _readline.parse_and_bind("tab: complete")
        # Shell behaviour: the first Tab extends to the longest common prefix, the
        # second lists the candidates. Without this, readline only rings the bell
        # when the common prefix is already on the line.
        _readline.parse_and_bind("set show-all-if-unmodified on")
        _hooked = True
    return True


def remember(line: str) -> None:
    """Put an accepted path into the readline history (up-arrow recalls it)."""
    if _readline is None or not line.strip():
        return
    try:
        _readline.add_history(line)
    except ValueError:  # pragma: no cover - readline refuses an empty line
        pass
