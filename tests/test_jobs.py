import dataclasses
import itertools
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXED_NOW
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app import jobs
from app.jobs import (
    JobContext,
    claim_job,
    enqueue_job,
    enqueue_scheduled_refresh,
    job_context,
    last_scheduled_time,
    notify_worker,
    recover_interrupted_jobs,
    run_pending_jobs,
    scheduled_refresh_due,
)
from app.main import create_app
from app.models import Job
from app.services.settings import set_setting


def test_enqueue_is_idempotent_while_pending(db):  # Review focus 3
    a = enqueue_job(db, "refresh", "user", FIXED_NOW)
    b = enqueue_job(db, "refresh", "schedule", FIXED_NOW + timedelta(seconds=1))
    assert a.id == b.id and db.scalar(select(func.count()).select_from(Job)) == 1


def test_enqueue_returns_the_running_job_too(db):
    a = enqueue_job(db, "refresh", "user", FIXED_NOW)
    db.execute(update(Job).where(Job.id == a.id).values(status="running"))
    db.commit()
    assert enqueue_job(db, "refresh", "user", FIXED_NOW).id == a.id


def test_enqueue_starts_a_new_job_once_the_last_one_has_finished(db):
    a = enqueue_job(db, "refresh", "user", FIXED_NOW)
    db.execute(update(Job).where(Job.id == a.id).values(status="done"))
    db.commit()
    b = enqueue_job(db, "refresh", "user", FIXED_NOW)
    assert b.id != a.id and b.status == "queued"


def test_enqueue_is_per_kind(db):
    a = enqueue_job(db, "refresh", "user", FIXED_NOW)
    b = enqueue_job(db, "research", "user", FIXED_NOW)
    assert a.id != b.id


def test_enqueue_records_what_was_asked(db):
    job = enqueue_job(db, "refresh", "schedule", FIXED_NOW)
    db.expire_all()
    row = db.get(Job, job.id)
    assert (row.kind, row.status, row.requested_by) == ("refresh", "queued", "schedule")
    assert row.created_at == FIXED_NOW
    assert row.started_at is None and row.finished_at is None and row.error is None


