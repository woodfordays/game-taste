"""Research runs as jobs: the start checks, the first segment's refresh and notes, three
rounds then a pause, one more round per continue, stop, cancel, failures and a restart."""

import threading
import time

import pytest
from fakes import (
    FIXED_NOW,
    fill_lane,
    messages,
    profile_reply,
    two_version,
    verdicts,
)
from sqlalchemy import delete, func, select, update

import app.routines.research as research_module
from app.jobs import RESEARCH_KIND, run_pending_jobs
from app.main import create_app
from app.models import (
    Job,
    JobAssignment,
    RefreshRun,
    ResearchChoice,
    ResearchEvent,
    ResearchRun,
)
from app.services import research_runs
from app.services.ai import StructuredReplyError
from app.services.queue import recommended_counts
from app.services.research_runs import (
    ResearchError,
    cancel_research,
    continue_research,
    recover_interrupted_research,
    research_blockers,
    run_targets,
    start_research,
    stop_research,
)
from app.services.reviews import Lane, ReviewForm, publish
from app.services.settings import delete_setting

CANCELLED_KEPT = "Cancelled. Games the judge accepted before this were kept."
RUNNING_REFUSAL = (
    "Research is already running. Wait for it to pause or finish, or cancel it."
)


def research(app, db, add_five=()):
    run = start_research(db, app.state.vault, set(add_five), FIXED_NOW)
    run_pending_jobs(app)
    db.expire_all()
    return db.get(ResearchRun, run.id)


def choices(db, run):
    return list(
        db.scalars(
            select(ResearchChoice.choice)
            .where(ResearchChoice.run_id == run.id)
            .order_by(ResearchChoice.id)
        )
    )


def research_jobs(db):
    return list(
        db.scalars(select(Job.status).where(Job.kind == RESEARCH_KIND).order_by(Job.id))
    )


# --- the plan's tests ----------------------------------------------------------------


def test_start_needs_every_model_and_tavily(db, app, solo_version):
    with pytest.raises(ResearchError) as err:
        start_research(db, app.state.vault, set(), FIXED_NOW)
    assert (
        err.value.message
        == "Set up the Recommender model in Settings, then run research again."
    )


def test_two_starts_give_one_run_and_one_job(db, app, research_world):  # Review focus 4
    start_research(db, app.state.vault, set(), FIXED_NOW)
    with pytest.raises(ResearchError, match="Research is already running."):
        start_research(db, app.state.vault, set(), FIXED_NOW)
    assert (
        db.scalar(select(func.count()).select_from(Job).where(Job.kind == "research"))
        == 1
    )
    assert [c.choice for c in db.scalars(select(ResearchChoice))] == ["start"]


def test_profiles_are_refreshed_before_research(app, db, research_world, fake_ai):
    publish(db, 20, Lane.SOLO, ReviewForm(rating=6, text="Too slow."), FIXED_NOW)
    run = research(app, db)
    assert run.refresh_run_id == db.scalar(
        select(RefreshRun.id).where(
            RefreshRun.job_id == db.scalar(select(Job.id).where(Job.kind == "research"))
        )
    )
    prompts = [p for _, _, p in fake_ai.calls]
    assert prompts.index(next(p for p in prompts if "Too slow." in p)) < prompts.index(
        next(p for p in prompts if p.startswith("# research_plan"))
    )


def test_lanes_without_a_profile_are_skipped_with_a_message(app, db, research_world):
    run = research(app, db)
    assert (
        "No profile yet for Three to four. Publish a review played at this head count first."
        in messages(db, run)
    )


def test_a_full_lane_is_left_out_unless_ticked(app, db, research_world, solo_version):
    fill_lane(db, Lane.SOLO, 20, solo_version)
    run = research(app, db)
    assert (
        "Solo already has 20 recommended games." in messages(db, run)
        and run.status == "finished"
    )
    run = research(app, db, add_five=[Lane.SOLO])
    assert run_targets(run)[0].target == 25


