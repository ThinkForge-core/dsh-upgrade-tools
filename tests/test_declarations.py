"""Declaration integrity and inline purity — the two checks a passing build hid.

Both cover a plugin that is installed (or about to be installed) and does not work
in fact, while everything a normal audit looks at is green.

A client plugin's bundle may inline the client half of another DSH package three ways:

* the package declares ``dsh.client`` and the host loads it as its own graph row —
  the bundle now carries a SECOND copy of that module's state;
* the inlined package is not inline-safe in the target version — the duplicate is
  not interchangeable with the host's, so ``Symbol``/``instanceof``/singleton
  identity silently stops matching and a panel stays empty with no error at all;
* the inlined bundle self-registers, so boot dies loudly with
  ``client-modules: duplicate factory registration``.

The artifact is the only honest witness — which is what these checks read.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import analysis as analysis_mod  # noqa: E402
from dshupgrade import checkout as checkout_mod  # noqa: E402
from dshupgrade import locals as locals_mod  # noqa: E402
from dshupgrade.compat import (  # noqa: E402
    BUILTIN_INLINE_SAFE,
    BUILTIN_PLATFORM_MODULES,
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
    InlinePolicy,
    compile_classification,
    external_base_names,
    inlined_packages,
    scan_client_declaration,
    scan_inline_purity,
)
from dshupgrade.paths import find_checkout  # noqa: E402
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402

NAME = "example-client-plugin"
SESSION = "@deepseek-ai/dsh-api-session-controller"


def pnpm_region(package: str, rest: str, version: str = "0.1.5-rc.2") -> str:
    """The exact ``//#region`` line tsdown emits for a pnpm-store module."""
    encoded = package.replace("/", "+")
    return (f"//#region node_modules/.pnpm/{encoded}@{version}/"
            f"node_modules/{package}/{rest}\n")


def flat_region(package: str, rest: str) -> str:
    return f"//#region node_modules/{package}/{rest}\n"


def write_package(root: Path, files: dict[str, str], manifest: dict) -> Path:
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def manifest_for(*, exports: object = ..., platform: object = "web",
                 inject: object = ..., external: object = ..., immediately: object = ...,
                 client: object = ...) -> dict:
    """A manifest with only the fields a test cares about."""
    declaration: dict = {}
    if platform is not ...:
        declaration["platform"] = platform
    if inject is not ...:
        declaration["inject"] = inject
    if external is not ...:
        declaration["external"] = external
    if immediately is not ...:
        declaration["immediately"] = immediately
    if client is not ...:
        section: dict = {"client": client}
    else:
        section = {"client": declaration}
    manifest = {"name": NAME, "version": "0.1.5-rc.2", "engines": {"dsh": "^0.1.5-rc.2"},
                "dsh": section}
    if exports is not ...:
        manifest["exports"] = exports
    return manifest


class InlinedPackagesTest(unittest.TestCase):
    """The region marker is the honest record of what a bundle inlined."""

    def test_reads_the_pnpm_store_layout(self):
        text = pnpm_region(SESSION, "lib/client.js") + pnpm_region(
            "@deepseek-ai/dsh-util-workspace-path", "lib/index.js")
        self.assertEqual(inlined_packages(text), [SESSION, "@deepseek-ai/dsh-util-workspace-path"])

    def test_reads_the_flat_layout(self):
        self.assertEqual(inlined_packages(flat_region(SESSION, "lib/client.js")), [SESSION])

    def test_nested_node_modules_resolves_to_the_innermost_package(self):
        text = "//#region node_modules/foo/node_modules/@scope/bar/index.js\n"
        self.assertEqual(inlined_packages(text), ["@scope/bar"])

    def test_unscoped_packages_are_read_for_completeness(self):
        self.assertEqual(inlined_packages(flat_region("clsx", "dist/clsx.mjs")), ["clsx"])

    def test_plugin_own_sources_and_virtual_ids_are_not_packages(self):
        text = ("//#region src/client/index.ts\n"
                "//#region \\0dsh-css:src/client/x.module.css.mjs\n")
        self.assertEqual(inlined_packages(text), [])

    def test_a_bundle_without_markers_reports_nothing(self):
        """esbuild output and hand-written bundles have no markers at all."""
        self.assertEqual(inlined_packages("window.__ModuleLoader__.load({ id: 'x' });"), [])

    def test_duplicates_are_reported_once(self):
        text = pnpm_region(SESSION, "lib/client.js") + flat_region(SESSION, "lib/other.js")
        self.assertEqual(inlined_packages(text), [SESSION])


