import re
from datetime import datetime, timezone

import httpx
import pytest

from app.services.steam import OwnedGame, SteamClient, SteamError, StoreHit

REFUSED = "Steam refused the API key. Check the key and try again."
NO_RESPONSE = "Steam didn't respond. Try again in a minute."
PRIVATE = (
    "Steam returned no games. In Steam, open your profile's Privacy Settings, "
    "set Game details to Public, then try again."
)


def test_resolve_steamid_forms(steam_client, fake_steam):
    fake_steam.vanity = {"sampleuser": "76561198000000002"}
    assert (
        steam_client.resolve_steamid(
            "k", "https://steamcommunity.com/profiles/76561198000000001/"
        )
        == "76561198000000001"
    )
    assert steam_client.resolve_steamid("k", "76561198000000001") == "76561198000000001"
    assert (
        steam_client.resolve_steamid("k", "https://steamcommunity.com/id/sampleuser/")
        == "76561198000000002"
    )
    with pytest.raises(
        SteamError, match="Couldn't find a Steam profile called 'nobody'"
    ):
        steam_client.resolve_steamid("k", "nobody")


def test_resolve_steamid_accepts_a_bare_vanity_name_and_stray_whitespace(
    steam_client, fake_steam
):
    fake_steam.vanity = {"sampleuser": "76561198000000002"}
    assert steam_client.resolve_steamid("k", "  sampleuser \n") == "76561198000000002"
    assert (
        steam_client.resolve_steamid("k", "steamcommunity.com/id/sampleuser?l=english")
        == "76561198000000002"
    )


def test_numeric_forms_do_not_call_steam(steam_client, fake_steam):
    steam_client.resolve_steamid("k", "76561198000000001")
    steam_client.resolve_steamid(
        "k", "https://steamcommunity.com/profiles/76561198000000001"
    )
    assert fake_steam.requests == []


def test_unknown_vanity_names_what_was_typed_not_the_url(steam_client):
    with pytest.raises(SteamError) as caught:
        steam_client.resolve_steamid("k", "https://steamcommunity.com/id/Nobody/")
    assert str(caught.value) == (
        "Couldn't find a Steam profile called 'Nobody'. "
        "Paste your profile link or SteamID64 and try again."
    )
    # The message holds user text, so it must stay a plain str for autoescaping.
    assert type(str(caught.value)) is str


def test_unknown_vanity_with_markup_is_kept_as_plain_text(steam_client):
    with pytest.raises(SteamError) as caught:
        steam_client.resolve_steamid("k", "<b>x</b>")
    assert "'<b>x</b>'" in str(caught.value)


def test_empty_input_is_an_unknown_name_without_asking_steam(steam_client, fake_steam):
    with pytest.raises(SteamError, match="Couldn't find a Steam profile called ''"):
        steam_client.resolve_steamid("k", "  ")
    assert fake_steam.requests == []


def test_resolve_steamid_sends_the_key_and_name_to_the_vanity_endpoint(
    steam_client, fake_steam
):
    fake_steam.vanity = {"sampleuser": "76561198000000002"}
    steam_client.resolve_steamid("sample-key", "sampleuser")
    request = fake_steam.requests[0]
    assert str(request.url).startswith(
        "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?"
    )
    assert request.url.params["key"] == "sample-key"
    assert request.url.params["vanityurl"] == "sampleuser"


@pytest.mark.parametrize(
    "failure,message",
    [
        (403, REFUSED),
        (401, REFUSED),
        (500, NO_RESPONSE),
        (503, NO_RESPONSE),
        (404, NO_RESPONSE),
        (httpx.ConnectTimeout("slow"), NO_RESPONSE),
        (httpx.ReadTimeout("slow"), NO_RESPONSE),
        (httpx.ConnectError("refused"), NO_RESPONSE),
    ],
)
def test_resolve_steamid_failures(steam_client, fake_steam, failure, message):
    fake_steam.fail_with(failure)
    with pytest.raises(SteamError, match=re.escape(message)):
        steam_client.resolve_steamid("k", "sampleuser")


@pytest.mark.parametrize(
    "failure,message",
    [
        (403, REFUSED),
        (401, REFUSED),
        (500, NO_RESPONSE),
        (503, NO_RESPONSE),
        (404, NO_RESPONSE),
        (httpx.ConnectTimeout("slow"), NO_RESPONSE),
        (httpx.ReadTimeout("slow"), NO_RESPONSE),
        (httpx.ConnectError("refused"), NO_RESPONSE),
    ],
)
def test_owned_games_failures(
    steam_client, fake_steam, failure, message
):  # Review focus 1
    fake_steam.fail_with(failure)
    with pytest.raises(SteamError, match=re.escape(message)):
        steam_client.owned_games("k", "76561198000000001")


