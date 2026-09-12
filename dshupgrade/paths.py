"""Path resolution: DSH home, profile directory, installed core, checkouts.

The core directory is located independently of the plugins: by resolving the
``dsh`` binary from PATH (symlink → ``lib/bin.js`` → the package root). No
marketplace plugin is needed for that — which matters, because during an upgrade
it is detached together with all the other plugins.

Version checkouts (see :func:`checkouts_root`) are a means to an end — the
comparison — so by default they are cloned under the **system temporary
directory** (``/tmp``): a version is downloaded once and reused by later runs,
and the operating system clears the directory on reboot, so nothing permanent is
accumulated. A directory chosen in the settings is used instead (and kept).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


def dsh_home() -> Path:
    """DSH data root: ``$DSH_HOME`` or ``~/.dsh``."""
    env = os.environ.get("DSH_HOME", "").strip()
    return Path(env).expanduser().resolve() if env else Path.home() / ".dsh"


def profile_dir(name: str) -> Path:
    """Profile directory: ``<DSH_HOME>/profiles/<name>``."""
    if not name:
        raise ValueError("profile_dir: a profile name is required")
    return dsh_home() / "profiles" / name


def read_json(path: Path):
    """Read JSON or return None."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def package_version(directory: Path) -> str | None:
    """Package version from its package.json, or None."""
    manifest = read_json(Path(directory) / "package.json")
    if isinstance(manifest, dict) and isinstance(manifest.get("version"), str):
        return manifest["version"]
    return None


