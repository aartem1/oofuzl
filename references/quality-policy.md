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

## Agent-ergonomics metrics

Three L2/L3 metrics exist because they cost an *agent* specifically, not because a human style
guide asks for them. Report them under their own "Agent ergonomics" section, not folded into the
Sonar-shaped groups below.

- **Token cost** (`file_tokens`). `chars / chars_per_token` over a file's UTF-8 text — an estimate,
  never a tokenizer's own count. A file that fits a human's scroll wheel can still fail to fit in a
  model's context window; the largest file measured by hand was 8974 lines ≈ 107k tokens, invisible
  to every Sonar rule because none of `S138`/`S104`/`S134` is active in a built-in profile (see
  `references/sonar.md`).
- **Symbol collisions** (`symbol_collision`). The same function or class name defined in several
  files. A human reading one call site already knows which file they are in; an agent that greps a
  name and gets N hits has to disambiguate from scratch every time, and silently picking the wrong
  definition is a correctness bug the agent will not notice it made.
- **Churn × complexity** (`hotspot`) and **edit fan-out** (`edit_fanout`). Complexity in a file
  nobody has touched in six months is not urgent; the same complexity in a file changed 40 times is
  — an agent asked to change it is working in the most failure-prone part of the repository, not an
  arbitrary one. Fan-out is a ratio against the repository's own median commit size, not a raw
  count, because the raw count is dominated by the team's commit-size habits rather than by the
  file: editing a file whose edits routinely drag in more than a typical commit (`ratio > 1`) is
  itself a design smell an agent cannot fix by writing better code in isolation. Read
  `edit_fanout_basis` before quoting a ratio: the median is the intended denominator, but a
  repository whose typical commit touches one file has a median of `0`, and the measurer falls back
  to the **mean** rather than reporting a column of zeros. `median` and `mean` are not the same
  scale — a `ratio > 1` against the mean is a weaker signal than the same number against the
  median. `unavailable` means no ratio was computed and none should be quoted.

**These three never form their own ledger group.** Hotspots, fan-out, and symbol collisions
re-rank the groups above instead — a `Fix first` entry's priority moves because of them, and they
appear as evidence inside that group's `why`, never as a standalone "hotspot" or "fan-out" work
item with its own remediation.

## Dead code grouping

Group `dead_symbol` findings by their owning module or package, never repository-wide — "24
unreferenced helpers in `legacy/`" is one group, not one finding per helper and not one group for
the whole repository regardless of package boundaries. Both `vulture` and `knip` have known false
positives on dynamic dispatch, so a `dead_symbol` group is capped at **P2** even when the count is
large: a static-analysis "unreferenced" verdict is evidence to inspect, not proof of the kind a P0
or P1 verdict requires elsewhere.

## Comment noise grouping

Group `comment_noise` findings by subsystem/directory, never repository-wide, the same rule as
`dead_symbol` above.

**The 0.85 floor for a counted class is this plugin's own rule**, set here and nowhere else — it is
not inherited from Steidl or from any other source, and nothing outside this repository requires it.
A detected comment class is summed into `noise_comment_lines` only when its precision clears 0.85;
below that it is reported in `evidence` and never counted, because summing classes gives the whole
metric the precision of its weakest member. `short_inline`, at ~0.70, is the class this rule keeps
out. `ticket_ref` clears it on live code (102 of 104 sampled lines) and is counted. `history_note`
also cleared it in its sample (51 of 56) and is still kept out, for a different reason: a history
line often carries the reason the code is shaped as it is, so its fix is a rewrite and a count of
lines a correct fix keeps would not fall when the debt is paid. The floor is referred to wherever a counted class is justified, and always refers back to this
paragraph.

**Which precision figure to quote.** Three numbers exist and they are not interchangeable; the
distinction and the evidence behind each are in `<plugin-root>/references/metrics.md`, "Where
`comment_noise`'s precision figures come from". In short: the literature's ~0.89/~0.90 (Steidl et
al., ICPC 2013) is the **ancestry** of the algorithm, not a measurement of what ships — the decision
rule here is deliberately not Steidl's. This plugin's own live-code result over 15,318 files in
three repositories is 11 of 11 `commented_out` lines true (a small numerator, so no percentage is
quoted from it) and 31 `trivial_doc` lines of which 4 are false positives and 14 are section headers
over a type declaration. The corpus's 1.00 (`tests/comment_corpus.py`) is a regression pin written
alongside the detectors by their own author and must never be quoted as a measured precision.

A `comment_noise` group is capped at **P2** for the same reason `dead_symbol` is: this is evidence
to inspect, not proof. Two things the report states rather than leaving the reader to discover:

- **A section header is a judgement call, not an automatic deletion.** `// Filters` over
  `export interface FiltersState` scores 1.00 and is the dominant live shape of `trivial_doc`. It
  satisfies the detector's definition, but it is navigation for a human reading a types file top to
  bottom. Say so when a group is mostly this shape.
- **A tracker-key group is usually the largest and the cheapest.** In a repository whose comments
  cite tasks it can span half the files (951 of 1,673 on jira-team-analytics). Say it as one
  pattern — "2,778 comment lines carry a task key" — and recommend fixing it per directory, not as
  one repository-wide sweep, so each commit stays reviewable. The fix keeps the meaning and drops
  the key (`skills/fix/SKILL.md`, "The shape of change for a `comment_noise` group").
- **History notes are evidence, never their own group.** Name the count in the group's `why` when
  the same files carry one; a fix session rewrites them once the file is open.
- **Density is context.** A file whose comment lines outnumber its code is worth a look, but density
  is not a quality metric and never sets a priority.
- **What the detector cannot see.** A comment that paraphrases the code in different words
  (`// loop through items` over `for item in items:`) shares no lexical overlap with it and needs a
  language model to catch; this layer does not run one.

## The Sonar-less ceiling

A Sonar-less audit can **structurally never raise a P0**. The P0 tier is defined by
correctness/reliability/security evidence, and only L1 (Sonar) produces that evidence — L2/L3
measure size, duplication, import structure, dead code, and churn, none of which is itself a
correctness or security finding. When `L1: not_measured`, say so plainly in the report rather than
implying the repository has no P0: the true answer is "not measured", not "none found".

## Human-facing language

Translate scanner terminology into consequences.

Prefer:
- `Checkout pricing has one 78-point cognitive-complexity function; changes here are hard to reason about.`

Over:
- `S3776 violation: cognitive complexity 78 > 15.`

Keep the rule/key available in secondary detail so the user can select the finding for a fix session.
