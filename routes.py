from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus
from pathlib import Path
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import aliased

from auth import _require_auth, _safe_redirect_target, current_language, current_username
from config import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    ATTACHMENTS_DIR,
    DEFAULT_CURRENCY,
    MADRID_TZ,
    MAX_ATTACHMENT_SIZE,
    STATIC_DIR,
    pwd_context,
    _EXPENSE_CATEGORY_NAMES,
    _categorize_expense,
    _in_market_hours,
    _to_madrid,
)
from database import SessionLocal
from models import (
    Account,
    Asset,
    AutoContribution,
    ExpenseRecord,
    ExpenseSchedule,
    Goal,
    Holding,
    IncomeRecord,
    IncomeSchedule,
    InterestPromo,
    MortgageProfile,
    SavedMortgage,
    PortfolioSnapshot,
    User,
    PortfolioTypeSnapshot,
    Trade,
    TransferRecord,
    TransferSchedule,
    WatchlistItem,
)
from prices import (
    _analysis_cache,
    _asset_source_url,
    _compute_rsi,
    _download_prices,
    _fetch_analysis_history,
    _fetch_asset_history,
    _find_support_resistance,
    _get_finnhub_client,
    _is_generic_asset_name,
    _lookup_descriptive_name,
    _price_cache,
    _refresh_single_ticker,
    _sym,
    fetch_latest_price,
    get_effective_price,
    yahoo_symbol_search,
)
from services import (
    _adjust_cash,
    _build_allocation_data,
    _execute_cash_flow,
    _execute_transfer,
    _get_account_currency,
    _get_or_create_cash_holding,
    _period_label,
    _round_money,
    ensure_portfolio_type_snapshots_table,
    record_portfolio_snapshots,
    refresh_prices,
    run_auto_contributions,
    run_scheduled_expenses,
    run_scheduled_incomes,
    run_scheduled_transfers,
    snapshot_data,
)
from templating import templates

router = APIRouter()


# ── attachment helpers ────────────────────────────────────────────────────────
def _sanitize_filename(filename: str) -> str:
    base = Path(filename or "").name.strip()
    if not base:
        return "archivo"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)


def _is_allowed_attachment(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_ATTACHMENT_EXTENSIONS


def _save_record_attachment(
    upload: Optional[UploadFile],
    *,
    user_id: int,
    record_type: str,
) -> tuple[Optional[str], Optional[str]]:
    if upload is None or not upload.filename:
        return None, None

    safe_original_name = _sanitize_filename(upload.filename)
    if not _is_allowed_attachment(safe_original_name):
        raise ValueError("Tipo de archivo no permitido")

    ext = Path(safe_original_name).suffix.lower()
    user_dir = ATTACHMENTS_DIR / f"user_{user_id}" / record_type
    user_dir.mkdir(parents=True, exist_ok=True)

    stored_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(8)}{ext}"
    destination = user_dir / stored_name
    written = 0

    try:
        with destination.open("wb") as out_file:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ATTACHMENT_SIZE:
                    raise ValueError("El archivo supera el tamaño máximo permitido (8 MB)")
                out_file.write(chunk)
    except Exception:
        if destination.exists():
            destination.unlink(missing_ok=True)
        raise
    finally:
        upload.file.close()

    relative_url = f"/static/record_attachments/user_{user_id}/{record_type}/{stored_name}"
    return relative_url, safe_original_name


def _attachment_url_to_path(attachment_url: Optional[str]) -> Optional[Path]:
    if not attachment_url or not attachment_url.startswith("/static/record_attachments/"):
        return None

    relative = attachment_url.removeprefix("/static/")
    candidate = (STATIC_DIR / relative).resolve()
    root = ATTACHMENTS_DIR.resolve()
    if root == candidate or root in candidate.parents:
        return candidate
    return None


def _delete_attachment_file(attachment_url: Optional[str]) -> None:
    target = _attachment_url_to_path(attachment_url)
    if not target:
        return
    target.unlink(missing_ok=True)


# ── main routes ───────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    # Safety net in case the background scheduler was not running.
    run_auto_contributions()
    run_scheduled_expenses()
    run_scheduled_incomes()
    run_scheduled_transfers()
    # Record a fresh snapshot so cash changes (e.g. spending at night) appear in
    # the evolution chart immediately, even when markets are closed.
    try:
        record_portfolio_snapshots()
    except Exception:
        pass
    with SessionLocal() as db:
        return templates.TemplateResponse(request, "dashboard.html", {
            "request": request,
            "username": current_username(request),
        "language": current_language(request),
            **snapshot_data(db, uid),
        })


def _mortgage_payment(principal: float, annual_rate: float, years: int) -> float:
    """Return a monthly French-amortization payment without rounding intermediate values."""
    if principal <= 0 or years <= 0:
        return 0.0
    monthly_rate = max(0.0, annual_rate) / 100.0 / 12.0
    months = years * 12
    if monthly_rate == 0:
        return principal / months
    return principal * monthly_rate / (1 - (1 + monthly_rate) ** (-months))


def _mortgage_profile_payload(profile: MortgageProfile) -> dict:
    return {
        "age": profile.age,
        "employment_status": profile.employment_status,
        "monthly_net_income": profile.monthly_net_income,
        "monthly_fixed_expenses": profile.monthly_fixed_expenses,
        "monthly_debt_payments": profile.monthly_debt_payments,
        "savings": profile.savings,
        "dependents": profile.dependents,
        "property_use": profile.property_use,
    }


def _saved_mortgage_payload(scenario: SavedMortgage) -> dict:
    return {
        "id": scenario.id,
        "name": scenario.name,
        "property_price": scenario.property_price,
        "initial_contribution": scenario.initial_contribution,
        "purchase_costs_pct": scenario.purchase_costs_pct,
        "term_years": scenario.term_years,
        "interest_rate": scenario.interest_rate,
        "opening_fee_pct": scenario.opening_fee_pct,
        "monthly_extra": scenario.monthly_extra,
        "created_at": scenario.created_at.strftime("%d/%m/%Y") if scenario.created_at else "",
    }


@router.get("/loans", response_class=HTMLResponse)
def loan_simulator(request: Request):
    """Render the client-side mortgage simulator using common Spanish assumptions."""
    uid, redir = _require_auth(request)
    if redir:
        return redir
    with SessionLocal() as db:
        profile = db.query(MortgageProfile).filter(MortgageProfile.user_id == uid).first()
        profile_exists = profile is not None
        profile_data = _mortgage_profile_payload(profile) if profile else {}
        saved_mortgages = [
            _saved_mortgage_payload(scenario)
            for scenario in db.query(SavedMortgage)
            .filter(SavedMortgage.user_id == uid)
            .order_by(SavedMortgage.created_at.desc(), SavedMortgage.id.desc())
            .all()
        ]
    return templates.TemplateResponse(request, "loans.html", {
        "request": request,
        "username": current_username(request),
        "language": current_language(request),
        "profile_exists": profile_exists,
        "profile_data": profile_data,
        "saved_mortgages": saved_mortgages,
    })


