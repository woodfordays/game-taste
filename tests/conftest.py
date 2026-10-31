from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fakes import VALID, FakeAdapterFactory, FakeSearchFactory, FakeSteam
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import advance_step
from app.main import Services, create_app
from app.models import Game
from app.services.ai_setup import AiSetup, parse_ai_form, save_ai_setup
from app.services.library import sync_library
from app.services.people import add_group, add_person
from app.services.secrets import hash_password
from app.services.settings import set_secret, set_setting
from app.services.steam import SteamClient

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
def fake_ai() -> FakeAdapterFactory:
    return FakeAdapterFactory()


@pytest.fixture
def fake_search() -> FakeSearchFactory:
    return FakeSearchFactory()


@pytest.fixture
def fake_steam() -> FakeSteam:
    return FakeSteam()


@pytest.fixture
def services(fake_ai, fake_search, fake_steam) -> Iterator[Services]:
    services = Services(
        clock=lambda: FIXED_NOW,
        adapter_factory=fake_ai,
        search_factory=fake_search,
        steam_http=httpx.Client(transport=httpx.MockTransport(fake_steam.handler)),
        background=False,
    )
    yield services
    services.steam_http.close()


@pytest.fixture
def steam_client(services) -> SteamClient:
    return SteamClient(services.steam_http)


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
def client_at_steam_step(logged_in_client, db):
    """Like `logged_in_client`, moved on to the `steam` step."""
    advance_step(db, "steam")
    return logged_in_client


@pytest.fixture
def client_at_people_step(logged_in_client, db):
    """Like `logged_in_client`, moved on to the `people` step."""
    advance_step(db, "people")
    return logged_in_client


@pytest.fixture
def onboarded_client(logged_in_client, db):
    """Like `logged_in_client`, with onboarding finished."""
    advance_step(db, "done")
    return logged_in_client


@pytest.fixture
def synced_games(db, app, steam_client):
    """Steam connected (key and ID saved) and the library synced from `fake_steam`'s
    default games: Sample Quest (appid 10, 600 min) and Test Tactics (appid 20, 120 min)."""
    set_secret(db, app.state.vault, "steam_api_key", "steam-sample")
    set_setting(db, "steam_id", "76561198000000001")
    sync_library(db, steam_client, "steam-sample", "76561198000000001", FIXED_NOW)


@pytest.fixture
def games(db):
    """Two owned games straight in the table, no Steam call: Sample Quest (appid 10,
    600 min, last played a day before FIXED_NOW) and Test Tactics (appid 20, 120 min,
    last played two days before)."""
    db.add_all(
        [
            Game(
                appid=10,
                name="Sample Quest",
                owned=True,
                playtime_minutes=600,
                last_played_at=FIXED_NOW - timedelta(days=1),
                source="steam_library",
                created_at=FIXED_NOW,
            ),
            Game(
                appid=20,
                name="Test Tactics",
                owned=True,
                playtime_minutes=120,
                last_played_at=FIXED_NOW - timedelta(days=2),
                source="steam_library",
                created_at=FIXED_NOW,
            ),
        ]
    )
    db.commit()


@pytest.fixture
def people(db):
    """(Sam, Alex, the group "Sample Crew" of both)."""
    sam = add_person(db, "Sam", FIXED_NOW)
    alex = add_person(db, "Alex", FIXED_NOW)
    crew = add_group(db, "Sample Crew", [sam.id, alex.id], FIXED_NOW)
    return sam, alex, crew


@pytest.fixture
def saved_ai_setup(db, app):
    """The `VALID` AI setup saved, as a finished onboarding leaves it: Anthropic (key
    `ant-sample`) and Local, a model for each job, and the Tavily key `tvly-sample`."""
    blank = AiSetup(providers={}, jobs={}, tavily_key="")
    setup, errors = parse_ai_form(VALID, blank)
    assert errors == []
    save_ai_setup(db, app.state.vault, setup, now=FIXED_NOW)
    return setup
