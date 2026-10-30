from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app.config import Paths


@dataclass
class Services:
    """Things the app reaches out to, swapped for fakes in tests."""

    clock: Callable[[], datetime]


def default_services() -> Services:
    return Services(clock=lambda: datetime.now(timezone.utc))


def create_app(data_dir: Path, *, services: Services | None = None) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI()
    app.state.paths = Paths(data_dir)
    app.state.services = default_services() if services is None else services

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    return app