class ScanClientDeclarationTest(unittest.TestCase):
    """Check 4: the host composes the boot graph from this declaration."""

    def _hits(self, manifest, files=None, *, source=True, require_bundle=False):
        code = None
        if source:
            code = locals_mod.CodeSource(files=files or {"client.js": "x"},
                                         client_files={"client.js"})
        return scan_client_declaration(manifest, code, package_name=NAME,
                                       require_bundle=require_bundle)

    def test_a_clean_declaration_has_no_hits(self):
        manifest = manifest_for(exports={"./client": "./client.js"},
                                inject=[SESSION], external=["@deepseek-ai/dsh-api-gateway/client"])
        self.assertEqual(self._hits(manifest), [])

    def test_a_plugin_without_dsh_client_is_not_checked(self):
        manifest = {"name": NAME, "version": "1.0.0"}
        self.assertEqual(self._hits(manifest), [])

    def test_non_object_declaration_is_a_shape_hit(self):
        manifest = manifest_for(exports={"./client": "./client.js"}, client="web")
        hits = self._hits(manifest)
        self.assertEqual([hit["kind"] for hit in hits], ["shape"])
        self.assertIn("non-object", hits[0]["message"])

    def test_missing_platform_is_a_shape_hit(self):
        hits = self._hits(manifest_for(exports={"./client": "./client.js"}, platform=...))
        self.assertEqual([hit["kind"] for hit in hits], ["shape"])
        self.assertIn("platform", hits[0]["message"])

    def test_non_string_array_inject_is_a_shape_hit(self):
        hits = self._hits(manifest_for(exports={"./client": "./client.js"}, inject=SESSION))
        self.assertEqual([hit["kind"] for hit in hits], ["shape"])
        self.assertIn("inject", hits[0]["message"])

    def test_non_string_array_external_is_a_shape_hit(self):
        hits = self._hits(manifest_for(exports={"./client": "./client.js"}, external=[1, 2]))
        self.assertEqual([hit["kind"] for hit in hits], ["shape"])
        self.assertIn("external", hits[0]["message"])

    def test_non_boolean_immediately_is_a_shape_hit(self):
        hits = self._hits(manifest_for(exports={"./client": "./client.js"}, immediately="yes"))
        self.assertEqual([hit["kind"] for hit in hits], ["shape"])
        self.assertIn("immediately", hits[0]["message"])

    def test_client_without_the_client_export_is_a_hit(self):
        hits = self._hits(manifest_for(exports={".": "./index.js"}))
        self.assertEqual([hit["kind"] for hit in hits], ["missing-export"])

    def test_a_malformed_client_export_is_a_hit(self):
        hits = self._hits(manifest_for(exports={"./client": {"import": "./client.js"}}))
        self.assertEqual([hit["kind"] for hit in hits], ["export-shape"])

    def test_the_conditional_default_form_is_accepted(self):
        manifest = manifest_for(exports={"./client": {"default": "./client.js"}})
        self.assertEqual(self._hits(manifest), [])

    def test_a_row_requesting_its_own_package_is_a_hit(self):
        manifest = manifest_for(exports={"./client": "./client.js"}, external=[f"{NAME}/client"])
        hits = self._hits(manifest)
        self.assertEqual([hit["kind"] for hit in hits], ["self-external"])

    def test_a_promised_bundle_missing_from_a_packed_artifact_is_a_hit(self):
        manifest = manifest_for(exports={"./client": "./client.js"})
        hits = self._hits(manifest, files={"index.js": "x"}, require_bundle=True)
        self.assertEqual([hit["kind"] for hit in hits], ["missing-bundle"])

    def test_a_repository_link_is_not_required_to_be_built_yet(self):
        """A ``link:`` to a repository legitimately has no bundle until built."""
        manifest = manifest_for(exports={"./client": "./client.js"})
        hits = self._hits(manifest, files={"index.js": "x"}, require_bundle=False)
        self.assertEqual(hits, [])


