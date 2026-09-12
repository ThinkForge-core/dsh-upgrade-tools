"""Tests for the interactive menu: no-argument run, items, safety.

The menu must be a FULL entry point into the tool, not a "demo": every item calls
the same function as the CLI with the same arguments, and destructive operations
happen only after a preview and an explicit confirmation.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import inspect
import io
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import config as config_mod  # noqa: E402
from dshupgrade import effects  # noqa: E402
from dshupgrade import menu as menu_mod  # noqa: E402
from dshupgrade import paths  # noqa: E402
from dshupgrade import wire  # noqa: E402

#: The suite drives the settings screen, which writes a settings file. Point it at
#: a throwaway path for the whole module: a test run must never touch (or depend
#: on) the real user configuration. ``DSH_HOME`` is redirected for the same reason:
#: the menu offers the loader tree by reading the selected profile, and a test must
#: not read the profile of whoever runs the suite.
_SETTINGS_DIR = None
_SETTINGS_ENV = None


def setUpModule():
    global _SETTINGS_DIR, _SETTINGS_ENV
    _SETTINGS_DIR = TemporaryDirectory()
    _SETTINGS_ENV = mock.patch.dict(
        os.environ, {"DSH_UPGRADE_CONFIG": str(Path(_SETTINGS_DIR.name) / "config.json"),
                     "DSH_HOME": str(Path(_SETTINGS_DIR.name) / "dsh-home")})
    _SETTINGS_ENV.start()


def tearDownModule():
    _SETTINGS_ENV.stop()
    _SETTINGS_DIR.cleanup()


class Recorder:
    """Stands in for the CLI commands: records the arguments, does nothing."""

    def __init__(self, code: int = 0):
        self.calls: list = []
        self.code = code

    def __call__(self, args):
        self.calls.append(args)
        return self.code

    def names(self) -> list[str]:
        return [call.command for call in self.calls]


def fake_dispatch(recorder: Recorder | None = None) -> dict:
    """Every real CLI command, replaced by one recorder.

    Derived from the production ``default_dispatch()`` on purpose: a command added to
    the CLI then shows up in the menu tests without any edit here, and a menu item
    that calls a command the real dispatch lacks fails in the suite instead of on the
    user's terminal. (Item 14 did exactly that: ``act_verify`` called ``verify``,
    which ``default_dispatch`` had been missing.)
    """
    recorder = recorder or Recorder()
    return {name: recorder for name in menu_mod.default_dispatch()}


def make_menu(keys: str, recorder: Recorder | None = None):
    console = menu_mod.Console(stdin=io.StringIO(keys), stdout=io.StringIO())
    menu = menu_mod.Menu(settings=menu_mod.Settings(), console=console,
                         dispatch=fake_dispatch(recorder))
    # The wire scan reads the machine's real profile and core; a menu test must not
    # depend on either, and the scan has tests of its own.
    menu.wire_now = lambda: (object(), {})
    return menu, console


class MenuMapTest(unittest.TestCase):
    """A run without arguments must not crash and must show every action."""

    def test_all_cli_commands_reachable_from_menu(self):
        """Every CLI subcommand (except the menu itself) is a menu item.

        The list is read from the parser, not written out here: item 14 (verify) was
        forgotten exactly because this test spelled the commands out by hand.
        """
        keys = {action.handler for action in menu_mod.ACTIONS}
        for name in MenuDispatchContractTest.subcommands():
            if name == "menu":
                continue
            with self.subTest(command=name):
                self.assertIn(name.replace("-", "_"), keys)

    def test_read_only_group_matches_the_actions(self):
        """Every item that cannot change the profile belongs to the safe group.

        The check (4) reads the profile and writes state files, but it installs
        and detaches nothing, so it must not be listed among the dangerous ones.
        """
        for key in ("1", "2", "3", "4", "10", "11", "12", "0"):
            with self.subTest(key=key):
                self.assertIn(key, menu_mod.READ_ONLY_KEYS)
        for key in ("5", "6", "7", "8", "9"):
            with self.subTest(key=key):
                self.assertNotIn(key, menu_mod.READ_ONLY_KEYS)
        # Only items 5, 6, 8, 9 are marked as destructive at all.
        danger = {action.key for action in menu_mod.ACTIONS if action.danger}
        self.assertEqual(danger, {"5", "6", "8", "9"})

    def test_map_lists_every_action(self):
        text = menu_mod.menu_map()
        for action in menu_mod.ACTIONS:
            self.assertIn(action.key, text)
            self.assertIn(action.title, text)

    def test_no_arguments_without_tty_prints_map(self):
        """No TTY (a script, a pipe) — print the action map and exit instead of hanging."""
        buffer = io.StringIO()
        original = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            with redirect_stdout(buffer):
                code = dsh_upgrade.main([])
        finally:
            sys.stdin = original
        self.assertEqual(code, 0)
        self.assertIn("dsh-upgrade — available actions:", buffer.getvalue())
        self.assertIn("Full pipeline", buffer.getvalue())

    def test_menu_subcommand_registered(self):
        parser = dsh_upgrade.build_parser()
        args = parser.parse_args(["menu"])
        self.assertIs(args.func, dsh_upgrade.cmd_menu)

    def test_flags_before_and_after_the_subcommand(self):
        """A common flag must survive on either side of the subcommand.

        argparse shares the action objects of a ``parents`` parser with every
        subparser, so a ``set_defaults`` call would silently overwrite a flag
        given before the subcommand.
        """
        parser = dsh_upgrade.build_parser()
        both = [
            dsh_upgrade.apply_defaults(parser.parse_args(
                ["--profile", "other", "--color", "never", "status"])),
            dsh_upgrade.apply_defaults(parser.parse_args(
                ["status", "--profile", "other", "--color", "never"])),
        ]
        for args in both:
            with self.subTest(argv=vars(args)):
                self.assertEqual(args.profile, "other")
                self.assertEqual(args.color, "never")
        bare = dsh_upgrade.apply_defaults(parser.parse_args(["status"]))
        self.assertEqual(bare.profile, "web")
        self.assertIsNone(bare.color)

    def test_options_without_subcommand_do_not_fail(self):
        parser = dsh_upgrade.build_parser()
        args = parser.parse_args(["--profile", "other", "--offline"])
        self.assertIsNone(getattr(args, "func", None))
        settings = menu_mod.Settings(profile=args.profile, offline=args.offline)
        self.assertEqual(settings.profile, "other")
        self.assertTrue(settings.offline)


class MenuLoopTest(unittest.TestCase):

    def test_exit_immediately(self):
        menu, console = make_menu("0\n")
        self.assertEqual(menu.run(), 0)
        self.assertIn("exit", console.stdout.getvalue())

    def test_eof_exits_cleanly(self):
        menu, _ = make_menu("")
        self.assertEqual(menu.run(), 0)

    def test_unknown_item_is_reported(self):
        menu, console = make_menu("99\n0\n")
        menu.run()
        self.assertIn("no such item", console.stdout.getvalue())

    def test_menu_renders_all_items_and_status_line(self):
        menu, console = make_menu("0\n")
        menu.run()
        text = console.stdout.getvalue()
        for action in menu_mod.ACTIONS:
            self.assertIn(action.title, text)
        self.assertIn("dsh-upgrade — DSH core and profile plugin upgrade", text)
        self.assertIn("profile", text)
        self.assertIn("mode:", text)


class MenuDispatchTest(unittest.TestCase):
    """Menu items call exactly the CLI commands with the same arguments."""

    def test_status_item(self):
        recorder = Recorder()
        menu, _ = make_menu("1\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.calls[0].command, "status")
        # The menu is the human path: item 1 fills the "loads" column itself.
        self.assertTrue(recorder.calls[0].verify)

    def test_verify_item(self):
        """Item 14 runs the runtime verification (it once failed with a dispatch gap)."""
        recorder = Recorder()
        menu, console = make_menu("14\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), ["verify"])
        self.assertNotIn("is not available", console.stdout.getvalue())
        self.assertIn("imported cleanly", console.stdout.getvalue())

    def test_plan_limit_prompt(self):
        recorder = Recorder()
        menu, _ = make_menu("3\n2\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.calls[0].command, "plan")
        self.assertEqual(recorder.calls[0].limit, 2)
        self.assertFalse(recorder.calls[0].all)

    def test_plan_all_prompt(self):
        recorder = Recorder()
        menu, _ = make_menu("3\nall\n\n0\n", recorder)
        menu.run()
        self.assertTrue(recorder.calls[0].all)

    def test_check_prompts_pass_flags(self):
        recorder = Recorder(code=2)
        #            item  target(Enter)  --update=y  --verbose=n
        menu, console = make_menu("4\n" + "\n" + "y\n" + "n\n" + "\n0\n", recorder)
        menu.run()
        call = recorder.calls[0]
        self.assertEqual(call.command, "check")
        self.assertTrue(call.update)     # --update: «y»
        self.assertFalse(call.verbose)   # --verbose: «n»
        self.assertIn("There are incompatible plugins", console.stdout.getvalue())

    def test_settings_change_profile_for_next_action(self):
        recorder = Recorder()
        menu, _ = make_menu("11\n1\nother\n0\n\n1\n\n0\n", recorder)
        menu.run()
        self.assertEqual(menu.settings.profile, "other")
        self.assertEqual(recorder.calls[0].profile, "other")

    def test_settings_offline_reaches_commands(self):
        recorder = Recorder()
        menu, _ = make_menu("11\n3\n0\n\n1\n\n0\n", recorder)
        menu.run()
        self.assertTrue(recorder.calls[0].offline)


class LoaderOfferTest(unittest.TestCase):
    """The tree is offered where it is needed, not only named.

    The report ends with "the effective loader tree is printed by this command", and
    that is a hint the reader has to act on outside the menu: leave it, find the
    script, remember the profile. When a plugin is shadowed the menu asks instead —
    and prints the same tree here.
    """

    def _menu(self, keys: str, shadows: dict):
        menu, console = make_menu(keys)
        menu.shadows_now = lambda: (object(), shadows)
        return menu, console

    @staticmethod
    def verdict(plugin: str = "cleaner") -> effects.Shadow:
        """A declared shadow: the manifest names the module, so it is a verdict."""
        return effects.Shadow(plugin=plugin, token="ui-workspace",
                              row=SimpleNamespace(id="ui-workspace", name="ui@1",
                                                  disabled_by="loader"),
                              evidence="package.json: names it", kind=effects.DECLARED)

    def test_nothing_is_offered_when_nothing_is_shadowed(self):
        menu, console = make_menu("1\n\n0\n")
        menu.shadows_now = lambda: (object(), {})
        menu.run()
        self.assertNotIn("loader tree now", console.stdout.getvalue())

    def test_accepting_prints_the_tree(self):
        menu, console = self._menu("1\ny\n\n0\n", {"cleaner": [self.verdict()]})
        printed = []
        with mock.patch("dsh_upgrade.print_loader_tree",
                        side_effect=lambda profile, shadows: printed.append(shadows)):
            menu.run()
        self.assertEqual(len(printed), 1)
        self.assertIn("print the effective loader tree now", console.stdout.getvalue())

    def test_declining_names_the_exact_command(self):
        menu, console = self._menu("1\nn\n\n0\n", {"cleaner": [self.verdict()]})
        menu.run()
        self.assertIn("status --loader", console.stdout.getvalue())

    def test_an_explained_reference_does_not_offer_the_tree(self):
        """A lead the replacement already accounts for is not a reason to ask."""
        lead = effects.Shadow(plugin="cleaner", token="ui-workspace",
                              row=SimpleNamespace(id="ui-workspace", name="ui@1",
                                                  disabled_by="replacement"),
                              evidence="client.js:5: a comment",
                              kind=effects.REFERENCE, replaced_by="sample-replacement")
        menu, console = self._menu("1\n\n0\n", {"cleaner": [lead]})
        menu.run()
        self.assertNotIn("loader tree now", console.stdout.getvalue())

    def test_a_dead_wire_call_is_named_before_the_reader_digs(self):
        menu, _ = make_menu("1\n\n0\n")
        menu.wire_now = lambda: (object(), {"cleaner": [wire.WireCall(
            "cleaner", "/api/session.list", "session.list", "session.list",
            "client.js:72", wire.DEAD, 'the legacy separator: this core serves '
            '"session/list"')]})
        menu.run()
        self.assertIn("/api/session.list is not an endpoint of this core",
                      menu.console.stdout.getvalue())

    def test_the_offer_survives_an_unreadable_profile(self):
        """Reading the profile must never break a report that already ran."""
        menu, console = make_menu("1\n\n0\n")
        menu.shadows_now = lambda: None
        menu.run()
        self.assertNotIn("loader tree now", console.stdout.getvalue())


class MenuDispatchContractTest(unittest.TestCase):
    """The production dispatch and the CLI must agree — see how item 14 broke.

    ``act_verify`` called ``self.call("verify")`` while ``default_dispatch()`` had no
    ``verify`` entry, so the item answered "command verify is not available" on a real
    terminal. The suite missed it because the menu tests injected their own hand-written
    dispatch dictionary, which happened to be missing the same key. These checks are
    derived from the code itself, so they cannot drift the same way.
    """

    #: ``self.call("name")`` as it appears in the menu handlers.
    CALL_RE = re.compile(r'self\.call\(\s*"([a-z_]+)"')

    @staticmethod
    def subcommands() -> dict:
        """The real subparsers: ``{cli name: subparser}``.

        ``argparse`` keeps this on a private action; there is no public accessor, and
        reading it is the only way to assert the CLI and the menu dispatch agree.
        """
        parser = dsh_upgrade.build_parser()
        action = next(action for action in parser._actions
                      if isinstance(action, argparse._SubParsersAction))
        return dict(action.choices)

    def test_every_called_command_exists_in_the_dispatch(self):
        dispatch = menu_mod.default_dispatch()
        called = set(self.CALL_RE.findall(inspect.getsource(menu_mod)))
        self.assertTrue(called, "no self.call(...) found — the pattern or the menu changed")
        for command in sorted(called):
            with self.subTest(command=command):
                self.assertIn(command, dispatch)

    def test_every_cli_command_is_wired_to_the_same_function(self):
        """A subcommand missing from the menu dispatch — or bound to another handler."""
        dispatch = menu_mod.default_dispatch()
        for name, subparser in self.subcommands().items():
            with self.subTest(command=name):
                key = name.replace("-", "_")
                if name == "menu":
                    # Opening the menu is not itself a menu item.
                    self.assertNotIn(key, dispatch)
                    continue
                self.assertIn(key, dispatch)
                self.assertIs(dispatch[key], subparser.get_default("func"))

    def test_every_item_resolves_to_a_handler(self):
        """Every item resolves to a handler method (no typo can hide until runtime)."""
        for action in menu_mod.ACTIONS:
            with self.subTest(key=action.key, handler=action.handler):
                self.assertTrue(callable(getattr(menu_mod.Menu, f"act_{action.handler}", None)))


class MenuSafetyTest(unittest.TestCase):
    """Destructive work — only through a preview and the word yes."""

    def test_detach_previews_then_cancels(self):
        recorder = Recorder()
        menu, console = make_menu("5\nn\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), ["detach"])          # preview only
        self.assertTrue(recorder.calls[0].dry_run)
        self.assertFalse(recorder.calls[0].yes)
        self.assertIn("cancelled", console.stdout.getvalue())

    def test_detach_runs_after_confirmation(self):
        recorder = Recorder()
        menu, _ = make_menu("5\nyes\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), ["detach", "detach"])
        self.assertTrue(recorder.calls[0].dry_run)              # the preview comes first
        self.assertTrue(recorder.calls[1].yes)                  # and only then the real run
        self.assertFalse(recorder.calls[1].dry_run)

    def test_attach_previews_with_flags_then_cancels(self):
        recorder = Recorder()
        # item, target(Enter), snapshot(Enter=freshest), --update=y, unknown=y, prune=n, cancel
        menu, _ = make_menu("6\n\n\ny\ny\nn\nn\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), ["attach"])
        call = recorder.calls[0]
        self.assertTrue(call.dry_run)
        self.assertTrue(call.update)           # «y»
        self.assertTrue(call.install_unknown)  # «y»
        self.assertFalse(call.prune_failed)    # «n»

    def test_pipeline_previews_then_cancels(self):
        recorder = Recorder()
        # item, target(Enter), --update=y, --install-unknown=n, run-core=n, skip=n, cancel
        menu, _ = make_menu("9\n\ny\nn\nn\nn\nn\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), ["pipeline"])
        self.assertTrue(recorder.calls[0].dry_run)
        self.assertFalse(recorder.calls[0].run_core_upgrade)

    def test_pipeline_install_unknown_is_opt_in(self):
        """Unconfirmed plugins are installed only when the user asks for it."""
        recorder = Recorder()
        menu, _ = make_menu("9\n\ny\ny\nn\nn\nn\n\n0\n", recorder)
        menu.run()
        self.assertTrue(recorder.calls[0].dry_run)
        self.assertTrue(recorder.calls[0].update)
        self.assertTrue(recorder.calls[0].install_unknown)

    def test_pipeline_defaults_leave_install_unknown_off(self):
        recorder = Recorder()
        # Every question answered with Enter: the defaults must keep it off.
        menu, _ = make_menu("9\n" + "\n" * 6 + "\n0\n", recorder)
        menu.run()
        self.assertFalse(recorder.calls[0].install_unknown)

    def test_plugins_previews_then_cancels(self):
        recorder = Recorder()
        menu, _ = make_menu("8\n\nn\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), ["plugins"])
        self.assertTrue(recorder.calls[0].dry_run)

    def test_recheck_without_install_runs_once(self):
        recorder = Recorder()
        menu, _ = make_menu("7\n\nn\nn\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), ["recheck"])
        self.assertFalse(recorder.calls[0].install)
        self.assertFalse(recorder.calls[0].dry_run)
        # Without --install nothing is installed, so the unknown question is skipped.
        self.assertFalse(recorder.calls[0].install_unknown)

    def test_recheck_install_unknown_is_asked_only_with_install(self):
        recorder = Recorder()
        # item, target, list(Enter), --install=y, --install-unknown=y, --update=n, cancel
        menu, _ = make_menu("7\n\n\ny\ny\nn\nn\n\n0\n", recorder)
        menu.run()
        self.assertTrue(recorder.calls[0].dry_run)
        self.assertTrue(recorder.calls[0].install)
        self.assertTrue(recorder.calls[0].install_unknown)


class MenuPathInputTest(unittest.TestCase):
    """Item 13 (and every other path prompt) reads paths like a terminal does."""

    def console(self, keys: str):
        return menu_mod.Console(stdin=io.StringIO(keys), stdout=io.StringIO())

    def test_a_pipe_is_not_interactive(self):
        self.assertFalse(self.console("/tmp/x.tgz\n").interactive())

    def test_quotes_and_escapes_are_undone(self):
        self.assertEqual(self.console("'/tmp/a b.tgz'\n").ask_path("  p: "), "/tmp/a b.tgz")
        self.assertEqual(self.console("/tmp/a\\ b.tgz\n").ask_path("  p: "), "/tmp/a b.tgz")

    def test_a_typed_space_is_kept(self):
        self.assertEqual(self.console("/tmp/a b.tgz\n").ask_path("  p: "), "/tmp/a b.tgz")

    def test_the_home_directory_is_expanded(self):
        self.assertEqual(self.console("~/x.tgz\n").ask_path("  p: "),
                         str(Path.home() / "x.tgz"))

    def test_an_empty_answer_gives_the_default(self):
        self.assertEqual(self.console("\n").ask_path("  p: ", default="/tmp/fresh"), "/tmp/fresh")

    def test_item_13_asks_for_a_path_and_calls_inspect(self):
        recorder = Recorder()
        menu, _ = make_menu("13\n/tmp/my plugin.tgz\n\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), ["inspect"])
        self.assertEqual(recorder.calls[0].artifact, "/tmp/my plugin.tgz")
        self.assertIsNone(recorder.calls[0].since)

    def test_a_cancelled_path_does_not_call_the_command(self):
        recorder = Recorder()
        menu, console = make_menu("13\n\n\n0\n", recorder)
        menu.run()
        self.assertEqual(recorder.names(), [])
        self.assertIn("cancelled", console.stdout.getvalue())


class MenuSettingsPersistenceTest(unittest.TestCase):
    """The settings screen writes the settings file — that is the whole point."""

    def setUp(self):
        self.path = Path(os.environ["DSH_UPGRADE_CONFIG"])
        if self.path.exists():
            self.path.unlink()
        self.addCleanup(lambda: self.path.exists() and self.path.unlink())

    def drive(self, keys: str, settings: menu_mod.Settings | None = None, recorder=None):
        console = menu_mod.Console(stdin=io.StringIO(keys), stdout=io.StringIO())
        menu = menu_mod.Menu(settings=settings or menu_mod.Settings(), console=console,
                             dispatch=fake_dispatch(recorder))
        menu.run()
        return console

    def test_a_changed_option_is_remembered(self):
        self.drive("11\n1\nother\n0\n\n0\n")
        self.assertEqual(config_mod.load()["profile"], "other")

    def test_the_checkout_location_is_remembered(self):
        self.drive("11\n8\n/tmp/kept-checkouts\n0\n\n0\n")
        self.assertEqual(config_mod.load()["checkouts"], "/tmp/kept-checkouts")

    def test_a_pinned_flag_is_not_written(self):
        settings = menu_mod.Settings(profile="cli", pinned=frozenset({"profile"}))
        self.drive("11\n1\nother\n0\n\n0\n", settings=settings)
        self.assertNotIn("profile", config_mod.load())

    def test_switching_back_to_auto_forgets_the_target(self):
        self.drive("11\n2\n0.1.5-rc.2\n11\n2\n-\n0\n\n0\n")
        self.assertNotIn("core", config_mod.load())

    def test_forget_removes_the_saved_settings(self):
        self.drive("11\n1\nother\n0\n\n0\n")
        self.assertTrue(self.path.exists())
        self.drive("11\n10\n0\n\n0\n")
        self.assertFalse(self.path.exists())

    def test_pruning_the_temporary_checkouts_removes_them(self):
        """Item 9 deletes the temporary checkouts — never the real ones in a test."""
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "tmp-checkouts"
        root.mkdir()
        with mock.patch.object(paths, "temp_checkouts_root", return_value=root):
            self.drive("11\n9\n0\n\n0\n")
        self.assertFalse(root.exists())

    def test_a_dry_run_never_writes_settings(self):
        """A read-only action must leave the settings file alone."""
        recorder = Recorder()
        self.drive("4\n\nn\nn\n\n0\n", recorder=recorder)
        self.assertFalse(self.path.exists())


class MenuArgsTest(unittest.TestCase):
    """The Namespace for the commands carries every field they read."""

    def test_args_cover_every_flag(self):
        menu, _ = make_menu("0\n")
        args = menu.args("attach", update=True)
        for field in ("profile", "core", "offline", "no_clone", "json", "color", "verbose", "state_dir",
                      "checkouts",
                      "update", "yes", "dry_run", "from_file", "only", "install_unknown",
                      "prune_failed", "file", "install", "limit", "all", "run_core_upgrade",
                      "skip_attach"):
            with self.subTest(field=field):
                self.assertTrue(hasattr(args, field))
        self.assertEqual(args.command, "attach")
        self.assertTrue(args.update)

    def test_incompatible_viewer_without_state(self):
        """An empty state directory — a clear hint instead of an error."""
        with TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DSH_UPGRADE_STATE": tmp}):
                menu, console = make_menu("10\n\n0\n")
                menu.run()
        self.assertIn("No list yet", console.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
