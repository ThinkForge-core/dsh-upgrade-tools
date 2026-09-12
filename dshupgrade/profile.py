"""Reading the profile and detaching/installing plugins.

Detaching and installing go through the same path as ``dsh plugin --profile …``:
that CLI command merely forwards its arguments to ``pnpm`` inside the profile
directory and then reconciles ``dsh.profile.bundles`` with the actually installed
state. The DSH daemon is not needed for this — the command works with the harness
switched off.

Plugin data is left alone: ``pnpm remove`` only deletes code from
``node_modules``. The data lives in ``~/.dsh`` (sessions, storages, plugin-owned
directories) and stays in place — see :func:`plugin_data_paths`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .checkout import run
from .paths import dsh_home, read_json

SOURCE_PREFIXES = ("file:", "link:", "workspace:", "portal:", "github:", "git+", "http:", "https:")


@dataclass
class PluginEntry:
    """A plugin recorded in the profile."""

    name: str
    spec: str
    source: str
    installed: bool
    version: str | None
    directory: str | None
    in_bundles: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "spec": self.spec,
            "source": self.source,
            "installed": self.installed,
            "version": self.version,
            "directory": self.directory,
            "in_bundles": self.in_bundles,
        }


@dataclass
class Profile:
    """Profile state."""

    directory: Path
    name: str
    dependencies: dict[str, str] = field(default_factory=dict)
    bundles: list[str] = field(default_factory=list)
    plugins: list[PluginEntry] = field(default_factory=list)

    @property
    def plugin_names(self) -> list[str]:
        return [plugin.name for plugin in self.plugins]

    def by_name(self, name: str) -> PluginEntry | None:
        for plugin in self.plugins:
            if plugin.name == name:
                return plugin
        return None


def source_kind(spec: str) -> str:
    """Classify a dependency specifier."""
    if not isinstance(spec, str):
        return "npm"
    for prefix in SOURCE_PREFIXES:
        if spec.startswith(prefix):
            if prefix == "file:":
                return "file"
            if prefix == "link:":
                return "link"
            if prefix in ("workspace:", "portal:"):
                return "file"  # a local directory/artifact, not the registry
            if prefix == "github:" or prefix.startswith("git"):
                return "github"
            return "other"
    # A relative or absolute path is a local source too: such a name must never
    # be looked up in the registry.
    if spec.startswith(("./", "../", "/")):
        return "file"
    return "npm"


def read_profile(profile_dir: Path) -> Profile:
    """Read the profile manifest and the state of every plugin."""
    directory = Path(profile_dir)
    manifest = read_json(directory / "package.json") or {}
    dependencies = manifest.get("dependencies") or {}
    dsh_section = manifest.get("dsh") or {}
    profile_section = dsh_section.get("profile") or {}
    bundles = profile_section.get("bundles") or []

    plugins: list[PluginEntry] = []
    for name, spec in sorted(dependencies.items()):
        plugin_dir = directory / "node_modules" / name
        installed = (plugin_dir / "package.json").is_file()
        version = None
        if installed:
            version = (read_json(plugin_dir / "package.json") or {}).get("version")
        plugins.append(
            PluginEntry(
                name=name,
                spec=spec,
                source=source_kind(spec),
                installed=installed,
                version=version,
                directory=str(plugin_dir) if installed else None,
                in_bundles=name in bundles,
            )
        )
    return Profile(directory=directory, name=directory.name, dependencies=dependencies,
                   bundles=list(bundles), plugins=plugins)


def dsh_binary() -> str | None:
    """Path to the ``dsh`` CLI (works with the harness switched off)."""
    return shutil.which("dsh")


def pnpm_binary() -> str | None:
    return shutil.which("pnpm")


def _plugin_command(profile: Profile, args: list[str]) -> list[str]:
    """The profile plugin management command."""
    binary = dsh_binary()
    if binary is not None:
        return [binary, "plugin", "--profile", profile.name, *args]
    pnpm = pnpm_binary()
    if pnpm is None:
        raise RuntimeError("neither dsh nor pnpm found in PATH — plugins cannot be managed")
    return [pnpm, *args]


_PNPM_HINT = (
    "\nHint: pnpm keeps its store outside the working directory, so under the "
    "workspace-write sandbox it fails (EROFS / unable to open database file). Run this "
    "command in a normal terminal or with danger-full-access."
)


def _fail(action: str, result) -> RuntimeError:
    detail = (result.stderr or result.stdout or "").strip()
    return RuntimeError(f"{action} failed (exit code {result.returncode}):\n{detail}{_PNPM_HINT}")


def remove_plugins(profile: Profile, names: list[str], *, dry_run: bool = False, log=print):
    """Detach plugins — the same as ``dsh plugin --profile <p> remove <names…>``."""
    if not names:
        log("  nothing to detach")
        return None
    command = _plugin_command(profile, ["remove", *names])
    log(f"  $ {' '.join(command)}")
    if dry_run:
        return None
    result = run(command, cwd=profile.directory)
    if result.returncode != 0:
        raise _fail("plugin removal", result)
    return result


def add_plugins(profile: Profile, specs: list[str], *, dry_run: bool = False, log=print):
    """Install plugins — the same as ``dsh plugin --profile <p> add <specs…>``."""
    if not specs:
        log("  nothing to install")
        return None
    command = _plugin_command(profile, ["add", *specs])
    log(f"  $ {' '.join(command)}")
    if dry_run:
        return None
    result = run(command, cwd=profile.directory)
    if result.returncode != 0:
        raise _fail("plugin installation", result)
    return result


# Storage locations shared by all plugins — worth showing once instead of
# repeating them on every plugin line.
SHARED_DATA = ("sessions", "storages", "skills", ".agent-presets", "settings.yaml",
               ".credentials.yaml", ".anonymous-user-id")


def plugin_data_paths(name: str) -> list[Path]:
    """Directories/files owned by one plugin.

    The detach operation does not touch them — ``pnpm remove`` only deletes code
    from the profile's ``node_modules``.
    """
    home = dsh_home()
    candidates = [home / name, home / name.split("/")[-1]]
    seen: list[Path] = []
    for path in candidates:
        if path.exists() and path not in seen:
            seen.append(path)
    return seen


def shared_data_paths() -> list[Path]:
    """Data locations shared by all plugins (sessions, storages, settings)."""
    home = dsh_home()
    return [home / item for item in SHARED_DATA if (home / item).exists()]


def data_summary(names: list[str]) -> dict[str, list[str]]:
    """A "where does whose data live" map — for the report and for reassurance."""
    summary: dict[str, list[str]] = {}
    for name in names:
        own = [str(path) for path in plugin_data_paths(name)]
        if own:
            summary[name] = own
    shared = [str(path) for path in shared_data_paths()]
    if shared:
        summary["*shared by all*"] = shared
    return summary
