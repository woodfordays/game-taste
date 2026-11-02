"""One lane in one research round: candidate history, backlog, web, facts, players, the
Recommender's proposals, the judge's verdicts and the save."""

import json
from datetime import timedelta

import pytest
from fakes import (
    FIXED_NOW,
    T1,
    T2,
    counts_reply,
    fill_lane,
    judged,
    messages,
    save_facts,
    store_data,
    two_version,
    verdicts,
)
from sqlalchemy import delete, func, select

from app.jobs import job_context
from app.models import (
    AiCall,
    Candidate,
    Game,
    JobAssignment,
    ResearchEvent,
    ResearchRun,
    SearchCall,
)
from app.routines.research import (
    LaneOutcome,
    head_counts_seen,
    normalize_name,
    research_lane,
    resolve_name,
)
from app.services.ai import StructuredReplyError
from app.services.game_facts import GameVersion
from app.services.queue import lane_entries, recommended_counts, save_judged
from app.services.reviews import Lane, ReviewForm, publish


def run_lane(app, run, lane=Lane.SOLO, rnd=1):
    return research_lane(job_context(app), run.id, rnd, lane)


def earlier_candidate(db, solo_version, appid, verdict, reason, *, room=5, **over):
    """A candidate of `appid` in Solo from a finished run a month ago. With `room=0` an
    accepted one wasn't added."""
    earlier = ResearchRun(
        status="finished", targets=[], created_at=FIXED_NOW - timedelta(days=30)
    )
    db.add(earlier)
    db.commit()
    save_judged(
        db,
        [judged(appid, verdict, reason, **over)],
        run_id=earlier.id,
        round=1,
        lane=Lane.SOLO,
        room=room,
        profile_version_id=solo_version.id,
        now=FIXED_NOW - timedelta(days=30),
    )


def prompts(fake_ai, name):
    return [p for _, _, p in fake_ai.calls if p.startswith(f"# {name}")]


# --- the round ----------------------------------------------------------------------


def test_round_adds_what_the_judge_accepts(
    app, db, research_world, research_run, solo_version
):
    assert run_lane(app, research_run) == LaneOutcome(
        added=1, rejected=1, cancelled=False
    )
    [entry] = lane_entries(db, Lane.SOLO, "recommended")
    assert entry.game.appid == 40 and entry.candidate.source == "web_research"
    assert entry.candidate.fits == [
        {
            "section": "loved",
            "text": "Likes short, tight sessions",
            "tag": "stated",
            "how": "Runs take twenty minutes.",
        }
    ]
    old = db.scalar(select(Candidate).where(Candidate.appid == 60))
    assert (old.source, old.verdict, old.reason) == (
        "owned_backlog",
        "rejected",
        "Runs are too long.",
    )
    assert (
        old.profile_version_id == solo_version.id
        and old.facts["store"]["data"]["name"] == "Old Crate"
    )
    assert "Solo: 1 added, 1 rejected." in messages(db, research_run)


def test_an_over_long_reason_is_cut_and_the_verdict_kept(
    app, db, research_world, research_run
):  # Ruling 23
    long_reason = "Fits L1: " + "x" * 292  # 301 characters
    research_world["judge"] = {
        "verdicts": [
            {"appid": 40, "accept": True, "reason": long_reason},
            {"appid": 60, "accept": False, "reason": "Runs are too long."},
        ],
        "reject_all": None,
    }
    assert run_lane(app, research_run) == LaneOutcome(1, 1, False)
    accepted = db.scalar(select(Candidate).where(Candidate.appid == 40))
    assert (accepted.verdict, accepted.added) == ("accepted", True)
    assert accepted.reason == long_reason[:300]


def test_the_log_of_a_round(app, db, research_world, research_run):
    run_lane(app, research_run)
    events = db.execute(
        select(
            ResearchEvent.round,
            ResearchEvent.lane,
            ResearchEvent.step,
            ResearchEvent.message,
        )
        .where(ResearchEvent.run_id == research_run.id)
        .order_by(ResearchEvent.id)
    ).all()
    assert [tuple(e) for e in events] == [
        (1, "solo", "plan", "Asking the Recommender where to look."),
        (1, "solo", "search", "Searching the web: short cozy roguelites"),
        (1, "solo", "search", "Found 1 game on the web."),
        (1, "solo", "facts", "Checking Steam for 2 games."),
        (1, "solo", "propose", "The Recommender is choosing games."),
        (1, "solo", "judge", "The judge is checking 2 games."),
        (1, "solo", "saved", "Solo: 1 added, 1 rejected."),
    ]


