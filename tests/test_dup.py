import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
from code_health import config as C  # noqa: E402
from code_health import dup as DUP  # noqa: E402


class TokenizerTests(unittest.TestCase):
    def test_cyrillic_identifiers_survive_intact(self):
        src = 'def считать_итог(данные):\n    x = "квартальный кэш"\n'
        tokens = DUP.tokenize(src)
        self.assertIn("считать_итог", tokens)
        self.assertIn("данные", tokens)

    def test_string_literal_is_one_token(self):
        tokens = DUP.tokenize('x = "квартальный кэш"\n')
        self.assertIn('"квартальный кэш"', tokens)

    def test_triple_quoted_string_is_one_token(self):
        tokens = DUP.tokenize('x = """a\nb"""\n')
        self.assertEqual(sum(1 for t in tokens if t.startswith('"""')), 1)

class StructuralTests(unittest.TestCase):
    def test_identifiers_become_ID_and_literals_become_LIT(self):
        tokens = DUP.tokenize('def foo(bar):\n    return "s" + 1\n')
        out = DUP.structural(tokens, "python")
        self.assertEqual(out.count("ID"), 2)          # foo, bar
        self.assertEqual(out.count("LIT"), 2)         # "s", 1
        self.assertIn("def", out)                     # keyword preserved
        self.assertIn("return", out)


class SimilarityTests(unittest.TestCase):
    def _sim(self, a, b, lang="python"):
        ta, tb = DUP.tokenize(a), DUP.tokenize(b)
        lex = DUP.jaccard(DUP.shingle_hashes(ta), DUP.shingle_hashes(tb))
        st = DUP.jaccard(DUP.shingle_hashes(DUP.structural(ta, lang)),
                         DUP.shingle_hashes(DUP.structural(tb, lang)))
        return lex, st

    BODY = (
        "def fetch_quarterly(store, key, ttl):\n"
        "    cached = store.get(key)\n"
        "    if cached is not None and not cached.expired(ttl):\n"
        "        return cached.value\n"
        "    value = compute(key)\n"
        "    store.put(key, value, ttl)\n"
        "    return value\n"
    )

    def test_identical_functions_score_one(self):
        lex, st = self._sim(self.BODY, self.BODY)
        self.assertEqual(lex, 1.0)
        self.assertEqual(st, 1.0)

    RENAMED = (BODY.replace("fetch_quarterly", "fetch_monthly")
                   .replace("store", "cache").replace("value", "payload"))

    def test_renamed_identifiers_score_high_structurally(self):
        lex, st = self._sim(self.BODY, self.RENAMED)
        # Measured: structural 1.00, lexical 0.32.
        self.assertGreaterEqual(st, 0.90)
        self.assertLess(lex, 0.40)

    def test_the_structural_branch_actually_accepts_that_pair(self):
        # Guards the exact bug the 0.40 floor introduced: a renamed copy scores
        # below it, so the branch meant to catch renames would never fire.
        lex, st = self._sim(self.BODY, self.RENAMED)
        accepted = lex >= 0.60 or (st >= DUP.STRUCTURAL_MIN and lex >= DUP.STRUCTURAL_LEX_FLOOR)
        self.assertTrue(accepted)

    def test_unrelated_pair_is_rejected_by_both_branches(self):
        other = ("def render(template, ctx):\n"
                 "    for name, node in template.nodes():\n"
                 "        ctx.emit(node.render(name))\n"
                 "    return ctx.finish()\n")
        lex, st = self._sim(self.BODY, other)
        accepted = lex >= 0.60 or (st >= DUP.STRUCTURAL_MIN and lex >= DUP.STRUCTURAL_LEX_FLOOR)
        self.assertFalse(accepted)

    def test_differing_only_in_string_constants_matches_structurally(self):
        a = self.BODY + '    log("quarterly")\n'
        b = self.BODY + '    log("monthly")\n'
        lex, st = self._sim(a, b)
        self.assertGreaterEqual(st, 0.85)

    def test_unrelated_functions_score_low(self):
        other = ("def render(template, ctx):\n"
                 "    for name, node in template.nodes():\n"
                 "        ctx.emit(node.render(name))\n"
                 "    return ctx.finish()\n")
        lex, st = self._sim(self.BODY, other)
        self.assertLess(lex, 0.60)


class LshTests(unittest.TestCase):
    def test_similar_signatures_become_candidates(self):
        base = DUP.shingle_hashes(DUP.tokenize(SimilarityTests.BODY))
        near = set(base)
        near.discard(next(iter(near)))
        sigs = {"a": DUP.signature(base), "b": DUP.signature(near)}
        self.assertIn(("a", "b"), DUP.lsh_pairs(sigs))

    def test_dissimilar_signatures_usually_are_not_candidates(self):
        sigs = {"a": DUP.signature(DUP.shingle_hashes(DUP.tokenize("a b c d e f g h"))),
                "b": DUP.signature(DUP.shingle_hashes(DUP.tokenize("z y x w v u t s")))}
        self.assertEqual(DUP.lsh_pairs(sigs), set())


class ClusterTests(unittest.TestCase):
    def test_transitive_pairs_form_one_cluster(self):
        pairs = [("a", "b"), ("b", "c"), ("d", "e")]
        clusters = sorted(sorted(c) for c in DUP.cluster_pairs(pairs))
        self.assertEqual(clusters, [["a", "b", "c"], ["d", "e"]])


class EndToEndTests(unittest.TestCase):
    def test_eight_wrappers_form_one_cluster_of_eight(self):
        root = Path(tempfile.mkdtemp())
        cfg = C.load_config(root)
        functions = []
        for index in range(8):
            body = (
                "def load_%s_report(store, key, ttl, user, scope, fmt, tz, locale):\n"
                "    cached = store.get(key)\n"
                "    if cached is not None and not cached.expired(ttl):\n"
                "        return cached.value\n"
                "    value = build_%s(key, user, scope, fmt, tz, locale)\n"
                "    store.put(key, value, ttl)\n"
                "    return value\n"
            ) % (("q%d" % index), ("q%d" % index))
            name = "mod%d.py" % index
            (root / name).write_text(body, encoding="utf-8")
            functions.append({
                "filename": name, "function_name": "load_q%d_report" % index,
                "start_line": 1, "end_line": 7, "token": 80,
            })
        payload = DUP.find_near_duplicates(root, functions, cfg)
        self.assertEqual(len(payload["clusters"]), 1)
        self.assertEqual(len(payload["clusters"][0]["members"]), 8)
        self.assertGreaterEqual(payload["clusters"][0]["min_lexical"], 0.60)

    def test_functions_below_min_tokens_are_ignored(self):
        root = Path(tempfile.mkdtemp())
        cfg = C.load_config(root)
        (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (root / "b.py").write_text("def g():\n    return 1\n", encoding="utf-8")
        functions = [
            {"filename": "a.py", "function_name": "f", "start_line": 1, "end_line": 2, "token": 7},
            {"filename": "b.py", "function_name": "g", "start_line": 1, "end_line": 2, "token": 7},
        ]
        payload = DUP.find_near_duplicates(root, functions, cfg)
        self.assertEqual(payload["clusters"], [])


if __name__ == "__main__":
    unittest.main()
