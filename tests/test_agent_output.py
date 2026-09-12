"""Agent-oriented report fields: reason codes, blocking, summary, diff, confidence.

Each feature is opt-in and additive: the default reports and the existing JSON
fields stay as they are, while a consumer that asks for a machine-readable value
gets one it can branch on — a stable identifier instead of prose, an explicit
statement about whether a plugin stops the pipeline, a comparison with an earlier
result, one line of counts, and the confidence a finding is worth.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import analysis as analysis_mod  # noqa: E402
from dshupgrade import codes as codes_mod  # noqa: E402
from dshupgrade import effects  # noqa: E402
from dshupgrade import verify as verify_mod  # noqa: E402
from dshupgrade import wire as wire_mod  # noqa: E402
from dshupgrade.compat import (  # noqa: E402
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
    STATUS_UNKNOWN,
    InlinePolicy,
)
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402

NAME = "sample-plugin"
HOST = {"@deepseek-ai/dsh"}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def write_package(root: Path, manifest: dict, files: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    for relative, text in (files or {}).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def analysis_for(root: Path, *, host=None, seed=None, removed=None,
                 policy=None) -> analysis_mod.Analysis:
    return analysis_mod.Analysis(
        target="0.1.5-rc.2",
        current_core="0.1.1-rc.2",
        checkout=Path(root),
        host_packages=host if host is not None else set(HOST),
        seed_words=seed if seed is not None else set(),
        removed=removed if removed is not None else [],
        inline_policy=policy,
    )


def entry_for(root: Path, manifest: dict, files: dict[str, str], analysis) -> dict:
    write_package(Path(root), manifest, files)
    plugin = PluginEntry(name=NAME, spec=f"link:{root}", source="link", installed=False,
                         version=manifest.get("version"), directory=None, in_bundles=False)
    return analysis_mod._analyse_plugin(plugin, analysis, offline=True, check_updates=False,
                                        profile_dir=None)


def base_manifest(**extra) -> dict:
    manifest = {"name": NAME, "version": "1.0.0", "engines": {"dsh": "^0.1.5-rc.2"}}
    manifest.update(extra)
    return manifest


def analysis_entry(name: str, status: str, **extra) -> dict:
    entry = {
        "name": name, "spec": f"{name}@1.0.0", "source": "npm", "sourceLabel": "npm",
        "version": "1.0.0", "installed": True, "in_bundles": False, "local": None,
        "localVersion": None, "installable": True, "status": status, "reason": "",
        "reason_code": None, "requirement": None, "declarations": [], "removed_hits": [],
        "client_hits": [], "registration_hits": [], "declaration_hits": [], "inline_hits": [],
        "code_checked": True, "code_clean": True, "code_origin": None, "empirical": False,
        "latest": None, "latest_status": None, "npmLatest": None, "npmLatestStatus": None,
        "recommended": f"{name}@1.0.0", "manifest": None, "blocks_core_upgrade": False,
    }
    entry.update(extra)
    return entry


def make_profile(names: list[str]) -> Profile:
    plugins = [PluginEntry(name=name, spec=f"{name}@1.0.0", source="npm", installed=True,
                           version="1.0.0", directory=None, in_bundles=False)
               for name in names]
    return Profile(directory=Path("/nowhere"), name="web", dependencies={}, bundles=[],
                   plugins=plugins)


def handler_record(path: str, error: str | None = None, logs=None) -> dict:
    return {"path": path, "error": error, "logs": logs or [], "status": 200, "body": "",
            "timedOut": False, "ms": 4}


# --------------------------------------------------------------------------- #
# Task 1: stable reason codes
# --------------------------------------------------------------------------- #

class ReasonCodeTest(unittest.TestCase):
    """Every class of incompatibility carries a stable identifier."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _entry(self, manifest, files=None, **options) -> dict:
        analysis = analysis_for(self.root, **options)
        return entry_for(self.root / "pkg", manifest, files or {}, analysis)

    def test_peer_range_mismatch(self):
        entry = self._entry({"name": NAME, "version": "1.0.0",
                             "peerDependencies": {"@deepseek-ai/dsh": "^9.0.0"}})
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertEqual(entry["reason_code"], codes_mod.PEER_RANGE_MISMATCH)

    def test_engines_dsh_mismatch(self):
        entry = self._entry({"name": NAME, "version": "1.0.0",
                             "engines": {"dsh": "^9.0.0"}})
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertEqual(entry["reason_code"], codes_mod.ENGINES_DSH_MISMATCH)

    def test_removed_package_required(self):
        entry = self._entry(base_manifest(),
                            {"index.js": 'require("@deepseek-ai/dsh-gone");\n'},
                            removed=["@deepseek-ai/dsh-gone"])
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertEqual(entry["reason_code"], codes_mod.REMOVED_PACKAGE_REQUIRED)

    def test_browser_module_table_miss(self):
        manifest = base_manifest(exports={"./client": "./client.js"},
                                 dsh={"client": {"platform": "web"}})
        entry = self._entry(manifest,
                            {"client.js": 'require("@deepseek-ai/dsh-nowhere/client");\n'},
                            seed={"react"})
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertEqual(entry["reason_code"], codes_mod.BROWSER_MODULE_TABLE_MISS)

    def test_duplicate_factory_registration(self):
        manifest = base_manifest(exports={"./client": "./client.js"},
                                 dsh={"client": {"platform": "web"}})
        bundle = ('__ModuleLoader__.load({ id: "@deepseek-ai/dsh-api-session-controller", '
                  'factory: () => {} });\n')
        entry = self._entry(manifest, {"client.js": bundle})
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertEqual(entry["reason_code"], codes_mod.DUPLICATE_FACTORY_REGISTRATION)

    def test_declaration_integrity_failure(self):
        manifest = base_manifest(dsh={"client": {"platform": "web"}})
        entry = self._entry(manifest, {"client.js": "export const x = 1;\n"})
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertEqual(entry["reason_code"], codes_mod.DECLARATION_INTEGRITY_FAILURE)

    def test_inline_purity_violation(self):
        manifest = base_manifest(exports={"./client": "./client.js"},
                                 dsh={"client": {"platform": "web"}})
        bundle = ("//#region node_modules/@deepseek-ai/dsh-api-session-controller/lib/client.js\n"
                  "export const y = 1;\n")
        policy = InlinePolicy(platform=set(),
                              client_rows={"@deepseek-ai/dsh-api-session-controller"})
        entry = self._entry(manifest, {"client.js": bundle}, policy=policy)
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertEqual(entry["reason_code"], codes_mod.INLINE_PURITY_VIOLATION)

    def test_undeclared_manifest(self):
        entry = self._entry({"name": NAME, "version": "1.0.0"},
                            {"index.js": "export const x = 1;\n"})
        self.assertEqual(entry["status"], STATUS_UNKNOWN)
        self.assertEqual(entry["reason_code"], codes_mod.UNKNOWN_DECLARATIONS)

    def test_a_compatible_plugin_has_no_code(self):
        entry = self._entry(base_manifest(), {"index.js": "export const x = 1;\n"})
        self.assertEqual(entry["status"], STATUS_COMPATIBLE)
        self.assertIsNone(entry["reason_code"])

    def test_an_unreadable_manifest_has_no_code(self):
        analysis = analysis_for(self.root)
        plugin = PluginEntry(name=NAME, spec=f"link:{self.root / 'nope'}", source="link",
                             installed=False, version=None, directory=None, in_bundles=False)
        entry = analysis_mod._analyse_plugin(plugin, analysis, offline=True,
                                             check_updates=False, profile_dir=None)
        self.assertIsNone(entry["reason_code"])
        self.assertIn("local source not found", entry["reason"])

    def test_primary_prefers_the_failure_that_is_independent_of_the_core(self):
        combined = [codes_mod.UNKNOWN_DECLARATIONS, codes_mod.PEER_RANGE_MISMATCH]
        self.assertEqual(codes_mod.primary(combined), codes_mod.PEER_RANGE_MISMATCH)
        self.assertIsNone(codes_mod.primary([]))
        self.assertIsNone(codes_mod.primary(["NOT_A_CODE"]))

    def test_wire_verdicts_map_to_their_codes(self):
        self.assertEqual(codes_mod.wire_codes([wire_mod.DEAD]),
                         [codes_mod.WIRE_ENDPOINT_DEAD])
        self.assertEqual(codes_mod.wire_codes([wire_mod.MISMATCH]),
                         [codes_mod.WIRE_METHOD_MISMATCH])
        self.assertEqual(codes_mod.wire_codes([wire_mod.OK, wire_mod.UNCHECKED]), [])


