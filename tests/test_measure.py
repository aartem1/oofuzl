"""The cross-layer status contract.

Every module in this package passes its own tests. The defects that survived
sixteen clean per-task reviews all lived in the seams BETWEEN them: a layer
assembled from another layer's rows, reporting `measured` because *its own*
tool ran, while the layer it consumed had nothing to give it. That is the one
thing this design forbids everywhere else -- a confident zero.

The contract, asserted directly below:

    no layer may report `status: "measured"` with no results at all while a
    layer it consumes is not `measured`.

The end-to-end test runs the real `measure.py` over a real git repository with
lizard forced absent, which is the cheapest way to make `size` not-measured
while `churn` and `imports` still are.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PLUGIN_ROOT, load_package

load_package()
from code_health import dup as DUP        # noqa: E402
from code_health import findings as F     # noqa: E402

MEASURE = PLUGIN_ROOT / "scripts" / "measure.py"

# Every result slot in the document, and the layers it is derived FROM.
# Per-slot rather than per-layer on purpose: `derived` bundles three metrics
# with three different dependencies, and a populated `edit_fanout` (which
# needs only churn) would otherwise mask an empty `hotspots` (which needs
# size) inside the same layer.
RESULT_DEPS = {
    "size": {"files": (), "functions": ()},
    "dup": {"near.clusters": ("size",), "exact": ()},
    "imports": {"cycles": (), "degree": ()},
    "dead": {"symbols": ()},
    "churn": {"files": ()},
    "derived": {"hotspots": ("size", "churn"),
                "symbol_collisions": ("imports",),
                "edit_fanout": ("churn",)},
    "comments": {"files": ()},
}


def _dig(layer, dotted):
    value = layer
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _git(root, *args):
    subprocess.run(["git", "-C", str(root)] + list(args),
                   check=True, capture_output=True)


def make_repo():
    root = Path(tempfile.mkdtemp())
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text(
        "from . import b\n\n\ndef alpha(x):\n    return b.beta(x)\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text(
        "def beta(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-qm", "init")
    return root


def make_js_repo():
    root = Path(tempfile.mkdtemp())
    (root / "a.js").write_text("const b = require('./b');\nmodule.exports = b;\n",
                               encoding="utf-8")
    (root / "b.js").write_text("module.exports = function beta(x) { return x; };\n",
                               encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-qm", "init")
    return root


def run_measure(root, *extra):
    env = dict(os.environ)
    env["CODE_HEALTH_LIZARD"] = "/nonexistent/lizard"
    proc = subprocess.run(
        [sys.executable, str(MEASURE), "--repo", str(root)] + list(extra),
        capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("measure.py exited %d: %s" % (proc.returncode, proc.stderr))
    return json.loads(proc.stdout)


class LayerStatusContractTests(unittest.TestCase):
    """One assertion, three defects. Before the honest-degradation fixes this
    failed on `derived`, which reported `measured` with `hotspots: []`
    because it received zero function rows from a `size` layer that had
    already declared itself `partial`."""

    @classmethod
    def setUpClass(cls):
        cls.doc = run_measure(make_repo())

    def assert_no_confident_zero(self, doc):
        layers = doc["layers"]
        for name, slots in RESULT_DEPS.items():
            layer = layers[name]
            for slot, deps in slots.items():
                broken = [d for d in deps if layers[d]["status"] != "measured"]
                if not broken or _dig(layer, slot):
                    continue
                where = "%s.%s" % (name, slot)
                because = ", ".join("%s is %s" % (d, layers[d]["status"]) for d in broken)
                self.assertIsNone(
                    _dig(layer, slot),
                    "%s is empty while %s -- 'not measured' must not look like zero"
                    % (where, because))
                self.assertNotEqual(
                    layer["status"], "measured",
                    "layer %r reports measured with nothing in %s while %s"
                    % (name, slot, because))

    def test_no_layer_claims_measured_on_an_unmeasured_dependency(self):
        self.assertEqual(self.doc["layers"]["size"]["status"], "partial",
                         "fixture is wrong: lizard was supposed to be absent")
        self.assert_no_confident_zero(self.doc)

    def test_every_layer_that_is_not_measured_says_why(self):
        for name, layer in self.doc["layers"].items():
            if layer["status"] != "measured":
                self.assertTrue(layer.get("reason"), name)

    def test_derived_declares_imports_as_a_dependency(self):
        """`measure_derived` reads `imports_layer["symbols"]`, so
        `--layers derived` without it silently produced no collisions --
        and `fix` step 3.1 runs exactly that, then skips any group whose
        findings are gone."""
        self.assertIn("imports", F.LAYER_DEPS["derived"])
        self.assertEqual(F.resolve_layers(["derived"]),
                         {"derived", "size", "churn", "imports"})


class JavaScriptOnlyRepoTests(LayerStatusContractTests):
    """The same contract where the *symbol table* is the missing dependency.

    A repository with no Python has no symbol table at all, so
    `symbol_collisions` must come back as "not measured" rather than as an
    empty list next to `derived.status: measured` -- and that holds whether or
    not madge is installed. With madge present the *import graph* is measured
    for JavaScript while the symbol table is still absent, which is exactly the
    case where an empty collision list would look like a clean result.
    """

    @classmethod
    def setUpClass(cls):
        cls.doc = run_measure(make_js_repo())

    def test_no_layer_claims_measured_on_an_unmeasured_dependency(self):
        layers = self.doc["layers"]
        self.assertIsNone(layers["derived"]["symbol_collisions"])
        self.assertFalse(layers["imports"].get("symbols"))
        self.assertNotIn("python", layers["imports"].get("languages_measured") or [])
        if layers["imports"]["status"] != "measured":
            self.assertTrue(layers["imports"].get("reason"))
        self.assert_no_confident_zero(self.doc)


class DupAssemblyTests(unittest.TestCase):
    """`dup` is assembled from two independent sources, so its status has to
    come from both. jscpd is usually absent on this machine, which would hide
    the interesting case behind a skip -- so the assembly is exercised
    directly with a jscpd result that DID run."""

    JSCPD_OK = {"status": "measured", "clones": []}

    def test_a_measured_jscpd_does_not_make_an_unmeasured_size_look_clean(self):
        layer = DUP.assemble({"status": "partial", "reason": "lizard failed"},
                             {"clusters": [], "compared": 0}, self.JSCPD_OK)
        self.assertNotEqual(layer["status"], "measured")
        self.assertIsNone(layer["near"])
        self.assertIn("size", layer["reason"])

    def test_both_sources_measured_reports_measured(self):
        layer = DUP.assemble({"status": "measured"},
                             {"clusters": [], "compared": 41}, self.JSCPD_OK)
        self.assertEqual(layer["status"], "measured")
        self.assertIsNone(layer["reason"])

    def test_an_absent_jscpd_never_reports_zero_exact_clones(self):
        layer = DUP.assemble({"status": "measured"},
                             {"clusters": [], "compared": 41},
                             {"status": "not_measured", "reason": "jscpd unavailable",
                              "clones": []})
        self.assertEqual(layer["status"], "partial")
        self.assertIsNone(layer["exact"])
        self.assertIn("jscpd", layer["reason"])


if __name__ == "__main__":
    unittest.main()
