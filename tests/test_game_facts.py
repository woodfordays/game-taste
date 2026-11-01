from datetime import timedelta

import httpx
import pytest
from fakes import FIXED_NOW, T1, T2, facts_for, save_facts, store_data
from sqlalchemy import event, func, select

from app.models import Game, GameFact
from app.services.game_facts import (
    FACTS_MAX_AGE,
    MAX_STEAM_FAILURES_IN_A_ROW,
    NEWS,
    PLAYERS,
    REVIEWS,
    SOURCE_RESEARCH,
    STORE,
    Fact,
    FactsRefresh,
    GameFacts,
    GameVersion,
    current_facts,
    ensure_game,
    refresh_store_facts,
    save_player_counts,
    store_fact_data,
)
from app.services.library import add_store_game, list_library, sync_library
from app.services.steam import SteamClient, parse_app_details

NO_RESPONSE = "Steam didn't respond. Try again in a minute."


def scripted_steam(fake_steam, outcomes):
    """A SteamClient whose requests succeed or fail as `outcomes` says, one True or False
    per request, in order. Requests past the end succeed. Successes are answered by
    `fake_steam`, and every request is recorded in `fake_steam.requests`."""
    pending = iter(outcomes)

    def handler(request):
        if next(pending, True):
            return fake_steam.handler(request)
        fake_steam.requests.append(request)
        return httpx.Response(500)

    return SteamClient(httpx.Client(transport=httpx.MockTransport(handler)))


def test_facts_are_fetched_once_a_week(db, steam_client, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest")
    assert refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW) == FactsRefresh(
        fetched=3
    )
    calls = len(fake_steam.requests)
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW + timedelta(days=6))
    assert len(fake_steam.requests) == calls
    refresh_store_facts(
        db, steam_client, [10], "us", FIXED_NOW + timedelta(days=7, seconds=1)
    )
    assert len(fake_steam.requests) == calls + 3


def test_snapshots_are_kept_and_the_latest_is_current(
    db, steam_client, fake_steam, games
):
    fake_steam.details[10] = store_data("Sample Quest", early_access=True)
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW)
    fake_steam.details[10] = store_data("Sample Quest", early_access=False)
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW + timedelta(days=8))
    facts = current_facts(db, [10])[10]
    assert facts.early_access is False
    assert facts.store.fetched_at == FIXED_NOW + timedelta(days=8)
    assert (
        db.scalar(
            select(func.count()).select_from(GameFact).where(GameFact.kind == "store")
        )
        == 2
    )


def test_an_unavailable_app_is_recorded_and_is_not_a_game(
    db, steam_client, games
):  # Review focus 2
    assert refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW).fetched == 1
    assert current_facts(db, [10])[10].is_game is False


def test_a_failure_keeps_the_old_facts_and_says_why(
    db, steam_client, fake_steam, games
):  # Review focus 2
    fake_steam.details[10] = store_data("Sample Quest")
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW)
    fake_steam.fail_with(500)
    result = refresh_store_facts(
        db, steam_client, [10], "us", FIXED_NOW + timedelta(days=8)
    )
    assert result.problems == {10: "Steam didn't respond. Try again in a minute."}
    assert current_facts(db, [10])[10].store.fetched_at == FIXED_NOW


def test_steam_down_costs_three_calls(db, steam_client, fake_steam):  # Review focus 2
    for appid in range(100, 110):
        ensure_game(db, appid, f"Sample {appid}", FIXED_NOW)
    fake_steam.fail_with(500)
    result = refresh_store_facts(db, steam_client, range(100, 110), "us", FIXED_NOW)
    assert len(fake_steam.requests) == 3 and set(result.problems) == set(
        range(100, 110)
    )


@pytest.mark.parametrize(
    "earlier,now,updated",
    [
        (GameVersion(True, None), GameVersion(False, None), True),
        (GameVersion(False, T1), GameVersion(False, T2), True),
        (GameVersion(False, T2), GameVersion(False, T2), False),
        (GameVersion(None, None), GameVersion(False, None), False),
        (GameVersion(False, None), GameVersion(False, T1), True),
    ],
)
def test_version_updates(earlier, now, updated):
    assert now.updated_since(earlier) is updated
    assert GameVersion.from_json(now.as_json()) == now


def test_price_and_review_text(db, steam_client, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest", price_cents=1999)
    fake_steam.review_summaries[10] = {
        "review_score_desc": "Very Positive",
        "total_positive": 1200,
        "total_negative": 34,
        "total_reviews": 1234,
    }
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW)
    facts = current_facts(db, [10])[10]
    assert (
        facts.price_text == "$19.99"
        and facts.review_text == "Very Positive (1,234 reviews)"
    )


