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

An entity absent from the baseline has no baseline value, so its ceiling is `warn`. That is the
whole rule — there is no separate, more permissive case for new code. The alternative (checking new
entities against `hard` instead) is *more* permissive for new code than for old: it would let a
brand-new 800-line file through while failing an existing 401-line one that has carried its size
for years. New code does not get a looser bar than old code; if anything the reverse is intended.

A function is matched to its baseline by **file path + function name**. A rename counts as a new
entity and faces `warn`, not the old size — deliberate: a rename is exactly the moment not to carry
the previous size forward, and fuzzy name matching would make the ratchet unpredictable.

## 2. Which thresholds apply

The **current** `.code-health/config.json` in the target repository, never the frozen copy stored
inside `baseline.json`. The frozen copy exists only for drift reporting: lowering a threshold in
`config.json` tightens the ceiling immediately (the intended way to raise the bar over time), and
raising one is announced by `check` as a loosening rather than silently taking effect.

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
           types_or: [python, javascript, ts, tsx, jsx, go, java, kotlin, rust, ruby, php, c, c++, c#, swift, scala]
   ```

   `pass_filenames: true` and `types: [file]` are pre-commit defaults and are deliberately omitted.
   `types_or` earns its place: it is what makes pre-commit skip the hook entirely when a commit
   touches only docs or config, instead of paying process startup for `guard.py` to filter them out
   itself.
3. **Vendor `<plugin-root>/scripts/guard.py` to `.code-health/guard.py`.** This copy is
   self-contained (standard library plus the `lizard` binary pre-commit provides) so a colleague
   without this plugin and a CI runner both pass the same hook. If a copy already exists and differs
   from the plugin's, report the drift before overwriting it.
4. **Write `.code-health/baseline.json` by shelling out to the vendored copy just written**, not the
   plugin's own `scripts/guard.py`:

   ```bash
   python .code-health/guard.py --baseline .code-health/baseline.json --repo .
   ```

   This is what "one measurement path" (below) means in practice: the same script and the same
   pinned lizard version write the baseline and later enforce it.
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

This is also where the two code paths diverge deliberately: the hook path makes **no git calls at
all** — pre-commit already passes it the exact file list and stashes unstaged changes first, so
`guard.py` reading straight from disk reads the staged content. `--check` needs git precisely
because it also has to pick up untracked files, which the hook path never has to worry about since
pre-commit only ever passes files it already tracks as part of the commit.

## 5. `update-baseline`

Re-measure via the vendored `.code-health/guard.py` (never the plugin's own copy — see "One
measurement path" below) and rewrite `.code-health/baseline.json`. Print what moved in each
direction, and call out explicitly any metric that moved **up** — that is the ratchet slipping, not
routine churn, and it should never scroll past unremarked.

`update-baseline` is **never** run as part of `/code-health:fix`. It is a separate, deliberate act
taken after a real improvement lands, not an automatic step in the fix cycle and not an escape hatch
reached for while a fix is still in progress (Hard rules).

## 6. One measurement path, one lizard version

The baseline is written by the same script that enforces it — the vendored `.code-health/guard.py`,
called directly rather than through the plugin's own `scripts/guard.py` — against the lizard version
pinned in `additional_dependencies` (`lizard==1.23.0`). Writing the baseline with one parser or
lizard version and enforcing it with another produces false failures on code nobody touched, the
first time a CCN or nesting calculation shifts upstream between versions.

## 7. Why only a git hook

`git commit` is run by the model itself, so a failed hook returns its error straight into the
model's own context — the model is caught by the same channel a human is. The same hook, being an
ordinary pre-commit hook rather than a Claude Code hook, works unchanged in Codex, in Cursor, and in
CI. There is no Claude Code hook here by design.

## 8. Never move the baseline to escape a failing commit

The two escape hatches a failing hook prints are `git commit --no-verify`, for bypassing one commit,
and `/code-health:guard update-baseline`, for moving the ratchet after a genuine improvement.
Reaching for `update-baseline` to make a still-failing commit pass defeats the entire point of the
ratchet: it would let exactly the growth the hook just caught become the new permanent floor. Use it
only after the code has actually gotten smaller or simpler, never before.