class InspectWireCodeTest(unittest.TestCase):
    """``inspect`` names a dead call when the declarations came out clean."""

    LEGACY = 'const t = "client-request";\nfetch("/api/session.list", { method: "session.list" });\n'
    MISMATCH = 'const t = "client-request";\nfetch("/api/session/list", { method: "session.diff" });\n'

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _run(self, client_code: str) -> dict:
        root = self.root / "artifact"
        write_package(root, {"name": NAME, "version": "1.0.0", "main": "lib/index.js"},
                      {"client.js": client_code})
        core = wire_mod.CoreEndpoints(endpoints=frozenset({"session/list"}))
        stub = analysis_mod.Analysis(target="0.1.5-rc.2", current_core="0.1.5-rc.2",
                                     checkout=None, plugins=[analysis_entry(NAME,
                                                                           STATUS_COMPATIBLE)])
        args = SimpleNamespace(artifact=str(root), core=None, since=None, offline=True,
                               no_clone=True, json=True, verbose=False, profile="web",
                               color="never", state_dir=None)
        with mock.patch.object(dsh_upgrade.wire_mod, "core_endpoints", return_value=core), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade.analysis_mod, "analyse", return_value=stub):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = dsh_upgrade.cmd_inspect(args)
        return code, json.loads(buffer.getvalue())

    def test_a_dead_endpoint_fills_the_reason_code(self):
        code, payload = self._run(self.LEGACY)
        self.assertEqual(code, 2)
        self.assertEqual(payload["plugins"][0]["reason_code"], codes_mod.WIRE_ENDPOINT_DEAD)

    def test_a_method_mismatch_has_its_own_code(self):
        code, payload = self._run(self.MISMATCH)
        self.assertEqual(code, 2)
        self.assertEqual(payload["plugins"][0]["reason_code"],
                         codes_mod.WIRE_METHOD_MISMATCH)

    def test_a_static_finding_outranks_the_wire_finding(self):
        core = wire_mod.CoreEndpoints(endpoints=frozenset({"session/list"}))
        entry = analysis_entry(NAME, STATUS_INCOMPATIBLE)
        entry["reason_code"] = codes_mod.PEER_RANGE_MISMATCH
        root = self.root / "artifact"
        write_package(root, {"name": NAME, "version": "1.0.0", "main": "lib/index.js"},
                      {"client.js": self.LEGACY})
        stub = analysis_mod.Analysis(target="0.1.5-rc.2", current_core="0.1.5-rc.2",
                                     checkout=None, plugins=[entry])
        args = SimpleNamespace(artifact=str(root), core=None, since=None, offline=True,
                               no_clone=True, json=True, verbose=False, profile="web",
                               color="never", state_dir=None)
        with mock.patch.object(dsh_upgrade.wire_mod, "core_endpoints", return_value=core), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade.analysis_mod, "analyse", return_value=stub):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                dsh_upgrade.cmd_inspect(args)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["plugins"][0]["reason_code"], codes_mod.PEER_RANGE_MISMATCH)