def test_the_plan_sees_the_backlog_and_the_games_in_hand(
    app, db, research_world, research_run, solo_version, fake_ai
):
    fill_lane(db, Lane.SOLO, 1, solo_version)  # Sample Filler 100 is recommended
    run_lane(app, research_run)
    [plan] = prompts(fake_ai, "research_plan")
    # Sample Quest is reviewed in Solo, and Test Tactics' 120 minutes aren't under 120.
    backlog = plan.split("<backlog>")[1].split("</backlog>")[0]
    assert backlog.strip() == "appid 60: Old Crate (0.5 hours played)"
    known = plan.split("<known>")[1].split("</known>")[0]
    assert known.strip() == "Sample Filler 100"


def test_the_judge_reads_the_reviews_behind_the_profile(
    app, db, research_world, research_run, fake_ai
):
    publish(db, 20, Lane.SOLO, ReviewForm(rating=6, text="Too slow."), FIXED_NOW)
    run_lane(app, research_run)  # the profile hasn't learned from that one yet
    [judge_prompt] = prompts(fake_ai, "research_judge")
    assert "Short runs, real choices every turn." in judge_prompt
    assert "Too slow." not in judge_prompt


def test_every_call_belongs_to_the_run(app, db, research_world, research_run):
    run_lane(app, research_run)
    ref = f"research:{research_run.id}"
    assert {
        c.run_ref
        for c in db.scalars(select(AiCall).where(AiCall.prompt_name.like("research_%")))
    } == {ref}
    assert {s.run_ref for s in db.scalars(select(SearchCall))} == {ref}


def test_a_lane_never_overfills(
    app, db, research_world, research_run, solo_version
):  # Review focus 4
    fill_lane(db, Lane.SOLO, 19, solo_version)  # 19 accepted, added games 100..118
    research_world["judge"] = verdicts(accept=[40, 60])
    assert run_lane(app, research_run).added == 1
    assert recommended_counts(db)[Lane.SOLO] == 20
    surplus = db.scalar(
        select(Candidate).where(
            Candidate.run_id == research_run.id, Candidate.appid == 60
        )
    )
    assert surplus.verdict == "accepted" and surplus.added is False
    assert (
        "Solo: 1 added, 0 rejected. 1 more was accepted, but the lane is full."
        in messages(db, research_run)
    )


def test_the_log_counts_more_than_one_game_in_the_plural(
    app, db, research_world, research_run, solo_version, fake_steam
):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    fake_steam.store_hits.append({"id": 50, "name": "Party Pile"})
    fake_steam.details[50] = store_data("Party Pile")
    research_world["extract"]["games"].append(
        {"name": "Party Pile", "url": "https://example.com/cozy"}
    )
    research_world["propose"]["proposals"].append(
        {"appid": 50, "fits": [{"line": "L1", "how": "Rounds are short."}], "risk": "r"}
    )
    research_world["judge"] = verdicts(accept=[40, 50, 60])
    run_lane(app, research_run)
    log = messages(db, research_run)
    assert "Found 2 games on the web." in log
    assert "Checking Steam for 3 games." in log
    assert (
        "Solo: 1 added, 0 rejected. 2 more were accepted, but the lane is full." in log
    )


def test_invented_games_never_reach_the_judge(
    app, db, research_world, research_run, fake_ai
):  # Review focus 1
    research_world["propose"]["proposals"].append(
        {"appid": 99, "fits": [{"line": "L1", "how": "x"}], "risk": "r"}
    )
    run_lane(app, research_run)
    assert db.scalar(select(Candidate).where(Candidate.appid == 99)) is None
    assert "App 99 wasn't one of the candidates, so it was left out." in messages(
        db, research_run
    )
    [judge_prompt] = prompts(fake_ai, "research_judge")
    assert 'appid="99"' not in judge_prompt


