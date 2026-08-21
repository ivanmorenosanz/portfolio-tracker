# Portfolio Pi

Personal portfolio & wealth tracker: investments, cash, recurring money
movements (expenses/incomes/transfers), and a calendar view — all in one
self-hosted web app. Spanish UI, served from a Raspberry Pi / Linux box.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.13 |
| Web framework | FastAPI 0.115.0 |
| Server | uvicorn[standard] 0.30.6 |
| ORM / DB | SQLAlchemy 2.0.35 + SQLite (`portfolio.db`) |
| Templates | Jinja2 3.1.4 |
| Scheduler | APScheduler 3.10.4 |
| Market data | Finnhub (primary) → yfinance → Financial Times (fund NAV) |
| Auth | passlib[bcrypt] 1.7.4 + bcrypt 3.2.2 (must stay **< 4.0**) |

Full pin list in [`requirements.txt`](requirements.txt).

## Layout

```
portfolio/
├── main.py          # Entry point: FastAPI app, lifespan, router includes
├── config.py        # Constants, .env loading, maps, keys, expense categories
├── models.py        # SQLAlchemy models
├── database.py      # Engine, session, ensure_schema() migrations
├── templating.py    # Shared Jinja2 templates instance
├── auth.py          # Auth helpers + login/register/forgot/logout routes
├── prices.py        # Market-data fetch, technical analysis, name enrichment
├── services.py      # Money movements, auto-contributions, snapshots, allocations
├── scheduler.py     # Background scheduler wiring
├── routes.py        # All HTTP handlers + attachment helpers
├── templates/       # login, dashboard, calendar, expenses_history
├── static/          # favicon + record_attachments/ (uploaded documents)
├── requirements.txt
└── run.sh           # Startup script (loads .env, activates venv)
```

Dependency order is acyclic:
`config → models → database → templating/prices → services → scheduler/auth/routes → main`.

## Running

```bash
cd /srv/appdata/web-apps/Portfolio
source .venv/bin/activate
./run.sh            # or: python3 main.py
```

Serves on `0.0.0.0:8000`.

## Environment variables (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `FINNHUB_API_KEY` | *(empty)* | Finnhub API key. Missing → yfinance fallback only. |
| `DATABASE_URL` | `sqlite:///<DATA_DIR>/portfolio.db` | DB connection string |
| `DATA_DIR` | project dir | Where the DB and key files live |
| `DEFAULT_CURRENCY` | `EUR` | Default currency |
| `SECRET_KEY` | auto-generated | Session secret (persisted in `.secret_key`) |
| `RECOVERY_CODE` | auto-generated | Password-recovery code (persisted in `.recovery_key`) |
| `REFRESH_HOUR` / `REFRESH_MINUTE` | `6` / `0` | Daily scheduled-refresh time |
| `PRICE_REFRESH_INTERVAL_SECONDS` | `3600` | Stock/ETF price refresh cadence |
| `FUND_REFRESH_INTERVAL_SECONDS` | `3600` | Fund NAV refresh cadence |
| `SNAPSHOT_INTERVAL_SECONDS` | `3600` | Portfolio snapshot cadence |
| `ROOT_PATH` | *(empty)* | Optional fallback URL prefix; normally auto-detected per request from the `X-Portfolio-Prefix` header set by the reverse proxy (see `request_root_path` in `config.py`) |
| `FT_FUND_SYMBOL_MAP` | *(built-in)* | Override fund ISIN map, `TICKER=ISIN:CUR;…` |
| `ASSET_NAME_ALIASES` | *(built-in)* | Override asset name aliases, `TICKER=Name;…` |

`.env` only needs `FINNHUB_API_KEY` in practice; everything else has sane
defaults.

## Runtime / data files

| File | Purpose | Keep? |
|---|---|---|
| `portfolio.db` | SQLite database (all user data) | ✅ required |
| `.env` | Environment config (API key) | ✅ required |
| `.secret_key` | Session secret (persistent) | ✅ required |
| `.recovery_key` | Password recovery code | ✅ required |
| `.venv/` | Virtual environment | ✅ required to run |
| `static/record_attachments/` | Uploaded document attachments (per user/type) | ✅ user data |

All of the above are ignored by git (see `.gitignore`).

## Features

