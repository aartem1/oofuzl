import os
import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
from code_health import config as C  # noqa: E402
from code_health import discovery as D  # noqa: E402


class PatternTests(unittest.TestCase):
    def assert_match(self, pattern, path, expected):
        rx = D.compile_pattern(pattern)
        self.assertEqual(bool(rx.match(path)), expected, "%s vs %s" % (pattern, path))

    def test_double_star_prefix_means_zero_or_more_directories(self):
        # The exact case fnmatch and PurePath.match get wrong.
        self.assert_match("**/dist/**", "dist/a.js", True)
        self.assert_match("**/dist/**", "packages/web/dist/a.js", True)
        self.assert_match("**/node_modules/**", "node_modules/x/y.js", True)
        self.assert_match("**/node_modules/**", "a/b/node_modules/x/y.js", True)

    def test_double_star_does_not_match_a_similar_prefix(self):
        self.assert_match("**/dist/**", "distribution/a.js", False)
        self.assert_match("**/dist/**", "dist", False)

    def test_single_star_stays_inside_one_segment(self):
        self.assert_match("**/test_*.py", "pkg/test_thing.py", True)
        self.assert_match("**/test_*.py", "pkg/test_sub/thing.py", False)

    def test_bare_name_matches_at_any_depth(self):
        self.assert_match("*.spec.ts", "a/b/c.spec.ts", True)
        self.assert_match("*.spec.ts", "c.spec.ts", True)

    def test_leading_slash_anchors_at_root(self):
        self.assert_match("/dist/**", "dist/a.js", True)
        self.assert_match("/dist/**", "web/dist/a.js", False)


class EnumerateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cfg = C.load_config(self.root)

    def write(self, rel, content="x = 1\n"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        return p

    def test_source_and_tests_are_separated(self):
        self.write("app/main.py")
        self.write("tests/test_main.py")
        self.write("app/thing.spec.ts")
        inv = D.enumerate_files(self.root, self.cfg)
        self.assertEqual(inv.source, ["app/main.py"])
        self.assertEqual(sorted(inv.tests), ["app/thing.spec.ts", "tests/test_main.py"])

    def test_excluded_paths_appear_in_neither_bucket(self):
        self.write("node_modules/pkg/index.js")
        self.write("packages/web/dist/bundle.js")
        self.write("app/main.py")
        inv = D.enumerate_files(self.root, self.cfg)
        self.assertEqual(inv.source, ["app/main.py"])
        self.assertEqual(inv.tests, [])

    def test_binary_files_are_skipped_and_counted(self):
        # The binary probe only ever sees files that carry a source
        # extension now, so the fixture has to be one: a UTF-16-encoded
        # `.py` is the realistic case, since UTF-16 text is full of NUL
        # bytes. A `.bin` never reaches this branch -- it is counted as
        # not_source instead, one check earlier.
        self.write("app/utf16.py", "x = 1\n".encode("utf-16"))
        self.write("app/main.py")
        inv = D.enumerate_files(self.root, self.cfg)
        self.assertEqual(inv.source, ["app/main.py"])
        self.assertEqual(inv.skipped["binary"], 1)

    def test_documentation_and_config_are_not_measured_as_source(self):
        """The audit used to measure every tracked text file. On this
        repository that meant 40 Markdown files against 16 Python ones, and
        `summary.by_cut` percentages computed over design documents."""
        self.write("README.md", "# title\n")
        self.write("package-lock.json", "{}\n")
        self.write("ci.yaml", "on: push\n")
        self.write("app/main.py")
        inv = D.enumerate_files(self.root, self.cfg)
        self.assertEqual(inv.source, ["app/main.py"])
        self.assertEqual(inv.tests, [])
        self.assertEqual(inv.skipped["not_source"], 3)

    def test_oversize_files_are_skipped_and_counted(self):
        cfg = dict(self.cfg, max_file_bytes=10)
        self.write("app/big.py", "x" * 100)
        inv = D.enumerate_files(self.root, cfg)
        self.assertEqual(inv.source, [])
        self.assertEqual(inv.skipped["too_large"], 1)

    def test_symlinks_are_skipped_and_counted(self):
        self.write("app/main.py")
        os.symlink(self.root / "app" / "main.py", self.root / "app" / "link.py")
        inv = D.enumerate_files(self.root, self.cfg)
        self.assertEqual(inv.source, ["app/main.py"])
        self.assertEqual(inv.skipped["symlink"], 1)

    def test_walk_fallback_never_descends_into_dot_git(self):
        self.write(".git/objects/ab/cdef", "not source")
        self.write("app/main.py")
        inv = D.enumerate_files(self.root, self.cfg)
        self.assertEqual(inv.source, ["app/main.py"])


if __name__ == "__main__":
    unittest.main()