def core_install_dir() -> Path | None:
    """Directory of the installed ``@deepseek-ai/dsh`` core, or None."""
    override = os.environ.get("DSH_INSTALL_DIR", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if (candidate / "package.json").is_file():
            return candidate.resolve()

    found = shutil.which("dsh")
    if found:
        real = Path(found).resolve()  # .../@deepseek-ai/dsh/lib/bin.js
        candidate = real.parent.parent
        if (candidate / "package.json").is_file():
            return candidate

    # A last resort: an nvm install whose bin directory is not on PATH. Which node
    # version holds the core is not knowable in advance, so every installed one is
    # tried, newest first.
    node_versions = Path.home() / ".nvm" / "versions" / "node"
    if node_versions.is_dir():
        for node_dir in sorted(node_versions.glob("v*"), key=_node_version_key, reverse=True):
            candidate = node_dir / "lib" / "node_modules" / "@deepseek-ai" / "dsh"
            if (candidate / "package.json").is_file():
                return candidate
    return None


def _node_version_key(directory: Path) -> tuple:
    """Order node version directories numerically, so ``v24`` sorts after ``v9``."""
    return tuple(int(part) if part.isdigit() else -1
                 for part in directory.name.lstrip("v").split("."))


def core_version(install_dir: Path | None = None) -> str | None:
    """Version of the installed core (or None)."""
    directory = install_dir if install_dir is not None else core_install_dir()
    return None if directory is None else package_version(directory)


#: Checkout locations. ``temp`` clones under the system temporary directory
#: (see :func:`temp_checkouts_root`); ``keep`` is ``<DSH_HOME>/checkouts``; anything
#: else is a directory the user chose, kept between runs.
TEMP = "temp"
KEEP = "keep"

#: Name of the temporary checkout root — one per user, under the system temp dir.
TEMP_ROOT_NAME = "dsh-upgrade-checkouts"

_checkout_setting: str | None = None
_fallback_root: Path | None = None


def normalize_checkouts(value) -> str:
    """One of ``temp`` / ``keep`` / an absolute path, from whatever was typed."""
    text = ("" if value is None else str(value)).strip()
    if not text:
        return TEMP
    lowered = text.lower()
    if lowered in ("temp", "tmp", "/tmp"):
        return TEMP
    if lowered in ("keep", "home"):
        return KEEP
    return str(Path(text).expanduser().resolve())


def set_checkouts_setting(value=None) -> str:
    """Pin the checkout location for this run (empty/None → the environment decides)."""
    global _checkout_setting
    text = "" if value is None else str(value).strip()
    _checkout_setting = normalize_checkouts(text) if text else None
    return checkouts_setting()


def checkouts_setting() -> str:
    """The configured location: the pinned one, else ``$DSH_CHECKOUTS_ROOT``, else temp."""
    if _checkout_setting:
        return _checkout_setting
    env = os.environ.get("DSH_CHECKOUTS_ROOT", "").strip()
    return normalize_checkouts(env) if env else TEMP


def checkouts_root() -> Path:
    """The directory checkouts are cloned into.

    A version checkout is a means to an end (the comparison), not a data store, so
    the default is a directory **under the system temporary directory**: the OS
    clears it on reboot, and until then the next run reuses what is already there
    instead of downloading the same tag again. A directory set in the settings (or
    by ``--checkouts``) is used instead.
    """
    setting = checkouts_setting()
    if setting == TEMP:
        return _temp_checkouts_root()
    if setting == KEEP:
        return (dsh_home() / "checkouts").resolve()
    return Path(setting)


def temp_checkouts_root() -> Path:
    """Where the temporary checkouts live: ``<tmp>/dsh-upgrade-checkouts-<uid>``.

    The path is derived, not random: it must be the SAME directory on the next run,
    otherwise every run would download the same tag again. A per-user suffix keeps
    a shared ``/tmp`` clean.
    """
    base = Path(tempfile.gettempdir())
    uid = os.getuid() if hasattr(os, "getuid") else None
    name = f"{TEMP_ROOT_NAME}-{uid}" if uid is not None else TEMP_ROOT_NAME
    return base / name


def _temp_checkouts_root() -> Path:
    """The temporary root: created on first use, reused until the OS clears it."""
    global _fallback_root
    root = temp_checkouts_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError:
        # A read-only or otherwise unwritable temp directory (a hardened sandbox):
        # a private directory of our own is the only option left.
        if _fallback_root is None or not _fallback_root.is_dir():
            _fallback_root = Path(tempfile.mkdtemp(prefix="dsh-upgrade-checkouts-"))
        return _fallback_root


def temporary_checkouts() -> bool:
    """True when the checkouts live in the system temporary directory."""
    return checkouts_setting() == TEMP


def prune_checkouts() -> list[Path]:
    """Delete the temporary checkouts; returns the directories actually removed.

    The tool never deletes them by itself: they are reused between runs and the
    operating system clears the temporary directory on reboot. This is the explicit
    "remove them now" operation behind ``--prune-checkouts`` and the settings screen.
    """
    global _fallback_root
    removed: list[Path] = []
    for root in (temp_checkouts_root(), _fallback_root):
        if root is None or not root.is_dir():
            continue
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            removed.append(root)
    _fallback_root = None
    return removed


def checkout_roots() -> list[Path]:
    """Where an existing checkout may be found, most specific first.

    The configured location comes first, then ``<DSH_HOME>/checkouts`` — the
    long-standing default — so a checkout downloaded by an earlier run (or cloned
    by hand) is reused instead of fetched again. Nothing is created here.
    """
    setting = checkouts_setting()
    roots: list[Path] = []
    if setting == TEMP:
        roots.append(temp_checkouts_root())
        if _fallback_root is not None:
            roots.append(_fallback_root)
    elif setting != KEEP:
        roots.append(Path(setting))
    roots.append((dsh_home() / "checkouts").resolve())
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def checkout_dir(version: str) -> Path:
    """Where the checkout of ``version`` belongs: ``<root>/deepseek-harness-<version>``."""
    return checkouts_root() / f"deepseek-harness-{version}"


def find_checkout(version: str) -> Path | None:
    """An existing checkout of ``version`` in any known location, or None."""
    for root in checkout_roots():
        candidate = root / f"deepseek-harness-{version}"
        if (candidate / "packages").is_dir():
            return candidate
    return None


def state_dir() -> Path:
    """Tool state directory (snapshots, incompatible lists, cache)."""
    env = os.environ.get("DSH_UPGRADE_STATE", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "state"


def cache_dir() -> Path:
    """Cache directory for network responses."""
    path = state_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path