- **Dashboard** — positions, account/type/geographic/sector allocation,
  an interactive compound-interest calculator for funds, stocks and ETFs,
  portfolio value & invested trend charts with daily P&L markers.
- **Accounts & positions** — cash and investment accounts; buy/sell/edit
  holdings; manual or fetched prices.
- **Market data** — hourly price refresh (5-min scheduler tick), single-ticker
  lookup, 1-year technical analysis.
- **Movimientos (ingresos y gastos)** — expenses, incomes, and transfers in
  one list; auto-categorised expenses; recurring monthly schedules; document
  attachments.
- **Calendario** — month/week/day/year views with week numbers, a running
  projected cash balance per day, and inline add/edit/delete of movements.
- **Metas** — savings goals with a progress bar; progress is either a manual
  amount or the live market value of a linked account. An optional target date
  shows an on-track / overdue / completed badge on the card, plus a suggested
  monthly contribution derived from the user's recurring cash flow (recurring
  incomes − expenses − auto-contributions, per currency). Goals without a date
  show how many months that recurring savings would take to reach the target.
  The section is collapsible and always starts collapsed (expanding is per
  page visit; it expands automatically when adding/editing a goal).
- **Cash interest promos** — `InterestPromo` model for tiered/new-money rates
  (e.g. Sabadell 2.5% first €50k, MyInvestor 2.5% on new money).
- **Simulador de hipoteca** — mortgage simulator with Spain-oriented defaults
  (French amortization, typical financing percentage, purchase costs, opening
  fee), yearly principal/interest breakdown, and remaining balance projection.
  A personal financial survey provides affordability and early-amortization tips;
  named mortgage scenarios can be saved and restored for comparison.
- **Auto-contributions** — recurring buys into funds/ETFs.
- **Auth** — register, login, forgot-password (recovery code), session cookie
  (30 days), clickable profile settings for theme, Spanish/English language,
  and password changes.

## Price data sources

1. **Finnhub** (primary) — quotes/candles, 60 calls/min on the free tier.
2. **yfinance** (fallback) — for anything Finnhub can't serve.
3. **Financial Times** delayed NAV — for `0P…` Morningstar fund codes (mapped
   via `FT_FUND_SYMBOL_MAP`), which Finnhub/Yahoo don't cover.

## Background jobs (APScheduler)

| Job | Cadence |
|---|---|
| `price_refresh` | every 5 min |
| `auto_contributions` | every 30 min |
| `scheduled_expenses` | every 30 min |
| `scheduled_incomes` | every 30 min |
| `scheduled_transfers` | every 30 min |

Portfolio snapshots are also written during the price-refresh loop and on
dashboard load (value-aware, so cash changes show immediately).

## Deployment (systemd)

```ini
[Unit]
Description=Portfolio App
After=network.target

[Service]
Type=simple
User=ivanms
WorkingDirectory=/srv/appdata/web-apps/Portfolio
EnvironmentFile=/srv/appdata/web-apps/Portfolio/.env
ExecStart=/srv/appdata/web-apps/Portfolio/.venv/bin/python3 main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now portfolio
sudo systemctl status portfolio
```

## Gotchas & conventions

- **bcrypt must stay `< 4.0`** while using `passlib[bcrypt]` — `requirements.txt`
  pins `bcrypt==3.2.2`; do not bump it.
- **`TemplateResponse` signature** — use the new
  `TemplateResponse(request, "name.html", ctx)` form.
- **Cash holdings** are assets with `asset_type == "cash"`, `average_cost = 1.0`,
  `manual_price = 1.0`; `quantity` *is* the cash balance. Cost-based charts must
  exclude cash to avoid double-counting.
- **Money is rounded to 2 decimals** (`_round_money`) on every cash mutation to
  avoid float artifacts. Asset `holding.quantity` (shares) is intentionally left
  unrounded.
- **Timezone** — timestamps are stored naive-UTC and rendered in
  `Europe/Madrid`.
- **Restarts** — backend changes (`main.py`/`services.py`/`routes.py`/etc.)
  require a service restart. Template-only edits are picked up by Jinja
  auto-reload immediately.
- **Migrations** — handled by `ensure_schema()` (idempotent `ALTER TABLE`
  additions) rather than Alembic.