def test_research_games_stay_out_of_the_library_until_added(db):
    ensure_game(db, 30, "Demo Dungeon", FIXED_NOW)
    assert list_library(db) == []
    add_store_game(db, 30, "Demo Dungeon", FIXED_NOW)
    assert [g.appid for g in list_library(db)] == [30]


# Beyond the brief's tests: the rest of the contract.


def test_ensure_game_adds_a_research_game(db):
    ensure_game(db, 30, "Demo Dungeon", FIXED_NOW)
    db.expire_all()
    game = db.get(Game, 30)
    assert (game.name, game.owned, game.playtime_minutes, game.source) == (
        "Demo Dungeon",
        False,
        None,
        SOURCE_RESEARCH,
    )
    assert game.created_at == FIXED_NOW and game.currently_playing is False


def test_ensure_game_returns_a_known_game_as_it_is(db, games):
    returned = ensure_game(db, 10, "Another Name", FIXED_NOW + timedelta(days=1))
    db.expire_all()
    game = db.get(Game, 10)
    assert returned is game
    assert (game.name, game.owned, game.source) == (
        "Sample Quest",
        True,
        "steam_library",
    )


def test_adding_a_known_library_game_changes_nothing(db, games):
    add_store_game(db, 10, "Sample Quest", FIXED_NOW)
    assert db.get(Game, 10).source == "steam_library"


def test_adding_a_research_game_keeps_its_name_and_owner_state(db):
    ensure_game(db, 30, "Demo Dungeon", FIXED_NOW)
    game = add_store_game(db, 30, "Another Name", FIXED_NOW + timedelta(days=1))
    assert (game.name, game.owned, game.source) == (
        "Demo Dungeon",
        False,
        "store_search",
    )


def test_a_syncing_library_takes_over_a_research_game(db, steam_client, fake_steam):
    ensure_game(db, 10, "Sample Quest", FIXED_NOW)
    sync_library(db, steam_client, "key", "76561198000000001", FIXED_NOW)
    assert db.get(Game, 10).source == "steam_library" and db.get(Game, 10).owned


def test_store_fact_data_drops_the_appid_and_adds_region():
    details = parse_app_details(
        10, {"10": {"success": True, "data": store_data("Sample Quest")}}
    )
    data = store_fact_data(details, "gb")
    assert "appid" not in data
    assert data["available"] is True and data["region"] == "gb"
    assert data["name"] == "Sample Quest" and data["categories"] == ["Single-player"]
    assert store_fact_data(None, "gb") == {"available": False, "region": "gb"}


def test_current_facts_has_an_entry_for_every_game_and_takes_one_query(app, db, games):
    save_facts(db, 10, "Sample Quest", players=((2, 4), None))
    save_facts(db, 20, "Test Tactics", fetched_at=FIXED_NOW - timedelta(days=3))
    save_facts(db, 10, "Sample Quest", fetched_at=FIXED_NOW + timedelta(days=1))
    statements = []

    def listener(conn, cursor, statement, *rest):
        statements.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", listener)
    try:
        found = current_facts(db, iter([10, 20, 99, 10]))
    finally:
        event.remove(app.state.engine, "before_cursor_execute", listener)
    assert len(statements) == 1
    assert set(found) == {10, 20, 99}
    assert found[99] == GameFacts(99)
    assert found[10].store.fetched_at == FIXED_NOW + timedelta(days=1)
    assert found[10].players.fetched_at == FIXED_NOW
    assert found[20].store.fetched_at == FIXED_NOW - timedelta(days=3)
    assert found[20].players is None


def test_current_facts_of_nothing_is_empty(db):
    assert current_facts(db, []) == {}


def test_the_later_row_wins_when_two_snapshots_share_a_time(db, games):
    for early_access in (True, False):
        db.add(
            GameFact(
                appid=10,
                kind=STORE,
                data={"available": True, "early_access": early_access},
                source_url=None,
                fetched_at=FIXED_NOW,
            )
        )
    db.commit()
    assert current_facts(db, [10])[10].early_access is False


def test_saved_facts_read_back_as_built(db, games):
    save_facts(db, 10, "Sample Quest", players=((2, 4), (1, 2)), is_free=True)
    assert current_facts(db, [10])[10] == facts_for(
        10, "Sample Quest", players=((2, 4), (1, 2)), is_free=True
    )


def test_save_facts_adds_the_game_when_there_is_none(db):
    save_facts(db, 30, "Demo Dungeon")
    assert db.get(Game, 30).source == SOURCE_RESEARCH
    assert current_facts(db, [30])[30].is_game is True


