---
name: audit
description: Measure repository code health for AI-agent work and return a compact prioritized report. Run local metrics (size, tokens, complexity, parameters, nesting, near-duplicates, import cycles, dead code, churn, comment noise) plus an optional SonarQube layer, mark every unavailable layer as not measured rather than silently dropping it, and write a debt ledger a fix session can resolve.
compatibility: Requires local shell access. lizard is installed automatically; Docker is optional and only enables the Sonar layer. Designed for Claude Code and Codex-compatible Agent Skills.
---

# Code Health Audit

Audit the target repository without changing its source, tests, build files, ignore files, or
Sonar/quality configuration. `scripts/measure.py` (local, always available) is the baseline of
every audit; SonarQube layers on top of it when reachable and is never required to produce a
useful report.

Use the repository/path supplied by the user. Otherwise use the current Git repository.

## Hard rules

- Read-only for the target repository. Never edit code, tests, build files, ignore files, Sonar
  config, or quality profiles inside it.
- Never improve a score by excluding real source code, disabling rules, changing thresholds,
  marking issues false-positive, or adding suppressions.
- Never transition from audit into code changes in the same session. If the user picks something
  to fix, return a fresh-session `/code-health:fix ...` handoff and stop.
- Never commit credentials. Do not ask the user to paste a Sonar token into chat; prefer
  `SONAR_TOKEN` or a local credential store.
- Preserve any pre-existing working-tree changes. Record `git status` before analysis and verify
  tracked/staged state is unchanged afterward. Never reset or stash user work automatically.
- Local measurement-tool provisioning (`lizard`, `jscpd`, `madge`, `vulture`) is **automatic**, via
  `measure.py --install-missing` — no per-tool prompt. Report what it installed; see step 2.
  `measure.py` probes only the tools the requested layers can actually use, so the `tools` block
  lists those and no others; a tool missing from the block was not needed, not silently skipped.
  `knip` is not among them: JS/TS dead code is `not_measured` because its output is not parsed
  (`<plugin-root>/references/tools.md`).
- Creating or starting the local SonarQube container is a **different, permission-gated** action:
  it is a persistent Docker artifact, not a project-scoped `npx`/`uv tool` install. If no usable
  container exists, ask once whether to install/create one before pulling images or creating it.
- If Docker itself is unavailable or unhealthy, do not install a container runtime implicitly.
  Report that concisely, mark `L1: not_measured` with the reason, and continue the audit on L2/L3
  alone — a missing or unhealthy Docker stops the Sonar layer, never the whole audit.
- Audit may write to the **Sonar server** (create the project, create/reconcile the quality
  profile) but never to the target repository. Disclose and confirm this once per server before
  the first write; later audits against the same server reconcile silently unless an actual write
  happens, which is then announced in the report (see step 10).

## 1. Resolve scope and baseline

1. Resolve the repository root and inspect its languages, build system, monorepo layout,
   generated/vendor directories, existing tests, CI commands, and coverage artifacts.
2. Record: current branch and `HEAD` SHA; whether the working tree is clean; existing
   tracked/staged changes; repository remote when available.
3. Derive a deterministic project key from the repository identity. Prefer the Git remote
   owner/repository; otherwise use the repository basename plus a short stable hash of its absolute
   path. Never require a project config file in the target repository (`.code-health/config.json`
   is optional and, when present, only overrides thresholds/excludes — see DESIGN §6).

## 2. Run the local measurer — always, first

`measure.py` is the L2+L3 baseline. Run it unconditionally, before touching Sonar, writing its
output somewhere **outside the target repository** (a fresh `mktemp -d`, never a path inside the
repo even if gitignored):

```bash
python3 <plugin-root>/scripts/measure.py --repo <repo> --install-missing --out <tmp>/measure.json
```

- **Exit code 0 means "produced a valid document"**, including when every optional tool is missing
  and every layer is `not_measured`. Findings never change the exit code. A non-zero exit is a
  usage error (bad `--repo`, unreadable config) — fix the invocation, do not read it as "the
  repository is unhealthy" or as an audit finding.
- Read the document's `tools` block and report it as **one line**: every tool with
  `available: true, installed_by_us: true` goes under "Installed for this audit"; every tool with
  `available: true, installed_by_us: false` goes under "Already present". For example:
  `Installed for this audit: jscpd@5.1.2, madge@8.0.0. Already present: lizard 1.23.0, vulture 2.7.`
  A tool that stayed `available: false` is not part of this line — its absence is carried instead
  by that layer's own `status`/`reason` (step 3, step 10).
- Keep the parsed `layers` and `findings` from this document; they feed grouping (step 7) and the
  report (step 10) regardless of whether Sonar ends up available.