def test_simultaneous_enqueues_give_one_job(app):  # Review focus 3
    results: list[int] = []
    barrier = threading.Barrier(8)

    def click() -> None:
        with app.state.session_factory() as session:
            barrier.wait()
            results.append(enqueue_job(session, "refresh", "user", FIXED_NOW).id)

    threads = [threading.Thread(target=click) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 8 and len(set(results)) == 1
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_run_pending_jobs_runs_handler_and_records_outcome(app, db):
    seen = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: seen.append(job_id)
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    assert run_pending_jobs(app) == 1 and seen == [job.id]
    db.expire_all()
    assert (
        db.get(Job, job.id).status == "done"
        and db.get(Job, job.id).finished_at is not None
    )


def test_run_pending_jobs_stamps_times_from_the_clock(app, db):
    app.state.job_handlers["refresh"] = lambda ctx, job_id: None
    job = enqueue_job(db, "refresh", "user", FIXED_NOW - timedelta(hours=1))
    run_pending_jobs(app)
    db.expire_all()
    row = db.get(Job, job.id)
    assert row.started_at == FIXED_NOW and row.finished_at == FIXED_NOW
    assert row.error is None


def test_the_handler_gets_a_context_built_from_the_app(app, db):
    contexts = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: contexts.append(ctx)
    enqueue_job(db, "refresh", "user", FIXED_NOW)
    run_pending_jobs(app)
    (ctx,) = contexts
    assert isinstance(ctx, JobContext)
    assert ctx.session_factory is app.state.session_factory
    assert ctx.vault is app.state.vault and ctx.services is app.state.services
    assert job_context(app) == ctx


def test_the_handler_sees_its_job_as_running(app, db):
    states = []

    def handler(ctx, job_id):
        with ctx.session_factory() as session:
            states.append(session.get(Job, job_id).status)

    app.state.job_handlers["refresh"] = handler
    enqueue_job(db, "refresh", "user", FIXED_NOW)
    run_pending_jobs(app)
    assert states == ["running"]


def test_run_pending_jobs_with_nothing_queued(app):
    assert run_pending_jobs(app) == 0


def test_run_pending_jobs_runs_oldest_first_one_at_a_time(app, db):
    order = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: order.append(
        ("refresh", job_id)
    )
    app.state.job_handlers["research"] = lambda ctx, job_id: order.append(
        ("research", job_id)
    )
    first = enqueue_job(db, "research", "user", FIXED_NOW)
    second = enqueue_job(db, "refresh", "user", FIXED_NOW)
    assert run_pending_jobs(app) == 2
    assert order == [("research", first.id), ("refresh", second.id)]
    assert run_pending_jobs(app) == 0


def test_handler_exception_fails_job_without_stopping(app, db):
    def boom(ctx, job_id):
        raise RuntimeError("sk-secret-in-message")

    app.state.job_handlers["refresh"] = boom
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    run_pending_jobs(app)
    db.expire_all()
    row = db.get(Job, job.id)
    assert (
        row.status == "failed"
        and row.error == "The refresh stopped unexpectedly: RuntimeError. Try again."
    )
    assert "sk-secret" not in row.error
    assert row.finished_at == FIXED_NOW


def test_a_failed_job_does_not_stop_the_next_one(app, db):
    seen = []

    def boom(ctx, job_id):
        raise ValueError("nope")

    app.state.job_handlers["refresh"] = boom
    app.state.job_handlers["research"] = lambda ctx, job_id: seen.append(job_id)
    bad = enqueue_job(db, "refresh", "user", FIXED_NOW)
    good = enqueue_job(db, "research", "user", FIXED_NOW)
    assert run_pending_jobs(app) == 2
    db.expire_all()
    assert db.get(Job, bad.id).status == "failed"
    assert db.get(Job, good.id).status == "done" and seen == [good.id]


def test_the_traceback_is_logged(app, db, caplog):
    def boom(ctx, job_id):
        raise RuntimeError("sk-secret-in-message")

    app.state.job_handlers["refresh"] = boom
    enqueue_job(db, "refresh", "user", FIXED_NOW)
    with caplog.at_level(logging.ERROR, logger="game_taste.jobs"):
        run_pending_jobs(app)
    (record,) = [r for r in caplog.records if r.name == "game_taste.jobs"]
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError


def test_a_kind_with_no_handler_fails_with_a_clear_message(app, db):
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    assert app.state.job_handlers == {}
    assert run_pending_jobs(app) == 1
    db.expire_all()
    row = db.get(Job, job.id)
    assert row.status == "failed" and row.error == "Nothing can run a refresh job yet."
    assert row.finished_at == FIXED_NOW


def test_claim_job_is_atomic(db):
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    assert claim_job(db, job.id, FIXED_NOW) is True
    # Someone else already holds it, so a second claim loses.
    assert claim_job(db, job.id, FIXED_NOW) is False
    db.expire_all()
    assert db.get(Job, job.id).status == "running"
    assert db.get(Job, job.id).started_at == FIXED_NOW


def test_a_job_claimed_elsewhere_is_skipped(app, db, monkeypatch):
    ran = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: ran.append(job_id)
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    real_claim = claim_job

    def lose_the_race(session, job_id, now):
        # Another worker gets there first.
        real_claim(session, job_id, now)
        return False

    monkeypatch.setattr("app.jobs.claim_job", lose_the_race)
    assert run_pending_jobs(app) == 0 and ran == []
    db.expire_all()
    assert db.get(Job, job.id).status == "running"


def test_restart_marks_running_jobs_failed(tmp_path, services):  # Review focus 2
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        job_id = enqueue_job(s, "refresh", "user", FIXED_NOW).id
        s.execute(update(Job).where(Job.id == job_id).values(status="running"))
        s.commit()
    app2 = create_app(tmp_path, services=services)
    with app2.state.session_factory() as s:
        row = s.get(Job, job_id)
        assert row.status == "failed" and row.error.startswith(
            "Interrupted by a restart."
        )
        assert row.finished_at == FIXED_NOW
        assert enqueue_job(s, "refresh", "user", FIXED_NOW).id != job_id
    app1.state.engine.dispose()
    app2.state.engine.dispose()


def test_recovery_message_and_count(db):
    a = enqueue_job(db, "refresh", "user", FIXED_NOW)
    b = enqueue_job(db, "research", "user", FIXED_NOW)
    c = enqueue_job(db, "other", "user", FIXED_NOW)
    db.execute(update(Job).where(Job.id.in_([a.id, b.id])).values(status="running"))
    db.commit()
    assert recover_interrupted_jobs(db, FIXED_NOW) == 2
    db.expire_all()
    for job_id in (a.id, b.id):
        row = db.get(Job, job_id)
        assert row.status == "failed"
        assert row.error == (
            "Interrupted by a restart. Start it again, or wait for the next scheduled refresh."
        )
    # A job that never started still waits its turn.
    assert db.get(Job, c.id).status == "queued"
    assert recover_interrupted_jobs(db, FIXED_NOW) == 0


@pytest.mark.parametrize(
    "now,time,tz,expected",
    [  # Review focus 5
        (
            datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            "04:00",
            "America/New_York",
            datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 15, 8, 0, tzinfo=UTC),
            "04:00",
            "America/New_York",
            datetime(2026, 1, 14, 9, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 3, 8, 12, 0, tzinfo=UTC),
            "02:30",
            "America/New_York",
            datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            "01:30",
            "America/New_York",
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            "23:59",
            "UTC",
            datetime(2026, 1, 14, 23, 59, tzinfo=UTC),
        ),
    ],
)
def test_last_scheduled_time(now, time, tz, expected):
    assert last_scheduled_time(now, time, tz) == expected