def test_pauses_after_three_rounds_and_each_continue_runs_one(
    app, db, research_world
):  # Review focus 4
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    assert (run.status, run.rounds_done) == ("paused", 3)
    assert "Paused after round 3. Still short: Solo." in messages(db, run)
    assert continue_research(db, run.id, FIXED_NOW) is True
    assert continue_research(db, run.id, FIXED_NOW) is False
    run_pending_jobs(app)
    db.expire_all()
    assert (
        db.get(ResearchRun, run.id).status,
        db.get(ResearchRun, run.id).rounds_done,
    ) == ("paused", 4)


def test_finishes_when_the_lane_is_full(app, db, research_world, solo_version):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    run = research(app, db)
    assert (run.status, run.finished_at) == ("finished", FIXED_NOW)
    assert recommended_counts(db)[Lane.SOLO] == 20


def test_stop_and_cancel_a_paused_run(app, db, research_world):  # Review focus 3
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    assert (
        stop_research(db, run.id, FIXED_NOW)
        and db.get(ResearchRun, run.id).status == "stopped"
    )
    run = research(app, db)
    assert (
        cancel_research(db, run.id, FIXED_NOW)
        and db.get(ResearchRun, run.id).status == "cancelled"
    )
    assert [
        c.choice for c in db.scalars(select(ResearchChoice).order_by(ResearchChoice.id))
    ][-1] == "cancel"


def test_a_cancel_before_the_job_starts_makes_no_calls(
    app, db, research_world, fake_ai
):  # Review focus 3
    run = start_research(db, app.state.vault, set(), FIXED_NOW)
    cancel_research(db, run.id, FIXED_NOW)
    run_pending_jobs(app)
    db.expire_all()
    assert db.get(ResearchRun, run.id).status == "cancelled" and fake_ai.calls == []


def test_a_restart_mid_run_pauses_it(tmp_path, services):  # Review focus 3
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        run = ResearchRun(
            status="running", targets=[], rounds_done=1, created_at=FIXED_NOW
        )
        s.add_all(
            [
                run,
                Job(
                    kind="research",
                    status="running",
                    requested_by="user",
                    created_at=FIXED_NOW,
                ),
            ]
        )
        s.commit()
        run_id = run.id
    app1.state.engine.dispose()
    app2 = create_app(tmp_path, services=services)
    try:
        with app2.state.session_factory() as s:
            row = s.get(ResearchRun, run_id)
            assert (row.status, row.rounds_allowed) == ("paused", 1)
            assert (
                "Interrupted by a restart. Run one more round to carry on, or stop here."
                in messages(s, row)
            )
    finally:
        app2.state.engine.dispose()


def test_an_unset_judge_fails_the_run_with_its_message(app, db, research_world):
    run = start_research(db, app.state.vault, set(), FIXED_NOW)
    db.execute(delete(JobAssignment).where(JobAssignment.job == "judge"))
    db.commit()
    run_pending_jobs(app)
    db.expire_all()
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.error) == (
        "failed",
        "Set up the Review judge model in Settings, then run research again.",
    )


def test_an_unexpected_error_fails_the_run_and_the_job(
    app, db, research_world, monkeypatch
):
    monkeypatch.setattr(
        research_module,
        "research_lane",
        lambda *a: (_ for _ in ()).throw(RuntimeError("x")),
    )
    run = research(app, db)
    assert (run.status, run.error) == (
        "failed",
        "Research stopped unexpectedly: RuntimeError. Try again.",
    )
    assert db.scalar(select(Job.status).where(Job.kind == "research")) == "failed"


# --- starting ----------------------------------------------------------------------


def test_the_blockers_are_every_missing_model_then_tavily(db, app):
    assert research_blockers(db, app.state.vault) == [
        "Set up the Recommender model in Settings, then run research again.",
        "Set up the Review judge model in Settings, then run research again.",
        "Set up the Everyday tasks model in Settings, then run research again.",
        "Add a Tavily key in Settings, then run research again.",
    ]


