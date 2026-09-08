# Sonar operations playbook

Use the installed SonarQube Web API documentation at `/web_api` as the source of truth when endpoints differ from these examples. Sonar is migrating endpoints toward Web API v2.

## Discover a local server first

1. Require a working Docker-compatible CLI: `docker info` must succeed. Do not install Docker Desktop, Colima, OrbStack, or another container runtime implicitly.
2. Inspect existing containers before creating anything:
   - `docker ps -a --format '{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Ports}}\t{{.Status}}'`
3. Prefer an existing SonarQube Community Build instance that is reachable and healthy. Check `http://localhost:9000/api/system/status` and, when authenticated, `/api/system/health`.
4. Start a stopped compatible container rather than creating a duplicate.

## First-time local installation

Only after explicit user approval:

- Prefer the official `sonarqube:community` image for a personal local installation.
- Use persistent named volumes so settings and analysis history survive container restarts:
  - `sonarqube_data:/opt/sonarqube/data`
  - `sonarqube_extensions:/opt/sonarqube/extensions`
  - `sonarqube_logs:/opt/sonarqube/logs`
- A practical personal-local container is conceptually equivalent to:

```bash
docker run -d \
  --name sonarqube \
  -p 9000:9000 \
  -e SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true \
  -v sonarqube_data:/opt/sonarqube/data \
  -v sonarqube_extensions:/opt/sonarqube/extensions \
  -v sonarqube_logs:/opt/sonarqube/logs \
  sonarqube:community
```

Use the embedded database only for this local personal-analysis use case. Do not represent it as a production deployment. Reuse an existing externally configured database instead of replacing it.

Wait until `/api/system/status` reports the server is ready before continuing. Surface startup failures from container logs rather than retrying blindly.

## Authentication

- Default URL: `http://localhost:9000`, unless an existing local instance uses another explicit URL.
- Prefer `SONAR_HOST_URL` and `SONAR_TOKEN` environment variables.
- Never put tokens in command history when a safer environment mechanism is available, and never write tokens into the audited repository.
- If no token exists, ask the user to complete the one-time local authentication/token setup. Do not ask them to paste the token into the conversation.
- Authenticate API requests with `Authorization: Bearer $SONAR_TOKEN`.

A workable pattern for a personal local server, when the user prefers not to run the setup by
hand: rotate the default credentials and mint the token through the API, store both in a
user-level config directory outside the audited repository (`~/.config/<tool>/`, `chmod 600`), and
reference them in later commands through `$(cat <file>)` so the secret never appears literally in
a command line or shell history. Tell the user where the files are.

Note that a SonarQube instance enforces a password policy on the rotation: a generated password
without at least one special character is rejected with
`400 Password must contain at least one special character`.

## Project identity

Prefer a stable key so repeated audits accumulate history:

1. Normalize the Git remote `owner/repo` into a Sonar-safe key, for example `github:aartem1:project`.
2. If there is no remote, use `local:<repo-name>:<stable-short-path-hash>`.
3. Reuse an existing project with that key.
4. Create it through the supported Web API only if needed and the scanner cannot create it automatically under the current permissions.

Never use branch names, timestamps, or random IDs as the project key.

## Host architecture before anything else

Check the host architecture against the architecture of every container image you are about to
run, and do it before the first analysis rather than while debugging one.

```bash
uname -m                                  # host: arm64 / aarch64 / x86_64
docker manifest inspect <image> | head -40 # look for a manifests[] list with your arch
```

A single-arch manifest (no `manifests[]` array) means the image exists for one architecture only.
Running it on a different host silently goes through emulation (QEMU/Rosetta), which the scanner
reports in its own log as `Detected os: ... arch: amd64` and `Platform: LINUX_X64`.

This matters concretely: **`sonarsource/sonar-scanner-cli` is published for amd64 only.** On an
arm64 host the JVM sensors merely run slow, but the plugin's embedded Node.js runtime becomes
unusable for type-aware JS/TS analysis — the run stalls indefinitely after
`Found N tsconfig.json file(s)` and eventually reports a bare `EXECUTION FAILURE` with no error
line. Adding memory or moving files to faster storage does not help, because neither is the cause.

On a host whose architecture the scanner image does not match, **run the scanner natively and keep
only the server in a container.** The server tolerates emulation; the scanner does not.

- Verify the native prerequisites first — a JRE and, for JS/TS, Node.js, both built for the host
  architecture: `java -XshowSettings:properties -version 2>&1 | grep os.arch` and
  `node -p "process.platform + '/' + process.arch"`.
- Official CLI builds are published per platform at
  `https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-<version>-<platform>.zip`
  (for example `macosx-aarch64`, `linux-x64`, `linux-aarch64`), plus a JRE-less generic `.zip`.
  Probe the URL with `curl -I` before downloading; unpack into a temporary directory outside the
  audited repository instead of installing a package manager formula.
