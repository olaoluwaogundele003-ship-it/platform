"""SQLite database setup. Monolithic app uses a single local DB file."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os

DB_PATH = os.environ.get("PLATFORM_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "platform.db"))
SQLALCHEMY_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(SQLALCHEMY_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
