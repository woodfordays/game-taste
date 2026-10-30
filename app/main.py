from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app.config import Paths
from app.db import make_engine, make_session_factory, run_migrations
from app.services.secrets import (
    Vault,
    load_or_create_key,
    load_or_create_session_secret,
)


@dataclass
class Services:
    """Things the app reaches out to, swapped for fakes in tests."""

    clock: Callable[[], datetime]


def default_services() -> Services:
    return Services(clock=lambda: datetime.now(timezone.utc))


def create_app(data_dir: Path, *, services: Services | None = None) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)

    paths = Paths(data_dir)
    run_migrations(paths.db_path)
    vault = Vault(load_or_create_key(paths.key_path))
    session_secret = load_or_create_session_secret(paths.session_secret_path)

    app = FastAPI()
    app.state.paths = paths
    app.state.services = default_services() if services is None else services
    app.state.vault = vault
    app.state.session_secret = session_secret
    app.state.engine = make_engine(paths.db_path)
    app.state.session_factory = make_session_factory(app.state.engine)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    return app
