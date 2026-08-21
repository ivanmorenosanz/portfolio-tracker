"""Re-export the shared JWT verification helper for Portfolio.

The actual implementation lives in `../_shared/auth_client.py` so FitTracker
can share it without duplicating the algorithm.

NOTE: this file's *name* (auth_client) shadows the shared module's name from
Python's perspective, so we import the shared module with a different alias
first and re-export its public symbols here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parent.parent / "_shared" / "auth_client.py"
_spec = importlib.util.spec_from_file_location("_shared_auth_client", _SHARED)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_shared_auth_client"] = _mod
_spec.loader.exec_module(_mod)

# Pull every public name from the loaded module into this module's namespace.
for _name in (
    "COOKIE_NAME",
    "JWT_ALGORITHM",
    "JWT_ISSUER",
    "claims_from_request",
    "claims_from_request_headers",
    "current_user_id",
    "current_username",
    "current_language",
    "login_redirect_url",
    "reset_secret_cache",
    "verify_session_token",
):
    globals()[_name] = getattr(_mod, _name)
