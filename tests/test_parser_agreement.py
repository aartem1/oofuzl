import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PLUGIN_ROOT, load_package

load_package()
from code_health import pyast as PA  # noqa: E402
from code_health import sizes as S  # noqa: E402

# Every numeric field holds a DISTINCT value on purpose. With coincidental
# duplicates (the real lizard row this is modelled on has ccn == param == 5 and
# nloc == length == end_line == 7) a column swap between two equal fields is
# invisible, and catching column swaps is this file's entire reason to exist.
# Consistency check: length == end_line - start_line + 1 == 9.
SAMPLE_CSV = (
    '7,5,41,3,9,"outer@11-19@probe.py","probe.py","outer",'
    '"outer( a , b , c = 1 , * args , ** kw )",11,19,4\n'
)


def load_guard():
    spec = importlib.util.spec_from_file_location(
        "vendored_guard", PLUGIN_ROOT / "scripts" / "guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParserAgreementTests(unittest.TestCase):
    """guard.py duplicates the CSV parse on purpose -- it runs where
    code_health does not exist. This keeps the two honest."""

    def test_both_parsers_produce_identical_numbers(self):
        guard = load_guard()
        mine = S.parse_lizard_csv(SAMPLE_CSV)[0]
        theirs = guard.parse_lizard_csv(SAMPLE_CSV)[0]
        for field in ("nloc", "ccn", "token", "param", "length",
                      "start_line", "end_line", "nesting"):
            self.assertEqual(mine[field], theirs[field], field)
        self.assertEqual(mine["function_name"], theirs["function_name"])
        # Guard the guard: if the fixture ever regains duplicate numeric values,
        # a swap between the duplicated columns stops being detectable.
        numbers = [mine[f] for f in ("nloc", "ccn", "token", "param", "length",
                                     "start_line", "end_line", "nesting")]
        self.assertEqual(len(numbers), len(set(numbers)),
                         "fixture must keep every numeric field distinct")

    def test_both_use_the_same_source_extension_set(self):
        """The audit and the ratchet must agree on what a file even is.

        They did not: `discovery.enumerate_files` measured every tracked
        non-binary file, so 40 `.md` files were measured as source on this
        repository while `guard.py` had always filtered by `SUPPORTED_EXT`.
        """
        from code_health import discovery as D
        guard = load_guard()
        self.assertEqual(set(D.SOURCE_EXT), set(guard.SUPPORTED_EXT))

    def test_both_use_the_same_default_thresholds(self):
        from code_health import config as C
        guard = load_guard()
        for key, value in C.DEFAULTS["thresholds"].items():
            if key in guard.DEFAULT_THRESHOLDS:
                self.assertEqual(guard.DEFAULT_THRESHOLDS[key], value, key)

    def test_both_agree_on_threshold_comparison(self):
        from code_health import config as C
        guard = load_guard()
        threshold = {"warn": 400, "hard": 800}
        for value in (399, 400, 799, 800, 801):
            self.assertEqual(
                C.level_for(value, threshold) is not None,
                guard.exceeds(value, 400),
                value)


class ExcludePatternAgreementTests(unittest.TestCase):
    """`guard.py` used to match excludes with `fnmatch`, which
    `references/tools.md` states as a standing rule this plugin does not use.
    It disagreed with `discovery.compile_pattern` on six of the thirteen
    patterns below, in both directions -- so one `exclude` list meant one
    thing to the audit and another to the ratchet, in the file most likely
    to be copied out of someone's `.gitignore`."""

    # Thirteen patterns a real `.code-health/config.json` plausibly holds.
    PATTERNS = [
        "**/node_modules/**",   # the plugin's own default
        "**/dist/**",
        "vendor",               # bare directory, straight out of .gitignore
        "build/",               # trailing slash, likewise
        "/dist/**",             # anchored at the repository root
        "*.min.js",
        "**/test_*.py",
        "src/**/*.ts",
        "docs",
        "**/__pycache__/**",
        "app/legacy/old.py",    # one exact path
        "*.lock",
        "**/.worktrees/**",
    ]
    PATHS = [
        "node_modules/x/y.js", "app/node_modules/x/y.js",
        "dist/a.js", "packages/web/dist/a.js", "distribution/a.js",
        "vendor/big.py", "vendor", "app/vendor/lib/x.go",
        "build/out/main.js", "build",
        "app/bundle.min.js", "pkg/test_thing.py", "pkg/test_sub/thing.py",
        "src/deep/nested/a.ts", "src/a.ts", "docs/guide/index.md",
        "app/__pycache__/x.pyc", "app/legacy/old.py", "app/legacy/new.py",
        "poetry.lock", ".worktrees/wt/app/main.py", "app/main.py",
    ]

    def test_both_matchers_agree_on_every_pattern_and_path(self):
        from code_health import discovery as D
        guard = load_guard()
        for pattern in self.PATTERNS:
            mine = D.compile_pattern(pattern)
            theirs = guard.compile_excludes([pattern])
            for path in self.PATHS:
                self.assertEqual(
                    bool(mine.match(path)), guard.excluded(path, theirs),
                    "%s vs %s" % (pattern, path))

    def test_a_bare_directory_pattern_excludes_what_is_inside_it(self):
        """The case that made the two disagree in the first place. A user
        copying `vendor` or `build/` out of `.gitignore` got nothing
        excluded from the audit and everything from the ratchet: the
        measurer's regex matched the directory name only, which never
        equals a file path."""
        from code_health import discovery as D
        guard = load_guard()
        for pattern, path in (("vendor", "vendor/big.py"),
                              ("vendor", "app/vendor/lib/x.go"),
                              ("build/", "build/out/main.js"),
                              ("docs", "docs/guide/index.md")):
            self.assertTrue(D.compile_pattern(pattern).match(path),
                            "%s vs %s" % (pattern, path))
            self.assertTrue(guard.excluded(path, guard.compile_excludes([pattern])),
                            "%s vs %s" % (pattern, path))

    def test_a_similar_prefix_is_still_not_a_match(self):
        from code_health import discovery as D
        guard = load_guard()
        for pattern, path in (("**/dist/**", "distribution/a.js"),
                              ("vendor", "vendors/big.py"),
                              ("/dist/**", "web/dist/a.js")):
            self.assertFalse(D.compile_pattern(pattern).match(path),
                             "%s vs %s" % (pattern, path))
            self.assertFalse(guard.excluded(path, guard.compile_excludes([pattern])),
                             "%s vs %s" % (pattern, path))


class NestingAgreementTests(unittest.TestCase):
    """guard.py duplicates the nesting-depth AST pass for the same reason it
    duplicates the CSV parser: it cannot import code_health. This is the
    corresponding cross-check for that duplication."""

    def assert_agree(self, name, source, expected):
        """Both implementations agree AND both get the pinned number right.

        Agreement alone is not enough: `guard.py` and `pyast.py` are two
        copies of one algorithm, so they can be wrong together.
        """
        root = Path(tempfile.mkdtemp())
        (root / name).write_text(source, encoding="utf-8")
        guard = load_guard()
        theirs = guard._python_nesting(source)
        mine, errors = PA.measure_nesting(root, [name])
        self.assertEqual(errors, [])
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["nesting"], expected)
        self.assertEqual(list(theirs.values()), [mine[0]["nesting"]])

    def test_depth_reached_inside_a_plain_else_counts(self):
        """Proved by mutation that this was a blind spot: deleting the
        `elif orelse:` arm of `_if_depth` -- the one that handles a plain
        `else:` at the end of an `if`/`elif` chain -- left every existing
        nesting test green, because all of them reach their depth through
        the `if` BODY and never through an `else`."""
        self.assert_agree("plain_else.py", (
            "def f(xs):\n"
            "    if xs:\n"
            "        return 1\n"
            "    elif len(xs) == 2:\n"
            "        return 2\n"
            "    else:\n"
            "        for x in xs:\n"
            "            while x:\n"
            "                return 3\n"
            "    return 0\n"
        ), 3)     # if/elif/else chain -> for -> while

    def test_depth_reached_inside_a_finally_counts(self):
        """Same blind spot for `Try.finalbody`: dropping it from
        `_stmt_bodies` left all existing tests green."""
        self.assert_agree("finally.py", (
            "def g(xs):\n"
            "    try:\n"
            "        return 1\n"
            "    except ValueError:\n"
            "        return 2\n"
            "    finally:\n"
            "        for x in xs:\n"
            "            if x:\n"
            "                pass\n"
        ), 3)     # try/finally -> for -> if

    def test_depth_reached_inside_a_loop_else_counts(self):
        """And for the `orelse` of a `for`/`while` -- the `else:` clause
        that runs when the loop was not broken out of."""
        self.assert_agree("loop_else.py", (
            "def h(xs):\n"
            "    for x in xs:\n"
            "        pass\n"
            "    else:\n"
            "        while xs:\n"
            "            with open('a') as fh:\n"
            "                pass\n"
        ), 3)     # for/else -> while -> with

    def test_guard_and_pyast_agree_on_a_flat_python_fixture(self):
        source = "def flat(x):\n" + "".join(
            "    if x == %d:\n        return %d\n" % (i, i) for i in range(5))
        root = Path(tempfile.mkdtemp())
        (root / "flat.py").write_text(source, encoding="utf-8")

        guard = load_guard()
        theirs = guard._python_nesting(source)
        mine, errors = PA.measure_nesting(root, ["flat.py"])

        self.assertEqual(errors, [])
        self.assertEqual(len(mine), 1)
        self.assertEqual(list(theirs.values()), [mine[0]["nesting"]])
        # Pin the actual number too: an agreement test that both sides get
        # wrong the same way would still pass without this.
        self.assertEqual(mine[0]["nesting"], 1)

    def test_guard_and_pyast_agree_on_a_genuinely_nested_python_fixture(self):
        source = (
            "def deep(x):\n"
            "    if x == 1:\n"
            "        if x == 2:\n"
            "            if x == 3:\n"
            "                return 3\n"
        )
        root = Path(tempfile.mkdtemp())
        (root / "deep.py").write_text(source, encoding="utf-8")

        guard = load_guard()
        theirs = guard._python_nesting(source)
        mine, errors = PA.measure_nesting(root, ["deep.py"])

        self.assertEqual(errors, [])
        self.assertEqual(list(theirs.values()), [mine[0]["nesting"]])
        self.assertEqual(mine[0]["nesting"], 3)

    def test_guard_and_pyast_agree_on_non_if_constructs(self):
        """Both fixtures above reach their depth entirely through `if`
        chains. Proved by mutation that this leaves a real gap: breaking
        guard.py's `for`/`while`/`try` branch of `_max_depth` (the
        `depth + 1` in the non-`If` arm of the loop) left both of them
        green, because neither one ever exercises that branch. This
        fixture reaches its depth only through
        `for` -> `while` -> `try`/`except` -> `with` (and, when the running
        interpreter supports it, one more level through `match`/`case`),
        with the expected depth pinned as its own assertion -- so this
        test fails on either a disagreement between the two
        implementations OR an absolute value both get wrong the same way.
        """
        base = (
            "def f(xs):\n"
            "    for x in xs:\n"
            "        while x:\n"
            "            try:\n"
            "                with open('a') as fh:\n"
        )
        if sys.version_info >= (3, 10):
            base += (
                "                    match x:\n"
                "                        case 1:\n"
                "                            return 1\n"
                "                        case _:\n"
                "                            return 0\n"
            )
            expected = 5     # for -> while -> try -> with -> match/case
        else:
            base += "                    return 1\n"
            expected = 4     # for -> while -> try -> with
        source = base + (
            "            except ValueError:\n"
            "                pass\n"
            "    return 0\n"
        )

        root = Path(tempfile.mkdtemp())
        (root / "mixed.py").write_text(source, encoding="utf-8")

        guard = load_guard()
        theirs = guard._python_nesting(source)
        mine, errors = PA.measure_nesting(root, ["mixed.py"])

        self.assertEqual(errors, [])
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["nesting"], expected)
        self.assertEqual(list(theirs.values()), [mine[0]["nesting"]])


