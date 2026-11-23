---
name: guard
description: Freeze current code health as a committed baseline and refuse commits that make it worse. Installs a git pre-commit hook that checks only changed files against the ratchet rule, vendors a self-contained guard script into the repository so colleagues and CI pass the same check, and moves the baseline deliberately after a fix.
compatibility: Requires git and pre-commit in the target repository. lizard is installed by pre-commit into its own isolated environment. Designed for Claude Code and Codex-compatible Agent Skills.
---

# Code Health Guard

```
/code-health:guard install           vendor the hook, write the baseline, activate it
/code-health:guard check             run the ratchet by hand, install nothing
/code-health:guard update-baseline   move the ratchet after a fix
```

`guard` writes into the target repository, not just reports on it: a `.pre-commit-config.yaml`
entry, a vendored `.code-health/guard.py`, and `.code-health/baseline.json`. Because this lands in
someone else's repository, every write happens with confirmation and in the fixed order below, and
nothing here edits Sonar configuration, quality profiles, or the debt ledger — those belong to
`/code-health:audit` and `/code-health:fix`.

## Hard rules

- Confirm before writing any of the three install artifacts (below); this changes what a colleague's
  commit and CI both have to pass.
- If `.git/hooks/pre-commit` already exists and was not written by `pre-commit` itself, report it
  and stop — never overwrite someone's existing hook.
- Never edit `.code-health/config.json` thresholds or excludes to make a commit pass. That file is
  the repository's own committed policy; changing it to dodge a failure is the same move `fix`'s
  anti-gaming check rejects for score-gaming, just performed by hand.
- `update-baseline` is never run as part of `/code-health:fix`, and never run to make a currently
  failing commit pass. It moves the ratchet after a real improvement, not around one.

## 1. The rule

Only changed files are checked, against exactly one rule:

> No entity may exceed `max(its baseline value, warn)`.

The folder `.code-health/` (the vendored `guard.py`, the baseline, the config) is never checked and
never enters the baseline, whatever `config.exclude` says.

An entity absent from the baseline has no baseline value, so its ceiling is `warn`. That is the
whole rule — there is no separate, more permissive case for new code. The alternative (checking new
entities against `hard` instead) is *more* permissive for new code than for old: it would let a
brand-new 800-line file through while failing an existing 401-line one that has carried its size
for years. New code does not get a looser bar than old code; if anything the reverse is intended.

A function is matched to its baseline by **file path + function name**. A Python method is named
with its class (`Big.__init__`), a nested function with its parent (`outer.inner`); lizard alone
reports a bare `__init__` for every method. Functions that still share a name in one file — TS
`(anonymous)` functions, mostly — are keyed `name`, `name#2` by order, but compared as a group: per
metric, the largest current value meets the largest baseline value, the second the second, and any
extra one faces `warn`. So a new same-named function above an old one does not move the old one onto
someone else's ceiling. The key carries no line number on purpose: a line-bearing key would turn
every function below an added `import` line into a "new" entity facing `warn`. A baseline written
before class-qualified names (`schema` 1) is matched by the last part of the name and keeps working
until the next `update-baseline`.

A rename counts as a new entity and faces `warn`, not the old size — deliberate: a rename is exactly
the moment not to carry the previous size forward, and fuzzy name matching would make the ratchet
unpredictable.

**Tracker keys in comments** (`ticket_refs`) are a file-level check under the same rule, with `warn`
fixed at `1` in the script rather than read from config: a new file may carry no comment line with
a bare key such as `ABC-123`, and an old file may keep the ones it had but not add one. The count is
the audit's `ticket_ref` class — keys in a URL or on a `TODO` line, standards like `UTF-8`, and
prefixes in config `ticket_ref_ignore` (case-sensitive) do not count. A baseline written before
this check has no `ticket_refs` for its files and is not checked on them until the next
`update-baseline`.

A file moved with `git mv` (or any move git itself recognises, `git diff -M`) keeps its old path's
baseline: moving files is ordinary tidying, not new code. Moves are counted from the commit the
baseline was written at, so the file keeps it in later commits too, until `update-baseline`
records the new path. A file that git does not see as moved —
rewritten past half its content on the way — is a new path and faces `warn`.

**Linter suppressions** (`suppressions`) follow the same rule with `warn` fixed at `1`: comment lines
with `noqa`, `type: ignore`, `pylint: disable`, `pyright: ignore`, `eslint-disable…`, `@ts-ignore`,
`@ts-expect-error` or `@ts-nocheck`. A new file may carry none, an old file may keep the ones it
had but not add one. Only real comments count, not a string or a docstring that mentions the word.
Silencing a check is the cheapest way to make a task look finished; fix the code the check points
at instead. A baseline written before this check is not checked on it until `update-baseline`.

## 2. Which thresholds apply