- Point the plugin at the native runtime explicitly with `sonar.nodejs.executable` when several
  Node installations exist.
- A native scanner also reads the working copy directly, which removes the container filesystem
  overhead described under *Analysis parameters*.

## Scanner selection

Inspect the repository and use the official scanner path that best fits it:

- Maven project: SonarScanner for Maven when appropriate.
- Gradle project: SonarScanner for Gradle when appropriate.
- .NET: SonarScanner for .NET.
- NPM/JS/TS where the existing build naturally supports it: SonarScanner for NPM can be considered.
- Otherwise: generic SonarScanner CLI, installed locally or run from an official scanner container.
- On a host architecture the scanner image is not built for, always prefer the native CLI over the
  container — see *Host architecture before anything else*.

Do not add scanner dependencies to the target project's package/build files solely for the audit.

If a scanner itself runs in Docker on macOS, remember that `localhost:9000` inside that scanner container is not the host SonarQube port. Use the Docker host address appropriate to the runtime (commonly `http://host.docker.internal:9000`) while keeping the user-facing Sonar URL at `http://localhost:9000`.

## Analysis parameters

Pass parameters through scanner CLI/environment or a temporary file outside the repository.

Always set/derive:

- `sonar.projectKey`
- `sonar.projectName`
- `sonar.host.url` or `SONAR_HOST_URL`
- current Git revision through `sonar.scm.revision=<HEAD>` when the working tree is clean

For a dirty working tree, do not falsely label the analysis as the pristine `HEAD`; use a descriptive build string and treat the result as working-tree-specific.

Where the scanner supports it, set `sonar.working.directory` to a unique temporary directory outside the repository. The scanner deletes this directory before analysis, so never point it at an existing/user-owned directory. SonarScanner for .NET is an exception; follow its native workflow.

Infer source/test roots only when the project layout makes them reliable. Prefer scanner defaults over guessed paths that would omit code.

When the scanner runs in a container on macOS or Windows, a bind mount of the working copy is
orders of magnitude slower than the container's own filesystem for the many small reads a
type-aware analyzer performs — measurably so: reading a dependency tree's type declarations can
take tens of seconds through a bind mount against well under a second from a named volume. If a
container scanner is genuinely the only option, copy the sources into a named volume first
(include the dependency directory, since type-aware analysis needs it) and keep the scanner cache
and working directory on volumes too. Fresh named volumes are root-owned while the official
scanner image runs as a non-root user, so ownership must be set for the scanner's uid or the run
fails immediately with `AccessDeniedException` on its cache directory. None of this is a substitute
for running natively when the architecture does not match.

For a first audit, keep the installed/default `Sonar way` quality profiles unless the project already has an intentional profile. Do not tune global rules or thresholds merely to make one repository pass.

### Safe exclusions

Exclude only directories that are clearly not maintained source, such as dependencies, generated artifacts, caches, build output, minified bundles, vendor copies, coverage output, and generated client/schema code when evidence supports that classification.

Typical examples may include `node_modules`, `.next`, `dist`, `build`, `coverage`, `.venv`, `vendor`, generated code directories, and cache directories, but never apply an exclusion merely because its name appears in this list. Inspect the project first.

Do not exclude difficult legacy modules, tests, or source files to improve metrics.

Two mistakes are easy to make and both silently corrupt the result:

- **Anchor patterns with `**/`, not at the repository root.** `dist/**` or `.ruff_cache/**` match
  only a top-level directory; the same directories nested one level down (a monorepo package, a
  language subproject) are still indexed. Prefer `**/dist/**`, `**/__pycache__/**`,
  `**/.ruff_cache/**`.
- **Exclude agent and tooling working directories that contain whole copies of the repository.**
  Coding agents create git worktrees, backups, and sandboxes under paths such as `.claude/`,
  `.agents/`, or `.worktrees/`. A forgotten worktree doubles the entire codebase in the index and
  every metric with it. Confirm with `git worktree list` and check what the scanner log lists
  under `Indexing files...`; the indexed file count is the cheapest sanity check that the scope is
  right.

## Coverage

Coverage is useful only when it is trustworthy.

1. Detect existing coverage configuration/report paths first.
2. If a documented/local-safe repository command already generates coverage, it may be run without changing project files.
3. Feed the resulting language-appropriate report into Sonar when supported.
4. If coverage cannot be obtained safely without changing configuration or causing expensive/environment-dependent work, omit it and report `Coverage: unavailable`, not `0%`.

Two things routinely go wrong:

