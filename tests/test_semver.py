"""Tests for version semantics and compatibility checks (stdlib unittest).

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import semver  # noqa: E402
from dshupgrade.compat import (  # noqa: E402
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
    STATUS_UNKNOWN,
    classify_failure,
    declarations_for,
    evaluate,
    required_specifiers,
    scan_removed_packages,
    _is_module_specifier,
)


class SemverTest(unittest.TestCase):
    """Ranges that actually occur in plugin peer dependencies."""

    CASES = [
        ("0.1.5-rc.1", "^0.1.1-rc.2", True),
        ("0.1.5-rc.2", "^0.1.5-rc.1", True),
        ("0.1.1-rc.2", "^0.1.2-rc.1", False),
        ("0.1.1-rc.2", ">=0.1.5-rc.1", False),
        ("0.1.5-rc.2", ">=0.1.0-rc.6", True),
        ("0.1.5-rc.2", ">=0.1.0-rc.6 <0.2.0", True),
        ("0.1.5-rc.2", "^0.1.0-rc.7 || ^0.1.1-rc.2 || ^0.1.2-alpha.2", True),
        ("0.1.5-rc.2", "*", True),
        ("0.1.5-rc.2", "0.1.1-rc.2", False),
        ("0.1.1-rc.2", "0.1.1-rc.2", True),
        ("0.2.0", "^0.1.1-rc.2", False),
        ("0.1.5-rc.2", "^0.1.1-rc.2", True),
        ("2.3.8", "^1.0.0", False),
        ("0.1.5-rc.2", "~0.1.5-rc.1", True),
        # ~ allows patch releases inside a minor: 0.1.6 matches, 0.2.0 does not.
        ("0.1.6", "~0.1.5-rc.1", True),
        ("0.2.0", "~0.1.5-rc.1", False),
    ]

    def test_ranges(self):
        for version, range_text, expected in self.CASES:
            with self.subTest(version=version, range=range_text):
                self.assertIs(semver.satisfies(version, range_text), expected)

    def test_unparseable_range_is_unknown(self):
        self.assertIsNone(semver.satisfies("0.1.5", "not-a-range"))

    def test_order(self):
        self.assertEqual(semver.compare_version("0.1.5-rc.1", "0.1.5-rc.2"), -1)
        self.assertEqual(semver.compare_version("0.1.5-rc.2", "0.1.5"), -1)
        self.assertEqual(semver.compare_version("0.1.5", "0.1.5"), 0)

    def test_max_satisfying(self):
        versions = ["0.1.1-rc.2", "0.1.5-rc.1", "0.1.5-rc.2", "0.2.0"]
        self.assertEqual(semver.max_satisfying(versions, "^0.1.0-rc.7 || ^0.1.1-rc.2"), "0.1.5-rc.2")


class SpecifierTest(unittest.TestCase):
    """Module name filter — protection against junk from minified code."""

    GOOD = ["react", "@deepseek-ai/dsh-client-runtime/client", "lodash", "a/b"]
    BAD = [",(0,c.jsxs)(", "${spec}", "a b", "P.branch??", "", "@", "foo/bar baz"]

    def test_specifier_shape(self):
        for value in self.GOOD:
            with self.subTest(value=value):
                self.assertTrue(_is_module_specifier(value))
        for value in self.BAD:
            with self.subTest(value=value):
                self.assertFalse(_is_module_specifier(value))


class DeclarationTest(unittest.TestCase):
    """Declarations are read in BOTH places — otherwise the requirement is invisible."""

    HOST = {"@deepseek-ai/dsh", "@deepseek-ai/dsh-tools", "@deepseek-ai/dsh-client-runtime"}

    def test_top_level_engines(self):
        manifest = {"engines": {"dsh": ">=0.1.2-rc.1"}}
        kinds = [d.kind for d in declarations_for(manifest, self.HOST)]
        self.assertEqual(kinds, ["engine"])

    def test_nested_engines(self):
        manifest = {"dsh": {"engines": {"dsh": ">=0.1.5-rc.1"}}}
        declarations = declarations_for(manifest, self.HOST)
        self.assertEqual([d.kind for d in declarations], ["engine-nested"])
        self.assertEqual(declarations[0].range, ">=0.1.5-rc.1")

    def test_peers_filtered_by_host_inventory(self):
        manifest = {"peerDependencies": {
            "@deepseek-ai/dsh-tools": "^0.1.0-rc.6",
            "@deepseek-ai/dsh-unknown-pkg": "^1.0.0",
            "react": "^18.2.0",
        }}
        declarations = declarations_for(manifest, self.HOST)
        self.assertEqual(len(declarations), 1)
        self.assertEqual(declarations[0].package, "@deepseek-ai/dsh-tools")

    def test_nested_engines_still_notice_incompatibility(self):
        """A requirement nested under dsh.engines is still one the marketplace misses."""
        manifest = {"dsh": {"engines": {"dsh": ">=0.1.5-rc.1"}}}
        verdict = evaluate("0.1.1-rc.2", declarations_for(manifest, self.HOST))
        self.assertEqual(verdict.status, STATUS_INCOMPATIBLE)
        self.assertEqual(verdict.declarations[0].direction, "below-min")


class EvaluateTest(unittest.TestCase):

    def test_compatible(self):
        manifest = {"peerDependencies": {"@deepseek-ai/dsh-tools": "^0.1.0-rc.6"}}
        verdict = evaluate("0.1.5-rc.2", declarations_for(manifest, DeclarationTest.HOST))
        self.assertEqual(verdict.status, STATUS_COMPATIBLE)

    def test_undeclared_is_unknown(self):
        verdict = evaluate("0.1.5-rc.2", [])
        self.assertEqual(verdict.status, STATUS_UNKNOWN)
        self.assertEqual(verdict.basis, "undeclared")

    def test_exact_pin_is_incompatible(self):
        manifest = {"peerDependencies": {"@deepseek-ai/dsh-tools": "0.1.1-rc.2"}}
        verdict = evaluate("0.1.5-rc.2", declarations_for(manifest, DeclarationTest.HOST))
        self.assertEqual(verdict.status, STATUS_INCOMPATIBLE)
        self.assertEqual(verdict.declarations[0].direction, "exact-pin")

    def test_implicit_caret_ceiling_is_not_incompatible(self):
        """^0.0.1 was never meant as a host ceiling — that is a warning, not a risk."""
        self.assertEqual(classify_failure("0.1.5-rc.2", "^0.0.1"), "above-implicit-ceiling")
        self.assertEqual(classify_failure("0.1.5-rc.2", "^0.1.1-rc.2"), "above-implicit-ceiling")
        self.assertEqual(classify_failure("0.1.1-rc.2", "^0.1.2-rc.1"), "below-min")
        self.assertEqual(classify_failure("0.1.5-rc.2", ">=0.1.0 <0.1.2"), "above-explicit-max")
        self.assertEqual(classify_failure("0.1.5-rc.2", "0.1.1-rc.2"), "exact-pin")


class CodeScanTest(unittest.TestCase):
    """Code checks: real requires only, no junk and no profile node_modules."""

    def _plugin(self, directory: Path, client_js: str, other_js: str = "") -> Path:
        (directory / "package.json").write_text('{"name":"x","version":"1.0.0"}', encoding="utf-8")
        (directory / "client.js").write_text(client_js, encoding="utf-8")
        if other_js:
            (directory / "index.js").write_text(other_js, encoding="utf-8")
        nested = directory / "node_modules" / "dep"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "index.js").write_text('require("should-be-ignored")', encoding="utf-8")
        return directory

    def test_removed_package_detected(self):
        with TemporaryDirectory() as tmp:
            plugin = self._plugin(
                Path(tmp),
                'let x = require("@deepseek-ai/dsh-client-runtime/client");',
                'require("@deepseek-ai/dsh-tools");',
            )
            hits = scan_removed_packages(plugin, {"@deepseek-ai/dsh-client-runtime"})
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["specifier"], "@deepseek-ai/dsh-client-runtime/client")
            self.assertEqual(hits[0]["files"], ["client.js"])

    def test_own_node_modules_ignored(self):
        with TemporaryDirectory() as tmp:
            plugin = self._plugin(Path(tmp), 'require("react");')
            self.assertNotIn("should-be-ignored", required_specifiers(plugin))

    def test_jsx_garbage_not_treated_as_module(self):
        with TemporaryDirectory() as tmp:
            plugin = self._plugin(
                Path(tmp),
                'var a=1;var s=`${spec}`;x({children:[(0,c.jsxs)(A,{from:"P.branch??"})]});',
            )
            self.assertEqual(required_specifiers(plugin), {})


class SnapshotPathTest(unittest.TestCase):
    """The file name of the incompatible list must survive the dots of a version."""

    def test_version_dots_preserved(self):
        from dshupgrade import snapshot

        json_path, md_path = snapshot.incompatible_path("0.1.5-rc.2")
        self.assertEqual(json_path.name, "incompatible-0.1.5-rc.2.json")
        self.assertEqual(md_path.name, "incompatible-0.1.5-rc.2.md")


if __name__ == "__main__":
    unittest.main()
