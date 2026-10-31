"""Background jobs: a queue in the database, a worker thread and the daily refresh schedule.

A job is a row in the `job` table. `enqueue_job` adds one, the worker thread runs queued
jobs one at a time through the handler registered for their kind in
`app.state.job_handlers`, and the scheduler thread enqueues the daily refresh when it is due.
Both threads exist only when `Services.background` is true. Tests leave them off and call
`run_pending_jobs(app)` themselves.
"""

import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.auth import setup_finished
from app.models import Job
from app.services.secrets import Vault
from app.services.settings import (
    DEFAULT_REFRESH_TIME,
    DEFAULT_TIMEZONE,
    REFRESH_TIME_SETTING,
    TIMEZONE_SETTING,
    get_setting,
    parse_refresh_time,
)

if TYPE_CHECKING:
    from app.main import Services

log = logging.getLogger("game_taste.jobs")

REFRESH_KIND = "refresh"

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
# A job in either of these states is still pending: asking for another of its kind gets it back.
PENDING = (QUEUED, RUNNING)

REQUESTED_BY_USER = "user"
REQUESTED_BY_SCHEDULE = "schedule"

INTERRUPTED_MESSAGE = (
    "Interrupted by a restart. Start it again, or wait for the next scheduled refresh."
)

WORKER_THREAD_NAME = "game-taste-worker"
SCHEDULER_THREAD_NAME = "game-taste-scheduler"
# How long the worker waits for a wake-up before looking for jobs anyway.
WORKER_POLL_SECONDS = 5
# How often the scheduler asks whether the daily refresh is due.
SCHEDULER_INTERVAL_SECONDS = 60
# How long shutdown waits for each thread. A job still running then is cut off with the
# process and marked failed at the next start.
SHUTDOWN_JOIN_SECONDS = 2

# Two requests for the same job (a double click, or a click at the scheduled moment) arrive on
# different threads. One process serves the app, so one lock keeps the check and the insert
# in `enqueue_job` from interleaving.
_enqueue_lock = threading.Lock()


@dataclass(frozen=True)
class JobContext:
    """What a job handler gets: it opens its own sessions from `session_factory`."""

    session_factory: sessionmaker[Session]
    vault: Vault
    services: "Services"


def job_context(app: FastAPI) -> JobContext:
    return JobContext(
        session_factory=app.state.session_factory,
        vault=app.state.vault,
        services=app.state.services,
    )


def notify_worker(app: FastAPI) -> None:
    """Wake the worker so it looks for jobs now rather than at its next poll."""
    app.state.job_wakeup.set()


# --- the queue ---------------------------------------------------------------


def _enqueue(
    db: Session, kind: str, requested_by: str, now: datetime
) -> tuple[Job, bool]:
    """The pending job of `kind` and False, or a new one and True."""
    with _enqueue_lock:
        existing = db.scalars(
            select(Job)
            .where(Job.kind == kind, Job.status.in_(PENDING))
            .order_by(Job.id)
            .limit(1)
        ).first()
        if existing is not None:
            return existing, False
        job = Job(kind=kind, status=QUEUED, requested_by=requested_by, created_at=now)
        db.add(job)
        db.commit()
        return job, True


def enqueue_job(db: Session, kind: str, requested_by: str, now: datetime) -> Job:
    """Queue a `kind` job, or return the one of that kind already queued or running."""
    return _enqueue(db, kind, requested_by, now)[0]


def claim_job(db: Session, job_id: int, now: datetime) -> bool:
    """Take a queued job by marking it running. False when someone else already did."""
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == QUEUED)
        .values(status=RUNNING, started_at=now)
    )
    db.commit()
    return result.rowcount == 1


@dataclass(frozen=True)
class JobOutcome:
    """How a job ended, kept until it is written to the job row."""

    status: str
    error: str | None
    finished_at: datetime


def _write_outcome(ctx: JobContext, job_id: int, outcome: JobOutcome) -> None:
    with ctx.session_factory() as db:
        db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=outcome.status,
                finished_at=outcome.finished_at,
                error=outcome.error,
            )
        )
        db.commit()


def _record_waiting_outcomes(app: FastAPI, ctx: JobContext) -> bool:
    """Write down the outcomes still waiting to be saved. True when none are left.

    A write can fail (the database stays locked, the disk is full). Its outcome stays in
    `app.state.unrecorded_outcomes`, so that the job doesn't stay "running" for good and
    block the next one of its kind: the next pass tries again before it claims anything.
    """
    waiting: dict[int, JobOutcome] = app.state.unrecorded_outcomes
    for job_id, outcome in list(waiting.items()):
        try:
            _write_outcome(ctx, job_id, outcome)
        except Exception:
            log.exception("Could not save how job %s ended. Will try again.", job_id)
            return False
        del waiting[job_id]
    return True


def run_pending_jobs(
    app: FastAPI, should_stop: Callable[[], bool] = lambda: False
) -> int:
    """Run the queued jobs one at a time, oldest first. Returns how many ran.

    A handler that raises fails its job and nothing else: the error saved on the job names
    only the exception's type, since the message might hold a secret. The traceback goes to
    the log. No new job starts once `should_stop()` is true, nor while the outcome of an
    earlier one is still unsaved (see `_record_waiting_outcomes`).
    """
    ctx = job_context(app)
    ran = 0
    while True:
        if not _record_waiting_outcomes(app, ctx) or should_stop():
            return ran
        with ctx.session_factory() as db:
            job = db.execute(
                select(Job.id, Job.kind)
                .where(Job.status == QUEUED)
                .order_by(Job.id)
                .limit(1)
            ).first()
            if job is None:
                return ran
            job_id, kind = job
            if not claim_job(db, job_id, ctx.services.clock()):
                # Another claimer won it. It is no longer queued, so the next pass moves on.
                continue
        ran += 1
        handler = app.state.job_handlers.get(kind)
        status, error = DONE, None
        if handler is None:
            status, error = FAILED, f"Nothing can run a {kind} job yet."
        else:
            try:
                handler(ctx, job_id)
            except Exception as exc:
                log.exception("The %s job %s stopped unexpectedly.", kind, job_id)
                status = FAILED
                error = (
                    f"The {kind} stopped unexpectedly: {type(exc).__name__}. Try again."
                )
        outcome = JobOutcome(status, error, ctx.services.clock())
        app.state.unrecorded_outcomes[job_id] = outcome


