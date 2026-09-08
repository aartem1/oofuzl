"""Effective configuration and the one definition of threshold semantics.

A threshold fires at `value >= threshold`. Nothing in this package may re-derive
that rule; Sonar's `value > max` is reconciled once, in references/sonar.md, by
configuring Sonar with `hard - 1`.
"""

import copy
import json
import math
from pathlib import Path

CONFIG_RELPATH = Path(".code-health") / "config.json"

DEFAULTS = {
    "schema": 1,
    "profile": "agent",
    "thresholds": {
        "file_lines": {"warn": 400, "hard": 800},
        "file_tokens": {"warn": 8000, "hard": 15000},
        "function_lines": {"warn": 60, "hard": 100},
        "function_ccn": {"warn": 15, "hard": 25},
        "function_cognitive": {"warn": 15, "hard": 25},
        "parameters": {"warn": 5, "hard": 8},
        "nesting": {"warn": 3, "hard": 4},
    },
    "exclude": [
        "**/node_modules/**", "**/.venv/**", "**/venv/**", "**/dist/**",
        "**/build/**", "**/__pycache__/**", "**/.mypy_cache/**",
        "**/.ruff_cache/**", "**/.worktrees/**",
    ],
    "tests": [
        "**/tests/**", "**/test/**", "**/spec/**",
        "**/test_*.py", "**/*_test.py", "**/*_test.go",
        "**/*.test.ts", "**/*.test.js", "**/*.spec.ts", "**/*.spec.js",
    ],
    "churn_window_days": 180,
    "chars_per_token": 4,
    "max_file_bytes": 4194304,
    "near_duplicate": {"min_similarity": 0.60, "min_tokens": 40},
    "collision_min_files": 3,
    "gate": None,
}

# Cuts reported in summary.by_cut: the two file_lines thresholds plus a round
# number, so a percentage can always be quoted at a named cut.
EXTRA_CUTS = [1000]


class ConfigError(Exception):
    pass


def level_for(value, threshold):
    """Return 'hard', 'warn', or None for one measured value."""
    if value >= threshold["hard"]:
        return "hard"
    if value >= threshold["warn"]:
        return "warn"
    return None


def estimate_tokens(text, chars_per_token):
    """Estimated LLM tokens for a piece of source. Characters, never bytes."""
    if chars_per_token <= 0:
        raise ConfigError("chars_per_token must be positive")
    return int(math.ceil(len(text) / float(chars_per_token)))


def _validate(user):
    unknown = set(user) - set(DEFAULTS)
    if unknown:
        raise ConfigError("unknown config keys: %s" % ", ".join(sorted(unknown)))
    thresholds = user.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ConfigError("thresholds must be an object")
    unknown_t = set(thresholds) - set(DEFAULTS["thresholds"])
    if unknown_t:
        raise ConfigError("unknown threshold keys: %s" % ", ".join(sorted(unknown_t)))
    for name, override in thresholds.items():
        unknown_l = set(override) - {"warn", "hard"}
        if unknown_l:
            raise ConfigError("threshold %s: unknown levels %s"
                              % (name, ", ".join(sorted(unknown_l))))


def _merge(base, user):
    out = copy.deepcopy(base)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(repo_root, config_path=None):
    """Merge the repository's config over DEFAULTS and validate the result."""
    path = Path(config_path) if config_path else Path(repo_root) / CONFIG_RELPATH
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(user, dict):
        raise ConfigError("%s must contain an object" % path)
    _validate(user)
    merged = _merge(DEFAULTS, user)
    for name, t in merged["thresholds"].items():
        if t["warn"] > t["hard"]:
            raise ConfigError("threshold %s: warn (%s) is above hard (%s)"
                              % (name, t["warn"], t["hard"]))
    return merged


def line_cuts(cfg):
    """The file-line cuts reported in summary.by_cut, ascending and deduplicated."""
    t = cfg["thresholds"]["file_lines"]
    return sorted({t["warn"], t["hard"], *EXTRA_CUTS})
