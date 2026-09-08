import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
from code_health import config as C  # noqa: E402
from code_health import discovery as D  # noqa: E402
from code_health import pyast as PA  # noqa: E402


class ModuleNameTests(unittest.TestCase):
    def test_plain_module(self):
        self.assertEqual(PA.module_name("pkg/sub/mod.py"), "pkg.sub.mod")

    def test_package_init_collapses_to_the_package(self):
        self.assertEqual(PA.module_name("pkg/__init__.py"), "pkg")


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def write(self, rel, body):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return rel

    def test_absolute_import_inside_the_repo_is_an_edge(self):
        self.write("pkg/__init__.py", "")
        a = self.write("pkg/a.py", "import pkg.b\n")
        b = self.write("pkg/b.py", "x = 1\n")
        out = PA.analyze(self.root, [a, b, "pkg/__init__.py"])
        self.assertIn("pkg.b", out["graph"]["pkg.a"])

    def test_relative_import_resolves_against_the_package(self):
        self.write("pkg/__init__.py", "")
        a = self.write("pkg/a.py", "from .b import thing\n")
        b = self.write("pkg/b.py", "thing = 1\n")
        out = PA.analyze(self.root, [a, b, "pkg/__init__.py"])
        self.assertIn("pkg.b", out["graph"]["pkg.a"])

    def test_relative_import_from_a_package_init(self):
        self.write("pkg/__init__.py", "from .b import thing\n")
        b = self.write("pkg/b.py", "thing = 1\n")
        out = PA.analyze(self.root, ["pkg/__init__.py", b])
        self.assertIn("pkg.b", out["graph"]["pkg"])

    def test_import_outside_the_repo_is_counted_not_dropped(self):
        a = self.write("a.py", "import json\nimport requests\n")
        out = PA.analyze(self.root, [a])
        self.assertEqual(out["graph"]["a"], [])
        self.assertEqual(out["unresolved"], 2)

    def test_syntax_error_is_reported_not_fatal(self):
        a = self.write("bad.py", "def (:\n")
        good = self.write("good.py", "x = 1\n")
        out = PA.analyze(self.root, [a, good])
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("good", out["graph"])

    def test_parameter_name_tuples_are_emitted(self):
        a = self.write("a.py",
                       "def build(store, key, ttl, user, scope, fmt, tz, locale):\n"
                       "    return 1\n")
        out = PA.analyze(self.root, [a])
        entry = [p for p in out["params"] if p["function"] == "build"][0]
        self.assertEqual(entry["names"],
                         ["store", "key", "ttl", "user", "scope", "fmt", "tz", "locale"])

    def test_symbols_cover_functions_and_classes(self):
        a = self.write("a.py", "class Report:\n    pass\n\ndef build():\n    return 1\n")
        out = PA.analyze(self.root, [a])
        kinds = {(s["name"], s["kind"]) for s in out["symbols"]}
        self.assertIn(("Report", "class"), kinds)
        self.assertIn(("build", "function"), kinds)


class CycleTests(unittest.TestCase):
    def test_three_node_cycle_is_found(self):
        graph = {"a": ["b"], "b": ["c"], "c": ["a"], "d": []}
        cycles = PA.find_cycles(graph)
        self.assertEqual(cycles, [["a", "b", "c"]])

    def test_acyclic_graph_has_no_cycles(self):
        self.assertEqual(PA.find_cycles({"a": ["b"], "b": ["c"], "c": []}), [])

    def test_self_import_is_a_cycle(self):
        self.assertEqual(PA.find_cycles({"a": ["a"]}), [["a"]])


class LayerTests(unittest.TestCase):
    def test_repo_without_python_reports_no_languages_measured(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.md").write_text("text\n", encoding="utf-8")
        cfg = C.load_config(root)
        inv = D.enumerate_files(root, cfg)
        layer = PA.measure_imports(root, inv, cfg)
        self.assertEqual(layer["languages_measured"], [])
        self.assertEqual(layer["status"], "not_measured")

    def test_degree_reports_fan_in_and_fan_out(self):
        root = Path(tempfile.mkdtemp())
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "a.py").write_text("import pkg.b\n", encoding="utf-8")
        (root / "pkg" / "b.py").write_text("x = 1\n", encoding="utf-8")
        cfg = C.load_config(root)
        inv = D.enumerate_files(root, cfg)
        layer = PA.measure_imports(root, inv, cfg)
        degree = {row["module"]: row for row in layer["degree"]}
        self.assertEqual(degree["pkg.a"]["fan_out"], 1)
        self.assertEqual(degree["pkg.b"]["fan_in"], 1)


if __name__ == "__main__":
    unittest.main()
