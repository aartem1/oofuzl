---
name: fix
description: Burn debt-ledger groups, one or as a queue, with the project's own test gate after each. Revalidate the finding, dispatch a narrow subagent, run the narrow-to-broad check ladder and then the gate, reject score-gaming, commit one group at a time, and stop the cycle on a red gate. Supports --p0, --p1 and --resume.
compatibility: Requires a repository previously audited by the code-health audit skill. Designed for Claude Code and Codex-compatible Agent Skills.
---

# Fix Debt-Ledger Groups

```
/code-health:fix A3              one group
/code-health:fix A3 A5 A1        a queue in the given order
/code-health:fix --p0            every P0 in the ledger   (--p1 likewise)
/code-health:fix --resume        continue an interrupted queue
```

`fix` reads the debt ledger that `audit` wrote (`<plugin-root>/references/ledger.md`), re-measures
each selected group before touching it, dispatches a narrow subagent per group, and runs the
repository's own gate after every group so a red gate stops the cycle before it compounds. This is
the only skill in this plugin that writes code and commits.

## Hard rules

- Optimize for real code quality and preserved behavior, not a green score.
- Never use `NOSONAR`, ignore annotations/comments, exclusions, disabled rules, weaker quality
  profiles, raised thresholds, or test deletion as a shortcut. The one narrow exception (a
  demonstrated false positive with explicit per-occurrence user approval) is a stop-and-ask, never a
  subagent's own call — see the anti-gaming check (step 4).
- Do not refactor unrelated code merely because it is nearby.
- Do not reset, overwrite, stash, or discard pre-existing user changes.
- Do not push.
- Prefer the simplest design that resolves the root cause. Do not turn one understandable piece of
  code into unnecessary layers/helpers just to lower a metric.
- Preserve public behavior and contracts unless the selected finding inherently requires a behavior
  change and the intended behavior is clear.
- Work happens in the **main working tree, no worktree** — the gate needs the repository's installed
  dependencies, which an isolated worktree would not have.
- `fix` never turns into `audit`, and `audit` never turns into `fix`. The ledger is a locator, not a
  source of truth (`<plugin-root>/references/ledger.md`): when the stored analysis does not match
  the current source, `fix` runs its own read-only measurement inside the fix session rather than
  invoking the audit skill.

## One deliberate reversal of v0.2.0

v0.2.0 said "do not commit or push unless the user separately asks." **Queue mode commits, one
commit per group.** Without a commit there is no unit for the ratchet, no `--resume`, and no way to
leave the machine unattended for an hour — the reversal is deliberate, not a relaxation of caution.

Two v0.2.0 rules are **not** touched by this reversal and stay exactly as strict:

- *Do not reset, overwrite, stash, or discard pre-existing user changes.*
- *Do not push.*

The commit stages **only the exact paths the subagent touched** for this group — never `git add -A`,
never `git commit -a`. Any of those paths that was already dirty at cycle start makes the item abort
instead of committing: the plugin has no way to separate its own edit from the user's inside one
already-modified file, so it does not try.

## 1. Resolve the invocation and the queue

1. Parse the command into a selection: a single label, an ordered list of labels, `--p0`/`--p1`
   (every group at that priority in the ledger, in ledger order), or `--resume` (continue a queue
   already in progress).
2. Resolve the repository root and derive the same project key `audit` uses; load the ledger from
   `${XDG_STATE_HOME:-$HOME/.local/state}/code-health/<project-key>.json` and require its
   `repo_path` to match the current repository. A mismatch or a missing ledger stops here — ask the
   user to run `/code-health:audit` first rather than guessing at findings.
3. **Resolve every label to its `gid` against the current ledger's `groups`, before anything else.**
   Labels (`A1…An`) are a fresh, priority-ordered display convenience reassigned by every audit run;
   only `gid` is stable across audits. See "Group identity" in
   `<plugin-root>/references/ledger.md` — do not restate that schema here, follow it.
