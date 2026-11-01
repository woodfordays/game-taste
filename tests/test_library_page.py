"""The Library page: the list, the "Currently playing" switch, re-sync and adding a game."""

import html
import json
import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.models import CurrentlyPlayingChange, Game, PlaytimeSnapshot, Review
from app.services.game_facts import ensure_game
from app.services.library import (
    add_store_game,
    artwork_url,
    list_library,
    set_currently_playing,
    sync_library,
)
from app.services.settings import delete_setting, set_setting
from conftest import FIXED_NOW

HTMX = {"HX-Request": "true"}


def page(client, url: str = "/library") -> str:
    """The page as the user reads it: character references decoded."""
    return html.unescape(client.get(url).text)


def row(text: str, appid: int) -> str:
    found = re.search(rf'<li[^>]*id="game-{appid}".*?</li>', text, re.S)
    assert found, f"no row for app id {appid}"
    return found[0]


def names(db, **kwargs) -> list[str]:
    return [game.name for game in list_library(db, **kwargs)]


def make_game(db, appid: int, name: str, **fields) -> Game:
    """A game row that no Steam sync created, so each test says what makes it listed."""
    values = {
        "owned": False,
        "playtime_minutes": None,
        "last_played_at": None,
        "source": "other",
        "created_at": FIXED_NOW,
    }
    game = Game(appid=appid, name=name, **{**values, **fields})
    db.add(game)
    db.commit()
    return game


def review_of(db, appid: int) -> Review:
    review = Review(appid=appid, lane="solo", created_at=FIXED_NOW)
    db.add(review)
    db.commit()
    return review


def game_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Game))


# --- the brief's tests ------------------------------------------------------


def test_list_sort_and_search(onboarded_client, synced_games):
    text = onboarded_client.get("/library").text
    assert text.index("Sample Quest") < text.index(
        "Test Tactics"
    )  # 600 min before 120 min
    assert "Test Tactics" not in onboarded_client.get("/library?q=quest").text


def test_currently_playing_records_changes(onboarded_client, db, synced_games):
    r = onboarded_client.post("/games/10/playing", data={"playing": "1"})
    assert (
        r.headers["HX-Trigger"] == "card-updated-10"
        and db.get(Game, 10).currently_playing
    )
    onboarded_client.post("/games/10/playing", data={"playing": "1"})
    onboarded_client.post("/games/10/playing", data={"playing": "0"})
    assert [
        c.playing
        for c in db.scalars(
            select(CurrentlyPlayingChange).order_by(CurrentlyPlayingChange.id)
        )
    ] == [True, False]


def test_resync_failure_shows_message(
    onboarded_client, synced_games, fake_steam
):  # Review focus 1
    fake_steam.fail_with(httpx.ConnectTimeout("slow"))
    assert "Steam didn't respond. Try again in a minute." in html.unescape(
        onboarded_client.post("/library/sync").text
    )


def test_add_game_from_store(onboarded_client, db, fake_steam):
    fake_steam.store_hits = [{"id": 30, "name": "Demo Dungeon"}]
    assert "Demo Dungeon" in onboarded_client.get("/library/search?term=demo").text
    onboarded_client.post(
        "/library/games", data={"appid": "30", "name": "Demo Dungeon"}
    )
    assert (
        db.get(Game, 30).source == "store_search"
        and "Demo Dungeon" in onboarded_client.get("/library").text
    )


def test_without_steam(onboarded_client):
    assert (
        "Connect Steam in Settings to import your library."
        in onboarded_client.get("/library").text
    )


# --- list_library -----------------------------------------------------------


def test_sort_orders(db, synced_games):
    add_store_game(db, 30, "Demo Dungeon", FIXED_NOW)  # no playtime, never played
    db.get(Game, 20).last_played_at = FIXED_NOW - timedelta(
        days=30
    )  # older than Sample Quest
    db.commit()
    # Playtime, most first; a game with none recorded comes last.
    assert names(db) == names(db, sort="hours")
    assert names(db, sort="hours") == ["Sample Quest", "Test Tactics", "Demo Dungeon"]
    # Last played, latest first; a game never played comes last.
    assert names(db, sort="last_played") == [
        "Sample Quest",
        "Test Tactics",
        "Demo Dungeon",
    ]
    assert names(db, sort="name") == ["Demo Dungeon", "Sample Quest", "Test Tactics"]


def test_ties_fall_back_to_the_name(db):
    make_game(db, 20, "Test Tactics", owned=True, playtime_minutes=60)
    make_game(db, 10, "Sample Quest", owned=True, playtime_minutes=60)
    assert names(db, sort="hours") == ["Sample Quest", "Test Tactics"]


