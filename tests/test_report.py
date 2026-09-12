"""Tests for the report/table layer: wrapping, grouping, colors.

The tables must never truncate a cell: a long ``reason`` is wrapped onto
continuation lines that stay aligned under their own column, and the total width
of the table respects the terminal width.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import report, style  # noqa: E402
from dshupgrade.compat import (  # noqa: E402
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
    STATUS_UNKNOWN,
)

LONG_REASON = ("client bundle requires a module missing from the target module "
               "table: @deepseek-ai/dsh-client-runtime/client")
HEADERS = ["", "plugin", "version", "source", "latest", "reason"]


def sample_rows():
    """Short names on purpose: only the ``reason`` column has to wrap here."""
    return [
        [report.status_glyph(STATUS_INCOMPATIBLE), "dsh-alpha", "0.1.1-rc.2",
         "file·tgz", "—", LONG_REASON],
        [report.status_glyph(STATUS_COMPATIBLE), "dsh-beta", "0.18.0", "npm", "0.19.1", ""],
        [report.status_glyph(STATUS_UNKNOWN), "dsh-gamma", "1.0.0", "link·dir", "—",
         "no requirement declarations for the DSH version; code checks are clean"],
    ]


class TableWidthTest(unittest.TestCase):
    """The table fits the terminal and never cuts text away."""

    def setUp(self):
        style.set_mode("never")

    def tearDown(self):
        style.set_mode("auto")

    def test_no_line_is_wider_than_the_limit(self):
        for width in (60, 80, 100, 120):
            with self.subTest(width=width):
                text = report.table(HEADERS, sample_rows(), width=width)
                for line in text.splitlines():
                    self.assertLessEqual(style.width(line), width, line)

    def test_reason_is_never_truncated(self):
        text = report.table(HEADERS, sample_rows(), width=80)
        # Whitespace is removed on both sides: the reason may be broken between
        # columns (and inside a long token), but every character must survive.
        compact = "".join("".join(style.strip(line).split()) for line in text.splitlines())
        expected = "".join(LONG_REASON.split())
        self.assertIn(expected, compact)

    def test_wrapped_reason_stays_under_its_column(self):
        text = report.table(HEADERS, sample_rows(), width=80)
        lines = text.splitlines()
        start = lines[0].index("reason")
        continuation = [line for line in lines
                        if line.startswith(" " * start) and not line.startswith(" " * (start + 1))]
        self.assertTrue(continuation, "expected at least one wrapped continuation line")
        for line in continuation:
            self.assertTrue(line[start:].strip(), line)

    def test_wide_cell_does_not_move_other_columns(self):
        """Columns keep their offsets on every physical line."""
        text = report.table(HEADERS, sample_rows(), width=100)
        lines = text.splitlines()
        plugin_start = lines[0].index("plugin")
        names = [line for line in lines if "dsh-beta" in line]
        self.assertEqual(len(names), 1)
        self.assertEqual(names[0].index("dsh-beta"), plugin_start)


class GroupedTableTest(unittest.TestCase):

    def setUp(self):
        style.set_mode("never")

    def tearDown(self):
        style.set_mode("auto")

    def test_groups_are_rendered_in_order(self):
        groups = [
            ("NO — incompatible (1)", [sample_rows()[0]]),
            ("ok — compatible (1)", [sample_rows()[1]]),
        ]
        grouped = report.grouped_table(HEADERS, groups, width=100)
        self.assertIn("NO — incompatible (1)", grouped)
        self.assertIn("ok — compatible (1)", grouped)
        self.assertLess(grouped.index("NO — incompatible"), grouped.index("ok — compatible"))
        # The header is printed once, before the first group.
        self.assertEqual(grouped.count("reason"), 1)

    def test_empty_groups_are_skipped(self):
        grouped = report.grouped_table(HEADERS, [("nothing (0)", [])], width=100)
        self.assertEqual(grouped, "")

    def test_analysis_groups_order_and_counts(self):
        plugins = [
            {"name": "a", "status": STATUS_COMPATIBLE, "version": "1.0.0", "source": "npm"},
            {"name": "b", "status": STATUS_INCOMPATIBLE, "version": "1.0.0", "source": "npm",
             "reason": "x"},
            {"name": "c", "status": STATUS_UNKNOWN, "version": "1.0.0", "source": "npm"},
        ]
        groups = report.analysis_groups(plugins)
        self.assertEqual(len(groups), 3)
        self.assertIn("NO", groups[0][0])
        self.assertIn("??", groups[1][0])
        self.assertIn("ok", groups[2][0])
        for label, rows in groups:
            self.assertIn("(1)", label)
            self.assertEqual(len(rows), 1)


class GlyphTest(unittest.TestCase):

    def test_status_glyphs_are_english(self):
        self.assertEqual(report.GLYPH[STATUS_COMPATIBLE], "ok")
        self.assertEqual(report.GLYPH[STATUS_INCOMPATIBLE], "NO")
        self.assertEqual(report.GLYPH[STATUS_UNKNOWN], "??")
        self.assertEqual(report.STATUS_LABEL[STATUS_INCOMPATIBLE], "incompatible")


class ColorTest(unittest.TestCase):

    def tearDown(self):
        style.set_mode("auto")

    def test_colors_off_by_default_without_a_tty(self):
        style.set_mode("never")
        self.assertEqual(style.paint("x", "red"), "x")
        self.assertEqual(style.width(style.paint("hello", "bold")), 5)

    def test_colors_on_when_forced(self):
        style.set_mode("always")
        painted = style.paint("x", "red")
        self.assertNotEqual(painted, "x")
        self.assertIn("\033[", painted)
        self.assertEqual(style.width(painted), 1)

    def test_colored_table_keeps_its_alignment(self):
        """Escape sequences must not count towards the column widths."""
        width = 80
        style.set_mode("never")
        plain = report.table(HEADERS, sample_rows(), width=width)
        style.set_mode("always")
        colored = report.table(HEADERS, sample_rows(), width=width)
        self.assertIn("\033[", colored)
        self.assertNotIn("\033[", plain)
        self.assertEqual([style.strip(line) for line in colored.splitlines()],
                         plain.splitlines())

    def test_mode_normalization(self):
        self.assertEqual(style.normalize_mode("ALWAYS"), "always")
        self.assertEqual(style.normalize_mode("off"), "never")
        self.assertEqual(style.normalize_mode("nonsense"), "auto")
        self.assertEqual(style.normalize_mode(None), "auto")


class WrappingHelperTest(unittest.TestCase):

    def test_wrap_breaks_long_tokens(self):
        lines = style.wrap("a" * 25, 10)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(len(line) <= 10 for line in lines))

    def test_wrap_lines_keeps_paragraphs(self):
        lines = style.wrap_lines("one\ntwo", 10)
        self.assertEqual(lines, ["one", "two"])

    def test_terminal_width_has_a_floor(self):
        self.assertGreaterEqual(style.terminal_width(), 40)


if __name__ == "__main__":
    unittest.main()
