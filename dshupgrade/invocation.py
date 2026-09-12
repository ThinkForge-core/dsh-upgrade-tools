"""The exact command that reproduces a report — what the "run this next" hints say.

A report ends with hints ("the effective loader tree is printed in full by
``dsh_upgrade.py status --loader``"), and a bare ``dsh_upgrade.py`` is not a
command anybody can run: it assumes the reader is standing in the tool's
directory, with the same interpreter, and it silently drops the profile the
report was made for. The hint then answers nothing, which is exactly the moment
it is needed.

These helpers build the command worth pasting: the interpreter that is running,
the absolute path of this script, and ``--profile`` whenever the profile is not
the built-in default. The result is one string — no arguments to remember, no
directory to be in.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from . import config as config_mod


def script_path() -> str:
    """The ``dsh_upgrade.py`` that is running, as an absolute path.

    The script sits next to the package this module lives in, so its location is
    knowable without guessing at ``sys.argv``. If it is not there (the package was
    vendored somewhere else), the command falls back to the name of the script the
    user actually typed, and then to the bare name.
    """
    candidate = Path(__file__).resolve().parent.parent / "dsh_upgrade.py"
    if candidate.is_file():
        return str(candidate)
    typed = Path(sys.argv[0] or "")
    if typed.name.endswith(".py"):
        return str(typed)
    return "dsh_upgrade.py"


def interpreter() -> str:
    """The Python running this process; ``python3`` when there is no usable one."""
    if sys.executable:
        return sys.executable
    return shutil.which("python3") or "python3"


def default_profile() -> str:
    """The profile a command uses when ``--profile`` is not given."""
    return str(config_mod.DEFAULTS.get("profile") or "web")


def profile_flags(profile: str | None) -> list[str]:
    """``--profile NAME``, or nothing for the default profile.

    The default is left out on purpose: it is what a command without the flag
    already uses, and a shorter hint is a hint that gets read.
    """
    return ["--profile", profile] if profile and profile != default_profile() else []


def command(*flags: str, profile: str | None = None, raw: tuple = ()) -> str:
    """A copy-pasteable invocation of this tool, shell-quoted.

    ``flags`` are quoted, so a value with a space survives the copy. ``raw`` parts
    are appended verbatim — for placeholders the user has to fill in themselves
    (``--core <version>``), which quoting would turn into ``'<version>'``.
    """
    quoted = [shlex.quote(part) for part in
              (interpreter(), script_path(), *flags, *profile_flags(profile))]
    return " ".join([*quoted, *raw])


def loader_hint(profile: str | None = None) -> str:
    """The command that prints the effective loader tree."""
    return command("status", "--loader", profile=profile)
