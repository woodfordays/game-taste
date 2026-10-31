import dataclasses
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXED_NOW
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

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
    enqueue_job(
        db, "refresh", "user", datetime(2026, 1, 15, 4, 30, tzinfo=UTC)
    )  # a manual refresh after 04:00 counts
    assert not scheduled_refresh_due(db, now)
    assert scheduled_refresh_due(db, datetime(2026, 1, 16, 4, 0, tzinfo=UTC))


def test_scheduled_refresh_uses_the_defaults_when_nothing_is_set(onboarded_client, db):
    assert scheduled_refresh_due(db, datetime(2026, 1, 15, 5, 0, tzinfo=UTC))
    enqueue_job(db, "refresh", "schedule", datetime(2026, 1, 15, 4, 0, tzinfo=UTC))
    assert not scheduled_refresh_due(db, datetime(2026, 1, 15, 5, 0, tzinfo=UTC))


def test_a_refresh_before_todays_time_does_not_count(onboarded_client, db):
    set_setting(db, "refresh_time", "04:00")
    set_setting(db, "timezone", "UTC")
    enqueue_job(db, "refresh", "user", datetime(2026, 1, 15, 3, 59, tzinfo=UTC))
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
    enqueue_job(db, "refresh", "schedule", datetime(2026, 1, 14, 9, 0, tzinfo=UTC))
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
