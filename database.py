from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL, _categorize_expense, pwd_context
from models import Base

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base.metadata.create_all(engine)


def ensure_schema():
    with engine.connect() as conn:
        # assets columns
        asset_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(assets)").fetchall()]
        if "isin" not in asset_cols:
            conn.exec_driver_sql("ALTER TABLE assets ADD COLUMN isin VARCHAR(20)")
        if "manual_price" not in asset_cols:
            conn.exec_driver_sql("ALTER TABLE assets ADD COLUMN manual_price FLOAT")
        if "ter" not in asset_cols:
            conn.exec_driver_sql("ALTER TABLE assets ADD COLUMN ter FLOAT")
        if "tier_limit" not in asset_cols:
            conn.exec_driver_sql("ALTER TABLE assets ADD COLUMN tier_limit FLOAT")
        if "tier_ter" not in asset_cols:
            conn.exec_driver_sql("ALTER TABLE assets ADD COLUMN tier_ter FLOAT")

        # holdings columns (for split TER tracking)
        holdings_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(holdings)").fetchall()]
        if "split_date" not in holdings_cols:
            conn.exec_driver_sql("ALTER TABLE holdings ADD COLUMN split_date DATETIME")
        if "split_ter" not in holdings_cols:
            conn.exec_driver_sql("ALTER TABLE holdings ADD COLUMN split_ter FLOAT")
        if "new_quantity" not in holdings_cols:
            conn.exec_driver_sql("ALTER TABLE holdings ADD COLUMN new_quantity FLOAT")

        # users table
        tables = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "users" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE users (
                    id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    username     VARCHAR(100) UNIQUE NOT NULL,
                    hashed_password VARCHAR(200) NOT NULL,
                    language     VARCHAR(5) NOT NULL DEFAULT 'es'
                )
            """)
        else:
            user_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()]
            if "language" not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN language VARCHAR(5) NOT NULL DEFAULT 'es'")

        if "interest_promos" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE interest_promos (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL REFERENCES users(id),
                    holding_id INTEGER NOT NULL REFERENCES holdings(id),
                    label      VARCHAR(80),
                    rate       FLOAT NOT NULL DEFAULT 0,
                    mode       VARCHAR(10) NOT NULL DEFAULT 'balance',
                    cap        FLOAT,
                    baseline   FLOAT,
                    start_date DATETIME,
                    end_date   DATETIME
                )
            """)

        # accounts.user_id
        acct_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(accounts)").fetchall()]
        if "user_id" not in acct_cols:
            conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN user_id INTEGER REFERENCES users(id)")
            # Create default admin user (password: admin) and reassign existing accounts
            existing_user = conn.exec_driver_sql("SELECT id FROM users LIMIT 1").fetchone()
            if not existing_user:
                hashed = pwd_context.hash("admin")
                conn.exec_driver_sql(
                    "INSERT INTO users (username, hashed_password) VALUES (?, ?)",
                    ("admin", hashed),
                )
            uid = conn.exec_driver_sql("SELECT id FROM users LIMIT 1").fetchone()[0]
            conn.exec_driver_sql(f"UPDATE accounts SET user_id = {uid} WHERE user_id IS NULL")

        # Drop the old global UNIQUE constraint on accounts.name if it still exists.
        # SQLite doesn't support DROP CONSTRAINT, so recreate the table without it.
        idx_rows = conn.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='accounts'"
        ).fetchall()
        has_name_unique = any(
            r[1] and "name" in r[1].upper() and "UNIQUE" in r[1].upper()
            for r in idx_rows
        )
        # Also check inline unique on the column itself via table DDL
        tbl_ddl = (conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone() or ("",))[0] or ""
        inline_unique = (
            "name" in tbl_ddl.lower()
            and "unique" in tbl_ddl.lower()
            and "user_id" not in tbl_ddl.split("UNIQUE")[0].lower().split("name")[-1][:30]
        )
        if has_name_unique or inline_unique:
            conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
            conn.exec_driver_sql("""
                CREATE TABLE accounts_new (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    name    VARCHAR(100) NOT NULL,
                    user_id INTEGER REFERENCES users(id)
                )
            """)
            conn.exec_driver_sql(
                "INSERT INTO accounts_new (id, name, user_id) SELECT id, name, user_id FROM accounts"
            )
            conn.exec_driver_sql("DROP TABLE accounts")
            conn.exec_driver_sql("ALTER TABLE accounts_new RENAME TO accounts")
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")

        # portfolio_snapshots table
        if "portfolio_snapshots" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE portfolio_snapshots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL REFERENCES users(id),
                    timestamp   DATETIME NOT NULL,
                    total_value FLOAT NOT NULL,
                    total_cost  FLOAT NOT NULL
                )
            """)

        # portfolio_type_snapshots table
        if "portfolio_type_snapshots" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE portfolio_type_snapshots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL REFERENCES users(id),
                    timestamp   DATETIME NOT NULL,
                    asset_type  VARCHAR(20) NOT NULL,
                    total_value FLOAT NOT NULL,
                    total_cost  FLOAT
                )
            """)
        type_snapshot_cols = [row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info(portfolio_type_snapshots)").fetchall()]
        if "total_cost" not in type_snapshot_cols:
            conn.exec_driver_sql("ALTER TABLE portfolio_type_snapshots ADD COLUMN total_cost FLOAT")

        # trades table
        if "trades" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE trades (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL REFERENCES users(id),
                    timestamp  DATETIME NOT NULL,
                    ticker     VARCHAR(30),
                    trade_type VARCHAR(10) NOT NULL,
                    quantity   FLOAT NOT NULL,
                    price      FLOAT NOT NULL
                )
            """)

        # watchlist table
        if "watchlist" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE watchlist (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    ticker  VARCHAR(30) NOT NULL,
                    name    VARCHAR(120)
                )
            """)

        # auto_contributions table
        if "auto_contributions" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE auto_contributions (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id              INTEGER NOT NULL REFERENCES users(id),
                    holding_id           INTEGER NOT NULL REFERENCES holdings(id),
                    amount               FLOAT NOT NULL,
                    day_of_month         INTEGER NOT NULL,
                    enabled              INTEGER NOT NULL DEFAULT 1,
                    last_executed_period VARCHAR(7)
                )
            """)
        else:
            ac_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(auto_contributions)").fetchall()]
            if "last_executed_period" not in ac_cols:
                conn.exec_driver_sql("ALTER TABLE auto_contributions ADD COLUMN last_executed_period VARCHAR(7)")

        # expense_schedules table
        if "expense_schedules" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE expense_schedules (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id              INTEGER NOT NULL REFERENCES users(id),
                    account_id           INTEGER NOT NULL REFERENCES accounts(id),
                    name                 VARCHAR(120) NOT NULL,
                    amount               FLOAT NOT NULL,
                    day_of_month         INTEGER NOT NULL,
                    enabled              INTEGER NOT NULL DEFAULT 1,
                    last_executed_period VARCHAR(7),
                    notes                VARCHAR(255),
                    category             VARCHAR(40)
                )
            """)
        else:
            es_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(expense_schedules)").fetchall()]
            if "notes" not in es_cols:
                conn.exec_driver_sql("ALTER TABLE expense_schedules ADD COLUMN notes VARCHAR(255)")
            if "last_executed_period" not in es_cols:
                conn.exec_driver_sql("ALTER TABLE expense_schedules ADD COLUMN last_executed_period VARCHAR(7)")
            if "category" not in es_cols:
                conn.exec_driver_sql("ALTER TABLE expense_schedules ADD COLUMN category VARCHAR(40)")

        # expense_records table
        if "expense_records" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE expense_records (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL REFERENCES users(id),
                    account_id   INTEGER NOT NULL REFERENCES accounts(id),
                    schedule_id  INTEGER REFERENCES expense_schedules(id),
                    name         VARCHAR(120) NOT NULL,
                    amount       FLOAT NOT NULL,
                    currency     VARCHAR(10) NOT NULL DEFAULT 'EUR',
                    timestamp    DATETIME NOT NULL,
                    notes        VARCHAR(255),
                    period_label VARCHAR(7),
                    attachment_path VARCHAR(255),
                    attachment_name VARCHAR(255),
                    category     VARCHAR(40)
                )
            """)
        else:
            er_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(expense_records)").fetchall()]
            if "notes" not in er_cols:
                conn.exec_driver_sql("ALTER TABLE expense_records ADD COLUMN notes VARCHAR(255)")
            if "period_label" not in er_cols:
                conn.exec_driver_sql("ALTER TABLE expense_records ADD COLUMN period_label VARCHAR(7)")
            if "attachment_path" not in er_cols:
                conn.exec_driver_sql("ALTER TABLE expense_records ADD COLUMN attachment_path VARCHAR(255)")
            if "attachment_name" not in er_cols:
                conn.exec_driver_sql("ALTER TABLE expense_records ADD COLUMN attachment_name VARCHAR(255)")
            if "category" not in er_cols:
                conn.exec_driver_sql("ALTER TABLE expense_records ADD COLUMN category VARCHAR(40)")

        # income_schedules table
        if "income_schedules" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE income_schedules (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id              INTEGER NOT NULL REFERENCES users(id),
                    account_id           INTEGER NOT NULL REFERENCES accounts(id),
                    name                 VARCHAR(120) NOT NULL,
                    amount               FLOAT NOT NULL,
                    day_of_month         INTEGER NOT NULL,
                    enabled              INTEGER NOT NULL DEFAULT 1,
                    last_executed_period VARCHAR(7),
                    notes                VARCHAR(255)
                )
            """)
        else:
            is_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(income_schedules)").fetchall()]
            if "notes" not in is_cols:
                conn.exec_driver_sql("ALTER TABLE income_schedules ADD COLUMN notes VARCHAR(255)")
            if "last_executed_period" not in is_cols:
                conn.exec_driver_sql("ALTER TABLE income_schedules ADD COLUMN last_executed_period VARCHAR(7)")

        # income_records table
        if "income_records" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE income_records (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL REFERENCES users(id),
                    account_id   INTEGER NOT NULL REFERENCES accounts(id),
                    schedule_id  INTEGER REFERENCES income_schedules(id),
                    name         VARCHAR(120) NOT NULL,
                    amount       FLOAT NOT NULL,
                    currency     VARCHAR(10) NOT NULL DEFAULT 'EUR',
                    timestamp    DATETIME NOT NULL,
                    notes        VARCHAR(255),
                    period_label VARCHAR(7),
                    attachment_path VARCHAR(255),
                    attachment_name VARCHAR(255)
                )
            """)
        else:
            ir_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(income_records)").fetchall()]
            if "notes" not in ir_cols:
                conn.exec_driver_sql("ALTER TABLE income_records ADD COLUMN notes VARCHAR(255)")
            if "period_label" not in ir_cols:
                conn.exec_driver_sql("ALTER TABLE income_records ADD COLUMN period_label VARCHAR(7)")
            if "attachment_path" not in ir_cols:
                conn.exec_driver_sql("ALTER TABLE income_records ADD COLUMN attachment_path VARCHAR(255)")
            if "attachment_name" not in ir_cols:
                conn.exec_driver_sql("ALTER TABLE income_records ADD COLUMN attachment_name VARCHAR(255)")

        # transfer_schedules table
        if "transfer_schedules" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE transfer_schedules (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id              INTEGER NOT NULL REFERENCES users(id),
                    from_account_id      INTEGER NOT NULL REFERENCES accounts(id),
                    to_account_id        INTEGER NOT NULL REFERENCES accounts(id),
                    amount               FLOAT NOT NULL,
                    day_of_month         INTEGER NOT NULL,
                    enabled              INTEGER NOT NULL DEFAULT 1,
                    last_executed_period VARCHAR(7),
                    notes                VARCHAR(255)
                )
            """)

        # transfer_records table
        if "transfer_records" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE transfer_records (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER NOT NULL REFERENCES users(id),
                    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    to_account_id   INTEGER NOT NULL REFERENCES accounts(id),
                    schedule_id     INTEGER REFERENCES transfer_schedules(id),
                    amount          FLOAT NOT NULL,
                    timestamp       DATETIME NOT NULL,
                    notes           VARCHAR(255),
                    period_label    VARCHAR(7)
                )
            """)

        # mortgage profile and saved scenarios
        if "mortgage_profiles" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE mortgage_profiles (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id               INTEGER NOT NULL UNIQUE REFERENCES users(id),
                    age                   INTEGER NOT NULL,
                    employment_status     VARCHAR(30) NOT NULL,
                    monthly_net_income    FLOAT NOT NULL,
                    monthly_fixed_expenses FLOAT NOT NULL,
                    monthly_debt_payments FLOAT NOT NULL,
                    savings               FLOAT NOT NULL,
                    dependents            INTEGER NOT NULL DEFAULT 0,
                    property_use          VARCHAR(20) NOT NULL DEFAULT 'primary',
                    created_at            DATETIME NOT NULL,
                    updated_at            DATETIME NOT NULL
                )
            """)
        if "saved_mortgages" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE saved_mortgages (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id              INTEGER NOT NULL REFERENCES users(id),
                    name                 VARCHAR(120) NOT NULL,
                    property_price       FLOAT NOT NULL,
                    initial_contribution FLOAT NOT NULL,
                    purchase_costs_pct   FLOAT NOT NULL,
                    term_years           INTEGER NOT NULL,
                    interest_rate        FLOAT NOT NULL,
                    opening_fee_pct      FLOAT NOT NULL,
                    monthly_extra        FLOAT NOT NULL,
                    created_at           DATETIME NOT NULL
                )
            """)

        # goals.target_date column (optional deadline for overdue/on-track display)
        goal_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(goals)").fetchall()]
        if goal_cols and "target_date" not in goal_cols:
            conn.exec_driver_sql("ALTER TABLE goals ADD COLUMN target_date DATE")

        # Backfill categories for legacy expense rows (idempotent).
        for eid, ename in conn.exec_driver_sql(
            "SELECT id, name FROM expense_records WHERE category IS NULL OR category = ''"
        ).fetchall():
            conn.exec_driver_sql(
                "UPDATE expense_records SET category = ? WHERE id = ?",
                (_categorize_expense(ename), eid),
            )
        for sid, sname in conn.exec_driver_sql(
            "SELECT id, name FROM expense_schedules WHERE category IS NULL OR category = ''"
        ).fetchall():
            conn.exec_driver_sql(
                "UPDATE expense_schedules SET category = ? WHERE id = ?",
                (_categorize_expense(sname), sid),
            )

        conn.commit()
