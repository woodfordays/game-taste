import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.auth import install_auth
from app.config import Paths
from app.db import make_engine, make_session_factory, run_migrations
from app.jobs import background_lifespan, recover_interrupted_jobs
from app.routes import auth as auth_routes
from app.routes import home as home_routes
from app.routes import library as library_routes
from app.routes import onboarding as onboarding_routes
from app.routes import reviews as review_routes
from app.routes import settings as settings_routes
from app.services.ai import Adapter, ProviderConfig, make_adapter
from app.services.search import SearchClient, make_search_client
from app.services.secrets import (
    Vault,
    load_or_create_key,
    load_or_create_session_secret,
)
from app.services.settings import bump_session_epoch, delete_setting
from app.web import STATIC_DIR


@dataclass
class Services:
    """Things the app reaches out to, swapped for fakes in tests."""

    clock: Callable[[], datetime]
    adapter_factory: Callable[[ProviderConfig], Adapter] = make_adapter
    search_factory: Callable[[str], SearchClient] = make_search_client
    steam_http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=20))
    # Whether the worker and scheduler threads run. Tests turn this off and run jobs by hand.
    background: bool = True


def default_services() -> Services:
    return Services(clock=lambda: datetime.now(timezone.utc))


def create_app(data_dir: Path, *, services: Services | None = None) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)

    paths = Paths(data_dir)
    run_migrations(paths.db_path)
    vault = Vault(load_or_create_key(paths.key_path))
    session_secret = load_or_create_session_secret(paths.session_secret_path)

    services = default_services() if services is None else services

    # No generated API pages: nothing here is a public API, and they would sit open to anyone
    # who can reach the port.
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=background_lifespan if services.background else None,
    )
    app.state.paths = paths
    app.state.services = services
    app.state.vault = vault
    app.state.session_secret = session_secret
    app.state.engine = make_engine(paths.db_path)
    app.state.session_factory = make_session_factory(app.state.engine)
    # Handlers by job kind, each called with a JobContext and the job's id.
    app.state.job_handlers = {}
    app.state.job_wakeup = threading.Event()

    with app.state.session_factory() as db:
        # A job still "running" now was cut off by the last shutdown. Fail it, so that it
        # doesn't block the next one.
        recover_interrupted_jobs(db, services.clock())

    if paths.reset_flag_path.exists():
        # Forgot the password: drop it, keep everything else, and ask for a new one.
        # Every session ends too, so a cookie from before can't walk back in afterwards.
        with app.state.session_factory() as db:
            delete_setting(db, "password_hash")
            bump_session_epoch(db)
        paths.reset_flag_path.unlink(missing_ok=True)

    install_auth(app)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    app.include_router(auth_routes.router)
    app.include_router(onboarding_routes.router)
    app.include_router(home_routes.router)
    app.include_router(library_routes.router)
    app.include_router(review_routes.router)
    app.include_router(settings_routes.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