The **current** `.code-health/config.json` in the target repository, never the frozen copy stored
inside `baseline.json`. The frozen copy exists only for drift reporting: lowering a threshold in
`config.json` tightens the ceiling immediately (the intended way to raise the bar over time), and
raising one is announced by `check` as a loosening rather than silently taking effect. `check`
prints that announcement on every run, even when nothing fails; the hook prints it only when a
commit is being rejected, so it explains the ceiling instead of nagging on every commit.

## 3. `install`

With confirmation, in this exact order:

1. **Ensure `pre-commit` itself exists.** A `.pre-commit-config.yaml` entry alone does nothing until
   `pre-commit install` writes `.git/hooks/pre-commit`. If `pre-commit` is missing, offer to install
   it — same provisioning order as the rest of this plugin's Python tools, `<plugin-root>/references/tools.md`
   (`uv tool install` → `pipx install` → a plugin-owned venv as the fallback that always works). If
   `.git/hooks/pre-commit` already exists and `pre-commit` did not write it, report this and stop
   (Hard rules) — do not overwrite it.
2. **Write the `.pre-commit-config.yaml` entry**, exactly as DESIGN §10 specifies:

   ```yaml
     - repo: local
       hooks:
         - id: code-health-guard
           name: code-health guard (ratchet)
           entry: python .code-health/guard.py
           language: python
           additional_dependencies: ["lizard==1.23.0"]
           types_or: [python, javascript, ts, tsx, jsx, go, java, kotlin, rust, ruby, php, c, c++, c#,
                      swift, scala, lua, objective-c, vue, svelte, sql, shell]
           always_run: true
   ```


   The block above is an **entry**, indented to sit under a top-level `repos:` list. Append it to an
   existing `.pre-commit-config.yaml`'s `repos:`. If the repository has no such file yet, create one
   whose entire content is:

   ```yaml
   repos:
     - repo: local
       hooks:
         - id: code-health-guard
           name: code-health guard (ratchet)
           entry: python .code-health/guard.py
           language: python
           additional_dependencies: ["lizard==1.23.0"]
           types_or: [python, javascript, ts, tsx, jsx, go, java, kotlin, rust, ruby, php, c, c++, c#,
                      swift, scala, lua, objective-c, vue, svelte, sql, shell]
           always_run: true
   ```

   `pass_filenames: true` and `types: [file]` are pre-commit defaults and are deliberately omitted.
   `always_run: true` runs the hook on every commit, also one that only edits or deletes
   `.code-health/baseline.json` or `config.json`: pre-commit passes no deleted files, and without
   it such a commit would never reach the check in section 8. `types_or` decides which file names
   the hook gets. Do not trim the tag list: a type that is missing from it never reaches the hook,
   so an extension `guard.py` claims to check would end up checked by nobody.
3. **Vendor `<plugin-root>/scripts/guard.py` to `.code-health/guard.py`.** This copy is
   self-contained (standard library plus the `lizard` binary pre-commit provides) so a colleague
   without this plugin and a CI runner both pass the same hook. If a copy already exists and differs
   from the plugin's, report the drift before overwriting it.
4. **Check the lizard version, then write `.code-health/baseline.json` by shelling out to the
   vendored copy just written**, not the plugin's own `scripts/guard.py`:

   ```bash
   lizard --version                                                   # must print 1.23.0
   python .code-health/guard.py --baseline .code-health/baseline.json --repo .
   ```

   **Run the version check first and stop if it disagrees** with the version pinned in
   `additional_dependencies` above. `guard.py --baseline` uses whatever lizard is on the current
   `PATH`, while the hook will use the pinned one pre-commit installs — so skipping this check
   writes the baseline with one lizard and enforces it with another, which is precisely what "One
   measurement path, one lizard version" (below) exists to prevent, and it produces false failures
   on untouched code the first time a CCN or nesting calculation shifts upstream. A mismatch is
   normal: `audit` keeps the shared `lizard` at the latest release. Do not downgrade it — that
   would only be upgraded again by the next audit. Use a separate pinned copy for this one command:

   ```bash
   python3 -m venv "${XDG_DATA_HOME:-$HOME/.local/share}/code-health/lizard-1.23.0"
   "${XDG_DATA_HOME:-$HOME/.local/share}/code-health/lizard-1.23.0/bin/pip" install "lizard==1.23.0"
   CODE_HEALTH_LIZARD="${XDG_DATA_HOME:-$HOME/.local/share}/code-health/lizard-1.23.0/bin/lizard" \
     python .code-health/guard.py --baseline .code-health/baseline.json --repo .
   ```

   Do not proceed with a different version. The guard's pin stays deliberately: a baseline is only
   comparable when one lizard version writes and enforces it.

   This is what "one measurement path" means in practice: the same script and the same pinned
   lizard version write the baseline and later enforce it.
5. **Stage all three files** (`.pre-commit-config.yaml`, `.code-health/guard.py`,
   `.code-health/baseline.json`) and state plainly that the baseline must be committed. An
   uncommitted baseline makes the ratchet meaningless for the second person who clones the repository
   and for CI — both would silently have no baseline to check against.
