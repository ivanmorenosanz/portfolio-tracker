"""Re-export the shared device-detection helper for Portfolio.

The actual implementation lives in `../_shared/device.py` so FitTracker (and
any future app) can share it without duplicating the User-Agent parsing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parent.parent / "_shared" / "device.py"
_spec = importlib.util.spec_from_file_location("_shared_device", _SHARED)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_shared_device"] = _mod
_spec.loader.exec_module(_mod)

detect_device = _mod.detect_device
device_from_request = _mod.device_from_request
