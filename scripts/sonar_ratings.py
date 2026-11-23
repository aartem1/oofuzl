#!/usr/bin/env python3
"""Explain every Sonar rating of a project: what sets the letter, what moves it.

A rating letter alone is not actionable and can be wrong for a reason that
has nothing to do with the code (a rule tripping on a framework idiom, test
code analysed as source, a coverage report that matched nothing). This
script turns each rating into evidence:

- reliability / security: the letter is the worst severity among open
  issues of that software quality (SonarQube MQR: BLOCKER=E, HIGH=D,
  MEDIUM=C, LOW=B, none=A). It lists exactly those issues and the letter
  the project gets once they are gone (the next-worst severity present);
- maintainability: the letter is the technical-debt ratio (debt divided by
  development cost; A <=5 %, B <=10 %, C <=20 %, D <=50 %, E above). It
  gives the debt to remove for the next letter and the files carrying most;
- security review: share of reviewed hotspots (A >=80 %, B >=70 %,
  C >=50 %, D >=30 %) and how many more hotspots to review;
- coverage, duplication: no letter; the value and the files that move it.

Reads only. The token comes from SONAR_TOKEN or --token-file and is never
printed. Output: JSON on stdout.
"""

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LETTERS = "ABCDE"
SEVERITY_LETTER = {"BLOCKER": "E", "HIGH": "D", "MEDIUM": "C", "LOW": "B"}
SEVERITY_ORDER = ["BLOCKER", "HIGH", "MEDIUM", "LOW"]
DEBT_LIMITS = [("A", 0.05), ("B", 0.10), ("C", 0.20), ("D", 0.50)]
REVIEW_LIMITS = [("A", 80.0), ("B", 70.0), ("C", 50.0), ("D", 30.0)]
MEASURES = [
    "software_quality_reliability_rating", "reliability_rating",
    "software_quality_security_rating", "security_rating",
    "software_quality_maintainability_rating", "sqale_rating",
    "software_quality_maintainability_debt_ratio", "sqale_debt_ratio",
    "software_quality_maintainability_remediation_effort", "sqale_index",
    "development_cost", "security_review_rating", "security_hotspots",
    "security_hotspots_reviewed", "coverage", "duplicated_lines_density",
]
TOP_FILES = 5


class Api:
    def __init__(self, host, token, timeout=30):
        self.host = host.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get(self, path, **params):
        url = "%s%s?%s" % (self.host, path, urllib.parse.urlencode(params))
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError("Sonar API %s ответил %d" % (path, exc.code))
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError("Sonar API %s недоступен: %s" % (path, exc))


def letter(value):
    try:
        return LETTERS[int(round(float(value))) - 1]
    except (TypeError, ValueError, IndexError):
        return None


def _measures(api, project):
    body = api.get("/api/measures/component", component=project, metricKeys=",".join(MEASURES))
    return {m["metric"]: m.get("value") for m in body.get("component", {}).get("measures", [])}


def _first(measures, *keys):
    for key in keys:
        if measures.get(key) not in (None, ""):
            return measures[key]
    return None


SEARCH_LIMIT = 10000


def _search(api, project, quality, severity, page, size):
    return api.get("/api/issues/search", components=project, resolved="false",
                   impactSoftwareQualities=quality, impactSeverities=severity, ps=size, p=page)


def _total(body):
    return int(body.get("paging", {}).get("total", body.get("total", 0)) or 0)


def _counts(api, project, quality):
    # One cheap query per severity: the counts stay exact however many
    # issues the project has.
    counts = {}
    for severity in SEVERITY_ORDER:
        try:
            counts[severity] = _total(_search(api, project, quality, severity, 1, 1))
        except RuntimeError:
            # SonarQube 10.2-10.7 has no BLOCKER impact and answers 400.
            if severity != "BLOCKER":
                raise
            counts[severity] = 0
    return counts


def _issues(api, project, quality, severity):
    """The open issues of one severity. The search answers 400 past its
    10 000th result, so a larger set is cut there and marked truncated."""
    issues, page = [], 1
    while True:
        body = _search(api, project, quality, severity, page, 500)
        issues.extend(body.get("issues", []))
        total = _total(body)
        if page * 500 >= min(total, SEARCH_LIMIT) or not body.get("issues"):
            return issues
        page += 1


def _impact(issue, quality):
    for impact in issue.get("impacts", []):
        if impact.get("softwareQuality") == quality:
            return impact.get("severity")
    return None


def _locator(issue, project):
    component = issue.get("component", "")
    path = component[len(project) + 1:] if component.startswith(project + ":") else component
    return {"key": issue.get("key"), "rule": issue.get("rule"), "file": path,
            "line": issue.get("line"), "message": issue.get("message")}


def explain_issue_rating(name, quality, measured_letter, counts, worst_issues, project):
    present = [s for s in SEVERITY_ORDER if counts.get(s)]
    computed = SEVERITY_LETTER[present[0]] if present else "A"
    out = {"rating": name, "letter": measured_letter or computed, "computed_letter": computed,
           "counts": {s: counts.get(s, 0) for s in SEVERITY_ORDER}}
    if measured_letter and measured_letter != computed:
        out["mismatch"] = ("Sonar показывает %s, а открытые задачи дают %s: анализ устарел "
                           "или задачи изменились после него." % (measured_letter, computed))
    if not present:
        out["driven_by"] = None
        out["next"] = None
        return out
    worst = present[0]
    after = SEVERITY_LETTER[present[1]] if len(present) > 1 else "A"
    shown = [i for i in worst_issues if _impact(i, quality) == worst]
    out["driven_by"] = {"severity": worst, "issues": [_locator(i, project) for i in shown],
                        "truncated": len(shown) < counts[worst]}
    out["next"] = {"letter": after,
                   "needs": "закрыть все %d задач серьёзности %s" % (counts[worst], worst)}
    return out


