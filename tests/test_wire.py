"""Tests for the wire contract: endpoints the core serves vs the calls a plugin makes.

The failure this covers: a client half that POSTs ``/api/session.list`` (the legacy
``namespace.method`` separator) on a core whose gateway serves ``session/list``.
Nothing throws, nothing fails to import — the call answers 404 and the feature
silently does nothing.

The endpoint set is read from the installed core's generated TYPERT faces, so these
tests build a core directory of their own instead of reading the real one.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import compat  # noqa: E402
from dshupgrade import locals as locals_mod  # noqa: E402
from dshupgrade import wire  # noqa: E402
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402


def make_face(root: Path, package: str, invocations: list[tuple[str, str]]) -> Path:
    """A core package publishing one generated TYPERT face."""
    directory = root / "node_modules" / "@deepseek-ai" / package
    (directory / "lib").mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps({
        "name": f"@deepseek-ai/{package}",
        "exports": {"./client": "./lib/client.js", "./typert": "./lib/typert.host.js"},
    }), encoding="utf-8")
    body = "".join(
        "    {\n      service: 'x',\n"
        f"      namespace: '{namespace}',\n      method: '{method}',\n"
        "      invocation: { kind: 'direct' },\n    },\n"
        for namespace, method in invocations)
    (directory / "lib" / "typert.host.js").write_text(
        f"export const TYPERT = {{\n  package: '@deepseek-ai/{package}',\n"
        f"  invocations: [\n{body}  ],\n}}\n",
        encoding="utf-8")
    return directory


def make_plugin(root: Path, name: str, client: str) -> PluginEntry:
    directory = root / "node_modules" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps({
        "name": name, "version": "1.0.0", "main": "index.js",
        "exports": {".": "./index.js", "./client": "./client.js"},
        "dsh": {"client": {"platform": "web"}},
    }), encoding="utf-8")
    (directory / "client.js").write_text(client, encoding="utf-8")
    (directory / "index.js").write_text("export const apply = () => {};\n", encoding="utf-8")
    return PluginEntry(name=name, spec="^1.0.0", source="npm", installed=True,
                       version="1.0.0", directory=str(directory), in_bundles=False)


#: The published 1.0.2 call, abridged to the parts the scan reads.
LEGACY_CALL = (
    'async function fetchSessionCatalog() {\n'
    '  const res = await fetch("/api/session.list", {\n'
    '    method: "POST",\n'
    '    body: JSON.stringify({ type: "client-request", rpcId: "x",\n'
    '                          method: "session.list", payload: {} }),\n'
    '  });\n'
    '  return res.json();\n'
    '}\n')


class CoreEndpointsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_endpoints_come_from_the_typert_faces_only(self):
        make_face(self.root, "dsh-api-session-controller",
                  [("session", "list"), ("session", "create"), ("fileReferences", "list")])
        (self.root / "node_modules" / "@deepseek-ai" / "dsh-widget").mkdir(parents=True)
        (self.root / "node_modules" / "@deepseek-ai" / "dsh-widget" / "package.json").write_text(
            json.dumps({"name": "@deepseek-ai/dsh-widget",
                        "exports": {"./client": "./client.js"}}), encoding="utf-8")
        core = wire.core_endpoints(self.root)
        self.assertEqual(core.packages, 1)
        self.assertEqual(sorted(core.endpoints),
                         ["fileReferences/list", "session/create", "session/list"])

    def test_an_absent_core_yields_no_endpoints_and_no_crash(self):
        self.assertEqual(wire.core_endpoints(self.root / "missing").endpoints, frozenset())

    def test_a_suggestion_stays_inside_the_namespace(self):
        make_face(self.root, "dsh-api-session-controller",
                  [("session", "list"), ("session", "search"), ("workspace", "list")])
        core = wire.core_endpoints(self.root)
        self.assertIn("session/search", core.suggestion("session.search2"))
        self.assertNotIn("workspace/", core.suggestion("session.search2"))

    def test_an_unknown_namespace_falls_back_to_a_flat_note(self):
        make_face(self.root, "dsh-api-session-controller", [("session", "list")])
        core = wire.core_endpoints(self.root)
        self.assertEqual(core.suggestion("ghost/thing"), "")
        call = wire.classify(wire.WireCall("p", "/api/ghost.thing", "ghost.thing"), core)
        self.assertEqual(call.verdict, wire.DEAD)
        self.assertEqual(call.note, "no endpoint in this namespace")


class CallScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_legacy_call_is_read_with_its_own_method(self):
        calls = wire.calls_in_text("cleaner", LEGACY_CALL)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].form, "session.list")
        # The nearest method literal is the envelope's, not the fetch option's POST.
        self.assertEqual(calls[0].method, "session.list")
        self.assertEqual(calls[0].source, "client.js:2")

    def test_a_file_without_the_envelope_is_not_an_rpc_caller(self):
        text = 'const url = "/api/present.open";\nawait fetch(url);\n'
        self.assertEqual(wire.calls_in_text("p", text), [])

    def test_extension_routes_are_not_rpc_endpoints(self):
        text = 'const t = "client-request";\nfetch("/api-ext/session.delete");\n'
        self.assertEqual(wire.calls_in_text("p", text), [])

    def test_a_path_assembled_from_variables_is_not_guessed(self):
        text = 'const t = "client-request";\nfetch(`/api/${method}`);\n'
        self.assertEqual(wire.calls_in_text("p", text), [])

    def test_a_plugin_without_calls_reports_nothing(self):
        make_plugin(self.root, "quiet", "export const nothing = 1;\n")
        self.assertEqual(
            wire.plugin_calls(self.root / "node_modules" / "quiet", "quiet"), [])


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.core = wire.CoreEndpoints(endpoints=frozenset({"session/list"}))
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_legacy_separator_is_dead_and_names_the_successor(self):
        call = wire.classify(wire.WireCall("cleaner", "/api/session.list", "session.list",
                                           "session.list"), self.core)
        self.assertEqual(call.verdict, wire.DEAD)
        self.assertIn("session/list", call.note)

    def test_a_served_call_is_ok(self):
        call = wire.classify(wire.WireCall("p", "/api/session/list", "session/list",
                                           "session/list"), self.core)
        self.assertEqual(call.verdict, wire.OK)
        self.assertEqual(call.note, "")

    def test_a_path_and_method_mismatch_is_reported(self):
        call = wire.classify(wire.WireCall("p", "/api/session/list", "session/list",
                                           "session.list"), self.core)
        self.assertEqual(call.verdict, wire.MISMATCH)
        self.assertIn("session.list", call.note)

    def test_without_a_core_the_call_is_unchecked_not_dead(self):
        call = wire.classify(wire.WireCall("p", "/api/session.list", "session.list"),
                             wire.CoreEndpoints())
        self.assertEqual(call.verdict, wire.UNCHECKED)
        self.assertIn("no installed core", call.note)

    def test_scan_reports_only_the_broken_calls(self):
        make_plugin(self.root, "cleaner", LEGACY_CALL)
        make_plugin(self.root, "quiet", "export const x = 1;\n")
        plugins = [PluginEntry(name=name, spec="^1", source="npm", installed=True,
                               version="1.0.0",
                               directory=str(self.root / "node_modules" / name),
                               in_bundles=False)
                   for name in ("cleaner", "quiet")]
        profile = Profile(directory=self.root, name="web", dependencies={}, bundles=[],
                          plugins=plugins)
        found = wire.scan_profile(profile, self.core)
        self.assertEqual(list(found), ["cleaner"])
        self.assertEqual(found["cleaner"][0].verdict, wire.DEAD)
        self.assertEqual(wire.total(found), 1)

    def test_a_clean_profile_reports_nothing(self):
        make_plugin(self.root, "cleaner",
                    'const t = "client-request";\nfetch("/api/session/list");\n')
        profile = Profile(directory=self.root, name="web", dependencies={}, bundles=[],
                          plugins=[PluginEntry(name="cleaner", spec="^1", source="npm",
                                               installed=True, version="1.0.0",
                                               directory=str(self.root / "node_modules" /
                                                             "cleaner"),
                                               in_bundles=False)])
        self.assertEqual(wire.scan_profile(profile, self.core), {})


class ArtifactWireTest(unittest.TestCase):
    """The pre-install path: an artifact that is not in the profile yet (``inspect``).

    A directory and a tarball are reduced to the same ``name -> text`` mapping, which
    is why one scan serves both and a ``.ts`` source is scanned before it is built.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.core_root = self.root / "core"
        make_face(self.core_root, "dsh-api-session-controller", [("session", "list")])
        self.core = wire.core_endpoints(self.core_root)

    def make_artifact(self) -> Path:
        directory = self.root / "artifact"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "package.json").write_text(json.dumps({
            "name": "sample-augmenter", "version": "1.0.2",
            "main": "lib/index.js",
        }), encoding="utf-8")
        (directory / "client.js").write_text(LEGACY_CALL, encoding="utf-8")
        return directory

    def test_a_directory_is_scanned_before_it_is_installed(self):
        directory = self.make_artifact()
        manifest = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        code = compat.CodeSource.from_directory(directory, manifest)
        broken = wire.check_source("sample-augmenter", code.files, self.core)
        self.assertEqual([call.verdict for call in broken], [wire.DEAD])
        self.assertEqual(broken[0].path, "/api/session.list")
        self.assertTrue(broken[0].source.startswith("client.js:"),
                        f"the line is reported: {broken[0].source}")

    def test_a_tarball_is_scanned_through_the_same_mapping(self):
        directory = self.make_artifact()
        archive = self.root / "artifact.tgz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(directory, arcname="package")
        source = locals_mod.from_path(archive)
        manifest = source.manifest()
        code = source.code_source(manifest)
        self.assertIsNotNone(code)
        broken = wire.check_source("sample-augmenter", code.files, self.core)
        self.assertEqual([call.verdict for call in broken], [wire.DEAD])
        self.assertEqual(broken[0].note.count("session/list"), 1)

    def test_inspect_exits_two_on_a_broken_call(self):
        """The pre-install gate returns the same code as an incompatible declaration."""
        from argparse import Namespace

        import dsh_upgrade

        directory = self.make_artifact()
        args = Namespace(artifact=str(directory), core=None, since=None, offline=True,
                         no_clone=True, json=False)
        stub = mock.Mock()
        stub.incompatible.return_value = False
        stub.plugins = {}
        with mock.patch.object(dsh_upgrade.wire_mod, "core_endpoints",
                               return_value=self.core), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade.analysis_mod, "analyse", return_value=stub), \
             mock.patch.object(dsh_upgrade.report, "print_analysis"), \
             mock.patch.object(dsh_upgrade, "_print_wire"):
            self.assertEqual(dsh_upgrade.cmd_inspect(args), 2)


if __name__ == "__main__":
    unittest.main()
