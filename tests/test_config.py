"""Tests for the settings file: options survive a restart, and safely.

Two properties matter. The file is user-editable, so a broken value must degrade
to the built-in default instead of crashing a run. And a flag given on the command
line must never become a permanent setting by accident: it wins for that run and
is not written back.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import config as config_mod  # noqa: E402
from dshupgrade import menu as menu_mod  # noqa: E402
from dshupgrade import paths  # noqa: E402


class SettingsFileTest(unittest.TestCase):
    """Reading and writing the settings file itself."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        patcher = mock.patch.dict(os.environ, {"DSH_UPGRADE_CONFIG": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_location_comes_from_the_environment(self):
        self.assertEqual(config_mod.config_path(), self.path)

    def test_location_follows_xdg_when_not_overridden(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}):
            os.environ.pop("DSH_UPGRADE_CONFIG", None)
            self.assertEqual(config_mod.config_path(),
                             Path(self.tmp.name) / "dsh-upgrade" / "config.json")

    def test_a_missing_file_is_no_settings(self):
        self.assertEqual(config_mod.load(), {})

    def test_save_then_load_round_trip(self):
        config_mod.save({"profile": "other", "no_clone": True, "core": "0.1.5-rc.2"})
        self.assertEqual(config_mod.load(),
                         {"profile": "other", "no_clone": True, "core": "0.1.5-rc.2"})

    def test_save_merges_instead_of_replacing(self):
        config_mod.save({"profile": "other", "verbose": True})
        config_mod.save({"color": "never"})
        self.assertEqual(config_mod.load(),
                         {"profile": "other", "verbose": True, "color": "never"})

    def test_none_removes_a_key(self):
        config_mod.save({"profile": "other", "core": "0.1.5-rc.2"})
        config_mod.save({"core": None})
        self.assertEqual(config_mod.load(), {"profile": "other"})
        self.assertNotIn("core", json.loads(self.path.read_text(encoding="utf-8")))

    def test_the_file_is_readable_json_with_only_known_keys(self):
        config_mod.save({"profile": "other", "nonsense": 1})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"profile": "other"})

    def test_broken_values_are_ignored_not_fatal(self):
        self.path.write_text(json.dumps({
            "profile": "   ",           # empty
            "offline": "yes",           # not a bool
            "color": "rainbow",         # not a mode
            "checkouts": 42,            # not a path
            "verbose": True,            # fine
        }), encoding="utf-8")
        self.assertEqual(config_mod.load(), {"checkouts": "42", "verbose": True})

    def test_unreadable_json_is_no_settings(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(config_mod.load(), {})

    def test_forget_removes_the_file_once(self):
        config_mod.save({"profile": "other"})
        self.assertEqual(config_mod.forget(), self.path)
        self.assertFalse(self.path.exists())
        self.assertIsNone(config_mod.forget())


class SettingsObjectTest(unittest.TestCase):
    """The menu's Settings object and its persistence rules."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        patcher = mock.patch.dict(os.environ, {"DSH_UPGRADE_CONFIG": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_as_config_uses_the_flag_names(self):
        settings = menu_mod.Settings(json_output=True)
        self.assertTrue(settings.as_config()["json"])
        self.assertNotIn("json_output", settings.as_config())

    def test_save_and_reload(self):
        """The file is a full snapshot of the settings, not a diff."""
        saved = menu_mod.Settings(profile="other", checkouts="/tmp/kept", verbose=False)
        self.assertEqual(saved.save(), self.path)
        self.assertEqual(config_mod.load(),
                         {"profile": "other", "checkouts": "/tmp/kept", "verbose": False,
                          "color": "auto", "offline": False, "no_clone": False, "json": False})

    def test_a_pinned_option_is_never_written(self):
        settings = menu_mod.Settings(profile="cli", pinned=frozenset({"profile"}))
        settings.save()
        self.assertNotIn("profile", config_mod.load())
        self.assertIn("color", config_mod.load())

    def test_apply_config_respects_pins(self):
        settings = menu_mod.Settings(profile="cli", pinned=frozenset({"profile"}))
        settings.apply_config({"profile": "from-file", "color": "never"})
        self.assertEqual(settings.profile, "cli")
        self.assertEqual(settings.color, "never")

    def test_apply_config_translates_json(self):
        settings = menu_mod.Settings()
        settings.apply_config({"json": True})
        self.assertTrue(settings.json_output)

    def test_a_reset_leaves_no_literal_none(self):
        """Item 9 returns the defaults: the screen must show values, not None."""
        settings = menu_mod.Settings(profile="other", color="never")
        settings.apply_config(config_mod.DEFAULTS)
        self.assertEqual(settings.color, "auto")
        self.assertEqual(settings.profile, "web")
        self.assertEqual(settings.checkouts, "temp")

    def test_checkouts_label_explains_itself(self):
        self.assertIn("temporary", menu_mod.Settings(checkouts="temp").checkouts_label())
        self.assertIn("checkouts", menu_mod.Settings(checkouts="keep").checkouts_label())
        self.assertIn("/data/checkouts",
                      menu_mod.Settings(checkouts="/data/checkouts").checkouts_label())


class PrecedenceTest(unittest.TestCase):
    """Flag > saved settings > built-in default."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        patcher = mock.patch.dict(os.environ, {"DSH_UPGRADE_CONFIG": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.parser = dsh_upgrade.build_parser()

    def defaults(self, argv, saved):
        return dsh_upgrade.apply_defaults(self.parser.parse_args(argv), saved)

    def test_saved_settings_fill_the_gaps(self):
        args = self.defaults(["status"], {"profile": "other", "checkouts": "/tmp/kept"})
        self.assertEqual(args.profile, "other")
        self.assertEqual(args.checkouts, "/tmp/kept")
        self.assertFalse(args.offline)

    def test_an_explicit_flag_beats_the_saved_value(self):
        args = self.defaults(["--profile", "cli", "status"], {"profile": "saved"})
        self.assertEqual(args.profile, "cli")

    def test_a_flag_before_the_subcommand_is_not_lost(self):
        args = self.defaults(["--checkouts", "temp", "status"], {"checkouts": "/tmp/kept"})
        self.assertEqual(args.checkouts, "temp")

    def test_without_saved_settings_the_builtins_apply(self):
        args = self.defaults(["status"], {})
        self.assertEqual(args.profile, "web")
        self.assertEqual(args.checkouts, "temp")

    def test_main_marks_what_the_user_typed(self):
        """Everything on the command line is pinned; everything else is free."""
        parser = self.parser
        args = parser.parse_args(["--profile", "cli", "status"])
        explicit = frozenset(vars(args))
        self.assertIn("profile", explicit)
        self.assertNotIn("checkouts", explicit)
        self.assertNotIn("color", explicit)

    def test_the_menu_receives_pinned_flags(self):
        args = SimpleNamespace(profile="cli", explicit=frozenset({"profile"}))
        with mock.patch.object(menu_mod, "run", return_value=0) as run:
            dsh_upgrade.cmd_menu(args)
        settings = run.call_args.kwargs["settings"]
        self.assertEqual(settings.profile, "cli")
        self.assertIn("profile", settings.pinned)


class CheckoutPrecedenceTest(unittest.TestCase):
    """Where the checkouts go: flag > environment > settings file > temporary.

    The environment must beat the settings file — and the settings file must not be
    pinned blindly, otherwise the built-in ``temp`` (written into ``args.checkouts``
    by :func:`apply_defaults`) would always shadow ``DSH_CHECKOUTS_ROOT``.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(paths.set_checkouts_setting, None)
        self.addCleanup(lambda: os.environ.pop("DSH_CHECKOUTS_ROOT", None))
        os.environ.pop("DSH_CHECKOUTS_ROOT", None)

    def parsed(self, argv):
        parser = dsh_upgrade.build_parser()
        args = parser.parse_args(argv)
        args.explicit = frozenset(vars(args))
        return dsh_upgrade.apply_defaults(args, {})

    def test_an_explicit_flag_wins(self):
        with mock.patch.dict(os.environ, {"DSH_CHECKOUTS_ROOT": "/tmp/from-env"}):
            args = self.parsed(["--checkouts", "/tmp/from-flag", "status"])
            dsh_upgrade._apply_checkouts(args, {"checkouts": "/tmp/from-file"})
        self.assertEqual(paths.checkouts_setting(), str(Path("/tmp/from-flag").resolve()))

    def test_the_environment_beats_the_settings_file(self):
        with mock.patch.dict(os.environ, {"DSH_CHECKOUTS_ROOT": "/tmp/from-env"}):
            args = self.parsed(["status"])
            dsh_upgrade._apply_checkouts(args, {"checkouts": "/tmp/from-file"})
            self.assertEqual(paths.checkouts_setting(), str(Path("/tmp/from-env").resolve()))

    def test_the_settings_file_is_used_when_nothing_overrides_it(self):
        args = self.parsed(["status"])
        dsh_upgrade._apply_checkouts(args, {"checkouts": "/tmp/from-file"})
        self.assertEqual(paths.checkouts_setting(), str(Path("/tmp/from-file").resolve()))

    def test_nothing_set_falls_back_to_the_temporary_default(self):
        args = self.parsed(["status"])
        dsh_upgrade._apply_checkouts(args, {})
        self.assertTrue(paths.temporary_checkouts())
        self.assertEqual(paths.checkouts_setting(), paths.TEMP)


if __name__ == "__main__":
    unittest.main()
