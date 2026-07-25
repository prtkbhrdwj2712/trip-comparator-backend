import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base

# On Render, set DATABASE_URL to the Postgres connection string it gives you
# (Internal Database URL from your Render Postgres instance).
# Falls back to a local sqlite file for testing on your own machine.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./local_dev.db")

# Render's Postgres URL starts with postgres:// ; SQLAlchemy wants postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Base.metadata.create_all() only creates tables that don't exist yet - it
# does NOT add new columns to tables that already exist. Since this runs
# against a live database with real production data, any schema change from
# here on needs an explicit, additive (never destructive) migration step
# like this one, rather than relying on create_all alone.
def _run_migrations():
    is_postgres = DATABASE_URL.startswith("postgresql")
    with engine.begin() as conn:
        if is_postgres:
            conn.execute(text("ALTER TABLE trip_baseline ADD COLUMN IF NOT EXISTS dc_name VARCHAR"))
            conn.execute(text("ALTER TABLE trip_baseline ADD COLUMN IF NOT EXISTS baseline_unavailable INTEGER DEFAULT 0"))
            conn.execute(text("ALTER TABLE stop_baseline ADD COLUMN IF NOT EXISTS reference_order_number VARCHAR"))
            conn.execute(text("ALTER TABLE stop_confirmed ADD COLUMN IF NOT EXISTS reference_order_number VARCHAR"))
            conn.execute(text("ALTER TABLE stop_baseline ADD COLUMN IF NOT EXISTS address VARCHAR"))
            conn.execute(text("ALTER TABLE stop_confirmed ADD COLUMN IF NOT EXISTS address VARCHAR"))
            conn.execute(text("ALTER TABLE dashboard_user ADD COLUMN IF NOT EXISTS totp_secret VARCHAR"))
            conn.execute(text("ALTER TABLE dashboard_user ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER DEFAULT 0"))
            conn.execute(text("ALTER TABLE dashboard_user ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP"))
        else:
            # SQLite has no "IF NOT EXISTS" for ADD COLUMN - check first.
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(trip_baseline)"))]
            if "dc_name" not in cols:
                conn.execute(text("ALTER TABLE trip_baseline ADD COLUMN dc_name VARCHAR"))
            if "baseline_unavailable" not in cols:
                conn.execute(text("ALTER TABLE trip_baseline ADD COLUMN baseline_unavailable INTEGER DEFAULT 0"))
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(stop_baseline)"))]
            if "reference_order_number" not in cols:
                conn.execute(text("ALTER TABLE stop_baseline ADD COLUMN reference_order_number VARCHAR"))
            if "address" not in cols:
                conn.execute(text("ALTER TABLE stop_baseline ADD COLUMN address VARCHAR"))
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(stop_confirmed)"))]
            if "reference_order_number" not in cols:
                conn.execute(text("ALTER TABLE stop_confirmed ADD COLUMN reference_order_number VARCHAR"))
            if "address" not in cols:
                conn.execute(text("ALTER TABLE stop_confirmed ADD COLUMN address VARCHAR"))
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(dashboard_user)"))]
            if "totp_secret" not in cols:
                conn.execute(text("ALTER TABLE dashboard_user ADD COLUMN totp_secret VARCHAR"))
            if "failed_login_attempts" not in cols:
                conn.execute(text("ALTER TABLE dashboard_user ADD COLUMN failed_login_attempts INTEGER DEFAULT 0"))
            if "locked_until" not in cols:
                conn.execute(text("ALTER TABLE dashboard_user ADD COLUMN locked_until TIMESTAMP"))

    _migrate_plaintext_keys_to_hashed()


def _migrate_plaintext_keys_to_hashed():
    """
    One-time, idempotent data migration: any DashboardUser row still holding
    a plaintext access_key (from before hashing was introduced) gets it
    replaced with a bcrypt hash of that SAME value - so existing users'
    passwords keep working exactly as before, just stored safely from now on.
    Safe to run on every startup: already-hashed rows are left untouched.
    """
    from .models import DashboardUser
    from .password_utils import hash_key, is_hashed

    db = SessionLocal()
    try:
        users = db.query(DashboardUser).all()
        migrated = 0
        for u in users:
            if u.access_key and not is_hashed(u.access_key):
                u.access_key = hash_key(u.access_key)
                migrated += 1
        if migrated:
            db.commit()
            print(f"[migration] Hashed {migrated} legacy plaintext dashboard user key(s).")
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