def test_name_sort_ignores_case(db):
    make_game(db, 10, "sample quest", owned=True)
    make_game(db, 20, "Test Tactics", owned=True)
    make_game(db, 30, "Demo Dungeon", owned=True)
    assert names(db, sort="name") == ["Demo Dungeon", "sample quest", "Test Tactics"]


def test_an_unknown_sort_is_an_error(db):
    with pytest.raises(ValueError):
        list_library(db, sort="bogus")


def test_search_is_a_case_insensitive_substring_match(db, synced_games):
    assert names(db, q="quest") == ["Sample Quest"]
    assert names(db, q="SAMPLE q") == ["Sample Quest"]
    assert names(db, q="ac") == ["Test Tactics"]
    assert names(db, q="zzz") == []
    assert names(db, q="") == ["Sample Quest", "Test Tactics"]


def test_search_ignores_spaces_around_the_term(db, synced_games):
    assert names(db, q="  quest ") == ["Sample Quest"]
    assert names(db, q="   ") == ["Sample Quest", "Test Tactics"]


def test_search_folds_case_beyond_ascii(db):
    make_game(db, 40, "Über Quest", owned=True)
    assert names(db, q="üBER") == ["Über Quest"]


def test_the_search_term_is_not_a_pattern(db, synced_games):
    # "%" and "_" would match everything if the term were handed to LIKE as typed.
    assert names(db, q="%") == []
    assert names(db, q="_") == []


def test_lists_owned_playing_and_reviewed_games_only(db):
    make_game(db, 10, "Sample Quest", owned=True)
    make_game(db, 20, "Test Tactics", currently_playing=True)
    make_game(db, 30, "Demo Dungeon")
    make_game(db, 40, "Other Game")
    review_of(db, 40)
    assert names(db, sort="name") == ["Other Game", "Sample Quest", "Test Tactics"]


def test_a_game_with_two_reviews_is_listed_once(db):
    make_game(db, 30, "Demo Dungeon")
    review_of(db, 30)
    db.add(Review(appid=30, lane="two", created_at=FIXED_NOW))
    db.commit()
    assert names(db) == ["Demo Dungeon"]


def test_a_game_added_by_name_is_listed_before_it_is_played_or_reviewed(db):
    add_store_game(db, 30, "Demo Dungeon", FIXED_NOW)
    assert names(db) == ["Demo Dungeon"]


def test_list_library_is_one_query(app, db, synced_games):
    # The "has a review" check is part of the query, not one lookup per game.
    make_game(db, 30, "Demo Dungeon")
    review_of(db, 30)
    statements = []

    def record(conn, cursor, statement, *rest):
        statements.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        assert len(list_library(db, q="e", sort="name")) == 3
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)
    assert len(statements) == 1


# --- set_currently_playing --------------------------------------------------


def changes(db) -> list[tuple[int, bool]]:
    db.expire_all()
    return [
        (c.appid, c.playing)
        for c in db.scalars(
            select(CurrentlyPlayingChange).order_by(CurrentlyPlayingChange.id)
        )
    ]


def test_turning_playing_on_and_off_adds_a_change_each_time(db, synced_games):
    assert set_currently_playing(db, 10, True, FIXED_NOW) is True
    assert db.get(Game, 10).currently_playing is True
    assert set_currently_playing(db, 10, False, FIXED_NOW) is True
    assert db.get(Game, 10).currently_playing is False
    assert changes(db) == [(10, True), (10, False)]


def test_setting_the_same_state_again_writes_nothing(db, synced_games):
    assert set_currently_playing(db, 10, False, FIXED_NOW) is False
    set_currently_playing(db, 10, True, FIXED_NOW)
    assert set_currently_playing(db, 10, True, FIXED_NOW) is False
    assert changes(db) == [(10, True)]