def test_last_scheduled_time_at_the_exact_moment_counts_as_today():
    now = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)
    assert last_scheduled_time(now, "04:00", "UTC") == now


def test_last_scheduled_time_uses_the_local_date_not_the_utc_date():
    # 02:00 UTC on the 16th is still the evening of the 15th in New York.
    now = datetime(2026, 1, 16, 2, 0, tzinfo=UTC)
    assert last_scheduled_time(now, "04:00", "America/New_York") == datetime(
        2026, 1, 15, 9, 0, tzinfo=UTC
    )


def test_last_scheduled_time_is_in_utc():
    result = last_scheduled_time(FIXED_NOW, "04:00", "America/New_York")
    assert result.utcoffset() == timedelta(0)


@pytest.mark.parametrize("tz", ["Not/AZone", "", "America", "../etc/passwd"])
def test_an_unknown_timezone_falls_back_to_utc(tz):
    assert last_scheduled_time(FIXED_NOW, "04:00", tz) == datetime(
        2026, 1, 15, 4, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("refresh_time", ["", "25:99", "soon", "4:00"])
def test_an_unreadable_refresh_time_falls_back_to_four(refresh_time):
    assert last_scheduled_time(FIXED_NOW, refresh_time, "UTC") == datetime(
        2026, 1, 15, 4, 0, tzinfo=UTC
    )


def test_scheduled_refresh_due_once_per_day(
    onboarded_client, db
):  # Review focus 3 and 5
    set_setting(db, "refresh_time", "04:00")
    set_setting(db, "timezone", "UTC")
    now = datetime(2026, 1, 15, 5, 0, tzinfo=UTC)
    assert scheduled_refresh_due(db, now)
    job = enqueue_job(
        db, "refresh", "user", datetime(2026, 1, 15, 4, 30, tzinfo=UTC)
    )  # a manual refresh after 04:00 counts
    assert not scheduled_refresh_due(db, now)
    # By the next day the worker has run it. A refresh still queued or running would count
    # for any day (see the tests at the end).
    _finish_refresh(db, job.id, datetime(2026, 1, 15, 4, 35, tzinfo=UTC))
    assert scheduled_refresh_due(db, datetime(2026, 1, 16, 4, 0, tzinfo=UTC))


def test_scheduled_refresh_uses_the_defaults_when_nothing_is_set(onboarded_client, db):
    assert scheduled_refresh_due(db, datetime(2026, 1, 15, 5, 0, tzinfo=UTC))
    enqueue_job(db, "refresh", "schedule", datetime(2026, 1, 15, 4, 0, tzinfo=UTC))
    assert not scheduled_refresh_due(db, datetime(2026, 1, 15, 5, 0, tzinfo=UTC))


def test_a_refresh_before_todays_time_does_not_count(onboarded_client, db):
    set_setting(db, "refresh_time", "04:00")
    set_setting(db, "timezone", "UTC")
    _refresh_job(
        db,
        datetime(2026, 1, 15, 3, 50, tzinfo=UTC),
        "done",
        datetime(2026, 1, 15, 3, 59, tzinfo=UTC),
    )
    assert scheduled_refresh_due(db, datetime(2026, 1, 15, 5, 0, tzinfo=UTC))


def test_a_failed_refresh_still_counts_for_the_day(onboarded_client, db):
    set_setting(db, "refresh_time", "04:00")
    set_setting(db, "timezone", "UTC")
    job = enqueue_job(
        db, "refresh", "schedule", datetime(2026, 1, 15, 4, 0, tzinfo=UTC)
    )
    db.execute(update(Job).where(Job.id == job.id).values(status="failed"))
    db.commit()
    assert not scheduled_refresh_due(db, datetime(2026, 1, 15, 5, 0, tzinfo=UTC))


def test_other_kinds_do_not_count_as_the_refresh(onboarded_client, db):
    set_setting(db, "refresh_time", "04:00")
    set_setting(db, "timezone", "UTC")
    enqueue_job(db, "research", "user", datetime(2026, 1, 15, 4, 30, tzinfo=UTC))
    assert scheduled_refresh_due(db, datetime(2026, 1, 15, 5, 0, tzinfo=UTC))


def test_the_schedule_follows_the_timezone_setting(
    onboarded_client, db
):  # Review focus 5
    set_setting(db, "refresh_time", "04:00")
    set_setting(db, "timezone", "America/New_York")
    # 04:00 in New York is 09:00 UTC in January. Yesterday's refresh ran at 09:00 UTC, so
    # at 08:00 UTC today's is not due yet, and at 09:00 UTC it is.
    _refresh_job(
        db,
        datetime(2026, 1, 14, 9, 0, tzinfo=UTC),
        "done",
        datetime(2026, 1, 14, 9, 1, tzinfo=UTC),
    )
    assert not scheduled_refresh_due(db, datetime(2026, 1, 15, 8, 0, tzinfo=UTC))
    assert scheduled_refresh_due(db, datetime(2026, 1, 15, 9, 0, tzinfo=UTC))


def test_not_due_before_setup_is_finished(logged_in_client, db):
    assert not scheduled_refresh_due(db, FIXED_NOW)


def test_enqueue_scheduled_refresh_queues_one_job_and_wakes_the_worker(
    app, onboarded_client, db
):
    set_setting(db, "refresh_time", "04:00")
    set_setting(db, "timezone", "UTC")
    assert not app.state.job_wakeup.is_set()
    assert enqueue_scheduled_refresh(app) is True
    assert app.state.job_wakeup.is_set()
    job = db.scalars(select(Job)).one()
    assert (job.kind, job.status, job.requested_by) == ("refresh", "queued", "schedule")
    assert job.created_at == FIXED_NOW
    app.state.job_wakeup.clear()
    assert enqueue_scheduled_refresh(app) is False
    assert not app.state.job_wakeup.is_set()
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_enqueue_scheduled_refresh_does_nothing_before_setup_is_finished(app, db):
    assert enqueue_scheduled_refresh(app) is False
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_notify_worker_sets_the_wakeup_event(app):
    assert not app.state.job_wakeup.is_set()
    notify_worker(app)
    assert app.state.job_wakeup.is_set()


def test_background_threads_only_when_enabled(tmp_path, services):
    app = create_app(
        tmp_path, services=services
    )  # services.background is False in tests
    with TestClient(app):
        assert not any(t.name.startswith("game-taste-") for t in threading.enumerate())
    app.state.engine.dispose()


def _game_taste_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("game-taste-")]


