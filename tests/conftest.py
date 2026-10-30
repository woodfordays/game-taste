from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import advance_step
from app.main import Services, create_app
from app.services.secrets import hash_password
from app.services.settings import set_setting

FIXED_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def fake_request(
    app, cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None
) -> Request:
    """A GET request for `/` with `app` in its scope, for calling render() without a server."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": raw_headers,
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }
    return Request(scope)


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


@pytest.fixture
def logged_in_client(client, db):
    """A client with the password `sample-password` set, logged in, still on the `ai` step."""
    set_setting(db, "password_hash", hash_password("sample-password"))
    response = client.post(
        "/login", data={"password": "sample-password"}, follow_redirects=False
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def onboarded_client(logged_in_client, db):
    """Like `logged_in_client`, with onboarding finished."""
    advance_step(db, "done")
    return logged_in_client