def _issue_rating(api, project, name, quality, measured_letter):
    counts = _counts(api, project, quality)
    worst = next((s for s in SEVERITY_ORDER if counts[s]), None)
    issues = _issues(api, project, quality, worst) if worst else []
    return explain_issue_rating(name, quality, measured_letter, counts, issues, project)


def explain_debt(measures):
    ratio = _first(measures, "software_quality_maintainability_debt_ratio", "sqale_debt_ratio")
    debt = _first(measures, "software_quality_maintainability_remediation_effort", "sqale_index")
    cost = measures.get("development_cost")
    measured = letter(_first(measures, "software_quality_maintainability_rating", "sqale_rating"))
    if ratio is None:
        return {"rating": "maintainability", "letter": measured, "next": None}
    ratio = float(ratio) / 100.0
    computed = next((l for l, limit in DEBT_LIMITS if ratio <= limit), "E")
    out = {"rating": "maintainability", "letter": measured or computed, "computed_letter": computed,
           "debt_ratio_percent": round(ratio * 100, 2),
           "debt_minutes": int(float(debt)) if debt is not None else None}
    better = [(l, limit) for l, limit in DEBT_LIMITS if LETTERS.index(l) < LETTERS.index(computed)]
    if not better or debt is None or cost in (None, "", "0"):
        out["next"] = None
        return out
    target, limit = better[-1]
    remove = max(0, math.ceil(float(debt) - limit * float(cost)))
    out["next"] = {"letter": target, "needs": "убрать не меньше %d мин техдолга (доля ≤ %d %%)"
                   % (remove, int(limit * 100)), "debt_to_remove_minutes": remove}
    return out


def explain_review(measures):
    reviewed = measures.get("security_hotspots_reviewed")
    total = measures.get("security_hotspots")
    measured = letter(measures.get("security_review_rating"))
    if reviewed is None or total in (None, "", "0"):
        return {"rating": "security_review", "letter": measured or "A",
                "hotspots": int(float(total or 0)), "next": None}
    reviewed, total = float(reviewed), int(float(total))
    computed = next((l for l, limit in REVIEW_LIMITS if reviewed >= limit), "E")
    out = {"rating": "security_review", "letter": measured or computed, "computed_letter": computed,
           "reviewed_percent": reviewed, "hotspots": total}
    better = [(l, limit) for l, limit in REVIEW_LIMITS if LETTERS.index(l) < LETTERS.index(computed)]
    if not better:
        out["next"] = None
        return out
    target, limit = better[-1]
    done = round(reviewed * total / 100)
    more = max(0, math.ceil(limit * total / 100) - done)
    out["next"] = {"letter": target, "needs": "проверить ещё %d уязвимых мест" % more}
    return out


def _top_files(api, project, metric):
    body = api.get("/api/measures/component_tree", component=project, metricKeys=metric,
                   qualifiers="FIL", s="metric", metricSort=metric, asc="false",
                   ps=TOP_FILES, metricSortFilter="withMeasuresOnly")
    rows = []
    for c in body.get("components", []):
        value = next((m.get("value") for m in c.get("measures", []) if m["metric"] == metric), None)
        if value not in (None, "0", "0.0"):
            rows.append({"file": c.get("path") or c.get("key"), metric: value})
    return rows


def explain(api, project):
    measures = _measures(api, project)
    reliability = _issue_rating(
        api, project, "reliability", "RELIABILITY",
        letter(_first(measures, "software_quality_reliability_rating", "reliability_rating")))
    security = _issue_rating(
        api, project, "security", "SECURITY",
        letter(_first(measures, "software_quality_security_rating", "security_rating")))
    maintainability = explain_debt(measures)
    if maintainability.get("next"):
        maintainability["top_files"] = _top_files(api, project, "sqale_index")
    coverage = {"rating": "coverage", "percent": measures.get("coverage")}
    if coverage["percent"] is not None:
        coverage["least_covered"] = _top_files(api, project, "uncovered_lines")
    else:
        coverage["note"] = "отчёт покрытия не подавался или не сопоставился с файлами"
    duplication = {"rating": "duplication", "percent": measures.get("duplicated_lines_density"),
                   "top_files": _top_files(api, project, "duplicated_lines")}
    return {"project": project,
            "ratings": [reliability, security, maintainability, explain_review(measures),
                        coverage, duplication]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sonar_ratings.py")
    parser.add_argument("--host", default=os.environ.get("SONAR_HOST_URL", "http://localhost:9000"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--token-file")
    args = parser.parse_args(argv)
    token = os.environ.get("SONAR_TOKEN")
    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    if not token:
        sys.stderr.write("Нет токена: задайте SONAR_TOKEN или --token-file\n")
        return 2
    try:
        doc = explain(Api(args.host, token), args.project)
    except RuntimeError as exc:
        sys.stderr.write("%s\n" % exc)
        return 1
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
