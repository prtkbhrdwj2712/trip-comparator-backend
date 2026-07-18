import os
from sqlalchemy import create_engine
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


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