def test_facts_read_from_the_store_fact():
    facts = facts_for(
        10,
        "Sample Quest",
        categories=("Single-player", "Online Co-op"),
        early_access=True,
        coming_soon=True,
        release_date="Coming soon",
    )
    assert facts.is_game is True
    assert facts.early_access is True and facts.coming_soon is True
    assert facts.is_free is False
    assert facts.categories == ["Single-player", "Online Co-op"]
    assert facts.release_date == "Coming soon"
    assert facts.short_description == "About Sample Quest."
    assert facts.price_text == "$19.99"
    assert facts.review_text == "Very Positive (100 reviews)"
    assert facts.version == GameVersion(True, None)


def test_a_demo_is_not_a_game():
    assert facts_for(10, "Sample Quest", type="demo").is_game is False


def test_price_text_for_free_and_unpriced_games():
    assert facts_for(10, "Sample Quest", is_free=True).price_text == "Free"
    assert facts_for(10, "Sample Quest", price_cents=None).price_text is None


def test_review_text_without_reviews_is_just_the_score():
    facts = GameFacts(
        10,
        reviews=Fact(
            {
                "score_desc": "No user reviews",
                "total_positive": 0,
                "total_negative": 0,
                "total_reviews": 0,
            },
            FIXED_NOW,
            None,
        ),
    )
    assert facts.review_text == "No user reviews"


@pytest.mark.parametrize(
    "players,most",
    [
        (((2, 4), (1, 6)), 6),
        (((2, 4), None), 4),
        ((None, (1, 2)), 2),
        ((None, None), None),
        (None, None),
    ],
)
def test_coop_max_is_the_larger_maximum(players, most):
    assert facts_for(10, "Sample Quest", players=players).coop_max == most


def test_the_players_fact_keeps_its_page():
    facts = facts_for(10, "Sample Quest", players=((2, 4), None))
    assert facts.players.source_url == "https://example.com/players"
    assert facts.players.data == {
        "online_coop": {"min": 2, "max": 4},
        "local_coop": None,
        "competitive": None,
    }


def test_nothing_known_reads_as_unknown():
    facts = GameFacts(10)
    assert facts.is_game is False
    assert facts.early_access is None and facts.is_free is None
    assert facts.coming_soon is None
    assert facts.categories == [] and facts.release_date == ""
    assert facts.short_description == ""
    assert facts.price_text is None and facts.review_text is None
    assert facts.coop_max is None
    assert facts.version == GameVersion(None, None)
    assert facts.snapshot() == {}


def test_an_unavailable_store_entry_reads_as_unknown():
    facts = GameFacts(10, store=Fact(store_fact_data(None, "us"), FIXED_NOW, None))
    assert facts.is_game is False and facts.early_access is None
    assert facts.price_text is None and facts.version == GameVersion(None, None)


def test_the_version_takes_the_patch_date_from_the_news_fact():
    facts = GameFacts(
        10,
        store=facts_for(10, "Sample Quest", early_access=False).store,
        news=Fact(
            {"latest_patch_at": T2.isoformat(), "latest_patch_title": "Patch 1.2"},
            FIXED_NOW,
            None,
        ),
    )
    assert facts.version == GameVersion(False, T2)


def test_a_snapshot_lists_each_kind_present():
    facts = facts_for(10, "Sample Quest", players=(None, (1, 2)))
    snapshot = facts.snapshot()
    assert list(snapshot) == [STORE, REVIEWS, NEWS, PLAYERS]
    assert snapshot[REVIEWS] == {
        "data": facts.reviews.data,
        "fetched_at": FIXED_NOW.isoformat(),
        "source_url": None,
    }
    assert snapshot[PLAYERS]["source_url"] == "https://example.com/players"
    assert list(GameFacts(10, news=facts.news).snapshot()) == [NEWS]


def test_the_patch_news_is_saved_as_text_and_read_back(
    db, steam_client, fake_steam, games
):
    fake_steam.details[10] = store_data("Sample Quest")
    fake_steam.news[10] = [
        {"title": "Patch 1.2", "date": int(T2.timestamp()), "tags": ["patchnotes"]}
    ]
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW)
    facts = current_facts(db, [10])[10]
    assert facts.news.data == {
        "latest_patch_at": T2.isoformat(),
        "latest_patch_title": "Patch 1.2",
    }
    assert facts.version == GameVersion(False, T2)


def test_an_unavailable_entry_skips_reviews_and_news(
    db, steam_client, fake_steam, games
):
    assert refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW).fetched == 1
    assert len(fake_steam.requests) == 1
    # It is not asked again until the facts are a week old.
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW + timedelta(days=1))
    assert len(fake_steam.requests) == 1
    facts = current_facts(db, [10])[10]
    assert facts.store.data == {"available": False, "region": "us"}
    assert facts.reviews is None and facts.news is None