def test_private_profile_message(steam_client, fake_steam):  # Review focus 1
    fake_steam.owned = []
    with pytest.raises(SteamError, match="set Game details to Public") as caught:
        steam_client.owned_games("k", "76561198000000001")
    assert str(caught.value) == PRIVATE


def test_error_messages_never_carry_the_key_or_the_url(steam_client, fake_steam):
    key = "SECRETKEY123"
    for failure in (401, 500, httpx.ConnectTimeout("slow")):
        fake_steam.fail_with(failure)
        with pytest.raises(SteamError) as caught:
            steam_client.owned_games(key, "76561198000000001")
        error = caught.value
        assert key not in str(error) and key not in repr(error)
        assert "steampowered" not in str(error)
        # Nothing chained underneath can leak it either.
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__


@pytest.mark.parametrize("body", ["not json", "[]", "{}", '{"response": []}'])
def test_unexpected_bodies_read_as_no_response(body):
    client = SteamClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
    )
    with pytest.raises(SteamError, match=re.escape(NO_RESPONSE)):
        client.owned_games("k", "76561198000000001")
    with pytest.raises(SteamError, match=re.escape(NO_RESPONSE)):
        client.resolve_steamid("k", "sampleuser")


def test_owned_games_parses_games(steam_client, fake_steam):
    fake_steam.owned = [
        {
            "appid": 10,
            "name": "Sample Quest",
            "playtime_forever": 600,
            "rtime_last_played": 1768000000,
        },
        {
            "appid": 20,
            "name": "Test Tactics",
            "playtime_forever": 120,
            "rtime_last_played": 0,
        },
        {"appid": 30, "name": "Demo Dungeon"},
    ]
    assert steam_client.owned_games("sample-key", "76561198000000001") == [
        OwnedGame(
            10,
            "Sample Quest",
            600,
            datetime.fromtimestamp(1768000000, tz=timezone.utc),
        ),
        OwnedGame(20, "Test Tactics", 120, None),
        OwnedGame(30, "Demo Dungeon", 0, None),
    ]
    assert steam_client.owned_games("k", "76561198000000001")[0].last_played_at.tzinfo


def test_owned_games_asks_for_names_and_free_games(steam_client, fake_steam):
    steam_client.owned_games("sample-key", "76561198000000001")
    request = fake_steam.requests[0]
    assert str(request.url).startswith(
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?"
    )
    assert dict(request.url.params) == {
        "key": "sample-key",
        "steamid": "76561198000000001",
        "include_appinfo": "1",
        "include_played_free_games": "1",
    }


def test_owned_games_skips_entries_without_an_app_id(steam_client, fake_steam):
    fake_steam.owned = [{"name": "No id"}, {"appid": 10, "name": "Sample Quest"}]
    assert [g.appid for g in steam_client.owned_games("k", "id")] == [10]


def test_owned_games_gives_a_nameless_game_a_placeholder_name(steam_client, fake_steam):
    fake_steam.owned = [{"appid": 77, "playtime_forever": 5}]
    (game,) = steam_client.owned_games("k", "id")
    assert game.name == "App 77"


def test_store_search(steam_client, fake_steam):
    fake_steam.store_hits = [{"id": 30, "name": "Demo Dungeon"}]
    assert steam_client.store_search("demo", "us") == [StoreHit(30, "Demo Dungeon")]


def test_store_search_sends_term_region_and_language(steam_client, fake_steam):
    steam_client.store_search("demo dungeon", "gb")
    request = fake_steam.requests[0]
    assert str(request.url).startswith(
        "https://store.steampowered.com/api/storesearch/?"
    )
    assert dict(request.url.params) == {
        "term": "demo dungeon",
        "cc": "gb",
        "l": "english",
    }


def test_store_search_with_no_hits_is_an_empty_list(steam_client, fake_steam):
    assert steam_client.store_search("zzz", "us") == []


@pytest.mark.parametrize(
    "failure",
    [403, 500, httpx.ConnectTimeout("slow"), httpx.ConnectError("refused")],
)
def test_store_search_failures(steam_client, fake_steam, failure):
    # The store takes no API key, so even a 403 is "didn't respond", never "refused the key".
    fake_steam.fail_with(failure)
    with pytest.raises(SteamError) as caught:
        steam_client.store_search("demo", "us")
    assert str(caught.value) == NO_RESPONSE
