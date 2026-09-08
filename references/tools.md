# Tool layer: install, invoke, parse, and the traps

Every fact below was a real bug during implementation, not a hypothetical. This file exists so a
future maintainer does not "simplify" one of these back into the bug it already was. Which metric
each tool feeds is catalogued in `<plugin-root>/references/metrics.md`; this file is about the tool
invocation itself — how to call it, how to parse its output, and what breaks when the call is
"simplified."

## `lizard` — size, complexity, parameters, nesting

Invocation: `lizard -ENS --csv <paths>`, with **no thresholds** — collect every row, apply this
plugin's own thresholds in Python afterward.

**`-ENS` is not optional.** It is what adds the 12th CSV column, `max_nested_structures`. Without
`-ENS` there is no nesting data in the output at all — the column simply does not exist, it is not
merely empty. The threshold field lizard itself understands for this metric is named
`max_nested_structures`; the intuitive short form `-Tnd=<n>` is **rejected outright** by the CLI.

**Column order** (no header row is emitted; parse positionally):

```
nloc, ccn, token, param, length, location, filename, function_name,
long_name, start_line, end_line, max_nested_structures
```

`long_name` contains commas and is quoted by lizard's own CSV writer — parse with `csv.reader`,
never `str.split(",")`, or a function whose signature contains a comma silently shifts every column
after it.

**`--csv` emits FUNCTION rows only.** A file with zero functions — a 5000-line constants file, a
generated data module, a barrel `__init__.py` — produces empty stdout and exit code 0. Nothing in
the output says the file was even considered. **File-level size therefore never comes from
lizard**; it comes from a direct read of the file (line count, char count, then the token estimate
in `<plugin-root>/references/metrics.md`). Without this, a 5000-line function-free file is
completely invisible to the measurer.

