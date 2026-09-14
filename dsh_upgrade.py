#!/usr/bin/env python3
"""dsh-upgrade — upgrade management for the DSH core and the profile plugins.

Runs on a plain Python 3 with DSH switched OFF: everything needed for the checks
is fetched by the script itself (npm registry, marketplace index, core version
checkout). Plugin installation and removal go through the ``dsh plugin`` CLI (a
forwarder to pnpm, so no daemon is required), falling back to plain ``pnpm``.

Two equally valid ways to work:

* **No arguments — an interactive menu** with every action, previews for the
  destructive operations and settings (profile, target core, offline) that are
  remembered between runs.
* **With arguments — an ordinary CLI** (handy for scripts and cron).

Examples:
    python3 dsh_upgrade.py                            # menu
    python3 dsh_upgrade.py status
    python3 dsh_upgrade.py check --core 0.1.5-rc.2 --update
    python3 dsh_upgrade.py detach --yes
    python3 dsh_upgrade.py attach --yes --update
    python3 dsh_upgrade.py recheck --install --yes
    python3 dsh_upgrade.py pipeline --core 0.1.5-rc.2 --yes
    python3 dsh_upgrade.py check --checkouts ~/.dsh/checkouts   # keep the checkouts
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dshupgrade import analysis as analysis_mod  # noqa: E402
from dshupgrade import codes as codes_mod  # noqa: E402
from dshupgrade import config as config_mod  # noqa: E402
from dshupgrade import effects as effects_mod  # noqa: E402
from dshupgrade import invocation as invocation_mod  # noqa: E402
from dshupgrade import locals as locals_mod  # noqa: E402
from dshupgrade import paths, registry, report, semver, snapshot, style  # noqa: E402
from dshupgrade import termux as termux_mod  # noqa: E402
from dshupgrade import verify as verify_mod  # noqa: E402
from dshupgrade import wire as wire_mod  # noqa: E402
from dshupgrade.compat import (  # noqa: E402
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    accepted,
    declarations_for,
    evaluate,
    scan_client_modules,
    scan_removed_packages,
)
from dshupgrade.paths import (  # noqa: E402
    core_install_dir,
    core_version,
    profile_dir,
    state_dir,
)
from dshupgrade.profile import (  # noqa: E402
    PluginEntry,
    Profile,
    add_plugins,
    data_summary,
    read_profile,
    remove_plugins,
)

DSH_PACKAGE = "@deepseek-ai/dsh"

#: Labels for the plugin groups of the status table, by source kind.
SOURCE_GROUP = {
    "npm": "npm registry",
    "file": "local artifact (file:/workspace:/portal:)",
    "link": "local link (link:)",
    "github": "git source",
    "other": "other sources",
}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def load_profile(args):
    directory = profile_dir(args.profile)
    if not (directory / "package.json").is_file():
        raise SystemExit(f"profile not found: {directory}")
    return read_profile(directory)


def target_problem(target: str, *, offline: bool = False) -> str | None:
    """Why ``target`` cannot be a check target — or ``None`` when it can.

    A compatibility check compares a plugin against a version, so the version has to
    be one the tool can actually obtain: the installed core, a checkout already on
    disk, or a version the registry publishes. Anything else is not a hard version
    to compare with — every declaration would come back "unconfirmed" for lack of
    anything to compare against, which reads like a result and is not one.
    """
    if semver.parse_version(target) is None:
        return f"'{target}' is not a version number"
    if core_version() == target or paths.find_checkout(target) is not None:
        return None
    try:
        published = {item["version"]
                     for item in registry.core_versions(offline=offline)["versions"]}
    except (RuntimeError, OSError, ValueError):
        published = set()
    if target in published:
        return None
    if published:
        newest = registry.newest_core_version(offline=offline)
        return (f"no such core version: {target} (the registry publishes "
                f"{len(published)} versions, newest {newest or '—'})")
    return (f"cannot confirm {target}: the registry is unreachable and it is neither "
            "the installed core nor a checkout on disk")


def resolve_target(args) -> str:
    """Target core version: from --core, otherwise the NEWEST published version.

    The automatic target is the newest published release (prereleases included),
    not the closest one and not the installed one: an upgrade is supposed to move
    forward. The installed core is only a fallback for when the registry cannot be
    reached at all; ``plan`` lists every candidate if another version is wanted.
    """
    if args.core:
        problem = target_problem(args.core, offline=getattr(args, "offline", False))
        if problem:
            raise SystemExit(f"{problem}; pass a published core version, or drop --core "
                             "to use the newest one")
        return args.core
    newest = registry.newest_core_version(offline=getattr(args, "offline", False))
    if newest is not None:
        # On Termux the newest published version is not automatically the right
        # target: the Android corrections are anchored to one release, so a core
        # past it cannot be patched. The cap is applied here, before anything is
        # compared against the target, so every command sees the same version.
        newest, note = termux_mod.target_for(args, newest)
        if note:
            print(style.dim(f"  auto target: {style.subheading(newest)} — {note} "
                            "(use --core to pick another one)"), file=_log_stream(args))
        else:
            print(style.dim(f"  auto target: {style.subheading(newest)} — the newest published "
                            "version (use --core to pick another one)"), file=_log_stream(args))
        return newest
    current = core_version()
    if current:
        print(report.warning(f"registry unavailable — falling back to the installed core "
                             f"{current} as the target"), file=_log_stream(args))
        return current
    raise SystemExit("could not determine the core version: pass --core")


def analyse_for(args, profile, target: str):
    log_to = _log_stream(args)
    result = analysis_mod.analyse(
        profile,
        target,
        offline=args.offline,
        allow_clone=not args.no_clone,
        check_updates=bool(getattr(args, "update", False)),
        log=lambda text: print(style.dim(f"  {text}"), file=log_to),
    )
    _mark_verified(result, profile)
    return result


def _mark_verified(result, profile) -> None:
    """Grade the entries a previous runtime probe has proven on this core.

    The verdict is taken from the verify cache and nothing is executed here: the
    cache is keyed by a fingerprint that covers the core and every installed copy, so
    a verdict taken before any of them changed is not reused. A plugin is graded only
    when the whole picture is clean — the probe saw it import, apply and answer, the
    declaration and code checks found nothing, its client half does not draw into a
    switched-off row, and none of its calls addresses an endpoint this core does not
    serve. Runtime evidence is evidence about ONE core, so it is applied only when the
    target IS the installed core.
    """
    if result.current_core is None or result.current_core != result.target:
        return
    probes = verify_mod.load_cache(profile, result.current_core)
    if not probes:
        return
    proven = {name for name, probe in probes.items() if verify_mod.runtime_verified(probe)}
    if not proven:
        return
    _, shadows, _ = profile_surfaces(profile, probes)
    wire = wire_surfaces(profile)
    clean = set()
    for plugin in result.plugins:
        name = plugin["name"]
        if name not in proven:
            continue
        if wire.get(name):
            continue
        if any(not shadow.explained for shadow in shadows.get(name, [])):
            continue
        clean.add(name)
    analysis_mod.apply_runtime_verification(result, clean)


def _selection(args, profile, *, what: str) -> list[str]:
    """The plugins named by ``--only``, in profile order — or every plugin.

    A name the profile does not have is an input error, not a silent no-op: the
    caller asked for a plugin that is not here, and proceeding would report success
    for an operation that never happened.
    """
    names = list(getattr(args, "only", None) or [])
    if not names:
        return [plugin.name for plugin in profile.plugins]
    known = {plugin.name for plugin in profile.plugins}
    missing = sorted({name for name in names if name not in known})
    if missing:
        raise SystemExit(
            f"not in profile {profile.name}: {', '.join(missing)} (--only {what}); "
            "installed plugins: " + (", ".join(sorted(known)) or "none"))
    wanted = set(names)
    return [plugin.name for plugin in profile.plugins if plugin.name in wanted]


def _quiet(args) -> bool:
    """True when stdout is reserved for one machine-readable value.

    ``--json`` writes a single document there, ``--summary`` writes a single line:
    neither tolerates a report mixed into it. Progress and notes then go to
    stderr, where a human still sees them and a parser never reads them.
    """
    return bool(getattr(args, "json", False) or getattr(args, "summary", False))


def _log_stream(args):
    """Where progress lines go: stderr when stdout is reserved, else stdout."""
    return sys.stderr if _quiet(args) else sys.stdout


@contextmanager
def _report_console(args):
    """Human report text: stdout normally, stderr when stdout carries JSON.

    The report is not dropped — it keeps being printed where a reader looks — only
    moved off the stream a script parses.
    """
    if getattr(args, "json", False):
        with redirect_stdout(sys.stderr):
            yield
    else:
        yield


def summary_payload(*, total: int, incompatible: int, unknown: int, wire_dead: int,
                    handler_failures: int, exit_code: int, verified: int | None = None) -> dict:
    """The one-line summary as data, with the code the command returns.

    ``verified`` is reported by the commands that judge compatibility against the
    installed core, where the runtime probe can grade a plugin; the commands that
    report on the probe itself leave it out.
    """
    payload = {
        "total": total,
        "incompatible": incompatible,
        "unknown": unknown,
        "wire_dead": wire_dead,
        "handler_failures": handler_failures,
        "exit_code": exit_code,
    }
    if verified is not None:
        payload["verified"] = verified
    return payload


def summary_line(payload: dict) -> str:
    """The summary as the one line it is meant to be."""
    line = (f"{payload['total']} plugins checked, "
            f"{payload['incompatible']} incompatible, "
            f"{payload['unknown']} unknown, ")
    if payload.get("verified") is not None:
        line += f"{payload['verified']} verified, "
    return (line
            + f"{payload['wire_dead']} wire-dead, "
            f"{payload['handler_failures']} handler-failures")


def emit_summary(payload: dict, args) -> None:
    """Print the summary: one line, or one JSON object with ``--json``."""
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(summary_line(payload))


def print_data_note(names: list[str]) -> None:
    summary = data_summary(names)
    print()
    print(style.subheading("  Plugin data (this operation does not touch it):"))
    for key, paths in summary.items():
        print(f"    {style.subheading(key)}")
        for path in paths:
            print(f"      {style.path(path)}")
    print(style.dim("    ~/.dsh/sessions and ~/.dsh/storages are shared by all plugins "
                    "and stay in place"))


def _path_row(path) -> str:
    return style.path(path)


def _file_date(path: Path) -> str:
    """Calendar date of a saved report, when it records no generation time."""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
    except OSError:
        return "unknown"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_core_versions(args) -> int:
    data = registry.core_versions(offline=args.offline)
    print()
    print(style.heading("=== Core tags ==="))
    tag_rows = [[tag, version] for tag, version in sorted(data["tags"].items())]
    print(report.grouped_table(["tag", "version"], [("release channels", tag_rows)]))

    print()
    print(style.heading(f"=== Latest versions (registry updated {data['modified']}) ==="))
    version_rows = [[item["version"], item["time"] or ""] for item in data["versions"][-15:]]
    print(report.grouped_table(["version", "date"], [("last 15 published", version_rows)]))

    current = core_version()
    newer = [item for item in data["versions"]
             if current and item["version"] != current
             and semver.compare_version(item["version"], current) > 0]
    if current and newer:
        print()
        print(f"  newer than the installed {style.subheading(current)}: "
              f"{style.warn(len(newer))} "
              f"(closest: {newer[0]['version']}, newest: {newer[-1]['version']})")
    return 0


def cmd_plan(args) -> int:
    """Overview of EVERY available new core version: what happens to the plugins.

    A quick check (no checkout, no code scans) — to see the whole picture and pick
    a version to move to. A full check of a single version is the command
    ``check --core <version>``.
    """
    profile = load_profile(args)
    current = core_version()
    data = registry.core_versions(offline=args.offline)
    tag_of = {}
    for tag, version in data["tags"].items():
        tag_of.setdefault(version, []).append(tag)

    candidates = [item for item in data["versions"]
                  if current is None or semver.compare_version(item["version"], current) > 0]
    if not args.all:
        candidates = candidates[-args.limit:]

    print()
    print(style.heading("=== Upgrade plan ==="))
    print(f"  current core: {style.subheading(current or '—')}; "
          f"plugins in profile {style.subheading(profile.name)}: {len(profile.plugins)}")
    if not candidates:
        print(style.good("  Nothing is newer than the installed version."))
        return 0

    ready, unconfirmed, broken = [], [], []
    details: list[tuple[str, list[str]]] = []
    for item in candidates:
        version = item["version"]
        try:
            result = analysis_mod.analyse(
                profile, version, offline=args.offline, allow_clone=False,
                check_updates=False, log=lambda *_: None,
            )
        except RuntimeError as error:
            broken.append([version, item.get("time") or "", ",".join(tag_of.get(version, [])),
                           "—", "—", "—", str(error)])
            continue
        counts = result.counts()
        bad = [plugin["name"] for plugin in result.plugins
               if not accepted(plugin["status"])]
        row = [
            version,
            item.get("time") or "",
            ",".join(tag_of.get(version, [])) or "—",
            str(counts.get(STATUS_COMPATIBLE, 0)),
            str(counts.get(STATUS_INCOMPATIBLE, 0)),
            str(counts.get(STATUS_UNKNOWN, 0)),
            "; ".join(bad) if bad else "",
        ]
        if counts.get(STATUS_INCOMPATIBLE, 0):
            broken.append(row)
        elif counts.get(STATUS_UNKNOWN, 0):
            unconfirmed.append(row)
        else:
            ready.append(row)
        if bad:
            details.append((version, bad))

    headers = ["version", "date", "tag", "ok", "NO", "??", "plugins that will not install"]
    print()
    print(report.grouped_table(headers, [
        (style.paint(f"safe — every plugin is compatible ({len(ready)})", "green"), ready),
        (style.paint(f"unconfirmed plugins present ({len(unconfirmed)})", "yellow"), unconfirmed),
        (style.paint(f"incompatible plugins present ({len(broken)})", "bold", "red"), broken),
    ]))
    print()
    print(style.dim("  Legend: ok — compatible, NO — proven incompatible, "
                    "?? — unconfirmed (no declarations)."))
    print(style.dim("  This check is quick: declarations only, no checkout, no code scans."))
    print("  Full check of one version: "
          + style.command(invocation_mod.command("check", "--verbose",
                                                 raw=("--core", "<version>"))))
    if details:
        print()
        print(style.subheading("  Who exactly is missing (first 12 versions):"))
        for version, bad in details[:12]:
            print(f"    {style.subheading(version)}: {', '.join(bad)}")
    return 0


def _loads_cell(plugin, probes: dict) -> str:
    """The ``loads`` cell of the status table: did this copy import at all?"""
    probe = probes.get(plugin.name)
    if probe is None:
        return style.warn("?") if plugin.installed else style.dim("—")
    status = probe.status
    if status == verify_mod.LOADS:
        return style.good("yes")
    if status == verify_mod.FAILED:
        return style.bad("no")
    if status == verify_mod.CLIENT_ONLY:
        return style.dim("client")
    if status == verify_mod.MISSING:
        return style.dim("—")
    return style.warn("?")


def _status_probes(args, profile, current: str | None) -> dict:
    """Load verdicts for the status table.

    The plain ``status`` command is read-only and never executes plugin code: it
    shows what a previous ``verify`` left in the cache. The menu passes ``--verify``,
    which fills the cache in when it is missing or stale (an upgrade changes every
    fingerprint, so the table is refreshed exactly once per upgrade).
    """
    if getattr(args, "verify", False):
        return verify_mod.resolve(profile, current, refresh=False, allow_probe=True,
                                  log=lambda text: print(text, file=_log_stream(args)))
    return verify_mod.load_cache(profile, current)


def _surface_flags(name: str, shadows: dict, wire: dict | None = None) -> dict:
    """The ``surface_text`` keywords one plugin's findings produce."""
    certain, suspect = effects_mod.shadow_flags(shadows.get(name, []))
    return {"shadowed": certain, "suspect": suspect,
            "dead_wire": len((wire or {}).get(name, []))}


