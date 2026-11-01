"""The records research keeps: the inventory of recommended games for each lane, the candidate
history (every proposal and the judge's verdict) and the research runs (their targets, the
user's choices and a progress log). Records are added, never edited: a change is a new row."""

from datetime import timedelta

import pytest
from fakes import FIXED_NOW, T1, T2, judged, save_facts
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    AiCall,
    Candidate,
    InventoryEntry,
    InventoryStatusChange,
    ResearchChoice,
    ResearchEvent,
    ResearchRun,
)
from app.services.game_facts import GameVersion, ensure_game
from app.services.library import set_currently_playing
from app.services.queue import (
    ACCEPTED,
    CANDIDATE_HISTORY,
    DECLINED,
    IN_PROGRESS,
    NO_VERDICT,
    OWNED_BACKLOG,
    PLAYED,
    RECOMMENDED,
    REJECTED,
    SOURCE_LABELS,
    STATUS_LABELS,
    TARGET,
    TOP_UP,
    WEB_RESEARCH,
    EntryView,
    earlier_candidates,
    excluded_appids,
    lane_entries,
    may_return,
    recommended_counts,
    rejected_in_run,
    save_judged,
    status_counts,
)
from app.services.research_runs import (
    ACTIVE,
    AUTO_ROUNDS,
    CANCELLED,
    FAILED,
    FINISHED,
    PAUSED,
    RUN_STATUS_LABELS,
    RUNNING,
    STOPPED,
    LaneTarget,
    active_run,
    add_event,
    create_run,
    lanes_short,
    past_runs,
    plan_targets,
    run_targets,
)
from app.services.reviews import Lane

# --- helpers ----------------------------------------------------------------


def save(db, run, items, *, version, lane=Lane.SOLO, room=5, round=1, now=FIXED_NOW):
    """`save_judged` with the arguments a test rarely varies filled in."""
    return save_judged(
        db,
        items,
        run_id=run.id,
        round=round,
        lane=lane,
        room=room,
        profile_version_id=version.id,
        now=now,
    )


def add_games(db, *pairs):
    for appid, name in pairs:
        ensure_game(db, appid, name, FIXED_NOW)


def fill_lane(db, run, lane, appids, version):
    """Games with these app ids recommended in `lane`, all accepted, in the order given."""
    add_games(db, *((appid, f"Crate {appid}") for appid in appids))
    save(
        db,
        run,
        [judged(appid) for appid in appids],
        version=version,
        lane=lane,
        room=len(appids),
    )


def finish(db, run, status=FINISHED):
    run.status = status
    db.commit()


def count_queries(app, work, *, reads_only=False):
    """How many SQL statements `work()` runs (only the SELECTs with `reads_only`, since an
    insert is one statement for each row however it is batched), and what it returns."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if not reads_only or statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        result = work()
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)
    return len(seen), result


def change_status(db, entry, status, when, reason=None):
    """What a later plan's status change will do: move the entry and add a record."""
    entry.status = status
    db.add(
        InventoryStatusChange(
            entry_id=entry.id, status=status, reason=reason, changed_at=when
        )
    )
    db.commit()


def fake_call(db) -> AiCall:
    call = AiCall(
        job="recommender",
        provider_kind="anthropic",
        model="sample-big",
        prompt_name="research_recommend",
        prompt_version="abc123",
        prompt_hash="0" * 64,
        request="prompt",
        response="reply",
        duration_ms=5,
        created_at=FIXED_NOW,
    )
    db.add(call)
    db.commit()
    return call


# --- the constants ------------------------------------------------------------


def test_the_fixed_numbers_and_labels():
    assert (TARGET, TOP_UP, AUTO_ROUNDS) == (20, 5, 3)
    assert (RECOMMENDED, IN_PROGRESS, PLAYED, DECLINED) == (
        "recommended",
        "in_progress",
        "played",
        "declined",
    )
    assert STATUS_LABELS == {
        "recommended": "Recommended",
        "in_progress": "In progress",
        "played": "Played",
        "declined": "Declined",
    }
    assert (WEB_RESEARCH, CANDIDATE_HISTORY, OWNED_BACKLOG) == (
        "web_research",
        "candidate_history",
        "owned_backlog",
    )
    assert SOURCE_LABELS == {
        "web_research": "Found by web research",
        "candidate_history": "From the candidate history",
        "owned_backlog": "From your backlog",
    }
    assert (ACCEPTED, REJECTED, NO_VERDICT) == ("accepted", "rejected", "no_verdict")
    assert (RUNNING, PAUSED, FINISHED, STOPPED, CANCELLED, FAILED) == (
        "running",
        "paused",
        "finished",
        "stopped",
        "cancelled",
        "failed",
    )
    assert ACTIVE == (RUNNING, PAUSED)
    assert RUN_STATUS_LABELS == {
        "running": "Running",
        "paused": "Paused",
        "finished": "Finished",
        "stopped": "Stopped",
        "cancelled": "Cancelled",
        "failed": "Failed",
    }


# --- the plan for a run ---------------------------------------------------------


def test_plan_targets():
    targets = plan_targets(
        {Lane.SOLO: 3, Lane.TWO: 20, Lane.THREE_FOUR: 25, Lane.FIVE_PLUS: 0},
        {Lane.THREE_FOUR},
    )
    assert [(t.lane, t.target) for t in targets] == [
        (Lane.SOLO, 20),
        (Lane.TWO, None),
        (Lane.THREE_FOUR, 30),
        (Lane.FIVE_PLUS, 20),
    ]
    # Review focus 4: exactly 20 is full
    assert targets[1].note == "Two players already has 20 recommended games."


