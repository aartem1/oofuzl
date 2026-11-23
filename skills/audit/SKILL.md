---
name: audit
description: Measure repository code health for AI-agent work and return a compact prioritized report. Run local metrics (size, tokens, complexity, parameters, nesting, near-duplicates, import cycles, dead code, churn, comment noise) plus SonarQube, which is required: Docker and the SonarQube container are brought up first and the audit stops if Sonar cannot be made to work. Explains every Sonar rating and how to raise it honestly, and writes a debt ledger a fix session can resolve.
compatibility: Requires local shell access and an installed Docker (started automatically on macOS, never installed). lizard, jscpd, madge, vulture, the SonarQube image and the scanner are brought to their latest stable release automatically. Designed for Claude Code and Codex-compatible Agent Skills.
---

# Code Health Audit

Audit the target repository without changing its source, tests, build files, ignore files, or
Sonar/quality configuration. Two sources make one report: `scripts/measure.py` (local metrics) and
SonarQube (reliability, security, coverage, maintainability). **Sonar is a precondition, not an
optional layer:** an audit without it is not worth reading, so the audit brings Sonar up first and
stops — with the reason and the one command that fixes it — if it cannot. There is no partial
report and no `not measured` for a Sonar row.

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
  `measure.py --update-tools`, which also brings each one to its latest stable release — no
  per-tool prompt. Report what it installed or upgraded; see step 3.
  `measure.py` probes only the tools the requested layers can actually use, so the `tools` block
  lists those and no others; a tool missing from the block was not needed, not silently skipped.
  `knip` is not among them: JS/TS dead code is `not_measured` because its output is not parsed
  (`<plugin-root>/references/tools.md`).
- Starting Docker, pulling the SonarQube image and creating/starting/upgrading its container are
  part of running the audit — no separate permission step (`scripts/sonar_ready.py`, step 2).
  Installing Docker or any other runtime is never done: a missing Docker stops the audit with a
  one-line instruction.
- Any Sonar step that fails stops the audit: say the reason and the one command that fixes it, and
  produce no report. Never fall back to a Sonar-less audit, never mark a Sonar row `not measured`.
- Audit may write to the **Sonar server** (create the project, create/reconcile the quality
  profile) but never to the target repository. On the local server `sonar_ready.py` brought up this
  needs no confirmation; on any other server (a `SONAR_HOST_URL` that is not `localhost`) disclose
  and confirm once before the first write. Every actual write is announced in the report (step 10).

## 1. Resolve scope and baseline

1. Resolve the repository root and inspect its languages, build system, monorepo layout,
   generated/vendor directories, existing tests, CI commands, and coverage artifacts.
2. Record: current branch and `HEAD` SHA; whether the working tree is clean; existing
   tracked/staged changes; repository remote when available.
3. Derive a deterministic project key from the repository identity. Prefer the Git remote
   owner/repository; otherwise use the basename of the main working tree plus a short stable hash of
   its absolute path. The main working tree is the path on the first `worktree` line of
   `git worktree list --porcelain`: it is the same from every worktree of
   the repository, so an audit run in a worktree and a fix run in the main tree find one ledger.
   Record that path as the ledger's `repo_path`. Never require a project config file in the target repository (`.code-health/config.json`
   is optional and, when present, only overrides thresholds/excludes — see DESIGN §6).

## 2. Bring Sonar up — before any measuring

```bash
python3 <plugin-root>/scripts/sonar_ready.py --scanner-cache "${XDG_CACHE_HOME:-$HOME/.cache}/code-health/sonar-scanner" \
  > <tmp>/sonar.json
```

Run it in the background, or with the longest command timeout the harness allows, and wait for
it: starting Docker, pulling the image and an upgrade with migration can take several minutes. Do
not kill it midway — an upgrade cut short is recovered by the next run (the database is saved
before the server is recreated), but this audit has nothing to measure until it ends.

It does, in order, and prints one JSON document without secrets:

1. Docker: no `docker` CLI — stop (it never installs Docker). Daemon not answering — on macOS
   `open -a Docker` and a bounded wait (180 s); elsewhere stop with the start command.
2. SonarQube: pulls the newest `sonarqube:community` and runs it as `code-health-sonarqube` on
   PostgreSQL (`code-health-sonar-db`), both on persistent volumes — the embedded H2 database
   cannot be upgraded, so a server kept at the latest release needs a real one. Reuses or starts
   that pair, creates it when missing, waits for `UP` (300 s). On a newer image it dumps the
   database, recreates the server, runs the migration the server asks for, and on a failed
   migration restores the dump and the previous image. A user's own SonarQube on an external
   database is used as is; an old embedded-database container is stopped, never deleted. A failed
   pull keeps the installed version.
3. Token: `SONAR_TOKEN`, else `~/.config/code-health/sonar-token`, else the older
   `~/.config/sonar-code-health/token`, each checked against the server. None valid — a fresh server
   (`admin:admin`) gets a random admin password and a new token, both saved with mode 600 under
   `~/.config/code-health/`; a stored admin password mints a new token.
4. Scanner: the newest native SonarScanner CLI for this platform, cached outside any repository.

