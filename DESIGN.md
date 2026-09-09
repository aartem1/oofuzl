# code-health — design

Status: draft for review. Supersedes `plugins/sonar-code-health` (v0.2.0).

## 1. Purpose

One tool for managing technical debt and for shaping a repository so an AI agent can work in it.
Three modes, three verbs, one shared state:

```
audit  →  measures, writes the debt ledger (groups A1…An)
fix    →  burns ledger entries, one or in a queue, with a project gate after each
guard  →  freezes the current state as a baseline and refuses commits that make it worse
```

The connecting idea is a **debt ledger** plus a **ratchet** over it. One measurer, three consumers.

## 2. Why Sonar alone is not enough

Verified on a real repository (`jira-team-analytics`, Python backend) and against a live
SonarQube 26.8, 2026-09:

- `S138` (function length), `S104` (file length), `S134` (nesting) exist and are `status=READY`
  with sane defaults (py: 100 / 1000 / 4), but they are **not active in any built-in profile** —
  neither `Sonar way` nor the newer `Sonar agentic AI` — and built-in profiles are `isBuiltIn=true`,
  read-only, and applied to every language by default.
- `python:S107` (parameter count) is active with `max=13`. `python:S3776` is active at `threshold=15`.
- Result: of 114 functions with ≥8 parameters, Sonar reported 7. Of 22 files ≥1000 lines
  (42.5 % of the backend, largest 8974 lines ≈ 107k tokens) it reported none.

`Sonar agentic AI` sounds like it should be the answer and is not: it activates none of
`S138`/`S104`/`S134` either. It is a different axis — rules about code an AI is likely to generate
badly — not about code an AI is likely to fail to *read*. This design forks from `Sonar way` for
that reason, stated so the choice is not mistaken for ignorance of the newer profile.

Forking a built-in profile is normal practice. This plugin **creates the profile** rather than
routing around it with scripts.

Four things Sonar cannot do at any setting, and which this plugin therefore owns:

1. **churn** — how often a file changes. Refactoring priority = complexity × churn
   (behavioural code analysis, Tornhill). Sonar keeps no history of this at all.
2. **near-duplicates** — CPD/jscpd match token-identical blocks. Eight wrappers at 64–75 % pairwise
   similarity were missed.
3. **import graph, cycles, fan-in/fan-out** — absent from Community Build.
4. **token cost of a file** — not a metric Sonar has as a concept.

## 3. Measurement layers

| Layer | Tools | Produces | Consumers |
|---|---|---|---|
| **L1 Sonar** | own quality profile (fork of `Sonar way` + S138/S104/S134/S107) | rules, history, quality gate, cognitive complexity, reliability/security findings, coverage, hotspots | `audit` only; **optional** — no Docker ⇒ layer marked `not_measured`, audit continues |
| **L2 local** | `lizard`, `jscpd`, `madge`, `vulture`, plain `git`, own Python `ast` pass, own near-duplicate pass, own comment reader (`tokenize`, `ast`, a brace-comment scanner; no external tool), direct file reads | size, token cost, CCN, parameters, nesting, exact clones, near-duplicates, import cycles, dead code, comment noise, churn | all three; **the only layer in `guard`** — a pre-commit hook must be fast |
| **L3 derived** | own code over L2 | churn × complexity hotspots, symbol-name collisions, edit fan-out | `audit` |

Every layer is always present in the output with an explicit
`status: measured | partial | not_measured` and, when not fully measured, a `reason`. A layer is
never silently dropped.

**Honest metric mapping.** `lizard` reports *cyclomatic* complexity (CCN). *Cognitive* complexity
(S3776) exists only in L1. Both are reported under their own names; L2 never claims to measure
cognitive complexity.

**Token cost is L2, not L3.** It is `chars / chars_per_token` over a file read — it needs nothing
from any tool. This placement is what lets `guard` enforce a token ceiling while remaining
stdlib-only.

### What is lost when L1 is unavailable

Named explicitly, because §8 must print it rather than leave a silent hole:

| Scorecard row | Source | Without Sonar |
|---|---|---|
| Reliability | L1 only | `not measured` |
| Security + hotspots | L1 only | `not measured` |
| Coverage | L1 only | `not measured` (never `0 %`) |
| Maintainability rating / debt | L1 only | `not measured` |
| Cognitive complexity | L1 only | `not measured`; CCN reported instead, labelled as such |
| Complexity (CCN), Size, Token cost, Duplication, Churn | L2/L3 | measured |

Consequence, stated in the report rather than hidden: **a Sonar-less audit can structurally never
raise a P0**, because the P0 tier is defined by correctness/security evidence that only L1
produces. The audit says so instead of implying the repository has no P0.

## 4. Default thresholds — profile `agent`

Calibrated for a model reading the code, not for a human.

**Comparison semantics, defined once and derived everywhere:** a threshold fires at
`value >= threshold`. `parameters.hard = 8` therefore means "8 parameters is already a violation",
which is what produces the established figure of 114 functions with ≥8 parameters. Sonar's rule
parameters fire at `value > max`, so §12 configures Sonar with `hard - 1`. This one-line rule is
the only place the off-by-one is handled.

| Metric | key | warn | hard | Sonar default | Measured by |
|---|---|---|---|---|---|
| lines in a file | `file_lines` | 400 | 800 | 1000 (S104) | L2 lizard + file read |
| tokens in a file | `file_tokens` | 8000 | 15000 | no such metric | L2 file read |
| lines in a function | `function_lines` | 60 | 100 | 100 (S138) | L2 lizard |
| cyclomatic complexity | `function_ccn` | 15 | 25 | n/a for Python — `S1541` exists for JS/Java only | L2 lizard |
| cognitive complexity | `function_cognitive` | 15 | 25 | 15 (S3776, already active) | L1 Sonar only |
| parameters | `parameters` | 5 | 8 | 13 (S107) | L2 lizard |
| nesting depth | `nesting` | 3 | 4 | 4 (S134) | L2 lizard `-ENS` for a verified-correct language; own `ast` pass for Python; `None` otherwise (§5, `references/tools.md`) |

Overridden per repository in `.code-health/config.json`.

### Token estimate

`tokens = ceil(chars / chars_per_token)` over the file's UTF-8 text, `chars_per_token` default `4`.
Calibration check: 8974 lines ≈ 107k tokens ⇒ ≈47.7 chars/line, consistent with Python source.

This is labelled an **estimate** everywhere it appears; it is not a tokenizer. `lizard`'s `token`
column is a *lexical* count on a different scale, is reported only per function, and is never fed
into `file_tokens`. The only knob is the numeric `chars_per_token`; there is no mode switch,
because a mode that changed the unit would silently invalidate the threshold table above.

## 5. `scripts/measure.py` — the single measurer

Pure Python 3.9+, standard library only. External tools are invoked as subprocesses.

```
python3 measure.py --repo PATH
                   [--config PATH]              # default <repo>/.code-health/config.json
                   [--layers size,dup,imports,dead,churn,derived]   # default: all
                   [--files PATH...]            # restrict to these files
                   [--install-missing]          # provision absent tools, report what was installed
                   [--out PATH]                 # default stdout
```

There is no `--format text`. Every consumer is a skill that parses the document; a second
output format would be a second thing to keep in sync for no reader.

**Exit code is 0 whenever a valid document was produced**, including when every tool is missing and
every layer is `not_measured`. Findings never change the exit code. Only a usage error or an
unreadable repository is non-zero. The decision that a lizard-less run is not worth reporting
belongs to the `audit` skill, not to the measurer.

### Layer dependencies

`--layers` values are resolved **transitively**: requesting a layer pulls in what it needs.

| Layer | Requires | Finding kinds it can produce |
|---|---|---|
| `size` | file enumeration | `file_too_long`, `file_too_expensive`, `function_too_long`, `function_too_complex`, `too_many_params`, `too_deep_nesting` |
| `dup` | `size` | `exact_clone`, `near_duplicate` |
| `imports` | file enumeration | `import_cycle` |
| `dead` | file enumeration | `dead_symbol` (Python only; see "L2 — dead code") |
| `churn` | git history | — (feeds `derived`) |
| `derived` | `size` + `churn` + `imports` | `hotspot`, `symbol_collision`, `edit_fanout` |

A requested layer whose dependency is unavailable is `not_measured` with the dependency named as
the reason, not silently empty. `derived` reads the symbol table out of `imports`, which is why
`imports` is listed there: without the declaration, `measure.py --layers derived` resolved to
`size,churn,derived` and produced no collisions at all, and `fix` step 3.1 — which runs exactly
that before deciding whether a group's findings are still alive — skipped every `symbol_collision`
group unconditionally.

