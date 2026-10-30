import socket

import pytest
from markupsafe import Markup
from sqlalchemy import select, text

from app.models import SearchCall
from app.services.ai import CheckResult
from app.services.search import (
    SearchError,
    SearchResult,
    check_search,
    make_search_client,
    search_and_log,
)
from conftest import FIXED_NOW


def test_search_and_log(db, fake_search):
    results = search_and_log(db, fake_search("tvly-sample"), "co-op games")
    assert results[0].url == fake_search.payload["results"][0]["url"]
    assert db.scalars(select(SearchCall)).one().raw_results == fake_search.payload


def test_search_and_log_returns_results_and_records_the_call(db, fake_search):
    results = search_and_log(
        db,
        fake_search("tvly-sample"),
        "co-op games",
        max_results=3,
        run_ref="run-1",
        now=FIXED_NOW,
    )
    assert results == [
        SearchResult(
            "Sample Quest review",
            "https://example.com/sample-quest",
            "A cozy co-op adventure for two.",
        ),
        SearchResult(
            "Test Tactics guide",
            "https://example.com/test-tactics",
            "Turn-based tactics for a small group.",
        ),
    ]
    assert fake_search.calls == [("co-op games", 3)]
    db.expire_all()
    row = db.scalars(select(SearchCall)).one()
    assert row.query == "co-op games"
    assert row.error is None and row.run_ref == "run-1"
    assert row.created_at == FIXED_NOW


def test_search_defaults_to_five_results(db, fake_search):
    search_and_log(db, fake_search("tvly-sample"), "co-op games")
    assert fake_search.calls == [("co-op games", 5)]
    assert db.scalars(select(SearchCall)).one().created_at.tzinfo is not None


def test_search_with_no_results_is_still_a_result(db, fake_search):
    fake_search.payload = {"results": []}
    assert search_and_log(db, fake_search("tvly-sample"), "co-op games") == []
    assert db.scalars(select(SearchCall)).one().raw_results == {"results": []}


def test_search_failure_is_logged_and_raised(db, fake_search):
    fake_search.fail(RuntimeError("invalid key"))
    with pytest.raises(SearchError, match="RuntimeError: invalid key") as raised:
        search_and_log(db, fake_search("bad"), "co-op games", now=FIXED_NOW)
    assert isinstance(raised.value.__cause__, RuntimeError)
    row = db.scalars(select(SearchCall)).one()
    assert row.error == "RuntimeError: invalid key"
    assert row.raw_results is None
    assert row.created_at == FIXED_NOW


def test_failed_search_stores_a_real_null(db, fake_search):
    fake_search.fail(RuntimeError("invalid key"))
    with pytest.raises(SearchError):
        search_and_log(db, fake_search("bad"), "co-op games")
    assert (
        db.execute(text("select raw_results is null from search_call")).scalar_one()
        == 1
    )


def test_check_search_success(db, fake_search):
    assert check_search(db, fake_search("tvly-sample")) == CheckResult(
        True, "Connected"
    )
    assert fake_search.calls == [("best co-op games on Steam", 1)]
    assert db.scalars(select(SearchCall)).one().query == "best co-op games on Steam"


def test_check_search_failure(db, fake_search):
    fake_search.fail(RuntimeError("invalid key"))
    assert check_search(db, fake_search("bad")) == CheckResult(
        False, "Didn't connect: RuntimeError: invalid key"
    )


def test_check_search_failure_text_is_plain_and_capped(db, fake_search):
    fake_search.fail(ValueError("<i>" + "y" * 300))
    result = check_search(db, fake_search("bad"), now=FIXED_NOW)
    assert type(result.message) is str and not isinstance(result.message, Markup)
    assert result.message == "Didn't connect: ValueError: <i>" + "y" * 197
    assert db.scalars(select(SearchCall)).one().error == "ValueError: <i>" + "y" * 300


def test_default_search_client_builds_offline_and_has_the_protocol_shape(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    client = make_search_client("tvly-sample")
    assert callable(client.search)


def test_default_search_client_wraps_tavily(monkeypatch):
    seen = {}

    class FakeTavily:
        def __init__(self, api_key):
            seen["api_key"] = api_key

        def search(self, query, max_results=None):
            seen["query"], seen["max_results"] = query, max_results
            return {"results": []}

    monkeypatch.setattr("app.services.search.TavilyClient", FakeTavily)
    client = make_search_client("tvly-sample")
    assert client.search("co-op games", 4) == {"results": []}
    assert seen == {"api_key": "tvly-sample", "query": "co-op games", "max_results": 4}