`SONAR_HOST_URL` set to a server other than localhost is someone else's server: the script only
checks that it is `UP` and that a token works — no Docker, no upgrade, no admin password, no token
minting. Writes to it (project, profile) still need the user's confirmation (hard rules).

Exit 0: keep `host`, `container`, `version`, `token_file` (null when the token came from
`SONAR_TOKEN`), `scanner`, `scanner_version`, `actions`, `notes`. Use the token only as
`SONAR_TOKEN="$(cat <token_file>)"` inside a command — never print it. Report `actions` (Docker
started, server upgraded, token minted) and every line of `notes` in the provisioning line.

Exit 1: the stderr already says «Sonar не готов, аудит остановлен: …» and «Исправить: …». Relay
those two lines to the user and stop — no measuring, no report, no ledger write.

## 3. Run the local measurer

`measure.py` is the L2+L3 baseline. Run it after step 2, writing its
output somewhere **outside the target repository** (a fresh `mktemp -d`, never a path inside the
repo even if gitignored):

```bash
python3 <plugin-root>/scripts/measure.py --repo <repo> --update-tools --out <tmp>/measure.json \
  --brief 10 --compare-history "${XDG_STATE_HOME:-$HOME/.local/state}/code-health/<project-key>.history.jsonl" \
  --tool sonarqube=<version> --tool sonar-scanner=<scanner_version>
```

`--compare-history` reads the previous audit's line in the history file next to the ledger (date,
`HEAD`, size totals, layer statuses, findings per kind and level, tool versions) and appends
nothing; this run's line is appended in step 9, once the Sonar analysis succeeded, so an audit that
stopped never becomes the next one's baseline. `--tool` passes the SonarQube server and scanner
versions from step 2, so a server upgrade is named next to the local tools. When the file already
has earlier lines, say in one sentence how the counts moved since the previous audit.

`--brief 10` prints a short document to stdout: tools, layer statuses, `summary`, `counts` (findings
per kind and level) and the ten worst findings of each kind (`hard` first, then the largest value).
Read that first; open `<tmp>/measure.json` for the rest of a kind only when grouping needs it.

- **Exit code 0 means "produced a valid document"**, including when every optional tool is missing
  and every layer is `not_measured`. Findings never change the exit code. A non-zero exit is a
  usage error (bad `--repo`, unreadable config) — fix the invocation, do not read it as "the
  repository is unhealthy" or as an audit finding.
- Read the document's `tools` block and report it as **one line** with exact versions: what was
  installed or upgraded for this audit (`updated: true`, with `previous_version → version`), what
  was already current, and one short clause per `update_error` (the installed version was kept).
  Add the SonarQube server and scanner versions from step 2. For example:
  `Инструменты: lizard 1.17.10 → 1.18.0, jscpd 5.4.1 (актуальная), SonarQube 26.8, сканер 8.1.0.5000; vulture не обновился (PyPI недоступен), остался 2.16.`
  A tool that stayed `available: false` is not part of this line — its absence is carried instead
  by that layer's own `status`/`reason` (step 10).
- When `--brief` reports tool versions that changed since the previous audit, the «what changed»
  sentence names them for the rows they feed, so a tool upgrade is not read as the code getting
  better or worse.
- Keep the parsed `layers` and `findings` from this document; they feed grouping (step 7) and the
  report (step 10).

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
never `copy` it. A `403` means the token lacks **Administer Quality Profiles**: stop the audit with
that reason and the fix (a token of a user with that permission — the admin token `sonar_ready.py`
mints has it). Do not fall back to the built-in profile.

**Coverage is fed to Sonar whenever the repository can produce it.** Reuse existing coverage reports
when they correspond to the current source state. Otherwise run the repository's own documented
test command with coverage (`pytest --cov`, `vitest --coverage`, `jest --coverage`, a `make` target)
with the report written **outside the repository** (`--cov-report=xml:<tmp>/coverage.xml`,
`COVERAGE_FILE=<tmp>/.coverage`, `--coverage.reportsDirectory=<tmp>/…`); do not invent or edit test
configuration. Rewrite report paths to be repository-relative before import, and confirm in the
scanner log that each report was actually matched. Only when the repository has no runnable test
command with coverage, or it needs services the machine does not have, the Coverage row says exactly
that — never `0 %`, never a bare «not measured».

## 5. Decide whether to reuse or rerun Sonar

`measure.py` is cheap and local: always run it fresh (step 3). Sonar analysis is the expensive part
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

### Explain every rating

```bash
SONAR_TOKEN="$(cat <token_file>)" python3 <plugin-root>/scripts/sonar_ratings.py \
  --host <host> --project <project-key> > <tmp>/ratings.json
```

For reliability, security, maintainability, security review, coverage and duplication it gives the
letter (or percent), the issues that set it (`driven_by`: the letter is the worst severity present,
so these exact issues decide it), the counts by severity, and `next`: the letter after they are
gone and what that takes. A `mismatch` means Sonar's letter disagrees with the open issues — say so.

Decide for every `driven_by` issue, after reading the code at its locator, which of three it is:

- **Real problem** — it belongs to a fix group like any finding.
- **Misleading** — the rule fires on a correct pattern (a framework idiom, generated or test code
  analysed as source). The group's remediation is the architecturally correct cure of the cause:
  change the code pattern the rule trips on; a rule or profile setting kept in the repository's
  Sonar configuration with a written reason; or a correct scanner setup (sources/tests split,
  generated code, coverage report paths). Name which one and why.
- **Scanner setup** — the letter comes from what was analysed, not from the code (tests counted as
  source, vendored code, a coverage report that matched nothing). The fix is the scanner setup.

Never game the letter: no blanket `NOSONAR`, no deleting tests, no disabling a rule without a
written reason, no excluding real code, no marking issues false-positive in the Sonar UI. The
audit itself changes nothing — the decision goes into the group. The goal is a letter that shows
the real state.

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

**Rating groups come first.** A group whose remediation removes the issues in some rating's
`driven_by` carries `rating: {metric, from, to}` (from `ratings.json`: current letter and `next`
letter) and is ranked before every other group of its priority tier — a P1 that raises a rating
goes ahead of other P1s.

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
- Append this run's history line:
  `python3 <plugin-root>/scripts/measure.py --record-history <the history file from step 3> --from
  <tmp>/measure.json --tool sonarqube=<version> --tool sonar-scanner=<scanner_version>`.
- Record in `audit.tools` the exact version of every measuring tool, the SonarQube server and the
  scanner, and in `audit.ratings` the letter of each Sonar rating. A rating group carries
  `rating: {metric, from, to}` so `/code-health:fix` re-checks that letter after the change.

## 10. Report for a human, not for a scanner

Reply in the user's language. Budget **≈60–80 lines** — raised from the previous 40–60 because this
report carries roughly 15 lines of genuinely new content (the provisioning line, three new
Scorecard rows, the Agent ergonomics section, per-layer status, noise accounting); do not shrink
back to the old budget, that just drops one of those silently.

1. **Provisioning** — the one-line summary from steps 2 and 3: what `sonar_ready.py` did (Docker
   started, server upgraded, token minted), exact tool, server and scanner versions, any update
   that failed.
2. **Repository health** — one-line verdict based on overall findings/metrics, plus quality-gate
   status, analyzed revision/time, and whether Sonar was reused or freshly scanned.
3. **Scorecard** — Reliability, Security, Maintainability, Complexity, Duplication, Coverage, plus
   **Size**, **Token cost**, **Churn** and **Comment noise**. Every row carries a plain-language
   status. Sonar rows are always measured (the audit stops otherwise); Coverage without a runnable
   coverage command says so in words. Cognitive complexity comes from Sonar, CCN from `lizard`,
   each under its own name.
4. **Ratings** — for each Sonar rating below A: the letter, the issues that set it (rule, file:line,
   how many), the verdict on them (real / misleading / scanner setup, from step 6) and what moves it
   to the next letter. One or two lines per rating; the full issue list stays in the ledger.

   **The Comment noise row** reads `layers.comments.status` and never softens it:
   - `measured` — read `layers.comments.totals`: `noise_comment_lines` and how many files carry
     any; `ticket_ref` lines and `files_with_ticket_ref` on their own, because a bare tracker key is
     the shape a reader recognises and the one `guard` ratchets; `history_note` lines as evidence
     ("N comment lines tell history rather than meaning; not counted"); and comment density as
     `comment_lines / lines` repo-wide, plus the two or three files with the highest
     `evidence.density.ratio` when it exceeds 0.5. Density is context, never a verdict — say so
     rather than call a well-commented file noisy.
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
5. **Fix first** — at most five grouped priorities, labelled `A1`, `A2`, ... , rating groups first.
   For each: `P0`/`P1`/`P2`/`P3`; short problem name; for a rating group the expected letter after
   the fix («надёжность E → D»); why it matters (including any hotspot/fan-out/collision evidence
   that moved its priority); approximate scope (issue count/files/modules); recommended
   remediation direction; enough rule/finding identity to select it without dumping every key.
6. **Hotspots** — only when useful, the few files/modules concentrating disproportionate
   complexity, duplication, or important findings. Not a file dump.
7. **Agent ergonomics** (≤10 lines) — token cost (largest/most expensive files), symbol
   collisions, churn×complexity hotspots, edit fan-out; each tagged `measured`/`not measured`. This
   is model-context cost, not code quality — keep it separate from the Scorecard and from Fix
   first, per `references/quality-policy.md`'s "Agent-ergonomics metrics".
8. **Worth noting** — meaningful lower-priority patterns, false-positive candidates, or missing
   signals (e.g. no runnable coverage command).
9. **Agent review** — optional, at most three observations (step 8).

Include the local Sonar dashboard URL when useful for drilling into details, and announce any
actual Sonar-server write (project created, profile created/reconciled) here.

Finish with one copyable handoff for a fresh session, using the display label:
`/code-health:fix A1`

Do not suggest fixing everything automatically. The user chooses the group to change. If they
choose one in this same session, provide only the fresh-session handoff; do not edit code here.
