"""Tests for the effective loader tree and the shadowed-surface detector.

``verify`` answers "did the entry import". These tests cover the other question —
"does the plugin do anything" — and, above all, the failure that prompted the
whole module: a client half that draws into a component the profile switched off.

The patch parser is tested against the shapes that actually occur in the wild, in
particular a ``!!js`` expression on ``disabled:``, an id-targeted disable in a later
bundle and a deep ``config:`` subtree whose keys must NOT be mistaken for row fields.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import effects  # noqa: E402
from dshupgrade import verify as verify_mod  # noqa: E402
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402


def make_bundle(root: Path, name: str, patch: str | None, *, store: Path | None = None):
    """A bundle package whose manifest declares a patch layer."""
    base = store or (root / "node_modules")
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "1.0.0"}
    if patch is not None:
        (directory / "cordis.patch.yml").write_text(patch, encoding="utf-8")
        manifest["dsh"] = {"bundle": {"patch": "./cordis.patch.yml"}}
    (directory / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def make_plugin(root: Path, name: str, manifest: dict, files: dict[str, str] | None = None):
    """An installed-looking plugin directory."""
    directory = root / "node_modules" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps({"name": name, **manifest}),
                                            encoding="utf-8")
    for relative, content in (files or {}).items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return directory


def profile_for(root: Path, bundles: list[str], plugins: list[PluginEntry]) -> Profile:
    return Profile(directory=root, name="web", dependencies={}, bundles=bundles,
                   plugins=plugins)


def entry(name: str, root: Path) -> PluginEntry:
    return PluginEntry(name=name, spec="^1.0.0", source="npm", installed=True,
                       version="1.0.0", directory=str(root / "node_modules" / name),
                       in_bundles=True)


# --------------------------------------------------------------------------- #
# The patch subset
# --------------------------------------------------------------------------- #

class ParsePatchTest(unittest.TestCase):
    def test_insert_rows_are_mounts_with_unquoted_names(self):
        ops = effects.parse_patch(
            "- insert:\n"
            "    - id: augmenter\n"
            "      name: 'sample-augmenter'\n"
            "    - id: other\n"
            "      name: \"other-plugin\"\n")
        self.assertEqual([op.kind for op in ops], ["mount", "mount"])
        self.assertEqual(ops[0].id, "augmenter")
        self.assertEqual(ops[0].name, "sample-augmenter")
        self.assertEqual(ops[1].name, "other-plugin")
        self.assertTrue(ops[0].enabled)

    def test_an_id_targeted_disable_is_a_set(self):
        ops = effects.parse_patch("- id: ui-workspace\n  disabled: true\n")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].kind, "set")
        self.assertEqual(ops[0].id, "ui-workspace")
        self.assertFalse(ops[0].enabled)

    def test_disabled_false_re_enables(self):
        ops = effects.parse_patch("- id: sample-subpath\n  name: sample-subpath/dsh\n"
                                  "  disabled: false\n")
        self.assertTrue(ops[0].enabled)
        self.assertEqual(ops[0].name, "sample-subpath/dsh")

    def test_a_js_expression_is_conditional_not_guessed(self):
        ops = effects.parse_patch(
            "- insert:\n"
            "    - id: sample-ui\n"
            "      name: 'sample-ui'\n"
            "      disabled: !!js \"[...ctx.loader.entries()].some((e) => !e.disabled)\"\n")
        self.assertTrue(ops[0].conditional)
        self.assertIsNone(ops[0].enabled)
        self.assertIn("ctx.loader.entries()", ops[0].condition)

    def test_a_config_subtree_is_not_read_as_row_fields(self):
        # Exactly the sample-subpath profile layer: a deep config whose body has its
        # own keys, and a nested list that is NOT another insert.
        ops = effects.parse_patch(
            "- id: sample-subpath\n"
            "  name: sample-subpath/dsh\n"
            "  config:\n"
            "    name: not-a-row\n"
            "    dbPath: !!js dshHomePath('sample-subpath/sample-subpath.db')\n"
            "    messageRetention:\n"
            "      keep: all\n"
            "      batchSize: 500\n"
            "    toolRows:\n"
            "      - id: also-not-a-row\n")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].kind, "set")
        self.assertEqual(ops[0].id, "sample-subpath")
        self.assertEqual(ops[0].name, "sample-subpath/dsh")

    def test_comments_are_stripped_but_a_hash_inside_quotes_survives(self):
        ops = effects.parse_patch(
            "- insert:\n"
            "    - id: a  # the first one\n"
            "      name: 'plugin#with-hash'\n")
        self.assertEqual(ops[0].id, "a")
        self.assertEqual(ops[0].name, "plugin#with-hash")

    def test_an_entry_without_a_name_falls_back_to_the_id(self):
        ops = effects.parse_patch("- insert:\n    - id: bare\n")
        self.assertEqual(ops[0].name, "bare")


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

class ResolveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.install = self.root / "install"
        self.install.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_later_layer_disables_a_row_and_is_named(self):
        make_bundle(self.root, "web-app",
                    "- insert:\n    - id: ui-workspace\n"
                    "      name: '@deepseek-ai/dsh-client-ui-workspace'\n")
        make_bundle(self.root, "replacement",
                    "- id: ui-workspace\n  disabled: true\n"
                    "- insert:\n    - id: replacement-workspace\n"
                    "      name: sample-replacement\n")
        profile = profile_for(self.root, ["web-app", "replacement"], [])
        effective = effects.resolve(profile, install_dir=self.install)
        row = effective.rows["ui-workspace"]
        self.assertTrue(row.disabled)
        self.assertEqual(row.disabled_by, "replacement")
        self.assertEqual(row.mounted_by, "web-app")
        self.assertIn("replacement-workspace", [r.id for r in effective.enabled()])

    def test_the_profile_own_patch_is_the_last_layer(self):
        make_bundle(self.root, "web-app",
                    "- insert:\n    - id: gm\n      name: sample-subpath/dsh\n")
        (self.root / "cordis.patch.yml").write_text(
            "- id: gm\n  disabled: true\n", encoding="utf-8")
        profile = profile_for(self.root, ["web-app"], [])
        effective = effects.resolve(profile, install_dir=self.install)
        self.assertTrue(effective.rows["gm"].disabled)
        self.assertIn("(profile cordis.patch.yml)", effective.rows["gm"].disabled_by)

    def test_the_same_module_mounted_twice_is_reported(self):
        make_bundle(self.root, "one",
                    "- insert:\n    - id: a\n      name: same-module\n")
        make_bundle(self.root, "two",
                    "- insert:\n    - id: b\n      name: same-module\n")
        profile = profile_for(self.root, ["one", "two"], [])
        effective = effects.resolve(profile, install_dir=self.install)
        self.assertEqual(effective.duplicates, {"same-module": ["a", "b"]})

    def test_a_disable_for_an_unknown_row_is_an_orphan(self):
        make_bundle(self.root, "one", "- id: never-mounted\n  disabled: true\n")
        profile = profile_for(self.root, ["one"], [])
        effective = effects.resolve(profile, install_dir=self.install)
        self.assertEqual(effective.orphans, ["never-mounted"])

    def test_a_conditional_row_is_neither_enabled_nor_disabled(self):
        make_bundle(self.root, "one",
                    "- insert:\n    - id: maybe\n      name: plugin\n"
                    "      disabled: !!js someCondition\n")
        profile = profile_for(self.root, ["one"], [])
        effective = effects.resolve(profile, install_dir=self.install)
        self.assertTrue(effective.rows["maybe"].conditional)
        self.assertEqual([row.id for row in effective.conditional()], ["maybe"])
        self.assertEqual(effective.disabled(), [])


# --------------------------------------------------------------------------- #
# Shadowed surfaces
# --------------------------------------------------------------------------- #

class ShadowTest(unittest.TestCase):
    """The failure this module exists for, plus the false positives it must avoid."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.install = self.root / "install"
        self.install.mkdir(parents=True, exist_ok=True)
        make_bundle(self.root, "web-app",
                    "- insert:\n    - id: ui-workspace\n"
                    "      name: '@deepseek-ai/dsh-client-ui-workspace'\n")
        make_bundle(self.root, "surgeon",
                    "- id: ui-workspace\n  disabled: true\n"
                    "- insert:\n    - id: replacement-workspace\n"
                    "      name: sample-replacement\n")
        self.effective = effects.resolve(
            profile_for(self.root, ["web-app", "surgeon"], []), install_dir=self.install)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_comment_naming_a_disabled_component_is_reported(self):
        make_plugin(self.root, "cleaner",
                    {"exports": {"./client": "./client.js"},
                     "dsh": {"client": {"platform": "web"}}},
                    {"client.js": "// The row menu is rendered by the upstream "
                                  "ui-workspace component with no public slot.\n"
                                  "new MutationObserver(() => {});\n"})
        found = effects.shadows_for(entry("cleaner", self.root), self.effective)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].row.id, "ui-workspace")
        self.assertIn("client.js:1", found[0].evidence)

    def test_a_name_inside_a_localized_string_is_not_a_dependency(self):
        # A user-facing string that names the module is not a dependency on it.
        make_bundle(self.root, "base",
                    "- insert:\n    - id: skill-filesystem\n"
                    "      name: '@deepseek-ai/dsh-skill-filesystem'\n"
                    "      disabled: true\n")
        effective = effects.resolve(
            profile_for(self.root, ["web-app", "surgeon", "base"], []),
            install_dir=self.install)
        make_plugin(self.root, "explorer",
                    {"exports": {"./client": "./client.js"},
                     "dsh": {"client": {"platform": "web"}}},
                    {"client.js": 'const zh = { note: "（skill-filesystem 会热扫描）" };\n'
                                  'document.addEventListener("click", () => {});\n'})
        self.assertEqual(effects.shadows_for(entry("explorer", self.root), effective), [])

    def test_the_plugin_that_disabled_the_row_is_not_a_victim(self):
        # replacement-workspace disables ui-workspace and names it in its own labels.
        make_plugin(self.root, "sample-replacement",
                    {"exports": {"./client": "./client.js"},
                     "dsh": {"client": {"platform": "web"}}},
                    {"client.js": 'ctx.effect(() => watch(), "ui-workspace: policy");\n'
                                  'document.querySelector("[role=treeitem]");\n'})
        self.assertEqual(
            effects.shadows_for(entry("sample-replacement", self.root), self.effective),
            [])

    def test_a_declared_inject_of_a_disabled_module_is_certain(self):
        make_plugin(self.root, "declared",
                    {"exports": {"./client": "./client.js"},
                     "dsh": {"client": {"inject": ["@deepseek-ai/dsh-client-ui-workspace"]}}},
                    {"client.js": "export const nothing = 1;\n"})
        found = effects.declared_shadows(entry("declared", self.root), self.effective)
        self.assertEqual(len(found), 1)
        self.assertIn("dsh.client.inject", found[0].evidence)

    def test_a_plugin_without_a_client_half_is_never_reported(self):
        make_plugin(self.root, "server-only", {"main": "index.js"}, {"index.js": "// ui-workspace\n"})
        self.assertEqual(
            effects.shadows_for(entry("server-only", self.root), self.effective), [])

    def test_scan_covers_the_profile_and_keeps_the_route_count(self):
        make_plugin(self.root, "cleaner",
                    {"exports": {"./client": "./client.js"},
                     "dsh": {"client": {"platform": "web"}}},
                    {"client.js": "// ui-workspace renders it\nMutationObserver;\n"})
        profile = profile_for(self.root, ["web-app", "surgeon"],
                              [entry("cleaner", self.root)])
        found = effects.scan_shadows(profile, self.effective,
                                     route_counts={"cleaner": 2})
        self.assertEqual(found["cleaner"][0].route_count, 2)

    # -- what the finding is worth ------------------------------------------- #

    def make_cleaner(self):
        """A comment names the row while the DOM is generic: a lead, not a verdict."""
        return make_plugin(
            self.root, "cleaner",
            {"exports": {"./client": "./client.js"},
             "dsh": {"client": {"platform": "web"}}},
            {"client.js": "// The row menu is rendered by the upstream ui-workspace\n"
                          "// component with no public slot.\n"
                          "new MutationObserver(() => {});\n"
                          "document.querySelectorAll('[role=\"menu\"]');\n"
                          "target.closest('[role=\"treeitem\"], [class*=\"sessionRow\"]');\n"})

    def test_a_code_reference_is_a_lead_not_a_verdict(self):
        self.make_cleaner()
        found = effects.shadows_for(entry("cleaner", self.root), self.effective,
                                    profile_dir=self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, effects.REFERENCE)
        self.assertFalse(found[0].certain)
        self.assertIsNone(found[0].replaced_by)
        self.assertEqual(effects.shadow_flags(found), (False, True))

    def test_a_replacement_that_repeats_the_dom_contract_explains_the_reference(self):
        # The replacement shape: the layer disables ui-workspace and mounts its own
        # session list, whose client half selects on the very same DOM names.
        self.make_cleaner()
        make_plugin(self.root, "sample-replacement",
                    {"exports": {"./client": "./client.js"},
                     "dsh": {"client": {"platform": "web"}}},
                    {"client.js": 'clsx(css.sessionRow, open && css.menuOpen);\n'
                                  'jsx("div", { role: "treeitem" });\n'
                                  'jsx(Menu, { role: "menu" });\n'})
        found = effects.shadows_for(entry("cleaner", self.root), self.effective,
                                    profile_dir=self.root)
        self.assertEqual(found[0].replaced_by, "sample-replacement")
        self.assertTrue(found[0].explained)
        self.assertEqual(effects.shadow_flags(found), (False, False))

    def test_a_replacement_missing_one_contract_name_explains_nothing(self):
        self.make_cleaner()
        make_plugin(self.root, "sample-replacement",
                    {"exports": {"./client": "./client.js"},
                     "dsh": {"client": {"platform": "web"}}},
                    {"client.js": 'jsx("div", { role: "treeitem" });\n'})
        found = effects.shadows_for(entry("cleaner", self.root), self.effective,
                                    profile_dir=self.root)
        self.assertIsNone(found[0].replaced_by)
        self.assertEqual(effects.shadow_flags(found), (False, True))

    def test_a_declared_shadow_stays_certain(self):
        make_plugin(self.root, "declared",
                    {"exports": {"./client": "./client.js"},
                     "dsh": {"client": {"inject": ["@deepseek-ai/dsh-client-ui-workspace"]}}},
                    {"client.js": "export const nothing = 1;\n"})
        found = effects.declared_shadows(entry("declared", self.root), self.effective)
        self.assertEqual(found[0].kind, effects.DECLARED)
        self.assertEqual(effects.shadow_flags(found), (True, False))


