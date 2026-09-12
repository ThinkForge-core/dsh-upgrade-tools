"""State snapshots and incompatible lists.

A snapshot is written BEFORE any destructive operation and holds everything
needed to restore the profile to its previous state: name, specifier, source,
version and whether the plugin is listed in ``dsh.profile.bundles``.

The incompatible file is the thing the user asked for separately: the list of
plugins that do not fit the chosen core version, suitable for a re-check with the
``recheck`` command (and for a later installation, should they become compatible).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .paths import state_dir

MARK_COMPATIBLE = "ok"
MARK_INCOMPATIBLE = "NO"
MARK_UNKNOWN = "??"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def read_json_file(path: Path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def snapshot_path() -> Path:
    return state_dir() / "snapshots"


def write_snapshot(profile, core_version: str | None, plugins: list[dict], *, note: str = "") -> dict:
    """Write a profile snapshot; return it as a dictionary."""
    payload = {
        "kind": "dsh-upgrade-tools/snapshot/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "profile": profile.name,
        "profileDir": str(profile.directory),
        "coreVersion": core_version,
        "bundles": profile.bundles,
        "plugins": plugins,
        "note": note,
    }
    json_path = snapshot_path() / f"{_stamp()}-{profile.name}.json"
    write_json(json_path, payload)
    payload["_jsonPath"] = str(json_path)

    md_path = json_path.with_suffix(".md")
    lines = [
        f"# Snapshot of profile `{profile.name}`",
        "",
        f"- Created: {payload['createdAt']}",
        f"- Core version: `{core_version}`",
        f"- Plugins: {len(plugins)}",
        f"- JSON: `{json_path}`",
        "",
        "| Plugin | Version | Source | Specifier | In bundles |",
        "|---|---|---|---|---|",
    ]
    for plugin in plugins:
        lines.append(
            "| `{name}` | {version} | {source} | `{spec}` | {in_bundles} |".format(
                name=plugin.get("name"),
                version=plugin.get("version") or "—",
                source=plugin.get("source") or "—",
                spec=plugin.get("spec") or "—",
                in_bundles="yes" if plugin.get("in_bundles") else "no",
            )
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload["_mdPath"] = str(md_path)
    return payload


def latest_snapshot() -> Path | None:
    """The most recent snapshot (or None)."""
    directory = snapshot_path()
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("*.json"))
    return files[-1] if files else None


def incompatible_path(core_version: str) -> tuple[Path, Path]:
    """Paths of the incompatible list files for a core version.

    The name is built by hand: ``Path.with_suffix`` would eat the ``.2`` in
    ``0.1.5-rc.2``.
    """
    base = state_dir() / f"incompatible-{core_version}"
    return base.parent / f"{base.name}.json", base.parent / f"{base.name}.md"


def write_incompatible(core_version: str, entries: list[dict], *, note: str = "") -> tuple[Path, Path]:
    """Write the incompatible list (json + md)."""
    json_path, md_path = incompatible_path(core_version)
    payload = {
        "kind": "dsh-upgrade-tools/incompatible/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "coreVersion": core_version,
        "count": len(entries),
        "note": note,
        "plugins": entries,
    }
    write_json(json_path, payload)

    lines = [
        f"# Incompatible plugins for core `{core_version}`",
        "",
        f"- Created: {payload['createdAt']}",
        f"- Plugins in the list: {len(entries)}",
        f"- Re-check with: `python3 dsh_upgrade.py recheck --file {json_path}`",
        "",
    ]
    if not entries:
        lines.append("Nothing is incompatible — every plugin can be installed on this core version.")
    else:
        lines += [
            "Status: `NO` — proven incompatibility; `??` — unconfirmed"
            " (no declared requirements on the DSH version).",
            "",
            "| Status | Plugin | Version | Specifier | Reason | Requirement |",
            "|---|---|---|---|---|---|",
        ]
        marks = {"incompatible": MARK_INCOMPATIBLE, "unknown": MARK_UNKNOWN,
                 "compatible": MARK_COMPATIBLE}
        for entry in entries:
            lines.append(
                "| {mark} | `{name}` | {version} | `{spec}` | {reason} | {requirement} |".format(
                    mark=marks.get(entry.get("status"), MARK_UNKNOWN),
                    name=entry.get("name"),
                    version=entry.get("version") or "—",
                    spec=entry.get("spec") or "—",
                    reason=(entry.get("reason") or "").replace("|", "\\|"),
                    requirement=(entry.get("requirement") or "—").replace("|", "\\|"),
                )
            )
        lines += [
            "",
            "## What to do about it",
            "",
            "1. If the plugin has a newer version, update it and check again:",
            "   `python3 dsh_upgrade.py check --core " + core_version + " --update`.",
            "2. A plugin with a local source (`file:`/`link:`/`workspace:`) has to be rebuilt",
            "   against the new core version in its own repository, and the artifact reinstalled:",
            "   npm may hold a DIFFERENT product under the same name, and this tool will not",
            "   install that one instead of the local artifact.",
            "   The manifest of the local artifact is stored in this list, so `recheck` can",
            "   evaluate it even without registry access; the artifact itself must be in place.",
            "3. If the plugin is not critical, leave it detached: no data is deleted, and the",
            "   plugin stays in this file.",
        ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def load_incompatible(path: Path) -> list[dict]:
    """Read the incompatible list."""
    payload = read_json_file(Path(path))
    return payload.get("plugins", []) if isinstance(payload, dict) else []


def latest_incompatible() -> Path | None:
    """The most recent incompatible list file (or None)."""
    directory = state_dir()
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("incompatible-*.json"), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None
