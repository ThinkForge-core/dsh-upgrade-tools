"""Tests for the runtime-graded verdict and for per-plugin selection.

A declaration check and a runtime probe answer different questions. A plugin that
declares no DSH version is not thereby broken, and one whose installed copy the
probe has seen import, apply and answer is better than "compatible on paper" — it is
*verified*. These tests keep that grade honest: what the probe must prove, what may
never be hidden by it, that it is evidence about ONE core version only, and that a
narrowed operation (`--only`) selects plugins instead of silently selecting none.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh_upgrade  # noqa: E402
from dshupgrade import analysis as analysis_mod  # noqa: E402
from dshupgrade import compat  # noqa: E402
from dshupgrade import report  # noqa: E402
from dshupgrade import verify as verify_mod  # noqa: E402
from dshupgrade.profile import PluginEntry, Profile  # noqa: E402

VERIFIED = compat.STATUS_VERIFIED
COMPATIBLE = compat.STATUS_COMPATIBLE
UNKNOWN = compat.STATUS_UNKNOWN
INCOMPATIBLE = compat.STATUS_INCOMPATIBLE


def handler_record(path: str, error: str | None = None) -> dict:
    return {"path": path, "error": error, "logs": [], "status": 200, "body": "",
            "timedOut": False, "ms": 4}


def entry(name: str, status: str, **extra) -> dict:
    base = {"name": name, "status": status, "reason": "", "code_clean": False,
            "declared_status": None}
    base.update(extra)
    return base


def analysis_with(*entries, target: str = "0.1.5-rc.2",
                  current: str | None = "0.1.5-rc.2"):
    result = analysis_mod.Analysis(target=target, current_core=current, checkout=None)
    result.plugins = list(entries)
    return result


def profile_with(*names: str) -> Profile:
    return Profile(
        directory=Path("/tmp/web"), name="web",
        plugins=[PluginEntry(name=name, spec=name, source="npm", installed=True,
                             version="1.0.0", directory=None, in_bundles=True)
                 for name in names])


class RuntimeVerifiedTest(unittest.TestCase):
    """What the probe has to prove before the tool calls a copy verified."""

    def test_a_clean_load_is_verified(self):
        self.assertTrue(verify_mod.runtime_verified(
            verify_mod.Probe("p", verify_mod.LOADS, applied=True)))

    def test_a_handler_broken_at_request_time_is_not_verified(self):
        probe = verify_mod.Probe(
            "p", verify_mod.LOADS, applied=True,
            handlers=[handler_record("/x", "ReferenceError: X is not defined")])
        self.assertFalse(verify_mod.runtime_verified(probe))

    def test_an_apply_error_is_not_verified(self):
        self.assertFalse(verify_mod.runtime_verified(
            verify_mod.Probe("p", verify_mod.LOADS, apply_error="boom")))

    def test_only_a_real_load_counts(self):
        """A probe that never ran, or has no server half, proves nothing."""
        for status in (verify_mod.FAILED, verify_mod.CLIENT_ONLY, verify_mod.MISSING,
                       verify_mod.UNAVAILABLE):
            with self.subTest(status=status):
                self.assertFalse(
                    verify_mod.runtime_verified(verify_mod.Probe("p", status)))


class GradingTest(unittest.TestCase):
    """The upgrade path: what becomes verified, and what never does."""

    def test_a_compatible_entry_becomes_verified(self):
        result = analysis_with(entry("a", COMPATIBLE))
        graded = analysis_mod.apply_runtime_verification(result, {"a"})
        self.assertEqual(len(graded), 1)
        self.assertEqual(result.plugins[0]["status"], VERIFIED)
        self.assertEqual(result.plugins[0]["declared_status"], COMPATIBLE)
        self.assertEqual(result.plugins[0]["reason"], "verified at runtime on this core")

    def test_a_clean_undeclared_entry_becomes_verified(self):
        result = analysis_with(entry(
            "a", UNKNOWN, code_clean=True,
            reason="the manifest declares no DSH version, so there is nothing to compare; "
                   "the code checks are clean"))
        analysis_mod.apply_runtime_verification(result, {"a"})
        self.assertEqual(result.plugins[0]["status"], VERIFIED)
        self.assertIn("declares no DSH version", result.plugins[0]["reason"])
        self.assertIn("verified at runtime", result.plugins[0]["reason"])

    def test_an_undeclared_entry_without_a_clean_code_check_stays_unconfirmed(self):
        """An input gap is not evidence: the code checks have to have run."""
        result = analysis_with(entry("a", UNKNOWN, code_clean=False,
                                     reason="manifest unavailable"))
        analysis_mod.apply_runtime_verification(result, {"a"})
        self.assertEqual(result.plugins[0]["status"], UNKNOWN)

    def test_a_proven_incompatibility_is_never_hidden(self):
        result = analysis_with(entry("a", INCOMPATIBLE, reason="hard link to a removed package"))
        analysis_mod.apply_runtime_verification(result, {"a"})
        self.assertEqual(result.plugins[0]["status"], INCOMPATIBLE)
        self.assertEqual(result.plugins[0]["reason"], "hard link to a removed package")

    def test_an_empirical_verdict_keeps_its_reason(self):
        """What a graded entry already said must survive the grade."""
        result = analysis_with(entry(
            "a", COMPATIBLE, empirical=True,
            reason="already installed and running on this core; declarations are stricter: "
                   "x → below-min"))
        analysis_mod.apply_runtime_verification(result, {"a"})
        self.assertEqual(result.plugins[0]["status"], VERIFIED)
        self.assertIn("stricter", result.plugins[0]["reason"])

    def test_names_that_are_not_in_the_result_are_ignored(self):
        result = analysis_with(entry("a", COMPATIBLE))
        analysis_mod.apply_runtime_verification(result, {"b"})
        self.assertEqual(result.plugins[0]["status"], COMPATIBLE)

    def test_the_counts_carry_the_new_bucket(self):
        result = analysis_with(entry("a", VERIFIED), entry("b", COMPATIBLE))
        counts = result.counts()
        self.assertEqual(counts[VERIFIED], 1)
        self.assertEqual(counts[COMPATIBLE], 1)


class AcceptanceTest(unittest.TestCase):
    """`accepted` answers "will this plugin be held back?"."""

    def test_verified_is_accepted_like_compatible(self):
        self.assertTrue(compat.accepted(COMPATIBLE))
        self.assertTrue(compat.accepted(VERIFIED))

    def test_unconfirmed_and_incompatible_are_not_accepted(self):
        self.assertFalse(compat.accepted(UNKNOWN))
        self.assertFalse(compat.accepted(INCOMPATIBLE))
        self.assertFalse(compat.accepted(None))


class MarkVerifiedTest(unittest.TestCase):
    """The cache is consulted, and only for the core it was taken against."""

    def setUp(self):
        self.profile = SimpleNamespace(name="web")

    def probes(self, name: str = "a") -> dict:
        return {name: verify_mod.Probe(name, verify_mod.LOADS, applied=True)}

    def grade(self, result, probes=None, *, shadows=None, wire=None):
        with mock.patch.object(verify_mod, "load_cache", return_value=probes or {}), \
             mock.patch.object(dsh_upgrade, "profile_surfaces",
                               return_value=(None, shadows or {}, set())), \
             mock.patch.object(dsh_upgrade, "wire_surfaces", return_value=wire or {}):
            dsh_upgrade._mark_verified(result, self.profile)

    def test_another_target_is_never_graded(self):
        result = analysis_with(entry("a", COMPATIBLE), target="0.1.5-rc.1")
        with mock.patch.object(verify_mod, "load_cache") as cache:
            dsh_upgrade._mark_verified(result, self.profile)
        cache.assert_not_called()
        self.assertEqual(result.plugins[0]["status"], COMPATIBLE)

    def test_a_missing_installed_core_is_never_graded(self):
        result = analysis_with(entry("a", COMPATIBLE), current=None, target=None)
        with mock.patch.object(verify_mod, "load_cache") as cache:
            dsh_upgrade._mark_verified(result, self.profile)
        cache.assert_not_called()

    def test_a_clean_cached_verdict_grades_the_entry(self):
        result = analysis_with(entry("a", COMPATIBLE))
        self.grade(result, self.probes())
        self.assertEqual(result.plugins[0]["status"], VERIFIED)

    def test_a_dead_wire_call_blocks_the_grade(self):
        result = analysis_with(entry("a", COMPATIBLE))
        self.grade(result, self.probes(), wire={"a": [object()]})
        self.assertEqual(result.plugins[0]["status"], COMPATIBLE)

    def test_a_switched_off_surface_blocks_the_grade(self):
        result = analysis_with(entry("a", COMPATIBLE))
        self.grade(result, self.probes(), shadows={"a": [SimpleNamespace(explained=False)]})
        self.assertEqual(result.plugins[0]["status"], COMPATIBLE)

    def test_a_surface_with_a_known_replacement_does_not_block_it(self):
        result = analysis_with(entry("a", COMPATIBLE))
        self.grade(result, self.probes(), shadows={"a": [SimpleNamespace(explained=True)]})
        self.assertEqual(result.plugins[0]["status"], VERIFIED)

    def test_a_plugin_the_probe_did_not_prove_is_left_alone(self):
        result = analysis_with(entry("a", COMPATIBLE))
        self.grade(result, {"a": verify_mod.Probe("a", verify_mod.UNAVAILABLE)})
        self.assertEqual(result.plugins[0]["status"], COMPATIBLE)


class ReportTest(unittest.TestCase):
    """The grade has to be visible, and it is the strongest one."""

    def test_the_verified_status_has_its_own_mark_and_label(self):
        self.assertIn(VERIFIED, report.GLYPH)
        self.assertEqual(report.STATUS_LABEL[VERIFIED], "verified")
        self.assertEqual(report.STATUS_LABEL[COMPATIBLE], "compatible")

    def test_verified_is_grouped_after_compatible(self):
        self.assertEqual(report.STATUS_ORDER[-1], VERIFIED)
        self.assertLess(report.STATUS_ORDER.index(COMPATIBLE),
                        report.STATUS_ORDER.index(VERIFIED))
        self.assertLess(report.STATUS_ORDER.index(UNKNOWN),
                        report.STATUS_ORDER.index(VERIFIED))


class SelectionTest(unittest.TestCase):
    """`--only` selects plugins; it can never silently select nothing."""

    def test_without_the_flag_every_plugin_is_selected(self):
        profile = profile_with("b", "a")
        chosen = dsh_upgrade._selection(SimpleNamespace(only=None), profile, what="plugins")
        self.assertEqual(chosen, ["b", "a"])

    def test_a_subset_keeps_the_profile_order(self):
        profile = profile_with("a", "b", "c")
        chosen = dsh_upgrade._selection(SimpleNamespace(only=["c", "a"]), profile,
                                        what="plugins")
        self.assertEqual(chosen, ["a", "c"])

    def test_an_unknown_name_is_an_error_that_names_the_operation(self):
        profile = profile_with("a")
        with self.assertRaises(SystemExit) as caught:
            dsh_upgrade._selection(SimpleNamespace(only=["a", "nope"]), profile,
                                   what="detach")
        message = str(caught.exception)
        self.assertIn("nope", message)
        self.assertIn("--only detach", message)
        self.assertIn("a", message)

    def test_the_parser_accepts_only_for_the_three_commands(self):
        parser = dsh_upgrade.build_parser()
        for command in ("detach", "attach", "plugins"):
            with self.subTest(command=command):
                parsed = parser.parse_args([command, "--only", "x", "y"])
                self.assertEqual(parsed.only, ["x", "y"])

    def test_commands_without_the_flag_do_not_carry_it(self):
        """Which is why the selection reads it with getattr — and then takes all."""
        parser = dsh_upgrade.build_parser()
        parsed = parser.parse_args(["check"])
        self.assertFalse(hasattr(parsed, "only"))
        self.assertEqual(dsh_upgrade._selection(SimpleNamespace(), profile_with("a"),
                                                what="plugins"), ["a"])


if __name__ == "__main__":
    unittest.main()
