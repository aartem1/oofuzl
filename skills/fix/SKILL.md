---
name: fix
description: Safely fix only user-selected SonarQube findings or audit priority groups. Revalidate the finding, understand its root cause in context, make the smallest semantically correct change, preserve behavior, run appropriate tests and checks, rerun Sonar, and reject score-gaming fixes.
compatibility: Requires a repository previously or currently analyzable by the code-health audit skill. Designed for Claude Code and Codex-compatible Agent Skills.
---

# Fix Selected Sonar Findings Safely

Use this skill only for findings or a priority group the user has explicitly chosen to fix, and start the implementation in a fresh session. If this same session already performed the audit, do not edit code: output the exact `/code-health:fix ...` handoff for a new session and stop.

If the command does not identify a finding/group and the current conversation does not contain an explicit user selection, ask the user to choose one; do not choose a large cleanup scope on their behalf.

## Non-negotiable rules

- Optimize for real code quality and preserved behavior, not a green Sonar score.
- Never use `NOSONAR`, ignore annotations/comments, exclusions, disabled rules, weaker quality profiles, higher thresholds, or test deletions as a shortcut. Treat suppression as a separate decision allowed only for a demonstrated false positive and explicit user approval.
- Do not refactor unrelated code merely because it is nearby.
- Do not reset, overwrite, stash, or discard pre-existing user changes.
- Do not commit or push unless the user separately asks.
- Prefer the simplest design that resolves the root cause. Do not turn one understandable function into unnecessary layers/helpers just to lower a metric.
- Preserve public behavior and contracts unless the selected issue inherently requires a behavior change and the intended behavior is clear.

## 1. Resolve and revalidate the selection

1. Resolve the current repository root and derive the same stable Sonar project key used by the audit.
2. If the selection is a short audit ID such as `A1`, load the latest local handoff from `${XDG_STATE_HOME:-$HOME/.local/state}/code-health/` for this project. Require the stored repository path/project key to match the current repository. Resolve `A1` to its stored issue/rule keys.
3. Treat handoff state only as a locator. Fetch current issue details and surrounding Sonar evidence before editing.
4. Record current branch, `HEAD`, working-tree state, and pre-existing changes.
5. If the stored/latest Sonar analysis does not correspond to the current source state, perform a fresh **read-only Sonar analysis inside this fix session before editing**. Do not invoke the audit skill and then continue from it; audit mode is intentionally terminal/read-only.
6. Read the affected code in context: callers, callees, tests, types/contracts, adjacent abstractions, and relevant project conventions.
7. Decide whether the selected finding reflects:
   - a real defect/risk;
   - a maintainability problem with a clear root cause;
   - a symptom of a broader local design problem;
   - a likely false positive.
8. If resolving it correctly requires a consequential product/architecture decision with multiple reasonable outcomes, present the smallest decision to the user instead of guessing. Otherwise proceed.

If a short audit ID cannot be resolved because local state is missing or belongs to another repository, do not guess. Ask for a direct Sonar issue key/group description or have the user run a fresh audit.

## 2. Establish a safety baseline

Before editing, establish the current behavior with repository-native checks.

- Run the narrowest existing tests that exercise the affected area.
- If the finding represents incorrect/reliability-sensitive behavior and the project has a suitable test harness, prefer a focused regression test that reproduces the problem before the implementation change. Confirm it fails for the expected reason, then keep it as protection after the fix.
- For a behavior-preserving maintainability refactor, do not invent a fake failing test. Use existing behavioral tests; add a focused characterization/regression test only when the affected behavior is important and insufficiently protected.
- Run typecheck/build before editing when interfaces, compilation boundaries, generated contracts, or build integration are relevant.
- Record pre-existing failures and do not attribute them to the fix later.

Do not introduce a new testing framework merely for this fix. If adequate automated tests are unavailable, inspect callers/contracts more carefully and state the reduced validation confidence in the final result.

## 3. Design the fix, then edit

Choose the remediation based on code semantics, not the rule text alone.

For complexity findings:
- first look for a simpler control flow, early returns, clearer data transformations, removal of unnecessary state, or separation of genuinely independent responsibilities;
- extract helpers only when they create meaningful names/cohesive units or reuse, not merely to move complexity elsewhere;
- avoid abstraction layers that increase navigation cost without reducing conceptual complexity.

For duplication:
- confirm the duplicated blocks represent the same concept and are expected to evolve together before extracting shared code;
- tolerate intentional duplication when the concepts are independent and premature unification would couple them incorrectly.

For reliability/security findings:
- trace inputs, outputs, error paths, state transitions, and callers;
- fix the root cause at the correct boundary rather than adding a downstream guard that hides it.

For maintainability smells:
- preserve local conventions unless those conventions are the problem;
- prefer reducing concepts and responsibilities over mechanically splitting files/functions.

Make one coherent change group at a time.

## 4. Validate behavior and quality

After each coherent group, escalate checks from narrow to broad according to risk and cost:

1. Run the focused/regression tests for the affected behavior.
2. Run the affected package/module/suite tests when available.
3. Run relevant typecheck, build, lint, static checks, or other repository-native checks.
4. For a high-risk or cross-cutting change, run the broader/full relevant test suite when reasonably bounded; do not claim full regression safety if it was not run.
5. Inspect the diff for accidental churn, changed contracts, duplicated work, dead code, and unnecessary abstractions.
6. Rerun Sonar against the resulting working tree.
7. Confirm:
   - the selected issue(s) are actually resolved or correctly reclassified;
   - no new higher-impact Sonar findings appeared;
   - relevant complexity/duplication/maintainability metrics did not regress elsewhere merely by moving the problem;
   - unrelated pre-existing failures remain clearly separated.

If the Sonar issue disappears but the implementation is less understandable or riskier, reject that implementation and choose a better one.

## 5. Stop at the right boundary

Do not continue into adjacent cleanup automatically. If the selected issue exposes another independent problem, mention it as a follow-up rather than expanding scope.

For a broad priority group, process issues sharing one root cause together; otherwise keep changes small enough to reason about and validate safely.

## Final human announcement

Always finish with a compact, structured announcement in the user's language. It must be understandable without reading the implementation log. Do not use a table.

Use exactly these sections when applicable:

**Исправлено** — one or two sentences naming the selected problem and the real root cause that was removed.

**Как исправлено** — 2–5 short bullets describing the design/implementation approach in plain language. Explain the method, not a file-by-file changelog.

**Как проверено** — short bullets naming the checks that actually ran: regression/targeted tests, broader tests when run, typecheck/build/lint or other repository-native checks, diff review, and Sonar before → after. Explicitly state whether the selected issue disappeared and whether new higher-impact findings appeared.

**Осталось** — only material residual risk, validation gaps, pre-existing failures, or intentionally deferred related problems. Omit this section when there is nothing meaningful to report.

Keep the announcement concise. Do not paste raw scanner output, long lists of files, or generic claims such as “everything is fine.” Say only what was actually verified.

If the finding should not be fixed because it is a false positive or because the proposed remediation would make the design worse, use the same compact format but state that no code change was made and explain the evidence. Do not manipulate Sonar configuration automatically.
