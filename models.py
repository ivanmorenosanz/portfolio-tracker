from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import DEFAULT_CURRENCY


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(5), default="es")
    accounts: Mapped[list["Account"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    user: Mapped[Optional["User"]] = relationship(back_populates="accounts")
    holdings: Mapped[list["Holding"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    ticker: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    isin: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(10), default=DEFAULT_CURRENCY)
    manual_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_updated: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ter: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tier_limit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tier_ter: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    holdings: Mapped[list["Holding"]] = relationship(back_populates="asset", cascade="all, delete-orphan")


class Holding(Base):
    __tablename__ = "holdings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    quantity: Mapped[float] = mapped_column(Float)
    average_cost: Mapped[float] = mapped_column(Float, default=0)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    split_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    split_ter: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    new_quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    account: Mapped[Account] = relationship(back_populates="holdings")
    asset: Mapped[Asset] = relationship(back_populates="holdings")
    promos: Mapped[list["InterestPromo"]] = relationship(back_populates="holding", cascade="all, delete-orphan")


class InterestPromo(Base):
    """A configurable interest rule for a cash holding.

    Each promo covers a slice of the balance at `rate` (%/year):
    - mode 'balance': the first `cap` euros of the balance (e.g. Sabadell 2.5% up to 50k).
    - mode 'new': money added since the promo started = balance above `baseline`
      (e.g. MyInvestor 2.5% on new money, while old money keeps the base rate).
    `start_date`/`end_date` make offers expire automatically.
    """
    __tablename__ = "interest_promos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    holding_id: Mapped[int] = mapped_column(ForeignKey("holdings.id"))
    label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    rate: Mapped[float] = mapped_column(Float, default=0.0)
    mode: Mapped[str] = mapped_column(String(10), default="balance")  # 'balance' | 'new'
    cap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    baseline: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    holding: Mapped["Holding"] = relationship(back_populates="promos")


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    total_value: Mapped[float] = mapped_column(Float)
    total_cost: Mapped[float] = mapped_column(Float)


class PortfolioTypeSnapshot(Base):
    __tablename__ = "portfolio_type_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    asset_type: Mapped[str] = mapped_column(String(20))
    total_value: Mapped[float] = mapped_column(Float)
    total_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ticker: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    trade_type: Mapped[str] = mapped_column(String(10))  # "buy" or "sell"
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)


class WatchlistItem(Base):
    __tablename__ = "watchlist"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ticker: Mapped[str] = mapped_column(String(30))
    name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)


class AutoContribution(Base):
    __tablename__ = "auto_contributions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    holding_id: Mapped[int] = mapped_column(ForeignKey("holdings.id"))
    amount: Mapped[float] = mapped_column(Float)
    day_of_month: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_executed_period: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)


class ExpenseSchedule(Base):
    __tablename__ = "expense_schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    day_of_month: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_executed_period: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class ExpenseRecord(Base):
    __tablename__ = "expense_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    schedule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("expense_schedules.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default=DEFAULT_CURRENCY)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    period_label: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    attachment_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attachment_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class IncomeSchedule(Base):
    __tablename__ = "income_schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    day_of_month: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_executed_period: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class IncomeRecord(Base):
    __tablename__ = "income_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    schedule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("income_schedules.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default=DEFAULT_CURRENCY)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    period_label: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    attachment_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attachment_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class TransferSchedule(Base):
    __tablename__ = "transfer_schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    from_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    to_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[float] = mapped_column(Float)
    day_of_month: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_executed_period: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class TransferRecord(Base):
    __tablename__ = "transfer_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    from_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    to_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    schedule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("transfer_schedules.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    period_label: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)


class MortgageProfile(Base):
    """User information used to provide affordability-oriented mortgage tips."""
    __tablename__ = "mortgage_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    age: Mapped[int] = mapped_column(Integer)
    employment_status: Mapped[str] = mapped_column(String(30))
    monthly_net_income: Mapped[float] = mapped_column(Float)
    monthly_fixed_expenses: Mapped[float] = mapped_column(Float)
    monthly_debt_payments: Mapped[float] = mapped_column(Float)
    savings: Mapped[float] = mapped_column(Float)
    dependents: Mapped[int] = mapped_column(Integer, default=0)
    property_use: Mapped[str] = mapped_column(String(20), default="primary")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SavedMortgage(Base):
    """A saved mortgage scenario belonging to one user."""
    __tablename__ = "saved_mortgages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    property_price: Mapped[float] = mapped_column(Float)
    initial_contribution: Mapped[float] = mapped_column(Float)
    purchase_costs_pct: Mapped[float] = mapped_column(Float)
    term_years: Mapped[int] = mapped_column(Integer)
    interest_rate: Mapped[float] = mapped_column(Float)
    opening_fee_pct: Mapped[float] = mapped_column(Float)
    monthly_extra: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Goal(Base):
    """A savings goal with a target amount, optional deadline, and optional link to an account.

    Progress is the linked account's current market value when `account_id` is
    set, otherwise the manually tracked `manual_amount`. When `target_date` is
    set, the dashboard shows whether the goal is on track or overdue.
    """
    __tablename__ = "goals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    target_amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default=DEFAULT_CURRENCY)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    manual_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped[Optional["Account"]] = relationship()
