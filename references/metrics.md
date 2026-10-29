# Metrics catalogue

Every metric this plugin measures: what it means, which layer and tool produces it, the default
`agent`-profile thresholds, and why it costs a model specifically rather than only a human reader.
Definitions and the invocation traps behind each tool live in `<plugin-root>/references/tools.md`;
this file is the metric-by-metric reference the three skills point at.

## Comparison semantics

A threshold fires at `value >= threshold`, defined once here and never re-derived elsewhere.
`parameters.hard = 8` means "8 parameters is already a violation" — this is what produces the
established figure of 114 functions with ≥8 parameters in the reference repository. Sonar's own
rule parameters fire at `value > max` instead, so `<plugin-root>/references/sonar.md` configures
Sonar with `hard - 1` to reconcile the one-line off-by-one in exactly one place.

## `config.thresholds` metrics

| Metric | key | warn | hard | Layer / tool | What it means | How it hurts an agent |
|---|---|---|---|---|---|---|
| Lines in a file | `file_lines` | 400 | 800 | L2, direct file read | Raw line count of one file | A file that does not fit a model's working context gets read in fragments, or not at all; the agent edits from a partial picture of the file it is changing. |
| Tokens in a file | `file_tokens` | 8000 | 15000 | L2, direct file read + `chars_per_token` | Estimated LLM token cost of a file | This is the metric line count is a proxy for. A file can be short in lines and still expensive (long lines, dense syntax) or long in lines and still cheap; `file_tokens` is what actually decides whether the file fits a context budget. |
| Lines in a function | `function_lines` | 60 | 100 | L2 `lizard -ENS --csv` | Lizard's own `length` field per function | A function longer than a screenful forces the agent to hold more state in its own attention while reasoning about one unit of behavior. |
| Cyclomatic complexity | `function_ccn` | 15 | 25 | L2 `lizard -ENS --csv` | McCabe cyclomatic complexity: count of independent linear paths through a function | More branches to trace means more paths the agent must consider correct simultaneously when changing the function; a wrong guess about one branch is a silent regression in another. |
| Cognitive complexity | `function_cognitive` | 15 | 25 | L1 Sonar only (`S3776`) | Sonar's own measure of how hard a human finds a function to read, distinct from cyclomatic complexity (nesting and boolean-operator chains are weighted, straight-line branching is not) | Not measured without Sonar — CCN is reported instead, and labelled as CCN, never silently substituted for cognitive complexity under the same name. |
| Parameters | `parameters` | 5 | 8 | L2 `lizard -ENS --csv` | Count of a function's declared parameters | A high parameter count is usually one implicit "context object" spread across positional arguments; the agent has to reconstruct that shape from every call site instead of reading it in one place. |
| Nesting depth | `nesting` | 3 | 4 | L2, `lizard -ENS --csv` for a verified-correct language, own `ast` pass (`pyast.measure_nesting`) for Python, `None` ("not measured") otherwise | The deepest chain of nested blocks (`if`/`elif`/`else`, `for`, `while`, `with`, `try`/`except`/`finally`, `match`/`case`) enclosing a statement in a function | Deep nesting means the condition under which a given line executes is the conjunction of everything above it; the agent has to hold the whole stack of conditions in mind to reason about one line. |

### CCN versus cognitive complexity

These are two different metrics on two different scales, reported under their own names and never
conflated. `lizard` reports *cyclomatic* complexity (CCN): the number of independent paths through
a function's control-flow graph. *Cognitive* complexity (Sonar's `S3776`) exists only in L1: it
weights nesting and boolean-operator chains more heavily than straight-line branching, on the theory
that those cost a human reader more than an equivalent flat `if`/`elif` chain does. Without Sonar,
L2 never claims to report cognitive complexity — the scorecard says `not measured` for that row and
reports CCN separately, labelled as CCN.

### Nesting is not a single-source metric

