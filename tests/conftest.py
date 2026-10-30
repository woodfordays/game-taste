from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import Services, create_app

FIXED_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def services() -> Services:
    return Services(clock=lambda: FIXED_NOW)


@pytest.fixture
def app(tmp_path, services):
    app = create_app(tmp_path, services=services)
    yield app
    app.state.engine.dispose()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def db(app):
    with app.state.session_factory() as session:
        yield session