4. With no argument and no explicit selection already present in the conversation, **ask** which
   group(s) to fix. Never pick a large cleanup scope on the user's behalf.
5. If a label cannot be resolved — the ledger is missing, belongs to another repository, or the
   group no longer exists in `groups` — do not guess. Report which label failed and ask the user to
   re-audit or choose a different one.
6. Build `queue.items` in the resolved order, each starting `{gid, state: "pending"}`; for
   `--resume`, load the existing `queue.items` instead of building a new one. Persist the ledger
   immediately so a crash right after this point is itself resumable.
7. **`--resume` and a `running` item:** an item found in state `running` belonged to a session that
   crashed or was killed mid-item — a normal finish always leaves `done`, `failed`, or `skipped`.
   Report it, check its recorded files for uncommitted changes left behind by the crashed attempt,
   and let the user choose retry or skip. Never silently retry or silently skip it.

## 2. Cycle start, once

Run this once per invocation, before the first item, whether the queue has one group or many.

1. **Detect the gate** and confirm it once with the user, then store it in `queue.gate`. Try in
   order; first match wins:
   0. `config.gate` in `.code-health/config.json`, if set — the repository's own committed answer,
      used without asking.
   1. `make gate` — a `gate` Makefile target exists.
   2. `make test` — a `test` Makefile target exists.
   3. `npm test` — `package.json` has a `scripts.test` that is not the npm placeholder stub.
   4. `pytest` — pytest is configured (`pytest.ini`, `[tool.pytest]`, `tox.ini`, or a `tests/`
      directory with pytest available).
   5. `pre-commit run --all-files` — `.pre-commit-config.yaml` exists.
   6. Otherwise ask the user directly what command proves the repository still works.
   A gate already stored from an earlier cycle is reused by `--resume` without re-asking.
2. Record the current branch, `HEAD`, and the exact set of paths already dirty in the working tree.
   These recorded paths are what makes the per-item commit rule enforceable later: a path this
   record already lists as dirty may never be claimed by a group's commit.
3. **Run the gate once, before any edit**, and record any pre-existing failures. If the gate is
   already red, **stop the whole cycle here** — a ratchet measured against a red baseline proves
   nothing, and no amount of per-item care recovers that.
4. **Snapshot `measure.py`'s repo-wide `summary`** into `queue.baseline_summary`. This is the
   baseline the anti-gaming check (step 4) compares against; without it, "the code moved somewhere
   the per-file re-measure no longer looks" is undetectable from a per-group diff alone.

## 3. Per queue item

Repeat for each item in queue order until the queue is exhausted or the cycle stops.

1. **Re-measure this dimension only**: `measure.py --layers <this group's layers> --files <this
   group's files>`, with files resolved transitively as in the measurement layers
   (`<plugin-root>/references/metrics.md`). Confirm the finding is still alive by matching this
   group's `finding_ids`. If it is gone, mark the item `skipped` with the reason recorded and move to
   the next item — do not treat a stale ledger entry as still actionable.
2. **Establish the narrow safety baseline before editing**, the same ladder a single-group fix
   always used: run the narrowest existing tests covering the affected area; for a
   correctness/reliability finding, write a focused regression test that fails for the expected
   reason before the change and is kept as protection after it; for a behavior-preserving refactor,
   use the existing behavioral tests and add a characterization test only where the behavior is
   important and currently unprotected. Never invent a fake failing test. Never introduce a new test
   framework for this.
3. **Dispatch a subagent** with a narrow brief: this group, these files, this shape of change, touch
   nothing else. The subagent inherits every rule in Hard rules above and the anti-gaming check in
   step 4 below.
   **Escalation, restored from v0.2.0:** if resolving the finding correctly requires a consequential
   product or architecture decision with several reasonable outcomes, stop this item here and put
   the smallest such decision to the user instead of guessing. This matters most in queue mode,
   where a group can be as broad as "cache protocol copied into 16 files" and several designs are
   equally plausible.
