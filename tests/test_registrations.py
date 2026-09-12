"""Factory registrations in a client bundle — the check a code scan owes.

A browser bundle is loaded as ONE graph row and must register exactly one factory
— its own. A bundle built with a type-only import written as
``import { type X } from '<pkg>/client'`` can have that import reduced to a
side-effect one, inlining another self-registering client package: the bundle then
installs two factories while the host graph row owns one of them, and boot dies
with:

    client-modules: duplicate factory registration for "<id>"

Every declaration is satisfied, every package exists, types check out, the test
suite is green — and DSH does not start. The artifact itself is the only honest
witness, which is what these checks read.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import analysis as analysis_mod  # noqa: E402
from dshupgrade import locals as locals_mod  # noqa: E402
from dshupgrade.compat import (  # noqa: E402
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
    registrations_of,
    scan_client_registrations,
)
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402

NAME = "example-client-plugin"

#: The tsdown banner, one property per line — the shape a real broken bundle had.
BROKEN_BUNDLE = f"""window.__ModuleLoader__.load({{
\tid: "{NAME}",
\tfactory: (require) => {{
\t\tvar module = {{ exports: {{}} }};
\t\twindow.__ModuleLoader__.load({{
\t\t\tid: "@deepseek-ai/dsh-api-session-controller",
\t\t\tfactory: (require) => {{ return {{}} }}
\t\t}});
\t\treturn module.exports;
\t}}
}});
"""

CLEAN_BUNDLE = f"""window.__ModuleLoader__.load({{
\tid: "{NAME}",
\tfactory: (require) => {{
\t\tvar module = {{ exports: {{}} }};
\t\treturn module.exports;
\t}}
}});
"""

MINIFIED_BUNDLE = (
    f'window.__ModuleLoader__.load({{id:"{NAME}",factory:function(r){{return{{}}}}}});'
)


def write_package(root: Path, files: dict[str, str], manifest: dict) -> Path:
    """A plugin directory: a manifest plus files relative to its root."""
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def write_tarball(path: Path, files: dict[str, str]) -> Path:
    """Build a tgz the way npm does it: the contents live under ``package/``."""
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


class RegistrationsOfTest(unittest.TestCase):
    """The facade call is read in the shapes the DSH build actually emits."""

    def test_reads_the_multiline_banner(self):
        self.assertEqual(registrations_of(BROKEN_BUNDLE),
                         [NAME, "@deepseek-ai/dsh-api-session-controller"])

    def test_reads_the_minified_form(self):
        self.assertEqual(registrations_of(MINIFIED_BUNDLE), [NAME])

    def test_plain_code_has_no_registrations(self):
        self.assertEqual(registrations_of("export const x = 1"), [])


class ScanClientRegistrationsTest(unittest.TestCase):
    """Two failures of one family, both invisible to every other check."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest(self, inject=None) -> dict:
        return {"name": NAME, "version": "0.1.5-rc.2",
                "exports": {"./client": "./client.js"},
                "engines": {"dsh": "^0.1.5-rc.2"},
                "dsh": {"client": {"platform": "web", "inject": inject or []}}}

    def test_clean_bundle_has_no_hits(self):
        write_package(self.root, {"client.js": CLEAN_BUNDLE}, self._manifest())
        source = locals_mod.resolve(f"link:{self.root}").code_source(self._manifest())
        self.assertEqual(scan_client_registrations(source, NAME), [])

    def test_inlined_self_registering_bundle_is_foreign_registration(self):
        """The 0.1.5-rc.2 regression: a second id from another package's row."""
        write_package(self.root, {"client.js": BROKEN_BUNDLE}, self._manifest())
        source = locals_mod.resolve(f"link:{self.root}").code_source(self._manifest())
        hits = scan_client_registrations(source, NAME)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["duplicate"])
        self.assertEqual(hits[0]["foreign"], ["@deepseek-ai/dsh-api-session-controller"])

    def test_same_id_twice_is_a_duplicate(self):
        write_package(self.root, {"client.js": CLEAN_BUNDLE + CLEAN_BUNDLE}, self._manifest())
        source = locals_mod.resolve(f"link:{self.root}").code_source(self._manifest())
        hits = scan_client_registrations(source, NAME)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["duplicate"])
        self.assertEqual(hits[0]["foreign"], [])

    def test_only_the_declared_bundle_is_scanned(self):
        """Documentation and build scripts quote the pattern — they never execute."""
        write_package(self.root, {
            "client.js": CLEAN_BUNDLE,
            "scripts/guard.mjs": f'// example: __ModuleLoader__.load({{ id: "{NAME}", factory }})\n',
            "README.md": BROKEN_BUNDLE,
        }, self._manifest())
        source = locals_mod.resolve(f"link:{self.root}").code_source(self._manifest())
        self.assertEqual(scan_client_registrations(source, NAME), [])

    def test_tarball_source_is_scanned_without_unpacking(self):
        tarball = write_tarball(self.root / "broken-1.0.0.tgz",
                                {"package/package.json": json.dumps(self._manifest()),
                                 "package/client.js": BROKEN_BUNDLE})
        source = locals_mod.resolve(f"file:{tarball}").code_source(self._manifest())
        hits = scan_client_registrations(source, NAME)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["file"], "client.js")