def recover_interrupted_jobs(db: Session, now: datetime) -> int:
    """Fail every job still marked running, which only a restart can leave behind.

    Returns how many there were. Queued jobs are left alone, since they never started.
    """
    result = db.execute(
        update(Job)
        .where(Job.status == RUNNING)
        .values(status=FAILED, finished_at=now, error=INTERRUPTED_MESSAGE)
    )
    db.commit()
    return result.rowcount


# --- the daily schedule --------------------------------------------------------


def last_scheduled_time(now: datetime, refresh_time: str, tz: str) -> datetime:
    """The latest moment at or before `now` when the daily refresh was due, in UTC.

    That is `refresh_time` ("HH:MM") on the local clock in the timezone `tz`: today's, or
    yesterday's when today's hasn't come yet. A local time that doesn't exist on a
    clock-change day (it was skipped) lands the way zoneinfo puts it with `fold=0`, on the
    pre-change offset; one that happens twice is taken at its first occurrence. A `tz`
    that isn't a known name counts as UTC, and a `refresh_time` that isn't HH:MM as 04:00.
    """
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = UTC
    parsed = parse_refresh_time(refresh_time) or DEFAULT_REFRESH_TIME
    hour, minute = (int(part) for part in parsed.split(":"))

    def due_on(day: date) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(UTC)

    today = now.astimezone(zone).date()
    due = due_on(today)
    return due if due <= now else due_on(today - timedelta(days=1))


def scheduled_refresh_due(db: Session, now: datetime) -> bool:
    """Whether the daily refresh should be queued now.

    True once setup is finished and no refresh job already stands for the day. A refresh job,
    of any status and whoever asked for it, stands for the day when it was created at or
    after the latest scheduled time, or finished at or after it, or is still queued or
    running. So a manual refresh at 19:59:50 for a 20:00 schedule is the day's refresh
    whether it ends before or after 20:00, and a failed one counts too.
    """
    if not setup_finished(db):
        return False
    refresh_time = get_setting(db, REFRESH_TIME_SETTING) or DEFAULT_REFRESH_TIME
    tz = get_setting(db, TIMEZONE_SETTING) or DEFAULT_TIMEZONE
    since = last_scheduled_time(now, refresh_time, tz)
    standing = db.scalars(
        select(Job.id)
        .where(
            Job.kind == REFRESH_KIND,
            or_(
                Job.created_at >= since,
                Job.finished_at >= since,
                Job.status.in_(PENDING),
            ),
        )
        .limit(1)
    ).first()
    return standing is None


def enqueue_scheduled_refresh(app: FastAPI) -> bool:
    """Queue the daily refresh if it is due, and wake the worker.

    Returns whether it created a job.
    """
    now = app.state.services.clock()
    with app.state.session_factory() as db:
        if not scheduled_refresh_due(db, now):
            return False
        _, created = _enqueue(db, REFRESH_KIND, REQUESTED_BY_SCHEDULE, now)
    if created:
        notify_worker(app)
    return created


# --- the threads -----------------------------------------------------------------


def _worker_loop(app: FastAPI, stop: threading.Event) -> None:
    wakeup: threading.Event = app.state.job_wakeup
    while True:
        # Cleared before looking, so a wake-up that arrives while jobs run is not lost. The stop
        # check comes after the clear: shutdown sets `stop` before it wakes the worker, so
        # either this sees `stop` or the wait below is cut short.
        wakeup.clear()
        if stop.is_set():
            return
        try:
            run_pending_jobs(app, should_stop=stop.is_set)
        except Exception:
            log.exception("The job worker hit an error and will look again shortly.")
        wakeup.wait(WORKER_POLL_SECONDS)


def _scheduler_loop(app: FastAPI, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            enqueue_scheduled_refresh(app)
        except Exception:
            log.exception("The refresh scheduler hit an error and will check again.")
        stop.wait(SCHEDULER_INTERVAL_SECONDS)


def start_background_threads(
    app: FastAPI, stop: threading.Event
) -> list[threading.Thread]:
    threads = [
        threading.Thread(
            target=_worker_loop, args=(app, stop), name=WORKER_THREAD_NAME, daemon=True
        ),
        threading.Thread(
            target=_scheduler_loop,
            args=(app, stop),
            name=SCHEDULER_THREAD_NAME,
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    return threads


def stop_background_threads(
    app: FastAPI, stop: threading.Event, threads: list[threading.Thread]
) -> None:
    stop.set()
    notify_worker(app)  # the worker may be waiting for a wake-up
    for thread in threads:
        thread.join(timeout=SHUTDOWN_JOIN_SECONDS)


@asynccontextmanager
async def background_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the worker and the scheduler for as long as the app is up."""
    stop = threading.Event()
    threads = start_background_threads(app, stop)
    try:
        yield
    finally:
        stop_background_threads(app, stop, threads)