@router.post("/api/mortgage/profile")
def save_mortgage_profile(
    request: Request,
    age: int = Form(...),
    employment_status: str = Form(...),
    monthly_net_income: float = Form(...),
    monthly_fixed_expenses: float = Form(...),
    monthly_debt_payments: float = Form(...),
    savings: float = Form(...),
    dependents: int = Form(0),
    property_use: str = Form(...),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    allowed_employment = {"permanent", "civil_servant", "temporary", "self_employed", "retired", "unemployed"}
    allowed_property_use = {"primary", "second", "investment"}
    if not 18 <= age <= 100:
        return JSONResponse({"error": "La edad debe estar entre 18 y 100 años."}, status_code=400)
    if employment_status not in allowed_employment:
        return JSONResponse({"error": "Situación laboral no válida."}, status_code=400)
    if property_use not in allowed_property_use:
        return JSONResponse({"error": "Uso de la vivienda no válido."}, status_code=400)
    if any(value < 0 for value in (monthly_net_income, monthly_fixed_expenses, monthly_debt_payments, savings)):
        return JSONResponse({"error": "Los importes no pueden ser negativos."}, status_code=400)
    if not 0 <= dependents <= 20:
        return JSONResponse({"error": "El número de dependientes no es válido."}, status_code=400)

    with SessionLocal() as db:
        profile = db.query(MortgageProfile).filter(MortgageProfile.user_id == uid).first()
        if profile is None:
            profile = MortgageProfile(user_id=uid)
            db.add(profile)
        profile.age = age
        profile.employment_status = employment_status
        profile.monthly_net_income = monthly_net_income
        profile.monthly_fixed_expenses = monthly_fixed_expenses
        profile.monthly_debt_payments = monthly_debt_payments
        profile.savings = savings
        profile.dependents = dependents
        profile.property_use = property_use
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/mortgage/recommendations")
def mortgage_recommendations(
    request: Request,
    property_price: float = Form(...),
    initial_contribution: float = Form(...),
    purchase_costs_pct: float = Form(...),
    term_years: int = Form(...),
    interest_rate: float = Form(...),
    opening_fee_pct: float = Form(...),
    monthly_extra: float = Form(...),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if property_price <= 0 or initial_contribution < 0 or purchase_costs_pct < 0 or not 1 <= term_years <= 40 or interest_rate < 0 or opening_fee_pct < 0 or monthly_extra < 0:
        return JSONResponse({"error": "Revisa los datos de la hipoteca."}, status_code=400)

    with SessionLocal() as db:
        profile = db.query(MortgageProfile).filter(MortgageProfile.user_id == uid).first()
        if profile is None:
            return JSONResponse({"error": "Completa primero la encuesta inicial."}, status_code=400)

    purchase_costs = property_price * purchase_costs_pct / 100.0
    property_down_payment = min(property_price, max(0.0, initial_contribution - purchase_costs))
    principal = max(0.0, property_price - property_down_payment)
    base_payment = _mortgage_payment(principal, interest_rate, term_years)
    selected_payment = base_payment + monthly_extra
    total_debt_payment = selected_payment + profile.monthly_debt_payments
    income = profile.monthly_net_income
    dependent_buffer = profile.dependents * 250.0
    monthly_surplus_before_mortgage = income - profile.monthly_fixed_expenses - profile.monthly_debt_payments - dependent_buffer
    debt_ratio = (total_debt_payment / income) if income > 0 else None
    max_by_income = income * 0.35 - profile.monthly_debt_payments if income > 0 else 0.0
    max_by_surplus = max(0.0, monthly_surplus_before_mortgage * 0.8)
    recommended_max_payment = max(0.0, min(max_by_income, max_by_surplus))
    max_term_by_age = max(1, min(40, 75 - profile.age))

    candidate_years = None
    for candidate in (15, 20, 25, 30, 35, 40):
        if candidate <= max_term_by_age and _mortgage_payment(principal, interest_rate, candidate) + monthly_extra <= recommended_max_payment:
            candidate_years = candidate
            break

    remaining_savings = max(0.0, profile.savings - initial_contribution)
    monthly_living_commitments = profile.monthly_fixed_expenses + profile.monthly_debt_payments + dependent_buffer
    emergency_target = monthly_living_commitments * (9 if profile.employment_status in {"temporary", "self_employed", "unemployed"} else 6)
    selected_interest = max(0.0, base_payment * term_years * 12 - principal)
    tips: list[str] = []

    if income <= 0:
        tips.append("Sin ingresos netos registrados no se puede validar la capacidad de pago; completa la encuesta con una cifra mensual realista.")
    elif debt_ratio is None or debt_ratio > 0.35:
        tips.append(f"La cuota más otras deudas representa aproximadamente el {debt_ratio * 100:.1f}% de tus ingresos. Intenta mantenerla por debajo del 35% para tener margen.")
    elif debt_ratio > 0.30:
        tips.append(f"La cuota total queda en torno al {debt_ratio * 100:.1f}% de tus ingresos: es viable como referencia, pero está en una zona poco holgada.")
    else:
        tips.append(f"La cuota total queda en torno al {debt_ratio * 100:.1f}% de tus ingresos, dentro de una zona prudente orientativa.")

    if remaining_savings < emergency_target:
        missing = max(0.0, emergency_target - remaining_savings)
        tips.append(f"Después de la aportación te quedarían aproximadamente {remaining_savings:,.0f} €. Prioriza conservar un fondo de emergencia de unos {emergency_target:,.0f} €; faltan aproximadamente {missing:,.0f} €.")
    else:
        tips.append("La aportación deja un colchón de ahorro suficiente según el fondo de emergencia orientativo indicado.")

    if candidate_years is None:
        age_note = f" y el límite orientativo por edad de {max_term_by_age} años" if max_term_by_age < 40 else ""
        tips.append(f"No hay un plazo recomendado que encaje con el límite de cuota calculado{age_note}. Considera reducir el precio, aumentar la aportación o buscar un tipo mejor antes de firmar.")
        term_tip = "No hay un plazo recomendado que encaje con el límite de cuota calculado."
    elif term_years > candidate_years:
        candidate_payment = _mortgage_payment(principal, interest_rate, candidate_years) + monthly_extra
        candidate_interest = max(0.0, (_mortgage_payment(principal, interest_rate, candidate_years) * candidate_years * 12) - principal)
        saving = max(0.0, selected_interest - candidate_interest)
        term_tip = f"Para ahorrar a largo plazo, {candidate_years} años sería el plazo más corto que encaja en el límite estimado ({candidate_payment:,.0f} €/mes); frente a {term_years} años ahorrarías aproximadamente {saving:,.0f} € de intereses."
        tips.append(term_tip)
    else:
        term_tip = f"El plazo seleccionado de {term_years} años encaja en el límite de cuota estimado. Si tu colchón lo permite, un plazo más corto reduce el coste total de intereses."
        tips.append(term_tip)

    if term_years > max_term_by_age:
        tips.append(f"El plazo seleccionado supera el límite orientativo de {max_term_by_age} años que algunas entidades aplican para terminar la hipoteca antes de cierta edad.")
    if interest_rate > 0 and principal > 0:
        tips.append("Amortizar anticipadamente suele ahorrar más intereses durante los primeros años, cuando el saldo pendiente es mayor. Si puedes elegir, reducir plazo normalmente ahorra más que reducir cuota; revisa antes la comisión de amortización de tu oferta.")
    if profile.property_use == "second":
        tips.append("Para una segunda residencia las entidades suelen financiar un porcentaje menor que para vivienda habitual; revisa que la aportación cubra esa diferencia además de los gastos.")
    if profile.property_use == "investment":
        tips.append("Para una vivienda de inversión el banco puede aplicar condiciones más exigentes y no siempre cuenta el alquiler previsto al 100%; no bases la cuota únicamente en una renta estimada.")

    return JSONResponse({
        "metrics": {
            "principal": round(principal, 2),
            "monthly_payment": round(selected_payment, 2),
            "debt_ratio": round(debt_ratio * 100, 1) if debt_ratio is not None else None,
            "recommended_max_payment": round(recommended_max_payment, 2),
            "remaining_savings": round(remaining_savings, 2),
            "selected_interest": round(selected_interest, 2),
            "recommended_years": candidate_years,
        },
        "tips": tips,
        "disclaimer": "Son reglas orientativas, no una preaprobación bancaria ni asesoramiento financiero personalizado.",
    })


@router.post("/api/mortgage/saved")
def save_mortgage_scenario(
    request: Request,
    name: str = Form("Escenario de hipoteca"),
    property_price: float = Form(...),
    initial_contribution: float = Form(...),
    purchase_costs_pct: float = Form(...),
    term_years: int = Form(...),
    interest_rate: float = Form(...),
    opening_fee_pct: float = Form(...),
    monthly_extra: float = Form(...),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cleaned_name = name.strip() or "Escenario de hipoteca"
    if property_price <= 0 or initial_contribution < 0 or purchase_costs_pct < 0 or not 1 <= term_years <= 40 or interest_rate < 0 or opening_fee_pct < 0 or monthly_extra < 0:
        return JSONResponse({"error": "Revisa los datos de la hipoteca."}, status_code=400)
    with SessionLocal() as db:
        scenario = SavedMortgage(
            user_id=uid,
            name=cleaned_name[:120],
            property_price=property_price,
            initial_contribution=initial_contribution,
            purchase_costs_pct=purchase_costs_pct,
            term_years=term_years,
            interest_rate=interest_rate,
            opening_fee_pct=opening_fee_pct,
            monthly_extra=monthly_extra,
        )
        db.add(scenario)
        db.commit()
        db.refresh(scenario)
        return JSONResponse({"saved": _saved_mortgage_payload(scenario)})


@router.post("/api/mortgage/saved/{scenario_id}/delete")
def delete_saved_mortgage(request: Request, scenario_id: int):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    with SessionLocal() as db:
        scenario = db.query(SavedMortgage).filter(SavedMortgage.id == scenario_id, SavedMortgage.user_id == uid).first()
        if scenario:
            db.delete(scenario)
            db.commit()
    return JSONResponse({"ok": True})


@router.post("/profile/preferences")
def update_profile_preferences(request: Request, language: str = Form("es")):
    """Update the user's language on the shared auth service.

    The auth service re-mints a fresh JWT carrying the new `lang` claim and
    sets it on the response cookie; subsequent reads in this app pick up the
    new language automatically (no local DB write needed).
    """
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if language not in {"es", "en"}:
        return JSONResponse({"error": "Idioma no válido."}, status_code=400)

    # Forward to the auth service. The auth service returns 200 + a fresh
    # session cookie; we want that cookie set on the browser that's calling
    # us, so we copy its Set-Cookie header onto our response.
    import requests as _req  # Portfolio already has `requests` in requirements.txt
    token = request.cookies.get("app_session", "")
    auth_url = "http://127.0.0.1:8002/api/auth/language"
    try:
        r = _req.post(auth_url, data={"language": language},
                      headers={"Cookie": f"app_session={token}"}, timeout=10.0)
    except Exception as e:
        return JSONResponse({"error": f"No se pudo contactar al servicio de auth: {e}"}, status_code=502)
    if r.status_code != 200:
        try: detail = r.json().get("detail", "Error del servicio de auth.")
        except Exception: detail = "Error del servicio de auth."
        return JSONResponse({"error": detail}, status_code=r.status_code)

    out = JSONResponse({"ok": True, "language": language})
    sc = r.headers.get("set-cookie")
    if sc:
        # Pass Set-Cookie through verbatim so the browser stores the new JWT.
        out.headers.append("set-cookie", sc)
    return out


@router.post("/profile/password")
def change_profile_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password2: str = Form(...),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if new_password != new_password2:
        return JSONResponse({"error": "Las contraseñas nuevas no coinciden."}, status_code=400)
    if len(new_password) < 6:
        return JSONResponse({"error": "La nueva contraseña debe tener al menos 6 caracteres."}, status_code=400)
    with SessionLocal() as db:
        user = db.get(User, uid)
        if not user or not pwd_context.verify(current_password, user.hashed_password):
            return JSONResponse({"error": "La contraseña actual no es correcta."}, status_code=400)
        user.hashed_password = pwd_context.hash(new_password)
        db.commit()
    return JSONResponse({"ok": True})


@router.get("/api/search-symbol")
def search_symbol(q: str = Query(..., min_length=2)):
    return JSONResponse({"results": yahoo_symbol_search(q)})


@router.post("/accounts")
def create_account(request: Request, name: str = Form(...)):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    with SessionLocal() as db:
        cleaned = name.strip()
        if cleaned:
            exists = db.query(Account).filter(
                Account.name == cleaned, Account.user_id == uid
            ).first()
            if not exists:
                db.add(Account(name=cleaned, user_id=uid))
                db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/goals")
def create_goal(
    request: Request,
    name: str = Form(...),
    target_amount: float = Form(...),
    currency: str = Form(DEFAULT_CURRENCY),
    account_id: Optional[str] = Form(None),
    manual_amount: Optional[str] = Form(None),
    target_date: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_name = name.strip()
    if not cleaned_name or target_amount <= 0:
        return RedirectResponse(url="/", status_code=303)

    parsed_account_id: Optional[int] = None
    if account_id and account_id.strip():
        try:
            candidate = int(account_id.strip())
        except ValueError:
            candidate = None
        if candidate is not None:
            with SessionLocal() as db:
                acct = db.query(Account).filter(Account.id == candidate, Account.user_id == uid).first()
                if acct:
                    parsed_account_id = candidate

    parsed_manual: Optional[float] = None
    if manual_amount is not None and manual_amount.strip():
        try:
            parsed_manual = float(manual_amount.strip())
        except ValueError:
            parsed_manual = None

    cleaned_currency = (currency or "").strip().upper() or DEFAULT_CURRENCY

    parsed_date: Optional[date] = None
    if target_date is not None and target_date.strip():
        try:
            parsed_date = datetime.strptime(target_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            parsed_date = None

    with SessionLocal() as db:
        db.add(Goal(
            user_id=uid,
            name=cleaned_name,
            target_amount=target_amount,
            currency=cleaned_currency,
            account_id=parsed_account_id,
            manual_amount=parsed_manual,
            target_date=parsed_date,
        ))
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/goals/{goal_id}/edit")
def edit_goal(
    request: Request,
    goal_id: int,
    name: str = Form(...),
    target_amount: float = Form(...),
    currency: str = Form(DEFAULT_CURRENCY),
    account_id: Optional[str] = Form(None),
    manual_amount: Optional[str] = Form(None),
    target_date: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_name = name.strip()
    if not cleaned_name or target_amount <= 0:
        return RedirectResponse(url="/", status_code=303)

    parsed_account_id: Optional[int] = None
    if account_id and account_id.strip():
        try:
            candidate = int(account_id.strip())
        except ValueError:
            candidate = None
        if candidate is not None:
            with SessionLocal() as db:
                acct = db.query(Account).filter(Account.id == candidate, Account.user_id == uid).first()
                if acct:
                    parsed_account_id = candidate

    parsed_manual: Optional[float] = None
    if manual_amount is not None and manual_amount.strip():
        try:
            parsed_manual = float(manual_amount.strip())
        except ValueError:
            parsed_manual = None

    cleaned_currency = (currency or "").strip().upper() or DEFAULT_CURRENCY

    parsed_date: Optional[date] = None
    if target_date is not None and target_date.strip():
        try:
            parsed_date = datetime.strptime(target_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            parsed_date = None

    with SessionLocal() as db:
        goal = db.get(Goal, goal_id)
        if goal and goal.user_id == uid:
            goal.name = cleaned_name
            goal.target_amount = target_amount
            goal.currency = cleaned_currency
            goal.account_id = parsed_account_id
            goal.manual_amount = parsed_manual
            goal.target_date = parsed_date
            db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/goals/{goal_id}/delete")
def delete_goal(request: Request, goal_id: int):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    with SessionLocal() as db:
        goal = db.get(Goal, goal_id)
        if goal and goal.user_id == uid:
            db.delete(goal)
            db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/transfers")
def create_transfer(
    request: Request,
    from_account_id: int = Form(...),
    to_account_id: int = Form(...),
    amount: float = Form(...),
    notes: Optional[str] = Form(None),
    recurring: Optional[str] = Form(None),
    day_of_month: Optional[str] = Form(None),
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_notes = notes.strip() if notes else None
    redirect_target = _safe_redirect_target(next_url, "/")
    if amount <= 0 or from_account_id == to_account_id:
        return RedirectResponse(url=redirect_target, status_code=303)

    with SessionLocal() as db:
        from_account = db.query(Account).filter(Account.id == from_account_id, Account.user_id == uid).first()
        to_account = db.query(Account).filter(Account.id == to_account_id, Account.user_id == uid).first()
        if not from_account or not to_account:
            return RedirectResponse(url=redirect_target, status_code=303)

        if recurring == "on":
            try:
                transfer_day = int((day_of_month or "").strip())
            except ValueError:
                return RedirectResponse(url=redirect_target, status_code=303)
            if not 1 <= transfer_day <= 31:
                return RedirectResponse(url=redirect_target, status_code=303)

            db.add(TransferSchedule(
                user_id=uid,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                amount=amount,
                day_of_month=transfer_day,
                enabled=1,
                notes=cleaned_notes,
            ))
        else:
            _execute_transfer(
                db,
                user_id=uid,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                amount=amount,
                notes=cleaned_notes,
            )
        db.commit()
    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/transfers/{schedule_id}/delete")
def delete_transfer_schedule(
    request: Request,
    schedule_id: int,
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    redirect_target = _safe_redirect_target(next_url, "/")
    with SessionLocal() as db:
        schedule = db.query(TransferSchedule).filter(
            TransferSchedule.id == schedule_id,
            TransferSchedule.user_id == uid,
        ).first()
        if schedule:
            db.delete(schedule)
            db.commit()
    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/transfers/{schedule_id}/edit")
def edit_transfer_schedule(
    request: Request,
    schedule_id: int,
    from_account_id: int = Form(...),
    to_account_id: int = Form(...),
    amount: float = Form(...),
    day_of_month: int = Form(...),
    notes: Optional[str] = Form(None),
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    redirect_target = _safe_redirect_target(next_url, "/")
    cleaned_notes = notes.strip() if notes else None
    if amount <= 0 or from_account_id == to_account_id or not 1 <= day_of_month <= 31:
        return RedirectResponse(url=redirect_target, status_code=303)
    with SessionLocal() as db:
        schedule = db.query(TransferSchedule).filter(
            TransferSchedule.id == schedule_id,
            TransferSchedule.user_id == uid,
        ).first()
        from_account = db.query(Account).filter(Account.id == from_account_id, Account.user_id == uid).first()
        to_account = db.query(Account).filter(Account.id == to_account_id, Account.user_id == uid).first()
        if not schedule or not from_account or not to_account:
            return RedirectResponse(url=redirect_target, status_code=303)
        schedule.from_account_id = from_account_id
        schedule.to_account_id = to_account_id
        schedule.amount = amount
        schedule.day_of_month = day_of_month
        schedule.notes = cleaned_notes
        db.commit()
    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/transfer-records/{record_id}/delete")
def delete_transfer_record(
    request: Request,
    record_id: int,
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    redirect_target = _safe_redirect_target(next_url, "/")
    with SessionLocal() as db:
        record = db.query(TransferRecord).filter(
            TransferRecord.id == record_id,
            TransferRecord.user_id == uid,
        ).first()
        if record:
            # Reverse the cash movement before removing the log entry.
            from_cash = _get_or_create_cash_holding(
                db, record.to_account_id, _get_account_currency(db, record.to_account_id)
            )
            to_cash = _get_or_create_cash_holding(
                db, record.from_account_id, _get_account_currency(db, record.from_account_id)
            )
            from_cash.quantity = _round_money(from_cash.quantity - record.amount)
            to_cash.quantity = _round_money(to_cash.quantity + record.amount)
            db.delete(record)
            db.commit()
    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/holdings")
def create_holding(
    request: Request,
    account_id: int = Form(...),
    quantity: float = Form(...),
    average_cost: float = Form(...),
    notes: Optional[str] = Form(None),
    asset_name: str = Form(...),
    ticker: Optional[str] = Form(None),
    isin: Optional[str] = Form(None),
    asset_type: str = Form(...),
    currency: str = Form(DEFAULT_CURRENCY),
    manual_price: Optional[str] = Form(None),
    ter: Optional[str] = Form(None),
    deduct_cash: Optional[str] = Form(None),
    auto_enabled: Optional[str] = Form(None),
    auto_amount: Optional[str] = Form(None),
    auto_day: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    # Verify the account belongs to this user
    with SessionLocal() as db:
        acct = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
        if not acct:
            return RedirectResponse(url="/", status_code=303)

    normalized_ticker = ticker.strip().upper() if ticker and ticker.strip() else None
    normalized_isin = isin.strip().upper() if isin and isin.strip() else None
    cleaned_asset_name = asset_name.strip()
    if normalized_ticker and _is_generic_asset_name(cleaned_asset_name, normalized_ticker):
        descriptive_name = _lookup_descriptive_name(normalized_ticker)
        if descriptive_name:
            cleaned_asset_name = descriptive_name

    parsed_manual_price = None
    if manual_price is not None and manual_price.strip():
        parsed_manual_price = float(manual_price.strip())
    if asset_type == "cash":
        parsed_manual_price = 1.0

    parsed_ter: Optional[float] = None
    if ter and ter.strip():
        try:
            parsed_ter = float(ter.strip())
        except (ValueError, AttributeError):
            pass

    with SessionLocal() as db:
        asset = None
        if normalized_ticker:
            asset = db.query(Asset).filter(Asset.ticker == normalized_ticker).first()
        if asset is None and normalized_isin:
            asset = db.query(Asset).filter(Asset.isin == normalized_isin).first()
        if asset is None:
            asset = Asset(
                name=cleaned_asset_name, ticker=normalized_ticker, isin=normalized_isin,
                asset_type=asset_type, currency=currency.strip().upper(),
                manual_price=parsed_manual_price, ter=parsed_ter,
            )
            db.add(asset)
            db.flush()
        else:
            asset.name = cleaned_asset_name
            asset.asset_type = asset_type
            asset.currency = currency.strip().upper()
            asset.isin = normalized_isin or asset.isin
            if parsed_manual_price is not None:
                asset.manual_price = parsed_manual_price
            if parsed_ter is not None:
                asset.ter = parsed_ter

        new_holding = Holding(
            account_id=account_id, asset_id=asset.id,
            quantity=quantity, average_cost=average_cost,
            notes=notes.strip() if notes else None,
        )
        db.add(new_holding)
        db.flush()  # assign new_holding.id before adding AutoContribution

        wants_auto = auto_enabled == "on" and asset_type != "cash"
        parsed_auto_amount_new: Optional[float] = None
        parsed_auto_day_new: Optional[int] = None
        if wants_auto:
            try:
                parsed_auto_amount_new = float((auto_amount or "").strip())
                parsed_auto_day_new = int((auto_day or "").strip())
            except (ValueError, AttributeError):
                wants_auto = False
        if wants_auto and parsed_auto_amount_new and parsed_auto_amount_new > 0 and parsed_auto_day_new and 1 <= parsed_auto_day_new <= 31:
            db.add(AutoContribution(
                user_id=uid,
                holding_id=new_holding.id,
                amount=parsed_auto_amount_new,
                day_of_month=parsed_auto_day_new,
                enabled=1,
            ))

        db.commit()
        saved_asset_id = asset.id
        saved_ticker = asset.ticker

    if deduct_cash == "on" and asset_type != "cash":
        total_investment = quantity * average_cost
        with SessionLocal() as db:
            cash_h = (
                db.query(Holding)
                .join(Asset)
                .filter(Holding.account_id == account_id, Asset.asset_type == "cash")
                .first()
            )
            if cash_h:
                cash_h.quantity = _round_money(cash_h.quantity - total_investment)
                db.commit()

    if saved_ticker:
        threading.Thread(target=_refresh_single_ticker,
                         args=(saved_asset_id, saved_ticker), daemon=True).start()

    if asset_type != "cash" and saved_ticker:
        with SessionLocal() as db:
            db.add(Trade(
                user_id=uid,
                timestamp=datetime.utcnow(),
                ticker=saved_ticker,
                trade_type="buy",
                quantity=quantity,
                price=average_cost,
            ))
            db.commit()

    return RedirectResponse(url="/", status_code=303)


@router.post("/refresh")
def refresh_now(request: Request):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    refresh_prices()
    return RedirectResponse(url="/", status_code=303)


@router.post("/expenses")
def create_expense(
    request: Request,
    account_id: int = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    notes: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    recurring: Optional[str] = Form(None),
    day_of_month: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None),
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_name = name.strip()
    cleaned_notes = notes.strip() if notes else None
    expense_category = (category.strip() if category else None) or _categorize_expense(f"{cleaned_name} {cleaned_notes or ''}")
    redirect_target = _safe_redirect_target(next_url, "/")
    if not cleaned_name or amount <= 0:
        return RedirectResponse(url=redirect_target, status_code=303)

    with SessionLocal() as db:
        account = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
        if not account:
            return RedirectResponse(url=redirect_target, status_code=303)

        if recurring == "on":
            try:
                expense_day = int((day_of_month or "").strip())
            except ValueError:
                return RedirectResponse(url=redirect_target, status_code=303)
            if not 1 <= expense_day <= 31:
                return RedirectResponse(url=redirect_target, status_code=303)

            db.add(ExpenseSchedule(
                user_id=uid,
                account_id=account_id,
                name=cleaned_name,
                amount=amount,
                day_of_month=expense_day,
                enabled=1,
                notes=cleaned_notes,
                category=expense_category,
            ))
        else:
            try:
                attachment_path, attachment_name = _save_record_attachment(
                    attachment,
                    user_id=uid,
                    record_type="expenses",
                )
            except ValueError:
                return RedirectResponse(url=redirect_target, status_code=303)
            _execute_cash_flow(
                db,
                record_cls=ExpenseRecord,
                sign=-1,
                user_id=uid,
                account_id=account_id,
                name=cleaned_name,
                amount=amount,
                notes=cleaned_notes,
                category=expense_category,
                attachment_path=attachment_path,
                attachment_name=attachment_name,
            )
        db.commit()

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/expenses/{schedule_id}/edit")
def edit_expense_schedule(
    request: Request,
    schedule_id: int,
    account_id: int = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    day_of_month: int = Form(...),
    notes: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_name = name.strip()
    cleaned_notes = notes.strip() if notes else None
    redirect_target = _safe_redirect_target(next_url, "/")
    if not cleaned_name or amount <= 0 or not 1 <= day_of_month <= 31:
        return RedirectResponse(url=redirect_target, status_code=303)

    with SessionLocal() as db:
        schedule = db.get(ExpenseSchedule, schedule_id)
        account = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
        if not schedule or schedule.user_id != uid or not account:
            return RedirectResponse(url=redirect_target, status_code=303)

        schedule.account_id = account_id
        schedule.name = cleaned_name
        schedule.amount = amount
        schedule.day_of_month = day_of_month
        schedule.notes = cleaned_notes
        schedule.category = (category.strip() if category else None) or _categorize_expense(cleaned_name)
        db.commit()

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/expenses/{schedule_id}/delete")
def delete_expense_schedule(
    request: Request,
    schedule_id: int,
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    redirect_target = _safe_redirect_target(next_url, "/")

    with SessionLocal() as db:
        schedule = db.get(ExpenseSchedule, schedule_id)
        if schedule and schedule.user_id == uid:
            db.delete(schedule)
            db.commit()

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/expense-records/{record_id}/edit")
def edit_expense_record(
    request: Request,
    record_id: int,
    account_id: int = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    notes: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None),
    remove_attachment: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_name = name.strip()
    cleaned_notes = notes.strip() if notes else None
    if not cleaned_name or amount <= 0:
        return RedirectResponse(url="/expenses/history", status_code=303)

    with SessionLocal() as db:
        record = db.get(ExpenseRecord, record_id)
        target_account = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
        if not record or record.user_id != uid or not target_account:
            return RedirectResponse(url="/expenses/history", status_code=303)

        new_attachment_path = None
        new_attachment_name = None
        try:
            new_attachment_path, new_attachment_name = _save_record_attachment(
                attachment,
                user_id=uid,
                record_type="expenses",
            )
        except ValueError:
            return RedirectResponse(url="/expenses/history", status_code=303)

        old_cash = _get_or_create_cash_holding(db, record.account_id, record.currency)
        old_cash.quantity = _round_money(old_cash.quantity + record.amount)

        new_currency = _get_account_currency(db, account_id, record.currency)
        new_cash = _get_or_create_cash_holding(db, account_id, new_currency)
        new_cash.quantity = _round_money(new_cash.quantity - amount)

        record.account_id = account_id
        record.name = cleaned_name
        record.amount = amount
        record.notes = cleaned_notes
        record.currency = new_currency
        record.category = (category.strip() if category else None) or _categorize_expense(cleaned_name)
        if new_attachment_path:
            _delete_attachment_file(record.attachment_path)
            record.attachment_path = new_attachment_path
            record.attachment_name = new_attachment_name
        elif remove_attachment == "on":
            _delete_attachment_file(record.attachment_path)
            record.attachment_path = None
            record.attachment_name = None
        db.commit()

    return RedirectResponse(url="/expenses/history", status_code=303)


@router.post("/expense-records/{record_id}/delete")
def delete_expense_record(request: Request, record_id: int):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    with SessionLocal() as db:
        record = db.get(ExpenseRecord, record_id)
        if record and record.user_id == uid:
            cash_holding = _get_or_create_cash_holding(db, record.account_id, record.currency)
            cash_holding.quantity = _round_money(cash_holding.quantity + record.amount)
            _delete_attachment_file(record.attachment_path)
            db.delete(record)
            db.commit()

    return RedirectResponse(url="/expenses/history", status_code=303)


@router.post("/incomes")
def create_income(
    request: Request,
    account_id: int = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    notes: Optional[str] = Form(None),
    recurring: Optional[str] = Form(None),
    day_of_month: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None),
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_name = name.strip()
    cleaned_notes = notes.strip() if notes else None
    redirect_target = _safe_redirect_target(next_url, "/")
    if not cleaned_name or amount <= 0:
        return RedirectResponse(url=redirect_target, status_code=303)

    with SessionLocal() as db:
        account = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
        if not account:
            return RedirectResponse(url=redirect_target, status_code=303)

        if recurring == "on":
            try:
                income_day = int((day_of_month or "").strip())
            except ValueError:
                return RedirectResponse(url=redirect_target, status_code=303)
            if not 1 <= income_day <= 31:
                return RedirectResponse(url=redirect_target, status_code=303)

            db.add(IncomeSchedule(
                user_id=uid,
                account_id=account_id,
                name=cleaned_name,
                amount=amount,
                day_of_month=income_day,
                enabled=1,
                notes=cleaned_notes,
            ))
        else:
            try:
                attachment_path, attachment_name = _save_record_attachment(
                    attachment,
                    user_id=uid,
                    record_type="incomes",
                )
            except ValueError:
                return RedirectResponse(url=redirect_target, status_code=303)
            _execute_cash_flow(
                db,
                record_cls=IncomeRecord,
                sign=1,
                user_id=uid,
                account_id=account_id,
                name=cleaned_name,
                amount=amount,
                notes=cleaned_notes,
                attachment_path=attachment_path,
                attachment_name=attachment_name,
            )
        db.commit()

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/incomes/{schedule_id}/edit")
def edit_income_schedule(
    request: Request,
    schedule_id: int,
    account_id: int = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    day_of_month: int = Form(...),
    notes: Optional[str] = Form(None),
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_name = name.strip()
    cleaned_notes = notes.strip() if notes else None
    redirect_target = _safe_redirect_target(next_url, "/")
    if not cleaned_name or amount <= 0 or not 1 <= day_of_month <= 31:
        return RedirectResponse(url=redirect_target, status_code=303)

    with SessionLocal() as db:
        schedule = db.get(IncomeSchedule, schedule_id)
        account = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
        if not schedule or schedule.user_id != uid or not account:
            return RedirectResponse(url=redirect_target, status_code=303)

        schedule.account_id = account_id
        schedule.name = cleaned_name
        schedule.amount = amount
        schedule.day_of_month = day_of_month
        schedule.notes = cleaned_notes
        db.commit()

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/incomes/{schedule_id}/delete")
def delete_income_schedule(
    request: Request,
    schedule_id: int,
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    redirect_target = _safe_redirect_target(next_url, "/")

    with SessionLocal() as db:
        schedule = db.get(IncomeSchedule, schedule_id)
        if schedule and schedule.user_id == uid:
            db.delete(schedule)
            db.commit()

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/income-records/{record_id}/edit")
def edit_income_record(
    request: Request,
    record_id: int,
    account_id: int = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    notes: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None),
    remove_attachment: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    cleaned_name = name.strip()
    cleaned_notes = notes.strip() if notes else None
    if not cleaned_name or amount <= 0:
        return RedirectResponse(url="/expenses/history", status_code=303)

    with SessionLocal() as db:
        record = db.get(IncomeRecord, record_id)
        target_account = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
        if not record or record.user_id != uid or not target_account:
            return RedirectResponse(url="/expenses/history", status_code=303)

        new_attachment_path = None
        new_attachment_name = None
        try:
            new_attachment_path, new_attachment_name = _save_record_attachment(
                attachment,
                user_id=uid,
                record_type="incomes",
            )
        except ValueError:
            return RedirectResponse(url="/expenses/history", status_code=303)

        old_cash = _get_or_create_cash_holding(db, record.account_id, record.currency)
        old_cash.quantity = _round_money(old_cash.quantity - record.amount)

        new_currency = _get_account_currency(db, account_id, record.currency)
        new_cash = _get_or_create_cash_holding(db, account_id, new_currency)
        new_cash.quantity = _round_money(new_cash.quantity + amount)

        record.account_id = account_id
        record.name = cleaned_name
        record.amount = amount
        record.notes = cleaned_notes
        record.currency = new_currency
        if new_attachment_path:
            _delete_attachment_file(record.attachment_path)
            record.attachment_path = new_attachment_path
            record.attachment_name = new_attachment_name
        elif remove_attachment == "on":
            _delete_attachment_file(record.attachment_path)
            record.attachment_path = None
            record.attachment_name = None
        db.commit()

    return RedirectResponse(url="/expenses/history", status_code=303)


@router.post("/income-records/{record_id}/delete")
def delete_income_record(request: Request, record_id: int):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    with SessionLocal() as db:
        record = db.get(IncomeRecord, record_id)
        if record and record.user_id == uid:
            cash_holding = _get_or_create_cash_holding(db, record.account_id, record.currency)
            cash_holding.quantity = _round_money(cash_holding.quantity - record.amount)
            _delete_attachment_file(record.attachment_path)
            db.delete(record)
            db.commit()

    return RedirectResponse(url="/expenses/history", status_code=303)


@router.post("/records/attachment")
def upsert_record_attachment(
    request: Request,
    record_type: str = Form(...),
    record_id: int = Form(...),
    attachment: Optional[UploadFile] = File(None),
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    redirect_target = _safe_redirect_target(next_url, "/expenses/history")
    normalized_type = (record_type or "").strip().lower()
    if normalized_type not in {"income", "expense"}:
        return RedirectResponse(url=redirect_target, status_code=303)
    if attachment is None or not attachment.filename:
        return RedirectResponse(url=redirect_target, status_code=303)

    with SessionLocal() as db:
        if normalized_type == "income":
            record = db.get(IncomeRecord, record_id)
            save_type = "incomes"
        else:
            record = db.get(ExpenseRecord, record_id)
            save_type = "expenses"

        if not record or record.user_id != uid:
            return RedirectResponse(url=redirect_target, status_code=303)

        try:
            new_attachment_path, new_attachment_name = _save_record_attachment(
                attachment,
                user_id=uid,
                record_type=save_type,
            )
        except ValueError:
            return RedirectResponse(url=redirect_target, status_code=303)

        _delete_attachment_file(record.attachment_path)
        record.attachment_path = new_attachment_path
        record.attachment_name = new_attachment_name
        db.commit()

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/records/attachment/delete")
def delete_record_attachment(
    request: Request,
    record_type: str = Form(...),
    record_id: int = Form(...),
    next_url: Optional[str] = Form(None, alias="next"),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    redirect_target = _safe_redirect_target(next_url, "/expenses/history")
    normalized_type = (record_type or "").strip().lower()
    if normalized_type not in {"income", "expense"}:
        return RedirectResponse(url=redirect_target, status_code=303)

    with SessionLocal() as db:
        record = db.get(IncomeRecord, record_id) if normalized_type == "income" else db.get(ExpenseRecord, record_id)
        if not record or record.user_id != uid:
            return RedirectResponse(url=redirect_target, status_code=303)

        _delete_attachment_file(record.attachment_path)
        record.attachment_path = None
        record.attachment_name = None
        db.commit()

    return RedirectResponse(url=redirect_target, status_code=303)


@router.get("/expenses/history", response_class=HTMLResponse)
def expense_history(
    request: Request,
    month: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    with SessionLocal() as db:
        # Fetch expense records
        expense_rows = (
            db.query(ExpenseRecord, Account)
            .join(Account, ExpenseRecord.account_id == Account.id)
            .filter(ExpenseRecord.user_id == uid, Account.user_id == uid)
            .order_by(ExpenseRecord.timestamp.desc())
            .all()
        )

        expense_records = [{
            "id": record.id,
            "account_id": account.id,
            "timestamp": _to_madrid(record.timestamp),
            "account": account.name,
            "name": record.name,
            "amount": record.amount,
            "currency": record.currency,
            "currency_sym": _sym(record.currency),
            "notes": record.notes or "",
            "period_label": record.period_label,
            "attachment_path": record.attachment_path,
            "attachment_name": record.attachment_name,
            "category": record.category or _categorize_expense(record.name),
            "is_recurring": record.schedule_id is not None,
            "type": "expense",
        } for record, account in expense_rows]

        # Fetch income records
        income_rows = (
            db.query(IncomeRecord, Account)
            .join(Account, IncomeRecord.account_id == Account.id)
            .filter(IncomeRecord.user_id == uid, Account.user_id == uid)
            .order_by(IncomeRecord.timestamp.desc())
            .all()
        )

        income_records = [{
            "id": record.id,
            "account_id": account.id,
            "timestamp": _to_madrid(record.timestamp),
            "account": account.name,
            "name": record.name,
            "amount": record.amount,
            "currency": record.currency,
            "currency_sym": _sym(record.currency),
            "notes": record.notes or "",
            "period_label": record.period_label,
            "attachment_path": record.attachment_path,
            "attachment_name": record.attachment_name,
            "is_recurring": record.schedule_id is not None,
            "type": "income",
        } for record, account in income_rows]

        # Fetch transfer records (cash moved between accounts)
        from_alias = aliased(Account)
        to_alias = aliased(Account)
        transfer_rows = (
            db.query(TransferRecord, from_alias, to_alias)
            .join(from_alias, TransferRecord.from_account_id == from_alias.id)
            .join(to_alias, TransferRecord.to_account_id == to_alias.id)
            .filter(TransferRecord.user_id == uid)
            .order_by(TransferRecord.timestamp.desc())
            .all()
        )

        transfer_records = [{
            "id": record.id,
            "timestamp": _to_madrid(record.timestamp),
            "from_account": from_account.name,
            "to_account": to_account.name,
            "name": record.notes or f"{from_account.name} → {to_account.name}",
            "amount": record.amount,
            "currency_sym": _sym(_get_account_currency(db, from_account.id)),
            "notes": record.notes or "",
            "attachment_path": None,
            "attachment_name": None,
            "is_recurring": record.schedule_id is not None,
            "type": "transfer",
        } for record, from_account, to_account in transfer_rows]

        # Combine and sort by timestamp descending
        all_records = sorted(
            expense_records + income_records + transfer_records,
            key=lambda x: x["timestamp"],
            reverse=True
        )

        recurring_expense_rows = (
            db.query(ExpenseSchedule, Account)
            .join(Account, ExpenseSchedule.account_id == Account.id)
            .filter(
                ExpenseSchedule.user_id == uid,
                ExpenseSchedule.enabled == 1,
                Account.user_id == uid,
            )
            .order_by(ExpenseSchedule.day_of_month, Account.name, ExpenseSchedule.name)
            .all()
        )
        recurring_expenses = []
        for schedule, account in recurring_expense_rows:
            currency = _get_account_currency(db, account.id)
            recurring_expenses.append({
                "id": schedule.id,
                "account_id": account.id,
                "account": account.name,
                "name": schedule.name,
                "amount": schedule.amount,
                "currency_sym": _sym(currency),
                "day": schedule.day_of_month,
                "last_executed_period": schedule.last_executed_period,
                "notes": schedule.notes or "",
                "category": schedule.category or _categorize_expense(schedule.name),
                "type": "expense",
            })

        recurring_income_rows = (
            db.query(IncomeSchedule, Account)
            .join(Account, IncomeSchedule.account_id == Account.id)
            .filter(
                IncomeSchedule.user_id == uid,
                IncomeSchedule.enabled == 1,
                Account.user_id == uid,
            )
            .order_by(IncomeSchedule.day_of_month, Account.name, IncomeSchedule.name)
            .all()
        )
        recurring_incomes = []
        for schedule, account in recurring_income_rows:
            currency = _get_account_currency(db, account.id)
            recurring_incomes.append({
                "id": schedule.id,
                "account_id": account.id,
                "account": account.name,
                "name": schedule.name,
                "amount": schedule.amount,
                "currency_sym": _sym(currency),
                "day": schedule.day_of_month,
                "last_executed_period": schedule.last_executed_period,
                "notes": schedule.notes or "",
                "type": "income",
            })

        recurring_transfer_rows = (
            db.query(TransferSchedule, from_alias, to_alias)
            .join(from_alias, TransferSchedule.from_account_id == from_alias.id)
            .join(to_alias, TransferSchedule.to_account_id == to_alias.id)
            .filter(
                TransferSchedule.user_id == uid,
                TransferSchedule.enabled == 1,
            )
            .order_by(TransferSchedule.day_of_month, from_alias.name, to_alias.name)
            .all()
        )
        recurring_transfers = []
        for schedule, from_account, to_account in recurring_transfer_rows:
            currency = _get_account_currency(db, from_account.id)
            recurring_transfers.append({
                "id": schedule.id,
                "from_account_id": from_account.id,
                "to_account_id": to_account.id,
                "from_account": from_account.name,
                "to_account": to_account.name,
                "name": schedule.notes or f"{from_account.name} → {to_account.name}",
                "amount": schedule.amount,
                "currency_sym": _sym(currency),
                "day": schedule.day_of_month,
                "last_executed_period": schedule.last_executed_period,
                "notes": schedule.notes or "",
                "type": "transfer",
            })

        current_month = datetime.now(MADRID_TZ).strftime("%Y-%m")
        total_spent = sum(r["amount"] for r in expense_records)
        total_earned = sum(r["amount"] for r in income_records)

        available_periods = sorted(
            {row["timestamp"].strftime("%Y-%m") for row in all_records},
            reverse=True,
        )
        if current_month not in available_periods:
            available_periods.insert(0, current_month)

        selected_month = ""
        if month and re.fullmatch(r"\d{4}-\d{2}", month.strip()):
            selected_month = month.strip()
            if selected_month not in available_periods:
                available_periods.insert(0, selected_month)

        # Text filter: only match the concept (name) and notes fields.
        search_term = (q or "").strip()
        if search_term:
            needle = search_term.lower()

            def _matches_search(row: dict) -> bool:
                fields = (
                    row.get("name"),
                    row.get("notes"),
                )
                return any(needle in (value or "").lower() for value in fields)

            filtered_records = [row for row in all_records if _matches_search(row)]
        else:
            filtered_records = all_records

        if selected_month:
            displayed_records = [
                row for row in filtered_records
                if row["timestamp"].strftime("%Y-%m") == selected_month
            ]
        else:
            displayed_records = filtered_records

        period_earned = sum(r["amount"] for r in displayed_records if r["type"] == "income")
        period_spent = sum(r["amount"] for r in displayed_records if r["type"] == "expense")
        period_balance = period_earned - period_spent
        selected_period_label = _period_label(selected_month) if selected_month else "Todo el histórico"

        available_months = [
            {"value": period, "label": _period_label(period)}
            for period in available_periods
        ]

        return_params = []
        if selected_month:
            return_params.append(f"month={quote_plus(selected_month)}")
        if search_term:
            return_params.append(f"q={quote_plus(search_term)}")
        return_url = "/expenses/history" + (
            ("?" + "&".join(return_params)) if return_params else ""
        )

        return templates.TemplateResponse(request, "expenses_history.html", {
            "request": request,
            "username": current_username(request),
        "language": current_language(request),
            "records": displayed_records,
            "accounts": db.query(Account).filter(Account.user_id == uid).order_by(Account.name).all(),
            "total_spent": total_spent,
            "total_earned": total_earned,
            "period_spent": period_spent,
            "period_earned": period_earned,
            "period_balance": period_balance,
            "selected_month": selected_month,
            "selected_period_label": selected_period_label,
            "available_months": available_months,
            "q": search_term,
            "return_url": return_url,
            "recurring_expenses": recurring_expenses,
            "recurring_incomes": recurring_incomes,
            "recurring_transfers": recurring_transfers,
            "expense_categories": _EXPENSE_CATEGORY_NAMES,
            "default_currency": DEFAULT_CURRENCY,
            "default_currency_sym": _sym(DEFAULT_CURRENCY),
        })


@router.post("/holdings/{holding_id}/edit")
def edit_holding(
    request: Request,
    holding_id: int,
    quantity: float = Form(...),
    average_cost: float = Form(...),
    notes: Optional[str] = Form(None),
    ter: Optional[str] = Form(None),
    tier_limit: Optional[str] = Form(None),
    tier_ter: Optional[str] = Form(None),
    split_date: Optional[str] = Form(None),
    split_ter: Optional[str] = Form(None),
    new_quantity: Optional[str] = Form(None),
    auto_enabled: Optional[str] = Form(None),
    auto_amount: Optional[str] = Form(None),
    auto_day: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir

    parsed_ter: Optional[float] = None
    if ter and ter.strip():
        try:
            parsed_ter = float(ter.strip())
        except (ValueError, AttributeError):
            pass

    parsed_tier_limit: Optional[float] = None
    if tier_limit and tier_limit.strip():
        try:
            parsed_tier_limit = float(tier_limit.strip())
        except (ValueError, AttributeError):
            pass

    parsed_tier_ter: Optional[float] = None
    if tier_ter and tier_ter.strip():
        try:
            parsed_tier_ter = float(tier_ter.strip())
        except (ValueError, AttributeError):
            pass

    parsed_split_date: Optional[datetime] = None
    if split_date and split_date.strip():
        try:
            parsed_split_date = datetime.fromisoformat(split_date.strip())
        except (ValueError, AttributeError):
            pass

    parsed_split_ter: Optional[float] = None
    if split_ter and split_ter.strip():
        try:
            parsed_split_ter = float(split_ter.strip())
        except (ValueError, AttributeError):
            pass

    parsed_new_quantity: Optional[float] = None
    if new_quantity and new_quantity.strip():
        try:
            parsed_new_quantity = float(new_quantity.strip())
        except (ValueError, AttributeError):
            pass

    with SessionLocal() as db:
        holding = db.get(Holding, holding_id)
        if holding and holding.account.user_id == uid:
            holding.quantity = quantity
            holding.average_cost = average_cost
            holding.notes = notes.strip() if notes else None
            if parsed_ter is not None:
                holding.asset.ter = parsed_ter
            if parsed_tier_limit is not None:
                holding.asset.tier_limit = parsed_tier_limit
            if parsed_tier_ter is not None:
                holding.asset.tier_ter = parsed_tier_ter
            if parsed_split_date is not None:
                holding.split_date = parsed_split_date
            if parsed_split_ter is not None:
                holding.split_ter = parsed_split_ter
            if parsed_new_quantity is not None:
                holding.new_quantity = parsed_new_quantity

            schedule = db.query(AutoContribution).filter(
                AutoContribution.user_id == uid,
                AutoContribution.holding_id == holding.id,
            ).first()

            wants_auto = auto_enabled == "on" and holding.asset.asset_type != "cash"
            parsed_auto_amount: Optional[float] = None
            parsed_auto_day: Optional[int] = None

            if wants_auto:
                try:
                    parsed_auto_amount = float((auto_amount or "").strip())
                    parsed_auto_day = int((auto_day or "").strip())
                except (ValueError, AttributeError):
                    wants_auto = False

            if wants_auto and parsed_auto_amount and parsed_auto_amount > 0 and parsed_auto_day and 1 <= parsed_auto_day <= 31:
                if schedule:
                    schedule.amount = parsed_auto_amount
                    schedule.day_of_month = parsed_auto_day
                    schedule.enabled = 1
                else:
                    db.add(AutoContribution(
                        user_id=uid,
                        holding_id=holding.id,
                        amount=parsed_auto_amount,
                        day_of_month=parsed_auto_day,
                        enabled=1,
                    ))
            elif schedule:
                db.delete(schedule)

            db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/holdings/{holding_id}/delete")
def delete_holding(request: Request, holding_id: int):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    with SessionLocal() as db:
        holding = db.get(Holding, holding_id)
        if holding and holding.account.user_id == uid:
            db.query(AutoContribution).filter(
                AutoContribution.user_id == uid,
                AutoContribution.holding_id == holding.id,
            ).delete()
            db.delete(holding)
            db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/holdings/{holding_id}/base-rate")
def set_base_rate(request: Request, holding_id: int, base_rate: str = Form("")):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    try:
        rate = float((base_rate or "").strip())
    except ValueError:
        rate = 0.0
    with SessionLocal() as db:
        holding = db.get(Holding, holding_id)
        if holding and holding.account.user_id == uid:
            holding.asset.ter = rate
            db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/holdings/{holding_id}/promos")
def add_interest_promo(
    request: Request,
    holding_id: int,
    label: Optional[str] = Form(None),
    rate: float = Form(...),
    mode: str = Form("balance"),
    cap: Optional[str] = Form(None),
    baseline: Optional[str] = Form(None),
    start_date: Optional[str] = Form(None),
    end_date: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    with SessionLocal() as db:
        holding = db.get(Holding, holding_id)
        if not holding or holding.account.user_id != uid:
            return RedirectResponse(url="/", status_code=303)

        def _f(s: Optional[str]) -> Optional[float]:
            try:
                return float(s.strip()) if s and s.strip() else None
            except (ValueError, AttributeError):
                return None

        def _dt(s: Optional[str]) -> Optional[datetime]:
            try:
                return datetime.fromisoformat(s.strip()) if s and s.strip() else None
            except (ValueError, AttributeError):
                return None

        promo = InterestPromo(
            user_id=uid,
            holding_id=holding.id,
            label=(label.strip() if label else None),
            rate=rate if rate > 0 else 0.0,
            mode=mode if mode in ("balance", "new") else "balance",
            cap=_f(cap),
            baseline=_f(baseline),
            start_date=_dt(start_date),
            end_date=_dt(end_date),
        )
        db.add(promo)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/promos/{promo_id}/delete")
def delete_interest_promo(request: Request, promo_id: int):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    with SessionLocal() as db:
        promo = db.get(InterestPromo, promo_id)
        if promo and promo.user_id == uid:
            db.delete(promo)
            db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/holdings/{holding_id}/close")
def close_holding(request: Request, holding_id: int):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    with SessionLocal() as db:
        holding = (
            db.query(Holding)
            .join(Account)
            .filter(Holding.id == holding_id, Account.user_id == uid)
            .first()
        )
        if not holding or holding.asset.asset_type == "cash":
            return RedirectResponse(url="/", status_code=303)

        proceeds = _round_money(holding.quantity * get_effective_price(holding.asset))
        account_id = holding.account_id
        holding_currency = holding.asset.currency

        cash_h = (
            db.query(Holding)
            .join(Asset)
            .filter(
                Holding.account_id == account_id,
                Asset.asset_type == "cash",
                Holding.id != holding_id,
            )
            .first()
        )

        if cash_h:
            cash_h.quantity = _round_money(cash_h.quantity + proceeds)
        else:
            new_cash_asset = Asset(
                name="Efectivo",
                asset_type="cash",
                currency=holding_currency,
                manual_price=1.0,
            )
            db.add(new_cash_asset)
            db.flush()
            db.add(Holding(
                account_id=account_id,
                asset_id=new_cash_asset.id,
                quantity=proceeds,
                average_cost=1.0,
            ))

        if holding.asset.ticker:
            db.add(Trade(
                user_id=uid,
                timestamp=datetime.utcnow(),
                ticker=holding.asset.ticker,
                trade_type="sell",
                quantity=holding.quantity,
                price=get_effective_price(holding.asset),
            ))
        db.query(AutoContribution).filter(
            AutoContribution.user_id == uid,
            AutoContribution.holding_id == holding.id,
        ).delete()
        db.delete(holding)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.get("/api/portfolio-history")
def portfolio_history(
    request: Request,
    period: str = Query("1m"),
    scope: str = Query("all"),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    now_utc = datetime.utcnow()
    now_madrid = datetime.now(MADRID_TZ)
    period_map: dict[str, timedelta] = {
        "1d": timedelta(days=1),
        "1w": timedelta(weeks=1),
        "1m": timedelta(days=30),
        "3m": timedelta(days=90),
        "6m": timedelta(days=180),
        "1y": timedelta(days=365),
    }
    delta = period_map.get(period)
    # ytd_start computed in Madrid calendar time, converted back to UTC for DB filtering
    ytd_start_utc = (
        now_madrid.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )

    allowed_scopes = {"all", "fund", "stock", "etf", "cash"}
    if scope not in allowed_scopes:
        scope = "all"

    ensure_portfolio_type_snapshots_table()

    with SessionLocal() as db:
        if scope == "all":
            q = db.query(PortfolioSnapshot).filter(PortfolioSnapshot.user_id == uid)
            if period == "ytd":
                q = q.filter(PortfolioSnapshot.timestamp >= ytd_start_utc)
            elif delta:
                q = q.filter(PortfolioSnapshot.timestamp >= now_utc - delta)
            snapshots = q.order_by(PortfolioSnapshot.timestamp).all()
        else:
            q = db.query(PortfolioTypeSnapshot).filter(
                PortfolioTypeSnapshot.user_id == uid,
                PortfolioTypeSnapshot.asset_type == scope,
            )
            if period == "ytd":
                q = q.filter(PortfolioTypeSnapshot.timestamp >= ytd_start_utc)
            elif delta:
                q = q.filter(PortfolioTypeSnapshot.timestamp >= now_utc - delta)
            snapshots = q.order_by(PortfolioTypeSnapshot.timestamp).all()

        last_refresh = db.query(func.max(Asset.last_updated)).scalar()

        tq = db.query(Trade).filter(Trade.user_id == uid)
        if period == "ytd":
            tq = tq.filter(Trade.timestamp >= ytd_start_utc)
        elif delta:
            tq = tq.filter(Trade.timestamp >= now_utc - delta)
        trades_raw = tq.order_by(Trade.timestamp).all()

    # Filter out snapshots outside market hours for market-linked scopes,
    # but keep full timeline for cash to reflect off-hours flows.
    if scope != "cash":
        trade_timestamps = {t.timestamp for t in trades_raw}

        def _keep(s: PortfolioSnapshot) -> bool:
            if _in_market_hours(_to_madrid(s.timestamp)):
                return True
            return any(abs((s.timestamp - tt).total_seconds()) <= 3600 for tt in trade_timestamps)

        snapshots = [s for s in snapshots if _keep(s)]

    labels = [_to_madrid(s.timestamp).strftime("%d %b %H:%M") for s in snapshots]
    values = [round(s.total_value, 2) for s in snapshots]
    costs = [round(s.total_cost, 2) if s.total_cost is not None else None for s in snapshots]

    # Daily PNL (skip for intraday)
    daily_pnl: list[dict] = []
    if len(snapshots) > 1 and period != "1d":
        daily_last: dict = {}
        for s in snapshots:
            daily_last[_to_madrid(s.timestamp).date()] = s  # group by Madrid date
        sorted_dates = sorted(daily_last.keys())
        for i in range(1, len(sorted_dates)):
            d = sorted_dates[i]
            prev_d = sorted_dates[i - 1]
            pnl_val = daily_last[d].total_value - daily_last[prev_d].total_value
            snap = daily_last[d]
            daily_pnl.append({
                "label": _to_madrid(snap.timestamp).strftime("%d %b %H:%M"),
                "value": round(pnl_val, 2),
            })

    # Map trades to nearest (post-filter) snapshot
    trades_data: list[dict] = []
    if scope == "all" and trades_raw and snapshots:
        snap_times = [s.timestamp for s in snapshots]
        for t in trades_raw:
            diffs = [abs((st - t.timestamp).total_seconds()) for st in snap_times]
            idx = diffs.index(min(diffs))
            trades_data.append({
                "label": labels[idx],
                "type": t.trade_type,
                "ticker": t.ticker or "",
                "value": values[idx],
            })

    return JSONResponse({
        "labels": labels,
        "values": values,
        "costs": costs,
        "daily_pnl": daily_pnl,
        "trades": trades_data,
        "scope": scope,
        "last_refresh": _to_madrid(last_refresh).strftime("%d %b %H:%M") if last_refresh else None,
    })


# ── holding detail (per-ticker evolution) ────────────────────────────────────
@router.get("/api/holding-detail")
def holding_detail(request: Request, holding_id: int = Query(..., ge=1)):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    with SessionLocal() as db:
        holding = db.get(Holding, holding_id)
        if holding is None:
            return JSONResponse({"error": "Posición no encontrada."}, status_code=404)
        asset = holding.asset
        account = holding.account
        price = get_effective_price(asset)
        quantity = holding.quantity
        avg_cost = holding.average_cost
        market_value = quantity * price
        cost_value = quantity * avg_cost
        pnl = market_value - cost_value
        pnl_pct = (pnl / cost_value * 100.0) if cost_value else 0.0
        name = (holding.notes or "").strip() or (asset.name or "")
        currency = asset.currency or DEFAULT_CURRENCY

        siblings = []
        for h in db.query(Holding).filter(Holding.asset_id == asset.id).all():
            if h.id == holding.id:
                continue
            siblings.append({
                "id": h.id,
                "account": h.account.name,
                "quantity": h.quantity,
                "market_value": round(h.quantity * get_effective_price(h.asset), 2),
            })

        ticker_url, ticker_source = _asset_source_url(asset.ticker)
        series = _fetch_asset_history(asset.ticker or "", asset.asset_type)
        series_payload = [
            {
                "date": d,
                "value": round(v * quantity, 2),
                "cost": round(avg_cost * quantity, 2),
            }
            for d, v in series
        ]
        source = "none"
        if series:
            source = "ft" if ((asset.asset_type or "") == "fund" or (asset.ticker or "").upper().endswith(".F")) else "yfinance"

        return JSONResponse({
            "holding": {
                "id": holding.id,
                "account": account.name,
                "asset": name,
                "ticker": asset.ticker or "-",
                "ticker_url": ticker_url,
                "ticker_source": ticker_source,
                "asset_type": asset.asset_type,
                "currency": currency,
                "currency_sym": _sym(currency),
                "quantity": quantity,
                "average_cost": avg_cost,
                "current_price": price,
                "market_value": round(market_value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            },
            "siblings": siblings,
            "series": series_payload,
            "source": source,
            "as_of": series[-1][0] if series else None,
        })


@router.get("/api/portfolio-allocation")
def portfolio_allocation(request: Request):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    with SessionLocal() as db:
        holdings = (
            db.query(Holding)
            .join(Account)
            .join(Asset)
            .filter(Account.user_id == uid, Holding.quantity > 0)
            .all()
        )
        return JSONResponse(_build_allocation_data(holdings))


# ── ticker lookup ─────────────────────────────────────────────────────────────
@router.get("/api/ticker-price")
def get_ticker_price(request: Request, ticker: str = Query(..., min_length=1)):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ticker = ticker.strip().upper()
    cached = _price_cache.get(ticker)
    if cached and time.time() - cached[0] < 300.0:
        return JSONResponse(cached[1])

    price: Optional[float] = None
    prev_close: Optional[float] = None
    currency: Optional[str] = None

    # Strategy 1: Try Finnhub quote
    client = _get_finnhub_client()
    if client:
        try:
            quote = client.quote(ticker)
            if quote:
                try:
                    v = float(quote.get("c", 0))
                    if v > 0:
                        price = v
                except Exception:
                    pass
                try:
                    v = float(quote.get("pc", 0))
                    if v > 0:
                        prev_close = v
                except Exception:
                    pass
        except Exception:
            pass

    # Strategy 2: Fallback to yfinance
    if price is None:
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            fi = t.fast_info
            try:
                v = float(fi.last_price)
                if v > 0:
                    price = v
            except Exception:
                pass
            try:
                v = float(fi.previous_close)
                if v > 0:
                    prev_close = v
            except Exception:
                pass
            try:
                currency = str(fi.currency)
            except Exception:
                pass
        except Exception:
            pass

    if price is None:
        price = fetch_latest_price(ticker)

    change = (price - prev_close) if (price is not None and prev_close is not None) else None
    change_pct = ((change / prev_close) * 100.0) if (change is not None and prev_close) else None

    payload = {
        "ticker": ticker,
        "price": round(price, 4) if price is not None else None,
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "change": round(change, 4) if change is not None else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "currency": currency,
    }
    if price is not None:
        _price_cache[ticker] = (time.time(), payload)
    return JSONResponse(payload)


# ── technical analysis endpoint ───────────────────────────────────────────────
@router.get("/api/ticker-analysis")
def get_ticker_analysis(request: Request, ticker: str = Query(..., min_length=1)):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ticker = ticker.strip().upper()
    cached = _analysis_cache.get(ticker)
    if cached and time.time() - cached[0] < 3600.0:
        return JSONResponse(cached[1])

    try:
        hist = _fetch_analysis_history(ticker)
        if hist.empty or len(hist) < 20:
            return JSONResponse({"error": "Datos insuficientes para el análisis."}, status_code=422)

        close = hist["Close"]
        current_price = float(close.iloc[-1])

        # RSI
        rsi = _compute_rsi(close)

        # MACD (12, 26, 9)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - signal_line
        macd_val = float(macd_line.iloc[-1])
        sig_val = float(signal_line.iloc[-1])
        macd_hist_val = float(macd_hist.iloc[-1])

        # Moving averages
        def _sma(n: int) -> Optional[float]:
            if len(close) < n:
                return None
            v = float(close.rolling(n).mean().iloc[-1])
            return None if v != v else v  # NaN guard

        sma20 = _sma(20)
        sma50 = _sma(50)
        sma200 = _sma(200)

        # Bollinger Bands (20, 2)
        roll20 = close.rolling(20)
        bb_mid = float(roll20.mean().iloc[-1])
        bb_std = float(roll20.std().iloc[-1])
        upper_bb = bb_mid + 2 * bb_std
        lower_bb = bb_mid - 2 * bb_std

        # Support / resistance
        sr = _find_support_resistance(hist)
        nearest_support = sr["nearest_support"]
        nearest_resistance = sr["nearest_resistance"]
        support_pct = ((current_price - nearest_support) / current_price * 100) if nearest_support else None
        resistance_pct = ((nearest_resistance - current_price) / current_price * 100) if nearest_resistance else None

        # ── Signal scoring ────────────────────────────────────────────────
        score = 0
        signals: list[dict] = []

        # RSI
        if rsi < 30:
            score += 2
            signals.append({"type": "buy", "text": f"RSI sobrevendido ({rsi:.1f})"})
        elif rsi < 45:
            score += 1
            signals.append({"type": "buy", "text": f"RSI en zona baja ({rsi:.1f})"})
        elif rsi > 70:
            score -= 2
            signals.append({"type": "sell", "text": f"RSI sobrecomprado ({rsi:.1f})"})
        elif rsi > 60:
            score -= 1
            signals.append({"type": "sell", "text": f"RSI en zona alta ({rsi:.1f})"})
        else:
            signals.append({"type": "neutral", "text": f"RSI neutro ({rsi:.1f})"})

        # MACD
        if macd_hist_val > 0:
            score += 1
            signals.append({"type": "buy", "text": "MACD alcista"})
        elif macd_hist_val < 0:
            score -= 1
            signals.append({"type": "sell", "text": "MACD bajista"})

        # Price vs SMA50
        if sma50:
            if current_price > sma50:
                score += 1
                signals.append({"type": "buy", "text": f"Precio sobre SMA50 ({sma50:.2f})"})
            else:
                score -= 1
                signals.append({"type": "sell", "text": f"Precio bajo SMA50 ({sma50:.2f})"})

        # Golden / Death cross
        if sma50 and sma200:
            if sma50 > sma200:
                score += 1
                signals.append({"type": "buy", "text": "Cruz dorada: SMA50 > SMA200"})
            else:
                score -= 1
                signals.append({"type": "sell", "text": "Cruz de la muerte: SMA50 < SMA200"})

        # Bollinger Bands
        if current_price < lower_bb:
            score += 1
            signals.append({"type": "buy", "text": "Precio bajo banda inferior de Bollinger"})
        elif current_price > upper_bb:
            score -= 1
            signals.append({"type": "sell", "text": "Precio sobre banda superior de Bollinger"})

        # Support / resistance proximity
        if nearest_support and support_pct is not None and support_pct < 3.0:
            score += 1
            signals.append({"type": "buy", "text": f"Cerca de soporte {nearest_support:.2f} (↓{support_pct:.1f}%)"})
        if nearest_resistance and resistance_pct is not None and resistance_pct < 3.0:
            score -= 1
            signals.append({"type": "sell", "text": f"Cerca de resistencia {nearest_resistance:.2f} (↑{resistance_pct:.1f}%)"})

        # ── Final verdict ─────────────────────────────────────────────────
        max_score = 9
        conviction_pct = min(abs(score) / max_score * 100, 100)

        if score >= 3:
            action, action_en = "COMPRAR", "buy"
            conviction_label = "Alta" if conviction_pct >= 55 else "Moderada"
        elif score >= 1:
            action, action_en = "COMPRAR", "buy"
            conviction_label = "Baja"
        elif score <= -3:
            action, action_en = "VENDER", "sell"
            conviction_label = "Alta" if conviction_pct >= 55 else "Moderada"
        elif score <= -1:
            action, action_en = "VENDER", "sell"
            conviction_label = "Baja"
        else:
            action, action_en = "MANTENER", "hold"
            conviction_label = "Moderada"

        payload = {
            "ticker": ticker,
            "action": action,
            "action_en": action_en,
            "conviction": conviction_label,
            "conviction_pct": round(conviction_pct, 1),
            "score": score,
            "signals": signals,
            "indicators": {
                "rsi": round(rsi, 1),
                "macd_hist": round(macd_hist_val, 4),
                "macd": round(macd_val, 4),
                "macd_signal": round(sig_val, 4),
                "sma20": round(sma20, 2) if sma20 else None,
                "sma50": round(sma50, 2) if sma50 else None,
                "sma200": round(sma200, 2) if sma200 else None,
                "bollinger_upper": round(upper_bb, 2),
                "bollinger_lower": round(lower_bb, 2),
                "bollinger_mid": round(bb_mid, 2),
                "current_price": round(current_price, 4),
            },
            "support_resistance": {
                "nearest_support": round(nearest_support, 4) if nearest_support else None,
                "nearest_resistance": round(nearest_resistance, 4) if nearest_resistance else None,
                "support_pct": round(support_pct, 2) if support_pct is not None else None,
                "resistance_pct": round(resistance_pct, 2) if resistance_pct is not None else None,
                "all_supports": [round(s, 2) for s in sr["all_supports"]],
                "all_resistances": [round(r, 2) for r in sr["all_resistances"]],
            },
        }
        _analysis_cache[ticker] = (time.time(), payload)
        return JSONResponse(payload)
    except Exception as exc:
        msg = str(exc)
        if any(token in msg.lower() for token in ("too many requests", "rate limited", "429", "rate limit")):
            return JSONResponse(
                {"error": "Límite de consultas a Yahoo Finance alcanzado. Espera un momento y vuelve a intentarlo."},
                status_code=429,
            )
        return JSONResponse({"error": msg}, status_code=500)


# ── watchlist endpoints ───────────────────────────────────────────────────────
@router.post("/watchlist/add")
def watchlist_add(
    request: Request,
    ticker: str = Form(...),
    name: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ticker = ticker.strip().upper()
    if not ticker:
        return JSONResponse({"error": "ticker required"}, status_code=400)

    with SessionLocal() as db:
        exists = db.query(WatchlistItem).filter(
            WatchlistItem.user_id == uid,
            WatchlistItem.ticker == ticker,
        ).first()
        if exists:
            return JSONResponse({"ok": True, "duplicate": True, "id": exists.id})
        item = WatchlistItem(user_id=uid, ticker=ticker, name=(name or "").strip() or None)
        db.add(item)
        db.commit()
        db.refresh(item)
        return JSONResponse({"ok": True, "id": item.id})


@router.post("/watchlist/{item_id}/delete")
def watchlist_delete(request: Request, item_id: int):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    with SessionLocal() as db:
        item = db.get(WatchlistItem, item_id)
        if item and item.user_id == uid:
            db.delete(item)
            db.commit()
    return JSONResponse({"ok": True})


@router.get("/api/watchlist-prices")
def get_watchlist_prices(request: Request):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    with SessionLocal() as db:
        items = db.query(WatchlistItem).filter(WatchlistItem.user_id == uid).all()
        items_data = [{"id": i.id, "ticker": i.ticker, "name": i.name} for i in items]

    if not items_data:
        return JSONResponse([])

    tickers = [i["ticker"] for i in items_data]
    prices = _download_prices(tickers)

    # Fallback: for tickers that got no price, try Finnhub or yfinance individually
    missing = [t for t in tickers if prices.get(t) is None]
    if missing:
        client = _get_finnhub_client()

        if client:
            # Use Finnhub for missing prices
            def _finnhub_fallback(ticker: str) -> tuple[str, Optional[float]]:
                try:
                    quote = client.quote(ticker)
                    if quote and quote.get("c"):
                        v = float(quote["c"])
                        if v > 0:
                            return ticker, v
                except Exception:
                    pass
                return ticker, None

            with ThreadPoolExecutor(max_workers=min(len(missing), 5)) as ex:
                for tk, price in ex.map(_finnhub_fallback, missing):
                    if price is not None:
                        prices[tk] = price

        # Final fallback: yfinance for remaining missing
        still_missing = [t for t in missing if prices.get(t) is None]
        if still_missing:
            def _yfinance_fallback(ticker: str) -> tuple[str, Optional[float]]:
                try:
                    import yfinance as yf
                    fi = yf.Ticker(ticker).fast_info
                    for attr in ("last_price", "previous_close"):
                        try:
                            v = float(getattr(fi, attr))
                            if v > 0:
                                return ticker, v
                        except Exception:
                            pass
                except Exception:
                    pass
                return ticker, None

            with ThreadPoolExecutor(max_workers=min(len(still_missing), 6)) as ex:
                for tk, price in ex.map(_yfinance_fallback, still_missing):
                    if price is not None:
                        prices[tk] = price

    result = []
    for item in items_data:
        price = prices.get(item["ticker"])
        result.append({
            "id": item["id"],
            "ticker": item["ticker"],
            "name": item["name"] or item["ticker"],
            "price": round(price, 4) if price is not None else None,
        })
    return JSONResponse(result)


# ── calendar view ─────────────────────────────────────────────────────────────
_WEEKDAYS_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
_MONTHS_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


def _schedule_fires_on(day_of_month: int, day: int, year: int, month: int) -> bool:
    last_day = monthrange(year, month)[1]
    return min(day_of_month, last_day) == day


def _day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) covering the Madrid calendar day of `d`."""
    local_start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=MADRID_TZ)
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _month_bounds_utc(year: int, month: int) -> tuple[datetime, datetime]:
    local_start = datetime(year, month, 1, 0, 0, 0, tzinfo=MADRID_TZ)
    if month == 12:
        local_end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=MADRID_TZ)
    else:
        local_end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=MADRID_TZ)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _year_bounds_utc(year: int) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) covering a whole calendar year in Madrid time."""
    local_start = datetime(year, 1, 1, 0, 0, 0, tzinfo=MADRID_TZ)
    local_end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=MADRID_TZ)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _parse_anchor(raw: Optional[str], today: date) -> date:
    """Parse a YYYY-MM-DD anchor date, falling back to `today` when invalid."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (raw or "").strip())
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            pass
    return today


def _local_datetime_to_utc(d: date, time_str: Optional[str]) -> datetime:
    """Convert a Madrid local date + optional HH:MM time to a naive UTC datetime."""
    hour, minute = 12, 0
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (time_str or "").strip())
    if m:
        hour = int(m.group(1)) % 24
        minute = int(m.group(2)) % 60
    local = datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=MADRID_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_optional_int(value) -> Optional[int]:
    """Coerce a form value (str/int/None) to an optional int, treating '' as None."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fetch_accounts(db, user_id: int) -> dict[int, str]:
    return {a.id: a.name for a in db.query(Account).filter(Account.user_id == user_id).all()}


def _fetch_records(db, user_id: int, start_utc: datetime, end_utc: datetime, accounts: dict) -> list[dict]:
    records: list[dict] = []
    for r in db.query(ExpenseRecord).filter(
            ExpenseRecord.user_id == user_id,
            ExpenseRecord.timestamp >= start_utc,
            ExpenseRecord.timestamp < end_utc).order_by(ExpenseRecord.timestamp).all():
        records.append({
            "type": "expense",
            "id": r.id,
            "account_id": r.account_id,
            "name": r.name,
            "amount": r.amount,
            "currency_sym": _sym(r.currency),
            "account": accounts.get(r.account_id, "?"),
            "category": r.category or _categorize_expense(r.name),
            "notes": r.notes or "",
            "time": _to_madrid(r.timestamp).strftime("%H:%M"),
            "date": _to_madrid(r.timestamp).date().isoformat(),
            "is_recurring": r.schedule_id is not None,
            "attachment_path": r.attachment_path,
            "attachment_name": r.attachment_name,
        })
    for r in db.query(IncomeRecord).filter(
            IncomeRecord.user_id == user_id,
            IncomeRecord.timestamp >= start_utc,
            IncomeRecord.timestamp < end_utc).order_by(IncomeRecord.timestamp).all():
        records.append({
            "type": "income",
            "id": r.id,
            "account_id": r.account_id,
            "name": r.name,
            "amount": r.amount,
            "currency_sym": _sym(r.currency),
            "account": accounts.get(r.account_id, "?"),
            "notes": r.notes or "",
            "time": _to_madrid(r.timestamp).strftime("%H:%M"),
            "date": _to_madrid(r.timestamp).date().isoformat(),
            "is_recurring": r.schedule_id is not None,
            "attachment_path": r.attachment_path,
            "attachment_name": r.attachment_name,
        })
    for r in db.query(TransferRecord).filter(
            TransferRecord.user_id == user_id,
            TransferRecord.timestamp >= start_utc,
            TransferRecord.timestamp < end_utc).order_by(TransferRecord.timestamp).all():
        records.append({
            "type": "transfer",
            "id": r.id,
            "from_account_id": r.from_account_id,
            "to_account_id": r.to_account_id,
            "name": r.notes or "Transferencia",
            "amount": r.amount,
            "currency_sym": _sym(_get_account_currency(db, r.from_account_id)),
            "from_account": accounts.get(r.from_account_id, "?"),
            "to_account": accounts.get(r.to_account_id, "?"),
            "notes": r.notes or "",
            "time": _to_madrid(r.timestamp).strftime("%H:%M"),
            "date": _to_madrid(r.timestamp).date().isoformat(),
            "is_recurring": r.schedule_id is not None,
        })
    return records


def _fetch_schedules(db, user_id: int):
    return (
        db.query(ExpenseSchedule).filter(ExpenseSchedule.user_id == user_id, ExpenseSchedule.enabled == 1).all(),
        db.query(IncomeSchedule).filter(IncomeSchedule.user_id == user_id, IncomeSchedule.enabled == 1).all(),
        db.query(TransferSchedule).filter(TransferSchedule.user_id == user_id, TransferSchedule.enabled == 1).all(),
    )


def _scheduled_for_day(db, d: date, accounts: dict, expense_schedules, income_schedules, transfer_schedules) -> list[dict]:
    scheduled: list[dict] = []
    for s in expense_schedules:
        if _schedule_fires_on(s.day_of_month, d.day, d.year, d.month):
            scheduled.append({
                "type": "expense",
                "name": s.name,
                "amount": s.amount,
                "currency_sym": _sym(_get_account_currency(db, s.account_id)),
                "account": accounts.get(s.account_id, "?"),
                "category": s.category or _categorize_expense(s.name),
                "notes": s.notes or "",
            })
    for s in income_schedules:
        if _schedule_fires_on(s.day_of_month, d.day, d.year, d.month):
            scheduled.append({
                "type": "income",
                "name": s.name,
                "amount": s.amount,
                "currency_sym": _sym(_get_account_currency(db, s.account_id)),
                "account": accounts.get(s.account_id, "?"),
                "notes": s.notes or "",
            })
    for s in transfer_schedules:
        if _schedule_fires_on(s.day_of_month, d.day, d.year, d.month):
            scheduled.append({
                "type": "transfer",
                "name": s.notes or "Transferencia",
                "amount": s.amount,
                "currency_sym": _sym(_get_account_currency(db, s.from_account_id)),
                "from_account": accounts.get(s.from_account_id, "?"),
                "to_account": accounts.get(s.to_account_id, "?"),
                "notes": s.notes or "",
            })
    return scheduled


def _scheduled_net_for_day(expense_schedules, income_schedules, d: date) -> float:
    """Net scheduled cash flow on a day (income minus expense); transfers are neutral."""
    net = 0.0
    for s in expense_schedules:
        if _schedule_fires_on(s.day_of_month, d.day, d.year, d.month):
            net -= s.amount
    for s in income_schedules:
        if _schedule_fires_on(s.day_of_month, d.day, d.year, d.month):
            net += s.amount
    return net


def _project_cash_balances(db, uid: int, start: date, end: date, today: date) -> dict[str, float]:
    """Running projected total cash balance per day over [start, end].

    Anchored at `today` (cash on hand right now), projected forward using
    scheduled incomes/expenses and reconstructed backward using executed records.
    """
    if start > end:
        return {}
    lo = min(start, today)
    hi = max(end, today)

    current_cash = sum(
        h.quantity
        for h in db.query(Holding)
        .join(Account)
        .join(Asset)
        .filter(Account.user_id == uid, Asset.asset_type == "cash")
        .all()
    )

    start_utc, _ = _day_bounds_utc(lo)
    _, end_utc = _day_bounds_utc(hi + timedelta(days=1))

    net_by_day: dict[str, float] = {}
    for r in db.query(ExpenseRecord).filter(
            ExpenseRecord.user_id == uid,
            ExpenseRecord.timestamp >= start_utc,
            ExpenseRecord.timestamp < end_utc).all():
        iso = _to_madrid(r.timestamp).date().isoformat()
        net_by_day[iso] = net_by_day.get(iso, 0.0) - r.amount
    for r in db.query(IncomeRecord).filter(
            IncomeRecord.user_id == uid,
            IncomeRecord.timestamp >= start_utc,
            IncomeRecord.timestamp < end_utc).all():
        iso = _to_madrid(r.timestamp).date().isoformat()
        net_by_day[iso] = net_by_day.get(iso, 0.0) + r.amount

    expense_schedules, income_schedules, _ = _fetch_schedules(db, uid)
    d = today + timedelta(days=1)
    while d <= hi:
        iso = d.isoformat()
        net = _scheduled_net_for_day(expense_schedules, income_schedules, d)
        if net:
            net_by_day[iso] = net_by_day.get(iso, 0.0) + net
        d += timedelta(days=1)

    balances: dict[str, float] = {today.isoformat(): current_cash}
    d = today + timedelta(days=1)
    prev_iso = today.isoformat()
    while d <= hi:
        iso = d.isoformat()
        balances[iso] = balances[prev_iso] + net_by_day.get(iso, 0.0)
        prev_iso = iso
        d += timedelta(days=1)
    d = today - timedelta(days=1)
    next_iso = today.isoformat()
    while d >= lo:
        iso = d.isoformat()
        balances[iso] = balances[next_iso] - net_by_day.get(next_iso, 0.0)
        next_iso = iso
        d -= timedelta(days=1)

    return balances


def _build_day_detail(db, uid: int, d: date) -> dict:
    start_utc, end_utc = _day_bounds_utc(d)
    accounts = _fetch_accounts(db, uid)
    records = _fetch_records(db, uid, start_utc, end_utc, accounts)
    today = datetime.now(MADRID_TZ).date()
    scheduled = []
    if d > today:
        scheduled = _scheduled_for_day(db, d, accounts, *_fetch_schedules(db, uid))
    balances = _project_cash_balances(db, uid, d, d, today)
    return {
        "label": f"{_WEEKDAYS_ES[d.weekday()]} {d.day} {_MONTHS_ES[d.month - 1]} {d.year}",
        "records": records,
        "scheduled": scheduled,
        "totals": {
            "spent": sum(r["amount"] for r in records if r["type"] == "expense"),
            "earned": sum(r["amount"] for r in records if r["type"] == "income"),
        },
        "balance": balances.get(d.isoformat()),
    }


def _calendar_month_context(db, uid: int, year: int, month: int, today: date) -> dict:
    start_utc, end_utc = _month_bounds_utc(year, month)
    accounts = _fetch_accounts(db, uid)
    records = _fetch_records(db, uid, start_utc, end_utc, accounts)
    expense_schedules, income_schedules, transfer_schedules = _fetch_schedules(db, uid)

    spent_by_day: dict[str, float] = {}
    earned_by_day: dict[str, float] = {}
    transfer_by_day: dict[str, int] = {}
    record_days: set[str] = set()
    for r in records:
        iso = r["date"]
        if r["type"] == "expense":
            spent_by_day[iso] = spent_by_day.get(iso, 0.0) + r["amount"]
        elif r["type"] == "income":
            earned_by_day[iso] = earned_by_day.get(iso, 0.0) + r["amount"]
        else:
            transfer_by_day[iso] = transfer_by_day.get(iso, 0) + 1
        record_days.add(iso)

    last_day = monthrange(year, month)[1]
    scheduled_days: set[str] = set()
    for schedules in (expense_schedules, income_schedules, transfer_schedules):
        for s in schedules:
            fire_date = datetime(year, month, min(s.day_of_month, last_day)).date()
            if fire_date > today:
                scheduled_days.add(fire_date.isoformat())

    first = datetime(year, month, 1)
    offset = first.weekday()  # Monday = 0
    total_cells = ((offset + last_day + 6) // 7) * 7

    grid_start = (first - timedelta(days=offset)).date()
    grid_end = grid_start + timedelta(days=total_cells - 1)
    balances = _project_cash_balances(db, uid, grid_start, grid_end, today)

    cells = []
    for i in range(total_cells):
        cd = (first - timedelta(days=offset)) + timedelta(days=i)
        iso = cd.date().isoformat()
        cells.append({
            "date": iso,
            "day": cd.day,
            "in_month": cd.year == year and cd.month == month,
            "is_today": iso == today.isoformat(),
            "is_future": cd.date() > today,
            "spent": spent_by_day.get(iso, 0.0),
            "earned": earned_by_day.get(iso, 0.0),
            "transfers": transfer_by_day.get(iso, 0),
            "has_records": iso in record_days,
            "has_scheduled": iso in scheduled_days,
            "balance": balances.get(iso),
        })
    weeks = []
    for i in range(0, len(cells), 7):
        row = cells[i:i + 7]
        monday = (first - timedelta(days=offset)) + timedelta(days=i)
        weeks.append({"week_number": monday.date().isocalendar().week, "days": row})

    if month == 1:
        prev_date = datetime(year - 1, 12, 1).date().isoformat()
    else:
        prev_date = datetime(year, month - 1, 1).date().isoformat()
    if month == 12:
        next_date = datetime(year + 1, 1, 1).date().isoformat()
    else:
        next_date = datetime(year, month + 1, 1).date().isoformat()

    return {
        "title": f"{_MONTHS_ES[month - 1]} {year}",
        "weeks": weeks,
        "prev_date": prev_date,
        "next_date": next_date,
    }


def _calendar_week_context(db, uid: int, anchor: date, today: date) -> dict:
    monday = anchor - timedelta(days=anchor.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]
    start_utc, _ = _day_bounds_utc(days[0])
    _, end_utc = _day_bounds_utc(days[-1] + timedelta(days=1))
    accounts = _fetch_accounts(db, uid)
    records = _fetch_records(db, uid, start_utc, end_utc, accounts)
    expense_schedules, income_schedules, transfer_schedules = _fetch_schedules(db, uid)

    by_date: dict[str, list[dict]] = {}
    for r in records:
        by_date.setdefault(r["date"], []).append(r)

    balances = _project_cash_balances(db, uid, days[0], days[-1], today)

    week_days = []
    for d in days:
        iso = d.isoformat()
        day_records = by_date.get(iso, [])
        scheduled = []
        if d > today:
            scheduled = _scheduled_for_day(db, d, accounts, expense_schedules, income_schedules, transfer_schedules)
        week_days.append({
            "date": iso,
            "day": d.day,
            "weekday": _WEEKDAYS_ES[d.weekday()],
            "is_today": d == today,
            "is_future": d > today,
            "spent": sum(r["amount"] for r in day_records if r["type"] == "expense"),
            "earned": sum(r["amount"] for r in day_records if r["type"] == "income"),
            "transfers": sum(1 for r in day_records if r["type"] == "transfer"),
            "records": day_records,
            "scheduled": scheduled,
            "balance": balances.get(iso),
        })

    first, last = days[0], days[-1]
    title = f"Semana {monday.isocalendar().week} · {first.day} {_MONTHS_ES[first.month - 1]} – {last.day} {_MONTHS_ES[last.month - 1]} {last.year}"
    return {
        "title": title,
        "week_days": week_days,
        "prev_date": (monday - timedelta(days=7)).isoformat(),
        "next_date": (monday + timedelta(days=7)).isoformat(),
    }


def _calendar_year_context(db, uid: int, year: int, today: date) -> dict:
    start_utc, end_utc = _year_bounds_utc(year)
    accounts = _fetch_accounts(db, uid)
    records = _fetch_records(db, uid, start_utc, end_utc, accounts)
    expense_schedules, income_schedules, transfer_schedules = _fetch_schedules(db, uid)

    spent_by_day: dict[str, float] = {}
    earned_by_day: dict[str, float] = {}
    transfer_by_day: dict[str, int] = {}
    record_days: set[str] = set()
    for r in records:
        iso = r["date"]
        if r["type"] == "expense":
            spent_by_day[iso] = spent_by_day.get(iso, 0.0) + r["amount"]
        elif r["type"] == "income":
            earned_by_day[iso] = earned_by_day.get(iso, 0.0) + r["amount"]
        else:
            transfer_by_day[iso] = transfer_by_day.get(iso, 0) + 1
        record_days.add(iso)

    scheduled_days: set[str] = set()
    for schedules in (expense_schedules, income_schedules, transfer_schedules):
        for s in schedules:
            for m in range(1, 13):
                fire_date = datetime(year, m, min(s.day_of_month, monthrange(year, m)[1])).date()
                if fire_date > today:
                    scheduled_days.add(fire_date.isoformat())

    balances = _project_cash_balances(db, uid, date(year, 1, 1), date(year, 12, 31), today)

    months = []
    for m in range(1, 13):
        first = datetime(year, m, 1)
        offset = first.weekday()
        last_day = monthrange(year, m)[1]
        total_cells = ((offset + last_day + 6) // 7) * 7
        cells = []
        for i in range(total_cells):
            cd = (first - timedelta(days=offset)) + timedelta(days=i)
            if cd.year != year or cd.month != m:
                cells.append(None)
                continue
            iso = cd.date().isoformat()
            cells.append({
                "day": cd.day,
                "date": iso,
                "is_today": iso == today.isoformat(),
                "spent": spent_by_day.get(iso, 0.0),
                "earned": earned_by_day.get(iso, 0.0),
                "transfers": transfer_by_day.get(iso, 0),
                "has_records": iso in record_days,
                "has_scheduled": iso in scheduled_days,
                "balance": balances.get(iso),
            })
        weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
        months.append({
            "number": m,
            "name": _MONTHS_ES[m - 1],
            "month_anchor": f"{year:04d}-{m:02d}-01",
            "weeks": weeks,
        })

    return {
        "title": str(year),
        "months": months,
        "prev_date": f"{year - 1:04d}-01-01",
        "next_date": f"{year + 1:04d}-01-01",
    }


def _calendar_day_context(db, uid: int, anchor: date, today: date) -> dict:
    detail = _build_day_detail(db, uid, anchor)
    return {
        "title": detail["label"],
        "day_records": detail["records"],
        "day_scheduled": detail["scheduled"],
        "day_totals": detail["totals"],
        "day_balance": detail["balance"],
        "is_today": anchor == today,
        "prev_date": (anchor - timedelta(days=1)).isoformat(),
        "next_date": (anchor + timedelta(days=1)).isoformat(),
    }


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, view: str = Query("month"), date: Optional[str] = Query(None), focus: Optional[str] = Query(None)):
    uid, redir = _require_auth(request)
    if redir:
        return redir
    if view not in {"month", "week", "day", "year"}:
        view = "month"

    now_madrid = datetime.now(MADRID_TZ)
    today = now_madrid.date()
    anchor = _parse_anchor(date, today)

    with SessionLocal() as db:
        accounts = [
            {"id": a.id, "name": a.name}
            for a in db.query(Account).filter(Account.user_id == uid).order_by(Account.name).all()
        ]
        if view == "month":
            ctx = _calendar_month_context(db, uid, anchor.year, anchor.month, today)
        elif view == "week":
            ctx = _calendar_week_context(db, uid, anchor, today)
        elif view == "year":
            ctx = _calendar_year_context(db, uid, anchor.year, today)
        else:
            ctx = _calendar_day_context(db, uid, anchor, today)

    server_records = []
    if view == "week":
        for wd in ctx["week_days"]:
            server_records.extend(wd["records"])
    elif view == "day":
        server_records = ctx["day_records"]

    ctx.update({
        "request": request,
        "username": current_username(request),
        "language": current_language(request),
        "view": view,
        "anchor": anchor.isoformat(),
        "today": today.isoformat(),
        "weekdays": _WEEKDAYS_ES,
        "default_currency_sym": _sym(DEFAULT_CURRENCY),
        "accounts": accounts,
        "expense_categories": _EXPENSE_CATEGORY_NAMES,
        "server_records": server_records,
        "focus": focus,
    })
    return templates.TemplateResponse(request, "calendar.html", ctx)


@router.get("/api/calendar/day")
def calendar_day(request: Request, date: str = Query(..., min_length=10, max_length=10)):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"error": "Fecha inválida"}, status_code=400)

    with SessionLocal() as db:
        detail = _build_day_detail(db, uid, d)
    return JSONResponse({"date": d.isoformat(), **detail})


@router.post("/api/calendar/day/movement")
def calendar_create_movement(
    request: Request,
    type: str = Form(...),
    date: str = Form(...),
    account_id: Optional[str] = Form(None),
    from_account_id: Optional[str] = Form(None),
    to_account_id: Optional[str] = Form(None),
    name: str = Form(""),
    amount: float = Form(...),
    category: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    time: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"error": "Fecha inválida"}, status_code=400)

    account_id = _parse_optional_int(account_id)
    from_account_id = _parse_optional_int(from_account_id)
    to_account_id = _parse_optional_int(to_account_id)

    ts = _local_datetime_to_utc(d, time)
    cleaned_name = name.strip()
    cleaned_notes = notes.strip() if notes else None
    if amount <= 0:
        return JSONResponse({"error": "El importe debe ser mayor que cero"}, status_code=400)
    if type in ("expense", "income") and not cleaned_name:
        return JSONResponse({"error": "El nombre es obligatorio"}, status_code=400)

    with SessionLocal() as db:
        try:
            if type == "transfer":
                if not from_account_id or not to_account_id:
                    return JSONResponse({"error": "Selecciona las cuentas de origen y destino"}, status_code=400)
                _execute_transfer(
                    db,
                    user_id=uid,
                    from_account_id=from_account_id,
                    to_account_id=to_account_id,
                    amount=amount,
                    notes=cleaned_notes,
                    timestamp=ts,
                )
            elif type in ("expense", "income"):
                if not account_id:
                    return JSONResponse({"error": "Selecciona una cuenta"}, status_code=400)
                try:
                    attachment_path, attachment_name = _save_record_attachment(
                        attachment,
                        user_id=uid,
                        record_type="expenses" if type == "expense" else "incomes",
                    )
                except ValueError:
                    return JSONResponse({"error": "Adjunto no válido"}, status_code=400)
                _execute_cash_flow(
                    db,
                    record_cls=ExpenseRecord if type == "expense" else IncomeRecord,
                    sign=-1 if type == "expense" else 1,
                    user_id=uid,
                    account_id=account_id,
                    name=cleaned_name,
                    amount=amount,
                    notes=cleaned_notes,
                    category=(category.strip() if category else None) if type == "expense" else None,
                    attachment_path=attachment_path,
                    attachment_name=attachment_name,
                    timestamp=ts,
                )
            else:
                return JSONResponse({"error": "Tipo no válido"}, status_code=400)
            db.commit()
            detail = _build_day_detail(db, uid, d)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({"date": d.isoformat(), **detail})


@router.post("/api/calendar/day/movement/{record_id}/edit")
def calendar_edit_movement(
    request: Request,
    record_id: int,
    type: str = Form(...),
    date: str = Form(...),
    account_id: Optional[str] = Form(None),
    from_account_id: Optional[str] = Form(None),
    to_account_id: Optional[str] = Form(None),
    name: str = Form(""),
    amount: float = Form(...),
    category: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    time: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None),
    remove_attachment: Optional[str] = Form(None),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"error": "Fecha inválida"}, status_code=400)

    account_id = _parse_optional_int(account_id)
    from_account_id = _parse_optional_int(from_account_id)
    to_account_id = _parse_optional_int(to_account_id)

    ts = _local_datetime_to_utc(d, time)
    cleaned_name = name.strip()
    cleaned_notes = notes.strip() if notes else None
    if amount <= 0:
        return JSONResponse({"error": "El importe debe ser mayor que cero"}, status_code=400)
    if type in ("expense", "income") and not cleaned_name:
        return JSONResponse({"error": "El nombre es obligatorio"}, status_code=400)

    with SessionLocal() as db:
        try:
            if type == "expense":
                record = db.get(ExpenseRecord, record_id)
                if not record or record.user_id != uid:
                    return JSONResponse({"error": "Movimiento no encontrado"}, status_code=404)
                if not account_id:
                    return JSONResponse({"error": "Selecciona una cuenta"}, status_code=400)
                target_account = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
                if not target_account:
                    return JSONResponse({"error": "Cuenta no encontrada"}, status_code=400)
                new_attachment_path, new_attachment_name = _save_record_attachment(
                    attachment, user_id=uid, record_type="expenses",
                )
                old_cash = _get_or_create_cash_holding(db, record.account_id, record.currency)
                old_cash.quantity = _round_money(old_cash.quantity + record.amount)
                new_currency = _get_account_currency(db, account_id, record.currency)
                new_cash = _get_or_create_cash_holding(db, account_id, new_currency)
                new_cash.quantity = _round_money(new_cash.quantity - amount)
                record.account_id = account_id
                record.name = cleaned_name
                record.amount = amount
                record.notes = cleaned_notes
                record.currency = new_currency
                record.category = (category.strip() if category else None) or _categorize_expense(cleaned_name)
                record.timestamp = ts
                if new_attachment_path:
                    _delete_attachment_file(record.attachment_path)
                    record.attachment_path = new_attachment_path
                    record.attachment_name = new_attachment_name
                elif remove_attachment == "on":
                    _delete_attachment_file(record.attachment_path)
                    record.attachment_path = None
                    record.attachment_name = None
            elif type == "income":
                record = db.get(IncomeRecord, record_id)
                if not record or record.user_id != uid:
                    return JSONResponse({"error": "Movimiento no encontrado"}, status_code=404)
                if not account_id:
                    return JSONResponse({"error": "Selecciona una cuenta"}, status_code=400)
                target_account = db.query(Account).filter(Account.id == account_id, Account.user_id == uid).first()
                if not target_account:
                    return JSONResponse({"error": "Cuenta no encontrada"}, status_code=400)
                new_attachment_path, new_attachment_name = _save_record_attachment(
                    attachment, user_id=uid, record_type="incomes",
                )
                old_cash = _get_or_create_cash_holding(db, record.account_id, record.currency)
                old_cash.quantity = _round_money(old_cash.quantity - record.amount)
                new_currency = _get_account_currency(db, account_id, record.currency)
                new_cash = _get_or_create_cash_holding(db, account_id, new_currency)
                new_cash.quantity = _round_money(new_cash.quantity + amount)
                record.account_id = account_id
                record.name = cleaned_name
                record.amount = amount
                record.notes = cleaned_notes
                record.currency = new_currency
                record.timestamp = ts
                if new_attachment_path:
                    _delete_attachment_file(record.attachment_path)
                    record.attachment_path = new_attachment_path
                    record.attachment_name = new_attachment_name
                elif remove_attachment == "on":
                    _delete_attachment_file(record.attachment_path)
                    record.attachment_path = None
                    record.attachment_name = None
            elif type == "transfer":
                record = db.get(TransferRecord, record_id)
                if not record or record.user_id != uid:
                    return JSONResponse({"error": "Movimiento no encontrado"}, status_code=404)
                if not from_account_id or not to_account_id:
                    return JSONResponse({"error": "Selecciona las cuentas de origen y destino"}, status_code=400)
                if from_account_id == to_account_id:
                    return JSONResponse({"error": "Las cuentas deben ser distintas"}, status_code=400)
                _adjust_cash(db, user_id=uid, account_id=record.from_account_id, amount=record.amount)
                _adjust_cash(db, user_id=uid, account_id=record.to_account_id, amount=-record.amount)
                _adjust_cash(db, user_id=uid, account_id=from_account_id, amount=-amount)
                _adjust_cash(db, user_id=uid, account_id=to_account_id, amount=amount)
                record.from_account_id = from_account_id
                record.to_account_id = to_account_id
                record.amount = amount
                record.notes = cleaned_notes
                record.timestamp = ts
            else:
                return JSONResponse({"error": "Tipo no válido"}, status_code=400)
            db.commit()
            detail = _build_day_detail(db, uid, d)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({"date": d.isoformat(), **detail})


@router.post("/api/calendar/day/movement/{record_id}/delete")
def calendar_delete_movement(
    request: Request,
    record_id: int,
    type: str = Form(...),
    date: str = Form(...),
):
    uid, redir = _require_auth(request)
    if redir:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"error": "Fecha inválida"}, status_code=400)

    with SessionLocal() as db:
        if type == "expense":
            record = db.get(ExpenseRecord, record_id)
            if not record or record.user_id != uid:
                return JSONResponse({"error": "Movimiento no encontrado"}, status_code=404)
            cash = _get_or_create_cash_holding(db, record.account_id, record.currency)
            cash.quantity = _round_money(cash.quantity + record.amount)
            _delete_attachment_file(record.attachment_path)
            db.delete(record)
        elif type == "income":
            record = db.get(IncomeRecord, record_id)
            if not record or record.user_id != uid:
                return JSONResponse({"error": "Movimiento no encontrado"}, status_code=404)
            cash = _get_or_create_cash_holding(db, record.account_id, record.currency)
            cash.quantity = _round_money(cash.quantity - record.amount)
            _delete_attachment_file(record.attachment_path)
            db.delete(record)
        elif type == "transfer":
            record = db.get(TransferRecord, record_id)
            if not record or record.user_id != uid:
                return JSONResponse({"error": "Movimiento no encontrado"}, status_code=404)
            _adjust_cash(db, user_id=uid, account_id=record.from_account_id, amount=record.amount)
            _adjust_cash(db, user_id=uid, account_id=record.to_account_id, amount=-record.amount)
            db.delete(record)
        else:
            return JSONResponse({"error": "Tipo no válido"}, status_code=400)
        db.commit()
        detail = _build_day_detail(db, uid, d)

    return JSONResponse({"date": d.isoformat(), **detail})
