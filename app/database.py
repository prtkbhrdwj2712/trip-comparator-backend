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
        else:
            # SQLite has no "IF NOT EXISTS" for ADD COLUMN - check first.
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(trip_baseline)"))]
            if "dc_name" not in cols:
                conn.execute(text("ALTER TABLE trip_baseline ADD COLUMN dc_name VARCHAR"))


def init_db():
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