def test_rejected_games_are_not_offered_again_in_the_run(
    app, db, research_world, research_run, fake_ai
):
    run_lane(app, research_run)
    run_lane(app, research_run, rnd=2)
    assert (
        db.scalar(
            select(func.count()).select_from(Candidate).where(Candidate.appid == 60)
        )
        == 1
    )
    second_plan = prompts(fake_ai, "research_plan")[-1]
    assert "Runs are too long." in second_plan
    assert "No candidates for Solo this round." in messages(db, research_run)


def test_an_owned_game_played_a_lot_is_not_proposed(
    app, db, research_world, research_run, fake_ai
):  # Ruling 26
    cozy = db.get(Game, 40)
    cozy.owned, cozy.playtime_minutes = True, 6000  # 100 hours, not reviewed in Solo
    db.commit()
    run_lane(app, research_run)  # the web search finds Cozy Crawl
    [propose_prompt] = prompts(fake_ai, "research_propose")
    assert 'appid="40"' not in propose_prompt and 'appid="60"' in propose_prompt
    assert db.scalar(select(Candidate).where(Candidate.appid == 40)) is None


def test_an_owned_game_played_a_lot_does_not_come_back_from_the_history(
    app, db, research_world, research_run, solo_version, fake_ai
):  # Ruling 26
    # Accepted earlier while the lane had no room, so it would come back.
    earlier_candidate(db, solo_version, 40, "accepted", "Fits L1.", room=0)
    cozy = db.get(Game, 40)
    cozy.owned, cozy.playtime_minutes = True, 120
    db.commit()
    research_world["plan"]["queries"] = []
    run_lane(app, research_run)
    assert not any("earlier candidate" in m for m in messages(db, research_run))
    [propose_prompt] = prompts(fake_ai, "research_propose")
    assert 'appid="40"' not in propose_prompt


# --- earlier candidates come back only through the history -----------------------------


def test_an_earlier_rejection_stays_out_when_nothing_changed(
    app, db, research_world, research_run, solo_version
):
    earlier_candidate(db, solo_version, 40, "rejected", "Too buggy at launch.")
    run_lane(app, research_run)  # the web search finds Cozy Crawl again
    assert (
        db.scalar(
            select(Candidate).where(
                Candidate.run_id == research_run.id, Candidate.appid == 40
            )
        )
        is None
    )
    assert "0 of 1 earlier candidates are back for another look." in messages(
        db, research_run
    )


def test_an_earlier_rejection_stays_out_of_the_backlog_too(
    app, db, research_world, research_run, solo_version, fake_ai
):
    earlier_candidate(db, solo_version, 60, "rejected", "Runs are too long.")
    run_lane(app, research_run)
    [plan] = prompts(fake_ai, "research_plan")
    assert "Old Crate" not in plan  # not offered, so the plan's pick of 60 is dropped
    assert (
        db.scalar(
            select(Candidate).where(
                Candidate.run_id == research_run.id, Candidate.appid == 60
            )
        )
        is None
    )


def test_an_earlier_rejection_comes_back_after_a_patch(
    app, db, research_world, research_run, solo_version, fake_steam, fake_ai
):
    earlier_candidate(
        db,
        solo_version,
        40,
        "rejected",
        "Too buggy at launch.",
        version=GameVersion(False, T1),
    )
    fake_steam.news[40] = [
        {"title": "Patch 1.0", "date": int(T2.timestamp()), "tags": ["patchnotes"]}
    ]
    run_lane(app, research_run)
    [entry] = lane_entries(db, Lane.SOLO, "recommended")
    assert entry.candidate.source == "candidate_history"
    assert all(
        "Too buggy at launch." in p
        for p in prompts(fake_ai, "research_propose")
        + prompts(fake_ai, "research_judge")
    )
    assert "1 of 1 earlier candidates are back for another look." in messages(
        db, research_run
    )


# --- who fits the lane --------------------------------------------------------------


def test_a_coop_lane_leaves_out_games_that_cant_seat_it(
    app, db, research_world, research_run, fake_ai
):  # Review focus 5
    two_version(db)  # a Two players review and profile version 1
    # Every search result says "Try Cozy Crawl.", so the game is told apart by the
    # prompt's own line, which quotes the name: The game is "Cozy Crawl".
    research_world["players"] = lambda p: counts_reply(
        url="https://example.com/cozy",
        online=(1, 4) if '"Cozy Crawl"' in p else (1, 1),
    )
    run_lane(app, research_run, lane=Lane.TWO)
    assert "Left out Old Crate: its co-op supports up to 1 player." in messages(
        db, research_run
    )
    assert {c.appid for c in db.scalars(select(Candidate))} == {40}


