"""Tests for the runtime verification probe.

The probe answers questions the declarations cannot: does the INSTALLED copy of the
plugin import at all, and does the code it registers actually run? These tests keep
it honest in four ways: what counts as a client-only package (nothing to import),
how a probe verdict is parsed out of the output, that a route handler broken at
request time is caught even when it swallows its own error, and that the cache is
invalidated when the thing it measured changes.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import paths  # noqa: E402
from dshupgrade import verify as verify_mod  # noqa: E402
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402

#: The real thing, when it is present: the handler test drives an actual plugin.
NODE = shutil.which("node")


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


def fake_node(directory: Path, *, payload: dict | None = None, stderr: str = "",
              exit_code: int = 0) -> Path:
    """A stand-in for ``node``: it prints the payload instead of importing anything."""
    script = [f"#!{sys.executable}", "import sys"]
    if payload is not None:
        script.append(f"print({verify_mod.PROBE_MARKER!r} + {json.dumps(payload)!r})")
    if stderr:
        script.append(f"print({stderr!r}, file=sys.stderr)")
    if exit_code:
        script.append(f"sys.exit({exit_code})")
    path = directory / "fake-node"
    path.write_text("\n".join(script) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class ServerEntryTest(unittest.TestCase):
    """Which packages have something for Node to import."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_exports_root_subpath(self):
        directory = make_plugin(self.root, "a", {"exports": {".": "./lib/index.js"}})
        self.assertEqual(verify_mod.server_entry(directory), ".")

    def test_exports_as_a_string(self):
        directory = make_plugin(self.root, "a", {"exports": "./lib/index.js"})
        self.assertEqual(verify_mod.server_entry(directory), "./lib/index.js")

    def test_exports_without_a_root_subpath_is_client_only(self):
        directory = make_plugin(self.root, "a", {"exports": {"./client": "./lib/client.js"}})
        self.assertIsNone(verify_mod.server_entry(directory))

    def test_main_is_used_when_there_is_no_exports(self):
        directory = make_plugin(self.root, "a", {"main": "lib/index.js"})
        self.assertEqual(verify_mod.server_entry(directory), "lib/index.js")

    def test_a_bare_directory_with_an_index_is_still_importable(self):
        directory = make_plugin(self.root, "a", {}, {"index.js": "export const x = 1;"})
        self.assertEqual(verify_mod.server_entry(directory), "index.js")

    def test_nothing_to_import(self):
        directory = make_plugin(self.root, "a", {"dsh": {"client": {"inject": []}}})
        self.assertIsNone(verify_mod.server_entry(directory))


class ParseProbeTest(unittest.TestCase):
    """The verdict is one marker line, even if the plugin printed something."""

    def test_a_clean_line(self):
        payload = verify_mod._parse_probe(verify_mod.PROBE_MARKER + '{"ok": true}')
        self.assertEqual(payload, {"ok": True})

    def test_plugin_output_around_it(self):
        stdout = ("hello from the plugin\n"
                  + verify_mod.PROBE_MARKER + '{"ok": false, "error": "boom"}\n'
                  + "trailing\n")
        self.assertEqual(verify_mod._parse_probe(stdout), {"ok": False, "error": "boom"})

    def test_garbage_is_no_verdict(self):
        self.assertIsNone(verify_mod._parse_probe("nothing here"))
        self.assertIsNone(verify_mod._parse_probe(verify_mod.PROBE_MARKER + "not json"))


