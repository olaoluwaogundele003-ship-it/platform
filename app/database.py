"""SQLite database setup. Monolithic app uses a single local DB file."""
from sqlalchemy import create_engine, text
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


def _columns(table):
    with engine.connect() as c:
        return {r[1] for r in c.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def init_db():
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # lightweight migration for DBs created before auth fields existed
    try:
        cols = _columns("users")
        with engine.begin() as c:
            if "username" not in cols:
                c.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR(64)"))
            if "password_hash" not in cols:
                c.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(256) DEFAULT ''"))
        mcols = _columns("memberships")
        with engine.begin() as c:
            if "prefs" not in mcols:
                c.execute(text("ALTER TABLE memberships ADD COLUMN prefs JSON"))
    except Exception:
        pass