def test_plan_targets_in_detail():
    counts = {Lane.SOLO: 19, Lane.TWO: 20, Lane.THREE_FOUR: 20, Lane.FIVE_PLUS: 40}
    targets = plan_targets(counts, {Lane.THREE_FOUR, Lane.SOLO})
    assert [(t.lane, t.start_count, t.target, t.add_five, t.note) for t in targets] == [
        # Under 20 fills to 20 whatever was ticked, and the tick changed nothing.
        (Lane.SOLO, 19, 20, False, None),
        (Lane.TWO, 20, None, False, "Two players already has 20 recommended games."),
        (Lane.THREE_FOUR, 20, 25, True, None),
        (
            Lane.FIVE_PLUS,
            40,
            None,
            False,
            "Five or more already has 40 recommended games.",
        ),
    ]


def test_a_lane_with_no_count_starts_at_zero():
    [solo, *_] = plan_targets({}, set())
    assert (solo.start_count, solo.target) == (0, 20)


def test_lane_targets_round_trip_through_json():
    for target in plan_targets(
        {Lane.SOLO: 3, Lane.TWO: 20, Lane.THREE_FOUR: 25, Lane.FIVE_PLUS: 0},
        {Lane.THREE_FOUR},
    ):
        data = target.as_json()
        assert data["lane"] == target.lane.value
        assert LaneTarget.from_json(data) == target
    assert isinstance(LaneTarget.from_json(data).lane, Lane)


# --- research runs ------------------------------------------------------------


def test_only_one_active_run(db):
    create_run(db, set(), FIXED_NOW)
    with pytest.raises(ValueError):
        create_run(db, set(), FIXED_NOW)


def test_a_paused_run_is_still_active(db):
    run = create_run(db, set(), FIXED_NOW)
    run.status = PAUSED
    db.commit()
    assert active_run(db).id == run.id
    with pytest.raises(ValueError):
        create_run(db, set(), FIXED_NOW)


@pytest.mark.parametrize("status", [FINISHED, STOPPED, CANCELLED, FAILED])
def test_a_run_that_is_over_makes_room_for_the_next(db, status):
    first = create_run(db, set(), FIXED_NOW)
    first.status = status
    db.commit()
    assert active_run(db) is None
    second = create_run(db, set(), FIXED_NOW)
    assert active_run(db).id == second.id


def test_no_active_run_to_begin_with(db):
    assert active_run(db) is None


def test_create_run_saves_the_run_its_targets_and_choices(db, games, solo_version):
    first = create_run(db, set(), FIXED_NOW)
    fill_lane(db, first, Lane.TWO, range(100, 120), solo_version)
    finish(db, first)

    later = FIXED_NOW + timedelta(hours=2)
    run = create_run(db, {Lane.TWO, Lane.SOLO}, later)
    db.expire_all()
    assert (run.status, run.rounds_allowed, run.rounds_done, run.cancel_requested) == (
        "running",
        3,
        0,
        False,
    )
    assert (run.created_at, run.finished_at, run.error, run.refresh_run_id) == (
        later,
        None,
        None,
        None,
    )
    assert [
        (t.lane, t.start_count, t.target, t.add_five) for t in run_targets(run)
    ] == [
        (Lane.SOLO, 0, 20, False),  # ticked, but under 20, so the tick changes nothing
        (Lane.TWO, 20, 25, True),
        (Lane.THREE_FOUR, 0, 20, False),
        (Lane.FIVE_PLUS, 0, 20, False),
    ]
    # The raw JSON is what a page or a later task reads.
    assert [t["lane"] for t in run.targets] == [lane.value for lane in Lane]
    choices = db.scalars(
        select(ResearchChoice)
        .where(ResearchChoice.run_id == run.id)
        .order_by(ResearchChoice.id)
    ).all()
    assert [(c.choice, c.lane, c.made_at) for c in choices] == [
        ("start", None, later),
        ("add_five", "solo", later),
        ("add_five", "two", later),
    ]


def test_a_start_with_no_ticks_makes_one_choice(db):
    run = create_run(db, set(), FIXED_NOW)
    [choice] = db.scalars(select(ResearchChoice)).all()
    assert (choice.run_id, choice.choice, choice.lane) == (run.id, "start", None)


def test_a_refused_start_writes_nothing(db):
    create_run(db, {Lane.SOLO}, FIXED_NOW)
    with pytest.raises(ValueError, match="already"):
        create_run(db, {Lane.TWO}, FIXED_NOW)
    assert len(db.scalars(select(ResearchRun)).all()) == 1
    assert len(db.scalars(select(ResearchChoice)).all()) == 2


def test_a_targets_list_changed_in_place_is_saved_when_reassigned(db, research_run):
    run = research_run
    targets = run_targets(run)
    targets[0].target = 40
    run.targets = [t.as_json() for t in targets]
    db.commit()
    db.expire_all()
    assert run_targets(db.get(ResearchRun, run.id))[0].target == 40