- **Paths in the report must resolve against the analysis base directory.** A report generated
  inside a container or from a subdirectory records paths relative to that working directory
  (`filename="main.py"`, `<source>/app/app</source>`), which match nothing when the scanner runs
  from the repository root. Rewrite the file paths to be repository-relative and set the report's
  source root to the analysis base directory before importing. Confirm the import in the scanner
  log — a silently unmatched report leaves coverage at zero without any warning.
- **Distinguish "not measured" from "0%" per language.** When only some languages produce a
  coverage report, the project-level percentage mixes measured code with unmeasured code counted
  as uncovered, and the aggregate becomes misleading. Report the measured figure per language or
  per module and label the rest `not measured`, never `0%`.

## Analysis completion

Scanner success does not prove Sonar finished processing.

1. Read the generated scanner metadata (`report-task.txt`) from its temporary scanner workspace when available.
2. Poll the Compute Engine task URL/id until it reaches `SUCCESS` or a terminal failure.
3. On failure, report the actual server task error/log context.

Capture the scanner's own output to a file (`> scan.log 2>&1`) rather than piping it into `tail`,
`head`, or a filter. A failing run's diagnosis is usually in the middle of the log — the warnings
naming unexpected indexed files, the sensor timings, the last sensor to start — and a truncated
capture destroys exactly that. Note also that the scanner can terminate with `EXECUTION FAILURE`
and a non-zero exit code while emitting no `ERROR` line at all, so never treat "no error in the
tail" as "no diagnosis available".

Keep scanner work/cache outside the target repository when possible; otherwise remove only audit-created untracked scanner artifacts after verifying they did not pre-exist.

## Freshness

For a clean repository, an existing result is reusable only if:

- latest analysis revision equals `git rev-parse HEAD`;
- analysis completed successfully;
- it is recent enough for the request (default 60 minutes);
- analysis configuration/scope has not materially changed.

Useful evidence may come from project-analysis Web API records plus `sonar.scm.revision`/analysis revision. If exact source-state equivalence cannot be proven, rerun.

For any dirty working tree, always rerun because the current files differ from the stored commit revision.

## Data to retrieve

Use the Web API and paginate instead of scraping HTML. Audit **overall code**, not only new-code conditions: a quality gate can be green while the repository still contains substantial existing debt.

Retrieve at minimum:

- quality gate/project status, clearly labeled as a gate rather than an overall-quality verdict;
- unresolved issues across the full analyzed codebase with software-quality/type, severity, rule, component/file, line/range, message, effort, tags, and issue key when available;
- security hotspots/review state when available;
- metrics available on the installed version for:
  - lines of code/code size;
  - cognitive complexity;
  - cyclomatic complexity;
  - duplication;
  - coverage;
  - reliability/security/maintainability ratings or equivalent issue metrics;
  - technical-debt/maintainability effort;
- component/file-level measures sufficient to identify real hotspots, especially files with unusually high complexity or duplication rather than only aggregate project totals;
- latest analysis timestamp/revision.

Metrics and issue fields vary between SonarQube modes and versions. Query `/api/metrics` or the installed API docs rather than assuming one hard-coded metric set.

## Triage for a stalled or silently failing run

Diagnose from the process's own behaviour before changing anything. Sample it:

```bash
docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}'
```

| Observation | Reading | Action |
|---|---|---|
| Memory flat well under the limit, CPU below one core, no new log lines | Blocked or emulated, **not** resource-starved | Check image vs host architecture; check the filesystem the hot path actually uses |
| Memory pinned at the limit, CPU near 100%, GC-style churn | Genuinely out of memory | Raise the limit, or lower the JVM/Node budgets so their sum fits |
| Dies sooner when given *more* memory | Sum of JVM heap + Node heap + other containers exceeds the host allocation, kernel kills a child | Budget the total, do not maximise one part |
| Non-zero exit, `EXECUTION FAILURE`, no `ERROR` line | Child process killed, or an emulated runtime that never returned | Re-check architecture first; capture the full log |

Two rules that save the most time:

- **Measure the hypothesis before acting on it.** A filesystem test that only lists filenames does
  not measure what a compiler does; read the file contents. A memory theory is testable in one
  `docker stats` sample.
- **Change one variable per run.** These runs cost tens of minutes; two simultaneous changes waste
  a whole cycle by making the result uninterpretable.

Prefer a cheap control experiment over another full run: if the project's own toolchain does the
same work quickly (a type-check, a build, a test run) while the scanner does not, the problem is in
how the scanner is being run, not in the code under analysis.

## Repository integrity check

Before and after analysis compare at least:

- `git status --porcelain=v1`
- staged diff name/status
- tracked diff name/status

The audit is acceptable only if it leaves the target repository's tracked/staged state unchanged. If a scanner/build command creates untracked artifacts, remove only files proven to have been created by this run and only when they are disposable generated output; otherwise disclose them instead of guessing.