def test_a_failed_player_lookup_leaves_the_game_out_this_round(
    app, db, research_world, research_run
):
    two_version(db)
    research_world["players"] = "nope"  # an unusable reply, for both games
    assert run_lane(app, research_run, lane=Lane.TWO) == LaneOutcome(0, 0, False)
    log = messages(db, research_run)
    assert "Looking up player counts for 2 games." in log
    assert "Left out Cozy Crawl: no co-op player count is known." in log
    assert "No candidates for Two players this round." in log


def test_a_game_steam_has_no_details_for_leaves_the_pool(
    app, db, research_world, research_run, fake_steam
):  # Review focus 2
    fake_steam.details[60] = None  # not in the store in this region
    run_lane(app, research_run)
    log = messages(db, research_run)
    assert "Steam's details are missing for: Old Crate." in log
    assert "App 60 wasn't one of the candidates, so it was left out." in log
    assert {c.appid for c in db.scalars(select(Candidate))} == {40}


def test_a_game_steam_fails_to_refresh_keeps_its_last_facts(
    app, db, research_world, research_run, solo_version, fake_steam
):  # Review focus 2
    eight_days_ago = FIXED_NOW - timedelta(days=8)
    save_facts(db, 40, "Cozy Crawl", fetched_at=eight_days_ago)
    save_facts(db, 60, "Old Crate", fetched_at=eight_days_ago)
    # Accepted earlier while the lane had no room, so it comes back through the history.
    earlier_candidate(db, solo_version, 40, "accepted", "Fits L1.", room=0)
    research_world["plan"]["queries"] = []
    fake_steam.fail_with(500)
    run_lane(app, research_run)
    log = messages(db, research_run)
    stale = "Couldn't refresh Steam's details for: {}. Using the last ones saved."
    assert stale.format("Cozy Crawl") in log  # the history scan
    assert stale.format("Old Crate") in log  # the facts step
    assert not any(m.startswith("Steam's details are missing") for m in log)
    saved = {
        c.appid: c
        for c in db.scalars(
            select(Candidate).where(Candidate.run_id == research_run.id)
        )
    }
    assert set(saved) == {40, 60}
    assert saved[60].facts["store"]["fetched_at"] == eight_days_ago.isoformat()


def test_names_without_an_exact_store_match_are_reported(
    app, db, research_world, research_run
):
    research_world["extract"]["games"].append(
        {"name": "Cozy Crawl Deluxe", "url": "https://example.com/cozy"}
    )
    run_lane(app, research_run)
    log = messages(db, research_run)
    assert "Couldn't find these on Steam: Cozy Crawl Deluxe." in log
    assert "Found 1 game on the web." in log


def test_a_steam_outage_costs_three_store_searches(
    app, db, research_world, research_run, fake_steam
):  # Review focus 2
    research_world["extract"]["games"] = [
        {"name": name, "url": "https://example.com/cozy"}
        for name in ("Cozy Crawl", "Party Pile", "Sample Quest", "Demo Dungeon")
    ]
    fake_steam.fail_with(500)
    assert run_lane(app, research_run) == LaneOutcome(0, 0, False)
    searches = [r for r in fake_steam.requests if r.url.path == "/api/storesearch/"]
    assert len(searches) == 3
    log = messages(db, research_run)
    assert (
        "Couldn't find these on Steam: Cozy Crawl, Party Pile, Sample Quest, Demo Dungeon."
        in log
    )
    assert "Steam's details are missing for: Old Crate." in log
    assert "No candidates for Solo this round." in log


# --- when things go wrong -----------------------------------------------------------


def test_a_search_failure_carries_on_with_the_backlog(
    app, db, research_world, research_run, fake_search
):
    fake_search.fail(RuntimeError("down"))
    assert run_lane(app, research_run) == LaneOutcome(
        added=0, rejected=1, cancelled=False
    )
    log = messages(db, research_run)
    assert any(m.startswith("The web search failed (") for m in log)
    assert "Checking Steam for 1 game." in log  # only Old Crate, from the backlog


