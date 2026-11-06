"""quant_engine — vendored commodity quant modules (sports + stochastic).

Elo/Dixon-Coles football prediction and Black-Scholes/Monte-Carlo/Kelly
options+derivatives math. Proprietary factor/ML/strategy engines stay in the
private Arthera platform.

Open-core boundary: callers MUST go through ``is_available()`` and degrade
gracefully when it returns False. Today the source is bundled, so it is always
available — but routing every use through this boundary means the day the engine
is split into a separate compiled (.so) wheel, the free shell keeps working with
no caller changes (see tools/build_quant_engine.py, CLOSING_SOURCE.md).
"""

from importlib.util import find_spec
from ._namespace import extend as _extend_namespace

_extend_namespace(__path__, "")

__version__ = "1.0.0"


def is_available() -> bool:
    """True if the quant engine (bundled source or compiled wheel) can be imported."""
    try:
        return find_spec("aria_code.packages.quant_engine.stochastic") is not None
    except Exception:
        return False