**The rule is per metric, not only per layer.** `derived` bundles three metrics with three
different dependencies, so a populated `edit_fanout` (which needs only churn) must not mask an
`hotspots: []` produced from a `size` layer that already declared itself `partial`. A metric
computed from a layer that is not `measured` and coming out empty is reported as `null` — "not
measured" — and its own layer drops to `partial` with the dependency named. `dup` follows the same
rule across its two independent sources: `near` from this plugin's own pass over `size`'s function
rows, `exact` from jscpd. `tests/test_measure.py` asserts this contract directly over a real run.

### File enumeration

`git -C <repo> ls-files -z` when `.git` exists — this gives `.gitignore` semantics for free and
never descends into `.git`. Otherwise `os.walk` with a hardcoded skip list
(`.git`, `.hg`, `.svn`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.tox`, `.mypy_cache`,
`.ruff_cache`).

Then, in order: drop symlinks; apply `exclude`; **drop anything that is not source code** and count
it as `summary.skipped.not_source`; drop files whose first 8 KiB contain a NUL byte (binary); drop
files over `max_file_bytes` (default 4 MiB) and count them in `summary.skipped`; split off `tests`.

**A tracked, readable text file is not automatically source.** `discovery.SOURCE_EXT` is the one
definition of "this file is code" — the same set `guard.py` has always enforced as `SUPPORTED_EXT`,
now shared and pinned by `tests/test_parser_agreement.py`. Without it the measurer and the ratchet
disagree about what a file is: measured on this plugin's own repository, 40 Markdown files
outnumbered 16 Python ones, three of three hard file-level findings were prose, and
`summary.by_cut["1000"].loc_pct` was computed over two design documents — while `guard.py` skipped
every one of them. Documentation, lockfiles and CI configuration are text and are still counted, as
`not_source`, so "412 files" and "60 not measured as source" appear in the same summary.

### Excludes and the pattern matcher

The `**/`-anchoring advice inherited from v0.2.0 is correct **for Sonar's matcher**, where `**/`
means zero-or-more directories. Python's `fnmatch` and `PurePath.match` do not agree — verified:
`**/dist/**` matches `packages/web/dist/a.js` but **not** top-level `dist/a.js`, and
`**/node_modules/**` matches neither.

`measure.py` is stdlib-only, so `compile_pattern` translates each gitwildmatch pattern into a
regex over the POSIX relative path — `**/` becomes `(?:[^/]+/)*` (zero or more directories), `*`
becomes `[^/]*` so it cannot cross a separator, and every other character is escaped as a literal.
No `fnmatch` anywhere in that path. An earlier draft of this section proposed normalizing each
pattern to the pair `{p, p.removeprefix("**/")}` and matching with `fnmatch`; implementing the
semantics directly turned out to be both shorter and correct for cases the pair trick still misses,
so that is what ships. The rule is restated in `references/tools.md` so nobody "simplifies" it back.

Exclusion discipline, inherited and non-negotiable: exclude only evidence-backed generated /
vendored / cache / build-output paths. Exclude agent working directories (`.claude/`, `.agents/`,
`.worktrees/`) that contain whole copies of the repository — confirm with `git worktree list`.
Never exclude difficult legacy code to improve a number.

### Source versus test code

v0.2.0 required tests to be classified so they do not distort source metrics. That is preserved and
made concrete, because without it the headline near-duplicate feature surfaces test fixtures ahead
of production clones and every percentage inflates.

`config.tests` is a pattern list (defaults: `**/tests/**`, `**/test_*.py`, `**/*_test.go`,
`**/*.test.ts`, `**/*.spec.ts`, `**/spec/**`). Test files are measured, kept in a separate bucket,
excluded from `summary.loc`/`token_cost` percentages and from the default near-duplicate pass, and
reported under their own heading. They are never simply dropped: a 3000-line test file is real, it
just is not the same problem.

### Output document

```jsonc
{
  "schema": 1,
  "generated_at": "2026-09-08T10:00:00Z",
  "repo":   { "root": "/abs/path", "head": "abc123", "branch": "main", "dirty": false },
  "config": { /* effective options after merge with defaults */ },
  "tools":  {
    "lizard": { "available": true,  "version": "1.23.0", "installed_by_us": false },
    "jscpd":  { "available": false, "reason": "npx download failed: network unreachable" }
  },
  "layers": {
    "size":    { "status": "measured",     "files": [...], "functions": [...] },
    "dup":     { "status": "partial",      "reason": "jscpd unavailable; near-duplicate pass ran",
                 "exact": [...], "near": [...] },
    "imports": { "status": "partial",      "languages_measured": ["python"],
                 "reason": "madge unavailable; JS/TS not measured", "cycles": [...], "degree": [...] },
    "dead":    { "status": "not_measured", "reason": "vulture unavailable" },
    "churn":   { "status": "measured",     "window_days": 180, "history_span_days": 412, "files": [...] },
    "derived": { "status": "measured",     "hotspots": [...], "symbol_collisions": [...],
                 "edit_fanout": [...] }
  },
  "findings": [
    { "id": "file_too_long:backend/reports.py",
      "kind": "file_too_long", "layer": "L2", "level": "hard",
      "subject": { "file": "backend/reports.py" },
      "metric": "file_lines", "value": 8974, "threshold": 800,
      "evidence": { "tokens": 107000, "functions": 63 } }
  ],
  "summary": {
    "files": 412, "loc": 91230, "tokens_total": 1094760,
    "tests": { "files": 96, "loc": 18400 },
    "skipped": { "binary": 12, "too_large": 1, "symlink": 0, "not_source": 137 },
    "by_cut": {
      "400":  { "files": 71, "loc": 52110, "loc_pct": 57.1 },
      "800":  { "files": 34, "loc": 44980, "loc_pct": 49.3 },
      "1000": { "files": 22, "loc": 38773, "loc_pct": 42.5 }
    }
  }
}
```

`summary.by_cut` reports at `file_lines.warn`, `file_lines.hard`, and the round `1000` so a figure
can be quoted at a named cut. Quoting "42.5 %" without saying "at the 1000-line cut" is what made
the hand-computed number and a hard-limit number disagree; `by_cut` removes the ambiguity.

`level` is `warn` or `hard`. Finding `id` is `"<kind>:<stable subject>"`, stable across runs so
ledgers can be diffed.

### L2 — lizard

Invocation: `lizard -ENS --csv <paths>` with **no thresholds** — collect everything, apply our own
thresholds in Python. Verified CSV column order (no header row):

```
nloc, ccn, token, param, length, location, filename, function_name,
long_name, start_line, end_line, max_nested_structures
```

`max_nested_structures` is appended last and only appears with `-ENS`. `long_name` contains commas
and is quoted; parse with `csv.reader`, never `split(",")`.

`max_nested_structures` is lizard's own column and threshold-field name — `-Tnd=` is rejected with
`Wrong field name 'nd'`. Once parsed, the value is carried under the key `nesting`, matching the
`thresholds.nesting` config key, so the same quantity has lizard's name on the way in and this
plugin's name everywhere after.

**File-level size never comes from lizard.** `--csv` emits function rows only — a 5000-line
function-free file produces empty output and exit 0, and would otherwise be invisible. File lines,
non-blank lines and chars come from a direct read.

Binary resolution order: `$CODE_HEALTH_LIZARD` → `shutil.which("lizard")` →
`[sys.executable, "-m", "lizard"]`. On the calibration machine lizard is a `uv tool` isolated
install: the CLI is on `PATH` but `import lizard` from the system interpreter fails. Never
`import lizard`.

**Nesting is not trusted from lizard for every language.** `lizard_ext/lizardnd.py` resets its
nesting counter on a closing brace; this is correct for a brace/semicolon reader and never resets
for an indentation-based or `end`-keyword one, because that token never appears. Verified directly
against real `lizard -ENS --csv`: a flat, 5-sequential-`if` Python function reports nesting 5
(should be 1), and the counter does not reset between functions or files in one invocation — three
copies of one function in a single call report 5, 10, 15, in `argv` order. `sizes.py` replaces
lizard's Python column with an exact value from `pyast.measure_nesting` (the same `ast` this design
already parses for the import graph), joined to lizard's row by `(file, start_line)` rather than
function name — a nested or method function's *name* does not reliably agree between the two readers
(lizard qualifies it, e.g. `outer.inner`; `ast` reports the bare `inner`), while `start_line` was
verified to agree in every case tested, including through decorators. Every language not verified
correct this way (Ruby and Lua included — both confirmed broken by the same probe) reports
`nesting: None`, never lizard's unverified number; `findings.py` and `guard.py` both treat `None` as
"not measured", never as zero, and never compare it against a threshold. `guard.py` carries its own
copy of the same `ast` pass, for the same reason it already carries its own CSV parser. Full
reproduction: `references/tools.md`.

### L2 — exact clones

`npx -y jscpd@5 --min-tokens <n> --reporters json --output <tmp outside repo> --silent <repo>`.
Optional; absence marks `dup.exact` unmeasured but never blocks the near-duplicate pass.

### L2 — near-duplicates (own, no dependencies)

This is the pass that catches what CPD misses.

1. Take every non-test function from lizard with `token >= near_duplicate.min_tokens` (default 40).
2. Read its source slice by `start_line..end_line`.
3. Tokenize, **string literals first** so their contents do not leak into the identifier stream:

   ```python
   TOKEN_RE = re.compile(
       r'"""[\s\S]*?"""'         # triple-quoted string
       r"|'''[\s\S]*?'''"
       r'|"(?:\\.|[^"\\])*"'     # single-line string
       r"|'(?:\\.|[^'\\])*'"
       r'|[^\W\d]\w*'            # identifier - Unicode-aware
       r'|\d+(?:\.\d+)?'         # number
       r'|[^\s\w]'               # punctuation
   )
   ```

   String literals are matched **before** identifiers, and identifiers use `[^\W\d]\w*` rather
   than `[A-Za-z_]\w*`. Verified on `def считать_итог(данные): x = "квартальный кэш"`:

   | pattern | tokens |
   |---|---|
   | `[A-Za-z_]\w*` form | `def`, `_итог`, `(`, `)`, `:`, `x`, `=`, `"`, `"`, ... |
   | the pattern above | `def`, `считать_итог`, `(`, `данные`, `)`, `:`, `x`, `=`, `"квартальный кэш"`, ... |

   The ASCII form does not merely drop non-ASCII identifiers, it **truncates** them
   (`считать_итог` becomes `_итог`) and loses `данные` entirely, because Python's `\w` is
   Unicode-aware while `[A-Za-z_]` is not. Without the string production, two functions differing
   only in their string constants also score as differing, since `"квартальный кэш"` degrades to
   two bare quote characters.
4. Build two normalizations:
   - **lexical** — tokens as-is, lowercased, string literals kept;
   - **structural** — every identifier outside a small per-language keyword set → `ID`, every
     string and numeric literal → `LIT`.
5. 5-gram shingles → 64-bit hashes → MinHash, 128 permutations.
6. **Two LSH indexes, not one.** 32 bands × 4 rows over the *lexical* signature (≈99 % recall at
   J=0.60) and the same banding over the *structural* signature (well served at J=0.85). One index
   tuned for 0.60 recovers only ~57 % of candidates at J=0.40, which is exactly the floor of the
   second branch — half the renamed-identifier clones the branch exists to catch would never become
   candidates.
7. Cheap prefilter: skip pairs whose token counts differ by more than 2×.
8. Exact Jaccard on candidates from either index. Report a pair when
   `lexical >= min_similarity` (default 0.60) **or** (`structural >= 0.90` and `lexical >= 0.25`).

   The second branch's floor is `0.25`, not `0.40`. Measured on a 7-line cache wrapper with three
   identifiers renamed: `structural = 1.00`, `lexical = 0.32`. A `0.40` floor rejects exactly the
   case the structural branch exists to catch. The structural bar is raised to `0.90` in exchange,
   and the `min_tokens = 40` cut plus the 2x size prefilter keep trivially-shaped small functions
   out. Unrelated functions measure `structural = 0.07` and are nowhere near either branch.
9. Cluster pairs into connected components; rank clusters by `size × avg_tokens`.

The headline number reported to the user is the **lexical** similarity, comparable to the "64–75 %
pairwise" figure that motivated this pass. Known limitation, stated in the report: comments are not
stripped, so heavily commented near-identical code scores lower than its logic warrants. A comment
reader now exists in this repository (`scripts/code_health/comments.py`, §5) that could strip them
before tokenizing; noted here as a possibility, not implemented — this pass still tokenizes raw
source, comments included.

### L2 — import graph

- **Python**: own pass using stdlib `ast`. Exact, no external tool, no network. Resolves
  `import x`, `from .x import y`, relative levels, `__init__.py` packages. Unresolvable imports are
  counted and reported, never dropped. The same pass emits **parameter-name tuples** for every
  function and the class/function definition table, both of which L3 needs.
- **JS/TS**: `npx -y madge@8 --json` for the dependency map, and cycles derived from that map.
  `--circular` replaces the map in the output rather than adding to it, so one invocation cannot
  provide both.
- Other languages: not measured, stated as such.

Outputs: `cycles` (each an ordered file list), `degree` (fan-in / fan-out per module).

### L2 — dead code

`vulture` for Python (`--min-confidence 80`). Optional; findings carry vulture's own confidence and
the symbol name, because the tool has known false positives on dynamic dispatch and because a
`dead_symbol` finding id keyed on the name survives an edit elsewhere in the file. Grouping and
priority are decided in §8, not here.

**JS/TS dead code is `not_measured`, and says so.** `npx -y knip@6` was invoked and its stdout was
never read — `measured` was appended on the return code alone, which is the same defect already
fixed for vulture: a confident "no dead code in JS/TS" produced by parsing nothing. There is no
verified parser here for knip's JSON reporter, so the layer reports `not_measured` with that as the
reason and knip is not run at all, rather than paying a 900-second subprocess for a result nobody
reads. Shipping an unverified parser would restore the same claim one level down.

### L2 — comments

Own reader, no external tool: `scripts/code_health/comments.py`. Three detectors —
`commented_out`, `trivial_doc`, `short_inline` — feed one metric, `noise_comment_lines`; only the
first two are summed into it. Folding `short_inline` in as well would give the whole metric the
precision of its weakest class, the same reason CCN and cognitive complexity are reported under
separate names rather than blended into one number.

**Two immunity passes, not one.** `is_directive` covers eradicate's own whitelist — tool directives
(`noqa`, `type: ignore`, `eslint-disable`, `pylint`, `pyright`, `nosec`, `fmt:`, `isort:`,
`NOSONAR`, `go:generate`, `ruff:`, `mypy:`), task tags (`TODO`/`FIXME`/`XXX`/`HACK`),
licence/copyright text, issue references and URLs, shebang and coding declarations — and gates
every detector, `commented_out` included. `is_prose_immune` covers the 30-word rule, reason words
(`because`/`workaround`/`otherwise` and their Russian equivalents), units and epoch/index
conventions, range-bound wording, null-value wording, and ownership/lifetime verbs — and gates only
the two prose detectors (`trivial_doc`, `short_inline`). The split exists because a commented-out
`# return None` was being spared by the null-value pattern under one combined list: commented-out
code is code, not documentation of null semantics, and only the directive list may excuse it.
`empty` and `closes` were dropped from the ownership/lifetime wording — both are ordinary verbs
that appear in exactly the restating comments this metric targets ("empty the cache", "closes the
file" over `f.close()`), not markers of genuine ownership documentation. Three of the directive
alternatives — the issue-key pattern, `None`, `SPDX` — are matched case-sensitively inside an
otherwise case-insensitive pass, because case-insensitively they matched `utf-8` and `python-3` as
issue keys.

**`commented_out` (Python only) is a majority rule, not "any line."** More than half of a
comment's non-empty lines must read as code — the earlier any-line rule flagged a fifteen-line
prose docstring in this plugin's own `pyast.py` as commented-out code, because the docstring's one
worked example contains `else:`. The pipeline's last step, ported from eradicate, is a real
`compile()`; a successful compile is now also rejected when the parsed result is a single bare
expression containing no call, because a `#` inside comment text truncates the line handed to
`compile` — this plugin's own comment ending `C/Java/C# typed form (…)` compiled as the valid
expression `C/Java/C`. Accepted cost: genuinely commented-out code shaped `x[1]` or `x % y` is now
missed too. Steidl's published `0.1` figure is not used anywhere in this module: it was tuned as
one input feature to a trained classifier in the source paper, not validated as a standalone
decision rule, and reusing it here would publish a number this repository never checked itself.

**`trivial_doc`'s inexact word match requires both words to be at least two characters.** Below
that length every distinct character pair sits one edit apart, so `coherence("и а к", "a_b_c")`
scored a perfect `1.0` before the floor was added. Exact equality still matches at any length.

**Languages.** `commented_out` is Python-only — a brace-language verdict needs its own verified
parser, the same precedent as `symbol_collision` and `dead_symbol` above. For a brace language its
evidence field is `null`, never an empty list: an empty list would read as "checked, found
nothing" for a class nobody checked there. `trivial_doc` and `short_inline` run for Python and for
the brace languages whose **declarations** a per-extension probe resolves — not for every language
whose comments the scanner can read. Both halves are required: a file whose comments are read but
whose declarations are invisible reports `trivial_doc: 0` on every file, which is the same silent
zero as lizard's nesting column one level down. The probe feeds one realistic declaration per
extension and `BRACE_EXT` keeps only the extensions that pass; it found C++ out-of-line member
definitions (`int Cache::load_all(int fd) {`) invisible, which was fixed, and Objective-C's
`- (void)loadAll:(int)x {` unmatched by any of the three declaration shapes, which removed `.m` and
`.mm` from the set. Objective-C, Objective-C++, Ruby, PHP and Lua are `not_measured` and named as
such. The full table, the two removals and the known recall gaps inside a passing language are in
`references/tools.md`; the probe ships as `BraceLanguageProbeTests`, which also asserts that no
extension can join `BRACE_EXT` without a passing probe row.

**`short_inline` is evidence-only.** Measured at ~0.70 precision, below the 0.85 floor this
repository requires of a counted class; never summed into `noise_comment_lines` and never its own
group, but free to report once `fix` already has the file open.

**Published precision, not this repository's own corpus.** `trivial_doc` is reported at ~0.90 and
`commented_out` at ~0.89 (Steidl et al., ICPC 2013) — the literature's validated figures;
`trivial_doc`'s corroborated there by a 16-developer survey over 30 examples. `tests/comment_corpus.py`
scores 1.00 precision on both counted classes, and that number is never published as a measured
precision: it is a regression pin written alongside the detectors by their own author, over
fixtures the same author chose because the answer was already known. The false-positive rate this
metric actually carries on a given repository is what threshold calibration there observes, not
what the corpus reports — the `warn`/`hard` values for `noise_comment_lines` are correspondingly
not assigned yet, calibrated against observed figures the same way `parameters.hard` and
`near_duplicate.min_similarity` were, rather than guessed.

**What this layer does not catch.** A comment that paraphrases its code in different words —
`// loop through items` over `for item in items:` — shares no lexical overlap with it under
coherence and goes undetected. Catching it needs an entailment model (Louis et al., 2018), which
this layer deliberately does not run — the same kind of stated gap as nesting for Ruby/Lua and
dead code for JS/TS above.

Grouping and priority are decided in §8 and `references/quality-policy.md`, not here.

### L2 — churn

Plain `git`, no external tool:

```
git -C <repo> log --since="<window> days ago" --no-merges --numstat -z -M \
    --format="%H%x09%aI%x09%aE"
```

Four things this command gets right that the obvious form does not:

- `git -C <repo>` and no `-- .` pathspec. `-- .` is **cwd-relative**, so `measure.py --repo PATH`
  invoked from elsewhere would silently measure a subtree or nothing.
- `%aE` is present. Without it there is no author identity in the output at all — `%aI` is the
  author *date*.
- `-z` is required for renames. Verified: without it `-M` emits the brace-compressed form
  `src/{old => new}/mod.py`, which a `split(" => ")` fold turns into `src/{old` and `new}/mod.py`.
  With `-z` the two paths arrive as separate NUL-delimited fields.
- Binary files emit `-` for both counts, not integers; the parser needs an explicit branch.

`--format` composes with `--numstat`. **Without `-z`** the commit header is followed by a blank
line and then the numstat block; **with `-z`** there is no blank line at all — every record is
NUL-terminated. Verified output (NUL shown as `|`):

```
<sha>\t<date>\t<email>|1\t0\t|src/old/mod.py|src/new/mod.py|<sha>\t<date>\t<email>|-\t-\tblob.bin|3\t0\tsrc/old/mod.py|
```

Read that as: header record, then `added\tremoved\t` with an **empty** path field followed by two
separate path fields when the entry is a rename, and `-\t-\t<path>` for a binary file. The
brace-compressed form never appears.

Per file: `commits`, `authors`, `lines_added`, `lines_removed`, `last_change`. Files not present at
`HEAD` are dropped. Default window `churn_window_days: 180`; a shorter history reports its actual
`history_span_days` and says so. No `.git` ⇒ `churn.status = not_measured`, and every L3 metric
that depends on churn is likewise `not_measured` — never a silent zero.

### L3 — derived metrics

| Metric | Definition | Why it matters to an agent |
|---|---|---|
| `hotspot` | `commits_in_window × sum(ccn of functions in file)`, reported with both percentile ranks | Tornhill's product: complex code nobody touches is not the problem |
| `symbol_collisions` | Symbol names (lizard functions, plus Python classes and functions from the `ast` pass) defined in ≥ `collision_min_files` distinct files (default 3), ranked by `count × avg_definition_tokens` | The agent greps a name, gets N hits, reads the wrong definition |
| `edit_fanout` | `mean(other files in commits touching f)` divided by the repository-wide **median** other-files-per-commit, falling back to the **mean** when that median is `0`; the denominator actually used is reported as `edit_fanout_basis` (`median` / `mean` / `unavailable`) | A ratio, not a raw count — the raw count is dominated by the repository's commit-size habits rather than by the file. `> 1` means editing here drags in more than a typical commit does |

## 6. Configuration — `.code-health/config.json` (target repo, committed)

```jsonc
{
  "schema": 1,
  "profile": "agent",
  "thresholds": {
    "file_lines":         { "warn": 400,  "hard": 800 },
    "file_tokens":        { "warn": 8000, "hard": 15000 },
    "function_lines":     { "warn": 60,   "hard": 100 },
    "function_ccn":       { "warn": 15,   "hard": 25 },
    "function_cognitive": { "warn": 15,   "hard": 25 },
    "parameters":         { "warn": 5,    "hard": 8 },
    "nesting":            { "warn": 3,    "hard": 4 }
  },
  "exclude": ["**/node_modules/**", "**/.venv/**", "**/dist/**", "**/__pycache__/**"],
  "tests":   ["**/tests/**", "**/test_*.py", "**/*.test.ts", "**/*.spec.ts", "**/spec/**"],
  "churn_window_days": 180,
  "chars_per_token": 4,
  "max_file_bytes": 4194304,
  "near_duplicate": { "min_similarity": 0.60, "min_tokens": 40 },
  "collision_min_files": 3,
  "gate": null
}
```

Absent file ⇒ built-in defaults; `audit` never writes it. `guard install` is the only command that
may create it, and only with confirmation.

`gate` is read by `fix` as **step 0** of gate detection (§9). A committed gate command is the
repository's own answer and is used without asking.

## 7. The debt ledger — `references/ledger.md`

Location: `${XDG_STATE_HOME:-$HOME/.local/state}/code-health/<project-key>.json`.
**Outside the target repository** — machine-specific, holds Sonar keys.

```jsonc
{
  "schema": 1,
  "project_key": "github:owner:repo",
  "repo_path": "/abs/path",
  "audit": {
    "at": "2026-09-08T10:00:00Z", "head": "abc123", "dirty": false, "branch": "main",
    "layers": { "L1": "measured", "L2": "measured", "L3": "partial" },
    "summary": { /* verbatim copy of measure.py summary — the repo-wide anti-gaming baseline */ },
    "sonar": { "host": "http://localhost:9000", "analysis_key": "...", "revision": "abc123",
               "profile": "Code Health — github:owner:repo" }
  },
  "groups": [
    { "gid": "nd-7f3a91c4", "label": "A1", "priority": "P1",
      "title": "Quarterly cache protocol copied into 16 files",
      "why": "One change to the cache contract needs 16 coordinated edits; the cluster sits in the top churn decile",
      "kind": "near_duplicate", "layer": "L2",
      "finding_ids": ["near_duplicate:cluster/7f3a91c4"],
      "files": ["..."], "functions": ["file.py:compute_summary_payload"],
      "sonar_issue_keys": [], "rule_keys": [],
      "metric": { "name": "near_duplicate", "worst": 0.91, "threshold": 0.60, "count": 16 },
      "remediation": "Extract one cache-protocol module; the 16 call sites become thin adapters." }
  ],
  "queue": {
    "created_at": "...",
    "gate": { "command": "make gate", "detected_from": "Makefile target `gate`", "confirmed_at": "..." },
    "baseline_summary": { /* snapshot at cycle start, for repo-wide anti-gaming */ },
    "items": [
      { "gid": "nd-7f3a91c4", "state": "done", "commit": "def456",
        "started_at": "...", "finished_at": "...", "note": "" },
      { "gid": "cx-2b81ee07", "state": "pending" }
    ]
  }
}
```

### Group identity

`gid` is **content-derived**: `"<kind-prefix>-" + sha1(kind + sorted subject paths)[:8]`. `label`
(`A1…An`) is a display convenience, reassigned by every audit in priority order.

**The queue stores `gid`, never `label`.** Positional ids plus a preserved queue is silent data
corruption: after a re-audit, a pending `A5` points at whatever is fifth *now*, and the safeguard
"report if the group no longer exists" never fires because `A5` does exist — it is simply a
different problem. With content ids, a group that genuinely vanished is detectable and is reported.

`state` ∈ `pending | running | done | failed | skipped`. An item found in `running` at the start of
a `--resume` belonged to a crashed or killed session: it is reported, its files are checked for
uncommitted changes, and the user chooses retry or skip. It is never silently retried.

The ledger never stores source snippets, tokens, credentials, or raw API dumps. It is a **locator,
not a source of truth**: `fix` re-measures before editing. One file per project, overwritten by
each audit, with any not-fully-done `queue` carried over. A queue in which **every** item is
`done`/`skipped`/`failed` is finished: it is dropped rather than carried, and the new ledger is
written with no `queue` key at all, so the next `fix` builds a fresh one instead of resuming a
queue with nothing left to do.

## 8. `/code-health:audit`

Preserves every v0.2.0 behaviour:

- read-only for the target repository — never edits code, tests, build files, ignore files, or repo
  Sonar config;
- never improves a score by excluding live code, disabling rules, moving thresholds, marking issues
  false-positive, or adding suppressions;
- never transitions into code changes in the same session — emits a `/code-health:fix …` handoff
  and stops;
- reuses a genuinely fresh analysis only (clean tree, same revision, < 60 min, unchanged scope);
- compact grouped report `A1…An`;
- handoff written to XDG state, never into the repository;
- verifies the repository's tracked/staged state is unchanged afterwards;
- **coverage discipline**: reuse existing reports when they match the current source; run only a
  documented, local-safe project command; rewrite report paths to be repository-relative and
  confirm the import in the scanner log; report `unavailable` per language, **never `0 %`**;
- **security hotspots** retrieved and reported when the server provides them.

The last two live in the current audit `SKILL.md` and are restated here because that file is being
rewritten.

New behaviour:

1. **L2 and L3 always run.** `measure.py` runs first; Sonar layers on top when available.
2. **Works without Sonar** — `L1: not_measured` with the reason printed, and the table in §3
   printed rather than the rows silently vanishing.
3. **Tool provisioning is automatic, with a report** (§11). No per-tool prompt.
4. **Sonar quality profile instead of routing around the rules** (§12).
5. **New report section "Agent ergonomics"**: token cost, symbol collisions, churn × complexity,
   edit fan-out.
6. **Explicit `not measured` per layer**, in the JSON and in the human report.

### One new permission boundary

Audit is read-only **for the target repository**. It may write to the **Sonar server** — create the
project and create/reconcile the quality profile. Disclosed and confirmed once per server. It never
touches the target repo. Reconciliation on later audits is silent only while it is a no-op; any
actual write to the profile is announced in the report (§12).

### From findings to groups

`measure.py` emits flat `findings`. The audit turns them into ledger groups. Grouping follows
`references/quality-policy.md` — one remediation per group, not one finding per group — plus:

| Kind | Grouped by | Typical group |
|---|---|---|
| `file_too_long`, `file_too_expensive` | subsystem/directory | "22 backend report files over the 1000-line cut" |
| `function_too_long`, `function_too_complex`, `too_deep_nesting` | the enclosing file when several fire in one file, otherwise the subsystem | "one 211-line, CCN-18 function" |
| `too_many_params` | the shared parameter-name set (below) | "114 functions carrying the same 8-argument report context" |
| `near_duplicate`, `exact_clone` | the cluster, one group per cluster | "cache protocol copied into 16 files" |
| `import_cycle` | one group per cycle | "3-module cycle between reports, cache, and models" |
| `dead_symbol` | the owning module or package, never repo-wide | "24 unreferenced helpers in `legacy/`" — capped at `P2` |
| Sonar reliability/security issues | rule + subsystem | "3 blocker null-deref issues in the payment module" |
| `comment_noise` | subsystem/directory | "31 files carrying commented-out code under `legacy/`" — capped at `P2` |
| `hotspot`, `edit_fanout`, `symbol_collision` | never their own group — they **re-rank** other groups and appear as evidence in `why` | — |

Sonar issues are in the table because they need a grouping rule as much as the L2 kinds do and the
audit skill had to invent one in its absence. How a re-ranking finding is nevertheless *referenced*
by a group — never creating one — is stated in `references/ledger.md`.

A `comment_noise` finding's `evidence.zero_coherence` lists comments that share no word with the
function they document. That is not a finding of its own and never becomes a group: when the
function also carries a `function_too_complex`, `function_too_long`, or `too_deep_nesting` finding,
name the comment in that group's `why` as evidence — the code needed prose to be understood, which
is the group's own argument. When the function carries no other finding, ignore the entry. Same
rule already in force for `hotspot`, `edit_fanout`, and `symbol_collision`: a group made only of
re-ranking findings is forbidden (`references/ledger.md`, "Re-ranking findings").

**Parameter-set clustering** for `too_many_params`, since lizard gives only a count and a
`long_name` string: the Python `ast` pass emits each function's ordered parameter-name tuple;
functions are grouped by Jaccard ≥ 0.6 over their parameter-name *sets*, via the same MinHash
machinery as §5. Languages with no `ast` pass fall back to grouping by subsystem, and the report
says which was used.

The `hotspot` row is the point of L3: churn and fan-out are not separate work items, they decide
which of the other groups is `P1` rather than `P3`. Complexity in a file untouched for six months
is not urgent; the same complexity in a file changed 40 times is.

Noise accounting stays required: state what share of findings comes from rules representing no
engineering decision (generated code, syntax-modernization rules), so the headline count is not
mistaken for actionable work.

### Report shape

Sections: **Repository health**, **Scorecard**, **Fix first** (≤5 groups, P0–P3), **Hotspots**,
**Agent ergonomics** (new, ≤10 lines), **Worth noting**, **Agent review** (≤3 observations).

Scorecard gains `Size`, `Token cost`, `Churn` rows, and every row carries `measured` /
`not measured`.

Budget: **≈60–80 lines**, raised from v0.2.0's 40–60. The additions — a provisioning line, three
Scorecard rows, the ergonomics section, per-layer status, noise accounting — are roughly 15 lines,
and pretending they fit the old budget would just push the model into dropping something silently.

## 9. `/code-health:fix`

```
/code-health:fix A3              one group
/code-health:fix A3 A5 A1        a queue in the given order
/code-health:fix --p0            every P0 in the ledger   (--p1 likewise)
/code-health:fix --resume        continue an interrupted queue
```

Labels on the command line are resolved to `gid` against the current ledger before anything else.
With no argument and no explicit selection in the conversation, ask — never pick a large cleanup
scope on the user's behalf.

### One deliberate reversal of v0.2.0

v0.2.0 said "do not commit or push unless the user separately asks". **Queue mode commits, one
commit per group.** Without a commit there is no unit for the ratchet, no `--resume`, and no way to
leave the machine for an hour. This is a reversal and is named as one.

Two v0.2.0 rules are **not** relaxed by it, and are restored verbatim:

- *Do not reset, overwrite, stash, or discard pre-existing user changes.*
- *Do not push.*

The commit stages **only the exact paths the subagent touched**, never `git add -A` or
`git commit -a`. Any of those paths that was already dirty at cycle start makes the item abort
rather than commit, because the plugin cannot separate its work from the user's inside one file —
and an abort at that point stops the cycle (§9 step 7), because the aborted edits are still sitting
in the working tree where the next item would inherit them.

### Cycle start, once

1. Resolve repo root and project key; load the ledger; require `repo_path` to match.
2. **Detect the gate** and confirm once, then store it in `queue.gate`. First match wins:
   0. `config.gate` from `.code-health/config.json`, if set — the repository's own answer, used
      without asking;
   1. `make gate` — a `gate` target exists;
   2. `make test` — a `test` target exists;
   3. `npm test` — `package.json` has a `scripts.test` that is not the npm placeholder stub;
   4. `pytest` — pytest is configured (`pytest.ini`, `[tool.pytest]`, `tox.ini`, or a `tests/` dir
      with pytest available);
   5. `pre-commit run --all-files` — `.pre-commit-config.yaml` exists;
   6. otherwise ask.
   A stored gate is reused by `--resume` without re-asking.
3. Record branch, `HEAD`, working-tree state, and the exact set of pre-existing dirty paths.
4. **Run the gate once before any edit.** Record pre-existing failures. A gate already red stops the
   cycle — a ratchet against a red baseline proves nothing.
5. **Snapshot `measure.py` `summary` repo-wide** into `queue.baseline_summary`. This is what
   anti-gaming rule 5 compares against; without it, "code moved somewhere the measurer no longer
   looks" is undetectable from a per-file re-measure.

### Per queue item

0. **Skip the item if it collides with the user's own work.** Compare the group's file list
   against the dirty paths recorded at cycle start. Any overlap ⇒ mark the item `skipped` with the
   reason and move on **without dispatching anything**. This check belongs here, before the edit,
   not at commit time: once a subagent has written into a file that already held uncommitted user
   content, the two are entangled in the working tree, and "leave it uncommitted" does not
   disentangle them. The commit-time check in step 6 stays as a backstop for paths the group did
   not list.
1. **Re-measure this dimension only**: `measure.py --layers <the group's layers> --files <the
   group's files>`. **`--layers` is resolved transitively; `--files` is not** — it is an exact
   membership filter, so the file list must already name everything the group covers. Confirm the
   finding is still alive by `finding_ids`. Gone ⇒ `skipped`, reason recorded, continue.
2. **Establish the narrow safety baseline** — the v0.2.0 ladder, which the gate does not replace:
   run the narrowest existing tests covering the affected area; for a reliability/correctness
   finding write a focused regression test that fails for the expected reason *before* the fix and
   survives after it; for a behaviour-preserving refactor use existing behavioural tests and add a
   characterization test only where the behaviour is important and unprotected. Never invent a fake
   failing test, never introduce a new test framework.
3. **Dispatch a subagent** with a narrow brief: this group, these files, this shape of change,
   touch nothing else. The subagent inherits every non-negotiable rule below.
   **Escalation path, restored from v0.2.0:** if correcting the finding needs a consequential
   product or architecture decision with several reasonable outcomes, the subagent stops and
   **returns the decision to the cycle rather than asking the user itself** — it holds a narrow
   brief and, in an unattended run, has no one to ask. The cycle marks the item `skipped`, records
   the question, and puts the smallest version of it to the user when the run ends or immediately
   if the user is present. The cycle does not guess. This matters most in queue mode, where a group
   can be "cache protocol copied into 16 files".
4. **Escalate checks narrow → broad**: focused/regression tests, then the affected module's suite,
   then typecheck/build/lint, then **the project gate**. Pre-existing failures stay attributed to
   the baseline, never to the fix.
5. **Re-measure and reject gaming** (below), including `summary` against `queue.baseline_summary`.
6. **Commit** the touched paths, one commit per group; mark `done` with the SHA.
7. **Any abort after the subagent has edited stops the cycle** — a red gate, an anti-gaming hit, or
   a touched path that was already dirty at cycle start. The item is marked `failed`, its edits stay
   uncommitted, nothing is reverted automatically, and the human decides. It stops rather than
   continuing because the aborted edits remain in the working tree: item N+1's step-0 dirty check
   compares against the paths recorded at *cycle start* and would let them be committed under the
   next item's SHA, `queue.baseline_summary` is stale for every later item, and the next gate run
   would validate the rejected work as baseline. Adding the aborted paths to the dirty set fixes
   only the first of the three.

Work happens in the **main working tree, no worktree** — the gate needs installed dependencies. The
queue survives the session, which is what `--resume` reads.

`fix` never turns into `audit` and `audit` never turns into `fix`. When the stored analysis does not
match the current source, `fix` runs its own read-only measurement inside the fix session.

### Anti-gaming check

Applied to each group's diff before its commit. Any hit ⇒ reject: edits stay uncommitted, item
`failed`, cycle stops.

1. Test files deleted; test functions removed; `skip` / `xfail` / `.skip` / `.only` /
   `pytest.mark.skip` added; assertions weakened to tautologies.
2. Any of `.code-health/config.json`, `.code-health/baseline.json`, `.code-health/guard.py`,
   `.pre-commit-config.yaml`, `sonar-project.properties`, or a lint configuration modified. The
   ratchet's own machinery is included deliberately — otherwise a subagent can neuter the hook and
   commit. Moving the ratchet is `guard update-baseline`, a separate explicit command.
3. Suppressions added: `NOSONAR`, `# noqa`, `# type: ignore`, `eslint-disable`,
   `@SuppressWarnings`, `# pylint: disable`, `pragma: no cover`.
4. Source files deleted, or moved into an excluded/generated path, without an equivalent
   replacement.
5. Repo-wide `summary` did not improve consistently with the local metric — code moved somewhere
   the measurer no longer looks. Compared against `queue.baseline_summary`.

**The false-positive exception, restored from v0.2.0.** Rule 3 is not absolute. A suppression is
permitted when the finding is demonstrably a false positive *and* the user explicitly approves it
for that occurrence. That approval is a stop-and-ask, never a subagent's own decision, and the
suppression carries a comment naming the evidence. Without this path a fix that legitimately needs
one `# type: ignore` can never be committed.

Everything else from v0.2.0's `fix` stays: smallest semantically correct change, root cause at the
right boundary, no unrelated refactoring, behaviour and public contracts preserved, and the compact
final announcement (`Исправлено` / `Как исправлено` / `Как проверено` / `Осталось`). In queue mode
each group gets one line; the full four-section announcement is emitted once at the end, or
immediately when the cycle stops.

## 10. `/code-health:guard`

```
/code-health:guard install           vendor the hook, write the baseline, activate it
/code-health:guard check             run the ratchet by hand, install nothing
/code-health:guard update-baseline   move the ratchet after a fix
```

### `install`

With confirmation, in this order:

1. **Ensure `pre-commit` itself.** Adding a hook entry to `.pre-commit-config.yaml` does nothing
   until `pre-commit install` writes `.git/hooks/pre-commit`. If `pre-commit` is absent, offer to
   install it (§11); if the repository already has a `.git/hooks/pre-commit` that pre-commit did
   not write, report it and stop rather than overwriting someone's hook.
2. **Write the `.pre-commit-config.yaml` entry:**

   ```yaml
     - repo: local
       hooks:
         - id: code-health-guard
           name: code-health guard (ratchet)
           entry: python .code-health/guard.py
           language: python
           additional_dependencies: ["lizard==1.23.0"]
           types_or: [python, javascript, ts, tsx, jsx, go, java, kotlin, rust, ruby, php, c, c++, c#,
                      swift, scala, lua, objective-c]
   ```

   That block is an **entry** under a top-level `repos:` list, not a whole file. Append it to an
   existing `.pre-commit-config.yaml`; in a repository that has none, create one whose entire
   content is `repos:` followed by the same block. A model installing into a fresh repository has
   no other way to know this from the snippet alone.

   `pass_filenames: true` and `types: [file]` are pre-commit defaults and are omitted. `types_or`
   is what earns its place: it makes pre-commit skip the hook entirely when a commit touches only
   docs or config, instead of paying process startup for `guard.py` to filter them.

   **The tag list must cover `SUPPORTED_EXT`.** A file whose type is not listed never reaches the
   hook at all, so an extension the hook claims to check and pre-commit never delivers is checked
   by nobody. `lua` and `objective-c` were missing (`.lua`, `.m`), and both are real `identify`
   tags — verified against `identify.ALL_TAGS`. `.mm` arrives as `c++`, which is already listed.

   pre-commit builds the isolated environment itself, so the user's machine stays clean and CI
   behaves identically. Verified: `language: python` + `additional_dependencies` puts lizard's
   console script on `PATH` for `entry`.
3. **Vendor `.code-health/guard.py`** — self-contained, standard library only plus the `lizard`
   binary pre-commit provides. A colleague without the plugin and a CI runner must both pass the
   hook. It is a copy of `scripts/guard.py`; `install` and `update-baseline` refresh it and report
   drift.
4. **Write `.code-health/baseline.json`** and stage all three files. **The baseline is committed** —
   an uncommitted baseline makes the ratchet meaningless for the second person and for CI.
5. **`pre-commit install`.**

```jsonc
{
  "schema": 1,
  "generated_at": "...", "head": "abc123",
  "thresholds": { /* thresholds in force when frozen — informational, for drift reporting */ },
  "files": {
    "backend/reports.py": {
      "lines": 1467, "tokens": 21300,
      "functions": {
        "compute_summary_payload": { "lines": 211, "ccn": 18, "params": 19, "nesting": 3 },
        "handler":   { "lines": 201, "ccn": 12, "params": 3, "nesting": 2 },
        "handler#2": { "lines": 2,   "ccn": 1,  "params": 1, "nesting": 0 }
      }
    }
  }
}
```

### Ratchet rule

Only changed files are checked. **One rule:**

> No entity may exceed `max(its baseline value, warn)`.

A file or function absent from the baseline has no baseline value, so its ceiling is `warn`. That
is the whole rule — there is no separate case for new code, and deliberately so: the alternative
(`hard` for new entities) is *more permissive for new code than for old*, letting a brand-new
800-line file through while failing an existing 401-line file. `hard` is used by `audit` for
prioritization and by §12 for Sonar; `guard` does not need it.

A function is matched to its baseline by `file path + function name`, **disambiguated by order of
appearance when the name is not unique in that file** — the second `handler` in a file is keyed
`handler#2`. lizard reports bare names for Python methods, so `A.handler` and `B.handler` both
arrive as `handler`; keying on the bare name collapsed them into one entry with the last row
winning, and a 201-line method next to a 2-line one recorded `{"handler": {"lines": 2}}` — the large
one vanished from the ratchet permanently, while the audit still reported it. The key deliberately
carries no line number (neither `start_line` nor lizard's `location`): adding one `import` line at
the top of a file moves every function down one line, and under a line-bearing key a grandfathered
200-line function would read as new, drop its ceiling from 200 to `function_lines.warn` (60), and
fail a commit that never touched it.

A rename counts as a new entity and faces `warn` — deliberate: a rename is the moment not to carry
the old size forward, and fuzzy matching makes the ratchet unpredictable. Reordering two same-named
functions reads the same way, for the same reason.

**Which thresholds apply:** the current `.code-health/config.json`, not the frozen copy. The frozen
copy exists so drift can be reported — lowering a threshold immediately tightens the ceiling, which
is the intended way to raise the bar; raising one is announced by `guard check` as a loosening.
`check` prints the drift on every run, whether or not anything fails; the hook path prints it only
alongside a failure, where it explains the ceiling, rather than on every commit.

Failure prints, per violation: file, entity, metric, baseline/threshold, current value, and both
escape hatches — `git commit --no-verify` and `/code-health:guard update-baseline`. Exit 1.

Performance budget: ≤2 s for 20 changed files. Measured on the calibration machine:
`lizard -ENS --csv` over 20 files totalling 72,900 lines takes 0.57 s. `guard.py` in the hook path
does one lizard subprocess, one JSON read, no network and **no git calls**.

### One measurement path, one lizard version

`update-baseline` **shells out to the vendored `.code-health/guard.py`** to produce the baseline,
and `measure.py` pins the same lizard version as `additional_dependencies`. Writing the baseline
with one parser and enforcing it with another — against a different lizard version — produces
immediate false failures on untouched code the first time a CCN or NS calculation changes upstream.

`update-baseline` prints what moved in each direction; a baseline that moved **up** on any metric is
called out explicitly, because that is the ratchet slipping. That requires reading the OUTGOING
baseline before overwriting it — an ordering the first implementation did not have, which made the
promise unimplementable. Files present in only one of the two baselines are new or removed entities,
counted separately rather than reported as movement.

### `check`

Runs the same comparison without installing anything, over
`git diff --name-only HEAD` **plus `git ls-files --others --exclude-standard`**. Untracked files are
exactly the "new entity" case, and `git diff` does not list them — verified. The git calls live in
`check` only; the hook path never shells out to git, because pre-commit already passes the file
list.

### Why only a git hook

`git commit` is run by the model itself, so a failed hook returns the error into the model's
context — the model is caught by the same channel as a human. The same hook works in Codex, in
Cursor, and in CI. No Claude Code hooks.

## 11. Tool provisioning

`measure.py --install-missing` provisions and reports; `guard.py` never installs anything.

| Tool | Purpose | Install order | If unavailable |
|---|---|---|---|
| `lizard` | size, CCN, params, nesting | `uv tool install` → `pipx install` → plugin-owned venv | `size` (and `dup`, `derived`) `not_measured`; audit still reports churn and token cost |
| `jscpd` | exact/token clones | `npx -y jscpd@5` | `dup.exact` not measured; near-duplicate pass still runs |
| `madge` | JS/TS import graph | `npx -y madge@8` | JS/TS imports not measured; Python still measured |
| `vulture` | Python dead code | `uv tool install` → `pipx` → venv | `dead` (Python) not measured |
| `knip` | JS/TS dead code — **not currently consumed**, its JSON output is not parsed (§5, `references/tools.md`) | `npx -y knip@6` | `dead` (JS/TS) is not measured either way |
| `pre-commit` | activates the guard hook | `uv tool install` → `pipx` → venv | `guard install` reports and stops before writing anything |
| `git` | churn | already present | `churn` and every churn-derived L3 metric not measured |
| Docker + SonarQube | L1 | `references/sonar.md` playbook | `L1: not_measured`, audit continues |

**No `pip install --user`.** PEP 668 rejects it on a Homebrew Python — verified, `EXTERNALLY-MANAGED`
is present on the calibration machine. The third fallback is a plugin-owned `python3 -m venv` under
`${XDG_DATA_HOME:-$HOME/.local/share}/code-health/venv`.

`dependency-cruiser` is **not** used. Its only justification would be a query madge cannot express,
and the two outputs this design needs — `cycles` and `degree` — both come from `madge --json`.

`npx -y` downloads into the npx cache on first use and needs network. Offline ⇒ those layers are
`not_measured` with `reason: network unavailable`, and the audit still completes.

Installation is reported as one line at the top of the audit output:
`Installed for this audit: jscpd@5.1.2, madge@8.0.0. Already present: lizard 1.23.0, vulture 2.7.`

## 12. Sonar quality profile

Creating the profile is what turns L1 from misleading into useful.

**Permission.** `create`, `change_parent`, `activate_rule` and `add_project` all require
**Administer Quality Profiles**. The v0.2.0 playbook mints an ordinary user token, so this can 403
on a normally-provisioned instance. On 403 the audit does **not** fail: it falls back to the
built-in `Sonar way` profile, marks `L1: partial`, and prints the reason and the permission needed.
That fallback is the honest state — Sonar's blind spots are then real, and §2 already says what
they cost.

**Extend, do not copy.** `POST /api/qualityprofiles/copy` produces a detached snapshot that never
picks up rules added by later SonarQube upgrades, and its `toName` is documented as the name for a
*new* profile — it is not idempotent. Instead:

1. `GET /api/qualityprofiles/search?language=<lang>&qualityProfile=<name>` — existence check.
2. If absent: `POST /api/qualityprofiles/create`, then
   `POST /api/qualityprofiles/change_parent` with the built-in `Sonar way` profile as parent.
   Inheritance means upstream rule additions arrive on their own.
3. `POST /api/qualityprofiles/activate_rule` for the rules below.
4. `POST /api/qualityprofiles/add_project` binds this project only. Global defaults never change.

**One profile per project**, named `Code Health — <project-key>`. A single shared
`Code Health (agent)` would be server-global while thresholds are per-repository: repo B's config
reconciling the profile would silently change what repo A measures.

**Rules activated.** Values are `hard - 1`, because Sonar fires at `value > max` while §4 fires at
`value >= hard`.

| Rule | Purpose | Value from |
|---|---|---|
| `S138` | function length | `function_lines.hard - 1` = 99 |
| `S104` | file length | `file_lines.hard - 1` = 799 |
| `S134` | nesting | `nesting.hard - 1` = 3 |
| `S107` | parameters | `parameters.hard - 1` = 7 |

`S3776` is **not** in the table: it is already active at `threshold=15` in every built-in profile,
so writing that value back is a no-op. It is left at the built-in default and read, not set.

**Parameter names are resolved, never hard-coded.** Verified against SonarQube 26.8: `python:S104`
takes `maximum` (not `maximumFileLocThreshold`), `python:S134` takes `max` (not
`maximumNestingLevel`), and `java:S104` takes `Max` with a capital M — **case matters**. Rule keys
are language-prefixed (`python:S138`, `javascript:S138`, …). The implementation resolves both the
key and the parameter name per language via `GET /api/rules/search` and `GET /api/rules/show`
before every activation. A rule absent for a language is skipped and reported, not treated as a
failure.

**One `hard` value across all languages is deliberate.** Sonar's own defaults differ substantially
(`javascript:S138` = 200 vs `python:S138` = 100; `java:S134` = 3), so `S138 max=99` is roughly a 2×
tightening for JS. The profile is named `agent`: what a model can hold in context does not vary by
language the way community style guides do.

**Reconciliation** runs on every audit but is silent only while it is a no-op. Any actual write —
a drifted parameter set back, a newly activated rule — is announced in the report, so no
unannounced server mutation happens after the one-time confirmation.

Everything else in the existing `sonar-operations.md` — discovery, install, auth, project identity,
host architecture, scanner selection, analysis parameters, exclusions, coverage, completion
polling, freshness, triage, repository integrity — moves to `references/sonar.md` unchanged. Its
one line "keep the installed default `Sonar way` profiles" is replaced by this section.

## 13. Plugin layout

```
plugins/code-health/
  .claude-plugin/plugin.json
  .codex-plugin/plugin.json
  DESIGN.md
  README.md
  skills/audit/SKILL.md      skills/audit/agents/openai.yaml
  skills/fix/SKILL.md        skills/fix/agents/openai.yaml
  skills/guard/SKILL.md      skills/guard/agents/openai.yaml
  references/metrics.md          catalogue: what each metric means, what measures it, how it hurts an agent
  references/tools.md            tool layer: install, invoke, parse output, the traps
  references/ledger.md           ledger and queue schema — shared by all three skills
  references/sonar.md            former sonar-operations.md + quality-profile creation
  references/quality-policy.md   carried over, extended with agent-ergonomics metrics
  scripts/measure.py             the single L2+L3 measurer, called by all three skills
  scripts/guard.py               the file vendored into the target repository
  tests/test_measure.py
  tests/test_guard.py
  tests/fixtures/…
```

Skills reference plugin-root files as `<plugin-root>/references/<file>.md`, the convention already
used by `plugins/sp`.

`references/tools.md` is where tool-specific traps live so `DESIGN.md` does not carry them: the
`-ENS` requirement, the `-Tnd=` field name (`max_nested_structures`, not `nd`), the `-z` rename
form, the `--format` blank line, the gitwildmatch normalization, and the npx cache behaviour.

`SKILL.md` files stay in English, matching the current ones. `.codex-plugin` `interface` stays in
Russian, matching the current one. `README.md` and `DESIGN.md` are English, matching the repository.

## 14. Rename and migration

- `git mv plugins/sonar-code-health plugins/code-health`, keeping history.
- Both manifests: `name: "code-health"`, `version: "0.3.0"`, description rewritten for three modes
  and the non-Sonar layers. Codex `interface.displayName` → `Code Health`.
- `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`: the existing
  `sonar-code-health` entry is **renamed in place**, not appended — it is the same plugin, and the
  append-only rule covers new entries.
- `.cursor-plugin/marketplace.json` is not touched: this plugin has never been distributed for
  Cursor (confirmed — it has no `.cursor-plugin/plugin.json`), and adding it is out of scope.
- Root `README.md`: catalog row, install line, and the `### sonar-code-health` section rewritten for
  three commands.
- Commands become `/code-health:audit`, `/code-health:fix`, `/code-health:guard`. Codex keeps
  `$audit` / `$fix` / `$guard`.
- Cost: one reinstall. `/plugin uninstall sonar-code-health` then
  `/plugin install code-health@oofuzl`. Accepted. (The plugin subsequently moved out of
  `personal_ai_tools` into its own repository; §14 records the rename, not the move.)
- State moves from `~/.local/state/sonar-code-health/` to `~/.local/state/code-health/`. No
  migration: the ledger is disposable and a fresh audit regenerates it. `audit` mentions the old
  directory once if it exists, so the user can delete it.

## 15. Testing

`scripts/measure.py` and `scripts/guard.py` are ordinary code and get ordinary tests, written
first. `SKILL.md` files are instructions and get skill validation.

| Area | Tests |
|---|---|
| lizard CSV parsing | 12-column `-ENS` row; commas inside quoted `long_name`; empty output for a function-free file; a file whose only function spans the whole file |
| file enumeration | `git ls-files` path vs walk fallback; symlink dropped; NUL-byte binary dropped; oversize file counted in `summary.skipped` |
| pattern matching | `**/dist/**` matches both `dist/a.js` and `packages/web/dist/a.js` after normalization; `exclude` and `tests` do not overlap-swallow each other |
| threshold semantics | `value == warn` fires; `value == warn - 1` does not; `value == hard` fires at level `hard`; config override; unknown key rejected |
| token estimate | known char count → expected tokens; UTF-8 multi-byte source counted by characters, not bytes |
| near-duplicates | two identical functions → 1.0; renamed identifiers → high structural, lower lexical, still reported via the structural index; differing only in string constants → still matched structurally; Cyrillic identifiers survive tokenization; unrelated functions below threshold; a cluster of 8 wrappers → one cluster of 8 |
| Python `ast` pass | absolute, relative and `__init__` imports; a 3-node cycle; an unresolvable import counted; parameter-name tuples emitted |
| churn | `-z` rename folded onto the new path; brace form never reached; binary `-`/`-` rows; deleted file dropped; author identity present; history shorter than the window; no `.git` → `not_measured` |
| layer status | every tool absent → every layer `not_measured`, **exit 0**, valid JSON; `--layers derived` pulls in `size` and `churn` |
| ratchet | existing file grows → fail; shrinks → pass; new file under `warn` → pass, over `warn` → fail; new function over `warn` in an old file → fail; renamed function treated as new; threshold lowered in config tightens immediately |
| guard performance | 20 changed files within the budget |
| gate detection | `config.gate` wins over `make gate`; each of the five remaining sources; the ask fallback |
| anti-gaming | each of the five rules triggers on a crafted diff, including a diff touching `.pre-commit-config.yaml` |

Fixtures are small synthetic repositories built in a temp directory, never checked-in copies of real
code. Tests needing `lizard` skip cleanly when it is absent, so the suite runs anywhere.

## 16. Non-goals

- No CI configuration is generated. `guard` produces a pre-commit hook; wiring it into CI is the
  repository's decision.
- No automatic threshold tuning per repository. The `agent` profile is a stated opinion; overriding
  it is a deliberate, committed act.
- No cross-repository dashboard, trend history, or reporting service.
- `fix` never opens a pull request, never pushes, never merges.
- Languages beyond what `lizard` supports are not measured for size/complexity, and this is stated
  rather than approximated.

## 17. Acceptance criteria

The plugin is done when `/code-health:audit` on `jira-team-analytics` reproduces, with no manual
scripting, every number already established there by hand:

| Established by hand | Produced by | Where it appears |
|---|---|---|
| 22 files ≥1000 lines, 42.5 % of backend LOC | `summary.by_cut["1000"]` — quoted at a named cut | Scorecard `Size` row and a `Fix first` group |
| largest file 8974 lines ≈ 107k tokens | L2 file read + `chars_per_token` | Agent ergonomics, first bullet |
| 114 functions with ≥8 parameters, max 19 | lizard `param` vs `parameters.hard = 8`, `value >= hard` | one `Fix first` group, clustered by parameter-name set |
| 46 `S3776` violations | L1 with the extended profile (S3776 read at its built-in default) | Scorecard, and a group when not already covered by L2 complexity |
| quarterly cache protocol copied into 16 files | L2 near-duplicate pass, one cluster of 16 | one `Fix first` group |
| ≈59 % of findings are noise from three syntax-modernization rules | noise accounting | `Worth noting` |

Three criteria that are not numbers:

- The same audit completes with Docker stopped: `L1: not measured`, the §3 table printed, every
  non-Sonar row still produced, and no P0 claimed either way.
- The same audit completes on a token that lacks Administer Quality Profiles: `L1: partial`,
  `Sonar way` used, the permission named.
- `/code-health:guard install` followed by an intentional 50-line growth of an already-oversized
  file fails the commit, and the failure names the file, the baseline, and both escape hatches.

## 18. Open points for the reviewer

1. **Version number.** `0.3.0` proposed. A rename that breaks every existing command invocation is
   arguably `1.0.0`.
2. **Auto-commit in `fix`.** §9 reverses a v0.2.0 non-negotiable. The reasoning is that the
   unattended queue is the whole point of decision 3, but it is the one place where the plugin
   writes to git history on its own.
3. **Near-duplicate similarity band.** `lexical ≥ 0.60` is calibrated so the observed 64–75 %
   wrappers are caught. That is one repository's calibration; it may be noisy elsewhere and is
   configurable for that reason.
4. **One Sonar profile per project.** Correct, and it will accumulate profiles on the server — one
   per audited repository. The alternative is a shared profile with plugin-default thresholds that
   repo config cannot influence.
5. **`edit_fanout` earns the least of the four derived metrics.** It is kept because it was
   specified, and normalized against the repository's own commit size so it measures the file rather
   than the team's commit habits. The median is the intended denominator and the mean is a
   documented fallback: measured on this plugin's own history, 35 of 64 commits touch exactly one
   file, so the median other-files-per-commit is `0` and dividing by it collapsed all 79 ratios to
   `0.0` while `mean_co_changed` was genuinely non-zero — a column of zeros that still looks like
   data. `edit_fanout_basis` says which denominator was used, and a ratio that cannot be computed
   at all is `null`, never `0.0`. If it proves noisy on the first real audit, it is still the first
   thing to cut.
