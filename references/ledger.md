# The debt ledger

Shared by all three skills: `audit` writes it, `fix` reads and updates it, `guard` does not touch it
(the ratchet has its own, separate baseline — `.code-health/baseline.json` inside the target repo,
not this file).

## Location

```
${XDG_STATE_HOME:-$HOME/.local/state}/code-health/<project-key>.json
```

**Outside the target repository.** It is machine-specific and holds Sonar host/analysis keys —
data that has no business being committed to the repository it describes, and that would be wrong
for a second machine or a second contributor to inherit verbatim.

## Schema

```jsonc
{
  "schema": 1,
  "project_key": "github:owner:repo",
  "repo_path": "/abs/path",
  "audit": {
    "at": "2026-09-08T10:00:00Z", "head": "abc123", "dirty": false, "branch": "main",
    "layers": { "L1": "measured", "L2": "measured", "L3": "partial" },
    "summary": { /* verbatim copy of measure.py's summary -- the repo-wide anti-gaming baseline */ },
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

## Group identity: `gid` versus `label`

`gid` is **content-derived**: `"<kind-prefix>-" + sha1(kind + sorted subject paths)[:8]`. Finding
ids follow the same shape where a finding has no single subject path: a near-duplicate cluster's id
is `near_duplicate:cluster/<sha1 of the cluster's sorted `file:function` members>[:8]`, matching the
example above. "Stable across runs" means *the same code always produces the same id* — it is not a
claim that an id survives the code changing. A cluster that gained or lost a member is a different
cluster, and its id says so, which is exactly what makes a group that genuinely vanished detectable
instead of silently substituted. What the id must never depend on is anything other than content:
the first form of this id keyed on `members[0].filename` from an unsorted list, so two runs over
identical code could disagree. `label`
(`A1…An`) is a **display convenience only** — a fresh, priority-ordered assignment made by every
`audit` run, not a stable identifier.

**The queue stores `gid`, never `label`.** Positional labels plus a preserved queue across audits is
silent data corruption: after a re-audit reshuffles priority order, a pending `A5` from before the
re-audit now points at whichever group happens to be fifth *this time* — a different problem
entirely — and the intended safeguard ("report if the referenced group no longer exists") never
fires, because `A5` still exists; it is simply not the group it used to be. With content-derived
`gid`, a group that genuinely disappeared between audits is detectable as a missing key, and is
reported as such rather than silently substituted.

## Re-ranking findings: `hotspot`, `edit_fanout`, `symbol_collision`

These three L3 metrics **never form their own group** — stated in DESIGN §8, `metrics.md`,
`quality-policy.md` and the audit skill, and it stands. That rule is about group *creation*, not
about reference, and the difference matters: `measure.py` emits `symbol_collision` findings, and a
finding no group may ever name is inert — it can never be revalidated by `fix`, and the evidence in
a group's `why` has nothing to point at.

**How they are referenced.** A re-ranking finding's id may appear in an existing group's
`finding_ids` when that group's own remediation also resolves it — the extraction that removes the
duplicate cluster is also what stops three files defining `build_payload` — and when it does, the
group's `why` must cite it in words, because that is the whole reason it is there. It never appears
alone: a `finding_ids` list consisting only of re-ranking findings is a group these metrics were
forbidden from creating, written a different way. A collision that no group's remediation touches
stays out of `finding_ids` entirely and is reported in the audit's **Agent ergonomics** section,
which is where the un-actioned L3 evidence lives.

Consequence for `fix`: a group carrying such an id re-measures the `derived` layer along with its
own (see the kind → layer table in `<plugin-root>/references/metrics.md`), so the collision is
revalidated with everything else rather than trusted from the ledger.

## Queue item state and `running` recovery

`state` is one of `pending | running | done | failed | skipped`.

An item found in state `running` at the start of a `--resume` belonged to a session that crashed or
was killed mid-item — it did not finish normally, because a normal finish always transitions to
`done`, `failed`, or `skipped` before the process exits. `--resume` must never silently retry or
silently skip such an item: it is reported to the user, its files are checked for uncommitted
changes left behind by the crashed attempt, and the user chooses retry or skip. Guessing either way
on the user's behalf is the one thing this state exists to prevent.

## Locator, not source of truth

The ledger never stores source snippets, tokens, credentials, or raw API response dumps — only
locators: file paths, function names, finding ids, metric values, and Sonar issue/rule keys. It is a
**locator, not a source of truth**: `fix` always re-measures the group's files before editing rather
than trusting the ledger's snapshot values, because the repository may have changed since the audit
that wrote them (including via an earlier item in the same queue). One file per project; each audit
overwrites it, carrying forward whatever `queue` was not yet fully done.

## Carrying the queue across an audit

An `audit` that finds an existing ledger **carries its `queue` forward unchanged** — every item, in
order, with its state, commit SHA and note untouched — and overwrites only `audit` and `groups`.

**Unless every item is already `done`, `skipped` or `failed`.** That queue is finished, and the
correct action is to *drop* it: write the new ledger with **no `queue` key at all**. Carrying a
finished queue forward makes `/code-health:fix --resume` find a queue with nothing to resume and
report it as an interrupted run, and it leaves a stale `baseline_summary` from a cycle that ended
around to be compared against by the next one. A finished queue is history, and this file is a
locator for present work, not a log — the commits the queue produced are the record.

An item in state `running` is **not** finished (see below); a queue holding one is carried forward
so `--resume` can recover it.