class RegistrationWiringTest(unittest.TestCase):
    """The scan must reach the verdict, not just the compat module."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = {
            "name": NAME, "version": "0.1.5-rc.2",
            "exports": {"./client": "./client.js"},
            "engines": {"dsh": "^0.1.5-rc.2"},
            "dsh": {"client": {"platform": "web"}},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, bundle: str) -> dict:
        write_package(self.root, {"client.js": bundle}, self.manifest)
        analysis = analysis_mod.Analysis(
            target="0.1.5-rc.2", current_core="0.1.1-rc.2", checkout=self.root,
            host_packages={"@deepseek-ai/dsh"}, seed_words=set(), removed=[],
        )
        plugin = PluginEntry(name=NAME, spec=f"link:{self.root}", source="link",
                             installed=False, version="0.1.5-rc.2", directory=None,
                             in_bundles=False)
        return analysis_mod._analyse_plugin(plugin, analysis, offline=True,
                                            check_updates=False, profile_dir=None)

    def test_a_foreign_registration_turns_the_verdict_incompatible(self):
        entry = self._entry(BROKEN_BUNDLE)
        self.assertEqual(entry["status"], STATUS_INCOMPATIBLE)
        self.assertTrue(entry["registration_hits"])
        self.assertIn("factory", entry["reason"])
        self.assertIn("@deepseek-ai/dsh-api-session-controller", entry["reason"])
        # Declarations alone were satisfied: the code check is what caught it.
        self.assertTrue(all(d["result"] is True for d in entry["declarations"]))

    def test_a_clean_bundle_stays_compatible_and_code_clean(self):
        entry = self._entry(CLEAN_BUNDLE)
        self.assertEqual(entry["status"], STATUS_COMPATIBLE)
        self.assertEqual(entry["registration_hits"], [])
        self.assertTrue(entry["code_clean"])


class InspectCommandTest(unittest.TestCase):
    """``inspect`` reads an artifact that is NOT in the profile."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _namespace(self, artifact: str, **overrides):
        values = dict(artifact=artifact, core=None, offline=True, no_clone=True,
                      json=False, verbose=False, since=None, profile="web",
                      color="never", state_dir=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_no_profile_is_read_and_the_target_defaults_to_the_installed_core(self):
        """The question is "will this run on MY core", not "what is the newest"."""
        manifest = {"name": NAME, "version": "0.1.5-rc.2",
                    "exports": {"./client": "./client.js"},
                    "engines": {"dsh": "^0.1.5-rc.2"},
                    "dsh": {"client": {"platform": "web"}}}
        write_package(self.root, {"client.js": CLEAN_BUNDLE}, manifest)

        captured = {}

        def fake_analyse(profile, target, **options):
            captured["profile"] = profile
            captured["target"] = target
            captured["options"] = options
            return analysis_mod.Analysis(target=target, current_core="0.1.5-rc.2",
                                         checkout=self.root, plugins=[])

        with mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(analysis_mod, "analyse", side_effect=fake_analyse), \
             redirect_stdout(io.StringIO()):
            code = dsh_upgrade.cmd_inspect(self._namespace(str(self.root)))

        self.assertEqual(code, 0)
        self.assertEqual(captured["target"], "0.1.5-rc.2")
        profile = captured["profile"]
        self.assertEqual(len(profile.plugins), 1)
        self.assertEqual(profile.plugins[0].name, NAME)
        self.assertFalse(profile.plugins[0].installed)
        self.assertIn(f"file:{self.root}", profile.plugins[0].spec)

    def test_since_is_forwarded_as_the_removed_package_baseline(self):
        manifest = {"name": NAME, "version": "1.0.0", "engines": {"dsh": "^0.1.1-rc.2"}}
        write_package(self.root, {"src/index.js": "export const x = 1"}, manifest)
        captured = {}

        def fake_analyse(profile, target, **options):
            captured["options"] = options
            return analysis_mod.Analysis(target=target, current_core="0.1.5-rc.2",
                                         checkout=self.root, plugins=[])

        with mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(analysis_mod, "analyse", side_effect=fake_analyse), \
             redirect_stdout(io.StringIO()):
            dsh_upgrade.cmd_inspect(self._namespace(str(self.root), since="0.1.1-rc.2"))

        self.assertEqual(captured["options"]["since"], "0.1.1-rc.2")

    def test_json_mode_keeps_stdout_parseable(self):
        """A pre-install gate is piped into scripts, so stdout must be one document."""
        manifest = {"name": NAME, "version": "1.0.0",
                    "exports": {"./client": "./client.js"},
                    "engines": {"dsh": "^0.1.5-rc.2"}}
        write_package(self.root, {"client.js": BROKEN_BUNDLE}, manifest)
        captured = {}

        def fake_analyse(profile, target, **options):
            captured["options"] = options
            return analysis_mod.Analysis(target=target, current_core="0.1.5-rc.2",
                                         checkout=self.root, plugins=[])

        buffer = io.StringIO()
        with mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(analysis_mod, "analyse", side_effect=fake_analyse), \
             redirect_stdout(buffer):
            dsh_upgrade.cmd_inspect(self._namespace(str(self.root), json=True))

        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["artifact"]["kind"], "dir")
        self.assertEqual(payload["target"], "0.1.5-rc.2")
        # The progress log was redirected, not mixed into the document.
        self.assertEqual(captured["options"]["since"], None)

    def test_a_missing_path_is_an_error(self):
        with self.assertRaises(SystemExit):
            dsh_upgrade.cmd_inspect(self._namespace(str(self.root / "nope")))

    def test_an_artifact_without_a_manifest_is_an_error(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit):
            dsh_upgrade.cmd_inspect(self._namespace(str(empty)))