if __name__ == "__main__":
    unittest.main()


# Every shape the two comment readers must agree on: own-line, inline and
# merged `#` comments, docstrings, a key in code, in a link and under a TODO,
# `//` and `/* */` own-line and inline, an unterminated block at EOF.
TICKET_REF_SOURCES = [
    ("a.py", '"""Module (ABC-1)."""\n'
             '# one ABC-2\n# two\n# three SEG-07\n'
             'KEY = "ABC-3"  # see #12\n'
             'x = 1  # https://t.example/ABC-4\n'
             '# TODO(ABC-5): later\n'
             'def f():\n    """Load.\n\n    Ordering (ABC-6).\n    """\n'),
    ("a.ts", '// ABC-1 own line\n'
             'const k = "ABC-2"; // inline WEB-3\n'
             '/*\n * Totals (WEB-4).\n * more\n */\n'
             'let y = 1; /* fixes #9 */\n'
             'const re = /[A-Z0-9]+/; // keys look like [A-Z0-9]+-1\n'
             '/* unterminated WEB-5'),
    ("a.py", "x = = 1  # ABC-1\n"),
    ("a.rb", "# ABC-1\n"),
]


class TicketRefAgreementTests(unittest.TestCase):
    """guard.py's comment reader is a copy of comments.py's, for the same
    reason the nesting pass is; the ratchet and the audit must count the
    same lines."""

    def test_both_count_the_same_lines(self):
        from code_health import comments as CM
        guard = load_guard()
        for name, text in TICKET_REF_SOURCES:
            ext = Path(name).suffix
            got = CM.classify_file(text, ext)
            mine = None if got is None else got["evidence"]["ticket_ref"]["count"]
            with self.subTest(source=text[:30]):
                self.assertEqual(mine, guard.count_ticket_refs(text, ext))

    def test_the_fixture_exercises_both_readers(self):
        guard = load_guard()
        self.assertEqual(guard.count_ticket_refs(TICKET_REF_SOURCES[0][1], ".py"), 5)
        self.assertEqual(guard.count_ticket_refs(TICKET_REF_SOURCES[1][1], ".ts"), 5)

    def test_both_use_the_same_vocabulary(self):
        from code_health import comments as CM
        guard = load_guard()
        self.assertEqual(CM.NOT_A_TRACKER, guard.NOT_A_TRACKER)
        self.assertEqual(set(CM.BRACE_EXT), set(guard.BRACE_EXT))
        for name in ("_TICKET_KEY_RE", "_ISSUE_NUMBER_RE", "_URL_RE", "_TASK_TAG_RE"):
            self.assertEqual(getattr(CM, name).pattern, getattr(guard, name).pattern)