def _probes_summary(profile, probes: dict, wire: dict, exit_code: int) -> dict:
    """The summary of a profile whose plugins carry runtime verdicts.

    ``incompatible`` counts the server entries that do not import, ``unknown`` the
    plugins no verdict was produced for (not installed, or the probe could not
    run), ``wire_dead`` the plugins with a call the core does not serve and
    ``handler_failures`` the plugins with a route that fails on its first call.
    """
    incompatible = 0
    unknown = 0
    handler_failures = 0
    for plugin in profile.plugins:
        probe = probes.get(plugin.name)
        if probe is None:
            unknown += 1
            continue
        if probe.status == verify_mod.FAILED:
            incompatible += 1
        elif probe.status in (verify_mod.UNAVAILABLE, verify_mod.MISSING):
            unknown += 1
        if verify_mod.handler_failures(probe):
            handler_failures += 1
    return summary_payload(total=len(profile.plugins), incompatible=incompatible,
                           unknown=unknown, wire_dead=len(wire),
                           handler_failures=handler_failures, exit_code=exit_code)


def _surface_cell(plugin, probes: dict, shadows: dict, clients: set,
                  wire: dict | None = None) -> str:
    """The ``surface`` cell: what this plugin actually puts into the deployment."""
    probe = probes.get(plugin.name)
    if probe is None:
        return style.warn("?") if plugin.installed else style.dim("—")
    text = verify_mod.surface_text(probe, client=plugin.name in clients,
                                   **_surface_flags(plugin.name, shadows, wire))
    if text in (verify_mod.SHADOWED, verify_mod.SHADOW_SUSPECT, verify_mod.HANDLER_SUSPECT):
        return style.warn(text)
    if text == "no" or text in (verify_mod.APPLY_FAILED, verify_mod.WIRE_DEAD,
                                verify_mod.HANDLER_FAILED):
        return style.bad(text)
    if text.startswith("live:"):
        return style.good(text)
    if text.startswith("404:") or text == verify_mod.NO_OP:
        return style.warn(text)
    if text == "?":
        return style.warn(text)
    return style.dim(text) if text in ("client", verify_mod.NO_APPLY, "—") else text


def wire_surfaces(profile) -> dict:
    """RPC calls this core does not serve, per installed plugin.

    Read from files, so it needs no running host and no cached verdict — the same
    reason the shadow scan runs on the ``status`` path.
    """
    try:
        return wire_mod.scan_profile(profile)
    except (OSError, ValueError):
        return {}


def profile_surfaces(profile, probes: dict):
    """Effective loader tree, shadowed plugins, and who ships a browser bundle."""
    effective = effects_mod.resolve(profile)
    route_counts = {name: len(probe.route_paths)
                    for name, probe in probes.items() if probe.route_paths}
    shadows = effects_mod.scan_shadows(profile, effective, route_counts=route_counts)
    clients = {plugin.name for plugin in profile.plugins
               if plugin.installed and plugin.directory
               and effects_mod.client_half(Path(plugin.directory)).present}
    return effective, shadows, clients


def _shadow_grades(shadows: dict) -> tuple[dict, dict, dict]:
    """Split the findings by what they are worth: verdict, lead, explained."""
    verdicts: dict[str, list] = {}
    leads: dict[str, list] = {}
    explained: dict[str, list] = {}
    for name, items in shadows.items():
        for shadow in items:
            bucket = verdicts if shadow.certain else (explained if shadow.explained else leads)
            bucket.setdefault(name, []).append(shadow)
    return verdicts, leads, explained


def _print_shadows(shadows: dict, effective, hint: str | None = None) -> None:
    """Explain every plugin whose host surface the profile switched off."""
    if not shadows:
        return
    verdicts, leads, explained = _shadow_grades(shadows)
    if verdicts:
        print()
        print(style.heading("=== Shadowed surfaces ==="))
        print(style.dim("  a client half that draws into a switched-off component: it loads, "
                        "then does nothing"))
        for name in sorted(verdicts):
            print()
            print(f"  {style.warn(name)} — the UI it augments is not mounted")
            for shadow in verdicts[name]:
                print(f"    row: {style.subheading(shadow.row.id)} ({shadow.row.name})")
                print(f"    disabled by: {shadow.row.disabled_by or 'an earlier layer'}")
                print(f"    evidence: {shadow.evidence}")
                if shadow.route_count:
                    print(f"    the server half still runs: {shadow.route_count} route(s) "
                          "registered")
        print()
        print(style.dim("  what to do: either you do not need the plugin, or the component it "
                        "augmented has to be re-enabled"))
        print("  the whole tree, with the rows above marked: "
              + style.command(hint or invocation_mod.loader_hint()))
    if leads:
        print()
        print(style.heading("=== Shadow leads (not a verdict) ==="))
        print(style.dim("  a switched-off row's name appears in the client half's text — a "
                        "comment naming the component it augments reads the same way as a"))
        print(style.dim("  real dependency. Check the line before acting on it; the tree "
                        "below shows what is actually mounted."))
        for name in sorted(leads):
            for shadow in leads[name]:
                print()
                print(f"  {style.warn(name)} — the component it names may still be mounted")
                print(f"    row: {style.subheading(shadow.row.id)} ({shadow.row.name}) "
                      f"is off")
                print(f"    evidence: {shadow.evidence}")
                print(style.dim("    no enabled row in that layer reproduces the DOM names "
                                "this client half selects on"))
    if explained:
        print()
        print(style.dim("  references explained by a replacement (not breakage):"))
        for name in sorted(explained):
            for shadow in explained[name]:
                print(style.dim(f"    {name} names {shadow.row.id}, and "
                                f"{shadow.replaced_by} re-mounts the same DOM contract"))


def _print_wire(wire: dict) -> None:
    """Explain every RPC call the installed core does not serve."""
    if not wire:
        return
    print()
    print(style.heading("=== Wire calls this core does not serve ==="))
    print(style.dim("  read from the plugin's own files against the core's declared "
                    "endpoints (its TYPERT faces): the call answers 404 at run time"))
    for name in sorted(wire):
        print()
        print(style.bad(f"  {name}"))
        for call in wire[name]:
            detail = f"    {call.verdict}: {call.path}"
            if call.method:
                detail += f'  (method: "{call.method}")'
            print(detail)
            print(f"      at {call.source}")
            if call.note:
                print(f"      {call.note}")


def _print_handlers(probes: dict) -> None:
    """Route handlers that fail on their first call — the partial-failure section.

    This is the failure the earlier checks could not see: the entry imports, the
    row applies, the route is registered, and the closure behind it is broken. A
    handler with its own ``try/catch`` swallows the error and answers ``200 []``,
    so the browser shows an empty list rather than a failure, and the only trace is
    the line it logged — which is exactly what the probe captured.
    """
    failures = [(probe, verify_mod.handler_failures(probe)) for probe in sort_probes(probes)]
    failures = [(probe, items) for probe, items in failures if items]
    leads = [(probe, verify_mod.handler_leads(probe)) for probe in sort_probes(probes)]
    leads = [(probe, items) for probe, items in leads if items]
    if failures:
        print()
        print(style.heading("=== Route handlers that fail on the first call ==="))
        print(style.dim("  apply() only registers the closure; the probe calls each route "
                        "handler once with a GET and reads"))
        print(style.dim("  what it threw and logged. A handler that catches its own error and "
                        "answers 200 looks healthy elsewhere."))
        for probe, items in failures:
            print()
            print(style.bad(f"  {probe.name}"))
            for reason, record in items:
                print(f"    route: GET {style.subheading(str(record.get('path')))}")
                print(f"    {reason}")
                answered = record.get("status")
                if answered is not None:
                    print(style.dim(f'      answered {answered} "{_body_head(record)}"'))
                for line in (record.get("logs") or [])[1:4]:
                    print(style.dim(f"      log: {line}"))
    if leads:
        print()
        print(style.heading("=== Handler leads (not a verdict) ==="))
        print(style.dim("  a handler that warned, logged something, threw an error the "
                        "recording stub can also cause, or was still"))
        print(style.dim("  running when the probe window closed — worth reading, not proof"))
        for probe, items in leads:
            print()
            print(style.warn(f"  {probe.name}"))
            for reason, record in items:
                print(f"    route: GET {style.subheading(str(record.get('path')))}")
                print(f"    {reason}")
    _print_probe_notes(probes)


