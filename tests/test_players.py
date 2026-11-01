import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fakes import FIXED_NOW, by_prompt, counts_reply, facts_for
from pydantic import ValidationError
from sqlalchemy import select

from app.models import AiCall, Game, GameFact, SearchCall
from app.prompts import load_prompt
from app.services.ai import StructuredReplyError
from app.services.game_facts import PLAYERS, Fact, GameFacts, current_facts
from app.services.players import (
    LANE_HEADS,
    PLAYER_QUERY,
    PlayerCounts,
    PlayerRange,
    lane_mismatch,
    lookup_player_counts,
    needs_player_counts,
    supports_lane,
)
from app.services.reviews import Lane
from app.services.search import SearchError


def lookup(db, app, services, fake_search, appid=10):
    lookup_player_counts(
        db,
        app.state.vault,
        fake_search("tvly-sample"),
        db.get(Game, appid),
        services=services,
        run_ref="research:1",
        now=FIXED_NOW,
    )


def players_snapshots(db, appid=10):
    return list(
        db.scalars(
            select(GameFact).where(GameFact.appid == appid, GameFact.kind == PLAYERS)
        )
    )


def test_lookup_saves_counts_with_their_source(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_ai.replies["sample-local"] = by_prompt(player_counts=counts_reply())
    lookup(db, app, services, fake_search)
    facts = current_facts(db, [10])[10]
    assert (
        facts.coop_max == 4
        and facts.players.source_url == "https://example.com/sample-quest"
    )
    assert (
        fake_search.calls[0][0] == '"Sample Quest" number of players co-op multiplayer'
    )
    assert (
        "A cozy co-op adventure for two." in fake_ai.calls[0][2]
    )  # the results reach the model


def test_lookup_asks_for_five_results_and_logs_both_calls_under_the_run(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_ai.replies["sample-local"] = by_prompt(player_counts=counts_reply())
    lookup(db, app, services, fake_search)
    assert fake_search.calls == [(PLAYER_QUERY.format(name="Sample Quest"), 5)]
    (search,) = db.scalars(select(SearchCall)).all()
    assert search.run_ref == "research:1"
    (call,) = db.scalars(select(AiCall)).all()
    assert (call.job, call.prompt_name, call.run_ref) == (
        "everyday",
        "player_counts",
        "research:1",
    )
    assert call.prompt_version == load_prompt("player_counts").version


def test_the_ranges_are_saved_as_the_model_gave_them(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_ai.replies["sample-local"] = by_prompt(
        player_counts=json.dumps(
            {
                "online_coop": {"min": 2, "max": 4},
                "local_coop": {"min": 1, "max": 2},
                "competitive": {"min": 2, "max": 8},
                "source_url": "https://example.com/test-tactics",
            }
        )
    )
    lookup(db, app, services, fake_search)
    (snapshot,) = players_snapshots(db)
    assert snapshot.data == {
        "online_coop": {"min": 2, "max": 4},
        "local_coop": {"min": 1, "max": 2},
        "competitive": {"min": 2, "max": 8},
    }
    assert snapshot.source_url == "https://example.com/test-tactics"
    assert snapshot.fetched_at == FIXED_NOW


def test_counts_from_a_page_not_in_the_results_are_dropped(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):  # Review focus 5
    fake_ai.replies["sample-local"] = by_prompt(
        player_counts=counts_reply(url="https://invented.example/")
    )
    lookup(db, app, services, fake_search)
    facts = current_facts(db, [10])[10]
    assert (
        facts.players is not None
        and facts.coop_max is None
        and facts.players.source_url is None
    )


@pytest.mark.parametrize(
    "url",
    [None, "", "https://example.com/sample-quest/", "HTTPS://EXAMPLE.COM/sample-quest"],
)
def test_the_source_must_be_a_result_url_exactly(
    url, db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_ai.replies["sample-local"] = by_prompt(
        player_counts=counts_reply(url=url, local=(1, 2))
    )
    lookup(db, app, services, fake_search)
    (snapshot,) = players_snapshots(db)
    assert snapshot.data == {
        "online_coop": None,
        "local_coop": None,
        "competitive": None,
    }
    assert snapshot.source_url is None


def test_a_result_without_a_url_cannot_vouch_for_an_empty_source(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_search.payload["results"].append(
        {"title": "No link", "url": None, "content": "Up to 4 players."}
    )
    fake_ai.replies["sample-local"] = by_prompt(player_counts=counts_reply(url=""))
    lookup(db, app, services, fake_search)
    (snapshot,) = players_snapshots(db)
    assert snapshot.source_url is None and current_facts(db, [10])[10].coop_max is None


def test_a_game_with_no_known_count_is_saved_so_it_waits_a_week(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_ai.replies["sample-local"] = by_prompt(
        player_counts=counts_reply(online=None, local=None)
    )
    lookup(db, app, services, fake_search)
    facts = current_facts(db, [10])[10]
    assert facts.players is not None and facts.coop_max is None
    assert not needs_player_counts(facts, FIXED_NOW + timedelta(days=7))


def test_a_failed_search_saves_nothing_and_the_error_reaches_the_caller(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_search.fail(RuntimeError("Tavily is down"))
    with pytest.raises(SearchError, match="Tavily is down"):
        lookup(db, app, services, fake_search)
    assert players_snapshots(db) == [] and fake_ai.calls == []
    assert needs_player_counts(current_facts(db, [10])[10], FIXED_NOW)


@pytest.mark.parametrize(
    "reply",
    [
        "I could not find the player counts.",
        # min above max
        json.dumps(
            {
                "online_coop": {"min": 5, "max": 2},
                "local_coop": None,
                "competitive": None,
                "source_url": "https://example.com/sample-quest",
            }
        ),
    ],
)
def test_a_reply_that_cannot_be_used_saves_nothing(
    reply, db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_ai.replies["sample-local"] = by_prompt(player_counts=reply)
    with pytest.raises(StructuredReplyError) as raised:
        lookup(db, app, services, fake_search)
    assert raised.value.reason == "unusable"
    assert players_snapshots(db) == []
    assert needs_player_counts(current_facts(db, [10])[10], FIXED_NOW)


def test_a_missing_ai_setup_saves_nothing(db, app, services, fake_search, games):
    with pytest.raises(StructuredReplyError) as raised:
        lookup(db, app, services, fake_search)
    assert raised.value.reason == "not_configured" and players_snapshots(db) == []


def test_needs_counts_again_after_seven_days(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_ai.replies["sample-local"] = by_prompt(player_counts=counts_reply())
    lookup(db, app, services, fake_search)
    facts = current_facts(db, [10])[10]
    assert not needs_player_counts(facts, FIXED_NOW + timedelta(days=7))
    assert needs_player_counts(facts, FIXED_NOW + timedelta(days=7, seconds=1))


def test_a_game_with_no_players_fact_needs_counts():
    assert needs_player_counts(GameFacts(appid=10), FIXED_NOW)
    assert needs_player_counts(facts_for(10, "Sample Quest"), FIXED_NOW)


def test_a_lookup_after_a_week_adds_a_snapshot_and_keeps_the_old_one(
    db, app, services, fake_ai, fake_search, saved_ai_setup, games
):
    fake_ai.replies["sample-local"] = by_prompt(
        player_counts=counts_reply(online=(1, 2))
    )
    lookup(db, app, services, fake_search)
    fake_ai.replies["sample-local"] = by_prompt(
        player_counts=counts_reply(online=(1, 6))
    )
    later = FIXED_NOW + timedelta(days=8)
    lookup_player_counts(
        db,
        app.state.vault,
        fake_search("tvly-sample"),
        db.get(Game, 10),
        services=services,
        run_ref="research:2",
        now=later,
    )
    assert len(players_snapshots(db)) == 2
    facts = current_facts(db, [10])[10]
    assert facts.coop_max == 6 and not needs_player_counts(facts, later)


def test_the_player_ranges_are_checked():
    assert PlayerRange(min=2, max=2).max == 2
    for bad in ({"min": 0, "max": 2}, {"min": 1, "max": 1001}, {"min": 5, "max": 2}):
        with pytest.raises(ValidationError):
            PlayerRange(**bad)
    counts = PlayerCounts.model_validate(
        {"online_coop": {"min": 1, "max": 4}, "source_url": "https://example.com/a"}
    )
    assert counts.local_coop is None and counts.competitive is None
    with pytest.raises(ValidationError):
        PlayerCounts.model_validate({"online_coop": {"min": 1, "max": 4}, "players": 4})


def test_the_prompt_names_the_game_and_gives_each_result():
    prompt = load_prompt("player_counts")
    text = prompt.render(
        game="Sample Quest",
        results=[
            {
                "title": "A title",
                "url": "https://example.com/a",
                "content": "Body {{ 6 * 7 }}",
            }
        ],
    )
    assert text.startswith("# player_counts\n")
    for part in (
        "Sample Quest",
        "A title",
        "https://example.com/a",
        "Body {{ 6 * 7 }}",
    ):
        assert part in text
    for kind in ("online_coop", "local_coop", "competitive", "source_url", "null"):
        assert kind in text
    assert "data" in text and "nothing" in text.lower()
    assert "No results" in prompt.render(game="Sample Quest", results=[])


def test_the_lane_heads():
    assert LANE_HEADS == {
        Lane.SOLO: (1, 1),
        Lane.TWO: (2, 2),
        Lane.THREE_FOUR: (3, 4),
        Lane.FIVE_PLUS: (5, None),
    }
    assert PLAYER_QUERY == '"{name}" number of players co-op multiplayer'


@pytest.mark.parametrize(
    "lane,online,local,fits",
    [  # Review focus 5
        (Lane.TWO, (1, 4), None, True),
        (Lane.TWO, (4, 8), None, False),
        (Lane.THREE_FOUR, (1, 2), (1, 3), True),
        (Lane.FIVE_PLUS, (1, 4), None, False),
        (Lane.FIVE_PLUS, (2, 64), None, True),
        (Lane.TWO, None, None, False),
    ],
)
def test_supports_lane(lane, online, local, fits):
    assert (
        supports_lane(facts_for(10, "Sample Quest", players=(online, local)), lane)
        is fits
    )


@pytest.mark.parametrize(
    "lane,online,local,fits",
    [
        (Lane.TWO, (2, 2), None, True),
        (Lane.TWO, (1, 1), None, False),
        (Lane.TWO, (3, 4), None, False),
        (Lane.THREE_FOUR, (4, 8), None, True),
        (Lane.THREE_FOUR, (5, 8), None, False),
        (Lane.THREE_FOUR, (1, 2), None, False),
        (Lane.THREE_FOUR, (3, 3), None, True),
        (Lane.FIVE_PLUS, (5, 5), None, True),
        (Lane.FIVE_PLUS, None, (2, 6), True),
        (Lane.FIVE_PLUS, (1, 2), (2, 6), True),
        (Lane.TWO, (1, 1), (2, 4), True),
        (Lane.TWO, (1, 1), (3, 4), False),
    ],
)
def test_a_co_op_range_has_to_overlap_the_lane(lane, online, local, fits):
    assert (
        supports_lane(facts_for(10, "Sample Quest", players=(online, local)), lane)
        is fits
    )


@pytest.mark.parametrize("lane", [Lane.SOLO, Lane.TWO, Lane.THREE_FOUR, Lane.FIVE_PLUS])
def test_a_competitive_count_never_qualifies_a_game_for_a_co_op_lane(lane):
    base = facts_for(10, "Sample Quest", players=(None, None))
    competitive = {
        "online_coop": None,
        "local_coop": None,
        "competitive": {"min": 1, "max": 64},
    }
    facts = replace(base, players=Fact(competitive, FIXED_NOW, "https://example.com/p"))
    assert supports_lane(facts, lane) is (lane is Lane.SOLO)


def test_solo_needs_steam_to_list_single_player():
    assert supports_lane(facts_for(10, "Sample Quest"), Lane.SOLO)
    assert not supports_lane(
        facts_for(10, "Sample Quest", categories=("Online Co-op",)), Lane.SOLO
    )
    # Counts don't make a game single-player.
    assert not supports_lane(
        facts_for(
            10, "Sample Quest", players=((1, 4), None), categories=("Online Co-op",)
        ),
        Lane.SOLO,
    )
    assert not supports_lane(GameFacts(appid=10), Lane.SOLO)


def test_a_single_player_game_with_co_op_counts_fits_both():
    facts = facts_for(10, "Sample Quest", players=((1, 4), None))
    assert supports_lane(facts, Lane.SOLO) and supports_lane(facts, Lane.TWO)


@pytest.mark.parametrize(
    "lane,players,categories,why",
    [  # Review focus 5
        (Lane.SOLO, None, ("Online Co-op",), "Steam doesn't list it as single-player"),
        (Lane.TWO, None, ("Single-player",), "no co-op player count is known"),
        (
            Lane.THREE_FOUR,
            ((1, 2), None),
            ("Online Co-op",),
            "its co-op supports up to 2 players",
        ),
        (
            Lane.TWO,
            ((4, 8), None),
            ("Online Co-op",),
            "its co-op needs at least 4 players",
        ),
        (Lane.SOLO, None, ("Single-player",), None),
    ],
)
def test_lane_mismatch(lane, players, categories, why):
    assert (
        lane_mismatch(
            facts_for(10, "Sample Quest", players=players, categories=categories), lane
        )
        == why
    )


def test_lane_mismatch_says_player_for_one_and_players_for_more():
    one = facts_for(10, "Sample Quest", players=((1, 1), None))
    assert lane_mismatch(one, Lane.TWO) == "its co-op supports up to 1 player"
    assert lane_mismatch(one, Lane.FIVE_PLUS) == "its co-op supports up to 1 player"


def test_lane_mismatch_uses_the_larger_maximum_and_the_smallest_minimum():
    both = facts_for(10, "Sample Quest", players=((1, 2), (1, 3)))
    assert lane_mismatch(both, Lane.FIVE_PLUS) == "its co-op supports up to 3 players"
    needs = facts_for(10, "Sample Quest", players=((6, 8), (5, 8)))
    assert lane_mismatch(needs, Lane.THREE_FOUR) == "its co-op needs at least 5 players"


def test_lane_mismatch_blames_the_modes_that_reach_the_lane():
    # Online takes 1 or 2, local 5 to 8. Neither seats three or four. The game does take one
    # player online, so "needs at least 1 players" would be untrue.
    gap = facts_for(10, "Sample Quest", players=((1, 2), (5, 8)))
    assert not supports_lane(gap, Lane.THREE_FOUR)
    assert lane_mismatch(gap, Lane.THREE_FOUR) == "its co-op needs at least 5 players"


def test_a_players_fact_with_no_counts_reads_as_no_count_known():
    facts = facts_for(10, "Sample Quest", players=(None, None))
    assert lane_mismatch(facts, Lane.TWO) == "no co-op player count is known"


@pytest.mark.parametrize("lane", list(Lane))
def test_a_lane_mismatch_comes_with_exactly_when_the_lane_is_not_supported(lane):
    for players in (
        None,
        ((1, 1), None),
        ((2, 2), None),
        ((3, 4), None),
        ((5, 9), (2, 3)),
    ):
        for categories in (("Single-player",), ("Online Co-op",)):
            facts = facts_for(
                10, "Sample Quest", players=players, categories=categories
            )
            why = lane_mismatch(facts, lane)
            assert (why is None) is supports_lane(facts, lane)
            if why is not None:
                # A phrase to go after a name: no full stop, and lower case but for "Steam".
                assert why == why.strip() and not why.endswith(".")
                assert why.startswith("Steam") or why[0].islower()


# The fakes later tasks lean on.


def test_by_prompt_picks_the_reply_by_the_prompts_first_line():
    reply = by_prompt(
        default="fallback",
        research_judge='{"verdicts": []}',
        player_counts=lambda prompt: f"saw {len(prompt)} characters",
    )
    assert reply("# research_judge\nRule on these.") == '{"verdicts": []}'
    prompt = "# player_counts\nFind the counts."
    assert reply(prompt) == f"saw {len(prompt)} characters"
    assert reply("# something_else\nBody") == "fallback"
    assert reply("No heading at all") == "fallback"
    assert reply("") == "fallback"
    # A heading further down doesn't route the prompt.
    assert reply("Intro\n# research_judge\n") == "fallback"
    assert by_prompt()("# anything") == "ok"


def test_a_search_responder_answers_each_query_in_place_of_the_payload(
    fake_search,
):
    seen = []

    def responder(query):
        seen.append(query)
        return {
            "results": [
                {"title": query, "url": "https://example.com/q", "content": "c"}
            ]
        }

    fake_search.responder = responder
    client = fake_search("tvly-sample")
    assert client.search("first", 3)["results"][0]["title"] == "first"
    assert client.search("second", 3)["results"][0]["title"] == "second"
    assert seen == ["first", "second"]
    assert fake_search.calls == [("first", 3), ("second", 3)]
    # A failure still wins, and with no responder the payload comes back.
    fake_search.fail(RuntimeError("down"))
    with pytest.raises(RuntimeError):
        client.search("third", 3)
    fake_search.failure = None
    fake_search.responder = None
    assert client.search("fourth", 3)["query"] == "co-op games"
