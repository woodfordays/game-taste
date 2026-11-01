import json
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from fakes import (
    FIXED_NOW,
    VALID,
    FakeAdapterFactory,
    FakeSearchFactory,
    FakeSteam,
    by_prompt,
    counts_reply,
    lane_version,
    profile_reply,
    store_data,
    verdicts,
)
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import advance_step
from app.main import Services, create_app
from app.models import Game
from app.services.ai_setup import AiSetup, parse_ai_form, save_ai_setup
from app.services.game_facts import ensure_game
from app.services.library import sync_library
from app.services.people import add_group, add_person
from app.services.research_runs import create_run
from app.services.reviews import Lane, ReviewForm, publish
from app.services.secrets import hash_password
from app.services.settings import set_secret, set_setting
from app.services.steam import SteamClient


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


@pytest.fixture
def solo_version(db, games):
    """The Solo profile's version 1, learned from a published Solo review of Sample Quest
    (appid 10, rating 8, "Short runs, real choices every turn."). It has one stated line
    under `loved`, "Likes short, tight sessions", that cites the game and the review."""
    review = publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=8, text="Short runs, real choices every turn."),
        FIXED_NOW,
    )
    return lane_version(db, Lane.SOLO, review, "Likes short, tight sessions")


@pytest.fixture
def research_run(db):
    """A research run that is running, with no lanes ticked for five more."""
    return create_run(db, set(), FIXED_NOW)


@pytest.fixture
def research_world(
    db, games, saved_ai_setup, solo_version, fake_ai, fake_search, fake_steam
) -> dict:
    """Solo research, ready to run.

    - Sample Quest (10) is reviewed in Solo, so research leaves it out, and Test Tactics
      (20, 120 minutes) has too much playtime for the backlog. Old Crate (60) is owned
      with 30 minutes: the backlog pick.
    - Every web search finds one result, "Cozy picks", which names Cozy Crawl. The store
      search resolves "Cozy Crawl" to 40, which is in the game table as research leaves
      a game it found (not owned, not in the Library), so a test can give it earlier
      candidates.
    - Steam has single-player store entries for 40 ($9.99) and 60 ($4.99).

    Returns `world`, whose entries the fake replies read each time they are asked, so a
    test can change one before running:
    - `plan`, `extract`, `propose`: the Recommender's replies (sample-big).
    - `judge`: the judge's reply, or `judge_fn(prompt)` when that is set.
    - `players`: the `player_counts` reply, a string or a function of the prompt.
    - `profile`: the Everyday model's reply to anything else, a profile update that keeps
      the Solo line and cites the Solo review.
    """
    db.add(
        Game(
            appid=60,
            name="Old Crate",
            owned=True,
            playtime_minutes=30,
            last_played_at=None,
            source="steam_library",
            created_at=FIXED_NOW,
        )
    )
    db.commit()
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    fake_search.responder = lambda query: {
        "query": query,
        "results": [
            {
                "title": "Cozy picks",
                "url": "https://example.com/cozy",
                "content": "Try Cozy Crawl.",
            }
        ],
    }
    fake_steam.store_hits = [{"id": 40, "name": "Cozy Crawl"}]
    fake_steam.details[40] = store_data("Cozy Crawl", price_cents=999)
    fake_steam.details[60] = store_data("Old Crate", price_cents=499)
    [solo_review] = solo_version.content["loved"][0]["reviews"]
    world = {
        "plan": {"backlog": [60], "queries": ["short cozy roguelites"]},
        "extract": {
            "games": [{"name": "Cozy Crawl", "url": "https://example.com/cozy"}]
        },
        "propose": {
            "proposals": [
                {
                    "appid": 40,
                    "fits": [{"line": "L1", "how": "Runs take twenty minutes."}],
                    "risk": "Light on depth.",
                },
                {
                    "appid": 60,
                    "fits": [{"line": "L1", "how": "Quick to pick up."}],
                    "risk": "Slow start.",
                },
            ]
        },
        "judge": verdicts(accept=[40], reject={60: "Runs are too long."}),
        "judge_fn": None,
        "players": counts_reply(url="https://example.com/cozy"),
        "profile": profile_reply(
            loved=[
                {
                    "text": "Likes short, tight sessions",
                    "tag": "stated",
                    "games": [10],
                    "reviews": [solo_review],
                }
            ]
        ),
    }

    def judge_reply(prompt: str) -> str:
        if world["judge_fn"] is not None:
            return world["judge_fn"](prompt)
        return json.dumps(world["judge"])

    def players_reply(prompt: str) -> str:
        players = world["players"]
        return players(prompt) if callable(players) else players

    fake_ai.replies["sample-big"] = by_prompt(
        research_plan=lambda prompt: json.dumps(world["plan"]),
        research_extract=lambda prompt: json.dumps(world["extract"]),
        research_propose=lambda prompt: json.dumps(world["propose"]),
    )
    fake_ai.replies["sample-local"] = by_prompt(
        lambda prompt: world["profile"],
        research_judge=judge_reply,
        player_counts=players_reply,
    )
    return world
