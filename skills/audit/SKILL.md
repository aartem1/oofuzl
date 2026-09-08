---
name: audit
description: Run a read-only code-health audit of a repository with a local SonarQube Community Build. Set up or reuse Sonar safely, choose project-appropriate analysis settings without editing the repository, reuse only genuinely fresh results, and return a compact prioritized report with bounded agent review for Sonar blind spots.
compatibility: Requires local shell access and Docker for automatic SonarQube setup. Designed for Claude Code and Codex-compatible Agent Skills.
---

# Sonar Code Health Audit

Audit the target repository without changing its source, tests, build files, quality configuration, or tracked repository state. Use SonarQube as the deterministic source of findings; add model judgment only where it improves prioritization or covers clear Sonar blind spots.

Use the repository/path supplied by the user. Otherwise use the current Git repository.

## Hard rules

- Audit mode is read-only for the target repository. Never edit code, tests, build files, ignore files, Sonar config, or quality profiles.
- Never transition from audit into code changes in the same session. If the user chooses something to fix, return a fresh-session `/code-health:fix ...` handoff and stop.
- Never improve a score by excluding real source code, disabling rules, changing thresholds, marking issues false-positive, or adding suppressions.
- Never commit credentials. Do not ask the user to paste a Sonar token into chat; prefer `SONAR_TOKEN` or a local credential store.
- Preserve any pre-existing working-tree changes. Record `git status` before analysis and verify tracked/staged state is unchanged afterward. Never reset or stash user work automatically.
- Installation is the only step that requires explicit permission: if no usable local SonarQube container exists, ask whether to install/create one before pulling images or creating the server container.
- If Docker itself is unavailable or unhealthy, report that prerequisite concisely and stop rather than installing a container runtime implicitly.

## 1. Resolve scope and baseline

1. Resolve the repository root and inspect its languages, build system, monorepo layout, generated/vendor directories, existing tests, CI commands, and coverage artifacts.
2. Record:
   - current branch and `HEAD` SHA;
   - whether the working tree is clean;
   - existing tracked/staged changes;
   - repository remote when available.
3. Derive a deterministic Sonar project key from the repository identity. Prefer the Git remote owner/repository; otherwise use the repository basename plus a short stable hash of its absolute path. Never require a project config file in the target repository.

## 2. Ensure local SonarQube

Follow `references/sonar-operations.md`.

- Reuse a healthy existing SonarQube Community Build container when possible, even if its name differs.
- Start a stopped compatible container rather than creating another one.
- If none exists, ask once whether to install/create the local Community Build. On approval, use persistent Docker volumes so later audits are cheap and state survives restarts.
- Wait for SonarQube to report a healthy/up state before analysis.
- If authentication is not configured, guide the user through the smallest one-time setup. Prefer a user token exposed through `SONAR_TOKEN`; never write it into the repository.

## 3. Configure analysis for this repository

Choose the scanner and analysis parameters from the actual project, not a generic template.

- Compare the host architecture with the architecture of any scanner image before using it. A scanner image that does not match the host runs under emulation, where type-aware JS/TS analysis can stall indefinitely; in that case run the scanner natively and keep only the server containerised. See `references/sonar-operations.md`.
- Prefer a build-system-specific official Sonar scanner when it materially improves analysis (for example .NET/build-integrated ecosystems); otherwise use the generic SonarScanner CLI or its Docker image.
- Pass configuration through CLI/environment/temporary files outside the repository. Do not create `sonar-project.properties` in audit mode.
- Include real maintained source. Exclude only evidence-backed generated, vendored, cache, dependency, build-output, minified, or coverage-output directories.
- Correctly classify tests where practical so test code does not distort source metrics.
- Reuse existing coverage reports when they correspond to the current source state. If the repository has an obvious documented/local-safe coverage command, it may be run; do not invent or edit test configuration merely to obtain coverage.
- Do not hide legacy or difficult code just because it produces findings.
- Verify the resulting scope before trusting it: check the indexed file count and the excluded-sources line in the scanner log. Whole-repository copies left behind by tooling (agent worktrees, backups) and nested cache directories that root-anchored patterns miss will otherwise inflate every metric.

## 4. Decide whether to reuse or rerun

Reuse a previous Sonar analysis only when all of these hold:

- the working tree is clean;
- the latest successful Sonar analysis records the same revision as current `HEAD`;
- it is recent enough to be useful (default: within 60 minutes);
- the requested scope/configuration has not materially changed;
- the user did not explicitly request a fresh scan.

If the working tree has any source changes, rerun. If freshness cannot be proven, rerun rather than guessing.

