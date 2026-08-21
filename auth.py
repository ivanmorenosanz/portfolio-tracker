"""Portfolio auth glue — wraps the SHARED auth service cookie.

Portfolio no longer owns usernames or password hashes. It reads the JWT cookie
issued by `auth/` (a separate FastAPI service), verifies it locally, and
maintains a *mirror* row in its own `users` table so existing tables that
reference `user_id` keep working.

This file keeps three things the rest of the app already imports:

    _require_auth(request)        → (uid, Redirect) —_redirects to /login if no session
    current_user_id(request)
    current_username(request)
    current_language(request)

The original `register/login/forgot` POST endpoints are gone; they all live
on the auth service. Visiting /login on this app redirects to the auth
service's /login (carrying the `next=` URL so we can return here after login).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from auth_client import (
    claims_from_request,
    current_language,
    current_user_id,
    current_username,
)
from config import request_root_path  # for prefix-aware redirects
from database import SessionLocal
from models import User

router = APIRouter()


# ── helpers used throughout the app ───────────────────────────────────────────
def _ensure_user_mirror(db: Session, claims: dict) -> User:
    """Make sure a local `users` row exists for the JWT's subject.

    The local row carries `id, username, language`. We never write passwords
    here anymore — the auth service is the source of truth for credentials.

    `id` from the JWT is preserved verbatim, so any pre-existing FKs (accounts,
    holdings, etc.) keep pointing at the right row.
    """
    uid = int(claims["sub"])
    user = db.get(User, uid)
    if user is not None:
        # Keep language in sync with the most recent cookie claim.
        lang = claims.get("lang") or user.language or "es"
        if lang in ("es", "en") and lang != user.language:
            user.language = lang
            db.commit()
        return user

    # First time this user visits Portfolio after upgrading to shared auth.
    # Create the local row mirroring the JWT info. language defaults to "es"
    # and will be re-synced from the JWT on subsequent requests.
    user = User(
        id=uid,
        username=(claims.get("u") or f"user_{uid}").strip(),
        hashed_password="!",   # placeholder; never compared (auth service owns passwords)
        language=claims.get("lang") or "es",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _safe_redirect_target(next_url: Optional[str], fallback: str = "/") -> str:
    """Allow only local absolute paths to avoid open redirects."""
    target = (next_url or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _require_auth(request: Request):
    """Return (user_id, None) when authenticated, otherwise (None, RedirectResponse).

    The redirect sends the browser straight to the SHARED /login page on the
    auth service (Caddy's `reverse_proxy /login` rule proxies it there). The
    `next=` carries the FULL original path (e.g. `/Portfolio/loans`) so the
    user lands back where they tried to go.
    """
    uid = current_user_id(request)
    if uid:
        # Auto-provision the local mirror row so other modules can FK into users.
        claims = claims_from_request(request) or {}
        try:
            with SessionLocal() as db:  # type: Session
                _ensure_user_mirror(db, claims)
        except Exception:
            # Never blow up the page over the mirror; the request still proceeds
            # with the request-scoped user_id from the JWT.
            pass
        return uid, None

    # The browser URL (e.g. /Portfolio/loans) has the prefix stripped before we
    # see it (Caddy's `handle_path /Portfolio/*`). Reconstruct the EXTERNAL
    # target so the user lands back where they came from after login, not at /.
    prefix = request_root_path(request)
    internal_path = request.url.path
    qs = request.url.query or ""
    external_path = (prefix + internal_path) if prefix and internal_path == "/" else (prefix + internal_path if prefix else internal_path)
    full_target = f"{external_path}?{qs}" if qs else external_path
    return None, RedirectResponse(url=f"/login?next={full_target}", status_code=303)


# ── backwards-compat page routes ───────────────────────────────────────────────
# The original Portfolio login page is gone — it's served by the auth service
# at the bare /login path. Hitting /login here is still useful for guests who
# bookmarketed the old URL: send them to the central login page.

def _central_login_redirect(request: Request) -> RedirectResponse:
    """Redirect any /login or /register hit on this app to the central /login.

    If the user came in with `?next=ALREADY_PRESENT`, keep it verbatim (don't
    tack our own URL onto it — that's how 30-deep loop chains happen).
    """
    incoming_next = request.query_params.get("next", "").strip()
    url = "/login"
    if incoming_next and incoming_next.startswith("/") and not incoming_next.startswith("//"):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}next={incoming_next}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_redirect(request: Request):
    if current_user_id(request) is not None:
        return RedirectResponse(url=request_root_path(request) + "/", status_code=303)
    return _central_login_redirect(request)


@router.post("/login", include_in_schema=False)
def login_post_redirect(request: Request):
    return _central_login_redirect(request)


@router.get("/register", include_in_schema=False)
def register_get_redirect(request: Request):
    return _central_login_redirect(request)


@router.post("/register", include_in_schema=False)
def register_post_redirect(request: Request):
    return _central_login_redirect(request)


@router.get("/forgot-password", include_in_schema=False)
def forgot_get_redirect(request: Request):
    return _central_login_redirect(request)


@router.post("/forgot-password", include_in_schema=False)
def forgot_post_redirect(request: Request):
    return _central_login_redirect(request)


@router.api_route("/logout", methods=["GET", "POST"], include_in_schema=False)
def logout_any(request: Request):
    """Forward the user to the auth service's logout, which clears the cookie.

    Once the cookie is cleared, the auth service bounces them back to /login.
    Both GET (deep link) and POST (form button) are accepted here.
    """
    return RedirectResponse(url="/logout", status_code=303)
