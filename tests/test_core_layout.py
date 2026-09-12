"""The installed core must be read in either ``node_modules`` layout.

A global install (``npm i -g``, nvm) keeps a package's dependencies *inside* the
package — ``<dsh>/node_modules/@deepseek-ai`` — while a hoisted install (a
project-local dependency, or any installer that flattens the tree) keeps them
*beside* it — ``node_modules/@deepseek-ai`` next to ``.../@deepseek-ai/dsh``.

Both hold the same facts, so both must yield the same host inventory, client rows
and wire endpoints. Reading only the nested path made a hoisted install look like
an empty scope, and the degradation was silent: the inventory fell back to the
seed list and plugins whose declarations matched nothing were reported
``unconfirmed`` instead of ``compatible``.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import effects, wire  # noqa: E402
from dshupgrade.host import CURATED_HOST_SEED, host_inventory, installed_client_rows  # noqa: E402
from dshupgrade.paths import host_modules_dir  # noqa: E402


def write_core_package(scope: Path, name: str, *, client: bool = False,
                       endpoints: tuple[tuple[str, str], ...] = ()) -> Path:
    """A core package inside ``scope``, publishing what the tool reads."""
    directory = scope / name
    (directory / "lib").mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": f"@deepseek-ai/{name}", "version": "0.1.1-rc.2"}
    if endpoints:
        manifest["exports"] = {"./typert": "./lib/typert.host.js"}
        body = "".join(f"    {{ namespace: '{ns}', method: '{m}' }},\n"
                       for ns, m in endpoints)
        (directory / "lib" / "typert.host.js").write_text(
            "export const TYPERT = {\n  invocations: [\n" + body + "  ],\n}\n",
            encoding="utf-8")
    if client:
        manifest["dsh"] = {"client": {"inject": []}}
    (directory / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def populate(scope: Path) -> None:
    """The packages every layout test expects to find."""
    write_core_package(scope, "dsh-base", client=True)
    write_core_package(scope, "dsh-client-store", client=True)
    write_core_package(scope, "dsh-api-gateway", endpoints=(("commands", "list"),
                                                            ("session", "list")))


def nested_core(root: Path) -> tuple[Path, Path]:
    """``npm i -g`` shape: dependencies inside the core package."""
    core = root / "node_modules" / "@deepseek-ai" / "dsh"
    (core / "lib").mkdir(parents=True, exist_ok=True)
    (core / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh", "version": "0.1.1-rc.2"}),
        encoding="utf-8")
    scope = core / "node_modules" / "@deepseek-ai"
    scope.mkdir(parents=True)
    populate(scope)
    return core, scope


def hoisted_core(root: Path) -> tuple[Path, Path]:
    """Project-local shape: dependencies beside the core package."""
    scope = root / "node_modules" / "@deepseek-ai"
    core = scope / "dsh"
    (core / "lib").mkdir(parents=True, exist_ok=True)
    (core / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh", "version": "0.1.1-rc.2"}),
        encoding="utf-8")
    populate(scope)
    return core, scope


class BothLayoutsTest(unittest.TestCase):
    """Every fact read from the core is the same in either layout."""

    def test_host_inventory(self):
        for builder in (nested_core, hoisted_core):
            with self.subTest(layout=builder.__name__), TemporaryDirectory() as tmp:
                core, _ = builder(Path(tmp))
                inventory = host_inventory(core)
                self.assertIn("@deepseek-ai/dsh-base", inventory)
                self.assertIn("@deepseek-ai/dsh-client-store", inventory)
                self.assertIn("@deepseek-ai/dsh-api-gateway", inventory)
                # the real inventory, not the 22-name seed fallback
                self.assertGreater(len(inventory), len(CURATED_HOST_SEED))

    def test_client_rows(self):
        for builder in (nested_core, hoisted_core):
            with self.subTest(layout=builder.__name__), TemporaryDirectory() as tmp:
                core, _ = builder(Path(tmp))
                self.assertEqual(installed_client_rows(core),
                                 {"@deepseek-ai/dsh-base", "@deepseek-ai/dsh-client-store"})

    def test_wire_endpoints(self):
        for builder in (nested_core, hoisted_core):
            with self.subTest(layout=builder.__name__), TemporaryDirectory() as tmp:
                core, _ = builder(Path(tmp))
                self.assertEqual(wire.core_endpoints(core).endpoints,
                                 frozenset({"commands/list", "session/list"}))

    def test_a_mounted_row_resolves_into_a_hoisted_core(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            core, _ = hoisted_core(root)
            profile = root / "profiles" / "web"
            profile.mkdir(parents=True)
            found = effects._package_dir("@deepseek-ai/dsh-base", profile, core)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "dsh-base")

    def test_the_nested_tree_wins_when_both_exist(self):
        """The running core resolves against the nested tree, so that one is read."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            core, nested_scope = nested_core(root)
            write_core_package(core.parent, "dsh-only-beside")
            modules = host_modules_dir(core)
            self.assertEqual(modules, core / "node_modules")
            self.assertIn("@deepseek-ai/dsh-base", host_inventory(core))
            self.assertNotIn("@deepseek-ai/dsh-only-beside", host_inventory(core))


class AbsentScopeTest(unittest.TestCase):
    """No layout at all is a missing fact, not an empty inventory."""

    def test_falls_back_to_the_seed_list(self):
        with TemporaryDirectory() as tmp:
            core = Path(tmp) / "dsh"
            (core / "lib").mkdir(parents=True)
            (core / "package.json").write_text(
                json.dumps({"name": "@deepseek-ai/dsh", "version": "0.1.1-rc.2"}),
                encoding="utf-8")
            self.assertIsNone(host_modules_dir(core))
            self.assertEqual(host_inventory(core), set(CURATED_HOST_SEED))
            self.assertEqual(installed_client_rows(core), set())
            # no endpoints is "unchecked", never "every call is dead"
            self.assertEqual(wire.core_endpoints(core).endpoints, frozenset())


if __name__ == "__main__":
    unittest.main()