def test_add_event_commits_and_keeps_the_order(db, research_run):
    add_event(db, research_run.id, "round", "Round 1 started.", FIXED_NOW, round=1)
    add_event(
        db,
        research_run.id,
        "search",
        "Searching.",
        FIXED_NOW + timedelta(seconds=1),
        round=1,
        lane=Lane.SOLO,
    )
    add_event(db, research_run.id, "note", "Plain.", FIXED_NOW)
    db.rollback()  # whatever was not committed would go now
    events = db.scalars(
        select(ResearchEvent)
        .where(ResearchEvent.run_id == research_run.id)
        .order_by(ResearchEvent.id)
    ).all()
    assert [(e.round, e.lane, e.step, e.message) for e in events] == [
        (1, None, "round", "Round 1 started."),
        (1, "solo", "search", "Searching."),
        (None, None, "note", "Plain."),
    ]
    assert events[1].created_at == FIXED_NOW + timedelta(seconds=1)


def test_lanes_short(db, games, solo_version, research_run):
    # Two players is full, so the next run leaves it out.
    fill_lane(db, research_run, Lane.TWO, range(100, 120), solo_version)
    finish(db, research_run)
    run = create_run(db, set(), FIXED_NOW)
    assert [t.target for t in run_targets(run)] == [20, None, 20, 20]
    assert lanes_short(db, run) == [Lane.SOLO, Lane.THREE_FOUR, Lane.FIVE_PLUS]

    fill_lane(db, run, Lane.SOLO, range(200, 219), solo_version)
    assert lanes_short(db, run)[0] is Lane.SOLO  # 19 of 20
    fill_lane(db, run, Lane.SOLO, [219], solo_version)
    assert lanes_short(db, run) == [Lane.THREE_FOUR, Lane.FIVE_PLUS]


def test_a_top_up_lane_is_short_until_it_has_five_more(db, games, solo_version):
    first = create_run(db, set(), FIXED_NOW)
    fill_lane(db, first, Lane.SOLO, range(100, 120), solo_version)
    finish(db, first)
    run = create_run(db, {Lane.SOLO}, FIXED_NOW)
    assert lanes_short(db, run)[0] is Lane.SOLO  # 20 of 25
    fill_lane(db, run, Lane.SOLO, range(120, 124), solo_version)
    assert lanes_short(db, run)[0] is Lane.SOLO  # 24 of 25
    fill_lane(db, run, Lane.SOLO, [124], solo_version)
    assert Lane.SOLO not in lanes_short(db, run)  # 25 of 25


def test_past_runs_count_what_each_run_judged(db, games, solo_version):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"), (50, "Party Pile"))
    first = create_run(db, set(), FIXED_NOW)
    save(
        db,
        first,
        [judged(40), judged(30), judged(20, "rejected")],
        version=solo_version,
        room=1,
    )
    save(
        db,
        first,
        [judged(50, "no_verdict")],
        version=solo_version,
        round=2,
    )
    first.status = FINISHED
    db.commit()
    second = create_run(db, set(), FIXED_NOW + timedelta(hours=1))
    save(db, second, [judged(10, "rejected")], version=solo_version)
    second.status = STOPPED
    db.commit()
    third = create_run(db, set(), FIXED_NOW + timedelta(hours=2))  # nothing judged yet

    summaries = past_runs(db)
    assert [s.run.id for s in summaries] == [third.id, second.id, first.id]
    assert [(s.accepted, s.rejected, s.added) for s in summaries] == [
        (0, 0, 0),
        (0, 1, 0),
        (2, 2, 1),  # 40 and 30 accepted, 20 rejected and 50 without a verdict
    ]


def test_past_runs_has_a_limit_and_a_fixed_number_of_queries(app, db):
    for _ in range(4):
        run = create_run(db, set(), FIXED_NOW)
        run.status = FINISHED
        db.commit()
    few, listed = count_queries(app, lambda: past_runs(db, limit=2))
    assert len(listed) == 2
    many, listed = count_queries(app, lambda: past_runs(db))
    assert len(listed) == 4
    assert few == many == 2  # the runs, then one query for every count


def test_past_runs_with_no_runs(db):
    assert past_runs(db) == []


# --- save_judged ----------------------------------------------------------------


def test_save_judged_adds_only_what_there_is_room_for(
    db, games, solo_version, research_run
):  # Review focus 4
    for appid, name in ((30, "Demo Dungeon"), (40, "Cozy Crawl")):
        ensure_game(db, appid, name, FIXED_NOW)
    saved = save_judged(
        db,
        [judged(40), judged(20, "rejected", "Too long."), judged(30)],
        run_id=research_run.id,
        round=1,
        lane=Lane.SOLO,
        room=1,
        profile_version_id=solo_version.id,
        now=FIXED_NOW,
    )
    assert [(c.appid, c.verdict, c.added) for c in saved] == [
        (40, "accepted", True),
        (20, "rejected", False),
        (30, "accepted", False),
    ]
    assert recommended_counts(db)[Lane.SOLO] == 1
    [change] = db.scalars(select(InventoryStatusChange)).all()
    assert (change.status, change.candidate_id) == ("recommended", saved[0].id)