class ProbeOneTest(unittest.TestCase):
    """Running the probe (against a stand-in for node)."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / "profiles" / "web"
        self.profile.mkdir(parents=True)

    def test_a_successful_import(self):
        node = fake_node(self.root, payload={"ok": True, "ms": 12, "keys": ["apply"]})
        probe = verify_mod.probe_one("demo", self.profile, node=str(node))
        self.assertEqual(probe.status, verify_mod.LOADS)
        self.assertEqual(probe.label, "yes")
        self.assertEqual(probe.exports, ["apply"])
        self.assertEqual(probe.ms, 12)

    def test_a_failing_import_keeps_the_message(self):
        node = fake_node(self.root, payload={"ok": False, "error": "Cannot find module X"})
        probe = verify_mod.probe_one("demo", self.profile, node=str(node))
        self.assertEqual(probe.status, verify_mod.FAILED)
        self.assertEqual(probe.label, "no")
        self.assertIn("Cannot find module X", probe.detail)

    def test_a_silent_failure_is_still_a_failure(self):
        node = fake_node(self.root, stderr="kaboom", exit_code=1)
        probe = verify_mod.probe_one("demo", self.profile, node=str(node))
        self.assertEqual(probe.status, verify_mod.FAILED)
        self.assertIn("kaboom", probe.detail)

    def test_a_client_only_package_is_not_executed(self):
        with mock.patch.object(verify_mod.subprocess, "run") as run:
            probe = verify_mod.probe_one("demo", self.profile, node="/bin/true", entry=None)
        self.assertEqual(probe.status, verify_mod.CLIENT_ONLY)
        run.assert_not_called()

    def test_without_node_nothing_is_claimed(self):
        with mock.patch.object(verify_mod, "node_binary", return_value=None):
            probe = verify_mod.probe_one("demo", self.profile)
        self.assertEqual(probe.status, verify_mod.UNAVAILABLE)
        self.assertEqual(probe.label, "?")


class ProbeProfileTest(unittest.TestCase):
    """The profile probe: installed plugins only, in the profile directory."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile_dir = self.root / "profiles" / "web"
        self.profile_dir.mkdir(parents=True)
        self.node = fake_node(self.root, payload={"ok": True, "ms": 1})

    def profile(self, entries) -> Profile:
        return Profile(directory=self.profile_dir, name="web", plugins=entries)

    def entry(self, name: str, version: str = "1.0.0", installed: bool = True) -> PluginEntry:
        return PluginEntry(name=name, spec=version, source="npm", installed=installed,
                           version=version,
                           directory=str(self.profile_dir / "node_modules" / name)
                           if installed else None,
                           in_bundles=True)

    def test_every_installed_plugin_is_probed(self):
        make_plugin(self.profile_dir, "demo", {"main": "index.js"})
        probes = verify_mod.probe_profile(self.profile([self.entry("demo")]), node=str(self.node))
        self.assertEqual(probes["demo"].status, verify_mod.LOADS)

    def test_a_missing_plugin_is_marked_missing_not_probed(self):
        probes = verify_mod.probe_profile(self.profile([self.entry("gone", installed=False)]),
                                          node=str(self.node))
        self.assertEqual(probes["gone"].status, verify_mod.MISSING)
        self.assertEqual(probes["gone"].label, "—")


