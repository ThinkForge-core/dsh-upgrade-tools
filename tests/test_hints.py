"""Tests for the "run this next" hints and the loader tree they point at.

A hint is only worth printing if it can be pasted: the interpreter, the absolute
path of the script, and the profile the report was made for. And the tree the hint
names has to answer the question that produced it — which row has to be re-enabled —
instead of leaving the reader to guess which of the rows matters.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import effects  # noqa: E402
from dshupgrade import invocation  # noqa: E402
from dshupgrade import style  # noqa: E402


def row(rid: str, name: str, *, disabled: bool = False, disabled_by: str | None = None,
        condition=None) -> SimpleNamespace:
    return SimpleNamespace(id=rid, name=name, disabled=disabled, disabled_by=disabled_by,
                           mounted_by="base-bundle", condition=condition)


def shadow(rid: str, *, route_count: int = 0) -> effects.Shadow:
    """A declared shadow: the manifest names the module, so this is a verdict."""
    return effects.Shadow(plugin="cleaner", token=rid, row=row(rid, f"@deepseek-ai/{rid}"),
                          evidence="client.js:5: comment", route_count=route_count,
                          kind=effects.DECLARED)


def effective(*rows: SimpleNamespace, orphans=None) -> SimpleNamespace:
    return SimpleNamespace(rows={item.id: item for item in rows},
                           order=[item.id for item in rows], layers=["base", "profile"],
                           duplicates={}, orphans=list(orphans or []))


class InvocationTest(unittest.TestCase):
    """The command in a hint must be runnable exactly as printed."""

    def test_the_interpreter_and_the_absolute_path_are_in_it(self):
        text = invocation.command("status", "--loader")
        self.assertTrue(text.startswith(sys.executable))
        self.assertIn("dsh_upgrade.py", text)
        self.assertTrue(Path(text.split()[1]).is_absolute())
        self.assertTrue(text.endswith("status --loader"))

    def test_the_default_profile_is_left_out(self):
        self.assertEqual(invocation.profile_flags("web"), [])
        self.assertEqual(invocation.profile_flags(None), [])
        self.assertEqual(invocation.profile_flags("other"), ["--profile", "other"])

    def test_another_profile_is_named(self):
        self.assertIn("--profile other", invocation.command("status", profile="other"))

    def test_the_profile_matches_the_configured_default(self):
        """Not a hardcoded "web": the default comes from the settings defaults."""
        with mock.patch.dict(invocation.config_mod.DEFAULTS, {"profile": "main"}, clear=False):
            self.assertEqual(invocation.profile_flags("main"), [])
            self.assertEqual(invocation.profile_flags("web"), ["--profile", "web"])

    def test_values_with_spaces_survive_the_copy(self):
        text = invocation.command("inspect", "a path/with spaces/plugin", profile="my profile")
        self.assertIn("'a path/with spaces/plugin'", text)
        self.assertIn("'my profile'", text)

    def test_placeholders_stay_unquoted(self):
        """A placeholder is for the reader to replace, not to quote."""
        text = invocation.command("check", "--verbose", raw=("--core", "<version>"))
        self.assertIn("--core <version>", text)
        self.assertNotIn("'<version>'", text)

    def test_the_hint_names_this_script(self):
        """The script the tests run from is the tool itself."""
        self.assertEqual(Path(invocation.script_path()).name, "dsh_upgrade.py")
        self.assertTrue(Path(invocation.script_path()).is_file())


class ShadowHintTest(unittest.TestCase):
    """The shadowed-surface section must end with a command that works."""

    def test_the_exact_command_is_printed(self):
        shadows = {"sample-augmenter": [shadow("ui-workspace")]}
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dsh_upgrade._print_shadows(shadows, effective(), hint="RUN-ME --loader")
        text = buffer.getvalue()
        self.assertIn("sample-augmenter", text)
        self.assertIn("ui-workspace", text)
        self.assertIn("RUN-ME --loader", text)

    def test_without_a_hint_the_default_names_the_script(self):
        shadows = {"cleaner": [shadow("ui-workspace", route_count=2)]}
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dsh_upgrade._print_shadows(shadows, effective())
        text = buffer.getvalue()
        self.assertIn("status --loader", text)
        self.assertIn(str(Path(invocation.script_path()).resolve()), text)
        self.assertIn("the server half still runs: 2 route(s)", text)

    def test_the_profile_of_the_report_is_in_the_hint(self):
        self.assertIn("--profile other", invocation.loader_hint("other"))
        self.assertNotIn("--profile", invocation.loader_hint("web"))


class LoaderTreeTest(unittest.TestCase):
    """The tree has to say which row matters, not just list every row."""

    def render(self, tree, shadows=None) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dsh_upgrade._print_loader(tree, shadows)
        return buffer.getvalue()

    def test_the_shadowed_row_is_marked(self):
        # Short names on purpose: this is about which row carries the mark, and a
        # wrapped cell would move the mark onto a continuation line.
        tree = effective(row("ui-workspace", "ui-workspace-pkg", disabled=True,
                             disabled_by="surgeon"),
                         row("chat", "chat-pkg"))
        text = self.render(tree, {"cleaner": [shadow("ui-workspace")]})
        self.assertIn("needed by", text)
        marked = [line for line in text.splitlines() if "ui-workspace" in line]
        self.assertTrue(marked, text)
        self.assertIn("cleaner", marked[0])
        self.assertIn("surgeon", marked[0])
        # The row nothing draws into keeps the empty mark.
        chat = [line for line in text.splitlines() if line.startswith("chat")]
        self.assertEqual(len(chat), 1, text)
        self.assertIn("—", chat[0])

    def test_without_shadows_the_column_is_absent(self):
        tree = effective(row("chat", "@deepseek-ai/dsh-client-ui-chat"))
        text = self.render(tree)
        self.assertNotIn("needed by", text)
        self.assertIn("chat", text)

    def test_an_empty_shadow_map_is_not_a_reason_to_show_the_column(self):
        tree = effective(row("chat", "@deepseek-ai/dsh-client-ui-chat"))
        text = self.render(tree, {})
        self.assertNotIn("needed by", text)

    def test_disabled_rows_still_report_who_disabled_them(self):
        tree = effective(row("ui-workspace", "@deepseek-ai/dsh-client-ui-workspace",
                             disabled=True, disabled_by="sample-replacement"))
        text = self.render(tree)
        self.assertIn("disabled", text)
        self.assertIn("sample-replacement", text)

    def test_orphans_are_still_reported(self):
        tree = effective(row("chat", "chat"), orphans=["ghost"])
        self.assertIn("ghost", self.render(tree))


class LoaderCommandTest(unittest.TestCase):
    """Both reporters accept --loader, so the hint is the same wherever it appears."""

    def test_verify_takes_the_flag(self):
        parser = dsh_upgrade.build_parser()
        args = parser.parse_args(["verify", "--loader"])
        self.assertTrue(args.loader)

    def test_status_takes_the_flag(self):
        parser = dsh_upgrade.build_parser()
        self.assertTrue(parser.parse_args(["status", "--loader"]).loader)

    def test_print_loader_tree_resolves_the_profile(self):
        """The menu shortcut runs the same code path as status --loader."""
        seen = []
        with mock.patch.object(dsh_upgrade.effects_mod, "resolve",
                               side_effect=lambda profile: seen.append(profile) or effective()), \
             mock.patch.object(dsh_upgrade.effects_mod, "scan_shadows",
                               return_value={"cleaner": [shadow("ui-workspace")]}):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                dsh_upgrade.print_loader_tree(SimpleNamespace(name="web"))
        self.assertEqual(len(seen), 1)
        self.assertIn("needed by", buffer.getvalue())

    def test_a_given_shadow_map_is_reused_not_recomputed(self):
        with mock.patch.object(dsh_upgrade.effects_mod, "resolve", return_value=effective()), \
             mock.patch.object(dsh_upgrade.effects_mod, "scan_shadows") as scan:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                dsh_upgrade.print_loader_tree(SimpleNamespace(name="web"),
                                              {"cleaner": [shadow("ui-workspace")]})
        scan.assert_not_called()


class LoaderPayloadTest(unittest.TestCase):
    """``--json --loader`` must carry the tree, not drop the flag."""

    def tree(self):
        return effective(row("ui-workspace", "ui-workspace-pkg", disabled=True,
                             disabled_by="surgeon"),
                         row("chat", "chat-pkg"),
                         row("maybe", "maybe-pkg", condition="{{ js }}"),
                         orphans=["ghost"])

    def test_states_and_marks(self):
        payload = dsh_upgrade.loader_payload(self.tree(),
                                             {"cleaner": [shadow("ui-workspace")]})
        by_id = {entry["id"]: entry for entry in payload["rows"]}
        self.assertEqual(payload["layers"], 2)
        self.assertEqual(by_id["ui-workspace"]["state"], "disabled")
        self.assertEqual(by_id["ui-workspace"]["disabledBy"], "surgeon")
        self.assertEqual(by_id["ui-workspace"]["neededBy"], ["cleaner"])
        self.assertEqual(by_id["chat"]["state"], "on")
        self.assertEqual(by_id["chat"]["neededBy"], [])
        self.assertEqual(by_id["maybe"]["state"], "conditional")
        self.assertEqual(payload["orphans"], ["ghost"])

    def test_without_shadows_no_row_is_marked(self):
        payload = dsh_upgrade.loader_payload(self.tree())
        self.assertTrue(all(not entry["neededBy"] for entry in payload["rows"]))

    def test_the_json_report_carries_it(self):
        args = SimpleNamespace(json=True, loader=True, offline=True, profile="web",
                               verbose=False, verify=False, color="never")
        profile = SimpleNamespace(name="web", directory=Path("/nowhere"), plugins=[],
                                  bundles=[])
        with mock.patch.object(dsh_upgrade, "core_install_dir", return_value=Path("/core")), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="1.0.0"), \
             mock.patch.object(dsh_upgrade, "load_profile", return_value=profile), \
             mock.patch.object(dsh_upgrade, "_status_probes", return_value={}), \
             mock.patch.object(dsh_upgrade, "profile_surfaces",
                               return_value=(self.tree(), {}, set())):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                dsh_upgrade.cmd_status(args)
        payload = json.loads(buffer.getvalue())
        self.assertIn("loader", payload)
        self.assertEqual(len(payload["loader"]["rows"]), 3)

    def test_without_the_flag_the_report_is_unchanged(self):
        args = SimpleNamespace(json=True, loader=False, offline=True, profile="web",
                               verbose=False, verify=False, color="never")
        profile = SimpleNamespace(name="web", directory=Path("/nowhere"), plugins=[],
                                  bundles=[])
        with mock.patch.object(dsh_upgrade, "core_install_dir", return_value=Path("/core")), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="1.0.0"), \
             mock.patch.object(dsh_upgrade, "load_profile", return_value=profile), \
             mock.patch.object(dsh_upgrade, "_status_probes", return_value={}), \
             mock.patch.object(dsh_upgrade, "profile_surfaces",
                               return_value=(self.tree(), {}, set())):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                dsh_upgrade.cmd_status(args)
        self.assertNotIn("loader", json.loads(buffer.getvalue()))


class StyleIsOffInTheseTestsTest(unittest.TestCase):
    """The captured hint must be the plain text the user copies."""

    def test_color_codes_are_not_in_the_checked_output(self):
        style.set_mode("never")
        self.assertNotIn("\x1b[", invocation.loader_hint())


if __name__ == "__main__":
    unittest.main()
