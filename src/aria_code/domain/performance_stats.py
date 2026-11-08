"""Compatibility alias for the managed finance statistics implementation."""

from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("aria_code.domain._finance_core.performance_stats")