def test_the_worker_thread_runs_a_job_and_shuts_down_cleanly(tmp_path, services):
    app = create_app(tmp_path, services=dataclasses.replace(services, background=True))
    seen = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: seen.append(job_id)
    with TestClient(app):
        names = {t.name for t in _game_taste_threads()}
        assert names == {"game-taste-worker", "game-taste-scheduler"}
        assert all(t.daemon for t in _game_taste_threads())
        with app.state.session_factory() as s:
            job_id = enqueue_job(s, "refresh", "user", FIXED_NOW).id
        notify_worker(app)
        status = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with app.state.session_factory() as s:
                status = s.get(Job, job_id).status
            if status == "done":
                break
            time.sleep(0.05)
        assert status == "done" and seen == [job_id]
    assert _game_taste_threads() == []
    app.state.engine.dispose()


def test_the_scheduler_thread_enqueues_a_due_refresh(tmp_path, services):
    app = create_app(tmp_path, services=dataclasses.replace(services, background=True))
    with app.state.session_factory() as s:
        set_setting(s, "password_hash", "x")
        set_setting(s, "onboarding_step", "done")
    seen = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: seen.append(job_id)
    with TestClient(app):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not seen:
            time.sleep(0.05)
    assert len(seen) == 1
    with app.state.session_factory() as s:
        job = s.scalars(select(Job)).one()
        assert (job.requested_by, job.status) == ("schedule", "done")
    app.state.engine.dispose()