def test_a_missing_tavily_key_alone_blocks_research(db, app, saved_ai_setup):
    assert research_blockers(db, app.state.vault) == []
    delete_setting(db, "tavily_api_key")
    assert research_blockers(db, app.state.vault) == [
        "Add a Tavily key in Settings, then run research again."
    ]
    with pytest.raises(ResearchError, match="Add a Tavily key"):
        start_research(db, app.state.vault, set(), FIXED_NOW)
    assert db.scalar(select(func.count()).select_from(ResearchRun)) == 0


def test_start_refuses_when_every_lane_is_full_and_none_is_ticked(
    db, app, research_world, solo_version
):
    for lane in Lane:
        fill_lane(db, lane, 20, solo_version)
    before = db.scalar(select(func.count()).select_from(ResearchRun))
    with pytest.raises(ResearchError) as err:
        start_research(db, app.state.vault, set(), FIXED_NOW)
    assert (
        err.value.message
        == "Every lane already has 20 recommended games. Tick a lane to add 5 more."
    )
    assert db.scalar(select(func.count()).select_from(ResearchRun)) == before
    assert research_jobs(db) == []
    run = start_research(db, app.state.vault, {Lane.TWO}, FIXED_NOW)
    assert [t.target for t in run_targets(run)] == [None, 25, None, None]


def test_a_running_run_is_named_before_a_full_house(
    db, app, research_world, solo_version
):
    start_research(db, app.state.vault, set(), FIXED_NOW)
    for lane in Lane:
        fill_lane(db, lane, 20, solo_version)
    with pytest.raises(ResearchError) as err:
        start_research(db, app.state.vault, set(), FIXED_NOW)
    assert err.value.message == RUNNING_REFUSAL


def test_a_paused_run_is_named_when_starting_again(app, db, research_world):
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    assert run.status == "paused"
    with pytest.raises(ResearchError) as err:
        start_research(db, app.state.vault, set(), FIXED_NOW)
    assert (
        err.value.message
        == "Research is paused. Run one more round, stop it or cancel it first."
    )
    assert db.scalar(select(func.count()).select_from(ResearchRun)) == 1
    assert research_jobs(db) == ["done"]