4. **Escalate checks narrow → broad**: the focused/regression tests from the safety baseline above,
   then the affected module's/package's suite, then typecheck/build/lint, then the project gate
   detected in step 2. Attribute any failure already recorded as pre-existing in step 2 to the
   baseline, never to this fix.
5. **Re-measure this group's dimension again and run the anti-gaming check** (step 4 below),
   including comparing the repo-wide `summary` against `queue.baseline_summary` from step 2.
6. **Commit on a pass.** Stage only the exact paths the subagent touched for this group — never
   `git add -A`, never `git commit -a` (see "One deliberate reversal of v0.2.0" above). If any of
   those paths was already dirty at cycle start, abort this item instead of committing: mark it
   `failed`, leave the edits uncommitted, and continue to the next item rather than the whole cycle.
   On success, mark the item `done` with the commit SHA.
7. **A red gate stops the whole cycle.** This group's edits stay uncommitted; nothing already
   committed by an earlier item in this cycle is reverted. The human decides what happens next.

## 4. Anti-gaming check

Run against each group's diff, before its commit in step 3. Any hit ⇒ reject: edits stay
uncommitted, the item is marked `failed`, and the cycle stops (step 3).

1. Test files deleted; test functions removed; `skip` / `xfail` / `.skip` / `.only` /
   `pytest.mark.skip` added; assertions weakened to tautologies.
2. Any of `.code-health/config.json`, `.code-health/baseline.json`, `.code-health/guard.py`,
   `.pre-commit-config.yaml`, `sonar-project.properties`, or a lint configuration modified. The
   ratchet's own machinery is included deliberately — otherwise a subagent could neuter the hook and
   still get its commit through. Moving the ratchet is `guard update-baseline`, a separate, explicit
   command outside this cycle.
3. Suppressions added: `NOSONAR`, `# noqa`, `# type: ignore`, `eslint-disable`,
   `@SuppressWarnings`, `# pylint: disable`, `pragma: no cover`.
4. Source files deleted, or moved into an excluded/generated path, without an equivalent
   replacement.
5. The repo-wide `summary` (step 2's `queue.baseline_summary`) did not improve consistently with the
   local metric this group targeted — a sign the problem moved somewhere the measurer no longer
   looks, rather than actually shrinking.

**The false-positive exception, restored from v0.2.0.** Rule 3 is not absolute. A suppression is
permitted when the finding is demonstrably a false positive *and* the user explicitly approves it
for that specific occurrence. That approval is always a stop-and-ask put to the user — never a
subagent's own decision — and the suppression itself carries a comment naming the evidence for why
it is a false positive. Without this path, a fix that legitimately needs one `# type: ignore` could
never be committed at all.

## 5. Final announcement

Always finish with a compact, structured announcement in the user's language, understandable
without reading the implementation log. Do not use a table.

In queue mode, each finished group gets one short status line as it completes. The full
four-section announcement below is emitted once, either at the end of a cycle that ran to
completion, or immediately when the cycle stops (a red gate, an escalation, or an unresolved anti-
gaming hit).

Use exactly these section names when applicable:

**Исправлено** — one or two sentences per group naming the problem and the real root cause removed.

**Как исправлено** — 2–5 short bullets per group describing the design/implementation approach in
plain language. Explain the method, not a file-by-file changelog.

**Как проверено** — short bullets naming the checks that actually ran: the narrow safety baseline,
the escalation ladder up to the gate, the anti-gaming check, and the gate's own before/after result
for each group. State explicitly whether the group's finding disappeared and whether the gate ended
green or red.

**Осталось** — material residual risk, validation gaps, pre-existing failures, groups still
`pending` in the queue, and any item stopped for escalation or a red gate. Omit this section only
when there is nothing meaningful to report.

Keep the announcement concise. Do not paste raw scanner output, long lists of touched files, or
generic claims such as "everything is fine." Say only what was actually verified.

If a group should not be fixed because it is a false positive, or because every remediation would
make the design worse, use the same compact format and state that no code change was made for that
group, with the evidence. Do not manipulate Sonar or ratchet configuration automatically to reach
that conclusion.
