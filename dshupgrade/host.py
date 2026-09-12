"""Core inventory and host package selection.

Mirrors the marketplace policy (``dshmarket``, https://github.com/dsh-market/dsh-market):
the installed ``@deepseek-ai/dsh*`` packages from
the core's own ``node_modules`` count as host packages. Passing the real
directory here is mandatory — otherwise only the curated list of 22 names
remains, and almost every plugin gets an ``unknown`` verdict (the
``dshHostInfo()`` of ``dshmarket`` can return undefined, so the real directory is
preferred over the seed).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Curated fallback seed: used when the core directory cannot be found.
CURATED_HOST_SEED: tuple[str, ...] = (
    "@deepseek-ai/dsh",
    "@deepseek-ai/dsh-base",
    "@deepseek-ai/dsh-web-app",
    "@deepseek-ai/dsh-headless",
    "@deepseek-ai/dsh-app-boot",
    "@deepseek-ai/dsh-home-paths",
    "@deepseek-ai/dsh-launch-environment",
    "@deepseek-ai/dsh-cmdline",
    "@deepseek-ai/dsh-tools",
    "@deepseek-ai/dsh-llm",
    "@deepseek-ai/dsh-system-prompt",
    "@deepseek-ai/dsh-attachment",
    "@deepseek-ai/dsh-agent",
    "@deepseek-ai/dsh-agent-loop",
    "@deepseek-ai/dsh-session",
    "@deepseek-ai/dsh-subagent",
    "@deepseek-ai/cordis",
    "@deepseek-ai/cordis-plugin-loader",
    "@deepseek-ai/cordis-plugin-include",
    "@deepseek-ai/cordis-plugin-hmr",
    "@deepseek-ai/cordis-plugin-timer",
    "@deepseek-ai/cordis-plugin-group",
)

_HOST_PEER_RE = re.compile(r"^@deepseek-ai/dsh(?:-|$)")


def host_inventory(install_dir: Path | None) -> set[str]:
    """Host package names: the contents of ``node_modules/@deepseek-ai`` of the installation."""
    names = set(CURATED_HOST_SEED)
    if install_dir is None:
        return names
    scope = Path(install_dir) / "node_modules" / "@deepseek-ai"
    try:
        for entry in scope.iterdir():
            if entry.name.startswith(("dsh", "cordis")):
                names.add(f"@deepseek-ai/{entry.name}")
    except OSError:
        pass
    manifest = None
    try:
        manifest = json.loads((Path(install_dir) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
        names.add(manifest["name"])
    return names


def installed_client_rows(install_dir: Path | None) -> set[str]:
    """Installed packages that declare ``dsh.client`` — the running host's rows.

    Used when the target IS the installed core: the installation carries the real
    declarations, while a checkout may not even be present. Reading them here
    keeps the inline-purity check working fully offline.
    """
    rows: set[str] = set()
    if install_dir is None:
        return rows
    scope = Path(install_dir) / "node_modules" / "@deepseek-ai"
    try:
        entries = sorted(scope.iterdir())
    except OSError:
        return rows
    for entry in entries:
        manifest_path = entry / "package.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        section = manifest.get("dsh")
        if isinstance(section, dict) and isinstance(section.get("client"), dict):
            name = manifest.get("name")
            if isinstance(name, str):
                rows.add(name)
    return rows


def host_peer_names(manifest: dict, host_packages: set[str]) -> dict[str, str]:
    """peerDependencies that the marketplace treats as host declarations.

    The filter is the same as in ``deriveHostCompatibility``: the name must be in
    the host inventory and start with ``@deepseek-ai/dsh``.
    """
    peers = manifest.get("peerDependencies") or {}
    if not isinstance(peers, dict):
        return {}
    return {
        name: value
        for name, value in peers.items()
        if isinstance(name, str) and isinstance(value, str)
        and name in host_packages and _HOST_PEER_RE.match(name)
    }