# --- a failed final write must not leave a job running ---------------------------


def _fail_writes(monkeypatch, times: int) -> None:
    """Make the next `times` writes of a job's outcome fail as a locked database does."""
    real = jobs._write_outcome
    left = [times]

    def flaky(ctx, job_id, outcome):
        if left[0] > 0:
            left[0] -= 1
            raise OperationalError("UPDATE job", {}, Exception("database is locked"))
        real(ctx, job_id, outcome)

    monkeypatch.setattr(jobs, "_write_outcome", flaky)


def test_a_failed_final_write_is_retried_on_the_next_pass(
    app, db, monkeypatch, caplog
):  # Review focus 2
    ticks = itertools.count()
    app.state.services.clock = lambda: FIXED_NOW + timedelta(minutes=next(ticks))
    seen = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: None
    app.state.job_handlers["research"] = lambda ctx, job_id: seen.append(job_id)
    first_id = enqueue_job(db, "refresh", "user", FIXED_NOW).id
    second_id = enqueue_job(db, "research", "user", FIXED_NOW).id
    _fail_writes(monkeypatch, 1)

    with caplog.at_level(logging.ERROR, logger="game_taste.jobs"):
        # The pass ends quietly, and the second job is not started on top of the first.
        assert run_pending_jobs(app) == 1
    assert "Could not save how job" in caplog.text
    db.expire_all()
    assert db.get(Job, first_id).status == "running"
    assert db.get(Job, second_id).status == "queued" and seen == []

    assert run_pending_jobs(app) == 1
    db.expire_all()
    first = db.get(Job, first_id)
    assert first.status == "done" and first.error is None
    # The time it ended, not the time the write finally went through.
    assert first.finished_at == FIXED_NOW + timedelta(minutes=1)
    assert db.get(Job, second_id).status == "done" and seen == [second_id]
    assert app.state.unrecorded_outcomes == {}


