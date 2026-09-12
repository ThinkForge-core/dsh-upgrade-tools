"""Tests for path input: completion candidates and shell-style unescaping.

The completion must behave like a shell: directories get a trailing slash, hidden
entries appear only when a dot was typed, spaces in a name are escaped (and the
escape is undone again on the way back). ``readline`` itself is optional, so the
logic under test is deliberately the pure part.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshupgrade import completion  # noqa: E402


class EscapeTest(unittest.TestCase):

    def test_escaping_then_unescaping_returns_the_path(self):
        for path in ("/tmp/plain.tgz", "/tmp/with space.tgz", "/tmp/it's here.tgz",
                     '/tmp/quote"here.tgz', "/tmp/parens (1).tgz"):
            with self.subTest(path=path):
                self.assertEqual(completion.unescape(completion.escape(path)), path)

    def test_an_unescaped_path_with_spaces_is_accepted_as_typed(self):
        """People type /tmp/a b.tgz without escaping — that must work too."""
        self.assertEqual(completion.unescape("/tmp/a b.tgz"), "/tmp/a b.tgz")

    def test_quotes_are_removed(self):
        self.assertEqual(completion.unescape("'/tmp/a b.tgz'"), "/tmp/a b.tgz")
        self.assertEqual(completion.unescape('"/tmp/a b.tgz"'), "/tmp/a b.tgz")

    def test_backslashes_that_are_not_escapes_survive(self):
        self.assertEqual(completion.unescape("/tmp/a\\b"), "/tmp/a\\b")

    def test_prepare_expands_the_home_directory(self):
        self.assertEqual(completion.prepare("~/x.tgz"), str(Path.home() / "x.tgz"))


class CandidateTest(unittest.TestCase):
    """Completion candidates for a typed prefix."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "plugin").mkdir()
        (self.root / "plugin" / "index.js").write_text("", encoding="utf-8")
        (self.root / "plugin-0.1.tgz").write_text("", encoding="utf-8")
        (self.root / "other.tgz").write_text("", encoding="utf-8")
        (self.root / ".hidden").mkdir()
        (self.root / "with space").mkdir()
        self.previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)

    def test_a_directory_is_completed_with_a_slash(self):
        self.assertEqual(completion.path_candidates("plu"),
                         ["plugin/", "plugin-0.1.tgz"])

    def test_the_contents_of_a_directory_are_completed(self):
        self.assertEqual(completion.path_candidates("plugin/"), ["plugin/index.js"])

    def test_an_absolute_path_is_completed(self):
        self.assertIn(f"{self.root}/other.tgz",
                      completion.path_candidates(f"{self.root}/oth"))

    def test_an_empty_word_lists_everything_visible(self):
        found = completion.path_candidates("")
        self.assertIn("plugin/", found)
        self.assertIn("other.tgz", found)
        self.assertNotIn(".hidden/", found)

    def test_hidden_entries_appear_only_when_the_dot_is_typed(self):
        self.assertEqual(completion.path_candidates("."), [".hidden/"])
        self.assertNotIn(".hidden/", completion.path_candidates("plu"))

    def test_a_space_in_a_name_is_escaped(self):
        self.assertEqual(completion.path_candidates("wi"), ["with\\ space/"])

    def test_no_match_is_an_empty_list(self):
        self.assertEqual(completion.path_candidates("nothing-here"), [])

    def test_a_specifier_prefix_is_preserved(self):
        """inspect accepts file:/link: specifiers, so completion must too."""
        self.assertEqual(completion.path_candidates(f"file:{self.root}/oth"),
                         [f"file:{self.root}/other.tgz"])
        self.assertEqual(completion.path_candidates("link:plu"),
                         ["link:plugin/", "link:plugin-0.1.tgz"])

    def test_the_home_directory_is_completed_but_the_tilde_is_kept(self):
        found = completion.path_candidates("~")
        self.assertTrue(found)
        self.assertTrue(all(candidate.startswith("~") for candidate in found))

    def test_an_unreadable_directory_is_not_an_error(self):
        self.assertEqual(completion.path_candidates("/definitely/not/here/x"), [])


class ReadlineHookTest(unittest.TestCase):

    def test_install_reports_whether_readline_exists(self):
        self.assertEqual(completion.install(), completion._readline is not None)

    def test_remember_ignores_blank_lines(self):
        completion.remember("   ")
        completion.remember("/tmp/x.tgz")
        if completion._readline is not None:
            self.assertIn("/tmp/x.tgz",
                          [completion._readline.get_history_item(i)
                           for i in range(1, completion._readline.get_current_history_length() + 1)])


if __name__ == "__main__":
    unittest.main()
