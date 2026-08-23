"""Re-export the shared i18n helpers for Portfolio.

The actual catalog lives in `../_shared/i18n.py` so every app translates the
same strings from one source of truth.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parent.parent / "_shared" / "i18n.py"
_spec = importlib.util.spec_from_file_location("_shared_i18n_portfolio", _SHARED)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_shared_i18n_portfolio"] = _mod
_spec.loader.exec_module(_mod)

translate = _mod.translate
catalog_for = _mod.catalog_for
weekdays = _mod.weekdays
weekdays_mini = _mod.weekdays_mini
months = _mod.months
DEFAULT_LANG = _mod.DEFAULT_LANG
SUPPORTED = _mod.SUPPORTED
