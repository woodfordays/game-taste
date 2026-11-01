from datetime import datetime, timezone

import httpx
import pytest
from fakes import store_data

from app.services.steam import (
    PatchNews,
    ReviewSummary,
    SteamClient,
    SteamError,
    StoreDetails,
    parse_app_details,
)

NO_RESPONSE = "Steam didn't respond. Try again in a minute."


def client_answering(**kwargs) -> SteamClient:
    """A client whose every request gets the same reply: `json=...` or `text=...`."""
    return SteamClient(
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, **kwargs))
        )
    )


def test_app_details_parses_store_data(steam_client, fake_steam):
    fake_steam.details[10] = store_data(
        "Sample Quest",
        categories=("Single-player", "Online Co-op"),
        early_access=True,
        price_cents=1499,
    )
    d = steam_client.app_details(10, "us")
    assert (d.name, d.type, d.early_access, d.price_cents, d.price_text) == (
        "Sample Quest",
        "game",
        True,
        1499,
        "$14.99",
    )
    assert d.categories == ["Single-player", "Online Co-op"]
    assert fake_steam.requests[-1].url.params["cc"] == "us"


def test_app_details_reads_every_field(steam_client, fake_steam):
    fake_steam.details[10] = store_data(
        "Sample Quest", coming_soon=True, release_date="Coming soon", type="demo"
    )
    assert steam_client.app_details(10, "gb") == StoreDetails(
        appid=10,
        type="demo",
        name="Sample Quest",
        short_description="About Sample Quest.",
        is_free=False,
        price_cents=1999,
        currency="USD",
        price_text="$19.99",
        release_date="Coming soon",
        coming_soon=True,
        early_access=False,
        categories=["Single-player"],
        genres=["Action"],
        developers=["Sample Studio"],
    )


def test_app_details_asks_the_store_for_one_app_in_english(steam_client, fake_steam):
    steam_client.app_details(10, "gb")
    request = fake_steam.requests[-1]
    assert str(request.url).startswith("https://store.steampowered.com/api/appdetails?")
    assert dict(request.url.params) == {"appids": "10", "cc": "gb", "l": "english"}


def test_unknown_app_is_none(steam_client):  # Review focus 2
    assert steam_client.app_details(999, "us") is None


def test_an_app_the_store_withholds_is_none(steam_client, fake_steam):  # Review focus 2
    fake_steam.details[10] = None
    assert steam_client.app_details(10, "us") is None


def test_free_game_has_no_price(steam_client, fake_steam):  # Review focus 2
    fake_steam.details[20] = store_data("Test Tactics", is_free=True)
    d = steam_client.app_details(20, "us")
    assert d.is_free and d.price_cents is None and d.price_text is None
    assert d.currency is None


def test_unreleased_game_has_no_price(steam_client, fake_steam):  # Review focus 2
    fake_steam.details[20] = store_data(
        "Test Tactics", price_cents=None, coming_soon=True
    )
    d = steam_client.app_details(20, "us")
    assert not d.is_free
    assert d.price_cents is None and d.price_text is None and d.coming_soon


def test_an_entry_that_is_not_a_game_keeps_its_type(
    steam_client, fake_steam
):  # Review focus 2
    fake_steam.details[40] = store_data("Cozy Crawl Soundtrack", type="music")
    assert steam_client.app_details(40, "us").type == "music"


def test_early_access_is_a_genre(steam_client, fake_steam):
    fake_steam.details[10] = store_data("Sample Quest", early_access=True)
    fake_steam.details[20] = store_data("Test Tactics")
    early = steam_client.app_details(10, "us")
    assert early.early_access is True and "Early Access" in early.genres
    assert steam_client.app_details(20, "us").early_access is False


