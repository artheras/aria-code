"""Compatibility import for :mod:`providers.google_imagen_provider`."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module("aria_code.providers.google_imagen_provider")