def test_a_failed_job_outcome_is_kept_through_a_failed_write(app, db, monkeypatch):
    def boom(ctx, job_id):
        raise RuntimeError("sk-secret-in-message")

    app.state.job_handlers["refresh"] = boom
    job_id = enqueue_job(db, "refresh", "user", FIXED_NOW).id
    _fail_writes(monkeypatch, 1)
    run_pending_jobs(app)
    run_pending_jobs(app)
    db.expire_all()
    row = db.get(Job, job_id)
    assert row.status == "failed"
    assert row.error == "The refresh stopped unexpectedly: RuntimeError. Try again."


def test_a_missing_handler_outcome_is_kept_through_a_failed_write(app, db, monkeypatch):
    job_id = enqueue_job(db, "refresh", "user", FIXED_NOW).id
    _fail_writes(monkeypatch, 1)
    run_pending_jobs(app)
    run_pending_jobs(app)
    db.expire_all()
    assert db.get(Job, job_id).error == "Nothing can run a refresh job yet."


def test_nothing_new_starts_while_an_outcome_cannot_be_saved(app, db, monkeypatch):
    seen = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: None
    app.state.job_handlers["research"] = lambda ctx, job_id: seen.append(job_id)
    first_id = enqueue_job(db, "refresh", "user", FIXED_NOW).id
    second_id = enqueue_job(db, "research", "user", FIXED_NOW).id
    _fail_writes(monkeypatch, 3)
    assert [run_pending_jobs(app) for _ in range(3)] == [1, 0, 0]
    db.expire_all()
    assert db.get(Job, first_id).status == "running"
    assert db.get(Job, second_id).status == "queued" and seen == []
    assert run_pending_jobs(app) == 1
    db.expire_all()
    assert db.get(Job, first_id).status == "done"
    assert db.get(Job, second_id).status == "done" and seen == [second_id]


def test_the_worker_thread_retries_a_failed_final_write(
    tmp_path, services, monkeypatch
):
    monkeypatch.setattr(jobs, "WORKER_POLL_SECONDS", 0.05)
    app = create_app(tmp_path, services=dataclasses.replace(services, background=True))
    app.state.job_handlers["refresh"] = lambda ctx, job_id: None
    _fail_writes(monkeypatch, 2)
    with TestClient(app):
        with app.state.session_factory() as s:
            job_id = enqueue_job(s, "refresh", "user", FIXED_NOW).id
        notify_worker(app)
        status = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with app.state.session_factory() as s:
                status = s.get(Job, job_id).status
            if status == "done":
                break
            time.sleep(0.05)
        assert status == "done"
        # Nothing is stuck: a new refresh can be queued.
        with app.state.session_factory() as s:
            assert enqueue_job(s, "refresh", "user", FIXED_NOW).id != job_id
    app.state.engine.dispose()


# --- stopping -------------------------------------------------------------------


def test_no_job_starts_once_a_stop_is_asked_for(app, db):
    app.state.job_handlers["refresh"] = lambda ctx, job_id: None
    job_id = enqueue_job(db, "refresh", "user", FIXED_NOW).id
    assert run_pending_jobs(app, should_stop=lambda: True) == 0
    db.expire_all()
    assert db.get(Job, job_id).status == "queued"


def test_a_stop_during_a_job_lets_it_finish_but_starts_no_other(app, db):
    stop = threading.Event()
    seen = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: stop.set()
    app.state.job_handlers["research"] = lambda ctx, job_id: seen.append(job_id)
    first_id = enqueue_job(db, "refresh", "user", FIXED_NOW).id
    second_id = enqueue_job(db, "research", "user", FIXED_NOW).id
    assert run_pending_jobs(app, should_stop=stop.is_set) == 1
    db.expire_all()
    assert db.get(Job, first_id).status == "done"
    assert db.get(Job, second_id).status == "queued" and seen == []