# --------------------------------------------------------------------------- #
# Task 2: does the plugin stop the core upgrade?
# --------------------------------------------------------------------------- #

class BlocksCoreUpgradeTest(unittest.TestCase):
    """A plugin-level incompatibility is deferred; only the core itself blocks."""

    def test_a_core_conflict_is_a_target_older_than_the_installed_core(self):
        self.assertIsNotNone(analysis_mod.core_conflict("0.1.5-rc.2", "0.1.1-rc.2"))
        self.assertIsNone(analysis_mod.core_conflict("0.1.1-rc.2", "0.1.5-rc.2"))
        self.assertIsNone(analysis_mod.core_conflict("0.1.5-rc.2", "0.1.5-rc.2"))
        self.assertIsNone(analysis_mod.core_conflict(None, "0.1.5-rc.2"))

    def test_an_incompatible_plugin_does_not_block_a_reachable_upgrade(self):
        analysis = analysis_mod.Analysis(target="0.1.5-rc.2", current_core="0.1.1-rc.2",
                                         checkout=None,
                                         plugins=[analysis_entry("a", STATUS_INCOMPATIBLE),
                                                  analysis_entry("b", STATUS_COMPATIBLE)])
        analysis_mod.apply_core_block(analysis)
        self.assertFalse(analysis.plugins[0]["blocks_core_upgrade"])
        self.assertFalse(analysis.plugins[1]["blocks_core_upgrade"])

    def test_a_core_conflict_marks_every_plugin_that_is_not_compatible(self):
        analysis = analysis_mod.Analysis(target="0.1.1-rc.2", current_core="0.1.5-rc.2",
                                         checkout=None, core_conflict="unreachable target",
                                         plugins=[analysis_entry("a", STATUS_INCOMPATIBLE),
                                                  analysis_entry("b", STATUS_UNKNOWN),
                                                  analysis_entry("c", STATUS_COMPATIBLE)])
        analysis_mod.apply_core_block(analysis)
        self.assertTrue(analysis.plugins[0]["blocks_core_upgrade"])
        self.assertTrue(analysis.plugins[1]["blocks_core_upgrade"])
        self.assertFalse(analysis.plugins[2]["blocks_core_upgrade"])

    def test_the_block_is_visible_in_the_check_document(self):
        result = analysis_mod.Analysis(
            target="0.1.1-rc.2", current_core="0.1.5-rc.2", checkout=None,
            core_conflict="unreachable target",
            plugins=[analysis_entry("a", STATUS_INCOMPATIBLE)])
        analysis_mod.apply_core_block(result)
        args = SimpleNamespace(core="0.1.1-rc.2", offline=True, no_clone=True, json=True,
                               summary=True, verbose=False, profile="web", color="never",
                               state_dir=None, update=False)
        with mock.patch.object(dsh_upgrade, "load_profile", return_value=make_profile(["a"])), \
             mock.patch.object(dsh_upgrade, "resolve_target", return_value="0.1.1-rc.2"), \
             mock.patch.object(dsh_upgrade, "analyse_for", return_value=result), \
             mock.patch.object(dsh_upgrade.snapshot, "write_incompatible",
                               return_value=(Path("/s/i.json"), Path("/s/i.md"))):
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                dsh_upgrade.cmd_check(args)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["incompatible"], 1)


