from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC (SQLite has no tz); API layer marks it as UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _Session
    if _engine is None or str(_engine.url.database) != str(settings.db_path):
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_con, _):
            cur = dbapi_con.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)
    Base.metadata.create_all(get_engine())
    from .seed import seed_defaults
    with session_scope() as s:
        seed_defaults(s)


def new_session() -> Session:
    get_engine()
    assert _Session is not None
    return _Session()


def session_scope() -> "SessionCtx":
    return SessionCtx()


class SessionCtx:
    def __enter__(self) -> Session:
        self.s = new_session()
        return self.s

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.s.commit()
        else:
            self.s.rollback()
        self.s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s
