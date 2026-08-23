from calendar import monthrange
from datetime import date, datetime, timedelta
from math import ceil
from typing import Any, Callable, Optional
import threading

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from config import DEFAULT_CURRENCY, MADRID_TZ, SNAPSHOT_INTERVAL_SECONDS, _categorize_expense, _to_madrid
from database import SessionLocal, engine
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
    PortfolioSnapshot,
    PortfolioTypeSnapshot,
    Trade,
    TransferRecord,
    TransferSchedule,
    User,
)
from prices import (
    _asset_source_url,
    _download_prices,
    _enrich_generic_asset_names,
    _fetch_open_price,
    _is_asset_refresh_due,
    _sym,
    get_effective_price,
)

_refresh_lock = threading.Lock()


def refresh_prices_background() -> None:
    if _refresh_lock.acquire(blocking=False):
        def _run():
            try:
                refresh_prices()
            finally:
                _refresh_lock.release()
        threading.Thread(target=_run, daemon=True).start()


def refresh_prices() -> int:
    try:
        now_utc = datetime.utcnow()
        with SessionLocal() as db:
            asset_rows = [
                (a.id, a.ticker)
                for a in db.query(Asset).all()
                if a.ticker and _is_asset_refresh_due(a, now_utc)
            ]

        if not asset_rows:
            print("[SCHEDULER] Price refresh: all tickers are within their refresh window, skipping.", flush=True)
            try:
                record_portfolio_snapshots()
            except Exception:
                pass
            return 0

        ticker_to_ids: dict[str, list[int]] = {}
        for aid, ticker in asset_rows:
            ticker_to_ids.setdefault(ticker, []).append(aid)

        prices = _download_prices(list(ticker_to_ids.keys()))

        updated = 0
        if prices:
            now = datetime.utcnow()
            with SessionLocal() as db:
                for ticker, price in prices.items():
                    for aid in ticker_to_ids.get(ticker, []):
                        asset = db.get(Asset, aid)
                        if asset:
                            asset.last_price = price
                            asset.last_updated = now
                            updated += 1
                db.commit()

        try:
            record_portfolio_snapshots()
        except Exception:
            pass

        print(f"[SCHEDULER] Price refresh: {updated} assets updated from {len(prices)} tickers", flush=True)
        return updated
    except Exception as e:
        print(f"[SCHEDULER] Error in refresh_prices: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return 0


def ensure_portfolio_type_snapshots_table() -> None:
    """Create per-type snapshot table if it is missing (safe to call repeatedly)."""
    with engine.connect() as conn:
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS portfolio_type_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                timestamp   DATETIME NOT NULL,
                asset_type  VARCHAR(20) NOT NULL,
                total_value FLOAT NOT NULL,
                total_cost  FLOAT
            )
        """)
        cols = [row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info(portfolio_type_snapshots)").fetchall()]
        if "total_cost" not in cols:
            conn.exec_driver_sql("ALTER TABLE portfolio_type_snapshots ADD COLUMN total_cost FLOAT")


def record_portfolio_snapshots() -> None:
    """Snapshot current portfolio value for every user.

    Writes at most once per SNAPSHOT_INTERVAL_SECONDS while nothing changes, but
    records a new point immediately whenever a value moves (e.g. spending cash
    overnight), so the trend chart stays accurate outside market hours too.
    """
    ensure_portfolio_type_snapshots_table()
    now = datetime.utcnow()
    recent_cutoff = now - timedelta(seconds=SNAPSHOT_INTERVAL_SECONDS)
    eps = 0.005  # ignore sub-cent float noise
    with SessionLocal() as db:
        users = db.query(User).all()
        for user in users:
            holdings = (
                db.query(Holding)
                .join(Account)
                .join(Asset)
                .filter(Account.user_id == user.id)
                .all()
            )
            total_value = sum(h.quantity * get_effective_price(h.asset) for h in holdings)
            total_cost = sum(h.quantity * h.average_cost for h in holdings)
            if total_value <= 0:
                continue

            last_snap = (
                db.query(PortfolioSnapshot)
                .filter(PortfolioSnapshot.user_id == user.id)
                .order_by(PortfolioSnapshot.timestamp.desc())
                .first()
            )
            time_due = not (
                last_snap
                and (now - last_snap.timestamp).total_seconds() < SNAPSHOT_INTERVAL_SECONDS
            )
            total_changed = (
                last_snap is None
                or abs((last_snap.total_value or 0.0) - total_value) >= eps
                or abs((last_snap.total_cost or 0.0) - total_cost) >= eps
            )

            has_recent_type_snapshot = (
                db.query(PortfolioTypeSnapshot.id)
                .filter(
                    PortfolioTypeSnapshot.user_id == user.id,
                    PortfolioTypeSnapshot.timestamp >= recent_cutoff,
                )
                .first()
                is not None
            )

            totals_by_type: dict[str, float] = {}
            costs_by_type: dict[str, float] = {}
            for h in holdings:
                mv = h.quantity * get_effective_price(h.asset)
                totals_by_type[h.asset.asset_type] = totals_by_type.get(h.asset.asset_type, 0.0) + mv
                cost = h.quantity * h.average_cost
                costs_by_type[h.asset.asset_type] = costs_by_type.get(h.asset.asset_type, 0.0) + cost

            if time_due or total_changed:
                db.add(PortfolioSnapshot(
                    user_id=user.id,
                    timestamp=now,
                    total_value=total_value,
                    total_cost=total_cost,
                ))

            for asset_type, type_total in totals_by_type.items():
                if type_total <= 0:
                    continue
                last_type = (
                    db.query(PortfolioTypeSnapshot)
                    .filter(
                        PortfolioTypeSnapshot.user_id == user.id,
                        PortfolioTypeSnapshot.asset_type == asset_type,
                    )
                    .order_by(PortfolioTypeSnapshot.timestamp.desc())
                    .first()
                )
                type_changed = (
                    last_type is None
                    or abs((last_type.total_value or 0.0) - type_total) >= eps
                )
                if not has_recent_type_snapshot or type_changed:
                    db.add(PortfolioTypeSnapshot(
                        user_id=user.id,
                        timestamp=now,
                        asset_type=asset_type,
                        total_value=type_total,
                        total_cost=costs_by_type.get(asset_type, 0.0),
                    ))
        db.commit()


# Public holidays on which major European/US markets are closed (MM-DD).
_MARKET_HOLIDAYS_MMDD: set[str] = {
    "01-01", "01-06",            # New Year, Epiphany (Spain)
    "04-18", "04-21",            # Good Friday / Easter Monday (approximate; update yearly)
    "05-01",                     # Labour Day
    "12-24", "12-25", "12-26",   # Christmas Eve, Christmas, Boxing Day
    "12-31",                     # New Year's Eve
}


def _is_market_day(d: datetime) -> bool:
    """Return True if d is a weekday and not a known market holiday."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d.strftime("%m-%d") not in _MARKET_HOLIDAYS_MMDD