def test_a_cancel_before_saving_drops_the_lane(
    app, db, research_world, research_run
):  # Review focus 3
    def judge_then_cancel(prompt):
        with app.state.session_factory() as s:
            s.get(ResearchRun, research_run.id).cancel_requested = True
            s.commit()
        return json.dumps(research_world["judge"])

    research_world["judge_fn"] = judge_then_cancel
    assert run_lane(app, research_run).cancelled is True
    assert db.scalar(select(func.count()).select_from(Candidate)) == 0


def test_a_cancel_during_the_last_player_lookup_drops_the_lane(
    app, db, research_world, research_run
):  # Review focus 3
    two_version(db)
    research_world["plan"][
        "queries"
    ] = []  # Old Crate, from the backlog, is the only one

    def counts_then_cancel(prompt):
        with app.state.session_factory() as s:
            s.get(ResearchRun, research_run.id).cancel_requested = True
            s.commit()
        return counts_reply(url="https://example.com/cozy", online=(1, 1))

    research_world["players"] = counts_then_cancel
    assert run_lane(app, research_run, lane=Lane.TWO) == LaneOutcome(0, 0, True)
    log = messages(db, research_run)
    assert "Looking up player counts for 1 game." in log
    assert "No candidates for Two players this round." not in log


def test_a_cancel_before_the_lane_starts_makes_no_calls(
    app, db, research_world, research_run, fake_ai, fake_search
):  # Review focus 3
    research_run.cancel_requested = True
    db.commit()
    assert run_lane(app, research_run) == LaneOutcome(0, 0, True)
    assert fake_ai.calls == [] and fake_search.calls == []


def test_an_unusable_judge_reply_ends_the_lane_not_the_run(
    app, db, research_world, research_run
):
    research_world["judge_fn"] = lambda p: "nope"
    assert run_lane(app, research_run) == LaneOutcome(0, 0, False)
    assert any(
        m.startswith(
            "Solo stopped this round: The model's reply wasn't in the expected format."
        )
        for m in messages(db, research_run)
    )
    assert db.scalar(select(func.count()).select_from(Candidate)) == 0


def test_an_unset_model_stops_the_whole_run(app, db, research_world, research_run):
    db.execute(delete(JobAssignment).where(JobAssignment.job == "recommender"))
    db.commit()
    with pytest.raises(StructuredReplyError) as err:
        run_lane(app, research_run)
    assert err.value.reason == "not_configured"


def test_a_lane_with_no_profile_is_skipped(app, db, research_world, research_run):
    assert run_lane(app, research_run, lane=Lane.FIVE_PLUS) == LaneOutcome(0, 0, False)
    assert (
        "No profile yet for Five or more. Publish a review played at this head count first."
        in messages(db, research_run)
    )


# --- the helpers --------------------------------------------------------------------


def test_head_counts_seen(db, games, people):
    sam, alex, crew = people
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(text="x", people=[sam.id]), FIXED_NOW)
    # 1 + Sam, raised to 3; the crew of two seats 3 too
    assert head_counts_seen(db, Lane.THREE_FOUR) == [3]
    assert head_counts_seen(db, Lane.SOLO) == []


def test_head_counts_seen_take_groups_that_fit_the_lane(db, games, people):
    sam, alex, crew = people
    publish(db, 20, Lane.TWO, ReviewForm(text="x", people=[sam.id, alex.id]), FIXED_NOW)
    # The version's 3 stands, though it is over the lane's 2. The crew seats 3, which
    # isn't a Two players head count, so it adds nothing.
    assert head_counts_seen(db, Lane.TWO) == [3]
    assert head_counts_seen(db, Lane.FIVE_PLUS) == []


def test_normalize_name():
    assert normalize_name("  Cozy  Crawl™: The Deluxe-Edition®  ") == (
        "cozy crawl the deluxe edition"
    )
    assert normalize_name("COZY_CRAWL©") == "cozy crawl"
    assert normalize_name("™") == ""


def test_resolve_name_takes_only_an_exact_match(steam_client, fake_steam):
    fake_steam.store_hits = [
        {"id": 41, "name": "Cozy Crawl Deluxe"},
        {"id": 40, "name": "Cozy Crawl™"},
    ]
    assert resolve_name(steam_client, "cozy crawl", "us").appid == 40
    assert resolve_name(steam_client, "Cozy Crawler", "us") is None