def test_a_change_records_when_it_happened(db, synced_games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    assert db.scalar(select(CurrentlyPlayingChange.changed_at)) == FIXED_NOW


def test_switching_one_game_leaves_the_others_alone(db, synced_games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    assert db.get(Game, 20).currently_playing is False
    assert changes(db) == [(10, True)]


def test_an_unknown_game_cannot_be_switched(db):
    with pytest.raises(LookupError):
        set_currently_playing(db, 999, True, FIXED_NOW)
    assert changes(db) == []


# --- add_store_game ---------------------------------------------------------


def test_add_store_game_creates_an_unowned_game_with_no_playtime(db):
    add_store_game(db, 30, "Demo Dungeon", FIXED_NOW)
    db.expire_all()
    stored = db.get(Game, 30)
    assert (
        stored.name,
        stored.owned,
        stored.source,
        stored.playtime_minutes,
        stored.last_played_at,
        stored.currently_playing,
        stored.created_at,
    ) == ("Demo Dungeon", False, "store_search", None, None, False, FIXED_NOW)


def test_add_store_game_is_idempotent(db):
    first = add_store_game(db, 30, "Demo Dungeon", FIXED_NOW)
    second = add_store_game(db, 30, "A different name", FIXED_NOW + timedelta(days=1))
    assert second.appid == first.appid
    db.expire_all()
    assert game_count(db) == 1
    kept = db.get(Game, 30)
    assert kept.name == "Demo Dungeon" and kept.created_at == FIXED_NOW


def test_add_store_game_leaves_an_owned_game_alone(db, synced_games):
    game = add_store_game(db, 10, "Sample Quest (store)", FIXED_NOW)
    db.expire_all()
    assert (game.owned, game.source, game.playtime_minutes) == (
        True,
        "steam_library",
        600,
    )
    assert db.get(Game, 10).name == "Sample Quest"


def test_owning_an_added_game_records_its_first_snapshot(db, steam_client, fake_steam):
    add_store_game(db, 30, "Demo Dungeon", FIXED_NOW)
    fake_steam.owned = [
        {
            "appid": 30,
            "name": "Demo Dungeon",
            "playtime_forever": 45,
            "rtime_last_played": 0,
        }
    ]
    assert db.scalar(select(func.count()).select_from(PlaytimeSnapshot)) == 0
    sync_library(db, steam_client, "steam-sample", "76561198000000001", FIXED_NOW)
    db.expire_all()
    game = db.get(Game, 30)
    assert (game.owned, game.source, game.playtime_minutes) == (
        True,
        "steam_library",
        45,
    )
    assert db.scalar(select(PlaytimeSnapshot.playtime_minutes)) == 45


# --- the page ---------------------------------------------------------------


def test_page_lists_each_game_with_its_details(onboarded_client, synced_games):
    text = page(onboarded_client)
    quest = row(text, 10)
    assert artwork_url(10) in quest
    assert "Sample Quest" in quest and "10.0 h" in quest
    assert (
        "Jan 9, 11:06 PM" in quest
    )  # last played 2026-01-09 23:06 UTC; the zone is UTC
    tactics = row(text, 20)
    assert (
        "Test Tactics" in tactics and "2.0 h" in tactics and "Never played" in tactics
    )


def test_page_is_the_library_section_with_a_count(onboarded_client, synced_games):
    text = page(onboarded_client)
    assert '<h1 class="text-headline">Library</h1>' in text
    assert 'href="/library" class="em-sidenav__item text-label is-current"' in text
    assert "<title>Library - Game Taste</title>" in text
    assert "2 games" in text
    assert "1 game<" in page(onboarded_client, "/library?q=quest")


def test_each_row_has_a_labelled_switch(onboarded_client, synced_games):
    quest = row(page(onboarded_client), 10)
    switch = re.search(r"<input[^>]*role=\"switch\"[^>]*>", quest)[0]
    assert 'type="checkbox"' in switch and "em-switch__track" in switch
    assert "checked" not in switch
    assert re.search(
        r'<label[^>]*for="playing-10"[^>]*>\s*Currently playing\s*</label>', quest
    )
    assert 'id="playing-10"' in switch
    assert 'hx-post="/games/10/playing"' in switch


def test_the_switch_sends_the_opposite_of_what_it_shows(
    onboarded_client, db, synced_games
):
    def next_value(appid: int) -> str:
        switch = re.search(
            r"<input[^>]*role=\"switch\"[^>]*>", row(page(onboarded_client), appid)
        )[0]
        return json.loads(re.search(r"hx-vals='([^']*)'", switch)[1])["playing"]

    assert next_value(10) == "1"
    set_currently_playing(db, 10, True, FIXED_NOW)
    text = page(onboarded_client)
    switch = re.search(r"<input[^>]*role=\"switch\"[^>]*>", row(text, 10))[0]
    assert "checked" in switch and next_value(10) == "0"
    assert next_value(20) == "1"


def test_each_row_has_a_secondary_button_that_opens_the_review_dialog(
    onboarded_client, synced_games
):
    quest = row(page(onboarded_client), 10)
    button = re.search(
        r"<button[^>]*hx-get=\"/games/10/review\"[^>]*>.*?</button>", quest, re.S
    )[0]
    assert 'hx-target="#dialog"' in button
    assert "em-btn--secondary" in button and "Write a review" in button
    assert 'aria-label="Write a review for Sample Quest"' in button


def test_the_page_has_at_most_one_primary_button(onboarded_client, synced_games):
    assert page(onboarded_client).count("em-btn--primary") <= 1


def test_page_orders_by_playtime_by_default_and_by_the_chosen_sort(
    onboarded_client, synced_games
):
    def order(url: str) -> list[int]:
        text = onboarded_client.get(url).text
        return sorted((10, 20), key=lambda appid: text.index(f'id="game-{appid}"'))

    assert order("/library") == [10, 20]
    assert order("/library?sort=name") == [10, 20]
    assert order("/library?sort=last_played") == [10, 20]
    # An unknown sort is not an error page: it falls back to playtime.
    assert onboarded_client.get("/library?sort=bogus").status_code == 200
    assert order("/library?sort=bogus") == [10, 20]


def test_filters_show_what_was_asked_for(onboarded_client, synced_games):
    text = page(onboarded_client, "/library?q=quest&sort=name")
    assert 'value="quest"' in text
    assert re.search(r'<option value="name"\s+selected>', text)
    assert not re.search(r'<option value="hours"\s+selected>', text)
    assert re.search(r'<option value="hours"\s+selected>', page(onboarded_client))


def test_search_term_is_escaped(onboarded_client, synced_games):
    text = onboarded_client.get(
        "/library", params={"q": "<script>alert(1)</script>"}
    ).text
    assert "<script>alert(1)" not in text and "&lt;script&gt;alert(1)" in text


def test_a_search_with_no_match_says_so(onboarded_client, synced_games):
    text = page(onboarded_client, "/library?q=zzz")
    assert 'No games match "zzz"' in text
    assert "game-10" not in text


def test_an_empty_library_says_what_will_appear(onboarded_client):
    text = page(onboarded_client)
    assert "No games yet" in text and 'id="library-list"' in text


def test_htmx_search_swaps_only_the_list(onboarded_client, synced_games):
    r = onboarded_client.get("/library?q=quest", headers=HTMX)
    assert r.status_code == 200
    assert "<html" not in r.text and 'id="library-list"' in r.text
    assert "Sample Quest" in r.text and "Test Tactics" not in r.text


def test_filters_are_applied_as_you_type_and_carried_by_the_other_buttons(
    onboarded_client, synced_games
):
    text = page(onboarded_client)
    form = re.search(r"<form[^>]*id=\"library-filters\"[^>]*>", text)[0]
    assert 'hx-get="/library"' in form and 'hx-target="#library-list"' in form
    for name in ("q", "sort"):
        assert f'name="{name}"' in text
    resync = re.search(r"<button[^>]*hx-post=\"/library/sync\"[^>]*>", text)[0]
    assert 'hx-include="#library-filters"' in resync
    assert 'hx-target="#library-list"' in resync


# --- the page without Steam -------------------------------------------------


def test_without_steam_the_page_says_how_to_connect_and_still_offers_add(
    onboarded_client,
):
    text = page(onboarded_client)
    assert "Connect Steam in Settings to import your library." in text
    assert "/library/sync" not in text and "Re-sync library" not in text
    assert "Add a game" in text and 'hx-get="/library/search"' in text


def test_with_steam_the_page_offers_resync_and_no_hint(onboarded_client, synced_games):
    text = page(onboarded_client)
    assert "Re-sync library" in text and 'hx-post="/library/sync"' in text
    assert "Connect Steam in Settings" not in text
    assert "Add a game" in text


def test_an_unreadable_steam_key_counts_as_not_connected(
    onboarded_client, db, synced_games, fake_steam
):
    # A value saved as plain text can't be decrypted, like a key from another key file.
    set_setting(db, "steam_api_key", "not-encrypted")
    requests_before = len(fake_steam.requests)
    text = page(onboarded_client)
    assert "Connect Steam in Settings to import your library." in text
    assert "Re-sync library" not in text
    assert "Sample Quest" in text  # the games already imported stay listed
    r = onboarded_client.post("/library/sync")
    assert r.status_code == 200
    assert "Connect Steam in Settings to import your library." in html.unescape(r.text)
    assert len(fake_steam.requests) == requests_before


def test_a_missing_steam_id_counts_as_not_connected(onboarded_client, db, synced_games):
    delete_setting(db, "steam_id")
    db.commit()
    assert "Re-sync library" not in page(onboarded_client)


# --- the switch -------------------------------------------------------------


def test_switching_returns_the_row_and_tells_the_cards(
    onboarded_client, db, synced_games
):
    r = onboarded_client.post("/games/10/playing", data={"playing": "1"})
    assert r.status_code == 200 and r.headers["HX-Trigger"] == "card-updated-10"
    assert (
        "<html" not in r.text
        and 'id="game-10"' in r.text
        and 'id="game-20"' not in r.text
    )
    assert re.search(r"<input[^>]*role=\"switch\"[^>]*checked", r.text)
    off = onboarded_client.post("/games/10/playing", data={"playing": "0"})
    assert not re.search(r"<input[^>]*role=\"switch\"[^>]*checked", off.text)


def test_switching_to_the_same_state_still_answers_with_the_row(
    onboarded_client, db, synced_games
):
    r = onboarded_client.post("/games/10/playing", data={"playing": "0"})
    assert r.status_code == 200 and 'id="game-10"' in r.text
    assert r.headers["HX-Trigger"] == "card-updated-10"
    assert changes(db) == []


@pytest.mark.parametrize(
    "data", [{}, {"playing": ""}, {"playing": "yes"}, {"playing": "2"}]
)
def test_a_switch_request_without_a_clear_state_changes_nothing(
    onboarded_client, db, synced_games, data
):
    r = onboarded_client.post("/games/10/playing", data=data)
    assert r.status_code == 400
    assert changes(db) == [] and db.get(Game, 10).currently_playing is False


def test_switching_an_unknown_game_is_not_found(onboarded_client, db):
    r = onboarded_client.post("/games/999/playing", data={"playing": "1"})
    assert r.status_code == 404 and changes(db) == []


@pytest.mark.parametrize("appid", ["0", "-1", "99999999999999999999"])
def test_switching_an_impossible_app_id_is_refused_not_a_server_error(
    onboarded_client, db, appid
):
    r = onboarded_client.post(f"/games/{appid}/playing", data={"playing": "1"})
    assert r.status_code == 422 and changes(db) == []


def test_a_game_played_but_not_owned_stays_listed_when_switched_on(
    onboarded_client, db
):
    make_game(db, 30, "Demo Dungeon")
    assert "Demo Dungeon" not in onboarded_client.get("/library").text
    onboarded_client.post("/games/30/playing", data={"playing": "1"})
    assert "Demo Dungeon" in onboarded_client.get("/library").text


# --- re-sync ----------------------------------------------------------------


def test_resync_reports_what_it_added(onboarded_client, db, synced_games, fake_steam):
    fake_steam.owned.append(
        {
            "appid": 30,
            "name": "Demo Dungeon",
            "playtime_forever": 30,
            "rtime_last_played": 0,
        }
    )
    r = onboarded_client.post("/library/sync")
    text = html.unescape(r.text)
    assert r.status_code == 200
    assert "Library synced: 1 new, 3 total." in text
    assert row(text, 30) and db.get(Game, 30).owned
    assert "em-toast--success" in text and "em-toast--danger" not in text


def test_resync_with_nothing_new_says_so(onboarded_client, synced_games):
    text = html.unescape(onboarded_client.post("/library/sync").text)
    assert "Library synced: 0 new, 2 total." in text


def test_resync_sends_the_saved_key_and_id(onboarded_client, synced_games, fake_steam):
    before = len(fake_steam.requests)
    onboarded_client.post("/library/sync")
    owned = [
        r
        for r in fake_steam.requests[before:]
        if r.url.path.endswith("GetOwnedGames/v1/")
    ]
    assert len(owned) == 1
    assert owned[0].url.params["key"] == "steam-sample"
    assert owned[0].url.params["steamid"] == "76561198000000001"


def test_a_full_page_resync_puts_the_toast_in_the_toast_region(
    onboarded_client, synced_games
):
    text = html.unescape(onboarded_client.post("/library/sync").text)
    assert "<html" in text and "<h1" in text
    region = re.search(r'<div id="toasts".*?</div>\s*</section>', text, re.S)[0]
    assert "Library synced: 0 new, 2 total." in region


def test_an_htmx_resync_swaps_the_list_and_appends_the_toast(
    onboarded_client, synced_games
):
    r = onboarded_client.post("/library/sync", headers=HTMX)
    assert r.status_code == 200 and "<html" not in r.text
    assert 'id="library-list"' in r.text
    assert 'hx-swap-oob="beforeend:#toasts"' in r.text
    assert r.text.index('id="library-list"') < r.text.index(
        'hx-swap-oob="beforeend:#toasts"'
    )
    assert "Library synced: 0 new, 2 total." in r.text


def test_the_toast_is_an_ember_toast_that_can_be_dismissed(
    onboarded_client, synced_games
):
    toast = re.search(
        r'<div class="em-toast[^"]*".*?</button>',
        onboarded_client.post("/library/sync", headers=HTMX).text,
        re.S,
    )[0]
    assert "em-toast--success" in toast and "em-toast__title" in toast
    assert 'aria-label="Dismiss"' in toast and "em-toast__close" in toast


def break_steam(fake_steam, how: str) -> None:
    if how == "private profile":
        fake_steam.owned = []
    elif how == "refused key":
        fake_steam.fail_with(403)
    elif how == "timeout":
        fake_steam.fail_with(httpx.ConnectTimeout("slow"))
    elif how == "server error":
        fake_steam.fail_with(503)


RESYNC_FAILURES = [
    ("private profile", "set Game details to Public"),
    ("refused key", "Steam refused the API key. Check the key and try again."),
    ("timeout", "Steam didn't respond. Try again in a minute."),
    ("server error", "Steam didn't respond. Try again in a minute."),
]


@pytest.mark.parametrize(
    "how, message", RESYNC_FAILURES, ids=[f[0] for f in RESYNC_FAILURES]
)
def test_every_resync_failure_is_explained_and_keeps_the_list(
    onboarded_client, db, synced_games, fake_steam, how, message
):  # Review focus 1
    break_steam(fake_steam, how)
    for headers in ({}, HTMX):
        r = onboarded_client.post("/library/sync", headers=headers)
        text = html.unescape(r.text)
        assert r.status_code == 200
        assert message in text
        assert "em-toast--danger" in text and "em-toast--success" not in text
        assert "Library synced" not in text
        # The list is still there and retry is still possible.
        assert row(text, 10) and row(text, 20)
        if not headers:
            assert "Re-sync library" in text
    assert game_count(db) == 2


def test_a_resync_failure_never_shows_the_key(
    onboarded_client, synced_games, fake_steam
):
    fake_steam.fail_with(403)
    assert "steam-sample" not in onboarded_client.post("/library/sync").text


def test_a_failed_resync_toast_stays_until_dismissed(
    onboarded_client, synced_games, fake_steam
):
    fake_steam.fail_with(503)
    failed = onboarded_client.post("/library/sync", headers=HTMX).text
    assert "setTimeout" not in failed
    fake_steam.fail_with(None)
    assert "setTimeout" in onboarded_client.post("/library/sync", headers=HTMX).text


def test_resync_keeps_the_filter(onboarded_client, synced_games):
    r = onboarded_client.post(
        "/library/sync", data={"q": "quest", "sort": "name"}, headers=HTMX
    )
    assert "Sample Quest" in r.text and "Test Tactics" not in r.text


def test_resync_without_steam_asks_to_connect_and_calls_nothing(
    onboarded_client, fake_steam
):
    r = onboarded_client.post("/library/sync", headers=HTMX)
    assert r.status_code == 200
    assert "Connect Steam in Settings to import your library." in r.text
    assert "em-toast--danger" in r.text
    assert fake_steam.requests == []


# --- searching the store ----------------------------------------------------


def test_search_lists_each_hit_with_an_add_button(onboarded_client, fake_steam):
    fake_steam.store_hits = [
        {"id": 30, "name": "Demo Dungeon"},
        {"id": 31, "name": "Demo Dungeon 2"},
    ]
    r = onboarded_client.get("/library/search?term=demo")
    text = html.unescape(r.text)
    assert r.status_code == 200 and "<html" not in text
    assert "Demo Dungeon 2" in text
    forms = re.findall(r"<form[^>]*hx-post=\"/library/games\".*?</form>", text, re.S)
    assert len(forms) == 2
    assert (
        'name="appid" value="30"' in forms[0]
        and 'name="name" value="Demo Dungeon"' in forms[0]
    )
    assert "Add" in forms[0] and "em-btn--secondary" in forms[0]
    assert "em-btn--primary" not in text


def test_search_sends_the_term_and_the_store_region(onboarded_client, db, fake_steam):
    onboarded_client.get("/library/search", params={"term": "  demo dungeon "})
    request = fake_steam.requests[-1]
    assert (
        request.url.params["term"] == "demo dungeon"
        and request.url.params["cc"] == "us"
    )
    set_setting(db, "store_region", "gb")
    onboarded_client.get("/library/search?term=demo")
    assert fake_steam.requests[-1].url.params["cc"] == "gb"


def test_search_marks_games_already_in_the_library(
    onboarded_client, synced_games, fake_steam
):
    fake_steam.store_hits = [
        {"id": 10, "name": "Sample Quest"},
        {"id": 30, "name": "Demo Dungeon"},
    ]
    text = html.unescape(onboarded_client.get("/library/search?term=a").text)
    assert text.count("In your library") == 1
    forms = re.findall(r"<form[^>]*hx-post=\"/library/games\".*?</form>", text, re.S)
    assert len(forms) == 1 and 'value="30"' in forms[0]


def test_search_with_no_hits_says_what_to_try(onboarded_client, fake_steam):
    text = html.unescape(onboarded_client.get("/library/search?term=zzzz").text)
    assert 'No games found for "zzzz".' in text
    assert "Check the spelling" in text and "hx-post" not in text


@pytest.mark.parametrize(
    "url", ["/library/search", "/library/search?term=", "/library/search?term=%20%20"]
)
def test_search_without_a_term_asks_for_one_and_calls_nothing(
    onboarded_client, fake_steam, url
):
    r = onboarded_client.get(url)
    assert r.status_code == 200 and "Enter a game name to search." in r.text
    assert fake_steam.requests == []


@pytest.mark.parametrize(
    "failure", [503, httpx.ConnectTimeout("slow")], ids=["server error", "timeout"]
)
def test_search_failure_is_explained(
    onboarded_client, fake_steam, failure
):  # Review focus 1
    fake_steam.fail_with(failure)
    r = onboarded_client.get("/library/search?term=demo")
    assert r.status_code == 200
    assert "Steam didn't respond. Try again in a minute." in html.unescape(r.text)
    assert 'role="alert"' in r.text


def test_search_escapes_what_it_shows(onboarded_client, fake_steam):
    fake_steam.store_hits = [
        {"id": 30, "name": '<img src=x onerror=alert(1)> "Dungeon"'}
    ]
    text = onboarded_client.get("/library/search?term=demo").text
    assert "<img src=x" not in text and "&lt;img src=x" in text
    # The term is quoted back when nothing matches it.
    fake_steam.store_hits = []
    none = onboarded_client.get("/library/search", params={"term": "<b>zzz</b>"}).text
    assert "<b>zzz" not in none and "&lt;b&gt;zzz" in none


def test_a_game_only_research_knows_can_still_be_added(
    onboarded_client, fake_steam, db
):
    ensure_game(db, 30, "Demo Dungeon", FIXED_NOW)
    ensure_game(db, 31, "Demo Dungeon 2", FIXED_NOW)
    add_store_game(db, 31, "Demo Dungeon 2", FIXED_NOW)
    fake_steam.store_hits = [
        {"id": 30, "name": "Demo Dungeon"},
        {"id": 31, "name": "Demo Dungeon 2"},
    ]
    text = html.unescape(onboarded_client.get("/library/search?term=demo").text)
    hits = re.findall(r'<li id="store-hit-\d+".*?</li>', text, re.S)
    assert len(hits) == 2
    assert 'name="appid" value="30"' in hits[0] and "In your library" not in hits[0]
    assert "In your library" in hits[1] and "<form" not in hits[1]


# --- adding a game ----------------------------------------------------------


def test_adding_a_game_lists_it_and_says_so(onboarded_client, db):
    r = onboarded_client.post(
        "/library/games", data={"appid": "30", "name": "Demo Dungeon"}
    )
    text = html.unescape(r.text)
    assert r.status_code == 200
    assert "Added Demo Dungeon to your library." in text and "em-toast--success" in text
    assert row(text, 30) and "Demo Dungeon" in row(text, 30)
    game = db.get(Game, 30)
    assert (game.name, game.owned, game.source, game.playtime_minutes) == (
        "Demo Dungeon",
        False,
        "store_search",
        None,
    )


def test_adding_a_game_only_research_knows_puts_it_in_the_library(
    onboarded_client, fake_steam, db
):
    ensure_game(db, 30, "Demo Dungeon", FIXED_NOW)
    assert "Demo Dungeon" not in page(onboarded_client)
    r = onboarded_client.post(
        "/library/games", data={"appid": "30", "name": "Demo Dungeon"}
    )
    text = html.unescape(r.text)
    assert "Added Demo Dungeon to your library." in text
    assert "already in your library" not in text
    assert row(text, 30) and "Demo Dungeon" in row(text, 30)
    db.expire_all()
    assert db.get(Game, 30).source == "store_search"
    assert names(db) == ["Demo Dungeon"]
    # And now the search shows it as added.
    fake_steam.store_hits = [{"id": 30, "name": "Demo Dungeon"}]
    assert "In your library" in onboarded_client.get("/library/search?term=demo").text


def test_an_added_game_is_marked_as_not_owned(onboarded_client, synced_games):
    onboarded_client.post(
        "/library/games", data={"appid": "30", "name": "Demo Dungeon"}
    )
    text = page(onboarded_client)
    assert "Not owned" in row(text, 30)
    assert "Not owned" not in row(text, 10)


def test_adding_a_game_twice_adds_it_once(onboarded_client, db):
    data = {"appid": "30", "name": "Demo Dungeon"}
    onboarded_client.post("/library/games", data=data)
    again = html.unescape(onboarded_client.post("/library/games", data=data).text)
    assert "Demo Dungeon is already in your library." in again
    assert game_count(db) == 1


def test_adding_a_game_you_own_changes_nothing(onboarded_client, db, synced_games):
    r = onboarded_client.post(
        "/library/games", data={"appid": "10", "name": "Sample Quest"}
    )
    assert "Sample Quest is already in your library." in html.unescape(r.text)
    db.expire_all()
    game = db.get(Game, 10)
    assert (game.owned, game.source, game.playtime_minutes) == (
        True,
        "steam_library",
        600,
    )


def test_an_htmx_add_swaps_the_list_and_appends_the_toast(onboarded_client):
    r = onboarded_client.post(
        "/library/games", data={"appid": "30", "name": "Demo Dungeon"}, headers=HTMX
    )
    assert r.status_code == 200 and "<html" not in r.text
    assert 'id="library-list"' in r.text and 'hx-swap-oob="beforeend:#toasts"' in r.text
    assert "Added Demo Dungeon to your library." in r.text


def test_adding_keeps_the_filter(onboarded_client, synced_games):
    r = onboarded_client.post(
        "/library/games",
        data={"appid": "30", "name": "Demo Dungeon", "q": "quest"},
        headers=HTMX,
    )
    assert "Sample Quest" in r.text and 'id="game-30"' not in r.text


def test_the_added_name_is_escaped(onboarded_client):
    name = "<script>alert(1)</script>"
    r = onboarded_client.post("/library/games", data={"appid": "30", "name": name})
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)" in r.text


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"appid": "30"},
        {"appid": "30", "name": ""},
        {"appid": "30", "name": "   "},
        {"name": "Demo Dungeon"},
        {"appid": "abc", "name": "Demo Dungeon"},
        {"appid": "0", "name": "Demo Dungeon"},
        {"appid": "-5", "name": "Demo Dungeon"},
        {"appid": "", "name": "Demo Dungeon"},
    ],
)
def test_an_incomplete_add_saves_nothing_and_says_what_to_do(
    onboarded_client, db, data
):
    r = onboarded_client.post("/library/games", data=data, headers=HTMX)
    assert r.status_code == 200
    assert (
        "Couldn't add that game. Search for it again and pick it from the results."
        in html.unescape(r.text)
    )
    assert "em-toast--danger" in r.text
    assert game_count(db) == 0


