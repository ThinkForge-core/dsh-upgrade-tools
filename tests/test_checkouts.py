"""Tests for the checkout location: under /tmp by default, chosen when asked.

A version checkout is a means to an end (the comparison), not a data store, so the
default must live where the operating system cleans up on its own (the temporary
directory) — while still being REUSED by the next run, otherwise every run would
download the same tag again. A location the user picked is used instead.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import checkout as checkout_mod  # noqa: E402
from dshupgrade import paths  # noqa: E402


class CheckoutSettingTest(unittest.TestCase):
    """Normalisation and precedence of the configured location."""

    def setUp(self):
        paths.set_checkouts_setting(None)
        self.addCleanup(paths.set_checkouts_setting, None)
        patcher = mock.patch.dict(os.environ, {"DSH_CHECKOUTS_ROOT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_synonyms(self):
        for value in ("", None, "  ", "temp", "TMP", "/tmp"):
            with self.subTest(value=value):
                self.assertEqual(paths.normalize_checkouts(value), paths.TEMP)
        for value in ("keep", "KEEP", "home"):
            with self.subTest(value=value):
                self.assertEqual(paths.normalize_checkouts(value), paths.KEEP)

    def test_a_directory_is_made_absolute(self):
        normalized = paths.normalize_checkouts("~/checkouts-demo")
        self.assertTrue(Path(normalized).is_absolute())
        self.assertEqual(Path(normalized), (Path.home() / "checkouts-demo").resolve())

    def test_temporary_is_the_default(self):
        self.assertEqual(paths.checkouts_setting(), paths.TEMP)
        self.assertTrue(paths.temporary_checkouts())

    def test_the_environment_is_honoured(self):
        with mock.patch.dict(os.environ, {"DSH_CHECKOUTS_ROOT": "/tmp/dsh-demo-checkouts"}):
            self.assertEqual(paths.checkouts_setting(),
                             str(Path("/tmp/dsh-demo-checkouts").resolve()))
            self.assertFalse(paths.temporary_checkouts())

    def test_an_explicit_setting_beats_the_environment(self):
        with mock.patch.dict(os.environ, {"DSH_CHECKOUTS_ROOT": "/tmp/from-env"}):
            paths.set_checkouts_setting("/tmp/from-settings")
            self.assertEqual(paths.checkouts_setting(),
                             str(Path("/tmp/from-settings").resolve()))


class CoreInstallDirTest(unittest.TestCase):
    """Finding the installed core when ``dsh`` is not on PATH (an nvm install).

    The node version directory cannot be assumed: it is whatever the user has, so
    the fallback has to discover the installed versions instead of naming one.
    """

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DSH_INSTALL_DIR": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _nvm_core(self, home: Path, version: str) -> Path:
        core = (home / ".nvm" / "versions" / "node" / version
                / "lib" / "node_modules" / "@deepseek-ai" / "dsh")
        core.mkdir(parents=True)
        (core / "package.json").write_text('{"version": "1.2.3"}', encoding="utf-8")
        return core

    def test_an_nvm_node_version_is_found_without_path(self):
        with TemporaryDirectory() as tmp:
            core = self._nvm_core(Path(tmp), "v24.19.0")
            with mock.patch.object(paths.shutil, "which", return_value=None), \
                    mock.patch.object(paths.Path, "home", return_value=Path(tmp)):
                self.assertEqual(paths.core_install_dir(), core.resolve())
                self.assertEqual(paths.core_version(), "1.2.3")

    def test_the_newest_node_version_wins(self):
        with TemporaryDirectory() as tmp:
            newest = self._nvm_core(Path(tmp), "v24.19.0")
            self._nvm_core(Path(tmp), "v9.11.2")
            with mock.patch.object(paths.shutil, "which", return_value=None), \
                    mock.patch.object(paths.Path, "home", return_value=Path(tmp)):
                self.assertEqual(paths.core_install_dir(), newest.resolve())

    def test_an_absent_nvm_is_not_an_error(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(paths.shutil, "which", return_value=None), \
                    mock.patch.object(paths.Path, "home", return_value=Path(tmp)):
                self.assertIsNone(paths.core_install_dir())


class TempRootPathTest(unittest.TestCase):
    """Where the temporary root is derived — it must be the same path every run."""

    def test_it_sits_in_the_system_temporary_directory(self):
        root = paths.temp_checkouts_root()
        self.assertEqual(root.parent, Path(tempfile.gettempdir()))
        self.assertTrue(root.name.startswith("dsh-upgrade-checkouts"))

    def test_it_is_stable_across_calls(self):
        """A random directory per run would re-download every tag on every run."""
        self.assertEqual(paths.temp_checkouts_root(), paths.temp_checkouts_root())


class TemporaryRootTest(unittest.TestCase):
    """The default is reused between runs and removable on demand."""

    def setUp(self):
        paths.set_checkouts_setting(None)
        self.addCleanup(paths.set_checkouts_setting, None)
        patcher = mock.patch.dict(os.environ, {"DSH_CHECKOUTS_ROOT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "tmp-checkouts"
        root_patcher = mock.patch.object(paths, "temp_checkouts_root", return_value=self.root)
        root_patcher.start()
        self.addCleanup(root_patcher.stop)

    def test_the_root_is_created_on_first_use(self):
        self.assertFalse(self.root.exists())
        self.assertEqual(paths.checkouts_root(), self.root)
        self.assertTrue(self.root.is_dir())

    def test_the_same_root_is_reused_between_runs(self):
        self.assertEqual(paths.checkouts_root(), paths.checkouts_root())

    def test_a_checkout_from_a_previous_run_is_found_not_re_fetched(self):
        """The whole point of the fixed path: no second download of the same tag."""
        existing = self.root / "deepseek-harness-0.1.5-rc.2" / "packages"
        existing.mkdir(parents=True)
        with mock.patch.object(checkout_mod, "run") as run:
            found = checkout_mod.ensure_checkout("0.1.5-rc.2", log=lambda *_: None)
        self.assertEqual(found, self.root / "deepseek-harness-0.1.5-rc.2")
        run.assert_not_called()

    def test_prune_removes_it_and_the_same_path_comes_back(self):
        (self.root / "deepseek-harness-0.1.5-rc.2").mkdir(parents=True)
        removed = paths.prune_checkouts()
        self.assertIn(self.root, removed)
        self.assertFalse(self.root.exists())
        self.assertEqual(paths.checkouts_root(), self.root)

    def test_prune_is_idempotent(self):
        self.assertEqual(paths.prune_checkouts(), [])

    def test_checkout_dir_sits_inside_the_root(self):
        self.assertEqual(paths.checkout_dir("0.1.5-rc.2"),
                         self.root / "deepseek-harness-0.1.5-rc.2")

    def test_the_old_default_is_still_searched(self):
        """Checkouts downloaded before must be reused, not fetched again."""
        self.assertIn((paths.dsh_home() / "checkouts").resolve(), paths.checkout_roots())

    def test_the_temp_root_is_searched_without_being_created(self):
        self.assertIn(self.root, paths.checkout_roots())
        self.assertFalse(self.root.exists())


class KeptRootTest(unittest.TestCase):
    """A chosen directory is kept and searched."""

    def setUp(self):
        paths.set_checkouts_setting(None)
        self.addCleanup(paths.set_checkouts_setting, None)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_keep_means_the_dsh_home(self):
        paths.set_checkouts_setting("keep")
        self.assertEqual(paths.checkouts_root(), (paths.dsh_home() / "checkouts").resolve())

    def test_a_directory_is_used_as_is(self):
        paths.set_checkouts_setting(self.tmp.name)
        self.assertEqual(paths.checkouts_root(), Path(self.tmp.name))
        self.assertEqual(paths.checkout_dir("0.1.5-rc.2"),
                         Path(self.tmp.name) / "deepseek-harness-0.1.5-rc.2")

    def test_find_checkout_sees_a_kept_checkout(self):
        paths.set_checkouts_setting(self.tmp.name)
        wanted = Path(self.tmp.name) / "deepseek-harness-0.1.5-rc.2" / "packages"
        wanted.mkdir(parents=True)
        self.assertEqual(paths.find_checkout("0.1.5-rc.2"),
                         Path(self.tmp.name) / "deepseek-harness-0.1.5-rc.2")

    def test_find_checkout_is_none_when_nothing_is_there(self):
        self.assertIsNone(paths.find_checkout("0.0.0-nope"))


class CloneTest(unittest.TestCase):
    """The clone itself: the smallest download that still works."""

    def setUp(self):
        paths.set_checkouts_setting(None)
        self.addCleanup(paths.set_checkouts_setting, None)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Isolate the legacy default: <DSH_HOME>/checkouts must look empty here,
        # otherwise a checkout of this machine would be found instead of a clone.
        home = mock.patch.object(paths, "dsh_home", return_value=Path(self.tmp.name) / "dsh-home")
        home.start()
        self.addCleanup(home.stop)

    def test_definition_only_needs_one_commit(self):
        """The comparison reads files, never history — so depth 1, not 2."""
        self.assertEqual(checkout_mod.CLONE_DEPTH, "1")

    def test_an_existing_checkout_is_reused_without_git(self):
        paths.set_checkouts_setting(self.tmp.name)
        existing = Path(self.tmp.name) / "deepseek-harness-0.1.5-rc.2"
        (existing / "packages").mkdir(parents=True)
        with mock.patch.object(checkout_mod, "run") as run:
            found = checkout_mod.ensure_checkout("0.1.5-rc.2", log=lambda *_: None)
        self.assertEqual(found, existing)
        run.assert_not_called()

    def test_a_missing_checkout_is_cloned_at_the_configured_depth(self):
        paths.set_checkouts_setting(self.tmp.name)
        with mock.patch.object(checkout_mod, "run") as run:
            run.return_value = mock.Mock(returncode=0, stderr="")
            found = checkout_mod.ensure_checkout("0.1.5-rc.2", log=lambda *_: None)
        self.assertEqual(found, Path(self.tmp.name) / "deepseek-harness-0.1.5-rc.2")
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["git", "clone"])
        self.assertIn("--depth=1", command)
        self.assertIn("dsh-v0.1.5-rc.2", command)

    def test_no_clone_reports_instead_of_cloning(self):
        paths.set_checkouts_setting(self.tmp.name)
        with mock.patch.object(checkout_mod, "run") as run:
            found = checkout_mod.ensure_checkout("0.1.5-rc.2", allow_clone=False,
                                                 log=lambda *_: None)
        self.assertIsNone(found)
        run.assert_not_called()

    def test_git_is_checked_before_cloning(self):
        paths.set_checkouts_setting(self.tmp.name)
        with mock.patch.object(checkout_mod.shutil, "which", return_value=None), \
             mock.patch.object(checkout_mod, "run") as run:
            self.assertIsNone(checkout_mod.ensure_checkout("0.1.5-rc.2", log=lambda *_: None))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
