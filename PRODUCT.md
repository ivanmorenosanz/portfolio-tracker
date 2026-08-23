# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- Primary user: the owner (Iván), a private individual in Spain tracking his
  own wealth day-to-day — investments, cash, recurring money movements, goals.
- Other audiences: the instance allows open registration, so additional users
  may create their own accounts and track their own finances independently
  (per-user data isolation exists via `user_id` scoping and
  `static/record_attachments/user_<id>/`).

## Product Purpose

A self-hosted personal finance and portfolio web app ("Portfolio Pi") that
unifies investment positions, cash accounts, recurring incomes/expenses/
transfers, savings goals (Metas), a financial calendar, mortgage simulation,
and auto-contributions in one place. It replaces scattered spreadsheets: market
prices refresh automatically, snapshots build historical trends, and recurring
movements post themselves on schedule. Success is being a reliable long-term
companion for years of financial tracking.

## Positioning

Self-hosted and Spain-oriented by design: multi-source market data
(Finnhub → yfinance → Financial Times fund NAV) without paid data vendors,
EUR-first with multi-currency support, Spanish-market defaults in the mortgage
simulator (French amortization, Spanish purchase costs), and cash-interest promo
tracking for Spanish banks (e.g. Sabadell, MyInvestor tiers). A generic tracker
could not truthfully claim this local-fit plus full data ownership.

## Operating Context

- Served from a Raspberry Pi / Linux box behind a reverse proxy that sets the
  `X-Portfolio-Prefix` header; systemd service (`portfolio`), port 8000.
- Timezone `Europe/Madrid` for all rendering; timestamps stored naive-UTC.
- Daily scheduled refresh at 06:00; price/fund/snapshot jobs run hourly;
  movements and auto-contributions every 30 minutes.
- Market data depends on external APIs; missing Finnhub key degrades to
  yfinance fallback.

## Capabilities and Constraints

- Dashboard: positions, account/type/geographic/sector allocation,
  compound-interest calculator, value & invested trend charts with daily P&L.
- Accounts & holdings: buy/sell/edit, manual or fetched prices; cash is an
  asset (`asset_type == "cash"`, quantity = balance).
- Movimientos: expenses/incomes/transfers, auto-categorised expenses,
  recurring monthly schedules, document attachments.
- Calendario: month/week/day/year views, projected daily cash balance,
  inline CRUD of movements.
- Metas: savings goals with progress (manual or linked account live value),
  target dates with on-track/overdue/done badges, suggested monthly
  contribution from recurring cash flow.
- Mortgage simulator (Simulador de hipoteca) with named savable scenarios and
  a personal affordability survey.
- Auto-contributions into funds/ETFs; cash interest promos (tiered rates).
- Auth: open registration, login, recovery-code password reset, 30-day
  session cookie, profile settings for theme, ES/EN language, password.
- Technical constraints: bcrypt must stay < 4.0 (passlib pin); SQLite via
  SQLAlchemy with idempotent `ensure_schema()` migrations (no Alembic);
  backend edits need a service restart, Jinja template edits hot-reload.

## Brand Commitments

- Name: "Portfolio Pi".
- Spanish-first UI; English available as a secondary language (i18n).

## Evidence on Hand

- Live production SQLite database (`portfolio.db`) with real user data.
- README.md documents architecture, features, gotchas, and deployment.
- No testimonials, press, or marketing assets exist; none may be fabricated.

## Product Principles

1. Trustworthy numbers first: rounding, snapshot, and cost-basis correctness
   outrank new features (cash must never double-count in cost charts).
2. Low-maintenance longevity: sane defaults, idempotent migrations, and
   graceful degradation when external data sources fail.
3. Data ownership: everything stays self-hosted on modest hardware.
4. Everyday usability in Spanish: fast answers to "how am I doing?" without
   spreadsheet wrangling.

## Accessibility & Inclusion

No product-specific standard established yet (open decision). Bilingual
ES/EN support already ships; keep it working in all future UI work.