`lizard`'s own nesting reader is verified correct only for brace/semicolon languages (C, C++, C#,
Java, JavaScript/TypeScript and their variants, Go, Kotlin, Rust, Swift, Scala, PHP, Objective-C) —
its reset logic keys on a closing brace, which those readers all emit and Python's indentation-based
reader and Ruby's/Lua's `end`-keyword readers never do. For those, lizard's column is not a
per-function measurement at all: verified directly, a flat 5-sequential-`if` Python function reports
nesting 5 (should be 1), and the counter does not reset between functions or files within one
invocation either — three copies of one function in a single `lizard` call report 5, 10, 15. Python
nesting is therefore computed by this plugin's own `ast` pass instead (`pyast.measure_nesting`,
joined to lizard's row by file + start line), and every other unverified language reports `nesting:
None` rather than a number nobody checked. The full reproduction and the per-language verification
method are in `references/tools.md`.

### Token estimate is an estimate

`tokens = ceil(chars / chars_per_token)` over the file's UTF-8 text, `chars_per_token` default `4`.
This is labelled an estimate everywhere it appears; it is **not** a tokenizer. Calibration check on
the reference repository: 8974 lines ≈ 107k tokens ⇒ ≈47.7 chars/line, consistent with Python
source. `lizard`'s own `token` column is a *lexical* token count on a different scale, reported only
per function, and is never fed into `file_tokens` — the two numbers are not interchangeable. The
only knob is the numeric `chars_per_token`; there is no mode switch, because a mode that changed the
unit would silently invalidate every threshold in this table.

## Finding kind → `--layers` value

`measure.py --layers` takes **layer names**, not the `layer` field a ledger group carries. A group
records `"layer": "L2"`, and `measure.py --layers L2` exits 2 with a usage error — so re-measuring
one group's dimension (`fix` step 3.1) needs this table, which is the only place the mapping is
written down.

| Finding kind | `--layers` value |
|---|---|
| `file_too_long`, `file_too_expensive` | `size` |
| `function_too_long`, `function_too_complex`, `too_many_params`, `too_deep_nesting` | `size` |
| `near_duplicate`, `exact_clone` | `dup` |
| `import_cycle` | `imports` |
| `dead_symbol` | `dead` |
| `comment_noise` | `comments` |
| `hotspot`, `symbol_collision`, `edit_fanout` | `derived` |

Pass the values for every kind in the group, comma-separated. Dependencies resolve transitively —
`--layers dup` pulls in `size`, `--layers derived` pulls in `size`, `churn` and `imports` — so
naming the kind's own layer is always enough. `--files` does **not** resolve transitively: it is an
exact membership filter, so the file list must already name everything the group covers.

## Metrics outside `config.thresholds`

These are not in the threshold table because they are not single-value/threshold metrics — they
are clusters, cycles, or rankings — but they are full plugin metrics with their own findings and
configuration.

| Metric | Layer / tool | What it means | How it hurts an agent |
|---|---|---|---|
| `near_duplicate` | L2, own MinHash/LSH pass (no external tool); config: `near_duplicate.min_similarity` (default 0.60), `near_duplicate.min_tokens` (default 40) | A cluster of functions that are not token-identical (which `jscpd`/CPD would catch) but share almost all their structure, typically with renamed identifiers or different string constants | The agent fixes a bug in one of N copies and ships a fix that silently does not apply to the other N-1; a duplicate the agent cannot see is a duplicate it cannot keep in sync. |
| `import_cycle` | L2, Python `ast` pass (exact) or `madge --json` for JS/TS | An import cycle: module A imports B which (directly or transitively) imports A again | A cycle means there is no safe order to read the modules in isolation — understanding either one requires already understanding the other — and it is exactly the shape that produces circular-import runtime errors when an agent reorganizes imports without noticing the cycle. |
| `dead_symbol` | L2, `vulture` (Python, `--min-confidence 80`); **JS/TS is `not_measured`** — knip's JSON output is not parsed (`<plugin-root>/references/tools.md`) | A function, class, or export with no detected reference elsewhere in the repository | Dead code is a false trail: the agent reads it, assumes it matters because it is in the codebase, and spends budget understanding something nothing calls. Both tools have known false positives on dynamic dispatch, so a finding here is evidence to inspect, not a certainty — grouped by owning module/package and capped at `P2` for that reason (`<plugin-root>/references/quality-policy.md`). |
| `comment_noise` | L2, own reader (no external tool): `tokenize` + `ast` for Python, an own brace-comment scanner for brace languages | A comment line counted into `noise_comment_lines` (a union of lines, not a sum): `commented_out` (Python only, code left behind in a comment), `trivial_doc` (a docstring or line comment that restates the identifier it sits on) or `ticket_ref` (a comment line carrying a bare tracker key — `ABC-123`, or `#123` after `issue`/`PR`/`fixes`/`see`/`задача`/`тикет`; not inside a URL, not on a `TODO`/`FIXME` line, not a standard such as `UTF-8`, `ISO-8601`, `SHA-256`, and not a prefix listed in config `ticket_ref_ignore`). Two classes are reported in `evidence` only and never counted: `short_inline` — measured precision 0.70, below the 0.85 floor a counted class must clear (this plugin's own rule, stated in `<plugin-root>/references/quality-policy.md`) — and `history_note`, a line telling what the code used to be (`раньше`, `теперь`, `переехал`, `used to`, `previously`, `moved out of`, `the old code`), whose fix is a rewrite rather than a deletion. `evidence.density` (`comment_lines`, non-blank `lines`, `ratio`) and the layer's `totals` are context for the report, never a score | Every line of a comment that repeats the code beside it is context the next agent pays for and learns nothing from — unlike a long function, it teaches nothing in exchange for the tokens it costs. A bare tracker key is the same cost with a dead end attached: it opens only for a reader with that tracker and the task's context, and the reason it stands for belongs in the comment itself or in the commit history. Precision: see "Where `comment_noise`'s precision figures come from" below — the literature's ~0.89/~0.90 is the algorithm's ancestry, not a measurement of this implementation. A `comment_noise` group is capped at `P2` for the same reason `dead_symbol` is (`<plugin-root>/references/quality-policy.md`). |
| `hotspot` | L3, derived from L2 size + churn | `commits_in_window × sum(ccn of functions in file)`, reported with both percentile ranks | Tornhill's product: complex code nobody touches is not urgent, but the same complexity in a file changed 40 times in the churn window is the highest-risk place in the repository for an agent to be asked to change something. Never its own group — it re-ranks other groups (`<plugin-root>/references/quality-policy.md`). |
| `symbol_collision` | L3, derived from the **Python `ast` pass only** (`imports.symbols`: classes and functions defined in `inventory.source`, tests excluded, minus a short convention-name list — `__init__`, `main`, `setUp`, `tearDown`, `__str__`, `__repr__`); config: `collision_min_files` (default 3) | A symbol name defined in `collision_min_files` or more distinct files, ranked by `count × avg_definition_tokens`. **Not measured for a repository with no Python**: `imports` reports `not_measured` there and `derived.symbol_collisions` comes back `null` with `derived.status: partial`, never an empty list. lizard's function rows are *not* a second source — they were never fed into this table | The agent greps a name, gets N hits across files, and has to disambiguate which definition is actually in scope for the call site it is editing — silently picking the wrong one is a correctness bug the agent will not notice it made. Never its own group. |
| `edit_fanout` | L3, derived from L2 churn | `mean(other files in commits touching f)` divided by the repository-wide **median** other-files-per-commit, falling back to the **mean** when that median is `0`; the denominator actually used is reported as `edit_fanout_basis` (`median` / `mean` / `unavailable`) — a ratio, not a raw count. A ratio that cannot be computed at all is `null`, never `0.0` | The raw co-change count is dominated by the repository's own commit-size habits rather than by the file; the ratio isolates the file. `> 1` means an edit here routinely drags in more files than a typical commit does — the file is coupled to more of the codebase than its own size suggests. Never its own group. |

