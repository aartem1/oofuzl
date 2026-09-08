# Quality prioritization policy

Use Sonar as evidence, not as the definition of good software. The purpose of the audit is to improve maintainability, correctness, security, and change safety without gaming metrics.

## Priority model

Assign priorities using impact, confidence, and remediation value together.

### P0 — urgent

Use only when there is strong evidence of a severe correctness/security/reliability problem with realistic impact, such as a blocker/high issue affecting a critical path or dangerous behavior. Do not promote ordinary maintainability findings to P0.

### P1 — fix soon

Use for high-confidence problems likely to cause defects, security exposure, difficult changes, or substantial maintenance cost. Typical examples:

- serious reliability/security findings;
- extremely complex central code;
- duplicated critical logic likely to diverge;
- a cluster of related issues pointing to one important design problem.

### P2 — worthwhile improvement

Use for genuine maintainability improvements with moderate risk/cost, such as localized complexity, repeated code, confusing control flow, or meaningful coverage gaps around noncritical code.

### P3 — optional

Use for low-impact cleanup, stylistic smells, marginal complexity, and findings whose expected benefit is small compared with change risk.

## Group by root cause

Do not present 37 near-identical issues as 37 independent tasks. Group them when one remediation can address them safely.

Good grouping dimensions:

- same Sonar rule and same subsystem;
- one function/class causing several related metrics;
- duplicated blocks representing one repeated concept;
- several downstream symptoms of one boundary/ownership problem.

Keep separate issues separate when combining them would create an artificial refactor scope.

## Interpret complexity carefully

High cognitive/cyclomatic complexity is a signal to inspect, not an instruction to split code mechanically.

Prefer, in order when applicable:

1. remove unnecessary branches/state;
2. make control flow linear with guards/early returns;
3. simplify data representation/transformation;
4. separate genuinely independent responsibilities;
5. extract cohesive named helpers only when they reduce conceptual load.

A lower metric is not an improvement if understanding now requires jumping through more files, layers, callbacks, or abstractions.

## Interpret duplication carefully

Duplication is most harmful when duplicated code represents the same concept and is likely to evolve together.

Do not recommend abstraction solely because two blocks look similar. Similar implementation for independent domain concepts can be safer than coupling them through a premature shared abstraction.

## Interpret coverage carefully

Coverage is a supporting signal, not proof of test quality.

Prioritize uncovered code when it is:

- high-risk or central behavior;
- complex branching/business logic;
- frequently changed;
- involved in a reliability/security finding.

Do not recommend chasing an arbitrary percentage by testing trivial getters, generated code, or implementation details.

## False positives and accepted debt

Never mark an issue false-positive or accepted during audit.

Flag a **false-positive candidate** only when repository evidence materially contradicts the finding. Give the user enough evidence to decide later.

Flag **intentional/accepted debt candidate** only when fixing it now would likely create more risk/cost than leaving it. Do not silently normalize debt.

Check the highest-severity findings individually before leading a report with them. Severity labels
are assigned by rule, not by consequence, and the most severe bucket is small enough to verify by
hand. A recurring shape worth knowing: rules that reason about a returned *expression* rather than
its *value* fire on functions whose several `return` statements name one accumulator variable
holding different contents on each path. Read the function before repeating the claim.

Also report what share of the total is noise. Generated or templated code — migration headers,
scaffolded boilerplate, schema clients — can contribute a large fraction of a repository's findings
from a single rule while representing no engineering decision at all. State that share explicitly
so the headline count is not mistaken for actionable work, and say where the exclusion belongs
(often the same place the project already excludes it from its own linters) instead of proposing to
"fix" it.

## Bounded model analysis

The agent may add at most three observations outside Sonar when they are high-confidence and high-value. Useful classes include:

- obvious overengineering or indirection not reflected by function-level metrics;
- misleading abstraction boundaries or dependency direction;
- tests that appear to assert implementation details rather than meaningful behavior;
- one architectural cause explaining many Sonar findings.

Do not turn the audit into an unconstrained subjective code review. If a claim cannot be supported by concrete repository evidence, omit it.

## Human-facing language

Translate scanner terminology into consequences.

Prefer:
- `Checkout pricing has one 78-point cognitive-complexity function; changes here are hard to reason about.`

Over:
- `S3776 violation: cognitive complexity 78 > 15.`

Keep the rule/key available in secondary detail so the user can select the finding for a fix session.