## 3. Layer Sonar on top, when reachable

Follow `<plugin-root>/references/sonar.md`.

- Reuse a healthy existing SonarQube Community Build container when possible, even if its name
  differs. Start a stopped compatible container rather than creating another one.
- If none exists, ask once whether to install/create the local Community Build (see hard rules
  above). On approval, use persistent Docker volumes so later audits are cheap and state survives
  restarts.
- Wait for SonarQube to report a healthy/up state before analysis.
- If authentication is not configured, guide the user through the smallest one-time setup. Prefer a
  user token exposed through `SONAR_TOKEN`; never write it into the repository.
- If Docker is unavailable/unhealthy, or the user declines installation, or setup otherwise fails:
  set `L1: not_measured` with the reason and move straight to step 7. Do not retry indefinitely and
  do not block the rest of the audit on this layer.

### What is lost without L1

State this table in the report whenever `L1: not_measured` or `partial`, rather than letting the
missing rows imply the repository has no issues there:

| Scorecard row | Without Sonar |
|---|---|
| Reliability | `not measured` |
| Security + hotspots | `not measured` |
| Coverage | `not measured` (never `0 %`) |
| Maintainability rating / debt | `not measured` |
| Cognitive complexity | `not measured`; CCN (from L2 `lizard`) reported instead, labelled as such |
| Complexity (CCN), Size, Token cost, Duplication, Churn | measured (L2/L3) |
| Comment noise | measured (L2), independent of Sonar — but it has its own `not_measured` case; read `layers.comments.status`, step 10 |

**Consequence, said plainly, not implied:** a Sonar-less audit can **structurally never raise a
P0** — the P0 tier is defined by correctness/security evidence that only L1 produces. Say this in
the report rather than letting the absence of a P0 read as "nothing urgent."

## 4. Configure Sonar analysis for this repository

Choose the scanner and analysis parameters from the actual project, not a generic template.

- Compare the host architecture with the architecture of any scanner image before using it. A
  scanner image that does not match the host runs under emulation, where type-aware JS/TS analysis
  can stall indefinitely; in that case run the scanner natively and keep only the server
  containerised. See `<plugin-root>/references/sonar.md`.
- Prefer a build-system-specific official Sonar scanner when it materially improves analysis (for
  example .NET/build-integrated ecosystems); otherwise use the generic SonarScanner CLI or its
  Docker image.
- Pass configuration through CLI/environment/temporary files outside the repository. Do not create
  `sonar-project.properties` in audit mode.
- Include real maintained source. Exclude only evidence-backed generated, vendored, cache,
  dependency, build-output, minified, or coverage-output directories.
- Correctly classify tests where practical so test code does not distort source metrics.
- Do not hide legacy or difficult code just because it produces findings.
- Verify the resulting scope before trusting it: check the indexed file count and the
  excluded-sources line in the scanner log. Whole-repository copies left behind by tooling (agent
  worktrees, backups) and nested cache directories that root-anchored patterns miss will otherwise
  inflate every metric.

**Quality profile.** Follow `<plugin-root>/references/sonar.md` and DESIGN §12: extend the built-in
`Sonar way` profile per language (`create` → `change_parent` → `activate_rule` → `add_project`),
never `copy` it. On a `403` from insufficient permission, fall back to the built-in profile, mark
`L1: partial`, and print the reason and the permission needed — this is not a failure, it is the
honest state described in step 3's table.

**Coverage discipline.** Reuse existing coverage reports when they correspond to the current source
state. If the repository has an obvious documented/local-safe coverage command, it may be run; do
not invent or edit test configuration merely to obtain coverage. Rewrite report paths to be
repository-relative before import, and confirm in the scanner log that each report was actually
matched. Report coverage as `unavailable` per language when no report was matched — **never `0 %`**.

## 5. Decide whether to reuse or rerun Sonar

`measure.py` is cheap and local: always run it fresh (step 2). Sonar analysis is the expensive part
worth reusing. Reuse a previous Sonar analysis only when all of these hold:

- the working tree is clean;
- the latest successful Sonar analysis records the same revision as current `HEAD`;
- it is recent enough to be useful (default: within 60 minutes);
- the requested scope/configuration has not materially changed;
- the user did not explicitly request a fresh scan.

If the working tree has any source changes, rerun. If freshness cannot be proven, rerun rather than
guessing.

## 6. Run and collect Sonar

1. Run analysis and wait for the Compute Engine task to finish successfully; scanner exit alone is
   not enough.