class InspectParserTest(unittest.TestCase):

    def test_subcommand_is_registered_with_a_positional_and_since(self):
        parser = dsh_upgrade.build_parser()
        args = parser.parse_args(["inspect", "/tmp/x", "--since", "0.1.1-rc.2"])
        self.assertIs(args.func, dsh_upgrade.cmd_inspect)
        self.assertEqual(args.artifact, "/tmp/x")
        self.assertEqual(args.since, "0.1.1-rc.2")


class TargetInventoryTest(unittest.TestCase):
    """The installed core ships vendor packages; they are not "removed".

    A plugin can reference a package that lives in the core's ``vendor/``
    directory (for example ``@deepseek-ai/schemastery``). The host inventory keeps
    only dsh*/cordis* names, so vendor packages used to be missing from the target
    side of the comparison and the plugin was reported as linking to a removed
    package — a false incompatibility on a perfectly installable artifact.
    """

    def test_vendor_packages_count_as_present_in_the_installed_core(self):
        with mock.patch.object(analysis_mod, "installed_core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(analysis_mod, "core_install_dir", return_value=Path("/core")), \
             mock.patch.object(analysis_mod, "host_inventory",
                               return_value={"@deepseek-ai/dsh", "@deepseek-ai/cordis"}), \
             mock.patch.object(analysis_mod.checkout_mod, "ensure_checkout",
                               return_value=Path("/checkout")), \
             mock.patch.object(analysis_mod.checkout_mod, "vendor_names",
                               return_value={"@deepseek-ai/schemastery", "@deepseek-ai/cosmokit"}), \
             mock.patch.object(analysis_mod.checkout_mod, "package_inventory", return_value=set()), \
             mock.patch.object(analysis_mod.checkout_mod, "client_seed_words", return_value=set()), \
             mock.patch.object(analysis_mod.checkout_mod, "preloaded_client_externals", return_value=set()), \
             mock.patch.object(analysis_mod.checkout_mod, "session_format_version", return_value=3), \
             redirect_stdout(io.StringIO()):
            profile = Profile(directory=Path("/profile"), name="web")
            result = analysis_mod.analyse(profile, "0.1.5-rc.2", offline=True, allow_clone=False)

        self.assertIn("@deepseek-ai/schemastery", result.host_packages)
        self.assertIn("@deepseek-ai/dsh", result.host_packages)

    def test_a_package_present_only_in_the_target_checkout_is_not_removed(self):
        """The installed tree is not the whole target: a platform module bundled
        into the shell (``dsh-client-ui-primitives``) has no ``node_modules`` entry
        of its own, so an installed-only inventory called it "removed" and rejected
        a bundle that legitimately requires a seed word."""
        target = Path("/target")
        baseline = Path("/baseline")

        def inventory(checkout):
            if Path(checkout) == target:
                return {"@deepseek-ai/dsh-client-ui-primitives"}
            return {"@deepseek-ai/dsh", "@deepseek-ai/dsh-client-ui-primitives"}

        def ensure(version, **_kwargs):
            return baseline if version == "0.1.1-rc.2" else target

        with mock.patch.object(analysis_mod, "installed_core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(analysis_mod, "core_install_dir", return_value=Path("/core")), \
             mock.patch.object(analysis_mod, "host_inventory",
                               return_value={"@deepseek-ai/dsh"}), \
             mock.patch.object(analysis_mod.checkout_mod, "ensure_checkout", side_effect=ensure), \
             mock.patch.object(analysis_mod.checkout_mod, "vendor_names", return_value=set()), \
             mock.patch.object(analysis_mod.checkout_mod, "package_inventory",
                               side_effect=inventory), \
             mock.patch.object(analysis_mod.checkout_mod, "client_seed_words", return_value=set()), \
             mock.patch.object(analysis_mod.checkout_mod, "preloaded_client_externals",
                               return_value=set()), \
             mock.patch.object(analysis_mod.checkout_mod, "session_format_version", return_value=3), \
             mock.patch.object(analysis_mod.checkout_mod, "client_rows", return_value=set()), \
             mock.patch.object(analysis_mod.checkout_mod, "inline_classification",
                               return_value=(None, None, None)), \
             redirect_stdout(io.StringIO()):
            profile = Profile(directory=Path("/profile"), name="web")
            result = analysis_mod.analyse(profile, "0.1.5-rc.2", offline=True,
                                          allow_clone=False, since="0.1.1-rc.2")

        self.assertNotIn("@deepseek-ai/dsh-client-ui-primitives", result.removed)


if __name__ == "__main__":
    unittest.main()