# --------------------------------------------------------------------------- #
# Client halves and the probe's surface column
# --------------------------------------------------------------------------- #

class ClientHalfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_exports_client_subpath_is_the_entry(self):
        make_plugin(self.root, "p", {"exports": {"./client": "./web/client.js"}},
                    {"web/client.js": "document.querySelector('[role=menu]');\n"})
        half = effects.client_half(self.root / "node_modules" / "p")
        self.assertTrue(half.present)
        self.assertTrue(half.dom)
        self.assertIn("menu", half.selectors)

    def test_injects_and_platform_are_read_from_the_manifest(self):
        make_plugin(self.root, "p", {"dsh": {"client": {"platform": "web",
                                                        "inject": ["a", "b"]}}})
        half = effects.client_half(self.root / "node_modules" / "p")
        self.assertEqual(half.injects, ["a", "b"])
        self.assertEqual(half.platform, "web")
        self.assertFalse(half.present)

    def test_string_blanking_keeps_a_comment_that_follows_a_code_line(self):
        line = 'const a = "name: x"; // uses ui-workspace here'
        self.assertNotIn("name: x", effects.blank_line_strings(line))
        self.assertIn("ui-workspace", effects.blank_line_strings(line))


class SurfaceTextTest(unittest.TestCase):
    def probe(self, **kwargs) -> verify_mod.Probe:
        values = {"name": "p", "status": verify_mod.LOADS}
        values.update(kwargs)
        return verify_mod.Probe(**values)

    def test_missing_and_unavailable(self):
        self.assertEqual(verify_mod.surface_text(self.probe(status=verify_mod.MISSING)), "—")
        self.assertEqual(verify_mod.surface_text(self.probe(status=verify_mod.UNAVAILABLE)), "?")

    def test_a_shadowed_surface_outranks_a_served_route(self):
        probe = self.probe(routes=[{"path": "/x"}], live={"/x": 200})
        self.assertEqual(verify_mod.surface_text(probe, shadowed=True), "shadowed")

    def test_live_beats_a_bare_route_count(self):
        probe = self.probe(routes=[{"path": "/x"}, {"path": "/y"}], live={"/x": 200, "/y": 404})
        self.assertEqual(verify_mod.surface_text(probe), "live:1")

    def test_a_registered_but_unserved_route_is_called_out(self):
        probe = self.probe(routes=[{"path": "/x"}], live={"/x": 404})
        self.assertEqual(verify_mod.surface_text(probe), "404:1")

    def test_routes_without_a_live_probe(self):
        probe = self.probe(routes=[{"path": "/x"}])
        self.assertEqual(verify_mod.surface_text(probe), "routes:1")

    def test_hooks_when_it_reached_for_a_service(self):
        probe = self.probe(applied=True, calls=["ctx.on(x)", "logger.warn"])
        self.assertEqual(verify_mod.surface_text(probe), "hooks:1")

    def test_an_empty_server_half_of_a_client_plugin_is_not_a_bug(self):
        probe = self.probe(applied=True, calls=[])
        self.assertEqual(verify_mod.surface_text(probe, client=True), "client")
        self.assertEqual(verify_mod.surface_text(probe), verify_mod.NO_OP)

    def test_a_declarative_bundle(self):
        probe = self.probe(applied=False)
        self.assertEqual(verify_mod.surface_text(probe), verify_mod.NO_APPLY)

    def test_a_lead_is_marked_as_a_question_not_as_a_verdict(self):
        probe = self.probe(routes=[{"path": "/x"}])
        self.assertEqual(verify_mod.surface_text(probe, suspect=True),
                         verify_mod.SHADOW_SUSPECT)
        self.assertEqual(verify_mod.surface_text(probe, shadowed=True),
                         verify_mod.SHADOWED)

    def test_a_dead_wire_call_outranks_a_lead_and_a_working_route(self):
        probe = self.probe(routes=[{"path": "/x"}], live={"/x": 200})
        self.assertEqual(verify_mod.surface_text(probe, dead_wire=2), verify_mod.WIRE_DEAD)
        self.assertEqual(verify_mod.surface_text(probe, dead_wire=2, suspect=True),
                         verify_mod.WIRE_DEAD)
        # A declared shadow still wins: the cell reports the strongest fact.
        self.assertEqual(verify_mod.surface_text(probe, dead_wire=2, shadowed=True),
                         verify_mod.SHADOWED)

    def test_a_broken_handler_outranks_a_registered_route(self):
        probe = self.probe(routes=[{"path": "/x"}], handlers=[{
            "path": "/x", "logs": ["log: read failed: ReferenceError: X is not defined"]}])
        self.assertEqual(verify_mod.surface_text(probe), verify_mod.HANDLER_FAILED)
        # The strongest fact still wins over it.
        self.assertEqual(verify_mod.surface_text(probe, dead_wire=1), verify_mod.WIRE_DEAD)
        self.assertEqual(verify_mod.surface_text(probe, shadowed=True), verify_mod.SHADOWED)

    def test_a_handler_lead_is_a_question_not_a_verdict(self):
        probe = self.probe(routes=[{"path": "/x"}], handlers=[{"path": "/x", "timedOut": True}])
        self.assertEqual(verify_mod.surface_text(probe), verify_mod.HANDLER_SUSPECT)


