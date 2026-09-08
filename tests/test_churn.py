import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
from code_health import config as C  # noqa: E402
from code_health import churn as CH  # noqa: E402


def git(root, *args):
    subprocess.run(["git", "-C", str(root)] + list(args), check=True,
                   capture_output=True)


def make_repo():
    root = Path(tempfile.mkdtemp())
    git(root, "init", "-q", ".")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    return root


class ParseTests(unittest.TestCase):
    # Verified real `git log --numstat -z -M --format="%H%x09%aI%x09%aE"` output:
    # records are NUL-terminated, a rename is an EMPTY path field followed by
    # two separate path fields, and a binary file reports "-" for both counts.
    PAYLOAD = (
        "sha2\t2026-09-08T11:00:00+03:00\tb@example.com\0"
        "1\t0\t\0src/old/mod.py\0src/new/mod.py\0"
        "sha1\t2026-09-07T11:00:00+03:00\ta@example.com\0"
        "-\t-\tblob.bin\0"
        "3\t0\tsrc/old/mod.py\0"
    )

    def test_rename_is_folded_onto_the_new_path(self):
        commits, per_file = CH.parse_log(self.PAYLOAD)
        self.assertIn("src/new/mod.py", per_file)
        self.assertNotIn("src/old/mod.py", per_file)
        self.assertEqual(per_file["src/new/mod.py"]["commits"], 2)

    def test_brace_form_never_appears(self):
        _, per_file = CH.parse_log(self.PAYLOAD)
        self.assertFalse(any("{" in path for path in per_file))

    def test_binary_rows_do_not_crash_and_count_no_lines(self):
        _, per_file = CH.parse_log(self.PAYLOAD)
        self.assertEqual(per_file["blob.bin"]["lines_added"], 0)
        self.assertEqual(per_file["blob.bin"]["commits"], 1)

    def test_author_identity_is_captured(self):
        _, per_file = CH.parse_log(self.PAYLOAD)
        self.assertEqual(per_file["src/new/mod.py"]["authors"], 2)

    def test_last_change_is_the_newest_date(self):
        _, per_file = CH.parse_log(self.PAYLOAD)
        self.assertTrue(per_file["src/new/mod.py"]["last_change"].startswith("2026-09-08"))

    def test_commits_carry_their_path_sets(self):
        commits, _ = CH.parse_log(self.PAYLOAD)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["paths"], ["src/new/mod.py"])
        self.assertEqual(sorted(commits[1]["paths"]), ["blob.bin", "src/old/mod.py"])


class MeasureTests(unittest.TestCase):
    def test_no_git_directory_is_not_measured(self):
        root = Path(tempfile.mkdtemp())
        cfg = C.load_config(root)
        layer = CH.measure_churn(root, cfg, present_paths=set())
        self.assertEqual(layer["status"], "not_measured")
        self.assertIn("git", layer["reason"])

    def test_deleted_files_are_dropped(self):
        root = make_repo()
        (root / "gone.py").write_text("x = 1\n")
        (root / "kept.py").write_text("y = 1\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "one")
        (root / "gone.py").unlink()
        git(root, "add", "-A")
        git(root, "commit", "-qm", "two")
        cfg = C.load_config(root)
        layer = CH.measure_churn(root, cfg, present_paths={"kept.py"})
        paths = {row["path"] for row in layer["files"]}
        self.assertEqual(paths, {"kept.py"})

    def test_history_shorter_than_window_reports_actual_span(self):
        root = make_repo()
        (root / "a.py").write_text("x = 1\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "one")
        cfg = C.load_config(root)
        layer = CH.measure_churn(root, cfg, present_paths={"a.py"})
        self.assertEqual(layer["status"], "measured")
        self.assertEqual(layer["window_days"], 180)
        self.assertLessEqual(layer["history_span_days"], 1)


if __name__ == "__main__":
    unittest.main()