def test_two_starts_racing_on_two_threads_give_one_run(
    app, research_world, monkeypatch
):  # Review focus 4, Ruling 10
    # Counting the lanes is slow here, which holds the gap between the check for an
    # active run and the insert open long enough for the other thread to reach it too.
    counts = research_runs.recommended_counts

    def slow_counts(db):
        time.sleep(0.2)
        return counts(db)

    monkeypatch.setattr(research_runs, "recommended_counts", slow_counts)
    both_ready = threading.Barrier(2)
    outcomes: list[str] = []

    def click_start():
        with app.state.session_factory() as s:
            both_ready.wait()
            try:
                start_research(s, app.state.vault, set(), FIXED_NOW)
                outcomes.append("started")
            except ResearchError as err:
                outcomes.append(err.message)

    threads = [threading.Thread(target=click_start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [RUNNING_REFUSAL, "started"]
    with app.state.session_factory() as s:
        assert s.scalar(select(func.count()).select_from(ResearchRun)) == 1
        assert research_jobs(s) == ["queued"]


# --- the first segment -------------------------------------------------------------


def test_the_first_segment_refreshes_then_notes_each_lane_left_out_in_lane_order(
    app, db, research_world, solo_version
):
    fill_lane(db, Lane.SOLO, 20, solo_version)
    run = research(app, db)
    assert messages(db, run) == [
        "Refreshing profiles first.",
        "Profiles are up to date.",
        "Solo already has 20 recommended games.",
        "No profile yet for Two players. Publish a review played at this head count first.",
        "No profile yet for Three to four. Publish a review played at this head count first.",
        "No profile yet for Five or more. Publish a review played at this head count first.",
        "No lane could be researched this time.",
    ]
    assert (run.status, run.finished_at, run.rounds_done) == (
        "finished",
        FIXED_NOW,
        0,
    )
    notes = [t.note for t in run_targets(run)]
    assert notes[1] == (
        "No profile yet for Two players. Publish a review played at this head count first."
    )
    skipped = db.scalars(
        select(ResearchEvent).where(
            ResearchEvent.run_id == run.id, ResearchEvent.step == "skipped"
        )
    ).all()
    assert [(e.lane, e.round) for e in skipped] == [
        ("solo", None),
        ("two", None),
        ("three_four", None),
        ("five_plus", None),
    ]


def test_a_lane_whose_first_review_the_refresh_learns_is_researched(
    app, db, research_world
):
    version = publish(
        db,
        20,
        Lane.TWO,
        ReviewForm(rating=7, text="Planning every turn together is the fun part."),
        FIXED_NOW,
    )
    research_world["profile"] = profile_reply(
        loved=[
            {
                "text": "Likes planning turns together",
                "tag": "stated",
                "games": [20],
                "reviews": [version.id],
            }
        ]
    )
    run = research(app, db)
    assert not any(
        m.startswith("No profile yet for Two players") for m in messages(db, run)
    )
    assert run_targets(run)[1].note is None
    two_lines = db.scalar(
        select(func.count())
        .select_from(ResearchEvent)
        .where(
            ResearchEvent.run_id == run.id,
            ResearchEvent.lane == "two",
            ResearchEvent.round == 1,
        )
    )
    assert two_lines > 0


def test_a_filled_run_says_so(app, db, research_world, solo_version):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    run = research(app, db)
    log = messages(db, run)
    assert log[-1] == "Every lane is filled."
    assert "Round 1." in log and "Round 2." not in log
    assert run.rounds_done == 1


def test_a_lane_is_never_reported_twice_and_a_continue_does_not_refresh_again(
    app, db, research_world
):
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    continue_research(db, run.id, FIXED_NOW)
    run_pending_jobs(app)
    db.expire_all()
    log = messages(db, run)
    for label in ("Two players", "Three to four", "Five or more"):
        note = f"No profile yet for {label}. Publish a review played at this head count first."
        assert log.count(note) == 1
    assert log.count("Refreshing profiles first.") == 1
    assert db.scalar(select(func.count()).select_from(RefreshRun)) == 1
    assert [m for m in log if m.startswith("Round ")] == [
        "Round 1.",
        "Round 2.",
        "Round 3.",
        "Round 4.",
    ]
    assert log[-1] == "Paused after round 4. Still short: Solo."
    assert choices(db, run) == ["start", "continue"]
    assert research_jobs(db) == ["done", "done"]


def test_the_pause_names_every_short_lane(app, db, research_world):
    two_version(db)
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    assert run.status == "paused"
    assert (
        messages(db, run)[-1] == "Paused after round 3. Still short: Solo, Two players."
    )


# --- continue, stop and cancel -------------------------------------------------------


def test_only_a_paused_run_continues_or_stops(app, db, research_world):
    run = start_research(db, app.state.vault, set(), FIXED_NOW)
    assert continue_research(db, run.id, FIXED_NOW) is False
    assert stop_research(db, run.id, FIXED_NOW) is False
    run_pending_jobs(app)
    db.expire_all()
    assert db.get(ResearchRun, run.id).status == "paused"
    assert stop_research(db, run.id, FIXED_NOW) is True
    assert stop_research(db, run.id, FIXED_NOW) is False
    assert continue_research(db, run.id, FIXED_NOW) is False
    assert cancel_research(db, run.id, FIXED_NOW) is False
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.finished_at, row.rounds_allowed) == (
        "stopped",
        FIXED_NOW,
        3,
    )
    assert messages(db, row)[-1] == "Stopped after round 3."
    assert choices(db, row) == ["start", "stop"]
    assert research_jobs(db) == ["done"]
    for act in (continue_research, stop_research, cancel_research):
        assert act(db, 999, FIXED_NOW) is False


def test_cancelling_a_paused_run_ends_it_at_once(app, db, research_world):
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    assert cancel_research(db, run.id, FIXED_NOW) is True
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.finished_at) == ("cancelled", FIXED_NOW)
    assert messages(db, row)[-1] == "Cancelled."
    assert cancel_research(db, run.id, FIXED_NOW) is False
    assert choices(db, row) == ["start", "cancel"]