def _body_head(record: dict, limit: int = 60) -> str:
    """The first characters of what the handler answered, on one line."""
    body = " ".join(str(record.get("body") or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def _print_probe_notes(probes: dict) -> None:
    """Anything the probe reported without calling it a failure.

    The probe's own sentence is not enough for a reader: one line of prose cannot
    say whether a route was skipped on purpose, whether a registration callback
    was broken by the recording stub rather than by the plugin, or whether the
    probe itself gave up. Each note is printed with its kind and with what that
    kind means — and none of it is a verdict.
    """
    noted = [probe for probe in sort_probes(probes) if probe.notes]
    if not noted:
        return

    marks = {
        verify_mod.SKIPPED: "skipped by design",
        verify_mod.STUB_CAUSE: "stub may be the cause",
        verify_mod.PROBE_ERROR: "probe error",
        verify_mod.UNCLASSIFIED: "note",
    }
    width = max(len(mark) for mark in marks.values())

    print()
    print(style.heading("=== Probe notes ==="))
    print(style.dim("  none of these is a failure: each says what the probe did not do, or what a"))
    print(style.dim("  plugin callback reported while the probe ran it"))
    for probe in noted:
        print()
        print(f"  {style.subheading(probe.name)}")
        for note in probe.notes:
            headline, meaning = verify_mod.note_line(note)
            mark = marks.get(verify_mod.note_family(note)[0], "note")
            print(f"    {style.dim(mark.ljust(width))}  {headline}")
            if meaning:
                print(style.dim(f"      {meaning}"))


def _print_loader_conflicts(effective) -> None:
    """Rows mounting the same module: the loader fails loudly on these."""
    if not effective.duplicates:
        return
    print()
    print(style.heading("=== Double mounts ==="))
    for name, ids in sorted(effective.duplicates.items()):
        print(style.bad(f"  {name}: mounted by {', '.join(ids)}"))


def _print_surface_legend(probes: dict, shadows: dict, clients: set,
                          wire: dict | None = None) -> None:
    """Explain the ``surface`` column once, under the table."""
    if not probes:
        print()
        print(style.dim("  surface: not verified yet — run 'dsh_upgrade.py verify'"))
        return
    seen: list[str] = []
    for probe in probes.values():
        text = verify_mod.surface_text(probe, client=probe.name in clients,
                                       **_surface_flags(probe.name, shadows, wire))
        key = text.split(":")[0] + ":" if ":" in text else text
        if key not in seen:
            seen.append(key)
    lines = []
    for key in seen:
        if key in verify_mod.SURFACE_LEGEND:
            lines.append((key.rstrip(":"), verify_mod.SURFACE_LEGEND[key]))
    if not lines:
        return
    print()
    for label, text in lines:
        print(style.dim(f"  {label:<11} {text}"))


def _print_loads_legend(probes: dict) -> None:
    """Explain the column once, under the table."""
    if not probes:
        print()
        print(style.dim("  loads: not verified yet — run 'dsh_upgrade.py verify' "
                        "(imports every installed plugin with node)"))
        return
    used = [status for status in (verify_mod.LOADS, verify_mod.FAILED,
                                  verify_mod.CLIENT_ONLY, verify_mod.UNAVAILABLE)
            if any(probe.status == status for probe in probes.values())]
    if not used:
        return
    print()
    for status in used:
        print(style.dim(f"  {verify_mod.LABELS[status]:<6} {verify_mod.LEGEND[status]}"))


def _print_load_failures(probes: dict) -> None:
    """The detail of every plugin whose entry did not import."""
    failed = [probe for probe in sort_probes(probes) if probe.status == verify_mod.FAILED]
    if not failed:
        return
    print()
    print(style.heading("=== Load failures ==="))
    for probe in failed:
        print(style.bad(f"  {probe.name}"))
        print(f"    {probe.detail}")


def sort_probes(probes: dict) -> list:
    """Probe results in a stable, human order."""
    return [probes[name] for name in sorted(probes)]


def cmd_status(args) -> int:
    install_dir = core_install_dir()
    current = core_version(install_dir)
    profile = load_profile(args)
    probes = _status_probes(args, profile, current)
    effective, shadows, clients = profile_surfaces(profile, probes)
    wire = wire_surfaces(profile)

    if getattr(args, "summary", False):
        emit_summary(_probes_summary(profile, probes, wire, 0), args)
        return 0

    def plugin_payload(plugin) -> dict:
        payload = plugin.to_dict()
        probe = probes.get(plugin.name)
        payload["loads"] = probe.to_dict() if probe is not None else None
        payload["surface"] = (verify_mod.surface_text(
                                  probe, client=plugin.name in clients,
                                  **_surface_flags(plugin.name, shadows, wire))
                              if probe is not None else ("?" if plugin.installed else "—"))
        payload["shadowed"] = [shadow.to_dict() for shadow in shadows.get(plugin.name, [])]
        payload["wire"] = [call.to_dict() for call in wire.get(plugin.name, [])]
        return payload

    payload = {
        "core": {"version": current, "installDir": str(install_dir) if install_dir else None,
                 "termux": termux_mod.summary(args)},
        "profile": {"name": profile.name, "dir": str(profile.directory),
                    "bundles": profile.bundles},
        "plugins": [plugin_payload(plugin) for plugin in profile.plugins],
    }
    if args.json:
        if getattr(args, "loader", False):
            payload["loader"] = loader_payload(effective, shadows)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print()
    print(style.heading("=== Core ==="))
    print(f"  version: {style.subheading(current or 'not found')}")
    print(f"  directory: {_path_row(install_dir) if install_dir else '—'}")
    try:
        data = registry.core_versions(offline=args.offline)
        payload["core"]["tags"] = data["tags"]
        print(f"  tags: {', '.join(f'{k}={v}' for k, v in sorted(data['tags'].items()))}")
        print(f"  recent versions: {', '.join(v['version'] for v in data['versions'][-6:])}")
    except RuntimeError as error:
        print(report.warning(f"registry unavailable: {error}"))

    # The Termux line is printed only where it changes what a command would do: on
    # Android a core upgrade without the patch layer produces a tree that does not
    # run, so "is the layer there, and which version does it validate" is part of the
    # core's state, not a footnote to the platform.
    if termux_mod.active(args):
        print(f"  termux layer: {_path_row(payload['core']['termux']['layer']) if payload['core']['termux']['layer'] else style.warn('not found')}")
        validated = payload["core"]["termux"]["validated"]
        if validated:
            print(f"  layer validates: {style.subheading(validated)}")

    print()
    print(style.heading("=== Profile ==="))
    print(f"  {style.subheading(profile.name)} ({_path_row(profile.directory)})")
    print(f"  bundle layers: {len(profile.bundles)}")
    print(f"  plugins: {len(profile.plugins)}")

    by_source: dict[str, list] = {}
    for plugin in profile.plugins:
        by_source.setdefault(plugin.source, []).append(plugin)
    groups = []
    for source, items in sorted(by_source.items()):
        label = SOURCE_GROUP.get(source, source)
        groups.append((style.subheading(f"{label} ({len(items)})"), [[
            plugin.name,
            plugin.version or "—",
            _loads_cell(plugin, probes),
            _surface_cell(plugin, probes, shadows, clients, wire),
            plugin.source,
            style.good("yes") if plugin.in_bundles else style.dim("no"),
            plugin.spec,
        ] for plugin in items]))
    print()
    print(style.heading("=== Plugins ==="))
    if groups:
        print(report.grouped_table(["plugin", "version", "loads", "surface", "source",
                                    "in bundles", "specifier"], groups))
    else:
        print(style.dim("  no plugins recorded in the profile"))
    _print_loads_legend(probes)
    _print_surface_legend(probes, shadows, clients, wire)
    _print_load_failures(probes)
    _print_handlers(probes)
    _print_wire(wire)
    _print_shadows(shadows, effective, hint=invocation_mod.loader_hint(profile.name))
    _print_loader_conflicts(effective)
    if getattr(args, "loader", False):
        _print_loader(effective, shadows)
    return 0


def _loader_state(row) -> str:
    """The state of a row as one word: ``on``, ``disabled`` or ``conditional``."""
    if row.condition is not None:
        return "conditional"
    return "disabled" if row.disabled else "on"


def _needed_rows(shadows: dict | None) -> dict[str, list[str]]:
    """Which plugins draw into which row — the ``needed by`` column, as data.

    A reference that an enabled replacement already explains is left out: the row
    is not needed by that plugin any more, and marking it would undo the point of
    telling the two findings apart.
    """
    needed: dict[str, list[str]] = {}
    for plugin, items in (shadows or {}).items():
        for shadow in items:
            if shadow.explained:
                continue
            needed.setdefault(shadow.row.id, []).append(plugin)
    return needed


def _print_loader(effective, shadows: dict | None = None) -> None:
    """The effective loader tree: every row, what mounts it, who turned it off.

    ``shadows`` is the shadowed-surface map, when it is already at hand: the rows
    those plugins draw into are then marked in the tree. Without it the tree is a
    list of rows and the "which one do I have to re-enable" question is still open —
    the row is in there, but nothing says it matters.
    """
    needed = _needed_rows(shadows)
    print()
    print(style.heading("=== Effective loader tree ==="))
    print(f"  {len(effective.rows)} rows over {len(effective.layers)} patch layers")
    headers = ["row", "module", "state", "disabled by"]
    if needed:
        headers.append("needed by")
    rows = []
    for rid in effective.order:
        row = effective.rows[rid]
        cells = [rid, row.name, _loader_state(row), row.disabled_by or row.mounted_by]
        if needed:
            users = sorted(needed.get(rid) or [])
            cells.append(", ".join(users) if users else "—")
        rows.append(cells)
    print()
    print(report.grouped_table(headers, [("", rows)]))
    if needed:
        print()
        print(style.dim("  'needed by': that plugin draws into this row, but the row is "
                        "switched off — it loads and then does nothing"))
    if effective.orphans:
        print()
        print(report.warning("these patch entries name a row no layer mounts: "
                             + ", ".join(effective.orphans)))


def loader_payload(effective, shadows: dict | None = None) -> dict:
    """The tree as data — what ``--json --loader`` returns.

    ``--loader`` is honoured together with ``--json``: a flag that is silently
    ignored is worse than one that does not exist.
    """
    needed = _needed_rows(shadows)
    rows = []
    for rid in effective.order:
        row = effective.rows[rid]
        rows.append({"id": rid, "name": row.name, "state": _loader_state(row),
                     "disabledBy": row.disabled_by or row.mounted_by,
                     "neededBy": sorted(needed.get(rid) or [])})
    return {"layers": len(effective.layers), "rows": rows,
            "duplicates": effective.duplicates, "orphans": effective.orphans}


def print_loader_tree(profile, shadows: dict | None = None) -> None:
    """Print the tree for a profile — the shortcut behind the menu's offer.

    Kept public (and probe-free) so the menu can show the tree in place: by the time
    the report names the command, leaving the menu to type it is busywork.
    """
    effective = effects_mod.resolve(profile)
    if shadows is None:
        shadows = effects_mod.scan_shadows(profile, effective)
    _print_loader(effective, shadows)


def findings_payload(probes: dict, shadows: dict, wire: dict) -> list[dict]:
    """Every runtime finding, with the confidence it is worth.

    One list over the three sources — broken route handlers, shadowed surfaces and
    calls the core does not serve — so a consumer does not have to know which
    per-plugin structure a finding lives in. ``confidence`` separates a verdict
    (proven by the probe, or by the core's own endpoint declarations) from a lead
    (evidence a reader still has to confirm).
    """
    findings: list[dict] = []
    for probe in sort_probes(probes):
        for reason, record in verify_mod.handler_failures(probe):
            findings.append({
                "plugin": probe.name,
                "surface": verify_mod.HANDLER_FAILED,
                "confidence": "verdict",
                "detail": reason,
                "path": record.get("path"),
                "reason_code": codes_mod.HANDLER_REFERENCE_ERROR,
            })
        for reason, record in verify_mod.handler_leads(probe):
            findings.append({
                "plugin": probe.name,
                "surface": verify_mod.HANDLER_SUSPECT,
                "confidence": "lead",
                "detail": reason,
                "path": record.get("path"),
            })
    for name in sorted(shadows):
        for shadow in shadows[name]:
            if shadow.explained:
                continue
            findings.append({
                "plugin": name,
                "surface": (verify_mod.SHADOWED if shadow.certain
                            else verify_mod.SHADOW_SUSPECT),
                "confidence": shadow.confidence,
                "detail": shadow.evidence,
                "row": shadow.row.id,
            })
    for name in sorted(wire):
        for call in wire[name]:
            findings.append({
                "plugin": name,
                "surface": verify_mod.WIRE_DEAD,
                "confidence": call.confidence,
                "detail": (f"{call.path}: {call.note}" if call.note else call.path),
                "path": call.path,
                "reason_code": codes_mod.primary(codes_mod.wire_codes([call.verdict])),
            })
    return findings


def _print_diff(payload: dict) -> None:
    """Human-readable comparison of two verification results."""
    since = payload.get("since") or "the baseline"
    print()
    print(style.heading(f"=== Changes since {since} ==="))
    if not (payload["changed"] or payload["added"] or payload["removed"]):
        print(style.dim("  no changes"))
        return
    for item in payload["changed"]:
        print(f"  {style.subheading(item['plugin'])}")
        for field, values in item["fields"].items():
            line = f"    {field + ':':<9}{values['old']} → {values['new']}"
            if (field == "loads" and values["old"] == verify_mod.LABELS[verify_mod.FAILED]
                    and values["new"] == verify_mod.LABELS[verify_mod.LOADS]):
                line += "  " + style.good("(fixed)")
            print(line)
    for name in payload["added"]:
        print(f"  {style.subheading(name)}: added")
    for name in payload["removed"]:
        print(f"  {style.dim(name)}: removed")


def cmd_verify(args) -> int:
    """Import every installed plugin with node and report which ones load.

    This is the direct answer to "do they all work": the installed copies are
    executed the way DSH executes them (a Node process in the profile directory).
    The result is cached until the core or a plugin copy changes, which is what the
    ``loads`` column of ``status`` reads.
    """
    install_dir = core_install_dir()
    current = core_version(install_dir)
    profile = load_profile(args)
    json_mode = bool(getattr(args, "json", False))
    summary_mode = bool(getattr(args, "summary", False))
    diff_path = getattr(args, "diff", None)

    if current is None:
        print(report.warning("the installed core was not found — a probe would prove nothing"),
              file=_log_stream(args))
        return 1

    if not (summary_mode or diff_path):
        with _report_console(args):
            print()
            print(style.heading("=== Runtime verification ==="))
            print(f"  core: {style.subheading(current)}")
            print(f"  profile: {style.subheading(profile.name)} ({_path_row(profile.directory)})")
            print(style.dim("  importing each installed server entry with node, in the profile "
                            "directory — client-only bundles are not executed (static scans cover them)"))
            print(style.dim("  then calling apply() against a recording context, to see what each "
                            "plugin registers"))
            print(style.dim("  then calling every route handler apply() registered, once, and "
                            "reading what it threw and logged"))

    cached_only = bool(getattr(args, "cached", False))
    handlers = not bool(getattr(args, "no_handlers", False))

    def progress(text: str) -> None:
        print(text, file=_log_stream(args))

    probes = verify_mod.resolve(profile, current, refresh=not cached_only,
                                allow_probe=not cached_only, log=progress, handlers=handlers)
    if not probes:
        if cached_only:
            print(report.warning("no cached verdicts yet — run verify without --cached"),
                  file=_log_stream(args))
        else:
            print(report.warning("nothing to verify (no installed plugins)"),
                  file=_log_stream(args))
        if summary_mode:
            emit_summary(summary_payload(total=len(profile.plugins), incompatible=0,
                                         unknown=len(profile.plugins), wire_dead=0,
                                         handler_failures=0, exit_code=0), args)
        return 0

    effective, shadows, clients = profile_surfaces(profile, probes)
    wire = wire_surfaces(profile)
    live_note = None
    if getattr(args, "live", False):
        base_url = getattr(args, "web_url", None) or verify_mod.DEFAULT_WEB_URL
        reachable, verdicts = verify_mod.live_probe(probes, base_url=base_url, log=progress)
        if reachable:
            verify_mod.apply_live(probes, verdicts)
        else:
            live_note = (f"nothing answers at {base_url} — start 'dsh web' to confirm the "
                         "routes it serves (--web-url overrides the address)")

    counts = {status: sum(1 for probe in probes.values() if probe.status == status)
              for status in (verify_mod.LOADS, verify_mod.FAILED, verify_mod.CLIENT_ONLY,
                             verify_mod.MISSING, verify_mod.UNAVAILABLE)}
    exit_code = 2 if counts[verify_mod.FAILED] else 0

    if diff_path:
        path = Path(diff_path).expanduser()
        if not path.is_file():
            raise SystemExit(f"diff baseline not found: {path}")
        try:
            generated, old_items = verify_mod.load_verified(path)
        except ValueError as error:
            raise SystemExit(f"diff baseline unreadable: {error}")
        payload = verify_mod.diff_probes(old_items, probes)
        payload["since"] = (generated or "").split("T")[0] or _file_date(path)
        if json_mode:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_diff(payload)
        return exit_code

    if summary_mode:
        emit_summary(_probes_summary(profile, probes, wire, exit_code), args)
        return exit_code

    rows = []
    for probe in sort_probes(probes):
        entry = profile.by_name(probe.name)
        cell = _loads_cell(entry, probes) if entry is not None else style.warn("?")
        detail = probe.detail
        if probe.apply_error:
            detail = f"apply(): {probe.apply_error}"
        elif not probe.detail and probe.routes:
            detail = ", ".join(probe.route_paths)
        rows.append([probe.name,
                     (entry.version if entry is not None and entry.version else "—"),
                     cell,
                     _surface_cell(entry, probes, shadows, clients, wire)
                     if entry is not None else style.warn("?"),
                     f"{probe.ms} ms" if probe.ms else "—",
                     detail])
    with _report_console(args):
        print()
        print(report.grouped_table(["plugin", "version", "loads", "surface", "time", "detail"],
                                   [("", rows)]))
        _print_loads_legend(probes)
        _print_surface_legend(probes, shadows, clients, wire)
        if live_note:
            print()
            print(report.warning(live_note))

        print()
        print("  " + "   ".join((
            style.good(f"load: {counts[verify_mod.LOADS]}"),
            style.bad(f"fail: {counts[verify_mod.FAILED]}"),
            style.dim(f"client-only: {counts[verify_mod.CLIENT_ONLY]}"),
            style.warn(f"unavailable: {counts[verify_mod.UNAVAILABLE]}"),
        )))
        print(f"  cached in: {_path_row(verify_mod.cache_path(profile.name))}")
        _print_load_failures(probes)
        _print_handlers(probes)
        _print_wire(wire)
        _print_shadows(shadows, effective, hint=invocation_mod.loader_hint(profile.name))
        _print_loader_conflicts(effective)
        if getattr(args, "loader", False):
            _print_loader(effective, shadows)

    if json_mode:
        payload = {"core": current, "profile": profile.name,
                   "fingerprint": verify_mod.fingerprint(profile, current),
                   "plugins": [probe.to_dict() for probe in sort_probes(probes)],
                   "findings": findings_payload(probes, shadows, wire),
                   "shadowed": {name: [shadow.to_dict() for shadow in items]
                                for name, items in shadows.items()},
                   "wire": {name: [call.to_dict() for call in calls]
                            for name, calls in wire.items()},
                   "duplicates": effective.duplicates}
        if getattr(args, "loader", False):
            payload["loader"] = loader_payload(effective, shadows)
        print()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def cmd_check(args) -> int:
    profile = load_profile(args)
    target = resolve_target(args)
    result = analyse_for(args, profile, target)
    exit_code = 2 if result.incompatible() else 0
    counts = result.counts()
    json_mode = bool(getattr(args, "json", False))
    summary_mode = bool(getattr(args, "summary", False))

    if not summary_mode:
        with _report_console(args):
            report.print_analysis(result, verbose=args.verbose)

    document = {
        "target": result.target,
        "current": result.current_core,
        "counts": counts,
        "removed": result.removed,
        "notes": result.notes,
        "plugins": result.plugins,
    }
    paths: dict[str, str] = {}
    if json_mode:
        out = state_dir() / f"check-{target}.json"
        snapshot.write_json(out, document)
        paths["report"] = str(out)

    # The list is ALWAYS written (including when there is nothing proven
    # incompatible): it is also the state used by recheck and by the menu viewer,
    # and "unconfirmed" entries belong in it too. The exit code stays 2 for "NO"
    # only.
    entries = [analysis_mod.incompatible_entry(plugin) for plugin in result.plugins
               if not accepted(plugin["status"])]
    json_path, md_path = snapshot.write_incompatible(target, entries, note="check")
    paths["incompatible"] = str(json_path)
    paths["markdown"] = str(md_path)

    if summary_mode:
        emit_summary(summary_payload(
            total=len(result.plugins),
            incompatible=counts.get(STATUS_INCOMPATIBLE, 0),
            unknown=counts.get(STATUS_UNKNOWN, 0),
            wire_dead=0, handler_failures=0, exit_code=exit_code,
            verified=counts.get(STATUS_VERIFIED, 0)), args)
        return exit_code

    if json_mode:
        document["state"] = paths
        print(json.dumps(document, ensure_ascii=False, indent=2, default=str))
        return exit_code

    print()
    print(f"  incompatible list: {_path_row(json_path)}")
    print(f"  human readable:    {_path_row(md_path)}")
    return exit_code


# --------------------------------------------------------------------------- #
# Inspect: an artifact that is NOT installed in the profile
# --------------------------------------------------------------------------- #

def _artifact_source(argument: str):
    """Resolve an ``inspect`` argument into a local source (directory or tarball).

    A plain path wins over the specifier syntax; ``file:``/``link:`` are accepted
    as well, so a value copied from ``package.json`` works as is.
    """
    candidate = Path(argument).expanduser()
    if candidate.exists():
        return locals_mod.from_path(candidate)
    return locals_mod.resolve(argument)


def _artifact_profile(source, name: str, manifest: dict) -> Profile:
    """A one-plugin profile standing in for an artifact that is not installed.

    ``analyse`` is reused unchanged: a local source is always checked against the
    artifact itself, and an entry without an installed directory is never promoted
    to "empirically compatible" — the artifact has not run on any core yet.
    """
    version = manifest.get("version") if isinstance(manifest.get("version"), str) else None
    entry = PluginEntry(
        name=name,
        spec=source.spec,
        source="file",
        installed=False,
        version=version,
        directory=None,
        in_bundles=False,
    )
    return Profile(directory=Path.cwd(), name=name, dependencies={name: source.spec},
                   bundles=[], plugins=[entry])


def cmd_inspect(args) -> int:
    """Compatibility of a plugin directory or tarball BEFORE it is installed.

    The default target is the INSTALLED core — the question here is "will this
    artifact run on the harness I have", not "what is the newest release". Pass
    ``--core`` when another version is the target. Nothing is written to the
    profile and nothing is installed: the same static checks run over the artifact's own bytes —
    the DSH version declarations, hard links to packages the target no
    longer ships, the browser module table, the ``dsh.client`` declaration (and the
    client bundle it promises) and inline purity — plus the wire contract: the
    literal
    ``/api`` calls it makes, against the endpoints the installed core declares).

    The wire check runs only when the target IS the installed core: the endpoint set
    is read from the core's generated TYPERT faces, which a source checkout does not
    publish. A different ``--core`` reports the check as not performed.
    """
    source = _artifact_source(args.artifact)
    if source is None:
        raise SystemExit(f"not a plugin directory or tarball: {args.artifact}")
    if not source.available:
        raise SystemExit(f"path not found: {source.path}")

    manifest = source.manifest()
    if manifest is None:
        raise SystemExit(f"package.json not found in the artifact: {source.path}")
    name = manifest.get("name")
    if not isinstance(name, str) or not name:
        raise SystemExit(f"the artifact manifest has no package name: {source.path}")

    installed = core_version()
    target = args.core or installed
    if target is None:
        raise SystemExit("could not determine the core version: pass --core")
    problem = target_problem(target, offline=args.offline)
    if problem:
        raise SystemExit(f"{problem}; pass a published core version, or drop --core to judge "
                         "the artifact against the installed one")

    baseline = args.since or installed
    # ``--json`` makes stdout a single JSON document, so the progress log of the
    # analysis goes to stderr and no report is printed: a pre-install gate is
    # meant to be piped into a script.
    def log(text: str) -> None:
        print(style.dim(f"  {text}"), file=sys.stderr if args.json else sys.stdout)

    if not args.json:
        print()
        print(style.heading("=== Artifact check (not installed) ==="))
        print(f"  artifact: {_path_row(source.path)} — {source.kind_label()}")
        version = manifest.get("version") if isinstance(manifest.get("version"), str) else None
        print(f"  package:  {style.subheading(name)} {version or ''}".rstrip())
        core_note = " (installed)" if target == installed else ""
        print(f"  target core: {target}{core_note}")
        if args.since:
            print(f"  baseline:    {args.since} (--since) — references to packages it shipped "
                  "are checked against the target")
        elif target == installed:
            print(style.dim("  baseline:    the installed core itself — the removed-package scan "
                            "has no signal in this mode; add --since <older core> to compare"))
        print(style.dim("  the profile is not read and not changed: the artifact is checked "
                        "against its own bytes"))

    # The wire check is the sixth static check, and the only one that reads the
    # caller side: the other five say whether the artifact will load and register,
    # this one says whether the calls it makes are answered. It needs the core's
    # own generated TYPERT faces, which exist for the INSTALLED core only — a
    # source checkout publishes no built faces, so a non-installed target is
    # reported as unchecked rather than guessed at.
    core_endpoints = (wire_mod.core_endpoints()
                      if target == installed else wire_mod.CoreEndpoints())
    calls: list[wire_mod.WireCall] = []
    if core_endpoints.endpoints:
        code = source.code_source(manifest)
        if code is not None and not code.empty:
            calls = wire_mod.check_source(name, code.files, core_endpoints)

    profile = _artifact_profile(source, name, manifest)
    result = analysis_mod.analyse(
        profile, target,
        offline=args.offline,
        allow_clone=not args.no_clone,
        check_updates=False,
        since=args.since,
        log=log,
    )

    # A dead call is a finding of the artifact itself, so it fills the reason code
    # when the static checks produced none: the artifact is otherwise "compatible"
    # and still does not work.
    if calls:
        found = codes_mod.wire_codes([call.verdict for call in calls])
        for entry in result.plugins:
            entry["reason_code"] = codes_mod.primary([entry.get("reason_code"), *found])

    if args.json:
        print(analysis_mod.describe_json({
            "artifact": source.to_dict(),
            "target": target,
            "installedCore": installed,
            "baseline": baseline,
            "plugins": result.plugins,
            "wire": [call.to_dict() for call in calls],
        }))
    else:
        report.print_analysis(result, verbose=True)
        if calls:
            _print_wire({name: calls})
        elif not core_endpoints.endpoints:
            print()
            print(style.dim("  wire: not checked — the target core is not installed, and a "
                            "source checkout publishes no generated endpoint faces"))
        else:
            print()
            print(style.dim(f"  wire: every literal RPC call is served by {target} "
                            f"({len(core_endpoints.endpoints)} endpoint(s) declared)"))
    return 2 if result.incompatible() or calls else 0


def cmd_detach(args) -> int:
    profile = load_profile(args)
    names = _selection(args, profile, what="detach")
    if not names:
        print(style.dim("No plugins are recorded in the profile — nothing to detach"))
        return 0

    if args.dry_run:
        print()
        print(style.heading(f"=== Dry run: profile {profile.name} ==="))
        print(f"  plugins to detach: {style.warn(len(names))}")
        for name in names:
            print(f"    {style.dim('-')} {name}")
        print()
        print(style.dim("  --dry-run: no snapshot is written, nothing was changed."))
        print(style.dim("  Plugin data (~/.dsh/sessions, ~/.dsh/storages and their own "
                        "directories) is not deleted."))
        return 0

    data = snapshot.write_snapshot(
        profile, core_version(), analysis_mod.snapshot_entries(profile), note="detach"
    )
    print()
    print(style.heading("=== Snapshot ==="))
    print(f"  saved: {_path_row(data['_jsonPath'])}")
    print(f"  human readable: {_path_row(data['_mdPath'])}")
    print(f"  plugins in the snapshot: {len(data['plugins'])}")
    print_data_note(names)

    print()
    print(style.heading(f"=== Detaching {len(names)} plugins ==="))
    for name in names:
        print(f"    {style.dim('-')} {name}")
    if not args.yes:
        print()
        print(style.warn("  This is a destructive operation. Repeat with --yes "
                         "(no data is deleted)."))
        return 1
    remove_plugins(profile, names)

    after = read_profile(profile.directory)
    print()
    print(f"  plugin records left: {len(after.plugins)}")
    print(f"  bundle layers after detaching: {len(after.bundles)} "
          f"({', '.join(after.bundles[:4])}…)")
    return 0


def _partition(entries: list[dict], result, *, offline: bool):
    """Split snapshot entries into the compatible and the blocked ones.

    The analysis result takes priority: it includes the checks over the actual
    code (hard links to removed packages, client bundles). When the analysis could
    not obtain a manifest (the local artifact was deleted, the registry is
    unreachable), the manifest from the snapshot is used: it was taken BEFORE the
    operation and therefore remains the source of truth for plugins with a local
    source — those are not in the registry at all.
    """
    by_name = {plugin["name"]: plugin for plugin in result.plugins}
    ready, blocked = [], []
    for entry in entries:
        name = entry.get("name")
        plugin = by_name.get(name)
        if plugin is not None:
            status = plugin["status"]
            reason = plugin.get("reason")
            requirement = plugin.get("requirement")
            manifest = plugin.get("manifest")
            if not manifest:
                snapshot_manifest = analysis_mod.load_manifest_from_snapshot(entry)
                if snapshot_manifest is not None:
                    verdict = evaluate(result.target,
                                       declarations_for(snapshot_manifest, result.host_packages))
                    requirement = verdict.requirement
                    # Code checks outrank declarations: a "NO" from the code is not overridden.
                    if status != STATUS_INCOMPATIBLE:
                        status = verdict.status
                        reason = verdict.reason()
                    manifest = snapshot_manifest
            record = {
                "name": name,
                "version": plugin.get("version") or entry.get("version"),
                "spec": entry.get("spec") or plugin.get("spec"),
                "source": plugin.get("source"),
                "status": status,
                "reason": reason,
                "requirement": requirement,
                "empirical": plugin.get("empirical", False),
                "manifest": manifest,
            }
            record.update(analysis_mod.local_extras(plugin))
            if plugin.get("installable") is False:
                blocked.append({**record, "installable": False,
                                "reason": "nothing to install — the artifact of the local "
                                          "source was not found: "
                                          + str((plugin.get("local") or {}).get("path"))
                                          + ("; " + reason if reason else "")})
                continue
        else:
            manifest = analysis_mod.load_manifest_from_snapshot(entry)
            if manifest is None:
                manifest = analysis_mod.manifest_for_entry(entry, result.profile_dir, offline=offline)
            if manifest is None:
                blocked.append({**entry, "status": STATUS_UNKNOWN,
                                "reason": "neither present in the analysis nor a manifest in the snapshot"})
                continue
            verdict = evaluate(result.target, declarations_for(manifest, result.host_packages))
            record = {
                "name": name,
                "version": entry.get("version"),
                "spec": entry.get("spec"),
                "source": entry.get("source"),
                "status": verdict.status,
                "reason": verdict.reason(),
                "requirement": verdict.requirement,
                "manifest": manifest,
            }
        if accepted(record["status"]):
            ready.append({**record, "install": record["spec"]})
        else:
            blocked.append(record)
    return ready, blocked


def _promote_unknown(ready: list[dict], blocked: list[dict]):
    """Move the unconfirmed plugins into the install list (opt-in).

    Unconfirmed means "no declarations at all, code checks clean" — such a plugin
    is neither proven compatible nor proven broken. Installing it is a deliberate
    choice (``--install-unknown``): the post-check afterwards re-runs the code
    checks against what was actually installed and reports the failures.

    Two kinds of entry stay blocked:

    * a proven incompatibility (the code checks found a hard breakage);
    * a plugin whose local artifact is gone — there is nothing to install, pnpm
      would fail.
    """
    promoted = [item for item in blocked
                if item["status"] != STATUS_INCOMPATIBLE and item.get("installable", True)]
    remaining = [item for item in blocked
                 if item["status"] == STATUS_INCOMPATIBLE or item.get("installable", True) is False]
    return ready + promoted, remaining, promoted


def _install_specs(ready: list[dict], result, *, update: bool, offline: bool,
                   pinned: dict[str, str] | None = None) -> list[str]:
    """Specifiers to install.

    npm packages are installed at an EXACT version rather than at the recorded
    range: a range like ``^0.3.16`` would pull the newest version on
    reinstallation, and that one may require a different core (a newer release can
    raise its own minimum core). ``--update`` raises the version
    upwards only, and only when the new version passes the compatibility check.

    ``pinned`` carries decisions already taken while the update was selected: those
    plugins are installed at the version that was checked instead of being recomputed
    here, so the plan the reader approved and the artifact that gets installed cannot
    disagree.
    """
    pinned = pinned or {}
    specs = []
    for item in ready:
        name = item["name"]
        recorded = item.get("spec") or name
        version = item.get("version")
        if item.get("source") == "npm":
            chosen = pinned.get(name)
            if chosen is None and update:
                chosen = analysis_mod.best_compatible_version(
                    name, result, offline=offline, minimum=version
                )
            # Always an EXACT version rather than a range: a range like ^0.3.16
            # would pull the newest version on reinstallation, and that one may
            # already require a different core.
            spec = f"{name}@{chosen or version or recorded}"
        else:
            spec = recorded
        item["install"] = spec
        specs.append(spec)
    return specs


def _is_newer(candidate: str | None, base: str | None) -> bool:
    """Strict semver upgrade test: false when either side is not a version."""
    if not candidate or not base:
        return False
    if semver.parse_version(candidate) is None or semver.parse_version(base) is None:
        return False
    return semver.compare_version(candidate, base) > 0


def _update_candidate(item: dict, result, *, offline: bool):
    """The version this plugin could be updated to, and whether it is confirmed.

    Returns ``(version, confirmed)`` or ``None`` when there is no update. Two kinds of
    update are told apart:

    * **confirmed** — the newest version that evaluates ``compatible`` on this core,
      which is exactly the version the install list would use;
    * **not confirmed** — a newer version exists, but nothing proves it compatible
      here; installing it is a deliberate choice.

    A version the code checks proved INCOMPATIBLE is never a candidate: no flag
    installs it. Only an npm source can have an update — a local or git source is
    reinstalled from its own specifier and is never compared with a registry version,
    so no update can be established for it.
    """
    name = item.get("name")
    if not name or item.get("source") != "npm":
        return None
    installed = item.get("version")
    confirmed = analysis_mod.best_compatible_version(
        name, result, offline=offline, minimum=installed)
    if _is_newer(confirmed, installed):
        return confirmed, True
    latest = item.get("latest")
    if _is_newer(latest, installed) and item.get("latest_status") != STATUS_INCOMPATIBLE:
        return latest, False
    return None


def _update_skip_reason(item: dict, *, offline: bool) -> str:
    """Why a plugin has nothing to update to — phrased for the reader of the report."""
    source = item.get("source")
    if source != "npm":
        if source:
            return (f"a {source} source — reinstalled from its own specifier, so there is "
                    "no registry version to compare")
        return "no source recorded — nothing to compare"
    if offline:
        return "the registry was not queried (--offline)"
    return "no newer version is published"


def _update_plan(names: list[str], result, *, offline: bool, allow_unconfirmed: bool):
    """Split a selection into what gets updated, what is held back and what has none.

    Returns ``(updating, held, untouched)``. ``updating`` is a list of
    ``(name, version, confirmed)`` for the plugins that will actually be installed at a
    new version; ``held`` are the plugins a newer version exists for but that is not
    installable — unconfirmed without the opt-in, or proven incompatible; ``untouched``
    are the ones with nothing newer at all. Every name lands in exactly one list, and
    the reasons travel with them, so the report is auditable rather than a silent
    narrowing.
    """
    by_name = {plugin["name"]: plugin for plugin in result.plugins}
    updating: list[tuple[str, str, bool]] = []
    held: list[dict] = []
    untouched: list[tuple[str, str | None, str]] = []
    for name in names:
        item = by_name.get(name)
        if item is None:
            untouched.append((name, None, "not part of the compatibility analysis"))
            continue
        installed = item.get("version")
        candidate = _update_candidate(item, result, offline=offline) \
            if item.get("source") == "npm" else None
        if candidate is None:
            latest = item.get("latest")
            if item.get("source") == "npm" and _is_newer(latest, installed):
                held.append({
                    "name": name, "installed": installed, "latest": latest,
                    "installable": False,
                    "reason": f"{latest} is published but proven incompatible with this core",
                })
            else:
                untouched.append((name, installed,
                                  _update_skip_reason(item, offline=offline)))
            continue
        version, confirmed = candidate
        if confirmed or allow_unconfirmed:
            updating.append((name, version, confirmed))
        else:
            held.append({
                "name": name, "installed": installed, "latest": version,
                "installable": True,
                "reason": f"{version} is published but not confirmed for this core",
            })
    return updating, held, untouched


def _apply_pins(ready: list[dict], blocked: list[dict], pins: dict[str, str]):
    """Move the plugins with a chosen update into the install list at that version.

    The version being INSTALLED is what was checked, not the copy that is there right
    now: a plugin whose installed copy is undeclared can still have a checked update,
    and judging that one by the old copy would hold it back instead of upgrading it.
    Everything that is not pinned keeps its own verdict.
    """
    if not pins:
        return ready, blocked
    kept_ready: list[dict] = []
    kept_blocked: list[dict] = []

    def move(items: list[dict], keep: list[dict]) -> None:
        for item in items:
            pinned = pins.get(item.get("name"))
            if pinned is None:
                keep.append(item)
                continue
            item["install"] = f"{item['name']}@{pinned}"
            item["updated_to"] = pinned
            kept_ready.append(item)

    move(ready, kept_ready)
    move(blocked, kept_blocked)
    return kept_ready, kept_blocked


def _print_update_plan(updating: list[tuple[str, str, bool]], held: list[dict],
                       untouched: list[tuple[str, str | None, str]], *,
                       install_unknown: bool) -> None:
    """The update decision, plugin by plugin."""
    print()
    print(style.heading(f"=== To update ({len(updating)}) ==="))
    for name, version, confirmed in updating:
        note = ""
        if not confirmed:
            note = style.warn("  not confirmed for this core — installed because of "
                              "--install-unknown, checked afterwards")
        print(f"    {style.good('↑')} {name} → {style.subheading(version)}{note}")
    if not updating:
        print(style.dim("    none"))
    if held:
        print()
        print(style.heading(f"=== Held back — a newer version exists ({len(held)}) ==="))
        for item in held:
            line = (f"    · {item['name']} {item['installed'] or '—'} → {item['latest']}: "
                    f"{item['reason']}")
            if item["installable"] and not install_unknown:
                line += (style.dim(" — pass --install-unknown to install it anyway, with a "
                                   "post-install code check"))
            print(line)
    if untouched:
        print()
        print(style.heading(f"=== Left alone — nothing to update ({len(untouched)}) ==="))
        for name, version, reason in untouched:
            print(f"    · {name} {style.dim(version or '—')}: {reason}")


def cmd_attach(args) -> int:
    profile = load_profile(args)
    snapshot_path = Path(args.from_file) if args.from_file else snapshot.latest_snapshot()
    if snapshot_path is None or not Path(snapshot_path).is_file():
        raise SystemExit("no snapshot — run detach first (or pass --from)")

    data = snapshot.read_json_file(Path(snapshot_path))
    entries = data.get("plugins", [])
    if args.only:
        wanted = set(args.only)
        entries = [entry for entry in entries if entry.get("name") in wanted]

    print()
    print(style.heading("=== Attach from snapshot ==="))
    print(f"  snapshot: {_path_row(snapshot_path)} (plugins: {len(entries)})")
    target = resolve_target(args)
    result = analyse_for(args, profile, target)

    ready, blocked = _partition(entries, result, offline=args.offline)
    if args.install_unknown:
        ready, blocked, promoted = _promote_unknown(ready, blocked)
        if promoted:
            print(report.warning(f"also installing unconfirmed plugins ({len(promoted)}) "
                                 "— with a post-install code check"))
    for item in blocked:
        if item.get("installable") is False:
            print(report.warning(f"{item['name']}: local artifact not found "
                                 f"({item.get('spec')}) — rebuild or restore the file "
                                 "and try again"))
    specs = _install_specs(ready, result, update=args.update, offline=args.offline)

    print()
    print(style.heading(f"=== To install ({len(specs)}) ==="))
    for spec in specs:
        print(f"    {style.good('+')} {spec}")
    print()
    print(style.heading(f"=== Deferred as incompatible ({len(blocked)}) ==="))
    for item in blocked:
        print(report.detail(f"{item['name']}: {item.get('reason') or ''}",
                            indent=4, mark=style.bad("-")))

    if args.dry_run:
        print()
        print(style.dim("  --dry-run: nothing was changed"))
        return 0
    if not args.yes:
        print()
        print(style.warn("  Repeat with --yes to install."))
        return 1

    if specs:
        add_plugins(profile, specs, log=lambda text: print(style.dim(f"  {text}")))

    # Post-check: the plugin code is in place now, so the bundle checks can run.
    verify = analyse_for(args, profile, target)
    failed = [plugin for plugin in verify.plugins
              if plugin["status"] == STATUS_INCOMPATIBLE]
    for plugin in failed:
        print(report.warning(f"{plugin['name']}: {plugin['reason']}"))

    entries_out = blocked + [analysis_mod.incompatible_entry(plugin) for plugin in failed]
    json_path, md_path = snapshot.write_incompatible(target, entries_out, note="attach")
    print()
    print(f"  incompatible list: {_path_row(json_path)}")
    print(f"  human readable:    {_path_row(md_path)}")

    if args.prune_failed and failed:
        names = [plugin["name"] for plugin in failed]
        print()
        print(style.heading(f"=== Detaching plugins that failed the post-check: "
                            f"{', '.join(names)} ==="))
        remove_plugins(profile, names, log=lambda text: print(style.dim(f"  {text}")))

    installed = read_profile(profile.directory)
    print()
    print(f"  plugins installed: {style.subheading(len(installed.plugins))}")
    return 0


def cmd_recheck(args) -> int:
    profile = load_profile(args)
    target = resolve_target(args)

    if args.file:
        path = Path(args.file)
    else:
        path, _ = snapshot.incompatible_path(target)
        if not path.is_file():
            # There may be no list for the current core — take the freshest one.
            fallback = snapshot.latest_incompatible()
            if fallback is not None:
                print(style.dim(f"  no list for core {target}; taking the most recent: "
                                f"{fallback.name}"))
                path = fallback
    if not path.is_file():
        print(style.warn(f"  incompatible list not found: {path}"))
        return 0

    entries = snapshot.load_incompatible(path)
    print()
    print(style.heading("=== Recheck ==="))
    print(f"  checking {len(entries)} plugins from {_path_row(path)} against core {target}")

    result = analyse_for(args, profile, target)
    still_blocked, now_ready = [], []

    for entry in entries:
        # Chain: manifest from the list → local artifact (file:/link:) → registry.
        # Hand-built plugins have no registry, so the artifact is read directly;
        # when it is missing the entry stays blocked.
        manifest = analysis_mod.manifest_for_entry(entry, profile.directory, offline=args.offline)
        if manifest is None:
            still_blocked.append({**entry, "reason": "manifest unavailable (neither in the list, "
                                                     "nor in a local artifact, nor in the registry)"})
            continue
        verdict = evaluate(target, declarations_for(manifest, result.host_packages))
        if verdict.status == STATUS_COMPATIBLE:
            now_ready.append({**entry, "status": verdict.status, "reason": "became compatible"})
        elif args.install_unknown and verdict.status != STATUS_INCOMPATIBLE:
            # Opt-in: an unconfirmed plugin may be installed as well, with the
            # post-check below judging it by the code that actually landed.
            now_ready.append({**entry, "status": verdict.status,
                              "reason": "unconfirmed — installed with a post-install code check"})
        else:
            still_blocked.append({**entry, "status": verdict.status, "reason": verdict.reason(),
                                  "requirement": verdict.requirement})

    print(f"  ready to install: {style.good(len(now_ready))}"
          + (style.dim("  (unconfirmed included)") if args.install_unknown else ""))
    print(f"  still incompatible: {style.bad(len(still_blocked))}")

    if now_ready and not args.dry_run:
        if not args.install:
            print()
            print(style.dim("  To install them, add --install"))
        elif not args.yes:
            print()
            print(style.warn("  Repeat with --install --yes"))
        else:
            specs = [item.get("spec") for item in now_ready if item.get("spec")]
            add_plugins(profile, specs, log=lambda text: print(style.dim(f"  {text}")))
            verify = analyse_for(args, profile, target)
            failed = {plugin["name"] for plugin in verify.plugins
                      if plugin["status"] == STATUS_INCOMPATIBLE}
            still_blocked += [
                {**item, "status": STATUS_INCOMPATIBLE,
                 "reason": "failed the post-install code check"}
                for item in now_ready if item["name"] in failed
            ]
            now_ready = [item for item in now_ready if item["name"] not in failed]

    json_path, md_path = snapshot.write_incompatible(target, still_blocked, note="recheck")
    print()
    print(f"  updated list: {_path_row(json_path)}")
    print(f"  human readable: {_path_row(md_path)}")
    return 0


def cmd_plugins(args) -> int:
    """Update the plugins that have a newer version, leaving the core alone.

    Only the plugins with an update are touched. A plugin with nothing newer is not
    detached, not reinstalled and not re-versioned — a run can therefore never leave
    one out of the profile — and the report names every one of them with the reason.
    Reinstalling the profile as a whole is what ``detach`` followed by ``attach`` is
    for.

    ``--only NAME…`` narrows the operation to the named plugins. The snapshot still
    records the WHOLE profile — a snapshot describes a state, not an operation.

    A newer version that evaluates ``compatible`` on this core is installed. A newer
    version that is merely unconfirmed — nothing declared to compare with, or not
    admitted by the declarations — is held back and reported, unless
    ``--install-unknown`` is given: that opt-in installs it and judges it afterwards
    by the post-install code check. A version the code checks proved incompatible is
    never installed, with or without the flag.

    ``--detach-first`` removes each plugin before installing its new version. By
    default the new version is installed over the current copy, which is what ``dsh``
    itself does and what keeps a failed install from leaving the plugin missing.
    """
    profile = load_profile(args)
    target = core_version()
    if target is None:
        raise SystemExit("installed core not found — install or pass a core first")
    names = _selection(args, profile, what="plugins")
    if not names:
        print(style.dim("No plugins are recorded in the profile — nothing to update"))
        return 0
    install_unknown = bool(getattr(args, "install_unknown", False))
    detach_first = bool(getattr(args, "detach_first", False))
    # Selecting an update means comparing versions, so the registry is always consulted.
    args.update = True

    print()
    print(style.heading(f"=== Updating plugins only, on the current core {target} ==="))
    if len(names) != len(profile.plugins):
        print(f"  selected: {style.subheading(len(names))} of {len(profile.plugins)} plugins — "
              + ", ".join(names))
    result = analyse_for(args, profile, target)
    report.print_analysis(result, verbose=args.verbose)

    updating, held, untouched = _update_plan(
        names, result, offline=args.offline, allow_unconfirmed=install_unknown)
    _print_update_plan(updating, held, untouched, install_unknown=install_unknown)
    if not updating:
        print()
        print(style.dim("  Nothing to update — the profile is left as it is"))
        return 0

    # The plan is computed before the confirmation so that what is approved is what
    # runs: the dry run, the prompt and the operation see the same list.
    pins = {name: version for name, version, _ in updating}
    selected = set(pins)
    entries = [entry for entry in analysis_mod.snapshot_entries(profile)
               if entry.get("name") in selected]
    ready, blocked = _partition(entries, result, offline=args.offline)
    ready, blocked = _apply_pins(ready, blocked, pins)
    for item in blocked:
        # Every selected plugin has a version that was checked, so this is a safeguard
        # rather than a normal outcome.
        print(report.warning(f"{item['name']}: no installable version ({item.get('reason')}) "
                             "— it is left as it is"))
    specs = _install_specs(ready, result, update=True, offline=args.offline, pinned=pins)

    print()
    print(style.heading(f"=== To install ({len(specs)}) ==="))
    for spec in specs:
        print(f"    {style.good('+')} {spec}")
    print()
    print(style.dim("  installing in place — only the listed plugins change, and the current "
                    "copy is replaced only when the new version installs successfully"
                    if not detach_first else
                    "  detaching each plugin before installing its new version "
                    "(--detach-first)"))

    if args.dry_run:
        print()
        print(style.dim("  --dry-run: nothing was changed"))
        return 0
    if not args.yes:
        print()
        print(style.warn(f"  Repeat with --yes: {len(pins)} plugin(s) will be installed at a "
                         "newer version."))
        return 1

    data = snapshot.write_snapshot(profile, target, analysis_mod.snapshot_entries(profile),
                                   note="plugins-only")
    print()
    print(f"  snapshot: {_path_row(data['_jsonPath'])}")
    if detach_first:
        remove_plugins(profile, [name for name, _, _ in updating],
                       log=lambda text: print(style.dim(f"  {text}")))
    add_plugins(profile, specs, log=lambda text: print(style.dim(f"  {text}")))

    fresh = read_profile(profile.directory)
    # The list is rebuilt from the post-check over the WHOLE profile, so a narrowed
    # run cannot leave the list describing only the plugins it touched. Entries that
    # never made it into the profile stay in it: the post-check cannot see them.
    verify = analyse_for(args, fresh, target)
    entries_out = {item["name"]: item for item in blocked}
    for plugin in verify.plugins:
        if not accepted(plugin["status"]):
            entries_out[plugin["name"]] = analysis_mod.incompatible_entry(plugin)
    json_path, md_path = snapshot.write_incompatible(
        target, list(entries_out.values()), note="plugins-only")
    print()
    print(f"  incompatible list: {_path_row(json_path)}")
    print(f"  human readable:    {_path_row(md_path)}")
    return 0


def _realign_termux(layer_dir, target: str) -> None:
    """Put the Android corrections back after a core install, natives included.

    The patcher is idempotent and safe on a pristine tree — ``install.sh`` and
    ``fix-dsh-runtime.sh`` share one anchor-based patcher — so it runs
    unconditionally rather than first asking whether the core looks patched. It
    recompiles nothing, though: a version bump can replace ``node-pty``/``koffi``
    with fresh sources that carry no built addon, and only the layer's full
    installer builds those. So the natives are probed afterwards and the installer
    runs only when the probe really fails, never on every upgrade.
    """
    print(style.dim("  re-applying the Termux patches…"))
    code = termux_mod.realign(layer_dir, log=lambda text: print(style.dim(f"    {text}")))
    if code != 0:
        print(report.warning(f"  the Termux patcher exited with {code} — the core may be only "
                             "partly patched; run its install.sh for the detail"))
    ok, detail = termux_mod.natives_ok()
    if ok:
        print(style.good(f"  native addons load (sharp {detail})"))
        return
    print(report.warning(f"  native addons do not load: {detail}"))
    print(style.dim("  rebuilding them with the layer's installer (this takes several minutes)…"))
    code = termux_mod.rebuild(layer_dir, target, log=lambda text: print(style.dim(f"    {text}")))
    if code != 0:
        print(report.warning(f"  the installer exited with {code} — the natives may still be missing"))
        return
    ok, detail = termux_mod.natives_ok()
    if ok:
        print(style.good(f"  native addons load (sharp {detail})"))
    else:
        print(report.warning(f"  native addons still do not load: {detail}"))


def cmd_pipeline(args) -> int:
    """The full pipeline: check → snapshot → detach → (core upgrade) → install."""
    profile = load_profile(args)
    target = resolve_target(args)
    step = 1

    def head(title: str) -> None:
        nonlocal step
        print()
        print(style.heading(f"=== Step {step}. {title} ==="))
        step += 1

    head(f"Checking compatibility with core {target}")
    result = analyse_for(args, profile, target)
    report.print_analysis(result, verbose=args.verbose)
    blocked_preview = [p for p in result.plugins if not accepted(p["status"])]
    if blocked_preview:
        if args.install_unknown:
            print(report.warning(
                f"{len(blocked_preview)} plugins are not confirmed for this version — only the "
                "proven-incompatible ones are held back (--install-unknown is on): the rest are "
                "installed and judged by the post-install code check."))
        else:
            print(report.warning(
                f"{len(blocked_preview)} plugins are not confirmed for this version — they will "
                "go into the incompatible list and will not be installed "
                "(add --install-unknown to install them with a post-check)."))

    if args.dry_run:
        head("Stop: --dry-run")
        print(style.dim("  Nothing was changed."))
        return 0
    if not args.yes:
        head("Stop: --yes is required")
        print(style.warn("  The pipeline detaches and installs plugins. Repeat with --yes."))
        return 1

    head("Snapshot and plugin detaching")
    data = snapshot.write_snapshot(profile, core_version(), analysis_mod.snapshot_entries(profile),
                                   note="pipeline")
    print(f"  snapshot: {_path_row(data['_jsonPath'])}")
    names = [plugin.name for plugin in profile.plugins]
    print_data_note(names)
    remove_plugins(profile, names, log=lambda text: print(style.dim(f"  {text}")))

    head("Core upgrade")
    current = core_version()
    command = f"npm i -g {DSH_PACKAGE}@{target}"
    print(f"  currently installed: {current or '—'}")
    print(f"  command: {style.command(command)}")

    # On Termux the npm command is only half the upgrade: it restores the pristine
    # upstream tree, which does not run on Android. The layer puts the corrections
    # back, and it has to happen BEFORE the plugin installation — a plugin judged
    # against an unpatched core is judged against a core that cannot write a file
    # on this platform, so the post-check would blame the plugins for the platform.
    layer_dir = None
    if termux_mod.active(args):
        layer_dir = termux_mod.ensure_layer(
            getattr(args, "termux_dir", None),
            clone=not args.offline and not args.no_clone,
            log=lambda text: print(style.dim(f"  {text}")))
        if layer_dir is not None:
            print(f"  termux layer: {_path_row(str(layer_dir))}")
            validated = termux_mod.validated_version(layer_dir)
            if validated and validated != target:
                print(report.warning(
                    f"  the layer validates {validated}, not {target}: its patcher matches "
                    "upstream by exact anchors and may refuse this version"))
        else:
            if getattr(args, "termux_dir", None):
                print(report.warning(
                    f"  {args.termux_dir} is not a Termux layer — expected "
                    f"{termux_mod.REALIGN_RELATIVE} and {termux_mod.PATCHER_RELATIVE} in it"))
            else:
                print(report.warning(
                    "  Termux detected, but no patch layer is available — npm will leave a "
                    "pristine tree that does not run here."))
                print(style.dim(f"  fetch it: git clone {termux_mod.LAYER_REPO}"))

    if args.run_core_upgrade:
        print(style.dim("  running (--run-core-upgrade)…"))
        completed = subprocess.run(command, shell=True, check=False)
        if completed.returncode != 0:
            print(report.warning(f"core upgrade exited with code {completed.returncode}; "
                                 "continuing the plugin installation against what is there"))
        after = core_version()
        print(f"  version after the upgrade: {after or '—'}")
        if after != target:
            print(report.warning(f"expected {target}. The plugin installation will run "
                                 "against the actual version."))
        if layer_dir is not None:
            _realign_termux(layer_dir, target)
    else:
        print(style.dim("  Run this command yourself (it needs access outside the workspace), then:"))
        # The layer's steps are inserted between npm and attach, which is the seam
        # that makes a Termux upgrade correct; without a layer there is nothing to
        # number and the line reads as it did before this module existed.
        # The loop variable must not be `step`: that name is the step counter the
        # `head()` closure above writes through, and shadowing it breaks every later
        # heading.
        attach = invocation_mod.command(
            "attach", "--yes", *(["--update"] if args.update else []), profile=args.profile)
        words = termux_mod.commands(layer_dir, target)
        if words:
            for number, line in enumerate([*words, attach], start=1):
                print(f"    {number}. " + style.command(line))
        else:
            print("    " + style.command(attach))

    if args.skip_attach:
        head("Done (installation deferred)")
        return 0

    head("Plugin installation")
    fresh = read_profile(profile.directory)
    ready, blocked = _partition(data["plugins"], result, offline=args.offline)
    if args.install_unknown:
        ready, blocked, promoted = _promote_unknown(ready, blocked)
        if promoted:
            print(report.warning(f"also installing unconfirmed plugins ({len(promoted)}) "
                                 "— the post-check below judges them by their code"))
    specs = _install_specs(ready, result, update=args.update, offline=args.offline)
    print(f"  to install: {len(specs)}")
    for spec in specs:
        print(f"    {style.good('+')} {spec}")
    if specs:
        add_plugins(fresh, specs, log=lambda text: print(style.dim(f"  {text}")))

    head("Post-check")
    verify = analyse_for(args, fresh, target)
    report.print_analysis(verify)
    failed = [plugin for plugin in verify.plugins if plugin["status"] == STATUS_INCOMPATIBLE]
    json_path, md_path = snapshot.write_incompatible(
        target,
        blocked + [analysis_mod.incompatible_entry(plugin) for plugin in failed],
        note="pipeline",
    )
    print()
    print(f"  incompatible list: {_path_row(json_path)}")
    print(f"  human readable:    {_path_row(md_path)}")
    print()
    print(style.good("  Restart DSH (dsh web) and check the GUI."))
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def cmd_menu(args) -> int:
    """The interactive menu — also opened when the tool runs without arguments.

    The menu calls exactly the same :func:`cmd_*` functions as the plain CLI and
    builds the same Namespace for them: there is no separate "menu" logic that
    could drift away from the command line.
    """
    from dshupgrade import menu as menu_mod

    settings = menu_mod.Settings(
        profile=getattr(args, "profile", "web") or "web",
        core=getattr(args, "core", None),
        offline=bool(getattr(args, "offline", False)),
        no_clone=bool(getattr(args, "no_clone", False)),
        color=style.mode(),
        # Verbose output is more useful in the menu: the commands print the per-plugin breakdown.
        verbose=True,
        state_dir=getattr(args, "state_dir", None),
        checkouts=getattr(args, "checkouts", paths.TEMP) or paths.TEMP,
        # A flag given on the command line wins for this run and is not written to
        # the settings file: `dsh_upgrade.py --profile other` must not silently
        # become a permanent choice.
        pinned=frozenset(getattr(args, "explicit", ()) or ()),
    )
    return menu_mod.run(settings=settings)


def build_parser() -> argparse.ArgumentParser:
    # The common options are accepted both before and after the subcommand: the
    # copy inside a subparser uses SUPPRESS defaults, otherwise it would wipe a
    # value passed before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default=argparse.SUPPRESS,
                        help="profile (web by default)")
    common.add_argument("--core", default=argparse.SUPPRESS, help="target core version")
    common.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                        help="do not touch the network (cache only)")
    common.add_argument("--no-clone", action="store_true", default=argparse.SUPPRESS,
                        help="do not clone the version checkout")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine readable output")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="full detail for every plugin")
    common.add_argument("--color", choices=style.MODES, default=argparse.SUPPRESS,
                        help="colorize the output: auto (default), always, never")
    common.add_argument("--state-dir", default=argparse.SUPPRESS, help="state directory")
    common.add_argument("--checkouts", default=argparse.SUPPRESS, metavar="LOCATION",
                        help="where the core version checkouts live: 'temp' (default: under "
                             "the system temp dir, reused between runs, cleared on reboot), "
                             "'keep' ($DSH_HOME/checkouts) or a directory to keep them in")
    common.add_argument("--prune-checkouts", action="store_true", default=argparse.SUPPRESS,
                        help="delete the temporary checkouts and continue (or exit when no "
                             "command was given)")
    # The Termux correction is on by default wherever it is needed, so both switches
    # exist only to override the detection: `--no-termux` for a deliberate pristine
    # core, `--termux` to exercise the layer where the detection cannot see it.
    common.add_argument("--termux", dest="termux", action="store_true", default=argparse.SUPPRESS,
                        help="force the Termux/Android correction on (default: detected)")
    common.add_argument("--no-termux", dest="termux", action="store_false",
                        default=argparse.SUPPRESS,
                        help="do not apply the Termux patch layer after a core upgrade")
    common.add_argument("--termux-dir", default=argparse.SUPPRESS, metavar="DIR",
                        help="where the Termux patch layer lives (default: discovered, then "
                             "cloned from the fork)")

    parser = argparse.ArgumentParser(
        prog="dsh-upgrade",
        parents=[common],
        description="Upgrade the DSH core and the profile plugins with DSH switched off. "
                    "Without arguments an interactive menu opens.",
        epilog="Tables wrap long cells onto continuation lines instead of truncating them, "
               "so a full reason is always visible.",
    )
    # NOTE: the defaults are applied after parsing (see apply_defaults), not through
    # parser.set_defaults(). argparse shares the action objects of a `parents`
    # parser between the top-level parser and every subparser, so set_defaults()
    # would overwrite their SUPPRESS defaults and silently discard a flag given
    # before the subcommand (`--profile other status` would still use "web").
    # The subcommand is optional: without it the menu opens.
    sub = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    add("core-versions", "available core versions and tags").set_defaults(func=cmd_core_versions)
    status = add("status", "core and profile state")
    status.add_argument("--verify", action="store_true",
                        help="fill in the 'loads' column: import every installed plugin "
                             "(probes only when what it measured has changed)")
    status.add_argument("--loader", action="store_true",
                        help="also print the effective loader tree: every row, what mounts "
                             "it and who disabled it")
    status.add_argument("--summary", action="store_true",
                        help="print one line of counts instead of the full report")
    status.set_defaults(func=cmd_status)

    runtime = add("verify", "do the installed plugins actually load — and do they do "
                            "anything? (imports each one, calls apply(), then calls every "
                            "route handler it registered)")
    runtime.add_argument("--cached", action="store_true",
                         help="reuse the cached verdicts instead of probing again")
    runtime.add_argument("--live", action="store_true",
                         help="also GET every route the probes registered on the running "
                              "DSH: a path that answers is proof the plugin applied")
    runtime.add_argument("--web-url", dest="web_url", default=None,
                         help=f"where 'dsh web' serves (default: {verify_mod.DEFAULT_WEB_URL})")
    runtime.add_argument("--no-handlers", dest="no_handlers", action="store_true",
                         help="do not call the route handlers apply() registered — a weaker "
                              "probe that cannot see a handler broken at request time; the "
                              "result is not cached")
    runtime.add_argument("--loader", action="store_true",
                         help="also print the effective loader tree, with the rows the "
                              "shadowed plugins draw into marked")
    comparison = runtime.add_mutually_exclusive_group()
    comparison.add_argument("--summary", action="store_true",
                            help="print one line of counts instead of the full report")
    comparison.add_argument("--diff", metavar="PATH",
                            help="compare with a verified JSON saved earlier and print only "
                                 "the differences (a state/verified-<profile>.json file)")
    runtime.set_defaults(func=cmd_verify)

    plan = add("plan", "overview of all new core versions: what happens to the plugins")
    plan.add_argument("--limit", type=int, default=6, help="how many recent new versions to look at")
    plan.add_argument("--all", action="store_true", help="every version newer than the installed one")
    plan.set_defaults(func=cmd_plan)

    check = add("check", "compatibility matrix for a core version")
    check.add_argument("--update", action="store_true", help="also check the newest plugin versions")
    check.add_argument("--summary", action="store_true",
                       help="print one line of counts instead of the full report")
    check.set_defaults(func=cmd_check)

    inspect = add("inspect", "check an artifact that is NOT installed yet (directory or tarball)")
    inspect.add_argument("artifact", help="path to the plugin directory or to its .tgz "
                                          "(file:/link: specifiers are accepted as well)")
    inspect.add_argument("--since", help="older core version that shipped the packages the "
                                         "artifact may still reference (default: the installed core)")
    inspect.set_defaults(func=cmd_inspect)

    detach = add("detach", "snapshot and detach all plugins")
    detach.add_argument("--only", nargs="+", metavar="NAME",
                        help="detach only the listed plugins; the snapshot still records the "
                             "whole profile")
    detach.add_argument("--yes", action="store_true", help="confirm the operation")
    detach.add_argument("--dry-run", action="store_true", help="only show the plan")
    detach.set_defaults(func=cmd_detach)

    attach = add("attach", "install the compatible plugins from a snapshot")
    attach.add_argument("--from", dest="from_file", help="snapshot file (the freshest one by default)")
    attach.add_argument("--only", nargs="+", metavar="NAME",
                        help="restore only the listed plugins from the snapshot")
    attach.add_argument("--update", action="store_true", help="install the newest compatible version")
    attach.add_argument("--install-unknown", action="store_true",
                        help="also install unconfirmed (undeclared) plugins — with a post-install code check")
    attach.add_argument("--prune-failed", action="store_true",
                        help="detach the plugins that fail the post-check")
    attach.add_argument("--yes", action="store_true")
    attach.add_argument("--dry-run", action="store_true")
    attach.set_defaults(func=cmd_attach)

    recheck = add("recheck", "re-check the incompatible list")
    recheck.add_argument("--file", help="list file (state/incompatible-<core>.json by default)")
    recheck.add_argument("--install", action="store_true",
                         help="also install the plugins that became compatible")
    recheck.add_argument("--install-unknown", action="store_true",
                         help="with --install, also install the unconfirmed (undeclared) ones "
                              "— with a post-install code check")
    recheck.add_argument("--update", action="store_true")
    recheck.add_argument("--yes", action="store_true")
    recheck.add_argument("--dry-run", action="store_true")
    recheck.set_defaults(func=cmd_recheck)

    plugins = add("plugins", "update the plugins that have a newer version")
    plugins.add_argument("--only", nargs="+", metavar="NAME",
                         help="update only the listed plugins; the rest of the profile is "
                              "left untouched")
    plugins.add_argument("--update", action="store_true",
                         help="install the newest compatible version — always on for this "
                              "command, accepted for symmetry with the others")
    plugins.add_argument("--install-unknown", action="store_true",
                         help="also install a newer version that is not confirmed for this "
                              "core — with a post-install code check")
    plugins.add_argument("--detach-first", dest="detach_first", action="store_true",
                         help="remove each plugin before installing its new version; by "
                              "default the new version is installed over the current copy")
    plugins.add_argument("--yes", action="store_true")
    plugins.add_argument("--dry-run", action="store_true")
    plugins.set_defaults(func=cmd_plugins)

    pipeline = add("pipeline", "the full upgrade pipeline")
    pipeline.add_argument("--update", action="store_true", help="install the newest compatible versions")
    pipeline.add_argument("--install-unknown", action="store_true",
                          help="also install the unconfirmed (undeclared) plugins — off by "
                               "default; the post-check judges them by their code")
    pipeline.add_argument("--run-core-upgrade", action="store_true",
                          help="run npm i -g yourself (needs access outside the workspace)")
    pipeline.add_argument("--skip-attach", action="store_true", help="stop after detaching")
    pipeline.add_argument("--yes", action="store_true")
    pipeline.add_argument("--dry-run", action="store_true")
    pipeline.set_defaults(func=cmd_pipeline)

    interactive = add("menu", "interactive menu (also opened without arguments)")
    interactive.set_defaults(func=cmd_menu)

    return parser