class MergeProbeTest(unittest.TestCase):
    def test_three_mounted_modules_fold_into_one_verdict(self):
        first = verify_mod.Probe("p", verify_mod.LOADS, module="p/knowledge", applied=True,
                                 routes=[{"path": "/a", "service": "webServer"}],
                                 calls=["webServer.register"], injects=["a"], ms=5)
        second = verify_mod.Probe("p", verify_mod.LOADS, module="p/tools", applied=True,
                                  routes=[{"path": "/a", "service": "webServer"},
                                          {"path": "/b", "service": "webServer"}],
                                  calls=["ctx.on(x)"], injects=["b"], ms=7)
        merged = verify_mod.merge_probe(first, second)
        self.assertEqual([route["path"] for route in merged.routes], ["/a", "/b"])
        self.assertEqual(merged.injects, ["a", "b"])
        self.assertEqual(merged.calls, ["ctx.on(x)", "webServer.register"])
        self.assertEqual(merged.ms, 12)
        self.assertEqual(merged.module, "p/knowledge, p/tools")

    def test_a_failure_anywhere_wins_and_keeps_the_module(self):
        good = verify_mod.Probe("p", verify_mod.LOADS, module="p", applied=True)
        bad = verify_mod.Probe("p", verify_mod.FAILED, module="p/broken", detail="boom")
        merged = verify_mod.merge_probe(good, bad)
        self.assertEqual(merged.status, verify_mod.FAILED)
        self.assertIn("p/broken", merged.detail)
        self.assertTrue(merged.applied)

    def test_handler_evidence_from_every_module_is_kept(self):
        first = verify_mod.Probe("p", verify_mod.LOADS, module="p/a",
                                 handlers=[{"path": "/a"}], notes=["n1"])
        second = verify_mod.Probe("p", verify_mod.LOADS, module="p/b",
                                  handlers=[{"path": "/b"}, {"path": "/a"}], notes=["n2"])
        merged = verify_mod.merge_probe(first, second)
        self.assertEqual([record["path"] for record in merged.handlers], ["/a", "/b"])
        self.assertEqual(merged.notes, ["n1", "n2"])


class MountedSpecifiersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.install = self.root / "install"
        self.install.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_subpaths_and_the_bare_name_are_all_returned(self):
        make_bundle(self.root, "knowledge",
                    "- insert:\n"
                    "    - id: knowledge\n      name: sample-multi/knowledge\n"
                    "    - id: tools\n      name: sample-multi/tool-knowledge\n"
                    "    - id: main\n      name: sample-multi\n")
        profile = profile_for(self.root, ["knowledge"], [])
        effective = effects.resolve(profile, install_dir=self.install)
        plugin = PluginEntry("sample-multi", "^1", "npm", True, "1.0.0", None, True)
        self.assertEqual(verify_mod.mounted_specifiers(plugin, effective),
                         ["sample-multi/knowledge", "sample-multi/tool-knowledge",
                          "sample-multi"])

    def test_a_disabled_plugin_has_no_mounted_specifier(self):
        make_bundle(self.root, "one",
                    "- insert:\n    - id: p\n      name: p\n"
                    "- id: p\n  disabled: true\n")
        profile = profile_for(self.root, ["one"], [])
        effective = effects.resolve(profile, install_dir=self.install)
        self.assertEqual(verify_mod.mounted_specifiers(PluginEntry(
            "p", "^1", "npm", True, "1.0.0", None, True), effective), [])