def test_save_judged_keeps_everything_about_a_proposal(
    db, games, solo_version, research_run
):
    add_games(db, (40, "Cozy Crawl"))
    recommender, judge = fake_call(db), fake_call(db)
    version = GameVersion(True, T1)
    facts = {"store": {"data": {"type": "game"}, "fetched_at": "x", "source_url": None}}
    [saved] = save_judged(
        db,
        [
            judged(
                40,
                source="candidate_history",
                version=version,
                facts=facts,
                list_rejected=True,
                recommender_call_id=recommender.id,
                judge_call_id=judge.id,
            )
        ],
        run_id=research_run.id,
        round=2,
        lane=Lane.TWO,
        room=5,
        profile_version_id=solo_version.id,
        now=FIXED_NOW + timedelta(minutes=3),
    )
    db.expire_all()
    row = db.get(Candidate, saved.id)
    assert (row.run_id, row.round, row.lane, row.appid) == (
        research_run.id,
        2,
        "two",
        40,
    )
    assert row.source == "candidate_history"
    assert row.fits == judged(40).fits
    assert row.risk == "Might be too light."
    assert GameVersion.from_json(row.version) == version
    assert row.facts == facts
    assert row.profile_version_id == solo_version.id
    assert (row.verdict, row.reason) == ("accepted", "Fits short sessions.")
    assert (row.list_rejected, row.added) == (True, True)
    assert (row.recommender_call_id, row.judge_call_id) == (recommender.id, judge.id)
    assert row.created_at == FIXED_NOW + timedelta(minutes=3)


def test_the_entry_points_at_the_candidate_that_got_it_in(
    db, games, solo_version, research_run
):
    add_games(db, (40, "Cozy Crawl"))
    [saved] = save(
        db, research_run, [judged(40)], version=solo_version, lane=Lane.THREE_FOUR
    )
    [entry] = db.scalars(select(InventoryEntry)).all()
    assert (entry.lane, entry.appid, entry.status, entry.candidate_id) == (
        "three_four",
        40,
        "recommended",
        saved.id,
    )
    assert entry.created_at == FIXED_NOW
    [change] = db.scalars(select(InventoryStatusChange)).all()
    assert (change.entry_id, change.reason, change.changed_at) == (
        entry.id,
        None,
        FIXED_NOW,
    )


def test_entries_are_ranked_after_the_highest_rank_in_order(
    db, games, solo_version, research_run
):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"), (50, "Party Pile"))
    save(db, research_run, [judged(40), judged(30)], version=solo_version)
    save(db, research_run, [judged(50)], version=solo_version, round=2)
    # Another lane starts from 0 on its own.
    save(db, research_run, [judged(40)], version=solo_version, lane=Lane.TWO)
    ranks = {
        (e.lane, e.appid): e.rank for e in db.scalars(select(InventoryEntry)).all()
    }
    assert ranks == {
        ("solo", 40): 0,
        ("solo", 30): 1,
        ("solo", 50): 2,
        ("two", 40): 0,
    }


def test_a_rank_follows_the_highest_even_when_that_game_is_no_longer_recommended(
    db, games, solo_version, research_run
):
    add_games(db, (40, "Cozy Crawl"), (50, "Party Pile"))
    save(db, research_run, [judged(40), judged(50)], version=solo_version)
    top = db.scalar(select(InventoryEntry).where(InventoryEntry.appid == 50))
    change_status(db, top, "played", FIXED_NOW)
    add_games(db, (60, "Old Crate"))
    save(db, research_run, [judged(60)], version=solo_version, round=2)
    assert db.scalar(select(InventoryEntry.rank).where(InventoryEntry.appid == 60)) == 2


def test_room_runs_out_in_order_and_zero_room_adds_nothing(
    db, games, solo_version, research_run
):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"))
    none = save(
        db, research_run, [judged(40), judged(30)], version=solo_version, room=0
    )
    assert [c.added for c in none] == [False, False]
    assert db.scalars(select(InventoryEntry)).all() == []
    assert db.scalars(select(InventoryStatusChange)).all() == []
    assert len(db.scalars(select(Candidate)).all()) == 2
    # A negative room is no room either.
    save(db, research_run, [judged(40)], version=solo_version, room=-3, round=2)
    assert db.scalars(select(InventoryEntry)).all() == []


@pytest.mark.parametrize("verdict", ["rejected", "no_verdict"])
def test_only_an_accepted_proposal_is_added(
    db, games, solo_version, research_run, verdict
):
    [saved] = save(db, research_run, [judged(20, verdict)], version=solo_version)
    assert saved.added is False
    assert db.scalars(select(InventoryEntry)).all() == []


def test_a_game_that_came_back_moves_to_recommended_on_the_new_candidate(
    db, games, solo_version, research_run
):
    add_games(db, (40, "Cozy Crawl"), (50, "Party Pile"))
    [first] = save(db, research_run, [judged(40)], version=solo_version)
    [entry] = db.scalars(select(InventoryEntry)).all()
    change_status(db, entry, DECLINED, FIXED_NOW + timedelta(days=1), "Not for me.")

    later = FIXED_NOW + timedelta(days=2)
    save(db, research_run, [judged(50)], version=solo_version, round=2, now=later)
    [again] = save(
        db,
        research_run,
        [judged(40, reason="Back with a patch.")],
        version=solo_version,
        round=3,
        now=later,
    )
    db.expire_all()
    entries = db.scalars(select(InventoryEntry).order_by(InventoryEntry.id)).all()
    assert len(entries) == 2  # the same entry, not a second one
    assert (entries[0].id, entries[0].status, entries[0].candidate_id) == (
        entry.id,
        "recommended",
        again.id,
    )
    assert entries[0].rank > entries[1].rank  # it joins the end of the line
    assert entries[0].created_at == FIXED_NOW  # it is the same entry
    history = db.scalars(
        select(InventoryStatusChange)
        .where(InventoryStatusChange.entry_id == entry.id)
        .order_by(InventoryStatusChange.id)
    ).all()
    assert [(c.status, c.candidate_id, c.reason) for c in history] == [
        ("recommended", first.id, None),
        ("declined", None, "Not for me."),
        ("recommended", again.id, None),
    ]
    assert recommended_counts(db)[Lane.SOLO] == 2