def _next_market_day_on_or_after(d: datetime) -> datetime:
    """Return d if it is a market day, otherwise advance to the next market day."""
    while not _is_market_day(d):
        d = d + timedelta(days=1)
    return d


def _previous_market_day_on_or_before(d: datetime) -> datetime:
    """Return d if it is a market day, otherwise move back to the previous market day."""
    while not _is_market_day(d):
        d = d - timedelta(days=1)
    return d


def _is_nomina_concept(name: Optional[str]) -> bool:
    """True when a recurring movement concept is payroll (Nómina / Nomina)."""
    normalized = (name or "").strip().casefold().replace("ó", "o")
    return normalized == "nomina"


def _configured_schedule_dt(day_of_month: int, year: int, month: int) -> datetime:
    last_day = monthrange(year, month)[1]
    due_day = min(day_of_month, last_day)
    return datetime(year, month, due_day, 0, 0, 0, tzinfo=MADRID_TZ)


def schedule_execution_date(
    day_of_month: int,
    year: int,
    month: int,
    concept_name: Optional[str] = None,
) -> date:
    """Calendar day when a recurring movement should run for the given month."""
    configured_dt = _configured_schedule_dt(day_of_month, year, month)
    if _is_nomina_concept(concept_name):
        return _previous_market_day_on_or_before(configured_dt).date()
    return _next_market_day_on_or_after(configured_dt).date()


def schedule_fires_on_date(
    day_of_month: int,
    d: date,
    concept_name: Optional[str] = None,
) -> bool:
    """True when a recurring movement is scheduled on calendar day `d`."""
    for delta in (-1, 0, 1):
        month, year = d.month + delta, d.year
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        if schedule_execution_date(day_of_month, year, month, concept_name) == d:
            return True
    return False


def _pending_schedule_period(
    day_of_month: int,
    dt_madrid: datetime,
    concept_name: Optional[str],
    last_executed_period: Optional[str],
) -> Optional[str]:
    """Return the YYYY-MM period ready to execute, or None."""
    if day_of_month < 1:
        return None

    today = dt_madrid.date()
    year, month = dt_madrid.year, dt_madrid.month
    pending: list[str] = []
    for delta in range(-1, 3):
        m, y = month + delta, year
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        period = f"{y}-{m:02d}"
        if period == last_executed_period:
            continue
        # Never re-run periods older than the marker (prevents backward loops
        # when the marker is ahead of an old pending period).
        if last_executed_period and period < last_executed_period:
            continue
        if today >= schedule_execution_date(day_of_month, y, m, concept_name):
            pending.append(period)

    return min(pending) if pending else None


