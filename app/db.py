from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy import Engine, URL, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _sqlite_url(db_path: Path) -> URL:
    return URL.create("sqlite", database=str(db_path))


def make_engine(db_path: Path) -> Engine:
    # FastAPI runs sync routes in a thread pool, so connections cross threads.
    engine = create_engine(
        _sqlite_url(db_path), connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        # A writer waits up to five seconds for another one rather than failing at once. This
        # comes first so that switching to WAL below waits too.
        cursor.execute("PRAGMA busy_timeout=5000")
        # WAL lets the background worker write while pages are being read. The mode is kept in
        # the database file, so after the first connection this only confirms it.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine)


def run_migrations(db_path: Path) -> None:
    """Bring the database at `db_path` up to the latest revision, creating it if needed."""
    config = Config()
    # A literal "%" would be read as interpolation by the config parser.
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    url = _sqlite_url(db_path).render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session
