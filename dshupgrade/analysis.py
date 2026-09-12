"""Analysis: what happens to the plugins when moving to a chosen core version.

Brings together the target checkout inventory, plugin declarations, hard links
and client bundles. It also works with DSH switched off: the only external
dependency is the network (the npm registry) and, when needed, ``git`` for the
checkout.

Plugins with a local source (``file:``/``link:``/``workspace:``) are checked
against the ARTIFACT ITSELF — a tarball or a directory (see
:mod:`dshupgrade.locals`) — and not against the registry: npm may hold a
different artifact under the same name, and a verdict based on it would be a
verdict about somebody else's code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from . import checkout as checkout_mod
from . import codes
from . import locals as locals_mod
from . import registry
from . import semver
from .compat import (
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    BUILTIN_PLATFORM_MODULES,
    CodeSource,
    InlinePolicy,
    accepted,
    compile_classification,
    declarations_for,
    evaluate,
    external_base_names,
    scan_client_declaration,
    scan_client_modules,
    scan_client_registrations,
    scan_inline_purity,
    scan_removed_packages,
)
from .host import host_inventory, installed_client_rows
from .paths import core_install_dir, core_version as installed_core_version, read_json
from .profile import Profile


@dataclass
class Analysis:
    """Analysis result for one target core version."""

    target: str
    current_core: str | None
    checkout: Path | None
    profile_dir: Path | None = None
    host_packages: set[str] = field(default_factory=set)
    removed: list[str] = field(default_factory=list)
    vendor: set[str] = field(default_factory=set)
    seed_words: set[str] = field(default_factory=set)
    preloaded: set[str] = field(default_factory=set)
    inline_policy: InlinePolicy | None = None
    session_format: int | None = None
    #: Why the target version itself is out of the tool's reach, when it is.
    core_conflict: str | None = None
    #: Whether the registry was queried for newer plugin versions (``--update``).
    #: Without it the ``latest`` column is empty for lack of data, not because
    #: every plugin is current, and the report has to say which of the two it is.
    checked_updates: bool = False
    plugins: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        result = {STATUS_COMPATIBLE: 0, STATUS_INCOMPATIBLE: 0, STATUS_UNKNOWN: 0,
                  STATUS_VERIFIED: 0}
        for plugin in self.plugins:
            result[plugin["status"]] = result.get(plugin["status"], 0) + 1
        return result

    def incompatible(self) -> list[dict]:
        return [p for p in self.plugins if p["status"] == STATUS_INCOMPATIBLE]

    def local_plugins(self) -> list[dict]:
        return [p for p in self.plugins if p.get("local")]


def _manifest_for(name: str, spec: str, version: str | None, *, offline: bool) -> dict | None:
    """Plugin manifest: from npm, by name and version."""
    try:
        if version is not None:
            found = registry.manifest(name, version, offline=offline)
            if found is not None:
                return found
        return registry.manifest(name, None, offline=offline)
    except RuntimeError:
        return None


def _latest_npm_version(name: str, *, offline: bool) -> str | None:
    try:
        return registry.dist_tags(name, offline=offline).get("latest")
    except RuntimeError:
        return None


def core_conflict(current: str | None, target: str) -> str | None:
    """Why an upgrade cannot reach ``target``, or None when it can.

    The session format is versioned monotonically (``SESSION_FORMAT_VERSION``), so
    a core older than the installed one is out of reach for an upgrade: the tool
    can compare plugins against it, but nothing can move the installation down to
    it. No plugin selection changes that, which is what makes it a fact about the
    target core rather than about a plugin.
    """
    if current is None or current == target:
        return None
    if semver.parse_version(current) is None or semver.parse_version(target) is None:
        return None
    if semver.compare_version(target, current) < 0:
        return (f"target {target} is older than the installed {current}; "
                "an upgrade cannot reach it")
    return None


def apply_core_block(analysis: Analysis) -> None:
    """Mark the plugins of a profile whose target core is out of reach.

    ``blocks_core_upgrade`` answers one question — "if the pipeline runs, does it
    stop?" — and a plugin-level incompatibility never stops it: the incompatible
    plugins are held back and the rest proceeds. A core-level conflict (see
    :func:`core_conflict`) is different: no plugin selection avoids it, so every
    plugin that is not compatible carries the mark.
    """
    for entry in analysis.plugins:
        entry["blocks_core_upgrade"] = bool(
            analysis.core_conflict and not accepted(entry.get("status")))


def apply_runtime_verification(analysis: Analysis, verified) -> list[dict]:
    """Grade the entries whose installed copy was proven at runtime.

    A runtime verdict is evidence about ONE core version, so the caller passes the
    names only when the probed core is the target of this analysis; for any other
    target the set is empty.

    The upgrade never hides a finding: a proven incompatibility keeps its verdict, and
    an unconfirmed entry is graded only when its code checks actually ran and came back
    clean — "nothing declared" alone is not evidence. An input gap (a manifest that
    could not be read, a local artifact that is gone) leaves the entry unconfirmed too.
    """
    graded = []
    for entry in analysis.plugins:
        if entry["name"] not in verified:
            continue
        if entry.get("status") == STATUS_INCOMPATIBLE:
            continue
        if entry.get("status") == STATUS_UNKNOWN and not entry.get("code_clean"):
            continue
        previous = entry["status"]
        entry["declared_status"] = previous
        entry["status"] = STATUS_VERIFIED
        entry["runtime"] = "verified"
        if not entry.get("reason"):
            entry["reason"] = "verified at runtime on this core"
        elif previous == STATUS_UNKNOWN:
            entry["reason"] = "declares no DSH version; verified at runtime on this core"
        # Anything else keeps its reason: an empirical verdict names the stricter
        # declaration it overrode, and that fact must survive the upgrade.
        graded.append(entry)
    return graded


def analyse(
    profile: Profile,
    target: str,
    *,
    offline: bool = False,
    allow_clone: bool = True,
    check_updates: bool = False,
    since: str | None = None,
    log=print,
) -> Analysis:
    """Check every plugin of the profile against the core version ``target``.

    ``since`` overrides the baseline of the "packages that disappeared" scan.
    By default the baseline is the INSTALLED core, which is self-referential when
    the target is the installed core — nothing has been removed from itself. For
    a plugin written against an older release, pass that release as ``since`` to
    catch references to packages the target no longer ships.
    """
    current = installed_core_version()
    log(f"Current core: {current or 'not found'}; target: {target}")

    target_checkout = checkout_mod.ensure_checkout(target, allow_clone=allow_clone, log=log)
    if target_checkout is None:
        log("  target checkout unavailable — the check is limited to declarations "
            "(no inventory, no bundles)")

    # Inventory of the target: when the target version is already installed, take
    # the real installation (the truth); otherwise take the checkout inventory.
    if current == target:
        install_dir = core_install_dir()
        host_packages = host_inventory(install_dir)
        # ``host_inventory`` mirrors the market policy and keeps only dsh*/cordis*
        # names, while the installation also ships the vendor packages
        # (schemastery, cosmokit). Without them a plugin that references one looks
        # like it links to a package that has been removed.
        if target_checkout is not None:
            host_packages = host_packages | checkout_mod.vendor_names(target_checkout)
        source = f"installed core {install_dir}"
    elif target_checkout is not None:
        host_packages = checkout_mod.package_inventory(target_checkout) | checkout_mod.vendor_names(target_checkout)
        source = f"checkout {target_checkout.name}"
    else:
        # No target checkout: the closest approximation is the inventory of the
        # CURRENT core (package names change little between neighbouring versions).
        current_install = core_install_dir()
        if current_install is not None:
            host_packages = host_inventory(current_install)
            source = (f"inventory of the current core {current_install.name} "
                      "(approximation, no target checkout)")
        else:
            host_packages = host_inventory(None)
            source = "curated fallback list (22 names)"

    vendor = checkout_mod.vendor_names(target_checkout) if target_checkout else set()
    # Everything the target version still ships. The INSTALLED tree is the truth for
    # the RUNNING core, but it does not hold every monorepo package: a platform
    # module bundled into the shell (``dsh-client-ui-primitives``) has no
    # ``node_modules`` entry of its own, so an installed-only inventory would call it
    # "removed" and reject a bundle that legitimately requires a seed word.
    # ``host_packages`` is deliberately left alone here: it drives the market's peer
    # policy, and broadening it would change which peer ranges count as host
    # declarations.
    target_present = set(host_packages) | vendor
    if target_checkout is not None:
        target_present |= checkout_mod.package_inventory(target_checkout)
    analysis = Analysis(
        target=target,
        current_core=current,
        checkout=target_checkout,
        profile_dir=Path(profile.directory) if profile.directory else None,
        host_packages=host_packages,
        vendor=vendor,
        core_conflict=core_conflict(current, target),
        checked_updates=check_updates,
    )
    analysis.notes.append(f"host inventory: {source}, names: {len(host_packages)}")

    if target_checkout is not None:
        analysis.seed_words = checkout_mod.client_seed_words(target_checkout)
        analysis.preloaded = checkout_mod.preloaded_client_externals(target_checkout)
        analysis.session_format = checkout_mod.session_format_version(target_checkout)
        analysis.notes.append(
            f"browser seed words: {len(analysis.seed_words)}; "
            f"SESSION_FORMAT_VERSION: {analysis.session_format}"
        )

    # What the target version lets a client bundle inline. The inline-purity check
    # needs two facts: which packages are module-table rows, and which few wire
    # layers stay inline by design. When the target IS the installed core, the
    # installation itself is authoritative and the check works with no checkout and
    # no network at all.
    install_dir_for_rows = core_install_dir() if current == target else None
    client_rows = installed_client_rows(install_dir_for_rows) if install_dir_for_rows else set()
    rows_source = f"installed core {install_dir_for_rows}" if client_rows else ""
    if not client_rows and target_checkout is not None:
        client_rows = checkout_mod.client_rows(target_checkout)
        rows_source = f"checkout {target_checkout.name}"
    if target_checkout is not None:
        inline_safe, vendored_re, generated_remote = checkout_mod.inline_classification(target_checkout)
        classification_source = f"checkout {target_checkout.name}"
    else:
        inline_safe, vendored_re, generated_remote = (None, None, None)
        classification_source = "built-in 0.1.5-rc.2 classification (no checkout)"
    compiled = compile_classification(inline_safe, vendored_re, generated_remote)
    analysis.inline_policy = InlinePolicy(
        platform=analysis.seed_words or set(BUILTIN_PLATFORM_MODULES),
        client_rows=client_rows,
        inline_safe=compiled[0],
        vendored=compiled[1],
        generated_remote=compiled[2],
        source=f"{rows_source or 'no client rows'} / {classification_source}",
    )
    if client_rows:
        analysis.notes.append(
            f"client rows (packages the host loads itself): {len(client_rows)}; "
            f"inline classification: {classification_source}"
        )

    # Removed packages: the baseline core inventory against the target inventory.
    # The baseline is the installed core unless ``since`` names another version —
    # without that override the scan is self-referential when target == installed
    # and reports nothing at all.
    if target_checkout is not None:
        current_install = core_install_dir()
        old_inventory: set[str] = set()
        if since is not None:
            since_checkout = checkout_mod.ensure_checkout(since, allow_clone=allow_clone, log=log)
            if since_checkout is not None:
                old_inventory = (checkout_mod.package_inventory(since_checkout)
                                 | checkout_mod.vendor_names(since_checkout))
                analysis.notes.append(f"removed packages measured against {since} (--since)")
            else:
                analysis.notes.append(f"baseline {since} (--since) is unavailable — "
                                      "the removed-package scan was skipped")
        elif current_install is not None:
            old_inventory = host_inventory(current_install)
        elif current is not None:
            old_checkout = checkout_mod.ensure_checkout(current, allow_clone=False, log=lambda *_: None)
            if old_checkout is not None:
                old_inventory = checkout_mod.package_inventory(old_checkout)
        if old_inventory:
            removed = set(checkout_mod.removed_packages(old_inventory, target_present))
            analysis.removed = sorted(removed)
            # The full list is printed by the report (it wraps), so no count note here.

    for plugin in profile.plugins:
        entry = _analyse_plugin(plugin, analysis, offline=offline, check_updates=check_updates,
                                profile_dir=profile.directory)
        analysis.plugins.append(entry)

    apply_core_block(analysis)

    local_count = len(analysis.local_plugins())
    if local_count:
        analysis.notes.append(
            f"local sources: {local_count} (checked against their own artifact, "
            "the registry is not used)"
        )

    return analysis


def _resolve_local(plugin, profile_dir: Path | None):
    """The local source of a plugin (or None for npm/github)."""
    return locals_mod.resolve(getattr(plugin, "spec", None), profile_dir)


def reason_code_for(entry: dict, *, basis: str | None = None) -> str | None:
    """Stable identifier for the class of failure an entry reports.

    Read from the finding lists rather than from the reason text, so a reworded
    reason keeps its identifier. ``basis`` is the verdict basis of
    :func:`dshupgrade.compat.evaluate`: a manifest that declares nothing is the
    only unknown that names a finding of its own — an unreadable manifest is a
    gap in the input, not a declaration.
    """
    candidates: list[str] = []
    if entry.get("declaration_hits"):
        candidates.append(codes.DECLARATION_INTEGRITY_FAILURE)
    if entry.get("registration_hits"):
        candidates.append(codes.DUPLICATE_FACTORY_REGISTRATION)
    if entry.get("removed_hits"):
        candidates.append(codes.REMOVED_PACKAGE_REQUIRED)
    if entry.get("client_hits"):
        candidates.append(codes.BROWSER_MODULE_TABLE_MISS)
    if entry.get("inline_hits"):
        candidates.append(codes.INLINE_PURITY_VIOLATION)
    failing = {declaration.get("kind") for declaration in entry.get("declarations") or []
               if declaration.get("result") is False}
    if "peer" in failing:
        candidates.append(codes.PEER_RANGE_MISMATCH)
    if failing & {"engine", "engine-nested"}:
        candidates.append(codes.ENGINES_DSH_MISMATCH)
    if entry.get("status") == STATUS_UNKNOWN and basis == "undeclared":
        candidates.append(codes.UNKNOWN_DECLARATIONS)
    return codes.primary(candidates)


def _analyse_plugin(plugin, analysis: Analysis, *, offline: bool, check_updates: bool,
                    profile_dir: Path | None = None) -> dict:
    """Checks for a single plugin."""
    local = _resolve_local(plugin, profile_dir)
    installed_dir = Path(plugin.directory) if plugin.directory else None
    installed_manifest = read_json(installed_dir / "package.json") if installed_dir is not None else None

    # Manifest: for a local plugin it comes from the artifact itself (a tarball or
    # a directory), and only when the artifact is missing — from the installed
    # copy. The registry is never queried for a local source: it may hold a
    # different product under the same name.
    manifest = None
    if local is not None and local.available:
        manifest = local.manifest()
    if manifest is None:
        manifest = installed_manifest
    # The registry is used only for non-local sources: npm may hold another
    # artifact under the name of a hand-built plugin, and its manifest must not
    # replace the local one.
    if manifest is None and local is None and not locals_mod.is_local_spec(plugin.spec):
        manifest = _manifest_for(plugin.name, plugin.spec, plugin.version, offline=offline)

    local_version = None
    if local is not None and isinstance(manifest, dict) and local.available:
        local_version = manifest.get("version") if isinstance(manifest.get("version"), str) else None

    entry = {
        "name": plugin.name,
        "spec": plugin.spec,
        "source": plugin.source,
        "sourceLabel": local.label() if local is not None else plugin.source,
        "version": plugin.version,
        "installed": plugin.installed,
        "in_bundles": plugin.in_bundles,
        "local": local.to_dict() if local is not None else None,
        "localVersion": local_version,
        "installable": not (local is not None and not local.available),
        "status": STATUS_UNKNOWN,
        "reason": "",
        "reason_code": None,
        "requirement": None,
        "declarations": [],
        "removed_hits": [],
        "client_hits": [],
        "registration_hits": [],
        "declaration_hits": [],
        "inline_hits": [],
        "code_checked": False,
        "code_clean": False,
        "code_origin": None,
        "empirical": False,
        "latest": None,
        "latest_status": None,
        "npmLatest": None,
        "npmLatestStatus": None,
        "recommended": plugin.spec,
        "manifest": manifest,
    }

    if local is not None and not local.available and manifest is None:
        entry["reason"] = (f"local source not found: {local.path} — "
                           "neither an artifact nor an installed copy")
        return entry

    if manifest is None:
        entry["reason"] = "manifest unavailable (neither locally nor in the registry)"
        return entry

    verdict = evaluate(analysis.target, declarations_for(manifest, analysis.host_packages))
    entry["status"] = verdict.status
    entry["requirement"] = verdict.requirement
    entry["reason"] = verdict.reason()
    entry["declarations"] = [
        {
            "kind": d.kind,
            "package": d.package,
            "range": d.range,
            "source": d.source,
            "result": d.result,
            "direction": d.direction,
        }
        for d in verdict.declarations
    ]

    if local is not None and not local.available:
        entry["reason"] = (
            f"local source not found: {local.path} (reinstallation impossible); "
            + (entry["reason"] or "")
        ).strip("; ")

    # Code for the scans: for a local plugin it is the artifact, otherwise the
    # installed copy. ``require_bundle`` distinguishes a packed artifact (whose
    # declared client bundle must exist) from a repository link (which may not be
    # built yet).
    code_source = None
    require_bundle = False
    if local is not None and local.available:
        code_source = local.code_source(manifest)
        require_bundle = local.kind == "tarball"
        if code_source is not None and not code_source.empty:
            entry["code_origin"] = local.describe()
    if (code_source is None or code_source.empty) and installed_dir is not None:
        code_source = CodeSource.from_directory(installed_dir, manifest)
        require_bundle = True
        if not code_source.empty:
            entry["code_origin"] = f"installed copy {installed_dir}"

    # Code checks run whenever there is code to take them from.
    if code_source is not None and not code_source.empty:
        entry["code_checked"] = True
        reasons: list[str] = []

        # 1. The dsh.client declaration and the bundle it promises. Independent of
        #    the target version: the host composes the graph from this declaration
        #    and then fetches the file, so a broken declaration fails on ANY core.
        entry["declaration_hits"] = scan_client_declaration(
            manifest, code_source, package_name=entry["name"], require_bundle=require_bundle,
        )
        if entry["declaration_hits"]:
            reasons.append("dsh.client declaration is not loadable: " + "; ".join(
                hit["message"] for hit in entry["declaration_hits"][:2]
            ))

        # 2. Inline purity against the TARGET version's rules: a bundle that inlines
        #    a module the host loads as its own row carries a second copy of that
        #    module's state, and the duplicate does not fail — it silently stops
        #    matching by Symbol/instanceof/singleton.
        if analysis.inline_policy is not None:
            entry["inline_hits"] = scan_inline_purity(
                code_source, analysis.inline_policy, entry["name"],
                external_base_names(manifest),
            )
            if entry["inline_hits"]:
                reasons.append(
                    "client bundle inlines a package that must come from the module table: "
                    + ", ".join(hit["package"] for hit in entry["inline_hits"][:3])
                )

        if analysis.removed:
            entry["removed_hits"] = scan_removed_packages(code_source, set(analysis.removed))
            if entry["removed_hits"]:
                reasons.append("hard link to a removed package: " + ", ".join(
                    hit["package"] for hit in entry["removed_hits"][:3]
                ))
        if analysis.seed_words:
            known = analysis.host_packages - analysis.vendor
            entry["client_hits"] = scan_client_modules(code_source, analysis.seed_words, known)
            if entry["client_hits"]:
                reasons.append("client bundle requires a module missing from the target "
                               "module table: " + ", ".join(
                                   hit["specifier"] for hit in entry["client_hits"][:3]
                               ))
        # Factory registrations: a bundle that inlines ANOTHER self-registering
        # client package installs a second factory under that package's id. The
        # host graph row owns the same id, so boot dies with "duplicate factory
        # registration" — while declarations and the module-table scan stay green.
        entry["registration_hits"] = scan_client_registrations(code_source, entry["name"])
        if entry["registration_hits"]:
            offending = list(dict.fromkeys(
                item for hit in entry["registration_hits"] for item in (hit["foreign"] or hit["ids"])
            ))
            reasons.append("client bundle registers a factory for another package "
                           "(a self-registering bundle was inlined, not required): "
                           + ", ".join(offending[:3]))

        if reasons:
            entry["status"] = STATUS_INCOMPATIBLE
            entry["reason"] = "; ".join(reasons)

        # There may be no declarations at all — then code is the only signal.
        entry["code_clean"] = (not entry["removed_hits"] and not entry["client_hits"]
                               and not entry["registration_hits"]
                               and not entry["declaration_hits"] and not entry["inline_hits"])
        if entry["status"] == STATUS_UNKNOWN and entry["code_clean"]:
            entry["reason"] = ("the manifest declares no DSH version, so there is nothing to "
                               "compare; the code checks are clean")

        # Empirical verdict: the check runs AGAINST THE SAME core the plugin is
        # already installed on, and the code is clean. So it does work in fact,
        # even if the declarations ask for a newer version (a typical case: a plugin
        # declares a minimum above the running core, yet works on it). A working
        # installation must not be torn down because of a
        # strict declaration — such a plugin is marked as empirically good. For a
        # local source the condition is stricter: the scanned artifact must match
        # the installed one (otherwise the verdict would be about different code).
        running_code = installed_dir is not None and (local is None or local_version in (None, plugin.version))
        if (entry["status"] == STATUS_INCOMPATIBLE and entry["code_clean"]
                and analysis.current_core == analysis.target and running_code):
            entry["status"] = STATUS_COMPATIBLE
            entry["empirical"] = True
            entry["reason"] = ("already installed and running on this core; "
                               "declarations are stricter: " + (entry["reason"] or ""))
    else:
        entry["reason"] = (entry["reason"] + "; code was not checked — the plugin is not installed").strip("; ")

    # A local plugin may be published on npm under the same name. That is a
    # DIFFERENT artifact and must never be installed instead of the local one,
    # but it is useful to know about it.
    if local is not None and check_updates:
        twin = _latest_npm_version(plugin.name, offline=offline)
        if twin is not None and twin != (local_version or plugin.version):
            entry["npmLatest"] = twin
            twin_manifest = _manifest_for(plugin.name, plugin.spec, twin, offline=offline)
            if twin_manifest is not None:
                entry["npmLatestStatus"] = evaluate(
                    analysis.target, declarations_for(twin_manifest, analysis.host_packages)
                ).status

    if check_updates and plugin.source == "npm":
        latest = _latest_npm_version(plugin.name, offline=offline)
        entry["latest"] = latest
        if latest is not None and latest != plugin.version:
            latest_manifest = _manifest_for(plugin.name, plugin.spec, latest, offline=offline)
            if latest_manifest is not None:
                latest_verdict = evaluate(
                    analysis.target, declarations_for(latest_manifest, analysis.host_packages)
                )
                entry["latest_status"] = latest_verdict.status
                if latest_verdict.status == STATUS_COMPATIBLE:
                    entry["recommended"] = f"{plugin.name}@{latest}"

    entry["reason_code"] = reason_code_for(entry, basis=verdict.basis)
    return entry


def evaluate_registry_version(name: str, version: str, analysis: Analysis, *, offline: bool = False) -> str:
    """Verdict for a specific registry package version (the basis for an update)."""
    manifest = _manifest_for(name, None, version, offline=offline)
    if manifest is None:
        return STATUS_UNKNOWN
    return evaluate(analysis.target, declarations_for(manifest, analysis.host_packages)).status


def best_compatible_version(name: str, analysis: Analysis, *, offline: bool = False,
                            minimum: str | None = None) -> str | None:
    """Newest package version compatible with the target core.

    ``minimum`` is a lower bound (usually the installed version): an update must
    not roll a plugin back just because newer versions require a newer core.
    """
    try:
        versions = registry.all_versions(name, offline=offline)
    except RuntimeError:
        return None
    candidates = [v for v in versions if semver.parse_version(v) is not None]
    # Versions cannot be compared with the plain `<` operator — a semver comparator is needed.
    candidates.sort(key=cmp_to_key(semver.compare_version))
    if minimum is not None and semver.parse_version(minimum) is not None:
        candidates = [v for v in candidates if semver.compare_version(v, minimum) >= 0]
    best: str | None = None
    for version in candidates:
        if evaluate_registry_version(name, version, analysis, offline=offline) == STATUS_COMPATIBLE:
            best = version
    return best


def snapshot_entries(profile: Profile) -> list[dict]:
    """Entries for a snapshot: specifier, source, version and the full manifest."""
    entries = []
    for plugin in profile.plugins:
        manifest = None
        if plugin.directory:
            manifest = read_json(Path(plugin.directory) / "package.json")
        entry = {**plugin.to_dict(), "manifest": manifest}
        local = _resolve_local(plugin, profile.directory)
        if local is not None:
            entry["localPath"] = str(local.path)
            entry["localPrefix"] = local.prefix
            entry["localAvailable"] = local.available
            if manifest is None and local.available:
                entry["manifest"] = local.manifest()
        entries.append(entry)
    return entries


def local_extras(plugin: dict) -> dict:
    """Local source fields that must be kept in the state lists."""
    local = plugin.get("local")
    if not local:
        return {}
    extras = {"localPath": local.get("path"), "localPrefix": local.get("prefix")}
    if plugin.get("manifest"):
        extras["manifest"] = plugin["manifest"]
    return extras


def incompatible_entry(plugin: dict) -> dict:
    """Compact entry for the incompatible list — this is what ``recheck`` reads.

    For local plugins the manifest and the artifact path are added as well: they
    have no registry, and without those the recheck could not evaluate them.
    """
    entry = {
        "name": plugin["name"],
        "version": plugin.get("version"),
        "spec": plugin.get("spec"),
        "source": plugin.get("source"),
        "sourceLabel": plugin.get("sourceLabel") or plugin.get("source"),
        "reason": plugin.get("reason"),
        "requirement": plugin.get("requirement"),
        "status": plugin["status"],
    }
    entry.update(local_extras(plugin))
    return entry


def load_manifest_from_snapshot(entry: dict) -> dict | None:
    manifest = entry.get("manifest")
    return manifest if isinstance(manifest, dict) else None


def manifest_for_entry(entry: dict, profile_dir: Path | None = None, *, offline: bool = False) -> dict | None:
    """Manifest for a state entry: snapshot → local artifact → registry.

    The local artifact comes BEFORE the registry in the chain precisely for
    ``file:``/``link:`` plugins: npm may hold a different product under the same
    name, and a verdict based on it would be a verdict about somebody else's
    code. When the specifier is local but the artifact is gone, the registry is
    not queried at all — returning a foreign manifest is worse than returning
    nothing.
    """
    if not isinstance(entry, dict):
        return None
    manifest = load_manifest_from_snapshot(entry)
    if manifest is not None:
        return manifest
    manifest = locals_mod.manifest_for_entry(entry, profile_dir)
    if manifest is not None:
        return manifest
    spec = entry.get("spec")
    if locals_mod.is_local_spec(spec) or entry.get("localPath"):
        return None
    return _manifest_for(entry.get("name") or "", spec or "",
                         entry.get("version"), offline=offline)


def describe_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
