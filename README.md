# code-health — technical debt for AI-agent work

A repository that grows past what a model can hold in its own attention degrades in a specific,
measurable way: functions too long to read in one pass, files too big to fit in context, parameter
lists that hide an implicit object, nesting that forces the agent to hold a stack of conditions in
mind, copy-pasted logic that drifts, import cycles, dead code, and files that keep changing anyway.
This plugin measures that, writes it down, fixes it, and then stops it from coming back. Three
verbs over one debt ledger and one ratchet:

```
audit  →  measures the repository, writes the debt ledger (groups A1…An)
fix    →  burns ledger entries, one or as a queue, with the project's own test gate after each
guard  →  freezes the current state as a baseline and refuses commits that make it worse
```

## Install

```text
/plugin marketplace add aartem1/oofuzl
/plugin install code-health@oofuzl
```

Codex reads the same repository through [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json).

## Commands

| Purpose | Claude Code | Codex |
|---|---|---|
| Measure and write the ledger | `/code-health:audit` | `$audit` |
| Fix a ledger group | `/code-health:fix A1` | `$fix A1` |
| Freeze a baseline and guard it | `/code-health:guard install` | `$guard install` |

### `audit`

- Runs local metrics (size, tokens, complexity, parameters, nesting, near-duplicates, import
  cycles, dead code, churn, comment noise) plus an optional SonarQube layer, without changing the
  repository.
- Marks every layer that could not run as `not measured`, with a reason, instead of dropping it
  silently — a Sonar-less audit says so rather than implying a clean bill of health.
- Writes a debt ledger, grouped and prioritized (`A1`, `A2`, …), that a `fix` session can resolve.

### `fix`

- Takes one group (`A3`) or a priority tier (`--p0`, `--p1`) as a queue, and supports `--resume`
  after an interruption.
- Revalidates the finding, makes the smallest change that addresses it, and runs the project's own
  test gate after every group — a red gate stops the cycle rather than continuing past it.
- Commits one group at a time. It never opens a pull request, pushes, or merges.

### `guard`

- Installs a git pre-commit hook and vendors a self-contained `.code-health/guard.py` into the
  repository, so colleagues and CI run the same check without installing this plugin.
- Freezes the current measurements as `.code-health/baseline.json` — new commits may not make a
  file or function worse than its own baseline entry.
- Checks only the files a commit actually touches, so it stays fast enough to run on every commit.

## What gets measured, and by whom

| Layer | Tools | Produces | Used by |
|---|---|---|---|
| L1 Sonar | a dedicated quality profile forked from `Sonar way` | reliability, security, coverage, maintainability rating, cognitive complexity | `audit` only — **optional** |
| L2 local | `lizard`, `jscpd`, `madge`, `vulture`, `git`, a Python AST pass, direct file reads | size, token cost, cyclomatic complexity, parameters, nesting, exact and near-duplicate clones, import cycles, dead code, churn | all three — **the only layer `guard` uses** |
| L3 derived | this plugin's own code over L2 | churn × complexity hotspots, symbol-name collisions, edit fan-out | `audit` only |

**L1 is optional.** Without Docker, the Sonar layer reports `not measured` with a stated reason and
the audit completes on local metrics alone. That has a real cost, stated rather than hidden:
reliability, security, coverage, maintainability rating, and cognitive complexity are Sonar-only —
nothing else in this plugin produces them. **A Sonar-less audit can therefore structurally never
raise a P0**, because the P0 tier is defined by evidence only Sonar produces. The report says this
plainly instead of implying the repository has no P0-level problems.

**Nesting depth is not one tool across all languages.** `lizard`'s nesting counter only resets
correctly on languages whose readers emit real block/statement delimiters (C/C++/C#, Java, JS/TS,
Go, Kotlin, Rust, Swift, Scala, PHP, Objective-C); indentation-based readers never emit those, so
for Python this plugin computes nesting depth itself from the same AST pass that measures imports
and symbols, and for languages neither path covers (Ruby, Lua) nesting is reported as unavailable
rather than as a wrong number.

## Default thresholds — profile `agent`

Calibrated for a model reading the code, not for a human. A threshold fires at `value >= threshold`.

| Metric | warn | hard |
|---|---|---|
| Lines in a file | 400 | 800 |
| Tokens in a file (estimate, `chars / 4`) | 8,000 | 15,000 |
| Lines in a function | 60 | 100 |
| Cyclomatic complexity | 15 | 25 |
| Cognitive complexity (Sonar only) | 15 | 25 |
| Parameters | 5 | 8 |
| Nesting depth | 3 | 4 |

Overridable per repository in `.code-health/config.json` — a deliberate, committed act, not a
per-run flag.

## Prerequisites

| Tool | Needed for | If missing |
|---|---|---|
| `lizard` | size, complexity, parameters, nesting for brace-delimited languages — the one hard requirement | installed automatically (`uv tool install` → `pipx` → a plugin-owned venv) |
| `git` | churn, and every churn-derived metric | already present on essentially every target repository |
| Docker + SonarQube | the L1 layer | `L1: not measured`, audit continues |
| `pre-commit` | activating the `guard` hook | `guard install` reports and stops before writing anything |

`jscpd`, `madge`, and `vulture` sharpen individual `audit` findings (exact clones, JS/TS import
graphs, Python dead code) and are installed the same way as `lizard` when missing; their absence
narrows one finding, not the whole audit. Each run probes only the tools its requested layers can
use. JS/TS dead code is reported as **not measured**: `knip` would be the tool for it, but this
plugin has no verified parser for its output and says so rather than reporting a clean result it
never read.

## Where state lives

- The **debt ledger** lives outside the repository, under
  `${XDG_STATE_HOME:-$HOME/.local/state}/code-health/<project-key>.json` — machine-specific, and
  it can hold Sonar keys.
- The **guard baseline** lives inside the repository, at `.code-health/baseline.json`, alongside
  the vendored `.code-health/guard.py` and an optional `.code-health/config.json`. The baseline
  **must be committed** — it is what every future commit is checked against, including in CI and
  for colleagues who never installed this plugin.

## What it deliberately does not do

- No CI configuration is generated. `guard` produces a pre-commit hook; wiring it into CI is the
  repository's own decision.
- No automatic threshold tuning per repository. The `agent` profile is a stated opinion; overriding
  it is a deliberate, committed act, not something the plugin infers.
- No cross-repository dashboard, trend history, or reporting service.
- `fix` never opens a pull request, never pushes, never merges.
- Languages beyond what `lizard` supports are not measured for size or complexity, and this is
  stated rather than approximated.