`hotspot`, `symbol_collision`, and `edit_fanout` share one rule, stated once here and enforced by
`<plugin-root>/references/quality-policy.md`: they never form their own ledger group. They re-rank
the groups the size/duplication/import/dead-code metrics above already produced, and they appear as
evidence inside that group's `why`.

### Where `comment_noise`'s precision figures come from

Three numbers circulate for this metric and they answer three different questions. Quoting one for
another is the mistake this section exists to prevent.

**The literature's ~0.89 / ~0.90 is ancestry, not a measurement of what ships.** Steidl et al.
(ICPC 2013) report ~0.90 for coherence-based trivial-comment detection and ~0.89 for the
commented-out-code feature, the second corroborated by a 16-developer survey over 30 examples.
This implementation descends from those algorithms and does not reproduce them: the `any()` rule
was replaced by a majority rule, Steidl's `0.1` was rejected as untransferable (it was tuned as one
input feature to a trained classifier, never validated as a standalone decision rule), and the four
post-parse discriminators are this plugin's own. Cite the figures for where the method came from.

**This branch's own live-code result** — 15,318 source files in three real repositories, every
flagged line read at its own line with its surrounding code:

| Class | Flagged | Judged |
|---|---|---|
| `commented_out` | 11 lines | 11 true positives, 0 false. Two deprecated annotations with `@deprecated` notes, two commented-out call statements, one seven-line commented-out block. Each line individually inspected at its source context. |
| `trivial_doc` | 31 lines (28 records) | 13 unambiguous restatements; 14 section headers (below); 4 false positives, all scoring 0.55–0.75 — comments whose extra clause carries what the identifier does not. |

