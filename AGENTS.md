# Portfolio Pi — Project Context

Read this before working on the repo. It saves re-explaining the stack.

## What it is

Self-hosted personal finance & portfolio tracker (Spanish UI) for Iván.
Serves at https://ivanms-apps.duckdns.org/Portfolio/ via Caddy → port 8000.

## Stack

- Python 3.13, FastAPI 0.115, uvicorn (host process, systemd unit `portfolio`)
- SQLAlchemy 2.0 + SQLite (`portfolio.db`, WAL). No Alembic — schema
  migrations are hand-rolled in `database.py: ensure_schema()`
- Jinja2 templates (`templates/`), Tailwind CSS
- APScheduler background jobs registered in `scheduler.py`:
  prices every 5 min; auto-contributions + scheduled expenses/incomes/
  transfers every 30 min
- Market data: Finnhub → yfinance → FT fund NAV fallback
- Auth: shared JWT cookie from ../auth service (verify via `_shared/auth_client.py`)
- bcrypt MUST stay < 4.0 (pin in requirements.txt)

## Folder structure

```
main.py        FastAPI app, lifespan startup (scheduler + initial job runs)
config.py      Constants, .env, categories, MADRID_TZ
models.py      All SQLAlchemy models
database.py    Engine + ensure_schema() migrations
services.py    Business logic: money movements, schedules, snapshots,
               auto-contributions, prices refresh. THE core file.
scheduler.py   APScheduler job registration
prices.py      Market data fetch + technical analysis
routes.py      All HTTP handlers
auth.py        Legacy auth helpers (login now delegated to ../auth)
templating.py  Shared Jinja2Templates instance
templates/     login, dashboard, calendar, expenses_history…
static/        favicon + record_attachments/user_<id>/ (uploaded docs)
portfolio.db   SQLite database (also -wal/-shm while running)
```

## Conventions

- Timestamps stored naive-UTC; rendered in Europe/Madrid (`MADRID_TZ`).
- Recurring schedules execute once per calendar month, tracked by
  `last_executed_period` ("YYYY-MM") on each schedule row. Execution day =
  configured day adjusted to next market day (previous for Nómina).
- Money as float rounded to 2 decimals via `_round_money`.
- Spanish UI strings directly in templates; i18n helper exists but ES is primary.
- Dependency order is acyclic: config → models → database → templating/prices
  → services → scheduler/auth/routes → main.

## Critical gotchas

- `portfolio-watcher.service` auto-restarts this app whenever source files
  change — expect restarts after every edit.
- On startup `main.py` runs pending scheduled jobs ONCE. The scheduler
  functions must stay concurrency-safe (BEGIN IMMEDIATE lock +
  no-backward-period check in `_pending_schedule_period`). A regression here
  duplicated thousands of records once (2026-08-23); don't undo it.
- Never commit `.env`, `.shared_secret_key`, `.recovery_key`.
- DB backups: copy portfolio.db with service stopped.

## Running

    sudo systemctl restart portfolio     # or stop/start
    journalctl -u portfolio -f           # logs
    ./run.sh                             # manual start (venv + main)