#: Defaults for the common options. They cannot live in the parsers themselves
#: (see the note in build_parser), so they are filled in after parsing. The list is
#: the same one the settings file uses (see :mod:`dshupgrade.config`).
DEFAULTS = dict(config_mod.DEFAULTS)


def apply_defaults(args, saved: dict | None = None):
    """Fill in every common option the user did not pass.

    Priority: the command-line flag (already in ``args``) > the saved settings >
    the built-in default. ``saved`` is passed explicitly by :func:`main`; when it
    is omitted nothing from the settings file is used, which keeps the parser
    testable on its own.
    """
    saved = saved or {}
    for name, value in DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, saved.get(name, value))
    return args


def _apply_checkouts(args, saved: dict) -> None:
    """Pin the checkout location, honouring **flag > environment > settings file**.

    The settings-file value may not be pinned blindly: ``apply_defaults`` has already
    written it (or the built-in ``temp``) into ``args.checkouts``, and an
    unconditional pin would then shadow ``DSH_CHECKOUTS_ROOT`` — the environment must
    beat the file. An empty pin lets :mod:`dshupgrade.paths` decide (the environment,
    then the temporary default).
    """
    if "checkouts" in args.explicit:
        paths.set_checkouts_setting(args.checkouts)
        return
    if os.environ.get("DSH_CHECKOUTS_ROOT", "").strip():
        paths.set_checkouts_setting(None)
        return
    paths.set_checkouts_setting(saved.get("checkouts") or None)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Everything the user actually typed: argparse keeps other options out of the
    # namespace thanks to the SUPPRESS defaults, so this is exactly the flag set.
    args.explicit = frozenset(vars(args))
    saved = config_mod.load()
    args = apply_defaults(args, saved)
    style.set_mode(args.color)
    if args.state_dir:
        os.environ["DSH_UPGRADE_STATE"] = args.state_dir
    _apply_checkouts(args, saved)

    if getattr(args, "prune_checkouts", False):
        removed = paths.prune_checkouts()
        if removed:
            print("removed: " + ", ".join(str(path) for path in removed))
        else:
            print(f"nothing to remove ({paths.temp_checkouts_root()} does not exist)")
        if getattr(args, "func", None) is None:
            return 0

    target = getattr(args, "func", None)
    if target is None:
        # No subcommand: open the menu (on a non-TTY — the action map).
        return cmd_menu(args)
    try:
        return target(args)
    except KeyboardInterrupt:
        print(style.warn("\ninterrupted"))
        return 130
    except RuntimeError as error:
        print(style.bad(f"error: {error}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