def scheduled_dates_in_month(
    day_of_month: int,
    year: int,
    month: int,
    concept_name: Optional[str] = None,
) -> list[date]:
    """Effective execution dates that fall inside the given calendar month."""
    dates: list[date] = []
    for delta in (-1, 0, 1):
        cfg_month, cfg_year = month + delta, year
        while cfg_month < 1:
            cfg_month += 12
            cfg_year -= 1
        while cfg_month > 12:
            cfg_month -= 12
            cfg_year += 1
        fire_date = schedule_execution_date(day_of_month, cfg_year, cfg_month, concept_name)
        if fire_date.year == year and fire_date.month == month:
            dates.append(fire_date)
    return dates


def schedule_concept_name(schedule: Any) -> Optional[str]:
    return getattr(schedule, "name", None) or getattr(schedule, "notes", None)


def _is_due_contribution_day(day_of_month: int, dt_madrid: datetime, has_ticker: bool = False) -> bool:
    if day_of_month < 1:
        return False
    last_day = monthrange(dt_madrid.year, dt_madrid.month)[1]
    due_day = min(day_of_month, last_day)
    configured_dt = dt_madrid.replace(day=due_day, hour=0, minute=0, second=0, microsecond=0)
    if has_ticker:
        # For exchange-traded assets, use the next market open on or after the configured day.
        effective_dt = _next_market_day_on_or_after(configured_dt)
    else:
        effective_dt = configured_dt
    return dt_madrid.date() >= effective_dt.date()


def run_auto_contributions() -> int:
    """Execute monthly auto-contributions once per calendar month."""
    now_madrid = datetime.now(MADRID_TZ)
    period = now_madrid.strftime("%Y-%m")
    executed = 0

    with SessionLocal() as db:
        schedules = db.query(AutoContribution).filter(AutoContribution.enabled == 1).all()
        for schedule in schedules:
            if schedule.last_executed_period == period:
                continue

            holding = (
                db.query(Holding)
                .join(Account)
                .join(Asset)
                .filter(Holding.id == schedule.holding_id, Account.user_id == schedule.user_id)
                .first()
            )
            if not holding or holding.asset.asset_type == "cash":
                continue

            has_ticker = bool(holding.asset.ticker)
            if not _is_due_contribution_day(schedule.day_of_month, now_madrid, has_ticker=has_ticker):
                continue

            cash_holding = (
                db.query(Holding)
                .join(Asset)
                .filter(Holding.account_id == holding.account_id, Asset.asset_type == "cash")
                .first()
            )
            if not cash_holding or cash_holding.quantity < schedule.amount:
                continue

            # For ticker assets try to buy at today's open price; fall back to last known price.
            if holding.asset.ticker:
                open_price = _fetch_open_price(holding.asset.ticker)
                price = open_price if (open_price and open_price > 0) else get_effective_price(holding.asset)
            else:
                price = get_effective_price(holding.asset)
            if price <= 0:
                continue

            buy_qty = schedule.amount / price
            prev_qty = holding.quantity
            new_qty = prev_qty + buy_qty
            total_cost_before = prev_qty * holding.average_cost
            total_cost_add = buy_qty * price
            holding.quantity = new_qty
            holding.average_cost = (total_cost_before + total_cost_add) / new_qty if new_qty > 0 else price
            cash_holding.quantity = _round_money(cash_holding.quantity - schedule.amount)
            schedule.last_executed_period = period

            if holding.asset.ticker:
                db.add(Trade(
                    user_id=schedule.user_id,
                    timestamp=datetime.utcnow(),
                    ticker=holding.asset.ticker,
                    trade_type="buy",
                    quantity=buy_qty,
                    price=price,
                ))

            executed += 1

        if executed:
            db.commit()

    return executed


def _get_or_create_cash_holding(db: Session, account_id: int, currency: Optional[str] = None) -> Holding:
    cash_holding = (
        db.query(Holding)
        .join(Asset)
        .filter(Holding.account_id == account_id, Asset.asset_type == "cash")
        .first()
    )
    if cash_holding:
        return cash_holding

    chosen_currency = currency or DEFAULT_CURRENCY
    cash_asset = (
        db.query(Asset)
        .filter(Asset.asset_type == "cash", Asset.name == "Efectivo", Asset.currency == chosen_currency)
        .first()
    )
    if cash_asset is None:
        cash_asset = Asset(
            name="Efectivo",
            asset_type="cash",
            currency=chosen_currency,
            manual_price=1.0,
        )
        db.add(cash_asset)
        db.flush()

    cash_holding = Holding(
        account_id=account_id,
        asset_id=cash_asset.id,
        quantity=0.0,
        average_cost=1.0,
    )
    db.add(cash_holding)
    db.flush()
    return cash_holding


def _get_account_currency(db: Session, account_id: int, fallback: Optional[str] = None) -> str:
    holdings = (
        db.query(Holding)
        .join(Asset)
        .filter(Holding.account_id == account_id)
        .all()
    )
    currency = next((h.asset.currency for h in holdings if h.asset.asset_type == "cash"), None)
    if currency:
        return currency
    currency = next((h.asset.currency for h in holdings if h.asset.currency), None)
    return currency or fallback or DEFAULT_CURRENCY