def test_the_same_game_twice_in_one_batch_makes_one_entry(
    db, games, solo_version, research_run
):
    add_games(db, (40, "Cozy Crawl"))
    first, second = save(
        db, research_run, [judged(40), judged(40)], version=solo_version
    )
    [entry] = db.scalars(select(InventoryEntry)).all()
    assert entry.candidate_id == second.id
    assert (first.added, second.added) == (True, True)
    assert len(db.scalars(select(InventoryStatusChange)).all()) == 2


def test_save_judged_commits_once(app, db, games, solo_version, research_run):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"))
    commits = []
    event.listen(app.state.engine, "commit", lambda conn: commits.append(1))
    save(
        db,
        research_run,
        [judged(40), judged(30), judged(20, "rejected")],
        version=solo_version,
    )
    assert len(commits) == 1


def test_a_failed_save_writes_nothing(db, games, solo_version, research_run):
    # 999 is not a game, so the whole batch is refused and the good proposal with it.
    with pytest.raises(IntegrityError):
        save(
            db,
            research_run,
            [judged(20, "rejected"), judged(999)],
            version=solo_version,
        )
    db.rollback()
    assert db.scalars(select(Candidate)).all() == []
    assert db.scalars(select(InventoryEntry)).all() == []


def test_save_judged_with_nothing_to_save(db, solo_version, research_run):
    assert save(db, research_run, [], version=solo_version) == []


def test_save_judged_looks_things_up_a_fixed_number_of_times(
    app, db, games, solo_version, research_run
):
    for appid in range(100, 112):
        ensure_game(db, appid, f"Crate {appid}", FIXED_NOW)
    few, _ = count_queries(
        app,
        lambda: save(db, research_run, [judged(100)], version=solo_version),
        reads_only=True,
    )
    many, saved = count_queries(
        app,
        lambda: save(
            db,
            research_run,
            [judged(appid) for appid in range(101, 112)],
            version=solo_version,
            room=20,
            round=2,
        ),
        reads_only=True,
    )
    assert many == few
    assert len(saved) == 11
    # Reading what came back costs nothing more.
    more, _ = count_queries(app, lambda: [(c.appid, c.verdict, c.added) for c in saved])
    assert more == 0


def test_unknown_lane_is_refused(db, solo_version, research_run):
    with pytest.raises(ValueError):
        save(db, research_run, [judged(20)], version=solo_version, lane="duo")


# --- counts -----------------------------------------------------------------------


def test_counts_cover_every_lane_and_status(db, games, solo_version, research_run):
    assert recommended_counts(db) == {lane: 0 for lane in Lane}
    assert list(recommended_counts(db)) == list(Lane)
    assert status_counts(db, Lane.SOLO) == {
        "recommended": 0,
        "in_progress": 0,
        "played": 0,
        "declined": 0,
    }
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"), (50, "Party Pile"))
    save(db, research_run, [judged(40), judged(30), judged(50)], version=solo_version)
    save(db, research_run, [judged(40)], version=solo_version, lane=Lane.TWO)
    entries = {
        e.appid: e
        for e in db.scalars(select(InventoryEntry).where(InventoryEntry.lane == "solo"))
    }
    change_status(db, entries[30], PLAYED, FIXED_NOW)
    change_status(db, entries[50], DECLINED, FIXED_NOW)
    assert status_counts(db, Lane.SOLO) == {
        "recommended": 1,
        "in_progress": 0,
        "played": 1,
        "declined": 1,
    }
    assert recommended_counts(db) == {
        Lane.SOLO: 1,
        Lane.TWO: 1,
        Lane.THREE_FOUR: 0,
        Lane.FIVE_PLUS: 0,
    }
    # Only the Solo lane's entries were counted for Solo.
    assert sum(status_counts(db, Lane.TWO).values()) == 1


# --- which games are left out of a search ------------------------------------------


def test_excluded_appids(db, games, solo_version, research_run):
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    set_currently_playing(db, 20, True, FIXED_NOW)
    save_judged(
        db,
        [judged(40)],
        run_id=research_run.id,
        round=1,
        lane=Lane.SOLO,
        room=5,
        profile_version_id=solo_version.id,
        now=FIXED_NOW,
    )
    assert excluded_appids(db, Lane.SOLO) == {
        10,
        20,
        40,
    }  # reviewed in Solo, playing, in the inventory
    assert excluded_appids(db, Lane.TWO) == {20}


def test_every_inventory_status_is_left_out(db, games, solo_version, research_run):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"), (50, "Party Pile"))
    save(
        db,
        research_run,
        [judged(30), judged(40), judged(50)],
        version=solo_version,
        lane=Lane.TWO,
    )
    for appid, status in ((30, PLAYED), (40, DECLINED), (50, IN_PROGRESS)):
        entry = db.scalar(select(InventoryEntry).where(InventoryEntry.appid == appid))
        change_status(db, entry, status, FIXED_NOW)
    assert excluded_appids(db, Lane.TWO) == {30, 40, 50}


def test_a_candidate_that_was_not_added_is_not_excluded(
    db, games, solo_version, research_run
):
    # The history is how a rejected game comes back, so it does not block the game.
    save(
        db, research_run, [judged(20, "rejected")], version=solo_version, lane=Lane.TWO
    )
    assert excluded_appids(db, Lane.TWO) == set()