class LiveProbeTest(unittest.TestCase):
    def test_a_404_is_not_served_and_anything_else_is(self):
        probes = {"p": verify_mod.Probe("p", verify_mod.LOADS,
                                        routes=[{"path": "/ok"}, {"path": "/gone"}])}
        statuses = {"/ok": 200, "/gone": 404}

        def fake_status(base, path, timeout=0.0):
            return statuses[path]

        with mock.patch.object(verify_mod, "route_status", fake_status), \
             mock.patch.object(verify_mod, "host_reachable", lambda *a, **k: True):
            reachable, verdicts = verify_mod.live_probe(probes)
        self.assertTrue(reachable)
        verify_mod.apply_live(probes, verdicts)
        self.assertEqual(verify_mod.surface_text(probes["p"]), "live:1")

    def test_a_dead_host_is_reported_as_unreachable_not_as_a_failure(self):
        probes = {"p": verify_mod.Probe("p", verify_mod.LOADS, routes=[{"path": "/x"}])}
        with mock.patch.object(verify_mod, "host_reachable", lambda *a, **k: False):
            reachable, verdicts = verify_mod.live_probe(probes)
        self.assertFalse(reachable)
        self.assertEqual(verdicts, {})
        self.assertEqual(verify_mod.surface_text(probes["p"]), "routes:1")

    def test_an_unreachable_path_reports_zero(self):
        with mock.patch.object(verify_mod, "urlopen", side_effect=OSError("no host"),
                               create=True):
            self.assertEqual(verify_mod.route_status("http://127.0.0.1:1", "/x"), 0)


class FingerprintTest(unittest.TestCase):
    def test_the_probe_schema_is_part_of_the_fingerprint(self):
        """An upgraded tool must not read verdicts that answer an older question."""
        profile = Profile(directory=Path("/nowhere"), name="web", dependencies={},
                          bundles=[], plugins=[])
        self.assertIn(f"probe={verify_mod.PROBE_SCHEMA}",
                      verify_mod.fingerprint(profile, "0.1.0"))


if __name__ == "__main__":
    unittest.main()