# --------------------------------------------------------------------------- #
# Task 3: verify --diff
# --------------------------------------------------------------------------- #

class DiffTest(unittest.TestCase):
    """A comparison reports only what differs between two verification results."""

    def old_items(self) -> list[dict]:
        return [
            {"name": "example-plugin", "status": verify_mod.LOADS, "applied": True,
             "routes": [{"path": "/a"}, {"path": "/b"}, {"path": "/c"}]},
            {"name": "another-plugin", "status": verify_mod.FAILED, "detail": "boom"},
            {"name": "gone-plugin", "status": verify_mod.LOADS, "applied": True},
        ]

    def current(self) -> dict:
        return {
            "example-plugin": verify_mod.Probe("example-plugin", verify_mod.LOADS,
                                               applied=True, apply_error="boom"),
            "another-plugin": verify_mod.Probe("another-plugin", verify_mod.LOADS, applied=True),
            "new-plugin": verify_mod.Probe("new-plugin", verify_mod.LOADS, applied=True),
        }

    def test_only_the_changed_fields_are_reported(self):
        payload = verify_mod.diff_probes(self.old_items(), self.current())
        by_name = {item["plugin"]: item["fields"] for item in payload["changed"]}
        self.assertEqual(by_name["example-plugin"]["surface"],
                         {"old": "routes:3", "new": "apply!"})
        self.assertEqual(by_name["another-plugin"]["loads"],
                         {"old": "no", "new": "yes"})
        self.assertNotIn("loads", by_name["example-plugin"])
        self.assertEqual(payload["added"], ["new-plugin"])
        self.assertEqual(payload["removed"], ["gone-plugin"])
        self.assertEqual(payload["unchanged_count"], 0)

    def test_two_equal_results_report_no_change(self):
        payload = verify_mod.diff_probes(self.old_items(),
                                         {item["name"]: verify_mod.probe_from_payload(item)
                                          for item in self.old_items()})
        self.assertEqual(payload["changed"], [])
        self.assertEqual(payload["unchanged_count"], 3)

    def test_the_human_line_marks_a_restored_load(self):
        payload = verify_mod.diff_probes(self.old_items(), self.current())
        payload["since"] = "2026-09-12"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dsh_upgrade._print_diff(payload)
        text = buffer.getvalue()
        self.assertIn("=== Changes since 2026-09-12 ===", text)
        self.assertIn("loads:   no → yes  (fixed)", text)
        self.assertIn("surface: routes:3 → apply!", text)
        self.assertIn("new-plugin: added", text)
        self.assertIn("gone-plugin: removed", text)

    def test_an_unchanged_baseline_says_so(self):
        payload = verify_mod.diff_probes(self.old_items(),
                                         {item["name"]: verify_mod.probe_from_payload(item)
                                          for item in self.old_items()})
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dsh_upgrade._print_diff(payload)
        self.assertIn("no changes", buffer.getvalue())

    def test_the_saved_state_file_and_a_bare_list_are_both_accepted(self):
        with TemporaryDirectory() as tmp:
            wrapped = Path(tmp) / "verified.json"
            wrapped.write_text(json.dumps({"generated": "2026-09-12T10:00:00",
                                           "plugins": self.old_items()}), encoding="utf-8")
            generated, items = verify_mod.load_verified(wrapped)
            self.assertEqual(generated, "2026-09-12T10:00:00")
            self.assertEqual(len(items), 3)

            bare = Path(tmp) / "list.json"
            bare.write_text(json.dumps(self.old_items()), encoding="utf-8")
            self.assertEqual(verify_mod.load_verified(bare), (None, self.old_items()))

    def test_an_unreadable_baseline_is_an_error(self):
        with TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("[\"not a result\"]\n", encoding="utf-8")
            self.assertEqual(verify_mod.load_verified(broken), (None, []))

            empty = Path(tmp) / "empty.json"
            empty.write_text("", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_mod.load_verified(empty)

    def test_verify_diff_prints_only_the_comparison(self):
        with TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "old.json"
            baseline.write_text(json.dumps({"generated": "2026-09-12T00:00:00",
                                            "plugins": self.old_items()}), encoding="utf-8")
            profile = make_profile(["example-plugin", "another-plugin", "new-plugin"])
            args = SimpleNamespace(cached=True, no_handlers=False, live=False, web_url=None,
                                  json=False, summary=False, diff=str(baseline), loader=False,
                                  verbose=False, profile="web", color="never", state_dir=None)
            with mock.patch.object(dsh_upgrade, "load_profile", return_value=profile), \
                 mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
                 mock.patch.object(dsh_upgrade, "core_install_dir", return_value=Path("/core")), \
                 mock.patch.object(dsh_upgrade.verify_mod, "resolve", return_value=self.current()), \
                 mock.patch.object(dsh_upgrade, "profile_surfaces",
                                   return_value=(None, {}, set())), \
                 mock.patch.object(dsh_upgrade, "wire_surfaces", return_value={}):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = dsh_upgrade.cmd_verify(args)
        text = buffer.getvalue()
        self.assertIn("=== Changes since 2026-09-12 ===", text)
        self.assertNotIn("Runtime verification", text)
        self.assertEqual(code, 0)


# --------------------------------------------------------------------------- #
# Task 4: --summary
# --------------------------------------------------------------------------- #

class SummaryTest(unittest.TestCase):
    """One line of counts, and the same numbers as data."""

    def payload(self) -> dict:
        return dsh_upgrade.summary_payload(total=5, incompatible=2, unknown=1,
                                           wire_dead=1, handler_failures=0, exit_code=2)

    def test_the_line_names_every_counter(self):
        self.assertEqual(
            dsh_upgrade.summary_line(self.payload()),
            "5 plugins checked, 2 incompatible, 1 unknown, 1 wire-dead, 0 handler-failures")

    def test_json_mode_prints_the_object(self):
        args = SimpleNamespace(json=True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dsh_upgrade.emit_summary(self.payload(), args)
        self.assertEqual(json.loads(buffer.getvalue()), self.payload())

    def test_the_counters_follow_the_probes(self):
        probes = {
            "a": verify_mod.Probe("a", verify_mod.FAILED, "boom",
                                  handlers=[handler_record("/x",
                                                           "ReferenceError: X is not defined")]),
            "b": verify_mod.Probe("b", verify_mod.LOADS),
            "c": verify_mod.Probe("c", verify_mod.UNAVAILABLE, "no node"),
            "d": verify_mod.Probe("d", verify_mod.MISSING, "not installed"),
        }
        wire = {"b": [wire_mod.WireCall("b", "/api/x", "x", verdict=wire_mod.DEAD)]}
        profile = make_profile(["a", "b", "c", "d"])
        payload = dsh_upgrade._probes_summary(profile, probes, wire, 2)
        self.assertEqual(payload, {"total": 4, "incompatible": 1, "unknown": 2,
                                   "wire_dead": 1, "handler_failures": 1, "exit_code": 2})


class CheckSummaryTest(unittest.TestCase):
    """``check --summary`` counts the same plugins as the full report."""

    def _namespace(self, **overrides):
        values = dict(core="0.1.5-rc.2", offline=True, no_clone=True, json=True,
                      summary=True, verbose=False, profile="web", color="never",
                      state_dir=None, update=False)
        values.update(overrides)
        return SimpleNamespace(**values)

    def _result(self) -> analysis_mod.Analysis:
        return analysis_mod.Analysis(
            target="0.1.5-rc.2", current_core="0.1.5-rc.2", checkout=None,
            plugins=[analysis_entry("a", STATUS_INCOMPATIBLE),
                     analysis_entry("b", STATUS_INCOMPATIBLE),
                     analysis_entry("c", STATUS_UNKNOWN),
                     analysis_entry("d", STATUS_COMPATIBLE)])

    def _run(self, args, tmp: str) -> tuple[int, str]:
        with mock.patch.object(dsh_upgrade, "load_profile", return_value=make_profile([])), \
             mock.patch.object(dsh_upgrade, "resolve_target", return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade, "analyse_for", return_value=self._result()), \
             mock.patch.object(dsh_upgrade, "state_dir", return_value=Path(tmp)), \
             mock.patch.object(dsh_upgrade.snapshot, "write_incompatible",
                               return_value=(Path("/s/i.json"), Path("/s/i.md"))):
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                code = dsh_upgrade.cmd_check(args)
        return code, buffer.getvalue()

    def test_the_summary_matches_the_full_counts_and_the_exit_code(self):
        with TemporaryDirectory() as tmp:
            code, text = self._run(self._namespace(), tmp)
        payload = json.loads(text)
        self.assertEqual(payload, {"total": 4, "incompatible": 2, "unknown": 1,
                                   "wire_dead": 0, "handler_failures": 0, "exit_code": 2})
        self.assertEqual(code, 2)

    def test_without_the_flag_the_report_document_is_printed_instead(self):
        with TemporaryDirectory() as tmp:
            code, text = self._run(self._namespace(summary=False), tmp)
        payload = json.loads(text)
        self.assertEqual(code, 2)
        self.assertIn("plugins", payload)
        self.assertNotIn("total", payload)


# --------------------------------------------------------------------------- #
# Task 5: verdict vs lead
# --------------------------------------------------------------------------- #

class ConfidenceTest(unittest.TestCase):
    """Every finding says whether it is a verdict or a lead."""

    def setUp(self):
        self.row = effects.Row(id="ui-workspace", name="dsh-client-ui-workspace",
                               mounted_by="core")
        self.verdict_shadow = effects.Shadow("shadow-plugin", token="ui-workspace",
                                             row=self.row, evidence="package.json: names it",
                                             kind=effects.DECLARED)
        self.lead_shadow = effects.Shadow("lead-plugin", token="ui-workspace",
                                          row=self.row, evidence="client.js:5: a comment",
                                          kind=effects.REFERENCE)

    def test_a_declared_shadow_is_a_verdict_and_a_reference_is_a_lead(self):
        self.assertEqual(self.verdict_shadow.confidence, "verdict")
        self.assertEqual(self.lead_shadow.confidence, "lead")
        self.assertEqual(self.verdict_shadow.to_dict()["confidence"], "verdict")
        self.assertEqual(self.lead_shadow.to_dict()["confidence"], "lead")

    def test_a_classified_wire_call_is_a_verdict(self):
        dead = wire_mod.WireCall("p", "/api/x", "x", verdict=wire_mod.DEAD)
        unchecked = wire_mod.WireCall("p", "/api/x", "x", verdict=wire_mod.UNCHECKED)
        self.assertEqual(dead.to_dict()["confidence"], "verdict")
        self.assertEqual(unchecked.to_dict()["confidence"], "lead")

    def test_the_findings_list_separates_the_two(self):
        probes = {
            "handler-plugin": verify_mod.Probe(
                "handler-plugin", verify_mod.LOADS,
                handlers=[handler_record("/x", "ReferenceError: X is not defined"),
                          handler_record("/y", "TypeError: y is not a function")]),
        }
        shadows = {"shadow-plugin": [self.verdict_shadow, self.lead_shadow]}
        wire = {"wire-plugin": [wire_mod.WireCall("wire-plugin", "/api/x", "x",
                                                  verdict=wire_mod.DEAD,
                                                  note="no endpoint in this namespace")]}
        findings = dsh_upgrade.findings_payload(probes, shadows, wire)
        by_surface = {}
        for finding in findings:
            by_surface.setdefault(finding["surface"], []).append(finding)

        self.assertEqual(by_surface["handler!"][0]["confidence"], "verdict")
        self.assertEqual(by_surface["handler!"][0]["reason_code"],
                         codes_mod.HANDLER_REFERENCE_ERROR)
        self.assertEqual(by_surface["handler?"][0]["confidence"], "lead")
        self.assertEqual(by_surface["shadowed"][0]["confidence"], "verdict")
        self.assertEqual(by_surface["shadowed?"][0]["confidence"], "lead")
        self.assertEqual(by_surface["wire:404"][0]["confidence"], "verdict")
        self.assertEqual(by_surface["wire:404"][0]["reason_code"],
                         codes_mod.WIRE_ENDPOINT_DEAD)

    def test_an_explained_shadow_is_not_a_finding(self):
        explained = effects.Shadow("quiet", token="ui-workspace", row=self.row,
                                   evidence="client.js:5", kind=effects.REFERENCE,
                                   replaced_by="replacement")
        self.assertEqual(dsh_upgrade.findings_payload({}, {"quiet": [explained]}, {}), [])


# --------------------------------------------------------------------------- #
# Task 7: one JSON document on stdout
# --------------------------------------------------------------------------- #

class JsonDocumentTest(unittest.TestCase):
    """``--json`` puts exactly one document on stdout; the report moves to stderr."""

    def test_verify_writes_one_document_and_keeps_the_report_on_stderr(self):
        profile = make_profile(["a"])
        probes = {"a": verify_mod.Probe(
            "a", verify_mod.LOADS, applied=True,
            handlers=[handler_record("/x", "ReferenceError: X is not defined")])}
        args = SimpleNamespace(cached=True, no_handlers=False, live=False, web_url=None,
                               json=True, summary=False, diff=None, loader=False,
                               verbose=False, profile="web", color="never", state_dir=None)
        with mock.patch.object(dsh_upgrade, "load_profile", return_value=profile), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade, "core_install_dir", return_value=Path("/core")), \
             mock.patch.object(dsh_upgrade.verify_mod, "resolve", return_value=probes), \
             mock.patch.object(dsh_upgrade, "profile_surfaces",
                               return_value=(effects.Effective(), {}, set())), \
             mock.patch.object(dsh_upgrade, "wire_surfaces", return_value={}):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = dsh_upgrade.cmd_verify(args)

        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertIn("findings", payload)
        self.assertEqual([f["surface"] for f in payload["findings"]], ["handler!"])
        self.assertIn("Runtime verification", err.getvalue())

    def test_check_writes_one_document_with_the_old_fields_and_the_new_ones(self):
        with TemporaryDirectory() as tmp:
            result = analysis_mod.Analysis(
                target="0.1.5-rc.2", current_core="0.1.5-rc.2", checkout=None,
                notes=["note"], plugins=[analysis_entry("a", STATUS_INCOMPATIBLE)])
            args = SimpleNamespace(core="0.1.5-rc.2", offline=True, no_clone=True, json=True,
                                   summary=False, verbose=False, profile="web",
                                   color="never", state_dir=None, update=False)
            with mock.patch.object(dsh_upgrade, "load_profile", return_value=make_profile([])), \
                 mock.patch.object(dsh_upgrade, "resolve_target", return_value="0.1.5-rc.2"), \
                 mock.patch.object(dsh_upgrade, "analyse_for", return_value=result), \
                 mock.patch.object(dsh_upgrade, "state_dir", return_value=Path(tmp)), \
                 mock.patch.object(dsh_upgrade.snapshot, "write_incompatible",
                                   return_value=(Path("/s/i.json"), Path("/s/i.md"))):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = dsh_upgrade.cmd_check(args)

            payload = json.loads(out.getvalue())
            for field in ("target", "current", "counts", "removed", "notes", "plugins"):
                self.assertIn(field, payload)
            self.assertIn("state", payload)
            plugin = payload["plugins"][0]
            self.assertIn("reason_code", plugin)
            self.assertIn("blocks_core_upgrade", plugin)
            self.assertIn("Compatibility with core", err.getvalue())
            self.assertEqual(code, 2)

            saved = json.loads((Path(tmp) / "check-0.1.5-rc.2.json").read_text())
            self.assertNotIn("state", saved)
            self.assertIn("reason_code", saved["plugins"][0])

    def test_status_writes_one_document(self):
        profile = make_profile(["a"])
        probes = {"a": verify_mod.Probe("a", verify_mod.LOADS, applied=True)}
        args = SimpleNamespace(json=True, summary=False, loader=False, verify=False,
                               offline=True, profile="web", color="never", state_dir=None)
        with mock.patch.object(dsh_upgrade, "load_profile", return_value=profile), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade, "core_install_dir", return_value=Path("/core")), \
             mock.patch.object(dsh_upgrade, "_status_probes", return_value=probes), \
             mock.patch.object(dsh_upgrade, "profile_surfaces",
                               return_value=(None, {}, set())), \
             mock.patch.object(dsh_upgrade, "wire_surfaces", return_value={}):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                dsh_upgrade.cmd_status(args)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["plugins"][0]["name"], "a")