class InlinePolicyTest(unittest.TestCase):

    def _policy(self, **overrides) -> InlinePolicy:
        values = dict(platform=set(BUILTIN_PLATFORM_MODULES),
                      client_rows={SESSION, "@deepseek-ai/dsh-client-ui-layout"},
                      inline_safe=compile_classification(None, None, None)[0],
                      vendored=compile_classification(None, None, None)[1],
                      generated_remote=compile_classification(None, None, None)[2])
        values.update(overrides)
        return InlinePolicy(**values)

    def test_platform_seed_words_are_allowed(self):
        self.assertTrue(self._policy().allows("react"))
        self.assertTrue(self._policy().allows("@deepseek-ai/dsh-client-store"))

    def test_own_declared_external_is_allowed(self):
        self.assertTrue(self._policy().allows("@deepseek-ai/dsh-api-gateway",
                                              {"@deepseek-ai/dsh-api-gateway"}))

    def test_inline_safe_wire_layers_are_allowed(self):
        self.assertTrue(
            self._policy().allows("@deepseek-ai/dsh-util-workspace-path", subpath="lib/index.js"))

    def test_vendored_libraries_are_allowed(self):
        self.assertTrue(self._policy().allows("@deepseek-ai/schemastery"))

    def test_a_generated_remote_contribution_is_allowed_by_its_subpath(self):
        self.assertTrue(self._policy().allows(SESSION, subpath="lib/typert.remote-client.js"))

    def test_the_remote_exemption_never_covers_a_package_as_a_whole(self):
        """Regression the check itself had: `/remote` matched EVERY dsh-* package.

        Synthesizing ``<package>/remote`` for the base name made the exemption
        match ``@deepseek-ai/dsh-<anything>``, so the inline check could never fire.
        """
        policy = self._policy()
        self.assertFalse(policy.allows(SESSION, subpath="lib/client.js"))
        self.assertFalse(policy.allows("@deepseek-ai/dsh-client-ui-layout", subpath="lib/client.js"))

    def test_a_client_row_is_not_allowed(self):
        self.assertFalse(self._policy().allows(SESSION, subpath="lib/client.js"))

    def test_an_unclassified_package_is_not_allowed(self):
        self.assertFalse(self._policy().allows("@deepseek-ai/dsh-something-new"))


