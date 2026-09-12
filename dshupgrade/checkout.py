"""Checking out a core version and extracting facts from its tree.

The tool fetches whatever it needs by itself: when there is no checkout of the
target version, it clones the ``dsh-v<version>`` tag with ``--depth 1``. One
commit is enough — the comparison is file-by-file and never walks history, so
there is no reason to download a tag together with its parents. SSH is
deliberately not used: it can fail in sandboxed environments on
``/etc/ssh/ssh_config.d`` permissions, so the clone goes over HTTPS.

A clone lands in the configured checkout location; by default that is a temporary
directory that is removed with the process (see :mod:`dshupgrade.paths`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import paths
from .paths import checkout_dir

REPO_URL = "https://github.com/deepseek-ai/deepseek-harness.git"

#: History depth limit for checkouts. The comparison reads files, never history,
#: so a single commit is exactly what is needed — and the smallest download.
CLONE_DEPTH = "1"


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    """Run a process and return the result (no exception on a non-zero exit code)."""
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ensure_checkout(version: str, *, allow_clone: bool = True, log=print) -> Path | None:
    """Path to the version checkout; clones the tag when it is missing.

    An existing checkout is reused wherever it is found (the configured location
    or ``<DSH_HOME>/checkouts``), so a version already on disk is never fetched
    twice.
    """
    existing = paths.find_checkout(version)
    if existing is not None:
        if existing != checkout_dir(version):
            log(f"  using the existing checkout {existing} — nothing to download")
        return existing

    path = checkout_dir(version)
    if not allow_clone:
        log(f"  checkout {path.name} is missing and cloning is disabled (--no-clone)")
        return None
    if shutil.which("git") is None:
        log("  git not found — cannot obtain the version checkout")
        return None

    tag = f"dsh-v{version}"
    if paths.temporary_checkouts():
        log(f"  (kept in the temporary checkout directory {path.parent} — reused by the "
            "next run, cleared by the OS on reboot; --prune-checkouts removes it now)")
    log(f"  cloning {tag} (--depth {CLONE_DEPTH}) → {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    result = run(["git", "clone", f"--depth={CLONE_DEPTH}", "--branch", tag, REPO_URL, str(path)])
    if result.returncode != 0:
        log(f"  clone failed: {result.stderr.strip().splitlines()[-1:] or result.stderr.strip()}")
        return None
    return path


def package_inventory(checkout: Path) -> set[str]:
    """Names of all packages and applications of the monorepo."""
    names: set[str] = set()
    root = Path(checkout)
    for group in ("packages", "apps"):
        for manifest_path in (root / group).glob("*/*/package.json"):
            if "tests/fixtures" in str(manifest_path):
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = manifest.get("name")
            if isinstance(name, str):
                names.add(name)
    for manifest_path in (root / "apps").glob("*/package.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = manifest.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _monorepo_manifests(checkout: Path):
    """``(name, manifest)`` for every package and application of the monorepo."""
    root = Path(checkout)
    seen: set[Path] = set()
    for pattern in ("packages/*/*/package.json", "apps/*/package.json", "apps/*/*/package.json"):
        for manifest_path in sorted(root.glob(pattern)):
            if "tests/fixtures" in str(manifest_path) or manifest_path in seen:
                continue
            seen.add(manifest_path)
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = manifest.get("name")
            if isinstance(name, str):
                yield name, manifest


def client_rows(checkout: Path) -> set[str]:
    """Packages that declare ``dsh.client``: each is its own boot-graph row.

    Such a package has a client bundle the HOST serves and the module table owns
    exactly one instance of. A plugin bundle that inlines one of them installs a
    second copy of its module state — the host's row and the copy are different
    objects, and ``Symbol``/``instanceof``/singleton identity silently stops
    matching.
    """
    return {name for name, manifest in _monorepo_manifests(checkout)
            if isinstance(manifest.get("dsh"), dict)
            and isinstance(manifest["dsh"].get("client"), dict)}


#: The three classification patterns of the core's build-time purity gate, read
#: out of the target checkout so a version that changes them is followed instead
#: of guessed. The bodies are plain enough to move from JS to Python regexes.
_CLASSIFICATION_RE = {
    "inline_safe": re.compile(r"export const INLINE_SAFE\s*=\s*/(.+)/\s*$", re.M),
    "vendored": re.compile(r"const VENDORED_LIBRARY\s*=\s*/(.+)/\s*$", re.M),
    "generated_remote": re.compile(r"const GENERATED_REMOTE\s*=\s*/(.+)/\s*$", re.M),
}


def inline_classification(checkout: Path) -> tuple[str | None, str | None, str | None]:
    """``(INLINE_SAFE, VENDORED_LIBRARY, GENERATED_REMOTE)`` from the checkout.

    ``None`` means the file or the pattern was not found — the caller then falls
    back to the built-in classification rather than skipping the check.
    """
    path = Path(checkout) / "packages" / "client" / "tsdown.client.ts"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return (None, None, None)
    found = []
    for key in ("inline_safe", "vendored", "generated_remote"):
        match = _CLASSIFICATION_RE[key].search(text)
        found.append(match.group(1) if match else None)
    return (found[0], found[1], found[2])


def removed_packages(old_inventory: set[str], new_inventory: set[str]) -> list[str]:
    """Packages that used to exist and are gone (the main hard-breakage signal)."""
    return sorted(old_inventory - new_inventory)


_CORDIS_VENDOR_HINT = {"@deepseek-ai/cordis", "@deepseek-ai/schemastery"}


def vendor_names(checkout: Path) -> set[str]:
    """Packages from ``vendor/``: they are not in ``packages/``, so they look "removed"."""
    names: set[str] = set()
    for manifest_path in (Path(checkout) / "vendor").glob("*/package.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = manifest.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def session_format_version(checkout: Path) -> int | None:
    """``SESSION_FORMAT_VERSION`` of the target version."""
    path = Path(checkout) / "packages" / "core" / "session" / "src" / "types.ts"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"export const SESSION_FORMAT_VERSION\s*=\s*(\d+)", text)
    return int(match.group(1)) if match else None


_SEED_BLOCK_RE = re.compile(r"getStaticModules\(\)[^{]*\{(.*?)\n\}", re.S)
_SEED_KEY_RE = re.compile(r"^\s*'([^']+)':", re.M)
_PRELOADED_RE = re.compile(r"PRELOADED_CLIENT_EXTERNALS\s*=\s*\[(.*?)\]", re.S)
_STRING_RE = re.compile(r"'([^']+)'")


def client_seed_words(checkout: Path) -> set[str]:
    """Seed words of the browser module table (``getStaticModules``)."""
    path = Path(checkout) / "packages" / "client" / "web" / "src" / "seed.ts"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    block = _SEED_BLOCK_RE.search(text)
    if block is None:
        return set()
    return set(_SEED_KEY_RE.findall(block.group(1)))


def preloaded_client_externals(checkout: Path) -> set[str]:
    """``PRELOADED_CLIENT_EXTERNALS`` — the client external modules preloaded up front."""
    path = Path(checkout) / "packages" / "client" / "web" / "src" / "platform.ts"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    block = _PRELOADED_RE.search(text)
    return set(_STRING_RE.findall(block.group(1))) if block else set()


def agent_notes(checkout: Path, since_days: int = 60) -> list[str]:
    """Recent "why" implementation notes (file names without dates)."""
    notes_dir = Path(checkout) / ".agents" / "notes" / "implemented"
    if not notes_dir.is_dir():
        return []
    notes = sorted(path.name for path in notes_dir.rglob("*.md"))
    return notes