## 5. Run and collect

1. Run analysis and wait for the Compute Engine task to finish successfully; scanner exit alone is not enough.
2. Fetch through the Sonar Web API for the **whole analyzed codebase**, not only new-code gate conditions:
   - quality gate status, treated only as one signal;
   - unresolved issues with pagination;
   - security hotspots when available;
   - current measures, especially code size, cognitive/cyclomatic complexity, duplication, coverage when available, and technical-debt/maintainability measures supported by the server;
   - file/component-level measures sufficient to identify complexity and duplication hotspots;
   - latest analysis revision/time.
3. A green quality gate must never be reported as proof that the repository is healthy if overall-code findings or metrics say otherwise.
4. Confirm that any coverage report you supplied was actually matched, and treat languages with no report as unmeasured rather than uncovered.
4. Keep raw API detail out of the user-facing answer unless needed to diagnose a failure.
5. After analysis, verify the repository's tracked/staged state is identical to the baseline. Remove only temporary artifacts created by this audit when they are unambiguously known; never delete pre-existing files.

## 6. Prioritize intelligently

Use `references/quality-policy.md`.

Do not dump every issue. Group findings by likely root cause, rule, subsystem, and remediation shape. Prioritize by expected engineering value, not severity alone.

Prefer this order when evidence supports it:

1. correctness/reliability and security risks;
2. high-complexity or high-coupling code that is costly or risky to change;
3. meaningful duplication and maintainability clusters;
4. coverage gaps around risky/central behavior;
5. low-value stylistic or localized smells.

A large number of low-value findings must not obscure a small number of important ones.

## 7. Persist a small fresh-session handoff

After grouping priorities, save a compact local JSON handoff **outside the audited repository** so a new session can resolve a short selection such as `A1`.

Use `${XDG_STATE_HOME:-$HOME/.local/state}/code-health/` and a filename derived from the stable Sonar project key. Create the directory if needed.

Store only:

- schema version;
- project key and local repository path;
- current `HEAD`, clean/dirty state, branch, and audit timestamp;
- Sonar analysis revision/key when available;
- priority groups `A1`, `A2`, ... with title, priority, issue keys, rule keys, and affected file paths.

Never store source snippets, secrets, credentials, scanner tokens, or raw API dumps. This file is convenience metadata, not a source of truth: a fix session must revalidate against the current repository and Sonar before editing.

Overwrite only this plugin-owned latest-audit state for the same project; do not create an unbounded history by default.

## 8. Add bounded model review only when useful

Sonar is the primary evidence. Add an `Agent review` section only if inspecting the repository can materially improve the conclusion, for example:

- several Sonar findings appear to share one architectural root cause;
- Sonar is green but the repository still shows obvious overengineering, inappropriate dependency direction, misleading abstractions, or weak tests around critical logic;
- an issue is likely a false positive or a naive fix would make the design worse.

Inspect enough surrounding code, call sites, tests, and module structure to support the claim. Limit this section to at most three high-value observations. Clearly label them as model analysis rather than Sonar findings. Do not invent business requirements.

## 9. Report for a human, not for a scanner

Reply in the user's language. Keep the default report compact and scannable; prefer roughly 40–60 lines or less unless the user asks for details.

**Repository health** — one-line verdict based on overall findings/metrics, plus quality-gate status, analyzed revision/time, and whether the result was reused or freshly scanned.

**Scorecard** — a small table or compact bullets for Reliability, Security, Maintainability, Complexity, Duplication, Coverage (if available), each with a plain-language status rather than raw metrics only. Use overall-code data unless explicitly labeled otherwise.

**Fix first** — at most five grouped priorities. Assign stable-for-this-audit IDs `A1`, `A2`, ... . For each include:
- `P0`/`P1`/`P2`/`P3`;
- short problem name;
- why it matters;
- approximate scope (issue count/files/modules);
- recommended remediation direction;
- enough Sonar rule/issue identity to understand what is selected without dumping every key.

**Hotspots** — only when useful, list the few files/modules that concentrate disproportionate complexity, duplication, or important findings. Do not turn this into a file dump.

**Worth noting** — only meaningful lower-priority patterns, false-positive candidates, or missing signals such as unavailable coverage.

**Agent review** — optional, maximum three observations.

Include the local Sonar dashboard URL when it is useful for drilling into details.

Finish with one copyable handoff for a fresh session, for example:
`/code-health:fix A1`

Do not suggest fixing everything automatically. The user chooses the problem/group to change. If they choose one in this same session, provide only the fresh-session handoff; do not edit code here.
