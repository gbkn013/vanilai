"""Let pure-logic tests import vanilai submodules without Home Assistant.

The package __init__ imports homeassistant, which unit tests of the pure
modules (normalize, providers, fusion) don't need. Register a bare package
so Python skips that __init__.
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "custom_components"

try:
    import homeassistant  # noqa: F401
except ImportError:
    for name, path in (
        ("custom_components", ROOT),
        ("custom_components.vanilai", ROOT / "vanilai"),
    ):
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(path)]
        sys.modules[name] = pkg

sys.path.insert(0, str(ROOT.parent))