6. **Run `pre-commit install`.** Only this step actually activates the hook.

## 4. `check`

```bash
python .code-health/guard.py --check --repo .
```

The same comparison as the hook, run by hand, installing nothing. `--check` covers
`git diff --name-only HEAD` **plus** `git ls-files --others --exclude-standard`, because untracked
files are exactly the new-entity case (rule 1) that `git diff` alone does not list.

This is also where the two code paths diverge deliberately: in the hook path pre-commit already
passes the exact file list and stashes unstaged changes first, so `guard.py` reading straight from
disk reads the staged content; its only git calls read the baseline and config at `HEAD`
(section 8). `--check` also has to pick up untracked files, which the hook path never has to worry
about since pre-commit only ever passes files it already tracks as part of the commit.

## 5. `update-baseline`

Re-measure via the vendored `.code-health/guard.py` (never the plugin's own copy — see "One
measurement path" below) and rewrite `.code-health/baseline.json`:

```bash
python .code-health/guard.py --baseline .code-health/baseline.json --repo .
```

The script itself prints what moved in each direction and marks every upward movement `UP`, because
it reads the outgoing baseline before overwriting it. Relay that output — do not summarize the `UP`
lines away. A metric that moved up is the ratchet slipping, not routine churn, and it should never
scroll past unremarked.

When the new baseline would raise any ceiling — a value grown to or past `warn`, or a new entity
granted at or above it — the script writes nothing, lists those entities and exits 1. That is the
same change the hook rejects at commit time. `--accept-worse` writes it anyway; pass it only when
the user explicitly decided to raise the bar for those entities, never on your own to get a commit
through. The hook rejects that baseline too, so the person commits it themselves with
`git commit --no-verify`, and the CI check of section 7 fails on it in the pull request, where the
reviewer sees what was raised. What the guard measures for the first time after an upgrade —
`.vue`, `.svelte`, `.sql`, `.sh` files that were already in the repository, and an existing
`# NOQA` in capitals — is not "raised": the first `update-baseline` records it without
`--accept-worse`. Run it right after replacing `guard.py`: until then a commit that touches such a
file is judged by the old baseline.

`update-baseline` is **never** run as part of `/code-health:fix`. It is a separate, deliberate act
taken after a real improvement lands, not an automatic step in the fix cycle and not an escape hatch
reached for while a fix is still in progress (Hard rules).

## 6. One measurement path, one lizard version

The baseline is written by the same script that enforces it — the vendored `.code-health/guard.py`,
called directly rather than through the plugin's own `scripts/guard.py` — against the lizard version
pinned in `additional_dependencies` (`lizard==1.23.0`). Writing the baseline with one parser or
lizard version and enforcing it with another produces false failures on code nobody touched, the
first time a CCN or nesting calculation shifts upstream between versions.

## 7. Why a git hook, and the same check on the server

`git commit` is run by the model itself, so a failed hook returns its error straight into the
model's own context — the model is caught by the same channel a human is. The same hook, being an
ordinary pre-commit hook rather than a Claude Code hook, works unchanged in Codex and in Cursor.
There is no Claude Code hook here by design.

A hook can always be skipped: `git commit --no-verify`, or a fresh clone where nobody ran
`pre-commit install`. The place where a skipped hook shows up is a CI job on every pull request:

```bash
git fetch origin main
git show origin/main:.code-health/guard.py | python - --base origin/main
```

`--base REF` checks every file changed since the merge base with `REF` and compares the branch's
baseline and config with the merge base's, so a branch that grew code past the ceiling, or moved
the bar, fails there, and what `REF` did after the branch left it does not count. The script is
taken from `REF`, so the branch cannot change the check that judges it. The CI checkout needs full
history (`fetch-depth: 0`): without the merge base the script exits 2, and without the commit the
baseline was written at it does not see moved files.
When proposing this job, show it and let the user add it; `install` does not write CI files.

## 8. Never move the baseline to escape a failing commit

`/code-health:guard update-baseline` moves the ratchet after a genuine improvement. Reaching for it
to make a still-failing commit pass defeats the entire point of the ratchet: it would let exactly
the growth the hook just caught become the new permanent floor. Use it only after the code has
actually gotten smaller or simpler, never before.

The hook enforces this. It compares `.code-health/baseline.json` and `config.json` with their
versions at `HEAD` (at the merge base with `REF` with `--base`) and rejects the commit when they
raise any ceiling:

- a baseline value that grew to or past `warn`, or a new entity granted at or above `warn`;
- a `ticket_refs` or `suppressions` value removed from a file's record, or a new record without
  them, which stops that check for the file;
- a loosened threshold, a larger `chars_per_token`, a new `exclude` or `ticket_ref_ignore` entry;
- a lower `schema`, or a deleted baseline.

A value that moved below `warn` changes no ceiling and passes; so does any improvement, and the
install commit, which has no baseline at `HEAD`. Raising the bar on purpose is a person's decision:
they commit it themselves (section 5), and the CI check above shows it in the pull request.
