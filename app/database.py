from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./portal.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """
    Production SQLite tuning applied to every new connection:

    - WAL journal mode: readers never block writers; writers don't block readers.
      Concurrent judge scoring sessions under real load no longer serialise.
    - busy_timeout 5000 ms: a second concurrent writer waits up to 5 s instead
      of failing instantly with "database is locked".
    - synchronous NORMAL: safe under WAL (no data loss on power failure after
      a commit), materially faster than FULL.
    - foreign_keys ON: SQLite silently ignores FK constraints by default.
      Enabling this turns referential-integrity violations into actual errors.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