def test_a_draft_review_does_not_exclude_a_game(db, games, solo_version):
    from app.services.reviews import ReviewForm, save_draft

    save_draft(db, 20, Lane.SOLO, ReviewForm(rating=5, text="Half done"), FIXED_NOW)
    assert excluded_appids(db, Lane.SOLO) == {10}


def test_excluded_appids_is_one_query(app, db, games, solo_version):
    queries, _ = count_queries(app, lambda: excluded_appids(db, Lane.SOLO))
    assert queries == 1


# --- the history of a run and of earlier runs ----------------------------------------


def test_rejected_in_run_is_this_runs_non_accepted_candidates_oldest_first(
    db, games, solo_version
):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"), (50, "Party Pile"))
    mine = create_run(db, set(), FIXED_NOW)
    save(
        db,
        mine,
        [judged(20, "rejected", "Too long."), judged(40)],
        version=solo_version,
    )
    save(
        db,
        mine,
        [judged(30, "no_verdict", "")],
        version=solo_version,
        round=2,
        now=FIXED_NOW + timedelta(minutes=1),
    )
    save(db, mine, [judged(50, "rejected")], version=solo_version, lane=Lane.TWO)
    mine.status = FINISHED
    db.commit()
    other = create_run(db, set(), FIXED_NOW)
    save(db, other, [judged(10, "rejected")], version=solo_version)

    found = rejected_in_run(db, mine.id, Lane.SOLO)
    assert [(c.appid, c.verdict) for c in found] == [
        (20, "rejected"),
        (30, "no_verdict"),
    ]
    assert rejected_in_run(db, mine.id, Lane.FIVE_PLUS) == []


def test_earlier_candidates_are_the_newest_per_game_from_other_runs(
    db, games, solo_version
):
    first = create_run(db, set(), FIXED_NOW)
    for rnd, reason in ((1, "Old reason"), (2, "New reason")):
        save_judged(
            db,
            [judged(20, "rejected", reason)],
            run_id=first.id,
            round=rnd,
            lane=Lane.SOLO,
            room=5,
            profile_version_id=solo_version.id,
            now=FIXED_NOW,
        )
    first.status = "finished"
    db.commit()
    second = create_run(db, set(), FIXED_NOW)
    assert [
        (c.appid, c.reason) for c in earlier_candidates(db, Lane.SOLO, second.id)
    ] == [(20, "New reason")]
    assert earlier_candidates(db, Lane.SOLO, first.id) == []


def test_earlier_candidates_come_newest_first_from_the_lane_asked_for(
    db, games, solo_version
):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"))
    first = create_run(db, set(), FIXED_NOW)
    save(
        db,
        first,
        [judged(20, "rejected", "Oldest")],
        version=solo_version,
        now=FIXED_NOW,
    )
    save(
        db,
        first,
        [judged(30, "rejected", "Newest")],
        version=solo_version,
        round=2,
        now=FIXED_NOW + timedelta(hours=2),
    )
    save(
        db,
        first,
        [judged(40, "rejected", "Middle")],
        version=solo_version,
        round=2,
        now=FIXED_NOW + timedelta(hours=1),
    )
    save(db, first, [judged(10, "rejected")], version=solo_version, lane=Lane.TWO)
    first.status = FINISHED
    db.commit()
    second = create_run(db, set(), FIXED_NOW)
    assert [c.reason for c in earlier_candidates(db, Lane.SOLO, second.id)] == [
        "Newest",
        "Middle",
        "Oldest",
    ]
    assert [c.appid for c in earlier_candidates(db, Lane.TWO, second.id)] == [10]


def test_the_newest_candidate_of_a_game_can_be_in_another_run_than_its_first(
    db, games, solo_version
):
    first = create_run(db, set(), FIXED_NOW)
    save(
        db, first, [judged(20, "rejected", "From the first run")], version=solo_version
    )
    first.status = FINISHED
    db.commit()
    second = create_run(db, set(), FIXED_NOW + timedelta(days=1))
    save(
        db,
        second,
        [judged(20, "rejected", "From the second run")],
        version=solo_version,
        now=FIXED_NOW + timedelta(days=1),
    )
    second.status = FINISHED
    db.commit()
    third = create_run(db, set(), FIXED_NOW + timedelta(days=2))
    assert [c.reason for c in earlier_candidates(db, Lane.SOLO, third.id)] == [
        "From the second run"
    ]
    # From the second run's point of view, the first run's candidate is the only one.
    assert [c.reason for c in earlier_candidates(db, Lane.SOLO, second.id)] == [
        "From the first run"
    ]


def test_history_reads_are_one_query_each(app, db, games, solo_version, research_run):
    run_id = research_run.id
    save(db, research_run, [judged(20, "rejected")], version=solo_version)
    assert count_queries(app, lambda: rejected_in_run(db, run_id, Lane.SOLO))[0] == 1
    assert count_queries(app, lambda: earlier_candidates(db, Lane.SOLO, 999))[0] == 1


# --- may a game come back ----------------------------------------------------------