class CacheTest(unittest.TestCase):
    """The cache is only used while what it measured is unchanged."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        state = mock.patch.dict(os.environ, {"DSH_UPGRADE_STATE": str(self.root / "state")})
        state.start()
        self.addCleanup(state.stop)
        self.profile_dir = self.root / "profiles" / "web"
        self.profile_dir.mkdir(parents=True)
        self.directory = make_plugin(self.profile_dir, "demo", {"main": "index.js"})

    def profile(self) -> Profile:
        return Profile(
            directory=self.profile_dir, name="web",
            plugins=[PluginEntry(name="demo", spec="1.0.0", source="npm", installed=True,
                                 version="1.0.0", directory=str(self.directory), in_bundles=True)],
        )

    def test_round_trip(self):
        probes = {"demo": verify_mod.Probe("demo", verify_mod.LOADS, ms=5, exports=["apply"])}
        verify_mod.save_cache(self.profile(), "0.1.5-rc.2", probes)
        cached = verify_mod.load_cache(self.profile(), "0.1.5-rc.2")
        self.assertEqual(cached["demo"].status, verify_mod.LOADS)
        self.assertEqual(cached["demo"].exports, ["apply"])

    def test_a_different_core_invalidates_the_cache(self):
        verify_mod.save_cache(self.profile(), "0.1.5-rc.2",
                              {"demo": verify_mod.Probe("demo", verify_mod.LOADS)})
        self.assertEqual(verify_mod.load_cache(self.profile(), "0.1.6-rc.1"), {})

    def test_a_changed_plugin_copy_invalidates_the_cache(self):
        verify_mod.save_cache(self.profile(), "0.1.5-rc.2",
                              {"demo": verify_mod.Probe("demo", verify_mod.LOADS)})
        manifest = self.directory / "package.json"
        os.utime(manifest, (manifest.stat().st_atime + 10, manifest.stat().st_mtime + 10))
        self.assertEqual(verify_mod.load_cache(self.profile(), "0.1.5-rc.2"), {})

    def test_handler_evidence_survives_the_cache(self):
        probes = {"demo": verify_mod.Probe(
            "demo", verify_mod.LOADS,
            handlers=[{"path": "/x", "error": "boom", "logs": ["log: failed: boom"]}],
            notes=["handler not called: /y (the path names a mutation and the probe only sends GET)"])}
        verify_mod.save_cache(self.profile(), "0.1.5-rc.2", probes)
        cached = verify_mod.load_cache(self.profile(), "0.1.5-rc.2")
        self.assertEqual(cached["demo"].handlers[0]["error"], "boom")
        self.assertEqual(len(cached["demo"].notes), 1)

    def test_a_weaker_probe_does_not_overwrite_the_cache(self):
        """--no-handlers answers a smaller question, so its verdict is not kept."""
        with mock.patch.object(verify_mod, "probe_profile",
                               return_value={"demo": verify_mod.Probe("demo", verify_mod.LOADS)}) as probe:
            verify_mod.resolve(self.profile(), "0.1.5-rc.2", handlers=False)
        probe.assert_called_once()
        self.assertFalse(probe.call_args.kwargs["handlers"])
        self.assertEqual(verify_mod.load_cache(self.profile(), "0.1.5-rc.2"), {})

    def test_without_a_cache_nothing_is_probed_when_that_is_forbidden(self):
        with mock.patch.object(verify_mod, "probe_profile") as probe:
            results = verify_mod.resolve(self.profile(), "0.1.5-rc.2", allow_probe=False)
        self.assertEqual(results, {})
        probe.assert_not_called()

    def test_resolve_probes_and_caches(self):
        with mock.patch.object(verify_mod, "probe_profile",
                               return_value={"demo": verify_mod.Probe("demo", verify_mod.LOADS)}) as probe:
            results = verify_mod.resolve(self.profile(), "0.1.5-rc.2")
        self.assertEqual(results["demo"].status, verify_mod.LOADS)
        probe.assert_called_once()
        # A second call is served from the cache.
        with mock.patch.object(verify_mod, "probe_profile") as again:
            verify_mod.resolve(self.profile(), "0.1.5-rc.2")
        again.assert_not_called()

    def test_refresh_ignores_a_valid_cache(self):
        verify_mod.save_cache(self.profile(), "0.1.5-rc.2",
                              {"demo": verify_mod.Probe("demo", verify_mod.FAILED, "old")})
        with mock.patch.object(verify_mod, "probe_profile",
                               return_value={"demo": verify_mod.Probe("demo", verify_mod.LOADS)}) as probe:
            results = verify_mod.resolve(self.profile(), "0.1.5-rc.2", refresh=True)
        probe.assert_called_once()
        self.assertEqual(results["demo"].status, verify_mod.LOADS)


class LabelTest(unittest.TestCase):
    """Every status has a label and an explanation — the column must never be a mystery."""

    def test_labels_and_legends_cover_every_status(self):
        for status in (verify_mod.LOADS, verify_mod.FAILED, verify_mod.CLIENT_ONLY,
                       verify_mod.MISSING, verify_mod.UNAVAILABLE):
            self.assertIn(status, verify_mod.LABELS)
            self.assertIn(status, verify_mod.LEGEND)
            self.assertTrue(verify_mod.LABELS[status])
            self.assertTrue(verify_mod.LEGEND[status])

    def test_an_unknown_status_degrades_to_a_question_mark(self):
        self.assertEqual(verify_mod.Probe("demo", "something-new").label, "?")


class HandlerProbeTest(unittest.TestCase):
    """Calling the route handlers is what catches a request-time failure.

    The first plugin below has that shape: a ``ReferenceError`` inside a handler
    that catches its own error, logs it and answers ``200 []``. It imports cleanly,
    ``apply()`` runs, the route is registered, and a live GET answers 200 — so
    calling the handler and reading what it logged is the only thing that sees the
    breakage.
    """

    SWALLOWED = """
export function apply(ctx) {
  ctx.inject(["webServer"], (sctx) => {
    const handler = async (req, res) => {
      try {
        const cachePath = SOME_CACHE;
        res.writeHead(200, { "content-type": "application/json" });
        res.end("[]");
      } catch (err) {
        console.log("[demo] models route: read failed:", String(err));
        res.writeHead(200, { "content-type": "application/json" });
        res.end("[]");
      }
    };
    ctx.effect(() => sctx.webServer.register({ kind: "exact", path: "/demo/models", handler }));
  });
}
"""

    WORKING = """
