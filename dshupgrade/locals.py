"""Local plugin sources: ``file:``, ``link:``, ``workspace:``, ``portal:``.

A hand-built plugin does not have to be on npm or GitHub: in the profile it is
recorded with a local specifier — a tarball (``file:…tgz``) or a directory link
(``link:/path/to/repository``). For such plugins the **registry must not be
queried by name**: npm may hold a different artifact under the same name (a
locally built version and the published one can carry different code and
different peer pins).

The source of truth is the artifact itself: a directory or a ``.tgz``. The
manifest and the code are read from it (for an archive — without unpacking, via
the stdlib ``tarfile``), so compatibility checks work even when the plugin is
not installed into the profile yet and even when it exists in no registry at all.
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from .compat import (
    CODE_SUFFIXES,
    MAX_FILE_BYTES,
    MAX_FILES,
    REPO_SKIP_PARTS,
    SKIP_PARTS,
    CodeSource,
    client_relative_names,
)

LOCAL_PREFIXES = ("file:", "link:", "workspace:", "portal:")
TARBALL_SUFFIXES = (".tgz", ".tar.gz", ".tar")


def is_local_spec(spec) -> bool:
    """Is this dependency specifier a local one?"""
    if not isinstance(spec, str):
        return False
    text = spec.strip()
    if any(text.startswith(prefix) for prefix in LOCAL_PREFIXES):
        return True
    return text.startswith("./") or text.startswith("../") or text.startswith("/")


def _split_spec(spec: str) -> tuple[str, str]:
    """Split a specifier into a prefix and a path."""
    text = spec.strip()
    for prefix in LOCAL_PREFIXES:
        if text.startswith(prefix):
            return prefix.rstrip(":"), text[len(prefix):].strip()
    return "", text


def _kind_of(path: Path) -> str:
    if path.is_dir():
        return "dir"
    if path.name.lower().endswith(TARBALL_SUFFIXES):
        return "tarball"
    return "other"


@dataclass
class LocalSource:
    """A resolved local plugin source."""

    spec: str
    prefix: str
    path: Path
    kind: str
    available: bool = False

    # ------------------------------------------------------------------ manifest
    def manifest(self) -> dict | None:
        """Artifact manifest: ``package.json`` from the directory or the archive."""
        if self.kind == "dir":
            return _read_json(self.path / "package.json")
        if self.kind == "tarball":
            return _tarball_manifest(self.path)
        return None

    def version(self) -> str | None:
        manifest = self.manifest()
        if isinstance(manifest, dict) and isinstance(manifest.get("version"), str):
            return manifest["version"]
        return None

    # ---------------------------------------------------------------------- code
    def code_source(self, manifest: dict | None = None) -> CodeSource | None:
        """Artifact code for the scans (checks 2 and 3)."""
        if self.kind == "dir":
            return CodeSource.from_directory(self.path, manifest, skip_parts=REPO_SKIP_PARTS)
        if self.kind == "tarball":
            return _tarball_code_source(self.path, manifest)
        return None

    # ------------------------------------------------------------------ display
    def kind_label(self) -> str:
        return {"dir": "directory", "tarball": "tarball", "other": "?"}.get(self.kind, "?")

    def describe(self) -> str:
        state = "" if self.available else " (NOT FOUND)"
        return f"{self.prefix or 'path'}:{self.path} — {self.kind_label()}{state}"

    def label(self) -> str:
        """"file·tgz" / "link·dir" — for the ``source`` column."""
        suffix = {"dir": "dir", "tarball": "tgz", "other": "?"}.get(self.kind, "?")
        return f"{self.prefix or 'path'}·{suffix}"

    def to_dict(self) -> dict:
        return {
            "spec": self.spec,
            "prefix": self.prefix,
            "path": str(self.path),
            "kind": self.kind,
            "available": self.available,
            "version": self.version() if self.available else None,
        }


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _strip_package_prefix(name: str) -> str | None:
    """Drop the ``package/`` prefix (the way npm packs) and return the inner path."""
    text = name.lstrip("./")
    if text in ("", "package"):
        return None
    if text.startswith("package/"):
        text = text[len("package/"):]
    return text or None


def _manifest_member(archive: tarfile.TarFile):
    """Pick the archive member that is the package's ``package.json``."""
    best = None
    for member in archive.getmembers():
        if not member.isfile():
            continue
        name = _strip_package_prefix(member.name)
        if name != "package.json":
            continue
        if best is None or len(member.name) < len(best.name):
            best = member
    return best