def _round_money(value: float) -> float:
    """Round a monetary value to 2 decimals to avoid binary floating-point artifacts."""
    return round(value, 2)


def _adjust_cash(
    db: Session,
    *,
    user_id: int,
    account_id: int,
    amount: float,
    currency: Optional[str] = None,
) -> str:
    """Add ±amount to an account's cash holding and return the account currency."""
    account = db.query(Account).filter(Account.id == account_id, Account.user_id == user_id).first()
    if not account:
        raise ValueError("Cuenta no encontrada")
    account_currency = _get_account_currency(db, account_id, currency)
    cash_holding = _get_or_create_cash_holding(db, account_id, account_currency)
    cash_holding.quantity = _round_money(cash_holding.quantity + amount)
    return account_currency


def _is_cobee_charge(name: Optional[str]) -> bool:
    """True when an expense was paid with the Cobee credit card."""
    return "cobee" in (name or "").strip().casefold()


COBEE_SALARY_FACTOR = 0.81  # charge minus 19% tax benefit when settled from salary


def _settle_cobee_from_salary(
    db: Session,
    *,
    user_id: int,
    salary_account_id: int,
) -> tuple[float, int]:
    """Deduct pending Cobee charges from a freshly recorded salary.

    Each pending charge settles at 81% of its amount (100 € charge -> 81 €
    deducted from the income, thanks to the card's 19% tax benefit).
    Returns (total deducted, number of charges settled).
    """
    pending = (
        db.query(ExpenseRecord)
        .filter(
            ExpenseRecord.user_id == user_id,
            ExpenseRecord.settled == 0,
        )
        .all()
    )
    if not pending:
        return 0.0, 0
    total = _round_money(sum(r.amount for r in pending) * COBEE_SALARY_FACTOR)
    for record in pending:
        record.settled = 1
    # Reduce the salary's cash impact by the settled total. The salary income
    # itself was already credited in full; this pulls the cobee repayment out.
    if total > 0:
        _adjust_cash(db, user_id=user_id, account_id=salary_account_id, amount=-total)
    return total, len(pending)


