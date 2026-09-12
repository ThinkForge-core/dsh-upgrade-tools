"""Tests for the update selection of ``plugins`` and for ``--install-unknown``.

``plugins`` updates the plugins that have a newer version and touches nothing else:
a plugin with nothing newer is not detached, not reinstalled and not dropped, so a run
can never leave it out of the profile. A newer version that is merely unconfirmed is
held back and named in the report until ``--install-unknown`` asks for it, and a
version the code checks proved incompatible is never installed at all. Reinstalling
the profile as a whole is what ``detach`` + ``attach`` is for.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import analysis as analysis_mod  # noqa: E402
from dshupgrade import compat  # noqa: E402
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402

VERIFIED = compat.STATUS_VERIFIED
COMPATIBLE = compat.STATUS_COMPATIBLE
UNKNOWN = compat.STATUS_UNKNOWN
INCOMPATIBLE = compat.STATUS_INCOMPATIBLE

CORE = "0.1.5-rc.2"


def analysis_with(*entries, target: str = CORE, current: str = CORE):
    result = analysis_mod.Analysis(target=target, current_core=current, checkout=None)
    result.plugins = list(entries)
    return result


def plugin_entry(name: str, status: str = VERIFIED, *, version: str = "1.0.0",
                 source: str = "npm", installable: bool = True, **extra) -> dict:
    base = {
        "name": name,
        "status": status,
        "reason": "",
        "version": version,
        "source": source,
        "spec": f"{name}@{version}",
        "installable": installable,
        "requirement": None,
        "manifest": {"name": name, "version": version},
        "empirical": False,
    }
    base.update(extra)
    return base


def snapshot_entry(name: str, *, version: str = "1.0.0", source: str = "npm") -> dict:
    return {"name": name, "version": version, "source": source, "spec": f"{name}@{version}",
            "manifest": {"name": name, "version": version}}


def profile_with(*names: str) -> Profile:
    return Profile(
        directory=Path("/tmp/web"), name="web",
        plugins=[PluginEntry(name=name, spec=name, source="npm", installed=True,
                             version="1.0.0", directory=None, in_bundles=True)
                 for name in names])


def plugins_args(**overrides) -> SimpleNamespace:
    values = {
        "profile": "web", "core": None, "offline": False, "no_clone": False,
        "verbose": False, "json": False, "summary": False, "state_dir": None,
        "checkouts": None, "update": False, "detach_first": False,
        "install_unknown": False, "dry_run": True, "yes": False, "only": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class VersionComparisonTest(unittest.TestCase):
    """``_is_newer`` decides what counts as an update, so it has to be strict."""

    def test_a_higher_version_is_newer(self):
        self.assertTrue(dsh_upgrade._is_newer("1.2.0", "1.1.9"))

    def test_the_same_version_is_not_an_update(self):
        self.assertFalse(dsh_upgrade._is_newer("1.0.0", "1.0.0"))

    def test_a_lower_version_is_not_an_update(self):
        self.assertFalse(dsh_upgrade._is_newer("1.0.0", "1.2.0"))

    def test_a_missing_side_is_not_an_update(self):
        for candidate, base in (("1.2.0", None), (None, "1.0.0"), ("", "")):
            with self.subTest(candidate=candidate, base=base):
                self.assertFalse(dsh_upgrade._is_newer(candidate, base))

    def test_an_unparseable_version_is_not_an_update(self):
        self.assertFalse(dsh_upgrade._is_newer("not-a-version", "1.0.0"))

    def test_a_prerelease_ordering_is_the_semver_one(self):
        self.assertTrue(dsh_upgrade._is_newer("0.1.5-rc.2", "0.1.5-rc.1"))
        self.assertFalse(dsh_upgrade._is_newer("0.1.5-rc.1", "0.1.5-rc.2"))


class UpdateCandidateTest(unittest.TestCase):
    """What counts as an update, and whether it is confirmed, per source kind."""

    def test_only_the_npm_source_can_have_an_update(self):
        for source in ("file", "link", "github", None):
            with self.subTest(source=source):
                item = plugin_entry("a", source=source)
                self.assertIsNone(self.candidate(item, {"a": "2.0.0"}))

    def candidate(self, item, compatible):
        result = analysis_with(item)
        with mock.patch.object(analysis_mod, "best_compatible_version",
                               side_effect=lambda name, *a, **k: compatible.get(name)):
            return dsh_upgrade._update_candidate(item, result, offline=False)

    def test_a_newer_compatible_version_is_a_confirmed_update(self):
        self.assertEqual(self.candidate(plugin_entry("a"), {"a": "1.4.0"}), ("1.4.0", True))

    def test_the_installed_version_is_not_an_update(self):
        self.assertIsNone(self.candidate(plugin_entry("a", version="1.4.0"), {"a": "1.4.0"}))

    def test_a_newer_version_the_registry_offers_is_a_candidate_even_unconfirmed(self):
        """Not being confirmed is what the opt-in decides about, not existence."""
        item = plugin_entry("a", latest="1.8.0", latest_status=UNKNOWN)
        self.assertEqual(self.candidate(item, {}), ("1.8.0", False))

    def test_a_proven_incompatible_latest_is_not_a_candidate(self):
        item = plugin_entry("a", latest="2.0.0", latest_status=INCOMPATIBLE)
        self.assertIsNone(self.candidate(item, {}))

    def test_a_scope_and_a_prerelease_still_compare(self):
        item = plugin_entry("@scope/pkg", version="0.1.5-rc.1")
        self.assertEqual(self.candidate(item, {"@scope/pkg": "0.1.5-rc.2"}),
                         ("0.1.5-rc.2", True))


class SkipReasonTest(unittest.TestCase):
    """A plugin left alone is reported with the reason, not silently dropped."""

    def test_a_local_source_says_it_has_no_registry_version(self):
        reason = dsh_upgrade._update_skip_reason(plugin_entry("a", source="link"), offline=False)
        self.assertIn("link source", reason)
        self.assertIn("no registry version", reason)

    def test_offline_says_the_registry_was_not_queried(self):
        self.assertIn("--offline",
                      dsh_upgrade._update_skip_reason(plugin_entry("a"), offline=True))

    def test_a_current_version_says_there_is_none_newer(self):
        reason = dsh_upgrade._update_skip_reason(plugin_entry("a", latest="1.0.0"), offline=False)
        self.assertIn("no newer version", reason)


class InstallSpecTest(unittest.TestCase):
    """The specifier list: pinned decisions win, and nothing is specified by a range."""

    def test_a_pin_is_installed_verbatim(self):
        item = plugin_entry("a")
        specs = self.specs([item], update=True, updates={"a": "9.9.9"}, pinned={"a": "1.4.0"})
        self.assertEqual(specs, ["a@1.4.0"])
        self.assertEqual(item["install"], "a@1.4.0")

    def test_without_a_pin_the_newest_compatible_version_is_used(self):
        specs = self.specs([plugin_entry("a")], update=True, updates={"a": "1.4.0"})
        self.assertEqual(specs, ["a@1.4.0"])

    def test_without_update_the_installed_version_is_reinstalled(self):
        specs = self.specs([plugin_entry("a")], update=False, updates={"a": "1.4.0"})
        self.assertEqual(specs, ["a@1.0.0"])

    def test_a_local_source_keeps_its_own_specifier(self):
        item = plugin_entry("a", source="file", spec="file:/tmp/a-1.0.0.tgz")
        self.assertEqual(self.specs([item], update=True), ["file:/tmp/a-1.0.0.tgz"])

    def specs(self, ready, *, update, updates=None, pinned=None):
        updates = updates or {}
        result = analysis_with(*ready)
        with mock.patch.object(analysis_mod, "best_compatible_version",
                               side_effect=lambda name, *a, **k: updates.get(name)):
            return dsh_upgrade._install_specs(ready, result, update=update, offline=False,
                                              pinned=pinned)


class ApplyPinsTest(unittest.TestCase):
    """A pinned plugin is installed even when the installed copy has no verdict."""

    def test_an_unconfirmed_installed_copy_does_not_block_its_own_update(self):
        ready, blocked = [], [{"name": "a", "status": UNKNOWN, "reason": "nothing declared"}]
        ready, blocked = dsh_upgrade._apply_pins(ready, blocked, {"a": "1.4.0"})
        self.assertEqual([item["name"] for item in ready], ["a"])
        self.assertEqual(ready[0]["install"], "a@1.4.0")
        self.assertEqual(blocked, [])

    def test_an_unpinned_plugin_keeps_its_verdict(self):
        ready = [{"name": "a", "status": VERIFIED}]
        blocked = [{"name": "b", "status": UNKNOWN}]
        ready, blocked = dsh_upgrade._apply_pins(ready, blocked, {"c": "1.0.0"})
        self.assertEqual([item["name"] for item in ready], ["a"])
        self.assertEqual([item["name"] for item in blocked], ["b"])

    def test_no_pins_changes_nothing(self):
        ready = [{"name": "a", "status": VERIFIED}]
        blocked = [{"name": "b", "status": UNKNOWN}]
        self.assertEqual(dsh_upgrade._apply_pins(ready, blocked, {}), (ready, blocked))


class UpdatePlanTest(unittest.TestCase):
    """The selection itself: what is updated, what is held back, what has nothing."""

    def plan(self, names, plugins, compatible, *, allow_unconfirmed=False):
        result = analysis_with(*plugins)
        with mock.patch.object(analysis_mod, "best_compatible_version",
                               side_effect=lambda name, *a, **k: compatible.get(name)):
            return dsh_upgrade._update_plan(names, result, offline=False,
                                            allow_unconfirmed=allow_unconfirmed)

    def test_only_the_plugins_with_an_update_are_selected(self):
        updating, held, untouched = self.plan(
            ["a", "b"], [plugin_entry("a"), plugin_entry("b")], {"a": "1.4.0"})
        self.assertEqual(updating, [("a", "1.4.0", True)])
        self.assertEqual(held, [])
        self.assertEqual([row[0] for row in untouched], ["b"])

    def test_a_name_outside_the_analysis_is_reported_not_silently_dropped(self):
        updating, held, untouched = self.plan(["ghost"], [], {})
        self.assertEqual(updating, [])
        self.assertIn("not part of the compatibility analysis", untouched[0][2])

    def test_the_profile_order_is_kept(self):
        updating, _, _ = self.plan(["b", "a"],
                                   [plugin_entry("a"), plugin_entry("b")],
                                   {"a": "1.4.0", "b": "2.0.0"})
        self.assertEqual([row[0] for row in updating], ["b", "a"])

    def test_an_unconfirmed_newer_version_is_held_back_by_default(self):
        updating, held, _ = self.plan(
            ["a"], [plugin_entry("a", latest="1.8.0", latest_status=UNKNOWN)], {})
        self.assertEqual(updating, [])
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["latest"], "1.8.0")
        self.assertTrue(held[0]["installable"])

    def test_the_opt_in_turns_the_held_back_version_into_an_update(self):
        updating, held, _ = self.plan(
            ["a"], [plugin_entry("a", latest="1.8.0", latest_status=UNKNOWN)], {},
            allow_unconfirmed=True)
        self.assertEqual(updating, [("a", "1.8.0", False)])
        self.assertEqual(held, [])

    def test_a_proven_incompatible_newer_version_is_held_back_even_with_the_opt_in(self):
        updating, held, _ = self.plan(
            ["a"], [plugin_entry("a", latest="2.0.0", latest_status=INCOMPATIBLE)], {},
            allow_unconfirmed=True)
        self.assertEqual(updating, [])
        self.assertFalse(held[0]["installable"])
        self.assertIn("incompatible", held[0]["reason"])

    def test_a_local_source_is_never_a_candidate(self):
        updating, held, untouched = self.plan(
            ["a"], [plugin_entry("a", source="link")], {"a": "2.0.0"})
        self.assertEqual((updating, held), ([], []))
        self.assertEqual([row[0] for row in untouched], ["a"])


class PluginsCommandTest(unittest.TestCase):
    """The command end to end, with the profile and the registry faked out."""

    def run_plugins(self, args, plugins, *, entries=None, updates=None):
        updates = updates or {}
        profile = profile_with(*[item["name"] for item in plugins])
        entries = entries if entries is not None else [
            snapshot_entry(item["name"], version=item.get("version") or "1.0.0",
                           source=item.get("source") or "npm")
            for item in plugins]
        result = analysis_with(*plugins)
        captured = {"removed": None, "added": None}
        buffer = io.StringIO()
        fresh = profile_with("a")

        def fake_remove(profile_arg, names, **kwargs):
            captured["removed"] = list(names)

        def fake_add(profile_arg, specs, **kwargs):
            captured["added"] = list(specs)

        patches = [
            mock.patch.object(dsh_upgrade, "load_profile", return_value=profile),
            mock.patch.object(dsh_upgrade, "core_version", return_value=CORE),
            mock.patch.object(dsh_upgrade, "analyse_for", return_value=result),
            mock.patch.object(dsh_upgrade.report, "print_analysis", lambda *a, **k: None),
            mock.patch.object(analysis_mod, "snapshot_entries", return_value=entries),
            mock.patch.object(analysis_mod, "best_compatible_version",
                              side_effect=lambda name, *a, **k: updates.get(name)),
            mock.patch.object(dsh_upgrade, "read_profile", return_value=fresh),
            mock.patch.object(dsh_upgrade, "remove_plugins", side_effect=fake_remove),
            mock.patch.object(dsh_upgrade, "add_plugins", side_effect=fake_add),
            mock.patch.object(dsh_upgrade.snapshot, "write_snapshot",
                              return_value={"_jsonPath": "/state/snapshot.json"}),
            mock.patch.object(dsh_upgrade.snapshot, "write_incompatible",
                              return_value=("/state/inc.json", "/state/inc.md")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with contextlib.redirect_stdout(buffer):
            code = dsh_upgrade.cmd_plugins(args)
        return code, buffer.getvalue(), captured

    def test_only_the_plugin_with_an_update_is_installed(self):
        code, out, _ = self.run_plugins(
            plugins_args(),
            [plugin_entry("a"), plugin_entry("b")],
            updates={"a": "1.4.0"})
        self.assertEqual(code, 0)
        self.assertIn("a@1.4.0", out)
        self.assertNotIn("b@1.0.0", out)
        self.assertIn("Left alone", out)

    def test_nothing_to_update_leaves_the_profile_alone(self):
        code, out, captured = self.run_plugins(
            plugins_args(yes=True, dry_run=False), [plugin_entry("a")], updates={})
        self.assertEqual(code, 0)
        self.assertIn("Nothing to update", out)
        self.assertNotIn("=== To install", out)
        self.assertIsNone(captured["removed"])
        self.assertIsNone(captured["added"])

    def test_plugins_without_an_update_are_not_detached(self):
        code, _, captured = self.run_plugins(
            plugins_args(yes=True, dry_run=False),
            [plugin_entry("a"), plugin_entry("b")],
            updates={"a": "1.4.0"})
        self.assertEqual(code, 0)
        self.assertIsNone(captured["removed"])
        self.assertEqual(captured["added"], ["a@1.4.0"])

    def test_the_default_installs_in_place_and_detach_first_removes(self):
        code, _, captured = self.run_plugins(
            plugins_args(yes=True, dry_run=False),
            [plugin_entry("a")], updates={"a": "1.4.0"})
        self.assertEqual(code, 0)
        self.assertIsNone(captured["removed"])

        code, _, captured = self.run_plugins(
            plugins_args(yes=True, dry_run=False, detach_first=True),
            [plugin_entry("a")], updates={"a": "1.4.0"})
        self.assertEqual(code, 0)
        self.assertEqual(captured["removed"], ["a"])
        self.assertEqual(captured["added"], ["a@1.4.0"])

    def test_a_pinned_update_survives_an_unconfirmed_installed_copy(self):
        """The verdict is about the version being installed, not about the old copy."""
        code, out, captured = self.run_plugins(
            plugins_args(yes=True, dry_run=False),
            [plugin_entry("a", status=UNKNOWN, reason="declares no DSH version")],
            updates={"a": "1.4.0"})
        self.assertEqual(code, 0)
        self.assertEqual(captured["added"], ["a@1.4.0"])
        self.assertNotIn("--install-unknown", out)

    def test_an_unconfirmed_newer_version_is_not_installed_by_default(self):
        """The plugin stays as it is — it is not detached, so it cannot be lost."""
        code, out, captured = self.run_plugins(
            plugins_args(yes=True, dry_run=False),
            [plugin_entry("a", latest="1.8.0", latest_status=UNKNOWN)],
            updates={})
        self.assertEqual(code, 0)
        self.assertIn("Held back", out)
        self.assertIn("1.8.0", out)
        self.assertIn("--install-unknown", out)
        self.assertIsNone(captured["removed"])
        self.assertIsNone(captured["added"])

    def test_the_opt_in_installs_the_unconfirmed_newer_version(self):
        code, out, captured = self.run_plugins(
            plugins_args(install_unknown=True, yes=True, dry_run=False),
            [plugin_entry("a", status=UNKNOWN, latest="1.8.0", latest_status=UNKNOWN)],
            updates={})
        self.assertEqual(code, 0)
        self.assertEqual(captured["added"], ["a@1.8.0"])
        self.assertIsNone(captured["removed"])
        self.assertIn("not confirmed", out)

    def test_a_proven_incompatible_newer_version_is_never_installed(self):
        code, out, captured = self.run_plugins(
            plugins_args(install_unknown=True, yes=True, dry_run=False),
            [plugin_entry("a", latest="2.0.0", latest_status=INCOMPATIBLE)],
            updates={})
        self.assertEqual(code, 0)
        self.assertIn("incompatible", out)
        self.assertIsNone(captured["added"])

    def test_dry_run_never_touches_the_profile(self):
        _, out, captured = self.run_plugins(
            plugins_args(), [plugin_entry("a")], updates={"a": "1.4.0"})
        self.assertIn("--dry-run", out)
        self.assertIsNone(captured["removed"])
        self.assertIsNone(captured["added"])


class CheckUpdatesFlagTest(unittest.TestCase):
    """Selecting an update means comparing versions, so the registry is always read."""

    def analyse(self, args):
        profile = profile_with("a")
        # current=None keeps _mark_verified out of the real state directory.
        result = analysis_with(plugin_entry("a"), current=None)
        with mock.patch.object(analysis_mod, "analyse", return_value=result) as analyse:
            dsh_upgrade.analyse_for(args, profile, CORE)
        return analyse.call_args.kwargs["check_updates"]

    def test_the_update_flag_asks_for_the_newer_versions(self):
        self.assertTrue(self.analyse(plugins_args(update=True)))

    def test_without_the_flag_the_check_stays_on_the_declarations(self):
        self.assertFalse(self.analyse(plugins_args()))


if __name__ == "__main__":
    unittest.main()