@pytest.mark.parametrize(
    "verdict,added,now_version,profile_now,back",
    [
        ("rejected", False, GameVersion(False, T1), 1, False),
        ("rejected", False, GameVersion(False, T2), 1, True),  # patched since
        ("rejected", False, GameVersion(False, T1), 2, True),  # the profile is newer
        ("no_verdict", False, GameVersion(False, T1), 2, True),
        (
            "accepted",
            False,
            GameVersion(False, T1),
            1,
            True,
        ),  # accepted when the lane had no room
        ("accepted", True, GameVersion(False, T2), 2, False),  # it is in the inventory
    ],
)
def test_may_return(verdict, added, now_version, profile_now, back):
    c = Candidate(
        verdict=verdict,
        added=added,
        version=GameVersion(False, T1).as_json(),
        profile_version_id=1,
    )
    assert may_return(c, now_version, profile_now) is back


def test_may_return_when_the_lane_has_no_profile_version():
    c = Candidate(
        verdict="rejected",
        added=False,
        version=GameVersion(False, T1).as_json(),
        profile_version_id=1,
    )
    assert may_return(c, GameVersion(False, T1), None) is False
    assert may_return(c, GameVersion(False, T2), None) is True


def test_may_return_when_the_game_left_early_access():
    c = Candidate(
        verdict="rejected",
        added=False,
        version=GameVersion(True, T1).as_json(),
        profile_version_id=3,
    )
    assert may_return(c, GameVersion(False, T1), 3) is True
    assert may_return(c, GameVersion(True, T1), 3) is False
    assert (
        may_return(c, GameVersion(True, T1), 2) is False
    )  # an older profile is not newer


def test_may_return_reads_a_version_kept_as_json_from_a_game_with_no_patch():
    c = Candidate(
        verdict="rejected",
        added=False,
        version=GameVersion(None, None).as_json(),
        profile_version_id=1,
    )
    assert may_return(c, GameVersion(None, None), 1) is False
    assert may_return(c, GameVersion(None, T2), 1) is True


# --- the inventory as the pages show it ------------------------------------------