def test_only_the_stale_kinds_are_fetched_again(db, steam_client, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest")
    save_facts(db, 10, "Sample Quest", fetched_at=FIXED_NOW - timedelta(days=8))
    db.add(
        GameFact(
            appid=10,
            kind=NEWS,
            data={"latest_patch_at": None, "latest_patch_title": None},
            source_url=None,
            fetched_at=FIXED_NOW - FACTS_MAX_AGE,
        )
    )
    db.commit()
    result = refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW)
    assert result == FactsRefresh(fetched=2)
    assert [r.url.path for r in fake_steam.requests] == [
        "/api/appdetails",
        "/appreviews/10",
    ]


def test_the_region_is_asked_for_and_saved(db, steam_client, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest")
    refresh_store_facts(db, steam_client, [10], "gb", FIXED_NOW)
    assert fake_steam.requests[0].url.params["cc"] == "gb"
    assert current_facts(db, [10])[10].store.data["region"] == "gb"


def test_games_are_taken_in_order_once_each(db, steam_client, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest")
    fake_steam.details[20] = store_data("Test Tactics")
    result = refresh_store_facts(db, steam_client, [20, 10, 20], "us", FIXED_NOW)
    assert result == FactsRefresh(fetched=6)
    stores = [
        r.url.params["appids"] for r in fake_steam.requests if "appids" in r.url.params
    ]
    assert stores == ["20", "10"]


def test_each_game_is_committed_before_the_next(db, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest")

    def handler(request):
        if request.url.params.get("appids") == "20":
            raise RuntimeError("the second game breaks the refresh")
        return fake_steam.handler(request)

    client = SteamClient(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError):
        refresh_store_facts(db, client, [10, 20], "us", FIXED_NOW)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(GameFact)) == 3
    assert current_facts(db, [10])[10].is_game


def test_a_failure_on_one_game_does_not_stop_the_others(
    db, steam_client, fake_steam, games
):
    fake_steam.details[10] = store_data("Sample Quest")
    fake_steam.details[20] = store_data("Test Tactics")
    client = scripted_steam(fake_steam, [False, True, True])
    result = refresh_store_facts(db, client, [10, 20], "us", FIXED_NOW)
    assert result.problems == {10: NO_RESPONSE}
    assert result.fetched == 5
    facts = current_facts(db, [10, 20])
    assert facts[10].store is None and facts[10].reviews is not None
    assert facts[20].is_game is True


def test_a_success_ends_the_run_of_failures(db, steam_client, fake_steam):
    for appid in (100, 101, 102):
        ensure_game(db, appid, f"Sample {appid}", FIXED_NOW)
        fake_steam.details[appid] = store_data(f"Sample {appid}")
    client = scripted_steam(fake_steam, [False, True, False] * 3)
    result = refresh_store_facts(db, client, [100, 101, 102], "us", FIXED_NOW)
    assert len(fake_steam.requests) == 9
    assert result.fetched == 3 and set(result.problems) == {100, 101, 102}


def test_games_left_after_the_cutoff_get_the_outage_message(db, fake_steam):
    assert MAX_STEAM_FAILURES_IN_A_ROW == 3
    for appid in (100, 101, 102):
        ensure_game(db, appid, f"Sample {appid}", FIXED_NOW)
        fake_steam.details[appid] = store_data(f"Sample {appid}")
    # The first game's store call works; its reviews and news calls and the next game's
    # store call fail.
    client = scripted_steam(fake_steam, [True, False, False, False])
    result = refresh_store_facts(db, client, [100, 101, 102], "us", FIXED_NOW)
    assert len(fake_steam.requests) == 4
    assert result.fetched == 1
    assert result.problems == {100: NO_RESPONSE, 101: NO_RESPONSE, 102: NO_RESPONSE}


def test_the_cutoff_leaves_what_was_fetched_before_it(db, fake_steam):
    for appid in (100, 101):
        ensure_game(db, appid, f"Sample {appid}", FIXED_NOW)
        fake_steam.details[appid] = store_data(f"Sample {appid}")
    client = scripted_steam(fake_steam, [True, True, True, False, False, False])
    result = refresh_store_facts(db, client, [100, 101], "us", FIXED_NOW)
    assert result.fetched == 3 and set(result.problems) == {101}
    facts = current_facts(db, [100, 101])
    assert facts[100].is_game and facts[101].store is None


def test_player_counts_are_saved_as_a_snapshot(db, games):
    counts = {
        "online_coop": {"min": 2, "max": 4},
        "local_coop": None,
        "competitive": None,
    }
    save_player_counts(db, 10, counts, "https://example.com/players", FIXED_NOW)
    newer = dict(counts, online_coop={"min": 2, "max": 6})
    save_player_counts(db, 10, newer, None, FIXED_NOW + timedelta(days=1))
    players = current_facts(db, [10])[10].players
    assert players == Fact(newer, FIXED_NOW + timedelta(days=1), None)
    assert (
        db.scalar(
            select(func.count()).select_from(GameFact).where(GameFact.kind == PLAYERS)
        )
        == 2
    )
