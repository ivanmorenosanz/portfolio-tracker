from typing import Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import RECOVERY_CODE, RECOVERY_CODE_FILE, pwd_context
from database import SessionLocal
from models import User
from templating import templates

router = APIRouter()


def current_user_id(request: Request) -> Optional[int]:
    return request.session.get("user_id")


def current_username(request: Request) -> Optional[str]:
    return request.session.get("username")


def current_language(request: Request) -> str:
    return request.session.get("language", "es") if request.session.get("language") in {"es", "en"} else "es"


def _require_auth(request: Request):
    """Return (user_id, None) or (None, RedirectResponse)."""
    uid = request.session.get("user_id")
    if not uid:
        return None, RedirectResponse(url="/login", status_code=303)
    return uid, None


def _safe_redirect_target(next_url: Optional[str], fallback: str = "/") -> str:
    """Allow only local absolute paths to avoid open redirects."""
    target = (next_url or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _render_login_template(request: Request, *, error: Optional[str] = None, message: Optional[str] = None, tab: str = "login"):
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "error": error,
        "message": message,
        "tab": tab if tab in {"login", "register", "forgot"} else "login",
        "recovery_code_path": str(RECOVERY_CODE_FILE),
    })


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, tab: str = Query("login")):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    return _render_login_template(request, tab=tab)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username.strip()).first()
        if not user or not pwd_context.verify(password, user.hashed_password):
            return _render_login_template(request, error="Usuario o contraseña incorrectos.", tab="login")
        request.session["user_id"] = user.id
        request.session["username"] = user.username
        request.session["language"] = user.language or "es"
    return RedirectResponse(url="/", status_code=303)


@router.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    if password != password2:
        return _render_login_template(request, error="Las contraseñas no coinciden.", tab="register")
    if len(password) < 6:
        return _render_login_template(request, error="La contraseña debe tener al menos 6 caracteres.", tab="register")
    with SessionLocal() as db:
        if db.query(User).filter(User.username == username.strip()).first():
            return _render_login_template(request, error="El nombre de usuario ya está en uso.", tab="register")
        user = User(username=username.strip(), hashed_password=pwd_context.hash(password))
        db.add(user)
        db.commit()
        db.refresh(user)
        request.session["user_id"] = user.id
        request.session["username"] = user.username
        request.session["language"] = user.language or "es"
    return RedirectResponse(url="/", status_code=303)


@router.post("/forgot-password")
def forgot_password(
    request: Request,
    username: str = Form(...),
    recovery_code: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    cleaned_username = username.strip()
    if password != password2:
        return _render_login_template(request, error="Las contraseñas no coinciden.", tab="forgot")
    if len(password) < 6:
        return _render_login_template(request, error="La nueva contraseña debe tener al menos 6 caracteres.", tab="forgot")
    if recovery_code.strip() != RECOVERY_CODE:
        return _render_login_template(request, error="La clave de recuperación no es válida.", tab="forgot")

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == cleaned_username).first()
        if not user:
            return _render_login_template(request, error="No existe ninguna cuenta con ese usuario.", tab="forgot")
        user.hashed_password = pwd_context.hash(password)
        db.commit()

    return _render_login_template(
        request,
        message=f"Contraseña actualizada para {cleaned_username}. Ya puedes iniciar sesión.",
        tab="login",
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