**Never `import lizard`.** On the calibration machine lizard was installed via `uv tool install`,
which is an *isolated* install: the `lizard` console script lands on `$PATH`, but the package is not
importable from the system Python interpreter — `import lizard` raises `ModuleNotFoundError` even
though `lizard --version` works fine in the same shell. Binary resolution therefore goes through
subprocess only, in this order: `$CODE_HEALTH_LIZARD` env override → `shutil.which("lizard")` →
`[sys.executable, "-m", "lizard"]` as a last resort (works when lizard was installed into the
current interpreter's own environment rather than an isolated tool install).

**The nesting column is not trustworthy for every language.** `lizard`'s nesting counter
(`lizard_ext/lizardnd.py`) resets on a closing brace: it increments on every `if`/`for`/`while`/...
token and decrements back to zero when it sees the reader's `bracket` token, `}` by default. That
works for a brace/semicolon language, where a function's own closing brace guarantees the reset. It
does **not** work for an indentation-based or `end`-keyword reader, which never emits that token —
the counter climbs across every control-flow keyword in the function and then keeps climbing into
every function that follows in the **same lizard invocation**, because nothing ever brings it back
to zero. Verified directly, with a flat-vs-deep probe run against real `lizard -ENS --csv`:

```
python  5 sequential `if`s (function depth 1)  -> nesting=5   (should be 1)
python  5 nested `if`s     (function depth 5)  -> nesting=5   (correct only by coincidence)
ruby    3 trailing-`if`s   (function depth 1)  -> nesting=3   (should be 1)
lua     5 sequential `if`/`then`/`end`         -> nesting=5   (should be 1)
js      flat / 5-deep                          -> 1 / 5       (correct)
c, c++, c#, java, go, kotlin, rust, swift,
scala, php, objective-c    flat / 5-deep       -> 1 / 5       (correct, every one tested)
```

And it does not reset between files or functions in one invocation, because `measure.py` chunks its
argv (`_ARG_CHUNK = 400`) and any real repository has more than one Python function in a chunk:

```
lizard -ENS --csv nest_flat.py nest_deep.py                -> 5, 10   (order-dependent)
lizard -ENS --csv nest_deep.py nest_flat.py                -> 5, 10
lizard -ENS --csv nest_flat.py nest_flat.py nest_flat.py    -> 5, 10, 15
```

For Python this is not a metric with an off-by-something error — it is a monotonically increasing
counter whose final value depends on `argv` order. `code_health/sizes.py` (`_apply_nesting_source`)
replaces it, for every `.py` function, with an exact value computed independently from the same AST
`code_health/pyast.py` already parses (`pyast.measure_nesting`), joined to lizard's row by
`(file, start_line)` — not by function name, because a nested or method function's name does not
reliably agree between the two (lizard qualifies it, e.g. `outer.inner`; `ast` reports the bare
`inner`), while `start_line` was verified to agree in every case tested, decorators and multi-line
decorators included. `scripts/guard.py` carries an independent copy of the same AST pass, for the
same reason it already carries its own CSV parser: it cannot import `code_health`.

For every language *not* verified above (`.rb`, `.lua`, and anything else `sizes.py`/`guard.py` has
not confirmed resets correctly), nesting is reported as `None` — "not measured" — rather than
lizard's unverified, possibly-accumulating number. `None` never reaches the `>=` threshold
comparison and never produces a `too_deep_nesting` finding; it is not silently treated as zero.

## `git log --numstat` — churn

Invocation:

```
git -C <repo> log --since="<window> days ago" --no-merges --numstat -z -M \
    --format="%H%x09%aI%x09%aE"
```

Every flag earns its place; the obvious simplification of any one of them reintroduces a bug:

- **`git -C <repo>`, no `-- .` pathspec.** `-- .` is resolved relative to the *current working
  directory*, not the repo. `measure.py --repo PATH` invoked from anywhere other than `PATH` itself
  would silently scope the log to a subtree, or to nothing.
- **`-z` is required, not cosmetic, when `-M` (rename detection) is on.** Without `-z`, `-M` emits
  the brace-compressed rename form `src/{old => new}/mod.py` for a same-directory rename (or the
  fully bracketed form for a cross-directory one). A `split(" => ")` fold on that string produces
  `src/{old` and `new}/mod.py` — two garbage paths, neither of which exists. With `-z`, a rename
  instead arrives as a numstat line with an **empty path field**, immediately followed by two
  separate NUL-terminated records: the old path, then the new path. The brace-compressed form never
  appears once `-z` is present.
- **`%aE` (author email) must be requested explicitly alongside `%aI`.** `%aI` is the author
  *date*, not an identity. Without `%aE` in the format string there is no author identity in the
  output at all — grouping churn by author silently degrades to "one unnamed author per commit".
- **Binary files emit `-` for both the added and removed count**, not integers. The parser needs an
  explicit branch (`"-" if field == "-" else int(field)`); feeding `"-"` to `int()` unguarded is a
  crash on the first binary file in the window, not a slow degradation.

**Record layout changes shape with `-z`.** `--format` composes with `--numstat`: **without** `-z`
the commit header line is followed by a *blank line*, then the numstat block. **With** `-z` there is
no blank line at all — every record, header and numstat alike, is NUL-terminated instead. Verified
output (NUL shown as `|`):

```
<sha>\t<date>\t<email>|1\t0\t|src/old/mod.py|src/new/mod.py|<sha>\t<date>\t<email>|-\t-\tblob.bin|3\t0\tsrc/old/mod.py|
```

Read left to right: a header record, then a numstat row `added\tremoved\t` with an **empty** path
field (a rename), followed by the two path records that belong to it; then the next header; then a
plain numstat row; then a binary row (`-\t-\t<path>`).

**Header detection uses a 40-hex-SHA regex (`^[0-9a-f]{40}\t`), not a heuristic.** Two heuristics
that look reasonable both break on real data:

- "first field is not numeric" fails because a SHA can happen to consist entirely of decimal
  digits, which is numeric.
- "the record has exactly N fields" fails on an ordinary numstat row whose path contains `@` — a
  three-field numstat row is shaped exactly like a three-field header for some inputs, so field
  count alone cannot distinguish them.

The regex anchors on the one structural fact that is always true of a header and never true of a
numstat row: a 40-character lowercase-hex SHA followed by a tab.

## gitwildmatch versus `fnmatch` / `PurePath.match`

`measure.py` is stdlib-only, so exclusion and test-classification patterns (`.code-health/config.json`
`exclude` / `tests`) are matched with a small purpose-built gitwildmatch-semantics matcher, not
`fnmatch` or `pathlib.PurePath.match`. This is not a style preference — the stdlib matchers give
**wrong answers**, verified:

```python
import fnmatch
fnmatch.fnmatch("dist/a.js", "**/dist/**")             # False -- should match
fnmatch.fnmatch("packages/web/dist/a.js", "**/dist/**") # True
fnmatch.fnmatch("node_modules/x.js", "**/node_modules/**")  # False -- should match
```

The rule gitwildmatch actually implements: **`**/` means zero or more directories**, not "one or
more". `**/dist/**` must match a top-level `dist/a.js` exactly as it matches a nested
`packages/web/dist/a.js`; neither `fnmatch.fnmatch` nor `PurePath.match` implements that — both
require at least one directory component to satisfy a leading `**/`. A config that excludes
`**/node_modules/**` intending to skip every `node_modules` directory anywhere in the tree, matched
with plain `fnmatch`, silently fails to exclude one sitting at the repository root — exactly the
common case.

## `npx -y` and Python installs

`npx -y <pkg>` downloads into the npx cache on first use and needs network access; offline, the
layer that depends on it (`dup.exact` via `jscpd`, JS/TS `imports` via `madge`, JS/TS `dead` via
`knip`) reports `not_measured` with `reason: network unavailable`, and the rest of the audit still
completes — a missing optional tool is never fatal to the run.

`pip install --user` is **refused** under PEP 668 on a Homebrew Python — verified,
`error: externally-managed-environment` / `EXTERNALLY-MANAGED` is present on the calibration
machine. Python tool provisioning (`lizard`, `vulture`, `pre-commit`) therefore tries, in order:
`uv tool install` → `pipx install` → a **plugin-owned venv** under
`${XDG_DATA_HOME:-$HOME/.local/share}/code-health/venv` as the fallback that always works regardless
of how the system Python is managed.

## `madge --circular`

`madge --circular <path>` **replaces** the dependency map in its output rather than adding cycle
information to it — one invocation cannot give both the graph and the cycle list. The measurer
therefore always asks for `madge --json <path>` (the full dependency map) and derives cycles from
that graph itself, using the same cycle-detection routine as the Python `ast`-pass import graph.
This is also why `dependency-cruiser` earns no place here: the two outputs this plugin needs from a
JS/TS import graph, `cycles` and `degree` (fan-in/fan-out), both come from one `madge --json` call,
and a second tool would only be justified by a query madge cannot express.

## `vulture` exit codes

`vulture` exits `1` for **both** "dead code was found" **and** "an error occurred" (a syntax error
in one of the scanned files, a missing path). The return code alone cannot discriminate between
those two cases — **stderr is what separates them**. Verified behavior: a clean repository with no
dead code exits `0` with empty stderr; a repository with real dead code exits `1` with empty
stderr; a repository vulture could not fully analyze (a syntax error, a missing file) also exits `1`
but with non-empty stderr. A naive `returncode != 0` check marks *every* repository that contains
any dead code at all as `not_measured` — the common case, not the rare one. The correct check
accepts `returncode in (0, 1)` as a successful run and inspects stderr separately to decide whether
the run was clean, partial (some files could not be analyzed), or a genuine failure.

## MinHash / LSH — the near-duplicate pass

Own implementation, no external dependency (`<plugin-root>/references/metrics.md` covers what
`near_duplicate` means and why it exists; this section covers the mechanics and their calibration).

- **128 permutations**, hashed via **XOR-salt** (`min(h ^ salt for h in hashes)` per permutation)
  rather than the classical `(a*h + b) mod prime` family. Measured: 0.90 ms per function at 100
  shingles with XOR-salt, versus 3.33 ms with the modular family — a ~3.7× difference that matters
  because every function in the repository gets a signature. Only candidate generation depends on
  the signature's speed; every candidate pair reported still gets an exact Jaccard computation, so
  the faster hash does not trade away accuracy.
- **32 bands × 4 rows** over each 128-permutation signature — the banding that turns "signatures
  are similar" into "these two are LSH *candidates*" cheaply, before the exact-Jaccard check.
- **Two separate LSH indexes are required, not one.** One index tuned so that a *lexical* Jaccard of
  0.60 becomes a candidate reaches ≈99% recall at `J=0.60`, but only ≈56% at `J=0.40` — the S-curve
  that banding produces falls off sharply below its tuned point. That 56% is exactly the floor the
  *structural* branch (renamed-identifier clones) needs to operate at: renamed-identifier clones
  routinely land in the 0.25–0.40 lexical range even when structurally near-identical, so a single
  lexical-tuned index would silently drop roughly half of exactly the clones that branch exists to
  catch. Hence two independent LSH indexes — one over the lexical signature, one over the
  structural signature — unioned into one candidate set before the exact-Jaccard filter.
- **Report threshold**: a pair is reported when `lexical >= 0.60` **or**
  (`structural >= 0.90` **and** `lexical >= 0.25`). The `0.25` floor, not the more obvious `0.40`,
  is itself a measured calibration: a real 7-line cache wrapper with three identifiers renamed
  scored `structural = 1.00`, `lexical = 0.32` — a `0.40` floor would have rejected exactly the case
  the structural branch exists to catch. Raising the structural bar to `0.90` (from a more
  permissive value) is the tradeoff that makes a `0.25` lexical floor safe: unrelated functions
  measured `structural = 0.07`, nowhere near either branch, and the `min_tokens = 40` cut plus a 2×
  size prefilter keep trivially-shaped small functions from ever becoming false-positive candidates.

The number surfaced to the user is always the **lexical** similarity — the figure comparable to a
manually-measured "64–75% pairwise similarity" baseline — even for pairs that were only found
through the structural index.
