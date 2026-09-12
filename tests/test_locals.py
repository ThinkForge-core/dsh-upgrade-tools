"""Tests for local plugin sources (home-built packages: file:, link:, tgz).

The key property under test: a plugin that exists neither on npm nor on GitHub is
checked against ITS OWN artifact, and the registry is not queried for it at all —
otherwise the verdict would be about somebody else's product with the same name.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import locals as locals_mod  # noqa: E402
from dshupgrade.compat import (  # noqa: E402
    CodeSource,
    scan_client_modules,
    scan_removed_packages,
)
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402


def _write_tarball(path: Path, files: dict[str, str]) -> Path:
    """Build a tgz the way npm does it: the contents live under ``package/``."""
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


class ResolveSpecTest(unittest.TestCase):
    """Specifier classification: what is local and what comes from the registry."""

    def test_local_prefixes(self):
        for spec, prefix in [("file:/tmp/x.tgz", "file"), ("link:/tmp/repo", "link"),
                             ("workspace:../pkg", "workspace"), ("portal:/tmp/pkg", "portal")]:
            with self.subTest(spec=spec):
                source = locals_mod.resolve(spec, Path("/tmp"))
                self.assertIsNotNone(source)
                self.assertEqual(source.prefix, prefix)
                self.assertTrue(locals_mod.is_local_spec(spec))

    def test_relative_spec_resolved_against_profile(self):
        source = locals_mod.resolve("./local-plugin", Path("/tmp/dsh-home/.dsh/profiles/web"))
        self.assertIsNotNone(source)
        self.assertEqual(source.path, Path("/tmp/dsh-home/.dsh/profiles/web/local-plugin"))
        self.assertFalse(source.available)

    def test_registry_specs_are_not_local(self):
        for spec in ["^0.18.0", "0.1.1-rc.2", "github:user/repo", "https://example.com/x.tgz",
                     "npm:foo@1.0.0", "latest", ""]:
            with self.subTest(spec=spec):
                self.assertIsNone(locals_mod.resolve(spec, Path("/tmp")))
                self.assertFalse(locals_mod.is_local_spec(spec))

    def test_kind_detection(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "repo"
            directory.mkdir()
            tarball = _write_tarball(root / "pkg-1.0.0.tgz", {"package/package.json": "{}"})
            self.assertEqual(locals_mod.resolve(f"link:{directory}").kind, "dir")
            self.assertEqual(locals_mod.resolve(f"file:{tarball}").kind, "tarball")
            self.assertEqual(locals_mod.resolve(f"file:{root / 'gone.tgz'}").kind, "tarball")
            self.assertFalse(locals_mod.resolve(f"file:{root / 'gone.tgz'}").available)


class TarballSourceTest(unittest.TestCase):
    """A hand-built plugin packed into a tgz: manifest and code are read from the archive."""

    MANIFEST = {
        "name": "my-local-plugin",
        "version": "1.2.3",
        "exports": {"./client": "./client.js"},
        "peerDependencies": {"@deepseek-ai/dsh-tools": ">=0.1.0"},
    }

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.tarball = _write_tarball(root / "my-local-plugin-1.2.3.tgz", {
            "package/package.json": json.dumps(self.MANIFEST),
            "package/index.js": 'require("@deepseek-ai/dsh-removed");\nrequire("./relative");',
            "package/client.js": 'import x from "@deepseek-ai/dsh-client-runtime/client";',
            "package/node_modules/dep/index.js": 'require("@deepseek-ai/dsh-removed");',
            "package/README.md": "not code",
        })
        self.source = locals_mod.resolve(f"file:{self.tarball}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_manifest_from_archive(self):
        manifest = self.source.manifest()
        self.assertEqual(manifest["name"], "my-local-plugin")
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(self.source.version(), "1.2.3")

    def test_code_source_skips_non_code_and_own_node_modules(self):
        code = self.source.code_source(self.source.manifest())
        self.assertEqual(sorted(code.files), ["client.js", "index.js"])
        self.assertEqual(code.client_files, {"client.js"})

    def test_removed_package_detected_in_archive(self):
        code = self.source.code_source(self.source.manifest())
        hits = scan_removed_packages(code, {"@deepseek-ai/dsh-removed"})
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["files"], ["index.js"])

    def test_client_module_scan_uses_client_bundle_only(self):
        code = self.source.code_source(self.source.manifest())
        hits = scan_client_modules(code, set(), set())
        self.assertEqual([hit["specifier"] for hit in hits],
                         ["@deepseek-ai/dsh-client-runtime/client"])
        self.assertEqual(hits[0]["files"], ["client.js"])

    def test_manifest_for_entry_by_local_path(self):
        entry = {"name": "my-local-plugin", "spec": "file:/nope/gone.tgz",
                 "localPath": str(self.tarball)}
        manifest = locals_mod.manifest_for_entry(entry)
        self.assertEqual(manifest["version"], "1.2.3")

    def test_broken_archive_is_not_fatal(self):
        with TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.tgz"
            broken.write_text("this is not an archive", encoding="utf-8")
            source = locals_mod.resolve(f"file:{broken}")
            self.assertIsNone(source.manifest())
            self.assertIsNone(source.code_source(None))


class DirectorySourceTest(unittest.TestCase):
    """A repository link: tests and node_modules must not produce false hits."""

    def test_repo_scan_skips_tests_and_node_modules(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "lib").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "node_modules" / "dep").mkdir(parents=True)
            (root / "lib" / "index.js").write_text('require("@deepseek-ai/dsh-gone");', encoding="utf-8")
            (root / "tests" / "fixture.js").write_text('require("@deepseek-ai/dsh-gone");', encoding="utf-8")
            (root / "node_modules" / "dep" / "index.js").write_text(
                'require("@deepseek-ai/dsh-gone");', encoding="utf-8")
            (root / "package.json").write_text(json.dumps({"name": "repo", "version": "1.0.0"}),
                                               encoding="utf-8")

            source = locals_mod.resolve(f"link:{root}")
            code = source.code_source(source.manifest())
            hits = scan_removed_packages(code, {"@deepseek-ai/dsh-gone"})
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["files"], ["lib/index.js"])

    def test_code_source_accepts_plain_directory(self):
        """Compatibility: a directory scan works without the CodeSource wrapper."""
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "client.js").write_text('require("react");', encoding="utf-8")
            self.assertIsInstance(CodeSource.from_directory(directory), CodeSource)
            self.assertEqual(scan_removed_packages(directory, {"react"})[0]["package"], "react")


class LocalAnalysisTest(unittest.TestCase):
    """The main point: the registry is never queried for a local plugin."""

    def _profile(self, root: Path, tarball: Path) -> Profile:
        plugin_dir = root / "profiles" / "web" / "node_modules" / "my-local-plugin"
        entry = PluginEntry(
            name="my-local-plugin",
            spec=f"file:{tarball}",
            source="file",
            installed=False,
            version=None,
            directory=None,
            in_bundles=True,
        )
        return Profile(directory=root / "profiles" / "web", name="web",
                       dependencies={"my-local-plugin": f"file:{tarball}"},
                       bundles=["my-local-plugin"], plugins=[entry])

    def test_local_plugin_checked_without_registry(self):
        from dshupgrade import analysis as analysis_mod

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = _write_tarball(root / "my-local-plugin-1.2.3.tgz", {
                "package/package.json": json.dumps({
                    "name": "my-local-plugin", "version": "1.2.3",
                    "peerDependencies": {"@deepseek-ai/dsh-tools": ">=0.1.0"},
                }),
                "package/index.js": "require('react');",
            })
            profile = self._profile(root, tarball)
            boom = AssertionError("the registry was queried for a local plugin")
            with mock.patch.object(analysis_mod.registry, "manifest", side_effect=boom), \
                 mock.patch.object(analysis_mod.registry, "dist_tags", side_effect=boom), \
                 mock.patch.object(analysis_mod.registry, "all_versions", side_effect=boom):
                result = analysis_mod.analyse(profile, "9.9.9-rc.1", offline=True,
                                              allow_clone=False, log=lambda *_: None)

            entry = result.plugins[0]
            self.assertEqual(entry["manifest"]["version"], "1.2.3")
            self.assertEqual(entry["local"]["kind"], "tarball")
            self.assertEqual(entry["status"], "compatible")
            self.assertTrue(entry["code_checked"])
            self.assertTrue(entry["installable"])

    def test_missing_local_artifact_blocks_install(self):
        from dshupgrade import analysis as analysis_mod

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root, root / "gone.tgz")
            result = analysis_mod.analyse(profile, "9.9.9-rc.1", offline=True,
                                          allow_clone=False, log=lambda *_: None)
            entry = result.plugins[0]
            self.assertFalse(entry["installable"])
            self.assertIsNone(entry["manifest"])
            self.assertIn("local source not found", entry["reason"])

    def test_incompatible_entry_keeps_local_manifest(self):
        from dshupgrade import analysis as analysis_mod

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = _write_tarball(root / "p.tgz", {
                "package/package.json": json.dumps({"name": "p", "version": "1.0.0"}),
            })
            profile = self._profile(root, tarball)
            result = analysis_mod.analyse(profile, "9.9.9-rc.1", offline=True,
                                          allow_clone=False, log=lambda *_: None)
            entry = analysis_mod.incompatible_entry(result.plugins[0])
            # The manifest and the path are kept: without them recheck could not
            # evaluate a local plugin that has no registry.
            self.assertEqual(entry["manifest"]["version"], "1.0.0")
            self.assertEqual(entry["localPath"], str(tarball))
            restored = analysis_mod.manifest_for_entry(
                {"name": "p", "spec": "file:/nope/gone.tgz", "localPath": str(tarball)})
            self.assertEqual(restored["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
