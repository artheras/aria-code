"""Moved to services/aria-bots: this name imports :mod:`aria_bots.relay_client`."""
from importlib import import_module as _import_module
from importlib.util import find_spec as _find_spec
from pathlib import Path as _Path
import sys as _sys

if _find_spec("aria_bots") is None:  # a source checkout run without installing it
    _sys.path.extend(str(_p / "services/aria-bots/src") for _p in _Path(__file__).resolve().parents
                     if (_p / "services/aria-bots/src/aria_bots").is_dir())

if __name__ == "__main__":
    import runpy as _runpy

    _runpy.run_module("aria_bots.relay_client", run_name="__main__", alter_sys=True)
else:
    _sys.modules[__name__] = _import_module("aria_bots.relay_client")