def test_early_access_by_genre_id_or_by_name():
    # Steam sends the genre id as a string. Either the id or the name is enough.
    by_name = store_data("Demo Dungeon")
    by_name["genres"] = [{"id": "999", "description": "Early Access"}]
    by_id = store_data("Cozy Crawl")
    by_id["genres"] = [{"id": "70", "description": "Anything"}]
    assert parse_app_details(
        30, {"30": {"success": True, "data": by_name}}
    ).early_access
    assert parse_app_details(40, {"40": {"success": True, "data": by_id}}).early_access


def test_short_description_is_plain_text():
    # Steam sends the description HTML-escaped. Templates escape again, and prompts
    # would carry the entities, so it is stored as plain text.
    data = store_data("Sample Quest")
    data["short_description"] = "Build &quot;tiny&quot; towns &amp; more"
    details = parse_app_details(10, {"10": {"success": True, "data": data}})
    assert details.short_description == 'Build "tiny" towns & more'


def test_missing_optional_fields_take_their_defaults():
    payload = {"10": {"success": True, "data": {"name": "Sample Quest"}}}
    assert parse_app_details(10, payload) == StoreDetails(
        appid=10,
        type="",
        name="Sample Quest",
        short_description="",
        is_free=False,
        price_cents=None,
        currency=None,
        price_text=None,
        release_date="",
        coming_soon=False,
        early_access=False,
        categories=[],
        genres=[],
        developers=[],
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"11": {"success": True, "data": {"name": "Other"}}},
        {"10": {"success": True}},
        {"10": {"success": True, "data": []}},
        {"10": {"success": True, "data": {"type": "game"}}},
        {"10": {"success": True, "data": {"name": ""}}},
        {"10": []},
    ],
)
def test_a_reply_without_the_app_or_its_name_is_no_response(payload):
    with pytest.raises(SteamError) as caught:
        parse_app_details(10, payload)
    assert str(caught.value) == NO_RESPONSE


def test_a_failed_lookup_is_none_even_with_nothing_else_in_the_entry():
    assert parse_app_details(10, {"10": {"success": False}}) is None


@pytest.mark.parametrize(
    "failure", [500, httpx.ConnectTimeout("slow")]
)  # Review focus 2
def test_store_failures_are_steam_errors(steam_client, fake_steam, failure):
    fake_steam.fail_with(failure)
    for call in (
        lambda: steam_client.app_details(10, "us"),
        lambda: steam_client.review_summary(10),
        lambda: steam_client.patch_news(10),
    ):
        with pytest.raises(SteamError, match="Steam didn't respond"):
            call()


@pytest.mark.parametrize("body", ["not json", "[]", "{}"])
def test_unexpected_bodies_read_as_no_response(body):
    client = client_answering(text=body)
    for call in (
        lambda: client.app_details(10, "us"),
        lambda: client.review_summary(10),
        lambda: client.patch_news(10),
    ):
        with pytest.raises(SteamError) as caught:
            call()
        assert str(caught.value) == NO_RESPONSE


def test_review_summary(steam_client, fake_steam):
    fake_steam.review_summaries[10] = {
        "review_score_desc": "Mostly Positive",
        "total_positive": 70,
        "total_negative": 20,
        "total_reviews": 90,
    }
    assert steam_client.review_summary(10) == ReviewSummary(
        "Mostly Positive", 70, 20, 90
    )


def test_review_summary_asks_for_the_totals_only(steam_client, fake_steam):
    steam_client.review_summary(10)
    request = fake_steam.requests[-1]
    assert request.url.path == "/appreviews/10"
    assert request.url.host == "store.steampowered.com"
    assert dict(request.url.params) == {
        "json": "1",
        "language": "all",
        "purchase_type": "all",
        "num_per_page": "0",
    }


def test_an_app_with_no_reviews_says_so(steam_client, fake_steam):
    fake_steam.review_summaries[10] = {
        "review_score_desc": "No user reviews",
        "total_positive": 0,
        "total_negative": 0,
        "total_reviews": 0,
    }
    assert steam_client.review_summary(10) == ReviewSummary("No user reviews", 0, 0, 0)


