"""sonar_ratings.py: every rating letter comes with the issues that set it
and what has to change for the next letter."""

import importlib.util
import unittest

from helpers import SCRIPTS

_spec = importlib.util.spec_from_file_location("sonar_ratings", SCRIPTS / "sonar_ratings.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

P = "github:o:r"


def issue(key, quality, severity, rule="python:S930", path="app/api.py", line=10):
    return {"key": key, "rule": rule, "component": "%s:%s" % (P, path), "line": line,
            "message": "m", "impacts": [{"softwareQuality": quality, "severity": severity}]}


class FakeApi:
    def __init__(self, measures, issues=(), tree=None):
        self.measures = measures
        self.issues = list(issues)
        self.tree = tree or {}

    def get(self, path, **params):
        if path == "/api/measures/component":
            return {"component": {"measures": [{"metric": k, "value": v}
                                               for k, v in self.measures.items()]}}
        if path == "/api/issues/search":
            # SonarQube answers 400 past the 10 000th result.
            assert params["p"] * params["ps"] <= 10000, "past the search limit"
            assert params["components"] == P and "componentKeys" not in params
            q = params["impactSoftwareQualities"]
            rows = [i for i in self.issues
                    if any(x["softwareQuality"] == q for x in i["impacts"])]
            start = (params["p"] - 1) * params["ps"]
            return {"issues": rows[start:start + params["ps"]], "paging": {"total": len(rows)}}
        if path == "/api/measures/component_tree":
            metric = params["metricKeys"]
            return {"components": [{"path": f, "measures": [{"metric": metric, "value": v}]}
                                   for f, v in self.tree.get(metric, [])]}
        raise AssertionError(path)


def rating(doc, name):
    return next(r for r in doc["ratings"] if r["rating"] == name)


class IssueRatingTests(unittest.TestCase):
    def test_e_is_explained_by_exactly_the_blocker_issues(self):
        api = FakeApi({"software_quality_reliability_rating": "5.0"}, [
            issue("b1", "RELIABILITY", "BLOCKER"),
            issue("b2", "RELIABILITY", "BLOCKER", rule="python:S5708", path="auto_pipeline.py"),
            issue("h1", "RELIABILITY", "HIGH"),
            issue("m1", "RELIABILITY", "MEDIUM"),
            issue("s1", "SECURITY", "BLOCKER"),
        ])
        r = rating(R.explain(api, P), "reliability")
        self.assertEqual(r["letter"], "E")
        self.assertEqual([i["key"] for i in r["driven_by"]["issues"]], ["b1", "b2"])
        self.assertEqual(r["driven_by"]["issues"][1]["file"], "auto_pipeline.py")
        self.assertEqual(r["next"]["letter"], "D")
        self.assertEqual(r["counts"]["HIGH"], 1)

    def test_fixing_the_worst_can_skip_letters(self):
        api = FakeApi({}, [issue("b1", "RELIABILITY", "BLOCKER"),
                           issue("l1", "RELIABILITY", "LOW")])
        self.assertEqual(rating(R.explain(api, P), "reliability")["next"]["letter"], "B")

    def test_no_issues_is_a_with_nothing_to_do(self):
        r = rating(R.explain(FakeApi({"software_quality_security_rating": "1.0"}), P), "security")
        self.assertEqual((r["letter"], r["driven_by"], r["next"]), ("A", None, None))

    def test_letter_disagreeing_with_open_issues_is_flagged(self):
        api = FakeApi({"software_quality_reliability_rating": "5.0"},
                      [issue("h1", "RELIABILITY", "HIGH")])
        self.assertIn("mismatch", rating(R.explain(api, P), "reliability"))

    def test_issues_are_paginated(self):
        many = [issue("k%d" % i, "RELIABILITY", "BLOCKER") for i in range(1203)]
        r = rating(R.explain(FakeApi({}, many), P), "reliability")
        self.assertEqual(len(r["driven_by"]["issues"]), 1203)

    def test_more_issues_than_search_returns_stops_at_the_limit(self):
        many = [issue("k%d" % i, "RELIABILITY", "BLOCKER") for i in range(10300)]
        r = rating(R.explain(FakeApi({}, many), P), "reliability")
        self.assertEqual(len(r["driven_by"]["issues"]), 10000)


class DebtAndReviewTests(unittest.TestCase):
    def test_maintainability_gives_debt_to_remove_for_next_letter(self):
        api = FakeApi({"software_quality_maintainability_debt_ratio": "12.0",
                       "software_quality_maintainability_remediation_effort": "1200",
                       "development_cost": "10000"},
                      tree={"sqale_index": [("big.py", "400")]})
        r = rating(R.explain(api, P), "maintainability")
        self.assertEqual(r["computed_letter"], "C")
        self.assertEqual(r["next"]["letter"], "B")
        self.assertEqual(r["next"]["debt_to_remove_minutes"], 200)
        self.assertEqual(r["top_files"][0]["file"], "big.py")

    def test_maintainability_a_has_no_next(self):
        api = FakeApi({"sqale_debt_ratio": "1.0", "sqale_index": "10", "development_cost": "1000"})
        self.assertIsNone(rating(R.explain(api, P), "maintainability")["next"])

    def test_security_review_counts_hotspots_left_to_review(self):
        api = FakeApi({"security_hotspots": "10", "security_hotspots_reviewed": "40.0"})
        r = rating(R.explain(api, P), "security_review")
        self.assertEqual(r["computed_letter"], "D")
        self.assertEqual((r["next"]["letter"], r["next"]["needs"]),
                         ("C", "проверить ещё 1 уязвимых мест"))

    def test_missing_coverage_says_report_was_not_fed(self):
        r = rating(R.explain(FakeApi({}), P), "coverage")
        self.assertIsNone(r["percent"])
        self.assertIn("не подавался", r["note"])

    def test_letter_from_numeric_rating(self):
        self.assertEqual([R.letter(v) for v in ("1.0", "3", "5.0", None)], ["A", "C", "E", None])


if __name__ == "__main__":
    unittest.main()