def test_a_second_cancel_of_a_running_run_is_a_no_op(app, db, research_world):
    run = start_research(db, app.state.vault, set(), FIXED_NOW)
    assert cancel_research(db, run.id, FIXED_NOW) is True
    assert cancel_research(db, run.id, FIXED_NOW) is False
    db.expire_all()
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.cancel_requested) == ("running", True)
    assert choices(db, row) == ["start", "cancel"]
    run_pending_jobs(app)
    db.expire_all()
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.finished_at) == ("cancelled", FIXED_NOW)
    assert messages(db, row) == [CANCELLED_KEPT]
    assert research_jobs(db) == ["done"]


def test_a_cancel_mid_run_keeps_what_the_judge_accepted(
    app, db, research_world, monkeypatch
):  # Review focus 3
    real_lane = research_module.research_lane

    def lane_then_cancel(ctx, run_id, round, lane):
        if round == 2:
            with ctx.session_factory() as s:
                assert cancel_research(s, run_id, FIXED_NOW) is True
        return real_lane(ctx, run_id, round, lane)

    monkeypatch.setattr(research_module, "research_lane", lane_then_cancel)
    run = research(app, db)
    assert (run.status, run.finished_at, run.rounds_done) == (
        "cancelled",
        FIXED_NOW,
        1,
    )
    assert messages(db, run)[-1] == CANCELLED_KEPT
    assert recommended_counts(db)[Lane.SOLO] == 1  # Cozy Crawl, accepted in round 1
    assert research_jobs(db) == ["done"]


def test_a_cancel_during_the_last_round_wins_over_the_pause(
    app, db, research_world, monkeypatch
):  # Review focus 3
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    real_lane = research_module.research_lane

    def lane_then_cancel(ctx, run_id, round, lane):
        outcome = real_lane(ctx, run_id, round, lane)
        if round == 3:
            with ctx.session_factory() as s:
                assert cancel_research(s, run_id, FIXED_NOW) is True
        return outcome

    monkeypatch.setattr(research_module, "research_lane", lane_then_cancel)
    run = research(app, db)
    assert (run.status, run.finished_at) == ("cancelled", FIXED_NOW)
    log = messages(db, run)
    assert log[-1] == CANCELLED_KEPT
    assert not any(m.startswith("Paused") for m in log)


def test_a_continue_queues_a_job_even_while_the_last_one_is_still_marked_running(
    app, db, research_world
):  # Review focus 3
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    # The job that paused the run hasn't been marked done yet, as when saving its
    # outcome failed and waits for the worker's next pass.
    db.execute(update(Job).where(Job.kind == RESEARCH_KIND).values(status="running"))
    db.commit()
    assert continue_research(db, run.id, FIXED_NOW) is True
    assert research_jobs(db) == ["running", "queued"]
    run_pending_jobs(app)
    db.expire_all()
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.rounds_done) == ("paused", 4)


# --- failures ------------------------------------------------------------------------


def test_a_model_unset_mid_run_fails_the_run_with_the_research_copy(
    app, db, research_world, fake_ai
):  # Ruling 16
    # The judge's model goes away while the Recommender is proposing, after the run
    # checked its blockers, so the judge's call is the one that finds it unset.
    recommender = fake_ai.replies["sample-big"]

    def propose_then_unset_the_judge(prompt):
        if prompt.startswith("# research_propose"):
            with app.state.session_factory() as s:
                s.execute(delete(JobAssignment).where(JobAssignment.job == "judge"))
                s.commit()
        return recommender(prompt)

    fake_ai.replies["sample-big"] = propose_then_unset_the_judge
    run = research(app, db)
    message = "Set up the Review judge model in Settings, then run research again."
    assert (run.status, run.error, run.finished_at) == ("failed", message, FIXED_NOW)
    assert messages(db, run)[-1] == message
    assert research_jobs(db) == ["done"]