# --- guard ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, url",
    [
        ("get", "/library"),
        ("get", "/library/search?term=demo"),
        ("post", "/library/sync"),
        ("post", "/library/games"),
        ("post", "/games/10/playing"),
    ],
)
def test_the_library_routes_need_a_login(onboarded_client, app, method, url):
    fresh = TestClient(app)
    r = getattr(fresh, method)(url, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = getattr(fresh, method)(url, headers=HTMX, follow_redirects=False)
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/login"


def test_an_htmx_add_turns_the_search_result_into_in_your_library(onboarded_client):
    r = onboarded_client.post(
        "/library/games", data={"appid": "30", "name": "Demo Dungeon"}, headers=HTMX
    )
    result = re.search(r'<li id="store-hit-30"[^>]*hx-swap-oob="true".*?</li>', r.text, re.S)[0]
    assert "In your library" in result and "Demo Dungeon" in result
    assert "<form" not in result and "<button" not in result


def test_a_full_page_add_has_no_out_of_band_result_row(onboarded_client):
    text = onboarded_client.post(
        "/library/games", data={"appid": "30", "name": "Demo Dungeon"}
    ).text
    assert "store-hit-30" not in text and "hx-swap-oob" not in text


def test_a_failed_add_leaves_the_search_results_alone(onboarded_client):
    r = onboarded_client.post("/library/games", data={"appid": "x", "name": "Y"}, headers=HTMX)
    assert "store-hit-" not in r.text