export function apply(ctx) {
  ctx.inject(["webServer"], (sctx) => {
    sctx.webServer.register({ kind: "exact", path: "/demo/models",
      handler: async (req, res) => {
        res.writeHead(200, { "content-type": "application/json" });
        res.end("[]");
      } });
  });
}
"""

    MUTATING = """
export function apply(ctx) {
  ctx.inject(["webServer"], (sctx) => {
    sctx.webServer.register({ kind: "exact", path: "/demo/session/delete",
      handler: async (req, res) => { res.end("deleted"); } });
  });
}
"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / "profiles" / "web"
        self.profile.mkdir(parents=True)

    def install(self, source: str):
        make_plugin(self.profile, "demo", {"exports": {".": "./index.js"}},
                    {"index.js": source})

    @unittest.skipIf(NODE is None, "node is not available")
    def test_a_handler_that_swallows_its_own_error_is_caught(self):
        self.install(self.SWALLOWED)
        probe = verify_mod.probe_one("demo", self.profile, node=NODE)
        self.assertEqual(probe.status, verify_mod.LOADS)
        self.assertEqual(len(probe.handlers), 1)
        reason = verify_mod.handler_failure(probe.handlers[0])
        self.assertIsNotNone(reason)
        self.assertIn("is not defined", reason)
        # It answered 200 anyway: the cell must not call this healthy.
        self.assertEqual(probe.handlers[0]["status"], 200)
        self.assertEqual(verify_mod.surface_text(probe), verify_mod.HANDLER_FAILED)

    @unittest.skipIf(NODE is None, "node is not available")
    def test_a_working_handler_is_not_flagged(self):
        self.install(self.WORKING)
        probe = verify_mod.probe_one("demo", self.profile, node=NODE)
        self.assertEqual(probe.handlers[0]["status"], 200)
        self.assertIsNone(verify_mod.handler_failure(probe.handlers[0]))
        self.assertIsNone(verify_mod.handler_suspect(probe.handlers[0]))
        self.assertEqual(verify_mod.surface_text(probe), "routes:1")

    @unittest.skipIf(NODE is None, "node is not available")
    def test_handler_calls_can_be_switched_off(self):
        self.install(self.SWALLOWED)
        probe = verify_mod.probe_one("demo", self.profile, node=NODE, handlers=False)
        self.assertEqual(probe.status, verify_mod.LOADS)
        self.assertEqual(probe.handlers, [])
        # The weaker probe really does miss it — that is why the default is on.
        self.assertEqual(verify_mod.surface_text(probe), "routes:1")

    @unittest.skipIf(NODE is None, "node is not available")
    def test_a_mutating_route_is_recorded_but_not_called(self):
        self.install(self.MUTATING)
        probe = verify_mod.probe_one("demo", self.profile, node=NODE)
        self.assertEqual(probe.handlers, [])
        self.assertEqual(probe.route_paths, ["/demo/session/delete"])
        self.assertTrue(any("not called" in note for note in probe.notes))


