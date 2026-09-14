"""Tests for the Termux correction: the layer, its version, and the upgrade seam.

The property under test is the one a plain ``npm i -g`` gets wrong on Android: a
core upgrade restores pristine upstream files, and the Android corrections have to
be put back **before** the plugins are installed — otherwise the post-check judges
plugins against a core that cannot write a file on this platform. These tests pin
the discovery rules, the version the layer validates, the corrected command order,
and the tri-state switch that can turn the whole thing off.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import os
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
from dshupgrade import termux as termux_mod  # noqa: E402


def make_layer(root: Path, *, validated: str | None = "0.1.5-rc.2") -> Path:
    """A minimal but *valid* layer: the two files that make a directory one."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / termux_mod.PATCHER_RELATIVE).write_text("// patcher\n", encoding="utf-8")
    (root / termux_mod.REALIGN_RELATIVE).write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    install = "#!/usr/bin/env bash\nset -euo pipefail\n"
    if validated:
        install += f'readonly VALIDATED_DSH_VERSION="{validated}"\n'
    (root / termux_mod.INSTALL_RELATIVE).write_text(install, encoding="utf-8")
    return root


class DetectionTest(unittest.TestCase):
    """Is this Termux, and was the correction asked for?"""

    def test_sys_platform_android(self):
        with mock.patch.object(sys, "platform", "android"):
            self.assertTrue(termux_mod.is_termux())

    def test_termux_version_marker(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"TERMUX_VERSION": "0.118.3"}, clear=False):
            self.assertTrue(termux_mod.is_termux())

    def test_prefix_marker(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"},
                             clear=False):
            self.assertTrue(termux_mod.is_termux())

    def test_plain_linux(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(termux_mod.is_termux())

    def test_active_is_tristate(self):
        with mock.patch.object(termux_mod, "is_termux", return_value=True):
            self.assertTrue(termux_mod.active(SimpleNamespace(termux=None)))
            self.assertFalse(termux_mod.active(SimpleNamespace(termux=False)))
        with mock.patch.object(termux_mod, "is_termux", return_value=False):
            self.assertFalse(termux_mod.active(SimpleNamespace(termux=None)))
            self.assertTrue(termux_mod.active(SimpleNamespace(termux=True)))

    def test_active_without_args_falls_back_to_detection(self):
        with mock.patch.object(termux_mod, "is_termux", return_value=True):
            self.assertTrue(termux_mod.active(None))


class DiscoveryTest(unittest.TestCase):
    """Which directory counts as the layer, and which one is chosen."""

    def test_looks_like_layer(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(termux_mod.looks_like_layer(root))
            make_layer(root)
            self.assertTrue(termux_mod.looks_like_layer(root))
            self.assertFalse(termux_mod.looks_like_layer(root / "nope"))

    def test_a_named_directory_is_authoritative(self):
        """A bad --termux-dir is not silently replaced by another checkout."""
        with TemporaryDirectory() as temporary:
            root = make_layer(Path(temporary) / "real")
            with mock.patch.object(termux_mod, "_candidates", return_value=[root]):
                self.assertIsNone(termux_mod.layer(str(Path(temporary) / "not-a-layer")))
                self.assertEqual(termux_mod.layer(str(root)), root.resolve())

    def test_environment_is_honoured(self):
        with TemporaryDirectory() as temporary:
            root = make_layer(Path(temporary) / "layer")
            with mock.patch.dict(os.environ, {"DSH_TERMUX_DIR": str(root)}, clear=False):
                self.assertEqual(termux_mod.layer(), root.resolve())

    def test_no_layer_anywhere(self):
        with TemporaryDirectory() as temporary, mock.patch.object(
                termux_mod, "_candidates", return_value=[Path(temporary) / "missing"]):
            self.assertIsNone(termux_mod.layer())

    def test_ensure_layer_does_not_clone_over_a_named_directory(self):
        with TemporaryDirectory() as temporary:
            with mock.patch.object(termux_mod.subprocess, "run") as run:
                self.assertIsNone(termux_mod.ensure_layer(str(Path(temporary) / "absent")))
            run.assert_not_called()

    def test_ensure_layer_clones_the_fork_when_absent(self):
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "termux-layer"

            def fake_clone(command, **options):  # what a successful clone leaves behind
                make_layer(target)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(termux_mod, "clone_root", return_value=target), \
                 mock.patch.object(termux_mod, "_candidates", return_value=[target]), \
                 mock.patch.object(termux_mod.shutil, "which", return_value="/usr/bin/git"), \
                 mock.patch.object(termux_mod.subprocess, "run", side_effect=fake_clone) as run:
                self.assertEqual(termux_mod.ensure_layer(), target.resolve())
            command = run.call_args[0][0]
            self.assertEqual(command[:2], ["git", "clone"])
            self.assertIn(termux_mod.LAYER_REPO, command)

    def test_ensure_layer_does_not_touch_a_stale_clone_root(self):
        """A directory at the clone root that is not a layer is left alone."""
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "termux-layer"
            target.mkdir()
            (target / "unrelated.txt").write_text("mine\n", encoding="utf-8")
            lines: list[str] = []
            with mock.patch.object(termux_mod, "clone_root", return_value=target), \
                 mock.patch.object(termux_mod, "_candidates", return_value=[target]), \
                 mock.patch.object(termux_mod.subprocess, "run") as run:
                self.assertIsNone(termux_mod.ensure_layer(log=lines.append))
            run.assert_not_called()
            self.assertTrue(any("not touching it" in line for line in lines))

    def test_ensure_layer_reports_a_failed_clone(self):
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "termux-layer"
            lines: list[str] = []
            with mock.patch.object(termux_mod, "clone_root", return_value=target), \
                 mock.patch.object(termux_mod, "_candidates", return_value=[target]), \
                 mock.patch.object(termux_mod.shutil, "which", return_value="/usr/bin/git"), \
                 mock.patch.object(termux_mod.subprocess, "run") as run:
                run.return_value = SimpleNamespace(returncode=128, stdout="",
                                                   stderr="fatal: could not resolve host")
                self.assertIsNone(termux_mod.ensure_layer(log=lines.append))
            self.assertTrue(any("clone failed" in line for line in lines))

    def test_ensure_layer_without_git(self):
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "termux-layer"
            lines: list[str] = []
            with mock.patch.object(termux_mod, "clone_root", return_value=target), \
                 mock.patch.object(termux_mod, "_candidates", return_value=[target]), \
                 mock.patch.object(termux_mod.shutil, "which", return_value=None):
                self.assertIsNone(termux_mod.ensure_layer(log=lines.append))
            self.assertTrue(any("git is not available" in line for line in lines))


class ValidatedVersionTest(unittest.TestCase):
    """The one dsh version the layer's anchor-based patcher accepts."""

    def test_read_from_install_sh(self):
        with TemporaryDirectory() as temporary:
            root = make_layer(Path(temporary), validated="0.1.5-rc.2")
            self.assertEqual(termux_mod.validated_version(root), "0.1.5-rc.2")

    def test_absent_constant(self):
        with TemporaryDirectory() as temporary:
            root = make_layer(Path(temporary), validated=None)
            self.assertIsNone(termux_mod.validated_version(root))

    def test_missing_directory(self):
        self.assertIsNone(termux_mod.validated_version(None))
        self.assertIsNone(termux_mod.validated_version(Path("/nonexistent-layer")))


class TargetCappingTest(unittest.TestCase):
    """The automatic target may not run past what the layer can patch."""

    def _args(self, termux=None, termux_dir=None):
        return SimpleNamespace(termux=termux, termux_dir=termux_dir)

    def test_not_termux_keeps_the_newest(self):
        with mock.patch.object(termux_mod, "active", return_value=False):
            version, note = termux_mod.target_for(self._args(), "0.1.6")
        self.assertEqual(version, "0.1.6")
        self.assertIsNone(note)

    def test_no_layer_keeps_the_newest(self):
        with mock.patch.object(termux_mod, "active", return_value=True), \
             mock.patch.object(termux_mod, "layer", return_value=None):
            version, note = termux_mod.target_for(self._args(), "0.1.6")
        self.assertEqual(version, "0.1.6")
        self.assertIsNone(note)

    def test_matching_validated_version_is_not_a_note(self):
        with TemporaryDirectory() as temporary:
            root = make_layer(Path(temporary), validated="0.1.5-rc.2")
            with mock.patch.object(termux_mod, "active", return_value=True), \
                 mock.patch.object(termux_mod, "layer", return_value=root):
                version, note = termux_mod.target_for(self._args(), "0.1.5-rc.2")
        self.assertEqual(version, "0.1.5-rc.2")
        self.assertIsNone(note)

    def test_newest_is_capped_to_the_validated_version(self):
        """The core case: a newer release exists, but the layer cannot patch it."""
        with TemporaryDirectory() as temporary:
            root = make_layer(Path(temporary), validated="0.1.5-rc.2")
            with mock.patch.object(termux_mod, "active", return_value=True), \
                 mock.patch.object(termux_mod, "layer", return_value=root), \
                 mock.patch.object(termux_mod.paths, "core_version", return_value="0.1.1-rc.2"):
                version, note = termux_mod.target_for(self._args(), "0.1.6")
        self.assertEqual(version, "0.1.5-rc.2")
        self.assertIn("validates 0.1.5-rc.2", note)

    def test_a_layer_behind_the_installed_core_is_not_a_downgrade(self):
        """Validated <= installed: the newest stands, and the note says why."""
        with TemporaryDirectory() as temporary:
            root = make_layer(Path(temporary), validated="0.1.1-rc.2")
            with mock.patch.object(termux_mod, "active", return_value=True), \
                 mock.patch.object(termux_mod, "layer", return_value=root), \
                 mock.patch.object(termux_mod.paths, "core_version", return_value="0.1.5-rc.2"):
                version, note = termux_mod.target_for(self._args(), "0.1.6")
        self.assertEqual(version, "0.1.6")
        self.assertIn("NOT been validated", note)
        self.assertIn("update the layer first", note)


class CommandOrderTest(unittest.TestCase):
    """The sequence that makes a Termux upgrade correct, in the right order."""

    def test_no_layer_means_no_extra_steps(self):
        self.assertEqual(termux_mod.commands(None, "0.1.6"), [])
        self.assertEqual(termux_mod.commands("", "0.1.6"), [])

    def test_realign_is_the_only_step_by_default(self):
        steps = termux_mod.commands("/layer", "0.1.6")
        self.assertEqual(len(steps), 1)
        # An absolute script path: the hint is runnable from any directory, which a
        # bare `bash fix-dsh-runtime.sh` would not be.
        self.assertTrue(steps[0].startswith("bash /layer/"), steps[0])
        self.assertTrue(steps[0].endswith(termux_mod.REALIGN_RELATIVE), steps[0])

    def test_rebuild_adds_the_installer(self):
        steps = termux_mod.commands("/layer", "0.1.6", rebuild=True)
        self.assertEqual(len(steps), 2)
        self.assertTrue(steps[0].startswith("bash /layer/"))
        self.assertTrue(steps[1].startswith(f"bash /layer/{termux_mod.INSTALL_RELATIVE}"))
        self.assertIn("0.1.6", steps[1])

    def test_rebuild_without_a_version(self):
        steps = termux_mod.commands("/layer", None, rebuild=True)
        self.assertEqual(steps[1].split("#")[0].strip(),
                         f"bash /layer/{termux_mod.INSTALL_RELATIVE}")


class RealignTest(unittest.TestCase):
    """Re-applying the layer, and the native probe that decides on a rebuild."""

    def test_realign_runs_the_layers_script(self):
        with TemporaryDirectory() as temporary:
            root = make_layer(Path(temporary))
            with mock.patch.object(termux_mod.subprocess, "run") as run:
                run.return_value = SimpleNamespace(returncode=0)
                self.assertEqual(termux_mod.realign(root), 0)
            command = run.call_args[0][0]
            self.assertEqual(command[0], "bash")
            self.assertTrue(command[1].endswith(termux_mod.REALIGN_RELATIVE))
            self.assertEqual(run.call_args[1]["cwd"], str(root))

    def test_realign_reports_a_missing_script(self):
        with TemporaryDirectory() as temporary:
            lines: list[str] = []
            self.assertEqual(termux_mod.realign(Path(temporary), log=lines.append), 1)
            self.assertTrue(any("no fix-dsh-runtime.sh" in line for line in lines))

    def test_natives_ok_probes_from_the_core_directory(self):
        with TemporaryDirectory() as temporary:
            with mock.patch.object(termux_mod.shutil, "which", return_value="/usr/bin/node"), \
                 mock.patch.object(termux_mod.subprocess, "run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout="0.35.4\n", stderr="")
                ok, detail = termux_mod.natives_ok(temporary)
            self.assertTrue(ok)
            self.assertEqual(detail, "0.35.4")
            self.assertEqual(run.call_args[1]["cwd"], str(temporary))

    def test_natives_failure_carries_the_reason(self):
        with TemporaryDirectory() as temporary:
            with mock.patch.object(termux_mod.shutil, "which", return_value="/usr/bin/node"), \
                 mock.patch.object(termux_mod.subprocess, "run") as run:
                run.return_value = SimpleNamespace(
                    returncode=1, stdout="",
                    stderr="Error: Cannot find module 'node-pty'\n    at ...")
                ok, detail = termux_mod.natives_ok(temporary)
            self.assertFalse(ok)
            self.assertIn("node-pty", detail)

    def test_natives_without_node_is_not_a_plugin_failure(self):
        with mock.patch.object(termux_mod.shutil, "which", return_value=None):
            ok, detail = termux_mod.natives_ok(Path("/tmp"))
        self.assertFalse(ok)
        self.assertIn("node is not on PATH", detail)


class SettingsTest(unittest.TestCase):
    """The switches that are remembered, and how they validate."""

    def test_termux_is_tristate(self):
        self.assertIsNone(config_mod._clean("termux", None))
        self.assertIs(config_mod._clean("termux", True), True)
        self.assertIs(config_mod._clean("termux", False), False)
        self.assertIsNone(config_mod._clean("termux", "yes"))

    def test_termux_dir_is_a_path(self):
        self.assertEqual(config_mod._clean("termux_dir", " ~/layer "), "~/layer")
        self.assertIsNone(config_mod._clean("termux_dir", "  "))

    def test_defaults_are_auto(self):
        self.assertIsNone(config_mod.DEFAULTS["termux"])
        self.assertIsNone(config_mod.DEFAULTS["termux_dir"])

    def test_saved_settings_round_trip(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            with mock.patch.dict(os.environ, {"DSH_UPGRADE_CONFIG": str(path)}, clear=False):
                config_mod.save({"termux": True, "termux_dir": "/opt/layer"})
                saved = config_mod.load()
            self.assertIs(saved["termux"], True)
            self.assertEqual(saved["termux_dir"], "/opt/layer")


class ParserTest(unittest.TestCase):
    """The flags exist, are accepted on either side of the subcommand, and default to auto."""

    def parse(self, *argv):
        parser = dsh_upgrade.build_parser()
        args = parser.parse_args(list(argv))
        return dsh_upgrade.apply_defaults(args, {})

    def test_default_is_auto(self):
        self.assertIsNone(self.parse("status").termux)
        self.assertIsNone(self.parse("status").termux_dir)

    def test_forcing_off_and_on(self):
        self.assertIs(self.parse("status", "--no-termux").termux, False)
        self.assertIs(self.parse("status", "--termux").termux, True)

    def test_flag_survives_being_given_before_the_subcommand(self):
        self.assertIs(self.parse("--no-termux", "pipeline").termux, False)

    def test_termux_dir(self):
        self.assertEqual(self.parse("pipeline", "--termux-dir", "/opt/layer").termux_dir,
                         "/opt/layer")

    def test_pipeline_accepts_the_switches(self):
        args = self.parse("pipeline", "--termux-dir", "/opt/layer", "--yes")
        self.assertEqual(args.termux_dir, "/opt/layer")


class SeamTest(unittest.TestCase):
    """The pipeline prints the corrected order, with realign BEFORE attach."""

    def _run_pipeline(self, *extra, termux_dir=None):
        args = SimpleNamespace(
            profile="web", core="0.1.6", offline=False, no_clone=False, json=False,
            verbose=False, color=None, state_dir=None, checkouts="temp",
            termux=True, termux_dir=termux_dir, update=False, install_unknown=False,
            run_core_upgrade=False, skip_attach=True, yes=True, dry_run=False,
            explicit=frozenset(), prune_checkouts=False)
        profile = SimpleNamespace(name="web", directory=Path("/tmp/profile"), plugins=[],
                                 bundles=[])
        buffer = io.StringIO()
        with mock.patch.object(dsh_upgrade, "load_profile", return_value=profile), \
             mock.patch.object(dsh_upgrade, "resolve_target", return_value="0.1.6"), \
             mock.patch.object(dsh_upgrade, "analyse_for",
                               return_value=SimpleNamespace(plugins=[])), \
             mock.patch.object(dsh_upgrade.report, "print_analysis"), \
             mock.patch.object(dsh_upgrade.snapshot, "write_snapshot",
                               return_value={"_jsonPath": "/tmp/s.json", "plugins": []}), \
             mock.patch.object(dsh_upgrade, "remove_plugins"), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade, "print_data_note"), \
             mock.patch.object(dsh_upgrade.termux_mod, "layer",
                               return_value=Path("/layer")), \
             redirect_stdout(buffer):
            code = dsh_upgrade.cmd_pipeline(args)
        return code, buffer.getvalue()

    def test_realign_precedes_attach(self):
        code, output = self._run_pipeline()
        self.assertEqual(code, 0)
        self.assertIn("termux layer", output)
        self.assertIn("fix-dsh-runtime.sh", output)
        self.assertIn("attach", output)
        # the whole point: npm → patches → plugins
        self.assertLess(output.index("npm i -g"), output.index("fix-dsh-runtime.sh"))
        self.assertLess(output.index("fix-dsh-runtime.sh"), output.rindex("attach"))

    def test_no_termux_switch_skips_the_layer_entirely(self):
        args = SimpleNamespace(
            profile="web", core="0.1.6", offline=False, no_clone=False, json=False,
            verbose=False, color=None, state_dir=None, checkouts="temp",
            termux=False, termux_dir=None, update=False, install_unknown=False,
            run_core_upgrade=False, skip_attach=True, yes=True, dry_run=False,
            explicit=frozenset(), prune_checkouts=False)
        profile = SimpleNamespace(name="web", directory=Path("/tmp/profile"), plugins=[],
                                 bundles=[])
        buffer = io.StringIO()
        with mock.patch.object(dsh_upgrade, "load_profile", return_value=profile), \
             mock.patch.object(dsh_upgrade, "resolve_target", return_value="0.1.6"), \
             mock.patch.object(dsh_upgrade, "analyse_for",
                               return_value=SimpleNamespace(plugins=[])), \
             mock.patch.object(dsh_upgrade.report, "print_analysis"), \
             mock.patch.object(dsh_upgrade.snapshot, "write_snapshot",
                               return_value={"_jsonPath": "/tmp/s.json", "plugins": []}), \
             mock.patch.object(dsh_upgrade, "remove_plugins"), \
             mock.patch.object(dsh_upgrade, "core_version", return_value="0.1.5-rc.2"), \
             mock.patch.object(dsh_upgrade, "print_data_note"), \
             redirect_stdout(buffer):
            code = dsh_upgrade.cmd_pipeline(args)
        self.assertEqual(code, 0)
        self.assertNotIn("fix-dsh-runtime.sh", buffer.getvalue())
        self.assertIn("attach", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
