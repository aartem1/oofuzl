"""Labelled comments, one row per behaviour the classifier must get right.

`clean` means the comment must reach no bucket at all: either it is immune or
no detector fires. Rows marked ERA are open ruff ERA001 false-positive reports.
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
    ("# see issue #10072 for the failing example\nx = 1\n", ".py", "clean"),
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

    # --- trivial_doc: informative comments to stay clean ----------------
    ('def remove_all_markers():\n'
     '    """Callers holding a marker reference must drop it first."""\n'
     '    pass\n', ".py", "clean"),
    ('def get_user_id():\n    """None while the session is still opening."""\n'
     '    return 1\n', ".py", "clean"),
    ('def parse_window():\n    """Days, not seconds; the caller converts."""\n'
     '    pass\n', ".py", "clean"),

    # --- short_inline ---------------------------------------------------
    ("n = n + 1  # bump n\n", ".py", "short_inline"),
    ("rows.sort()  # sort\n", ".py", "short_inline"),
    ("int y = f();  // call f\n", ".c", "short_inline"),

    # --- short_inline: immune short comments ----------------------------
    ("x = 1  # ms, not seconds\n", ".py", "clean"),
    ("x = 1  # 0-based\n", ".py", "clean"),
    ("x = 1  # see #42\n", ".py", "clean"),
    ("x = 1  # end exclusive\n", ".py", "clean"),

    # --- zero coherence: informative, never noise -----------------------
    ("# callers holding a stale handle crash on the second pass\n"
     "def flush():\n    pass\n", ".py", "clean"),

    # --- clean: comments that carry information -------------------------
    ("# vulture exits 1 both for 'found dead code' and for 'error'\nx = 1\n",
     ".py", "clean"),
    ("# workaround for a driver bug on the 5.4 kernel\nx = 1\n", ".py", "clean"),
    ("x = 1  # noqa: E501\n", ".py", "clean"),
    ("# Copyright 2026 Acme\nx = 1\n", ".py", "clean"),
]