class ParserTest(unittest.TestCase):
    """The new flags are opt-in and reachable after the subcommand."""

    def test_summary_is_available_on_the_report_commands(self):
        parser = dsh_upgrade.build_parser()
        for command in ("status", "verify", "check"):
            with self.subTest(command=command):
                args = parser.parse_args([command, "--summary"])
                self.assertTrue(args.summary)

    def test_diff_takes_a_path_and_excludes_summary(self):
        parser = dsh_upgrade.build_parser()
        args = parser.parse_args(["verify", "--diff", "/tmp/old.json"])
        self.assertEqual(args.diff, "/tmp/old.json")
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(["verify", "--diff", "/tmp/old.json", "--summary"])

    def test_without_the_flags_nothing_is_asked_for(self):
        parser = dsh_upgrade.build_parser()
        args = parser.parse_args(["verify"])
        self.assertFalse(args.summary)
        self.assertIsNone(args.diff)


def option_strings(parser) -> set[str]:
    """Every long/short option the parser (and its subparsers) accepts."""
    found: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public walk
        found.update(action.option_strings)
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for sub in choices.values():
                found.update(option_strings(sub))
    return found


class AgentGuideTest(unittest.TestCase):
    """The documentation mentions only codes and flags that exist."""

    ROOT = Path(__file__).resolve().parent.parent
    CODE_ROW = re.compile(r"^\|\s*`([A-Z][A-Z_]+)`")
    FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")

    def _text(self, name: str) -> str:
        return (self.ROOT / name).read_text(encoding="utf-8")

    def _documented_codes(self, name: str) -> set[str]:
        """The codes of the reason-code table, and nothing else from the file."""
        lines = self._text(name).splitlines()
        start = next(index for index, line in enumerate(lines) if "Reason codes" in line)
        found: set[str] = set()
        for line in lines[start + 1:]:
            match = self.CODE_ROW.match(line)
            if match:
                found.add(match.group(1))
            elif found:
                break
        return found

    def test_the_reason_code_tables_list_exactly_the_known_codes(self):
        for name in ("AGENTS.md", "README.md"):
            with self.subTest(document=name):
                self.assertEqual(self._documented_codes(name), set(codes_mod.PRIORITY))

    def test_every_flag_the_agent_guide_names_exists(self):
        known = option_strings(dsh_upgrade.build_parser())
        named = set(self.FLAG.findall(self._text("AGENTS.md")))
        self.assertTrue(named, "AGENTS.md names no flag")
        self.assertLessEqual(named, known)

    def test_the_agent_guide_names_the_read_only_and_destructive_sets(self):
        text = self._text("AGENTS.md")
        for command in ("status", "check", "plan", "inspect", "verify", "core-versions",
                        "detach", "attach", "pipeline", "recheck", "plugins"):
            with self.subTest(command=command):
                self.assertIn(f"`{command}", text)
        for flag in ("--summary", "--diff", "--json", "--yes"):
            with self.subTest(flag=flag):
                self.assertIn(flag, text)


if __name__ == "__main__":
    unittest.main()