def _execute_cash_flow(
    db: Session,
    *,
    record_cls: type,
    sign: int,
    user_id: int,
    account_id: int,
    name: str,
    amount: float,
    notes: Optional[str] = None,
    schedule_id: Optional[int] = None,
    period_label: Optional[str] = None,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
    category: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> None:
    """Record a cash movement: expense (sign=-1) or income (sign=+1).

    Cobee credit-card charges (name contains 'cobee') never touch a cash
    account: they are recorded unsettled and later deducted from the next
    salary income at 81% of the charge.
    """
    is_cobee = record_cls is ExpenseRecord and sign < 0 and _is_cobee_charge(name)
    if is_cobee:
        account_currency = _get_account_currency(db, account_id)
    else:
        account_currency = _adjust_cash(
            db,
            user_id=user_id,
            account_id=account_id,
            amount=sign * amount,
        )
    record = record_cls(
        user_id=user_id,
        account_id=account_id,
        schedule_id=schedule_id,
        name=name,
        amount=amount,
        currency=account_currency,
        timestamp=timestamp or datetime.utcnow(),
        notes=notes,
        period_label=period_label,
        attachment_path=attachment_path,
        attachment_name=attachment_name,
    )
    if record_cls is ExpenseRecord:
        record.category = category or _categorize_expense(name)
        if is_cobee:
            record.settled = 0
    db.add(record)
    db.flush()  # autoflush is off; make the new row visible to the queries below

    # Salary income: settle any pending Cobee charges against it.
    if record_cls is IncomeRecord and sign > 0 and _is_nomina_concept(name):
        deducted, count = _settle_cobee_from_salary(db, user_id=user_id, salary_account_id=account_id)
        if count:
            extra = f" [Cobee: -{deducted:.2f} ({count} cargos)]"
            record.notes = ((record.notes or "") + extra).strip()


def _execute_transfer(
    db: Session,
    *,
    user_id: int,
    from_account_id: int,
    to_account_id: int,
    amount: float,
    notes: Optional[str] = None,
    schedule_id: Optional[int] = None,
    period_label: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> None:
    """Move cash between two accounts and log the transfer."""
    if from_account_id == to_account_id:
        raise ValueError("La cuenta de origen y la de destino deben ser distintas")
    if amount <= 0:
        raise ValueError("El importe debe ser mayor que cero")

    from_account = db.query(Account).filter(Account.id == from_account_id, Account.user_id == user_id).first()
    to_account = db.query(Account).filter(Account.id == to_account_id, Account.user_id == user_id).first()
    if not from_account or not to_account:
        raise ValueError("Cuenta no encontrada")

    _adjust_cash(db, user_id=user_id, account_id=from_account_id, amount=-amount)
    _adjust_cash(db, user_id=user_id, account_id=to_account_id, amount=amount)

    db.add(TransferRecord(
        user_id=user_id,
        from_account_id=from_account_id,
        to_account_id=to_account_id,
        schedule_id=schedule_id,
        amount=amount,
        timestamp=timestamp or datetime.utcnow(),
        notes=notes,
        period_label=period_label,
    ))


def _run_scheduled_money_movements(
    kind: str,
    schedule_cls: type,
    execute: Callable[[Session, Any, str], None],
) -> int:
    """Run monthly recurring money-movement schedules once per calendar month.

    Concurrency-safe: opens the pass with SQLite BEGIN IMMEDIATE, taking the
    exclusive write lock up-front. A second process starting simultaneously
    gets 'database is locked' and skips this pass instead of double-running.
    Each period's records + marker bump commit atomically.
    """
    now_madrid = datetime.now(MADRID_TZ)
    executed = 0
    db = SessionLocal()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        schedules = db.query(schedule_cls).filter(schedule_cls.enabled == 1).all()
        for schedule in schedules:
            concept_name = schedule_concept_name(schedule)
            # Process every still-pending period (oldest first).
            while True:
                period = _pending_schedule_period(
                    schedule.day_of_month,
                    now_madrid,
                    concept_name,
                    schedule.last_executed_period,
                )
                if not period:
                    break
                try:
                    execute(db, schedule, period)
                    schedule.last_executed_period = period
                    executed += 1
                except Exception as e:
                    print(f"[{kind.upper()}] scheduled {kind} {schedule.id} failed: {e}", flush=True)
                    break
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        msg = str(e)
        if "locked" in msg.lower():
            print(f"[{kind.upper()}] skipped: another instance holds the write lock", flush=True)
        else:
            print(f"[{kind.upper()}] run failed: {e}", flush=True)
    finally:
        db.close()

    return executed


def _execute_expense_schedule(db: Session, schedule: ExpenseSchedule, period: str) -> None:
    _execute_cash_flow(
        db,
        record_cls=ExpenseRecord,
        sign=-1,
        user_id=schedule.user_id,
        account_id=schedule.account_id,
        name=schedule.name,
        amount=schedule.amount,
        notes=schedule.notes,
        schedule_id=schedule.id,
        period_label=period,
        category=schedule.category,
    )


def _execute_income_schedule(db: Session, schedule: IncomeSchedule, period: str) -> None:
    _execute_cash_flow(
        db,
        record_cls=IncomeRecord,
        sign=1,
        user_id=schedule.user_id,
        account_id=schedule.account_id,
        name=schedule.name,
        amount=schedule.amount,
        notes=schedule.notes,
        schedule_id=schedule.id,
        period_label=period,
    )


def _execute_transfer_schedule(db: Session, schedule: TransferSchedule, period: str) -> None:
    _execute_transfer(
        db,
        user_id=schedule.user_id,
        from_account_id=schedule.from_account_id,
        to_account_id=schedule.to_account_id,
        amount=schedule.amount,
        notes=schedule.notes,
        schedule_id=schedule.id,
        period_label=period,
    )


def run_scheduled_expenses() -> int:
    """Execute monthly recurring expenses once per calendar month."""
    return _run_scheduled_money_movements("expense", ExpenseSchedule, _execute_expense_schedule)


def run_scheduled_incomes() -> int:
    """Execute monthly recurring incomes once per calendar month."""
    return _run_scheduled_money_movements("income", IncomeSchedule, _execute_income_schedule)


def run_scheduled_transfers() -> int:
    """Execute monthly recurring transfers once per calendar month."""
    return _run_scheduled_money_movements("transfer", TransferSchedule, _execute_transfer_schedule)


def _promo_active(promo: "InterestPromo") -> bool:
    now = datetime.utcnow()
    if promo.start_date and now < promo.start_date:
        return False
    if promo.end_date and now >= promo.end_date:
        return False
    return True


def snapshot_data(db: Session, user_id: int):
    _enrich_generic_asset_names(db, user_id)

    holdings = (
        db.query(Holding)
        .join(Account)
        .join(Asset)
        .filter(Account.user_id == user_id)
        .all()
    )
    rows = []
    totals_by_account: dict = {}
    totals_by_type: dict = {}
    total_value = 0.0
    total_cost = 0.0
    invested_cost = 0.0

    auto_map = {
        ac.holding_id: ac
        for ac in db.query(AutoContribution).filter(AutoContribution.user_id == user_id).all()
    }

    recurring_contributions = []
    recurring_rows = (
        db.query(AutoContribution, Holding, Account, Asset)
        .join(Holding, AutoContribution.holding_id == Holding.id)
        .join(Account, Holding.account_id == Account.id)
        .join(Asset, Holding.asset_id == Asset.id)
        .filter(
            AutoContribution.user_id == user_id,
            AutoContribution.enabled == 1,
            Account.user_id == user_id,
        )
        .order_by(AutoContribution.day_of_month, Account.name, Asset.name)
        .all()
    )
    for ac, _holding, account, asset in recurring_rows:
        recurring_contributions.append({
            "account": account.name,
            "asset": asset.name,
            "ticker": asset.ticker or "-",
            "amount": ac.amount,
            "currency": asset.currency,
            "currency_sym": _sym(asset.currency),
            "day": ac.day_of_month,
            "last_executed_period": ac.last_executed_period,
        })

    recurring_expenses = []
    recurring_expense_rows = (
        db.query(ExpenseSchedule, Account)
        .join(Account, ExpenseSchedule.account_id == Account.id)
        .filter(
            ExpenseSchedule.user_id == user_id,
            ExpenseSchedule.enabled == 1,
            Account.user_id == user_id,
        )
        .order_by(ExpenseSchedule.day_of_month, Account.name, ExpenseSchedule.name)
        .all()
    )
    for schedule, account in recurring_expense_rows:
        currency = _get_account_currency(db, account.id)
        recurring_expenses.append({
            "id": schedule.id,
            "account_id": account.id,
            "account": account.name,
            "name": schedule.name,
            "amount": schedule.amount,
            "currency": currency,
            "currency_sym": _sym(currency),
            "day": schedule.day_of_month,
            "last_executed_period": schedule.last_executed_period,
            "notes": schedule.notes or "",
        })

    recurring_incomes = []
    recurring_income_rows = (
        db.query(IncomeSchedule, Account)
        .join(Account, IncomeSchedule.account_id == Account.id)
        .filter(
            IncomeSchedule.user_id == user_id,
            IncomeSchedule.enabled == 1,
            Account.user_id == user_id,
        )
        .order_by(IncomeSchedule.day_of_month, Account.name, IncomeSchedule.name)
        .all()
    )
    for schedule, account in recurring_income_rows:
        currency = _get_account_currency(db, account.id)
        recurring_incomes.append({
            "id": schedule.id,
            "account_id": account.id,
            "account": account.name,
            "name": schedule.name,
            "amount": schedule.amount,
            "currency": currency,
            "currency_sym": _sym(currency),
            "day": schedule.day_of_month,
            "last_executed_period": schedule.last_executed_period,
            "notes": schedule.notes or "",
        })

    recurring_transfers = []
    accounts_by_id = {a.id: a for a in db.query(Account).filter(Account.user_id == user_id).all()}
    transfer_rows = (
        db.query(TransferSchedule)
        .filter(TransferSchedule.user_id == user_id, TransferSchedule.enabled == 1)
        .order_by(TransferSchedule.day_of_month)
        .all()
    )
    for schedule in transfer_rows:
        from_account = accounts_by_id.get(schedule.from_account_id)
        to_account = accounts_by_id.get(schedule.to_account_id)
        recurring_transfers.append({
            "id": schedule.id,
            "from_account": from_account.name if from_account else "?",
            "to_account": to_account.name if to_account else "?",
            "amount": schedule.amount,
            "currency_sym": _sym(DEFAULT_CURRENCY),
            "day": schedule.day_of_month,
            "last_executed_period": schedule.last_executed_period,
            "notes": schedule.notes or "",
        })

    for h in holdings:
        current_price = get_effective_price(h.asset)
        market_value = h.quantity * current_price
        cost_value = h.quantity * h.average_cost
        pnl = market_value - cost_value
        pnl_pct = (pnl / cost_value * 100.0) if cost_value > 0 else 0.0

        total_value += market_value
        total_cost += cost_value
        if h.asset.asset_type != "cash":
            invested_cost += cost_value
        totals_by_account[h.account.name] = totals_by_account.get(h.account.name, 0.0) + market_value
        totals_by_type[h.asset.asset_type] = totals_by_type.get(h.asset.asset_type, 0.0) + market_value

        promos_payload = [
            {
                "id": p.id,
                "label": p.label or "",
                "rate": p.rate,
                "mode": p.mode,
                "cap": p.cap,
                "baseline": p.baseline,
                "start_date": p.start_date.isoformat() if p.start_date else "",
                "end_date": p.end_date.isoformat() if p.end_date else "",
                "active": _promo_active(p),
            }
            for p in sorted(h.promos, key=lambda x: x.id)
        ]

        ticker_url, ticker_source = _asset_source_url(h.asset.ticker)
        rows.append({
            "id": h.id,
            "account": h.account.name,
            "asset": h.asset.name,
            "ticker": h.asset.ticker or "-",
            "ticker_url": ticker_url,
            "ticker_source": ticker_source,
            "isin": h.asset.isin or "-",
            "asset_type": h.asset.asset_type,
            "currency": h.asset.currency,
            "currency_sym": _sym(h.asset.currency),
            "quantity": h.quantity,
            "average_cost": h.average_cost,
            "current_price": current_price,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "last_updated": h.asset.last_updated,
            "notes": h.notes or "",
            "ter": h.asset.ter,
            "tier_limit": h.asset.tier_limit,
            "tier_ter": h.asset.tier_ter,
            "split_date": h.split_date,
            "split_ter": h.split_ter,
            "new_quantity": h.new_quantity,
            "promos": promos_payload,
            "auto_enabled": bool((ac := auto_map.get(h.id)) and ac.enabled),
            "auto_amount": ac.amount if ac else None,
            "auto_day": ac.day_of_month if ac else None,
        })

    # Monthly savings capacity per currency, from enabled recurring schedules.
    # Transfers are internal moves (net zero) and are excluded; auto-contributions
    # are committed cash outflows, so they reduce what's left for new goals.
    monthly_savings_by_currency: dict[str, float] = {}
    for row in recurring_incomes:
        cur = row["currency"]
        monthly_savings_by_currency[cur] = monthly_savings_by_currency.get(cur, 0.0) + row["amount"]
    for row in recurring_expenses:
        cur = row["currency"]
        monthly_savings_by_currency[cur] = monthly_savings_by_currency.get(cur, 0.0) - row["amount"]
    for row in recurring_contributions:
        cur = row["currency"]
        monthly_savings_by_currency[cur] = monthly_savings_by_currency.get(cur, 0.0) - row["amount"]

    goals = []
    today = _to_madrid(datetime.now()).date()
    for g in db.query(Goal).filter(Goal.user_id == user_id).order_by(Goal.id).all():
        current = _goal_current_value(db, g)
        target = g.target_amount or 0.0
        pct = min(100.0, round(current / target * 100.0, 1)) if target > 0 else 0.0
        reached = target > 0 and current >= target
        status = None
        days_left = None
        if g.target_date:
            days_left = (g.target_date - today).days
            if reached:
                status = "done"
            elif days_left < 0:
                status = "overdue"
            else:
                status = "on_track"

        suggestion = None
        if not reached and target > 0:
            remaining = max(0.0, target - current)
            available = monthly_savings_by_currency.get(g.currency, 0.0)
            if g.target_date:
                months_left = (
                    (g.target_date.year - today.year) * 12
                    + (g.target_date.month - today.month)
                )
                if g.target_date.day < today.day:
                    months_left -= 1
                months_left = max(1, months_left)
                required = remaining / months_left
                suggestion = {
                    "kind": "date",
                    "required": round(required, 2),
                    "available": round(available, 2),
                    "covered": available >= required,
                    "months_left": months_left,
                }
            elif available > 0 and remaining > 0:
                suggestion = {
                    "kind": "undated",
                    "available": round(available, 2),
                    "months": max(1, ceil(remaining / available)),
                }

        goals.append({
            "id": g.id,
            "name": g.name,
            "target_amount": round(target, 2),
            "currency": g.currency,
            "currency_sym": _sym(g.currency),
            "current": current,
            "account_id": g.account_id,
            "account_name": g.account.name if g.account else None,
            "manual_amount": g.manual_amount,
            "target_date": g.target_date.isoformat() if g.target_date else None,
            "target_date_display": g.target_date.strftime("%d/%m/%Y") if g.target_date else None,
            "status": status,
            "days_left": days_left,
            "pct": pct,
            "reached": reached,
            "suggestion": suggestion,
        })

    profit_loss = total_value - total_cost
    profit_loss_pct = (profit_loss / total_cost * 100.0) if total_cost > 0 else 0.0
    invested_pct = (invested_cost / total_value * 100.0) if total_value > 0 else 0.0
    cash_value = totals_by_type.get("cash", 0.0)
    cash_pct = (cash_value / total_value * 100.0) if total_value > 0 else 0.0

    return {
        "goals": goals,
        "holdings": rows,
        "total_value": total_value,
        "invested_value": invested_cost,
        "invested_pct": invested_pct,
        "cash_value": cash_value,
        "cash_pct": cash_pct,
        "profit_loss": profit_loss,
        "profit_loss_pct": profit_loss_pct,
        "totals_by_account": totals_by_account,
        "totals_by_type": totals_by_type,
        "accounts": db.query(Account).filter(Account.user_id == user_id).order_by(Account.name).all(),
        "last_refresh": _to_madrid(lr) if (lr := db.query(func.max(Asset.last_updated)).scalar()) else None,
        "default_currency": DEFAULT_CURRENCY,
        "default_currency_sym": _sym(DEFAULT_CURRENCY),
        "recurring_contributions": recurring_contributions,
        "recurring_expenses": recurring_expenses,
        "recurring_incomes": recurring_incomes,
        "recurring_transfers": recurring_transfers,
        "monthly_savings": monthly_savings_by_currency,
        "allocation_data": _build_allocation_data(holdings),
        "cash_interest": {
            r["id"]: {
                "account": r["account"],
                "asset": r["asset"],
                "base_rate": r["ter"],
                "promos": r["promos"],
                "currency_sym": r["currency_sym"],
            }
            for r in rows
            if r["asset_type"] == "cash"
        },
    }


def _allocation_sector(asset: Asset, display_name: str = "") -> str:
    """Best-effort sector label from the instrument name/ticker.

    The asset model intentionally has no provider-specific sector field, so broad
    funds are kept as diversified instead of being assigned a misleading sector.
    """
    text = f"{display_name} {asset.name or ''} {asset.ticker or ''}".lower()
    keyword_groups = (
        ("Gestión activa", ("cobas selection", "cobas")),
        ("Criptoactivos", ("bitcoin", "crypto", "ethereum", "btc", "eth")),
        ("Materias primas", ("gold", "oro", "silver", "platinum", "commodity", "commodities")),
        ("Tecnología", ("technology", "tecnolog", "tech", "software", "semiconductor", "robot", "nasdaq")),
        ("Finanzas", ("financial", "finance", "finanzas", "bank", "banco", "insurance", "seguros")),
        ("Salud", ("health", "salud", "pharma", "biotech", "medical")),
        ("Energía", ("energy", "energía", "oil", "gas", "clean energy")),
        ("Consumo", ("consumer", "consumo", "retail", "staples")),
        ("Industria", ("industrial", "industria", "aerospace", "defense", "construction")),
        ("Renta fija", ("bond", "bonds", "renta fija", "fixed income", "deuda", "treasury", "government")),
    )
    for label, keywords in keyword_groups:
        if any(keyword in text for keyword in keywords):
            return label
    if any(keyword in text for keyword in (
        "world", "global", "global stock", "all-world", "all world", "msci world",
        "emerging", "emergentes", "s&p 500", "sp500", "index", "índice", "multi asset",
    )):
        return "Diversificado"
    return "Sin clasificar"


def _allocation_geography(asset: Asset, display_name: str = "") -> str:
    """Infer a display geography from common fund names, then currency."""
    text = f"{display_name} {asset.name or ''} {asset.ticker or ''}".lower()
    keyword_groups = (
        ("Global", ("world", "global", "all-world", "all world")),
        ("Emergentes", ("emerging", "emergentes")),
        ("EE.UU.", ("usa", "u.s.", "united states", "america", "s&p", "sp500", "nasdaq")),
        ("Europa", ("europe", "europa", "eurozone", "euro stoxx")),
        ("Japón", ("japan", "japón", "nikkei")),
        ("Asia", ("asia", "pacific", "china", "india")),
        ("Reino Unido", ("uk", "united kingdom", "britain")),
    )
    for label, keywords in keyword_groups:
        if any(keyword in text for keyword in keywords):
            return label
    currency_labels = {
        "EUR": "Europa", "USD": "EE.UU.", "GBP": "Reino Unido",
        "JPY": "Japón", "CHF": "Suiza", "CAD": "Canadá", "AUD": "Australia",
    }
    return currency_labels.get((asset.currency or "").upper(), "Otros")


def _goal_current_value(db: Session, goal: Goal) -> float:
    """Current progress of a goal: linked account market value, or manual amount."""
    if goal.account_id:
        account = db.get(Account, goal.account_id)
        if account:
            return round(
                sum(h.quantity * get_effective_price(h.asset) for h in account.holdings),
                2,
            )
    return round(goal.manual_amount or 0.0, 2)


def _build_allocation_data(holdings: list[Holding]) -> dict:
    fund_totals: dict[str, float] = {}
    geography_totals: dict[str, float] = {}
    sector_totals: dict[str, float] = {}
    total = 0.0
    for holding in holdings:
        if holding.asset.asset_type == "cash" or holding.quantity <= 0:
            continue
        value = holding.quantity * get_effective_price(holding.asset)
        if value <= 0:
            continue
        name = (holding.notes or "").strip() or holding.asset.name or holding.asset.ticker or "Sin nombre"
        fund_totals[name] = fund_totals.get(name, 0.0) + value
        geography = _allocation_geography(holding.asset, name)
        sector = _allocation_sector(holding.asset, name)
        geography_totals[geography] = geography_totals.get(geography, 0.0) + value
        sector_totals[sector] = sector_totals.get(sector, 0.0) + value
        total += value

    def rows(totals: dict[str, float]) -> list[dict]:
        return [
            {"label": label, "value": round(value, 2), "pct": round(value / total * 100, 2) if total else 0}
            for label, value in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        ]

    return {
        "funds": rows(fund_totals),
        "geography": rows(geography_totals),
        "sectors": rows(sector_totals),
        "total": round(total, 2),
    }


def _period_label(period: str) -> str:
    try:
        dt = datetime.strptime(period, "%Y-%m")
    except ValueError:
        return period
    month_names = [
        "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
    ]
    return f"{month_names[dt.month - 1]} {dt.year}"