# --- a refresh that spans the scheduled time --------------------------------------


def _refresh_job(db, created_at, status, finished_at=None) -> int:
    """A refresh job created at `created_at`, then put in `status`."""
    job_id = enqueue_job(db, "refresh", "user", created_at).id
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status=status, finished_at=finished_at)
    )
    db.commit()
    return job_id


def _finish_refresh(db, job_id, finished_at) -> None:
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status="done", finished_at=finished_at)
    )
    db.commit()


def _schedule(db, refresh_time="20:00", tz="UTC") -> None:
    set_setting(db, "refresh_time", refresh_time)
    set_setting(db, "timezone", tz)


def test_a_refresh_that_spans_the_scheduled_time_counts_for_the_day(
    onboarded_client, db
):  # Review focus 3
    _schedule(db)
    # Clicked at 19:59:50, finished after 20:00.
    _refresh_job(
        db,
        datetime(2026, 1, 14, 19, 59, 50, tzinfo=UTC),
        "done",
        datetime(2026, 1, 14, 20, 0, 40, tzinfo=UTC),
    )
    assert not scheduled_refresh_due(db, datetime(2026, 1, 14, 20, 5, tzinfo=UTC))
    # It is only that day's refresh: the next day's is due on time.
    assert scheduled_refresh_due(db, datetime(2026, 1, 15, 20, 0, tzinfo=UTC))


def test_a_refresh_that_ended_before_the_scheduled_time_does_not_count(
    onboarded_client, db
):
    _schedule(db)
    _refresh_job(
        db,
        datetime(2026, 1, 14, 19, 50, tzinfo=UTC),
        "done",
        datetime(2026, 1, 14, 19, 59, 10, tzinfo=UTC),
    )
    assert scheduled_refresh_due(db, datetime(2026, 1, 14, 20, 5, tzinfo=UTC))


def test_a_failed_refresh_that_ended_after_the_scheduled_time_counts(
    onboarded_client, db
):
    _schedule(db)
    _refresh_job(
        db,
        datetime(2026, 1, 14, 19, 59, 50, tzinfo=UTC),
        "failed",
        datetime(2026, 1, 14, 20, 0, 5, tzinfo=UTC),
    )
    assert not scheduled_refresh_due(db, datetime(2026, 1, 14, 20, 5, tzinfo=UTC))


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_queued_or_running_refresh_counts_however_old(onboarded_client, db, status):
    _schedule(db, "04:00")
    _refresh_job(db, datetime(2026, 1, 14, 10, 0, tzinfo=UTC), status)
    assert not scheduled_refresh_due(db, datetime(2026, 1, 15, 5, 0, tzinfo=UTC))


def test_the_scheduler_adds_no_second_refresh_around_a_manual_one(
    app, onboarded_client, db
):  # Review focus 3
    _schedule(db, "12:00")  # the app's clock is FIXED_NOW, 12:00 UTC
    job_id = _refresh_job(db, FIXED_NOW - timedelta(seconds=10), "running")
    assert enqueue_scheduled_refresh(app) is False
    _finish_refresh(db, job_id, FIXED_NOW + timedelta(seconds=30))
    assert enqueue_scheduled_refresh(app) is False
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_enqueue_scheduled_refresh_reports_whether_it_created_a_job(
    app, db, monkeypatch
):
    # Even if the due check was out of date, a pending refresh is not added to or announced.
    monkeypatch.setattr(jobs, "scheduled_refresh_due", lambda db, now: True)
    enqueue_job(db, "refresh", "user", FIXED_NOW)
    assert enqueue_scheduled_refresh(app) is False
    assert not app.state.job_wakeup.is_set()
    assert db.scalar(select(func.count()).select_from(Job)) == 1