2. Fetch through the Sonar Web API for the **whole analyzed codebase**, not only new-code gate
   conditions: quality gate status (one signal, not the verdict); unresolved issues with
   pagination; **security hotspots when the server provides them**; current measures (size,
   cognitive/cyclomatic complexity, duplication, coverage when available, maintainability/debt);
   file/component-level measures sufficient to identify complexity and duplication hotspots; latest
   analysis revision/time.
3. A green quality gate must never be reported as proof the repository is healthy if overall-code
   findings or metrics say otherwise.
4. Keep raw API detail out of the user-facing answer unless needed to diagnose a failure.
5. After analysis, verify the repository's tracked/staged state is identical to the baseline from
   step 1. Remove only temporary artifacts created by this audit when unambiguously known; never
   delete pre-existing files.

## 7. From findings to groups

Do not dump every finding. `measure.py` emits flat `findings`; Sonar emits flat issues/hotspots.
Turn both into ledger groups by likely root cause, using `<plugin-root>/references/quality-policy.md`
for priority and grouping judgment, plus this mapping (DESIGN §8):

| Kind | Grouped by | Typical group |
|---|---|---|
| `file_too_long`, `file_too_expensive` | subsystem/directory | "22 backend report files over the 1000-line cut" |
| `function_too_long`, `function_too_complex`, `too_deep_nesting` | the enclosing file when several fire there, otherwise the subsystem | "one 211-line, CCN-18 function" |
| `too_many_params` | the shared parameter-name **set** (below), not a raw count | "114 functions carrying the same 8-argument report context" |
| `near_duplicate`, `exact_clone` | the cluster, one group per cluster | "cache protocol copied into 16 files" |
| `import_cycle` | one group per cycle | "3-module cycle between reports, cache, and models" |
| `dead_symbol` | the owning module/package, never repo-wide | "24 unreferenced helpers in `legacy/`" — capped at `P2` |
| Sonar reliability/security issues | rule + subsystem | "3 blocker null-deref issues in the payment module" |
| `comment_noise` | subsystem/directory | "31 files carrying commented-out code under `legacy/`" — capped at `P2` |
| `hotspot`, `edit_fanout`, `symbol_collision` | **never their own group** | they re-rank the groups above and appear as evidence inside that group's `why` |

A `comment_noise` finding's `evidence.zero_coherence` lists comments that share
no word with the function they document. That is not a finding of its own and
never becomes a group. When such a function also carries a
`function_too_complex`, `function_too_long` or `too_deep_nesting` finding, name
the comment in that group's `why` as evidence: the code needed prose to be
understood, which is the group's own argument. When the function carries no
other finding, ignore the entry. This is the same rule already in force for
`hotspot`, `edit_fanout` and `symbol_collision`: a group made only of
re-ranking findings is forbidden (`<plugin-root>/references/ledger.md`,
"Re-ranking findings").

**Parameter-set clustering** for `too_many_params`: `measure.py`'s Python `ast` pass emits each
function's ordered parameter-name tuple; group functions by Jaccard ≥ 0.6 over their parameter-name
**sets**. Languages with no `ast` pass fall back to grouping by subsystem — say which was used.

Prefer this priority order when evidence supports it: correctness/reliability/security risk;
high-complexity or high-coupling code that is costly/risky to change; meaningful duplication and
maintainability clusters; coverage gaps around risky/central behavior; low-value stylistic smells.
State what share of findings comes from rules representing no engineering decision (generated code,
syntax-modernization rules) so the headline count is not mistaken for actionable work.

### Group identity: `gid` vs. label

Each group gets a content-derived `gid` (`"<kind-prefix>-" + sha1(kind + sorted subject paths)[:8]`)
and a display label `A1…An` assigned fresh, in priority order, by this run. **The ledger's `queue`
stores `gid`, never the label** — positional labels plus a queue preserved across audits would
silently point a resumed fix at a different problem: after a re-audit reshuffles priority order, a
pending `A5` from before now names whichever group happens to be fifth *this time*, and the
intended safeguard ("flag it if the referenced group is gone") never fires, because `A5` still
exists. Report labels to the user; write `gid` to the ledger.

## 8. Add bounded model review only when useful

Sonar/measure.py findings are the primary evidence. Add an "Agent review" section only if
inspecting the repository can materially improve the conclusion — several findings share one
architectural root cause, Sonar is green but the repository still shows obvious overengineering or
weak tests around critical logic, or an issue is likely a false positive. Inspect enough
surrounding code, call sites, tests, and module structure to support the claim. Limit this section
to **at most three** high-value observations, clearly labelled as model analysis rather than
scanner findings. Do not invent business requirements.

## 9. Persist the debt ledger