Eleven positives is a small numerator. It supports "no false positive was found in this sample",
not a confident percentage, and no percentage is quoted from it.

**`ticket_ref` and `history_note`, measured on 2026-10-29** over jira-team-analytics (1,673 source
files), xhh (13,351) and resume-profile-front (1,635), every sampled line read at its source:

| Class | Flagged lines | Random sample read | Judged |
|---|---|---|---|
| `ticket_ref` | 2,778 / 35 / 3 | 104 (60 / 40 / 4) | 102 tracker or plan references (`HH-…`, `PORTFOLIO-…`, plan and review ids such as `SEG-07`, `A1-5`). The 2 false were one shape: a regex character class `[A-Z0-9]` inside a minified, vendored `.yarn/releases/*.cjs`, read as the key `Z0-9`; a lookbehind now refuses it. |
| `history_note` | 529 / 16 / 2 | 56 (40 / 16) | 51 true. The 5 false: `Used to explain …` (utilisation, not history), `запечён старой версией` (a state, not a story), and three `теперь` describing a runtime sequence (`было выбрано «везде», а теперь выбрали …`). An earlier pattern set scored 13 false of 25 on xhh — comparative `раньше` (`чуть раньше попадания`), `ранее выбранной`, and plans written as `remove after migration` — and those shapes were cut. |

`history_note` clears the 0.85 floor in this sample and still stays evidence-only: a history line
often carries the reason the code is shaped as it is, so its fix is a rewrite into a why-line, not
a deletion, and a count of lines that a correct fix keeps would not fall when the debt is paid.

`tests/comment_history_sample.py` pins both classes against lines labelled by meaning before the
detector ran, known misses included, so its precision and recall assertions can fail; like the
corpus below, its score is a regression net and is never quoted as a precision.

**Known characteristic — the section header is `trivial_doc`'s dominant live shape.** 14 of the 31
flagged lines are a one- or two-word comment marking the start of a block of related declarations in
a types or DTO file: `// Filters` over `export interface FiltersState`, `// Sync` over
`export interface SyncError`, `# Education` over `class EducationLevelDTO`. Each scores 0.67 to
1.00, because a section header is by construction the name of the first thing under it. They
satisfy the detector's definition and are not a defect in it, but they are navigation for a human
reading top to bottom rather than documentation of one declaration. Read the live precision as
**27/31** under the detector's own definition and **13/31** if section headers count as worth
keeping; both readings belong in a report, and a `fix` session treats a section header as a
judgement call, never an automatic deletion.

**The corpus is a regression pin.** `tests/comment_corpus.py` scores 1.00 on both counted classes.
That number is never published as a precision: it was written alongside the detectors by their own
author, over fixtures the same author chose because the answer was already known.

**Why there is no `warn`/`hard`.** The same calibration measured the distribution: over 15,318
files, p50, p75, p90, p95 and p99 are all `0`, with a maximum of 7. `warn = p90 / hard = p99`
would set both to zero and flag every file carrying one noise line. A rare-event distribution has
no meaningful cut, so the metric lives in the "Metrics outside `config.thresholds`" table above
with `dead_symbol` — fixed `level: "warn"`, `threshold: null`, capped at `P2` — rather than
carrying an invented threshold.
