import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"


def load_package():
    """Put scripts/ on sys.path exactly the way `python3 scripts/measure.py` does."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import code_health  # noqa: F401
    return code_health
