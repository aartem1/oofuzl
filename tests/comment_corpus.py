"""Labelled comments, one row per behaviour the classifier must get right.

`clean` means the comment must reach no bucket at all: either it is immune or
no detector fires. Rows marked ERA are open ruff ERA001 false-positive reports.

What this corpus is: a regression pin written alongside `comments.py` by the
same person who wrote the detectors, so a perfect score on it is expected and
is not an unbiased precision estimate -- every row was picked because its
author already knew which way it should fall. What it is not: the source of
any published precision figure. The literature's ~0.89/~0.90 (Steidl et al.,
ICPC 2013) describes the algorithms this module descends from, not this
implementation; the false-positive rate on code this repository did not write
was observed by inspecting every flagged line across three live repositories,
and both figures are set out in `references/metrics.md`, "Where
`comment_noise`'s precision figures come from". This repository refuses to
publish a number nobody checked, and that rule applies to this corpus's own
score too: treat a green `CorpusPrecisionTests` run as "no known regression,"
not as "measured precision."
"""

# (source, extension, expected class)
CORPUS = [
    # --- commented_out -------------------------------------------------
    ("# print(old_result)\nx = 1\n", ".py", "commented_out"),
    ("# return None\nx = 1\n", ".py", "commented_out"),
    ("# result = fetch(\n#     url,\n# )\nx = 1\n", ".py", "commented_out"),
    ('# "key": value,\nx = 1\n', ".py", "commented_out"),
    ("# except ValueError:\nx = 1\n", ".py", "commented_out"),
    ("# import json\nx = 1\n", ".py", "commented_out"),

    # --- commented_out: false positives to stay clean (ERA) -------------
    ("# TODO: drop print(x) after RAM-436\nx = 1\n", ".py", "clean"),
    # An issue number is no longer a directive: these two are not
    # commented-out code, but they are tracker references.
    ("# see issue #10072 for the failing example\nx = 1\n", ".py", "ticket_ref"),
    ("x = 1  # see #42\n", ".py", "ticket_ref"),
    ("# /// script\n# requires-python = \">=3.11\"\n# ///\nx = 1\n", ".py", "clean"),
    ("# ruff: ignore[E501]\nx = 1\n", ".py", "clean"),
    ("# -*- coding: utf-8 -*-\nx = 1\n", ".py", "clean"),
    ('url = "http://x/#frag"\n', ".py", "clean"),

    # --- trivial_doc ----------------------------------------------------
    ('def remove_all_markers():\n    """Removes all markers."""\n    pass\n',
     ".py", "trivial_doc"),
    ('def get_user_id():\n    """Get the user id."""\n    return 1\n',
     ".py", "trivial_doc"),
    ("# increment counter\ndef increment_counter():\n    pass\n",
     ".py", "trivial_doc"),
    ("// returns the total price\nfunction returnTotalPrice() {}\n",
     ".js", "trivial_doc"),

    # --- trivial_doc: near the 0.5 coherence boundary --------------------
    # TRIVIAL_COHERENCE = 0.5 is a strict `>`; the four rows above all score
    # far from the line, which would let a wrong threshold slip through
    # unnoticed. These three pin the boundary itself, with the exact score
    # verified by direct call to `coherence` rather than assumed:
    #   coherence("resets the counter", "reset_counter")        == 0.667 (fires)
    #   coherence("parses data from config", "parse_config")    == 0.5   (must not fire)
    #   coherence("parses the window quickly today",
    #             "parse_window_size")                          == 0.4   (must not fire)
    ("# resets the counter\ndef reset_counter():\n    pass\n",
     ".py", "trivial_doc"),
    ("# parses data from config\ndef parse_config():\n    pass\n",
     ".py", "clean"),
    ("# parses the window quickly today\ndef parse_window_size():\n    pass\n",
     ".py", "clean"),

    # --- trivial_doc: brace-language declaration shapes beyond .js ------
    # commented_out stays Python-only by design (extract_python's own parser
    # is what makes that class accurate); these rows exercise only
    # trivial_doc, against the Go-receiver, C-pointer-return and Java typed
    # declaration shapes declarations_brace was built to recognise.
    ("// returns the total price\n"
     "public double returnTotalPrice() {\n    return total;\n}\n",
     ".java", "trivial_doc"),
    ("// loads all entries\n"
     "func (c *Cache) LoadAll() []Entry {\n    return c.entries\n}\n",
     ".go", "trivial_doc"),
    ("/* reads the next line */\n"
     "static char *read_next_line(FILE *f) {\n    return line;\n}\n",
     ".c", "trivial_doc"),

    # --- trivial_doc: informative comments to stay clean ----------------
    ('def remove_all_markers():\n'
     '    """Callers holding a marker reference must drop it first."""\n'
     '    pass\n', ".py", "clean"),
    ('def get_user_id():\n    """None while the session is still opening."""\n'
     '    return 1\n', ".py", "clean"),
    ('def parse_window():\n    """Days, not seconds; the caller converts."""\n'
     '    pass\n', ".py", "clean"),
    ("// returns null when the cache is empty\n"
     "public String getCachedValue() {\n    return null;\n}\n",
     ".java", "clean"),
    ("// nil when the entry was evicted\n"
     "func (c *Cache) Peek() *Entry {\n    return nil\n}\n",
     ".go", "clean"),
    ("/* returns NULL when allocation fails */\n"
     "static void *alloc_block(size_t n) {\n    return NULL;\n}\n",
     ".c", "clean"),

    # --- short_inline ---------------------------------------------------
    ("n = n + 1  # bump n\n", ".py", "short_inline"),
    ("rows.sort()  # sort\n", ".py", "short_inline"),
    ("int y = f();  // call f\n", ".c", "short_inline"),

    # --- short_inline: immune short comments ----------------------------
    ("x = 1  # ms, not seconds\n", ".py", "clean"),
    ("x = 1  # 0-based\n", ".py", "clean"),
    ("x = 1  # bytes, not chars\n", ".py", "clean"),
    ("x = 1  # end exclusive\n", ".py", "clean"),

    # --- zero coherence: informative, never noise -----------------------
    ("# callers holding a stale handle crash on the second pass\n"
     "def flush():\n    pass\n", ".py", "clean"),

    # --- non-English comments --------------------------------------------
    # A Cyrillic comment that must be immune via the reason-word pattern
    # (`\bиначе\b`).
    ("# трогать нельзя, иначе всё сломается\nx = 1\n", ".py", "clean"),
    # A Cyrillic comment that restates a Latin identifier's words, in the
    # sense trivial_doc requires, is not attempted here -- this row instead
    # documents what actually happens when one sits next to a Latin
    # declaration. `invalidate_cache` splits into `invalidate` and `cache`,
    # both multi-character, and no word below reaches either within one
    # edit (verified: `coherence("кэш недействителен после перезапуска",
    # "invalidate_cache") == 0.0`), so it lands in zero_coherence, not
    # trivial_doc.
    ("# кэш недействителен после перезапуска\n"
     "def invalidate_cache():\n    pass\n", ".py", "clean"),

    # --- clean: comments that carry information -------------------------
    # Placed above a declaration (it had none before) so coherence actually
    # runs instead of short-circuiting on a missing name: verified score is
    # coherence(text, "check_dead_code") == 0.2, well under the 0.5 bar --
    # the comment survives because it is genuinely uncorrelated with the
    # function name, not because no check ran on it.
    ("# vulture exits 1 both for 'found dead code' and for 'error'\n"
     "def check_dead_code():\n    pass\n", ".py", "clean"),
    ("# workaround for a driver bug on the 5.4 kernel\nx = 1\n", ".py", "clean"),
    ("x = 1  # noqa: E501\n", ".py", "clean"),
    ("# Copyright 2026 Acme\nx = 1\n", ".py", "clean"),

    # --- live-code false positives (Task 9) ------------------------------
    # These eight rows are the first in this corpus drawn from real, running
    # code rather than from imagination: each is the exact text that made a
    # `commented_out` hit against one of three inspected repositories
    # (15,318 source files) a false positive, found by hand-checking every
    # hit before this fix landed. Every other row above was picked because
    # its author already knew which way it should fall; these were picked
    # because a live repository proved the detector wrong.

    # Fix 1: a docstring must never reach the `commented_out` gate. Shape of
    # the xhh/ajax.py and resume-profile-front false positives -- a
    # `feature_id:` / `code_owner:` schema worked example, indented and
    # quoted, whose lines outnumber the surrounding prose 5-to-2 and used to
    # fire the majority rule on the whole docstring.
    ('def handler():\n'
     '    """Request payload.\n\n'
     '    {\n'
     '        "feature_id": 12,\n'
     '        "code_owner": "team-x",\n'
     '        "status": "active",\n'
     '        "priority": 3,\n'
     '    }\n'
     '    """\n'
     '    return {}\n', ".py", "clean"),

    # Fix 2: an empty parse is not evidence of code. Verbatim shape of a
    # `BRAND = (214, 0, 28)  # #D6001C -- HH brand red (service.hh), primary
    # accent` line: the nested '#' before "D6001C" opens a Python comment
    # that truncates everything after it, so `compile` succeeds on a module
    # body of nothing.
    ('BRAND = (214, 0, 28)  # #D6001C — HH brand red (service.hh), primary accent\n',
     ".py", "clean"),

    # Fix 3: a valueless annotation is code only when its annotation has the
    # shape Python's own grammar uses for a type -- Subscript, Attribute, a
    # BitOr of two type expressions, a bare Constant `None`, or a bare Name
    # against a small builtin/typing allow-list. `Format: "MemAvailable:
    # 1234567 kB"` (a /proc/meminfo parser's field-format note) parses as
    # ast.AnnAssign with a string Constant annotation and must stay clean;
    # `clusters: dict[str, Any]` (a real deprecated annotation found in xhh)
    # parses the same shape with a Subscript annotation and IS commented-out
    # code.
    ('# Format: "MemAvailable:   1234567 kB"\nx = 1\n', ".py", "clean"),
    ("# clusters: dict[str, Any]\nx = 1\n", ".py", "commented_out"),
    # Round 2: a bare Name annotation is code only against the type
    # allow-list -- pinned from the code side (`x: int`, a builtin type) and
    # the false-positive side (`Казахстан:Астана`, verbatim from
    # xhh/utils/employerview_meta_tags.py: a Country:Capital label comment
    # documenting a domain-area-id lookup table's key/value pair, valid
    # syntax only because Cyrillic identifiers are legal Python 3 names).
    ("# x: int\ny = 1\n", ".py", "commented_out"),
    ("# Казахстан:Астана\ny = 1\n", ".py", "clean"),

    # Fix 4: a call with whitespace before its parenthesis is prose, not
    # code. `lower_is_better (default)` (a metrics-direction note) parses as
    # Expr(Call) and must stay clean; `put_oauth_error_state(self,
    # HTTPStatus.FORBIDDEN)` (real commented-out code found in xhh) parses
    # the same shape with no space before the "(".
    ("# lower_is_better (default)\nx = 1\n", ".py", "clean"),
    ("# put_oauth_error_state(self, HTTPStatus.FORBIDDEN)\nx = 1\n",
     ".py", "commented_out"),

    # Fix 4 round 2: `end_col_offset` is a UTF-8 byte offset, and the space
    # check indexed the line by character. Every non-ASCII character before
    # the callee's end shifted the index left by one, so a Cyrillic-named
    # call landed on the space inside its own argument list and was rejected
    # as prose -- on two of the three repositories this discriminator was
    # tuned against, both Russian-language.
    ("# получить_кэш(ключ, значение)\nx = 1\n", ".py", "commented_out"),
    ("# получить кэш (ключ, значение)\nx = 1\n", ".py", "clean"),

    # Fix 5: a statement keyword must match a whole token, not just a `\b`
    # boundary a hyphen also satisfies. Verbatim shape of `except Exception
    # as exc:  # continue-on-error (CancelledError propagates`, where the
    # old `_STATEMENT_KEYWORD` pattern matched "continue" inside
    # "continue-on-error".
    ('try:\n    risky()\n'
     'except Exception as exc:  # continue-on-error (CancelledError propagates\n'
     '    log(exc)\n', ".py", "clean"),
]