def test_recommended_puts_owned_and_free_games_first(
    db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl", price_cents=999)
    save_facts(db, 50, "Party Pile", is_free=True)
    for appid, name in ((40, "Cozy Crawl"), (50, "Party Pile")):
        ensure_game(db, appid, name, FIXED_NOW)
    save_judged(
        db,
        [judged(40), judged(50), judged(20)],
        run_id=research_run.id,
        round=1,
        lane=Lane.SOLO,
        room=5,
        profile_version_id=solo_version.id,
        now=FIXED_NOW,
    )
    assert [v.game.appid for v in lane_entries(db, Lane.SOLO, "recommended")] == [
        50,
        20,
        40,
    ]


def test_an_entry_view_has_everything_a_card_shows(
    db, games, solo_version, research_run
):
    facts = save_facts(db, 40, "Cozy Crawl", price_cents=999)
    [saved] = save(db, research_run, [judged(40)], version=solo_version)
    [view] = lane_entries(db, Lane.SOLO, RECOMMENDED)
    assert isinstance(view, EntryView)
    assert (view.entry.appid, view.entry.status) == (40, "recommended")
    assert view.game.name == "Cozy Crawl"
    assert view.candidate.id == saved.id
    assert view.facts.price_text == facts.price_text == "$9.99"
    assert view.costs_money is True


def test_costs_money_is_for_games_that_are_not_owned_and_not_free(
    db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl", price_cents=999)
    save_facts(db, 50, "Party Pile", is_free=True)
    save_facts(
        db, 20, "Test Tactics", price_cents=999
    )  # owned, so it costs nothing more
    add_games(db, (60, "Old Crate"))  # no facts at all: not known to be free
    save(db, research_run, [judged(a) for a in (40, 50, 20, 60)], version=solo_version)
    costs = {
        v.game.appid: v.costs_money for v in lane_entries(db, Lane.SOLO, RECOMMENDED)
    }
    assert costs == {40: True, 50: False, 20: False, 60: True}


def test_recommended_keeps_rank_order_within_owned_and_paid(
    db, games, solo_version, research_run
):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"), (50, "Party Pile"))
    save_facts(db, 40, "Cozy Crawl", price_cents=999)
    save_facts(db, 50, "Party Pile", price_cents=999)
    save(db, research_run, [judged(a) for a in (50, 40, 20, 10)], version=solo_version)
    order = [v.game.appid for v in lane_entries(db, Lane.SOLO, RECOMMENDED)]
    assert order == [20, 10, 50, 40]


def test_other_statuses_come_newest_change_first(db, games, solo_version, research_run):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"), (50, "Party Pile"))
    save(db, research_run, [judged(a) for a in (30, 40, 50, 20)], version=solo_version)
    by_appid = {e.appid: e for e in db.scalars(select(InventoryEntry))}
    day = timedelta(days=1)
    change_status(db, by_appid[30], PLAYED, FIXED_NOW + day)
    change_status(db, by_appid[40], PLAYED, FIXED_NOW + 3 * day)
    change_status(db, by_appid[50], PLAYED, FIXED_NOW + 2 * day)
    change_status(db, by_appid[20], DECLINED, FIXED_NOW + 9 * day)
    assert [v.game.appid for v in lane_entries(db, Lane.SOLO, PLAYED)] == [40, 50, 30]
    assert [v.game.appid for v in lane_entries(db, Lane.SOLO, DECLINED)] == [20]
    assert lane_entries(db, Lane.SOLO, IN_PROGRESS) == []
    assert lane_entries(db, Lane.SOLO, RECOMMENDED) == []


def test_a_game_moved_twice_is_ordered_by_its_latest_change(
    db, games, solo_version, research_run
):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"))
    save(db, research_run, [judged(30), judged(40)], version=solo_version)
    by_appid = {e.appid: e for e in db.scalars(select(InventoryEntry))}
    day = timedelta(days=1)
    change_status(db, by_appid[30], IN_PROGRESS, FIXED_NOW + day)
    change_status(db, by_appid[40], IN_PROGRESS, FIXED_NOW + 2 * day)
    change_status(db, by_appid[30], PLAYED, FIXED_NOW + 3 * day)
    change_status(db, by_appid[30], IN_PROGRESS, FIXED_NOW + 4 * day)
    assert [v.game.appid for v in lane_entries(db, Lane.SOLO, IN_PROGRESS)] == [30, 40]


def test_changes_at_the_same_moment_keep_the_later_one_first(
    db, games, solo_version, research_run
):
    add_games(db, (30, "Demo Dungeon"), (40, "Cozy Crawl"))
    save(db, research_run, [judged(30), judged(40)], version=solo_version)
    by_appid = {e.appid: e for e in db.scalars(select(InventoryEntry))}
    change_status(db, by_appid[30], PLAYED, FIXED_NOW)
    change_status(db, by_appid[40], PLAYED, FIXED_NOW)
    assert [v.game.appid for v in lane_entries(db, Lane.SOLO, PLAYED)] == [40, 30]


def test_lane_entries_are_for_one_lane(db, games, solo_version, research_run):
    save(db, research_run, [judged(20)], version=solo_version, lane=Lane.TWO)
    save(db, research_run, [judged(10)], version=solo_version, lane=Lane.SOLO)
    assert [v.game.appid for v in lane_entries(db, Lane.SOLO, RECOMMENDED)] == [10]
    assert [v.game.appid for v in lane_entries(db, Lane.TWO, RECOMMENDED)] == [20]


def test_lane_entries_use_a_fixed_number_of_queries(
    app, db, games, solo_version, research_run
):
    for appid in range(100, 110):
        save_facts(db, appid, f"Crate {appid}", price_cents=500 + appid)
    save(db, research_run, [judged(100)], version=solo_version)
    few, first = count_queries(app, lambda: lane_entries(db, Lane.SOLO, RECOMMENDED))
    assert len(first) == 1
    save(
        db,
        research_run,
        [judged(a) for a in range(101, 110)],
        version=solo_version,
        room=20,
        round=2,
    )
    moved = db.scalars(select(InventoryEntry).where(InventoryEntry.appid > 105)).all()
    for entry in moved:
        change_status(db, entry, PLAYED, FIXED_NOW)
    db.expire_all()

    def read():
        views = lane_entries(db, Lane.SOLO, RECOMMENDED)
        # Reading what a card shows costs nothing more.
        return views, [
            (
                v.entry.rank,
                v.game.name,
                v.candidate.risk,
                v.facts.price_text,
                v.costs_money,
            )
            for v in views
        ]

    many, (views, shown) = count_queries(app, read)
    assert len(views) == 6 and len(shown) == 6
    assert many == few
    other, (played, _) = count_queries(
        app, lambda: (lane_entries(db, Lane.SOLO, PLAYED), None)
    )
    assert len(played) == 4
    assert other == few


# --- the tables themselves ------------------------------------------------------------


def test_a_game_has_one_entry_per_lane(db, games, solo_version, research_run):
    [saved] = save(db, research_run, [judged(20)], version=solo_version)
    save(
        db, research_run, [judged(20)], version=solo_version, lane=Lane.TWO
    )  # another lane
    db.add(
        InventoryEntry(
            lane="solo",
            appid=20,
            status="recommended",
            rank=9,
            candidate_id=saved.id,
            created_at=FIXED_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_a_candidate_names_a_real_game_run_and_profile_version(
    db, games, solo_version, research_run
):
    def candidate(**over):
        base = {
            "run_id": research_run.id,
            "round": 1,
            "lane": "solo",
            "appid": 20,
            "source": "web_research",
            "fits": [],
            "risk": "",
            "version": {},
            "facts": {},
            "profile_version_id": solo_version.id,
            "verdict": "rejected",
            "reason": "",
            "created_at": FIXED_NOW,
        }
        return Candidate(**(base | over))

    for bad in ({"appid": 999}, {"run_id": 999}, {"profile_version_id": 999}):
        db.add(candidate(**bad))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    db.add(candidate())
    db.commit()
    row = db.scalar(select(Candidate))
    assert (row.list_rejected, row.added) == (False, False)
    assert (row.recommender_call_id, row.judge_call_id) == (None, None)


def test_the_database_fills_in_the_counters_and_flags(db, games, solo_version):
    # A row written without them (as an older version of the app would) still reads right.
    db.execute(
        text(
            "insert into research_run (status, targets, created_at) "
            "values ('running', '[]', '2026-01-15 12:00:00')"
        )
    )
    db.commit()
    run = db.scalar(select(ResearchRun))
    assert (run.rounds_allowed, run.rounds_done, run.cancel_requested) == (3, 0, False)
    db.execute(
        text(
            "insert into candidate (run_id, round, lane, appid, source, fits, risk, version, "
            "facts, profile_version_id, verdict, reason, created_at) values "
            f"({run.id}, 1, 'solo', 20, 'web_research', '[]', '', '{{}}', '{{}}', "
            f"{solo_version.id}, 'rejected', '', '2026-01-15 12:00:00')"
        )
    )
    db.commit()
    candidate = db.scalar(select(Candidate))
    assert (candidate.list_rejected, candidate.added) == (False, False)
