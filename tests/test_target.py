"""Tests for the automatic upgrade target: it must be the NEWEST published version.

``--core`` always wins; without it the tool resolves the newest published release
(prereleases included) rather than the installed or "closest" one. The installed
core appears only as a fallback when the registry cannot be reached at all.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import menu as menu_mod  # noqa: E402
from dshupgrade import registry, style  # noqa: E402


def documents(*versions: str) -> dict:
    return {"tags": {"latest": versions[0] if versions else None},
            "versions": [{"version": version, "time": ""} for version in versions],
            "modified": None}


def args_for(core=None, offline=False) -> SimpleNamespace:
    """The minimum of the CLI Namespace that resolve_target() reads."""
    return SimpleNamespace(core=core, offline=offline, no_clone=True)


class NewestCoreVersionTest(unittest.TestCase):

    def test_picks_the_highest_semver_not_the_latest_tag(self):
        """The `latest` tag can point at an older release than the newest one."""
        with mock.patch.object(registry, "core_versions",
                               return_value=documents("0.1.5-rc.1", "0.1.5-rc.2", "0.1.1-rc.2")):
            self.assertEqual(registry.newest_core_version(), "0.1.5-rc.2")

    def test_prerelease_ordering(self):
        with mock.patch.object(registry, "core_versions",
                               return_value=documents("0.1.5-alpha.2", "0.1.5-rc.1", "0.1.3-alpha.2")):
            self.assertEqual(registry.newest_core_version(), "0.1.5-rc.1")

    def test_no_versions_at_all(self):
        with mock.patch.object(registry, "core_versions", return_value=documents()):
            self.assertIsNone(registry.newest_core_version())

    def test_unreachable_registry(self):
        with mock.patch.object(registry, "core_versions", side_effect=RuntimeError("offline")):
            self.assertIsNone(registry.newest_core_version())


class ResolveTargetTest(unittest.TestCase):

    def setUp(self):
        style.set_mode("never")

    def test_explicit_core_wins(self):
        with mock.patch.object(dsh_upgrade.registry, "newest_core_version",
                               return_value="9.9.9") as resolver:
            self.assertEqual(dsh_upgrade.resolve_target(args_for(core="0.1.5-rc.1")), "0.1.5-rc.1")
        resolver.assert_not_called()

    def test_auto_target_is_the_newest_published_version(self):
        with mock.patch.object(dsh_upgrade.registry, "newest_core_version",
                               return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.1-rc.2"):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                target = dsh_upgrade.resolve_target(args_for())
        self.assertEqual(target, "0.1.5-rc.2")
        self.assertIn("auto target: 0.1.5-rc.2", buffer.getvalue())
        self.assertIn("newest published version", buffer.getvalue())

    def test_installed_core_is_only_a_fallback(self):
        with mock.patch.object(dsh_upgrade.registry, "newest_core_version", return_value=None), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.1-rc.2"):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                target = dsh_upgrade.resolve_target(args_for())
        self.assertEqual(target, "0.1.1-rc.2")
        self.assertIn("registry unavailable", buffer.getvalue())

    def test_nothing_known_is_an_error(self):
        with mock.patch.object(dsh_upgrade.registry, "newest_core_version", return_value=None), \
             mock.patch.object(dsh_upgrade, "core_version", return_value=None):
            with self.assertRaises(SystemExit):
                dsh_upgrade.resolve_target(args_for())


class TargetProblemTest(unittest.TestCase):
    """A target has to be a version the tool can actually measure against.

    Reporting every plugin as "unconfirmed" because the target does not exist reads
    like a result and is not one, so such a target is refused instead.
    """

    def problem(self, target, *, installed=None, checkout=None, published=(),
                unreachable=False):
        versions = mock.Mock(side_effect=RuntimeError("offline")) if unreachable else \
            mock.Mock(return_value=documents(*published))
        with mock.patch.object(dsh_upgrade, "core_version", return_value=installed), \
             mock.patch.object(dsh_upgrade.paths, "find_checkout", return_value=checkout), \
             mock.patch.object(dsh_upgrade.registry, "core_versions", versions), \
             mock.patch.object(dsh_upgrade.registry, "newest_core_version",
                               return_value=(published[-1] if published else None)):
            return dsh_upgrade.target_problem(target)

    def test_a_version_shaped_string_is_required(self):
        self.assertIn("not a version number", self.problem("y", published=("0.1.5-rc.2",)))

    def test_the_installed_core_is_always_usable(self):
        self.assertIsNone(self.problem("0.1.5-rc.2", installed="0.1.5-rc.2"))

    def test_a_checkout_on_disk_is_usable(self):
        self.assertIsNone(self.problem("0.1.5-rc.2", checkout=Path("/c")))

    def test_a_published_version_is_usable(self):
        self.assertIsNone(self.problem("0.1.4", published=("0.1.4", "0.1.5-rc.2")))

    def test_a_version_nobody_published_is_refused(self):
        message = self.problem("9.9.9", published=("0.1.4", "0.1.5-rc.2"))
        self.assertIn("no such core version", message)
        self.assertIn("0.1.5-rc.2", message)

    def test_an_unreachable_registry_without_a_local_copy_is_refused(self):
        message = self.problem("0.1.4", unreachable=True)
        self.assertIn("cannot confirm", message)

    def test_resolve_target_refuses_a_bogus_core(self):
        with mock.patch.object(dsh_upgrade.paths, "find_checkout", return_value=None), \
             mock.patch.object(dsh_upgrade.registry, "core_versions",
                               return_value=documents("0.1.5-rc.2")):
            with self.assertRaises(SystemExit) as caught:
                dsh_upgrade.resolve_target(args_for(core="y"))
        self.assertIn("not a version number", str(caught.exception))


class SettingsTargetLabelTest(unittest.TestCase):
    """The menu header shows `auto — <version>` instead of the installed one."""

    def test_label_shows_the_newest_version(self):
        settings = menu_mod.Settings()
        with mock.patch.object(menu_mod.registry, "newest_core_version",
                               return_value="0.1.5-rc.2") as resolver:
            self.assertEqual(settings.target_label(), "auto — 0.1.5-rc.2")
            self.assertEqual(settings.target_label(), "auto — 0.1.5-rc.2")
        self.assertEqual(resolver.call_count, 1, "the header must not re-resolve on every loop")
        self.assertTrue(resolver.call_args.kwargs.get("offline"), "the header must not hit the network")

    def test_explicit_target_is_shown_as_is(self):
        self.assertEqual(menu_mod.Settings(core="0.1.5-rc.1").target_label(), "0.1.5-rc.1")

    def test_fallback_is_marked(self):
        settings = menu_mod.Settings()
        with mock.patch.object(menu_mod.registry, "newest_core_version", return_value=None), \
             mock.patch.object(menu_mod, "core_version", return_value="0.1.1-rc.2"):
            self.assertEqual(settings.target_label(refresh=True),
                             "auto — 0.1.1-rc.2 (installed fallback)")

    def test_refresh_goes_to_the_network(self):
        settings = menu_mod.Settings()
        with mock.patch.object(menu_mod.registry, "newest_core_version",
                               return_value="0.1.5-rc.2") as resolver:
            settings.target_label(refresh=True)
        self.assertFalse(resolver.call_args.kwargs.get("offline"))

    def test_offline_mode_never_goes_online(self):
        settings = menu_mod.Settings(offline=True)
        with mock.patch.object(menu_mod.registry, "newest_core_version",
                               return_value="0.1.5-rc.2") as resolver:
            settings.target_label(refresh=True)
        self.assertTrue(resolver.call_args.kwargs.get("offline"))


if __name__ == "__main__":
    unittest.main()