class ScanInlinePurityTest(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = InlinePolicy(
            platform=set(BUILTIN_PLATFORM_MODULES),
            client_rows={SESSION, "@deepseek-ai/dsh-client-ui-layout"},
            inline_safe=compile_classification(None, None, None)[0],
            vendored=compile_classification(None, None, None)[1],
            generated_remote=compile_classification(None, None, None)[2],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _source(self, bundle: str):
        manifest = manifest_for(exports={"./client": "./client.js"})
        write_package(self.root, {"client.js": bundle}, manifest)
        return locals_mod.resolve(f"link:{self.root}").code_source(manifest)

    def test_inlining_a_client_row_is_a_hit(self):
        source = self._source(pnpm_region(SESSION, "lib/client.js"))
        hits = scan_inline_purity(source, self.policy, NAME)
        self.assertEqual([hit["package"] for hit in hits], [SESSION])
        self.assertTrue(hits[0]["client_row"])
        self.assertEqual(hits[0]["file"], "client.js")

    def test_inlining_an_inline_safe_layer_is_not_a_hit(self):
        source = self._source(pnpm_region("@deepseek-ai/dsh-util-workspace-path", "lib/index.js"))
        self.assertEqual(scan_inline_purity(source, self.policy, NAME), [])

    def test_inlining_a_vendored_library_is_not_a_hit(self):
        source = self._source(flat_region("@deepseek-ai/schemastery", "index.js"))
        self.assertEqual(scan_inline_purity(source, self.policy, NAME), [])

    def test_a_package_the_plugin_declared_as_external_is_not_a_hit(self):
        source = self._source(pnpm_region("@deepseek-ai/dsh-api-gateway", "lib/client.js"))
        self.assertEqual(
            scan_inline_purity(source, self.policy, NAME, {"@deepseek-ai/dsh-api-gateway"}), [])

    def test_a_generated_remote_contribution_is_not_a_hit(self):
        source = self._source(pnpm_region(SESSION, "lib/typert.remote-client.js"))
        self.assertEqual(scan_inline_purity(source, self.policy, NAME), [])

    def test_the_plugin_itself_may_appear_in_the_region_list(self):
        source = self._source(flat_region(NAME, "lib/client.js"))
        self.assertEqual(scan_inline_purity(source, self.policy, NAME), [])

    def test_only_the_declared_bundle_is_read(self):
        manifest = manifest_for(exports={"./client": "./client.js"})
        write_package(self.root, {
            "client.js": "export const x = 1",
            "tools.js": pnpm_region(SESSION, "lib/client.js"),
        }, manifest)
        source = locals_mod.resolve(f"link:{self.root}").code_source(manifest)
        self.assertEqual(scan_inline_purity(source, self.policy, NAME), [])


class NewCheckWiringTest(unittest.TestCase):
    """Both checks must reach the verdict, not just the compat module."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, manifest: dict, files: dict) -> dict:
        write_package(self.root, files, manifest)
        policy = InlinePolicy(
            platform=set(BUILTIN_PLATFORM_MODULES),
            client_rows={SESSION},
            inline_safe=compile_classification(None, None, None)[0],
            vendored=compile_classification(None, None, None)[1],
            generated_remote=compile_classification(None, None, None)[2],
        )
        analysis = analysis_mod.Analysis(
            target="0.1.5-rc.2", current_core="0.1.1-rc.2", checkout=self.root,
            host_packages={"@deepseek-ai/dsh"}, seed_words=set(), removed=[],
            inline_policy=policy,
        )
        plugin = PluginEntry(name=NAME, spec=f"link:{self.root}", source="link",
                             installed=False, version="0.1.5-rc.2", directory=None,
                             in_bundles=False)
        return analysis_mod._analyse_plugin(plugin, analysis, offline=True,
                                            check_updates=False, profile_dir=None)

    def test_a_broken_declaration_turns_the_verdict_incompatible(self):
        manifest = manifest_for(exports={"./client": "./client.js"}, platform=...)
        entry = self._entry(manifest, {"client.js": "export const x = 1"})
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertTrue(entry["declaration_hits"])
        self.assertIn("dsh.client", entry["reason"])
        self.assertFalse(entry["code_clean"])

    def test_an_inlined_client_row_turns_the_verdict_incompatible(self):
        manifest = manifest_for(exports={"./client": "./client.js"})
        entry = self._entry(manifest, {"client.js": pnpm_region(SESSION, "lib/client.js")})
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertTrue(entry["inline_hits"])
        self.assertIn(SESSION, entry["reason"])
        self.assertFalse(entry["code_clean"])

    def test_a_clean_artifact_stays_compatible(self):
        manifest = manifest_for(exports={"./client": "./client.js"})
        entry = self._entry(manifest, {"client.js": "window.__ModuleLoader__.load({ id: '"
                                                     + NAME + "', factory: () => ({}) });"})
        self.assertEqual(entry["status"], STATUS_COMPATIBLE)
        self.assertEqual(entry["inline_hits"], [])
        self.assertEqual(entry["declaration_hits"], [])
        self.assertTrue(entry["code_clean"])

    def test_a_promised_bundle_missing_from_a_tarball_turns_it_incompatible(self):
        manifest = manifest_for(exports={"./client": "./client.js"})
        write_package(self.root, {"index.js": "export const x = 1"}, manifest)
        tarball = self.root / "broken.tgz"
        with tarfile.open(tarball, "w:gz") as archive:
            for name in ("package.json", "index.js"):
                payload = (self.root / name).read_bytes()
                info = tarfile.TarInfo(f"package/{name}")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        plugin = PluginEntry(name=NAME, spec=f"file:{tarball}", source="file",
                             installed=False, version="0.1.5-rc.2", directory=None,
                             in_bundles=False)
        analysis = analysis_mod.Analysis(
            target="0.1.5-rc.2", current_core="0.1.1-rc.2", checkout=self.root,
            host_packages={"@deepseek-ai/dsh"}, seed_words=set(), removed=[],
            inline_policy=InlinePolicy(),
        )
        entry = analysis_mod._analyse_plugin(plugin, analysis, offline=True,
                                             check_updates=False, profile_dir=None)
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertIn("not found", entry["reason"])


class TargetClassificationTest(unittest.TestCase):
    """The classification is read from the target tree, never guessed."""

    def setUp(self):
        # Look for a checkout of the target anywhere it may live (the configured
        # location, or <DSH_HOME>/checkouts) — never create one just to test.
        found = find_checkout("0.1.5-rc.2")
        if found is None:
            self.skipTest("0.1.5-rc.2 checkout is not available")
        self.checkout = found

    def test_client_rows_are_the_packages_declaring_dsh_client(self):
        rows = checkout_mod.client_rows(self.checkout)
        self.assertIn(SESSION, rows)
        self.assertNotIn("@deepseek-ai/dsh-util-workspace-path", rows)

    def test_the_classification_patterns_are_read_from_the_checkout(self):
        inline_safe, vendored, generated_remote = checkout_mod.inline_classification(self.checkout)
        self.assertIsNotNone(inline_safe)
        self.assertIn("util-workspace-path", inline_safe)
        self.assertIn("cosmokit", vendored)
        self.assertIn("remote", generated_remote)

    def test_the_checkout_patterns_compile_as_python_regexes(self):
        patterns = checkout_mod.inline_classification(self.checkout)
        compiled = compile_classification(*patterns)
        self.assertTrue(all(pattern is not None for pattern in compiled))
        # The built-in fallback must classify the same way, or the check would
        # change meaning just because a checkout is unavailable.
        fallback = compile_classification(None, None, None)
        self.assertTrue(fallback[0].match("@deepseek-ai/dsh-util-workspace-path"))
        self.assertIsNotNone(__import__("re").compile(BUILTIN_INLINE_SAFE))


class RealArtifactRegressionTest(unittest.TestCase):
    """End-to-end run over a provided pair of real builds.

    ``DSH_UPGRADE_TEST_BROKEN_TGZ`` and ``DSH_UPGRADE_TEST_FIXED_TGZ`` point at two
    builds of one client plugin — the broken one inlining the self-registering
    client row ``@deepseek-ai/dsh-api-session-controller``, the fixed one clean.
    Without both variables the class skips, so the suite stays self-contained.
    """

    def setUp(self):
        broken = os.environ.get("DSH_UPGRADE_TEST_BROKEN_TGZ", "").strip()
        fixed = os.environ.get("DSH_UPGRADE_TEST_FIXED_TGZ", "").strip()
        if not broken or not fixed:
            self.skipTest("no artifact pair configured "
                          "(DSH_UPGRADE_TEST_BROKEN_TGZ / DSH_UPGRADE_TEST_FIXED_TGZ)")
        self.broken, self.fixed = Path(broken).expanduser(), Path(fixed).expanduser()
        if not self.broken.is_file() or not self.fixed.is_file():
            self.skipTest("the configured artifacts are not on disk")
        # The plugin's own name comes from its manifest, not from a constant.
        self.name = str(self._source(self.broken)[0].get("name") or "")

    def _source(self, tarball: Path):
        source = locals_mod.resolve(f"file:{tarball}")
        manifest = source.manifest()
        return manifest, source.code_source(manifest)

    def _policy(self):
        return InlinePolicy(
            platform=set(BUILTIN_PLATFORM_MODULES), client_rows={SESSION},
            inline_safe=compile_classification(None, None, None)[0],
            vendored=compile_classification(None, None, None)[1],
            generated_remote=compile_classification(None, None, None)[2],
        )

    def test_the_broken_tarball_is_caught_by_both_new_checks(self):
        manifest, code = self._source(self.broken)
        hits = scan_inline_purity(code, self._policy(), self.name, external_base_names(manifest))
        self.assertEqual([hit["package"] for hit in hits], [SESSION])
        self.assertTrue(hits[0]["client_row"])

    def test_the_fixed_tarball_is_clean_for_both_new_checks(self):
        manifest, code = self._source(self.fixed)
        self.assertEqual(
            scan_inline_purity(code, self._policy(), self.name, external_base_names(manifest)), [])
        self.assertEqual(scan_client_declaration(manifest, code, package_name=self.name,
                                                 require_bundle=True), [])

    def test_the_broken_tarball_fails_the_inspect_gate_and_carries_the_hits(self):
        """The end-to-end contract: a pre-install gate exit code plus machine output."""
        version = analysis_mod.installed_core_version()
        if version and find_checkout(version) is None:
            self.skipTest(f"the installed core ({version}) has no checkout")
        args = SimpleNamespace(artifact=str(self.broken), core=None, offline=True,
                               no_clone=True, json=True, verbose=False, since=None,
                               profile="web", color="never", state_dir=None)
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = dsh_upgrade.cmd_inspect(args)
        self.assertEqual(code, 2)
        payload = json.loads(buffer.getvalue())
        entry = payload["plugins"][0]
        self.assertEqual([hit["package"] for hit in entry["inline_hits"]], [SESSION])
        self.assertTrue(entry["registration_hits"])


if __name__ == "__main__":
    unittest.main()