def _tarball_manifest(path: Path) -> dict | None:
    """Read ``package.json`` from an npm tarball without unpacking it."""
    try:
        with tarfile.open(path, "r:*") as archive:
            member = _manifest_member(archive)
            if member is None:
                return None
            handle = archive.extractfile(member)
            if handle is None:
                return None
            payload = json.loads(handle.read().decode("utf-8", "replace"))
    except (OSError, tarfile.TarError, ValueError, EOFError):
        return None
    return payload if isinstance(payload, dict) else None


def _tarball_code_source(path: Path, manifest: dict | None = None) -> CodeSource | None:
    """Collect the plugin code straight from the archive."""
    files: dict[str, str] = {}
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if len(files) >= MAX_FILES:
                    break
                if not member.isfile():
                    continue
                name = _strip_package_prefix(member.name)
                if name is None:
                    continue
                parts = name.split("/")
                if any(part in SKIP_PARTS or part.startswith(".") for part in parts[:-1]):
                    continue
                if not name.endswith(CODE_SUFFIXES):
                    continue
                if member.size > MAX_FILE_BYTES:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                files[name] = handle.read().decode("utf-8", "replace")
    except (OSError, tarfile.TarError, EOFError):
        return None
    source = CodeSource(files=files, origin=str(path))
    source.client_files = {name for name in client_relative_names(manifest) if name in files}
    return source


def from_path(path: Path, *, prefix: str = "file", spec: str | None = None) -> LocalSource:
    """Build a source from a known path (for example from ``localPath``)."""
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    return LocalSource(
        spec=spec or f"{prefix}:{resolved}",
        prefix=prefix,
        path=resolved,
        kind=_kind_of(resolved),
        available=resolved.exists(),
    )


def resolve(spec, profile_dir: Path | None = None) -> LocalSource | None:
    """Resolve a local specifier into a source; ``None`` means it is not local.

    Relative paths (``./x``, ``../x``) are taken from the profile directory —
    exactly the way pnpm understands them at install time.
    """
    if not isinstance(spec, str):
        return None
    text = spec.strip()
    if not text:
        return None
    prefix, raw = _split_spec(text)
    if not prefix and not (raw.startswith("./") or raw.startswith("../") or raw.startswith("/")):
        return None
    if not raw or raw.startswith("//"):
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        base = Path(profile_dir) if profile_dir else Path.cwd()
        candidate = base / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        pass
    return LocalSource(
        spec=text,
        prefix=prefix,
        path=candidate,
        kind=_kind_of(candidate),
        available=candidate.exists(),
    )


def manifest_for_entry(entry: dict, profile_dir: Path | None = None) -> dict | None:
    """Manifest of a local artifact from a snapshot/incompatible-list entry.

    Used by ``recheck``: a local plugin has no registry to take a manifest from,
    so the artifact itself is read — first by ``spec``, then by the saved
    ``localPath`` (the path may have been relative, or the file may have moved).
    """
    if not isinstance(entry, dict):
        return None
    source = resolve(entry.get("spec"), profile_dir)
    if source is not None and source.available:
        manifest = source.manifest()
        if manifest is not None:
            return manifest
    if entry.get("localPath"):
        fallback = from_path(Path(entry["localPath"]), prefix=entry.get("localPrefix") or "file")
        if fallback.available:
            return fallback.manifest()
    return None


def describe_missing(spec, profile_dir: Path | None = None) -> str | None:
    """Warning line when a local artifact is missing from disk."""
    source = resolve(spec, profile_dir)
    if source is None or source.available:
        return None
    return f"local source not found: {source.path} ({source.prefix or 'path'})"