def test_an_unset_model_error_without_its_job_falls_back_to_the_blockers(
    app, db, research_world, monkeypatch
):  # Ruling 16
    def not_configured(*args):
        with app.state.session_factory() as s:
            s.execute(delete(JobAssignment).where(JobAssignment.job == "everyday"))
            s.commit()
        raise StructuredReplyError("not_configured", "Set up the model.")

    monkeypatch.setattr(research_module, "research_lane", not_configured)
    run = research(app, db)
    assert (run.status, run.error) == (
        "failed",
        "Set up the Everyday tasks model in Settings, then run research again.",
    )


def test_an_unexpected_error_is_logged_on_the_run(app, db, research_world, monkeypatch):
    monkeypatch.setattr(
        research_module,
        "research_lane",
        lambda *a: (_ for _ in ()).throw(RuntimeError("secret detail")),
    )
    run = research(app, db)
    assert run.finished_at == FIXED_NOW
    assert (
        messages(db, run)[-1]
        == "Research stopped unexpectedly: RuntimeError. Try again."
    )
    assert not any("secret detail" in m for m in messages(db, run))


def test_a_job_with_no_running_run_does_nothing(app, db, research_world, fake_ai):
    db.add(
        Job(
            kind=RESEARCH_KIND,
            status="queued",
            requested_by="user",
            created_at=FIXED_NOW,
        )
    )
    db.commit()
    assert run_pending_jobs(app) == 1
    assert research_jobs(db) == ["done"]
    assert fake_ai.calls == []
    assert db.scalar(select(func.count()).select_from(ResearchEvent)) == 0


# --- a restart -----------------------------------------------------------------------


def test_a_restart_cancels_a_run_that_was_asked_to_cancel(db):  # Review focus 3
    run = ResearchRun(
        status="running",
        targets=[],
        rounds_done=2,
        cancel_requested=True,
        created_at=FIXED_NOW,
    )
    db.add(run)
    db.commit()
    assert recover_interrupted_research(db, FIXED_NOW) == 1
    db.expire_all()
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.finished_at, row.rounds_allowed) == (
        "cancelled",
        FIXED_NOW,
        3,
    )
    assert messages(db, row) == [CANCELLED_KEPT]


def test_a_restart_leaves_a_run_whose_job_is_still_queued(app, db, research_world):
    run = start_research(db, app.state.vault, set(), FIXED_NOW)
    assert recover_interrupted_research(db, FIXED_NOW) == 0
    db.expire_all()
    assert db.get(ResearchRun, run.id).status == "running"
    run_pending_jobs(app)
    db.expire_all()
    assert db.get(ResearchRun, run.id).status == "paused"


def test_a_restarted_run_carries_on_with_one_round(app, db, research_world):
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = start_research(db, app.state.vault, set(), FIXED_NOW)
    # Cut off in its first segment, before any round: the restart failed its job.
    db.execute(update(Job).values(status="failed"))
    db.commit()
    assert recover_interrupted_research(db, FIXED_NOW) == 1
    db.expire_all()
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.rounds_allowed, row.finished_at) == ("paused", 0, None)
    assert continue_research(db, run.id, FIXED_NOW) is True
    run_pending_jobs(app)
    db.expire_all()
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.rounds_done) == ("paused", 1)
    log = messages(db, row)
    assert (
        log[0]
        == "Interrupted by a restart. Run one more round to carry on, or stop here."
    )
    assert log[1] == "Refreshing profiles first."
    assert log[-1] == "Paused after round 1. Still short: Solo."