Write the ledger yourself, as JSON — there is no script for this step. Follow
`<plugin-root>/references/ledger.md` for the exact schema, location, and the group-identity and
locator-not-source-of-truth rules; do not restate the schema from memory.

- Location: `${XDG_STATE_HOME:-$HOME/.local/state}/code-health/<project-key>.json`. Create the
  directory if needed. If a `sonar-code-health/` directory exists alongside it from the previous
  plugin generation, mention it once so the user can delete it — do not migrate or delete it
  yourself.
- If a ledger already exists for this project, read it first and **carry its `queue` forward
  unchanged** — every item, in order, with its state and commit SHA untouched. Overwrite `audit` and
  `groups` with this run's fresh results; never touch `queue` items beyond preserving them.
- **If every item in that queue is `done`/`skipped`/`failed`, the queue is finished: drop it.**
  Write the new ledger with no `queue` key at all. Carrying a finished queue forward makes
  `/code-health:fix --resume` find a queue with nothing to resume, and leaves a stale
  `baseline_summary` from an ended cycle for the next one to compare against. An item in state
  `running` is not finished — a queue holding one is carried forward so `--resume` can recover it.
  See "Carrying the queue across an audit" in `<plugin-root>/references/ledger.md`.
- Store only locators (paths, function names, finding/issue/rule ids, metric values) — never
  source snippets, secrets, or raw API dumps.

## 10. Report for a human, not for a scanner

Reply in the user's language. Budget **≈60–80 lines** — raised from the previous 40–60 because this
report carries roughly 15 lines of genuinely new content (the provisioning line, three new
Scorecard rows, the Agent ergonomics section, per-layer status, noise accounting); do not shrink
back to the old budget, that just drops one of those silently.

1. **Provisioning** — the one-line tool-provisioning summary from step 2.
2. **Repository health** — one-line verdict based on overall findings/metrics, plus quality-gate
   status (if L1 ran), analyzed revision/time, and whether Sonar was reused or freshly scanned.
   When `L1: not_measured`/`partial`, say so with the reason here, not only in the Scorecard.
3. **Scorecard** — Reliability, Security, Maintainability, Complexity, Duplication, Coverage, plus
   the new **Size**, **Token cost**, **Churn** and **Comment noise** rows. Every row carries a
   plain-language status and an explicit `measured` / `not measured` tag; use the step-3 table for
   what to write when `not measured`.

   **The Comment noise row** reads `layers.comments.status` and never softens it:
   - `measured` — total `noise_comment_lines` and how many files carry any.
   - `partial` — the same numbers, plus the layer's `reason` and `not_measured_files` verbatim.
     Two causes are common and mean different things: files whose extension has no comment reader
     (Ruby, PHP, Lua, Objective-C — `<plugin-root>/references/tools.md`), and a repository with no
     measured Python file, where the `commented_out` class never ran at all and every file's
     `evidence.commented_out` is `null`.
   - `not_measured` — say so with the reason. A repository written entirely in a language with no
     reader here produces this, and the honest row is "not measured", never "no comment noise
     found".

   A `0` from a `not_measured` layer, and a `null` `evidence.commented_out`, are never reported as
   "clean" — that is the same defect this skill refuses for coverage and for JS/TS dead code.
4. **Fix first** — at most five grouped priorities, labelled `A1`, `A2`, ... . For each: `P0`
   /`P1`/`P2`/`P3`; short problem name; why it matters (including any hotspot/fan-out/collision
   evidence that moved its priority); approximate scope (issue count/files/modules); recommended
   remediation direction; enough rule/finding identity to select it without dumping every key.
5. **Hotspots** — only when useful, the few files/modules concentrating disproportionate
   complexity, duplication, or important findings. Not a file dump.
6. **Agent ergonomics** (new, ≤10 lines) — token cost (largest/most expensive files), symbol
   collisions, churn×complexity hotspots, edit fan-out; each tagged `measured`/`not measured`. This
   is model-context cost, not code quality — keep it separate from the Scorecard and from Fix
   first, per `references/quality-policy.md`'s "Agent-ergonomics metrics".
7. **Worth noting** — meaningful lower-priority patterns, false-positive candidates, or missing
   signals (e.g. unavailable coverage).
8. **Agent review** — optional, at most three observations (step 8).

Include the local Sonar dashboard URL when useful for drilling into details, and announce any
actual Sonar-server write (project created, profile created/reconciled) here per the permission
boundary in the hard rules.

Finish with one copyable handoff for a fresh session, using the display label:
`/code-health:fix A1`

Do not suggest fixing everything automatically. The user chooses the group to change. If they
choose one in this same session, provide only the fresh-session handoff; do not edit code here.