class StubFidelityTest(unittest.TestCase):
    """Two handler patterns that LOOK stub-induced but are real host APIs.

    Both were once reported against working plugins, and both were checked against
    the installed core before being excused, so the stub must keep serving them:

    * ``ctx.get(name)`` is a real cordis ``Context`` method (``ReflectService.get``,
      declared in ``cordis/lib/types/reflect.d.ts``); ``dsh-usage-chart`` reads
      ``ctx.get("credentials")`` on purpose, because touching an un-injected service
      as a property throws. Running that plugin's ``apply()`` on a real cordis
      ``Context`` registers its five routes and answers ``/balance`` with ``200``.
    * the ``req`` a route handler receives is the raw ``node:http``
      ``IncomingMessage`` — ``dsh-host-webserver`` builds it with
      ``createServer((req, res) => route.handler(req, res))`` — so a handler may
      consume the body with ``for await (const chunk of req)``. Driving
      ``dsh-model-by-preset``'s debug sink with a real POST writes the log line.
    """

    REAL_APIS = """
export function apply(ctx) {
  ctx.inject(["webServer"], (sctx) => {
    sctx.webServer.register({ kind: "exact", path: "/demo/probe",
      handler: async (req, res) => {
        // Real cordis: ctx.get(name) reads a service without injecting it.
        const credentials = ctx.get("credentials");
        const value = credentials === undefined
          ? ""
          : ((await credentials.resolve("DEEPSEEK_API_KEY"))?.value ?? "");
        // Real webServer: req is a node:http IncomingMessage, hence async-iterable.
        let body = "";
        for await (const chunk of req) body += chunk.toString("utf8");
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ value: value !== "", body }));
      } });
  });
}
"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / "profiles" / "web"
        self.profile.mkdir(parents=True)

    @unittest.skipIf(NODE is None, "node is not available")
    def test_a_handler_using_real_host_apis_is_not_flagged(self):
        make_plugin(self.profile, "demo", {"exports": {".": "./index.js"}},
                    {"index.js": self.REAL_APIS})
        probe = verify_mod.probe_one("demo", self.profile, node=NODE)
        record = probe.handlers[0]
        self.assertEqual(record["error"], None)
        self.assertEqual(record["status"], 200)
        # Iterating the synthetic request really yields an empty body, not an error.
        self.assertIn('"body":""', record["body"])
        self.assertIsNone(verify_mod.handler_failure(record))
        self.assertIsNone(verify_mod.handler_suspect(record))
        self.assertEqual(verify_mod.surface_text(probe), "routes:1")


class HandlerClassificationTest(unittest.TestCase):
    """The classifier reads what a handler did, without a live host.

    The split it enforces is the one that keeps the feature honest: a verdict only
    for evidence the recording stub cannot have produced (a ``ReferenceError``),
    a lead for everything else, with the line attached.
    """

    def test_a_reference_error_is_the_verdict(self):
        """An undeclared binding is undeclared under any context, stub or not."""
        self.assertTrue(verify_mod.handler_failure(
            {"error": "ReferenceError: SOME_CACHE is not defined"}))
        self.assertTrue(verify_mod.handler_failure(
            {"logs": ["log: read failed: ReferenceError: X is not defined"]}))

    def test_a_stub_inducible_throw_is_a_lead_not_a_verdict(self):
        """The recording context is a stub; a TypeError from it is not the plugin's fault."""
        record = {"error": "TypeError: resolved?.value.trim is not a function"}
        self.assertIsNone(verify_mod.handler_failure(record))
        reason = verify_mod.handler_suspect(record)
        self.assertIsNotNone(reason)
        self.assertIn("stub", reason)

    def test_a_stub_inducible_log_is_a_lead_not_a_verdict(self):
        record = {"logs": ["log: failed: ctx.get is not a function"]}
        self.assertIsNone(verify_mod.handler_failure(record))
        self.assertIsNotNone(verify_mod.handler_suspect(record))

    def test_a_missing_optional_file_is_a_lead_not_a_verdict(self):
        """A handler that falls back on purpose logs the same line a broken one does."""
        record = {"logs": ["log: read failed: Error: ENOENT: no such file, open '/x'"]}
        self.assertIsNone(verify_mod.handler_failure(record))
        self.assertIsNotNone(verify_mod.handler_suspect(record))

    def test_an_error_level_log_is_a_lead(self):
        record = {"logs": ["error: boom"]}
        self.assertIsNone(verify_mod.handler_failure(record))
        self.assertIn("logged an error", verify_mod.handler_suspect(record))

    def test_a_failure_class_in_a_plain_log_is_read(self):
        # The point of the case: console.log is the only trace that is left.
        record = {"logs": ["log: read failed: ReferenceError: X is not defined"]}
        self.assertIn("logged a failure", verify_mod.handler_failure(record))

    def test_an_ordinary_log_is_not_even_a_lead(self):
        self.assertIsNone(verify_mod.handler_failure({"logs": ["log: read 2048 bytes"]}))
        self.assertIsNone(verify_mod.handler_suspect({"logs": ["log: read 2048 bytes"]}))
        self.assertIsNone(verify_mod.handler_suspect({"logs": ["log: 5 models"]}))

    def test_a_warning_is_a_lead(self):
        record = {"logs": ["warn: cache missing"]}
        self.assertIsNone(verify_mod.handler_failure(record))
        self.assertIsNotNone(verify_mod.handler_suspect(record))

    def test_not_finishing_is_a_lead(self):
        record = {"timedOut": True}
        self.assertIsNone(verify_mod.handler_failure(record))
        self.assertIsNotNone(verify_mod.handler_suspect(record))

    def test_a_verdict_is_never_also_a_lead(self):
        self.assertIsNone(verify_mod.handler_suspect(
            {"logs": ["error: ReferenceError: X is not defined"]}))


if __name__ == "__main__":
    unittest.main()