@pytest.mark.parametrize(
    "body",
    [
        {"success": 0},
        {"success": 1},
        {"success": 1, "query_summary": []},
        {"success": 2, "query_summary": {"review_score_desc": "Very Positive"}},
    ],
)
def test_a_review_reply_without_a_summary_is_no_response(body):
    with pytest.raises(SteamError) as caught:
        client_answering(json=body).review_summary(10)
    assert str(caught.value) == NO_RESPONSE


def test_patch_news_takes_the_newest_patch_notes(steam_client, fake_steam):
    fake_steam.news[10] = [
        {"title": "Big sale", "date": 1767900000, "tags": []},
        {"title": "Patch 1.1", "date": 1767800000, "tags": ["patchnotes"]},
        {
            "title": "Patch 1.2",
            "date": 1767850000,
            "tags": ["patchnotes", "mod_reviewed"],
        },
    ]
    assert steam_client.patch_news(10) == PatchNews(
        datetime.fromtimestamp(1767850000, tz=timezone.utc), "Patch 1.2"
    )


def test_patch_news_is_in_utc(steam_client, fake_steam):
    fake_steam.news[10] = [
        {"title": "Patch", "date": 1767850000, "tags": ["patchnotes"]}
    ]
    assert steam_client.patch_news(10).latest_at.utcoffset().total_seconds() == 0


def test_patch_news_asks_for_the_announcements_feed_without_a_key(
    steam_client, fake_steam
):
    steam_client.patch_news(10)
    request = fake_steam.requests[-1]
    assert str(request.url).startswith(
        "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?"
    )
    assert dict(request.url.params) == {
        "appid": "10",
        "count": "20",
        "maxlength": "1",
        "feeds": "steam_community_announcements",
    }


def test_news_items_without_tags_or_dates_are_skipped(steam_client, fake_steam):
    fake_steam.news[10] = [
        {"title": "No tags", "date": 1767900000},
        {"title": "Null tags", "date": 1767900000, "tags": None},
        {"title": "No date", "tags": ["patchnotes"]},
        {"title": "Bad date", "date": "soon", "tags": ["patchnotes"]},
        "not an item",
        {"title": "Patch 1.1", "date": 1767800000, "tags": ["patchnotes"]},
    ]
    assert steam_client.patch_news(10) == PatchNews(
        datetime.fromtimestamp(1767800000, tz=timezone.utc), "Patch 1.1"
    )


def test_no_patch_notes(steam_client):
    assert steam_client.patch_news(10) == PatchNews(None, None)


def test_news_that_is_all_announcements_has_no_patch_notes(steam_client, fake_steam):
    fake_steam.news[10] = [{"title": "Big sale", "date": 1767900000, "tags": ["sale"]}]
    assert steam_client.patch_news(10) == PatchNews(None, None)


def test_a_news_reply_without_an_item_list_is_no_response():
    for body in ({"appnews": []}, {"appnews": {}}, {"appnews": {"newsitems": "x"}}):
        with pytest.raises(SteamError) as caught:
            client_answering(json=body).patch_news(10)
        assert str(caught.value) == NO_RESPONSE


def test_none_of_the_three_sends_the_api_key(steam_client, fake_steam):
    steam_client.app_details(10, "us")
    steam_client.review_summary(10)
    steam_client.patch_news(10)
    assert len(fake_steam.requests) == 3
    assert all("key" not in request.url.params for request in fake_steam.requests)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_store_call_is_not_a_refused_key(steam_client, fake_steam, status):
    # The store and the news take no key, so a 401 is just "didn't respond".
    fake_steam.fail_with(status)
    for call in (
        lambda: steam_client.app_details(10, "us"),
        lambda: steam_client.review_summary(10),
        lambda: steam_client.patch_news(10),
    ):
        with pytest.raises(SteamError) as caught:
            call()
        assert str(caught.value) == NO_RESPONSE
