"""Research runs: what a run set out to do, what the user chose and how it went.

A run has a target for each lane (`LaneTarget`), a log of what it did (`ResearchEvent`) and the
user's choices (`ResearchChoice`, never edited). Only one run is active at a time, which is
one that is running or paused. The work itself is done by the research job
(`app.routines.research.run_research`); this module keeps the records and the plan, and
makes the user's choices: start, one more round, stop and cancel.

A run goes through `AUTO_ROUNDS` rounds on its own and then pauses while lanes are still
short. From a pause, each `continue_research` allows exactly one more round. Every change of
a run's status is a conditional update (`end_run`, `continue_research`, `cancel_research`),
so a click on one thread and the job on another can't both act on the same state.
"""

import threading
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.jobs import QUEUED, REQUESTED_BY_USER, RESEARCH_KIND, enqueue_job
from app.models import Candidate, Job, ResearchChoice, ResearchEvent, ResearchRun
from app.services.ai import JOB_LABELS, job_config
from app.services.ai import Job as AiJob
from app.services.ai_setup import stored_ai_setup
from app.services.queue import ACCEPTED, TARGET, TOP_UP, recommended_counts
from app.services.reviews import LANE_LABELS, Lane
from app.services.secrets import Vault

RUNNING = "running"
PAUSED = "paused"
FINISHED = "finished"
STOPPED = "stopped"
CANCELLED = "cancelled"
FAILED = "failed"

# The statuses of a run that is still going: one at a time.
ACTIVE = (RUNNING, PAUSED)

RUN_STATUS_LABELS = {
    RUNNING: "Running",
    PAUSED: "Paused",
    FINISHED: "Finished",
    STOPPED: "Stopped",
    CANCELLED: "Cancelled",
    FAILED: "Failed",
}

# The rounds a run goes through on its own before it pauses.
AUTO_ROUNDS = 3

# The models research needs, in the order their blockers are listed.
RESEARCH_JOBS = (AiJob.RECOMMENDER, AiJob.JUDGE, AiJob.EVERYDAY)

# The user's choices that act on a run once it has started (`ResearchChoice.choice`).
CONTINUE = "continue"
STOP = "stop"
CANCEL = "cancel"

SET_UP_MODEL = "Set up the {label} model in Settings, then run research again."
ADD_TAVILY_KEY = "Add a Tavily key in Settings, then run research again."
ALREADY_RUNNING = "Research is already running."
EVERY_LANE_FULL = (
    f"Every lane already has {TARGET} recommended games. "
    f"Tick a lane to add {TOP_UP} more."
)
STOPPED_AFTER = "Stopped after round {n}."
CANCELLED_AT_ONCE = "Cancelled."
CANCELLED_KEPT = "Cancelled. Games the judge accepted before this were kept."
INTERRUPTED = "Interrupted by a restart. Run one more round to carry on, or stop here."

# Two clicks on Start arrive on different threads, and `create_run` checks for an active
# run with no unique index behind it. One process serves the app, so this lock keeps one
# start's check and insert from interleaving with another's, as `_enqueue_lock` does for
# jobs.
_start_lock = threading.Lock()


class ResearchError(Exception):
    """Research can't start. `message` says why and what to do, in words fit to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class LaneTarget:
    """How many recommended games a run takes a lane to. A `target` of None means the lane
    isn't in this run, and `note` says why. `start_count` is how many it had when the run
    started, and `add_five` that the user's "add 5 more" is why the target is what it is."""

    lane: Lane
    start_count: int
    target: int | None
    add_five: bool
    note: str | None

    def as_json(self) -> dict:
        return {
            "lane": self.lane.value,
            "start_count": self.start_count,
            "target": self.target,
            "add_five": self.add_five,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, data: dict) -> "LaneTarget":
        return cls(
            lane=Lane(data["lane"]),
            start_count=data["start_count"],
            target=data["target"],
            add_five=data["add_five"],
            note=data["note"],
        )


def plan_targets(counts: dict[Lane, int], add_five: set[Lane]) -> list[LaneTarget]:
    """The target for each lane, in lane order, given how many recommended games each has.

    A lane under `TARGET` is taken up to it. A full lane (`TARGET` or more) is taken `TOP_UP`
    higher if it is in `add_five`, and otherwise left out of the run with a note. A lane
    that is under `TARGET` and ticked is taken to `TARGET`, and the tick changes nothing.
    """
    targets = []
    for lane in Lane:
        count = counts.get(lane, 0)
        if count < TARGET:
            targets.append(LaneTarget(lane, count, TARGET, False, None))
        elif lane in add_five:
            targets.append(LaneTarget(lane, count, count + TOP_UP, True, None))
        else:
            note = f"{LANE_LABELS[lane]} already has {count} recommended games."
            targets.append(LaneTarget(lane, count, None, False, note))
    return targets


def active_run(db: Session) -> ResearchRun | None:
    """The run that is running or paused, or None."""
    return db.scalar(
        select(ResearchRun)
        .where(ResearchRun.status.in_(ACTIVE))
        .order_by(ResearchRun.id.desc())
        .limit(1)
    )


def run_targets(run: ResearchRun) -> list[LaneTarget]:
    """The run's target for each lane, in lane order."""
    return [LaneTarget.from_json(data) for data in run.targets]


def create_run(db: Session, add_five: set[Lane], now: datetime) -> ResearchRun:
    """Start a run: save it as running with its targets (see `plan_targets`) and the user's
    choices, which are `start` and an `add_five` for each lane ticked, in lane order.

    Raises `ValueError`, saving nothing, while another run is running or paused.
    """
    if active_run(db) is not None:
        raise ValueError("A research run is already in progress.")
    targets = plan_targets(recommended_counts(db), add_five)
    run = ResearchRun(
        status=RUNNING,
        targets=[target.as_json() for target in targets],
        rounds_allowed=AUTO_ROUNDS,
        rounds_done=0,
        cancel_requested=False,
        created_at=now,
    )
    try:
        db.add(run)
        db.flush()  # gives the run its id for the choices
        db.add(ResearchChoice(run_id=run.id, choice="start", lane=None, made_at=now))
        db.add_all(
            ResearchChoice(
                run_id=run.id, choice="add_five", lane=lane.value, made_at=now
            )
            for lane in Lane
            if lane in add_five
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return run


def research_blockers(db: Session, vault: Vault) -> list[str]:
    """What has to be set up before research can run, as lines to show, in order: a model
    for each of the Recommender, the Review judge and Everyday tasks, then the Tavily key.
    Empty when research can run."""
    blockers = [
        SET_UP_MODEL.format(label=JOB_LABELS[job])
        for job in RESEARCH_JOBS
        if job_config(db, vault, job) is None
    ]
    if not stored_ai_setup(db, vault).tavily_key:
        blockers.append(ADD_TAVILY_KEY)
    return blockers


def start_research(
    db: Session, vault: Vault, add_five: set[Lane], now: datetime
) -> ResearchRun:
    """Start a run (see `create_run`) and queue the job that does it. The caller wakes the
    worker.

    Raises `ResearchError`, saving nothing, with the first of the `research_blockers`, then
    while a run is running or paused, then when no lane gets a target (every lane is full
    and none is ticked for 5 more).
    """
    blockers = research_blockers(db, vault)
    if blockers:
        raise ResearchError(blockers[0])
    with _start_lock:
        if active_run(db) is not None:
            raise ResearchError(ALREADY_RUNNING)
        targets = plan_targets(recommended_counts(db), add_five)
        if all(target.target is None for target in targets):
            raise ResearchError(EVERY_LANE_FULL)
        run = create_run(db, add_five, now)
        _queue_research(db, now)
    return run


def continue_research(db: Session, run_id: int, now: datetime) -> bool:
    """Give a paused run exactly one more round: add 1 to `rounds_allowed`, set it running,
    save the `continue` choice and queue the job. The caller wakes the worker.

    Returns False, changing nothing, for a run that isn't paused, so a second click is a
    no-op. The check and the change are one conditional update, so two clicks on two
    threads can't both pass it.
    """
    resumed = db.execute(
        update(ResearchRun)
        .where(ResearchRun.id == run_id, ResearchRun.status == PAUSED)
        .values(status=RUNNING, rounds_allowed=ResearchRun.rounds_allowed + 1)
    )
    if resumed.rowcount != 1:
        db.rollback()
        return False
    _add_choice(db, run_id, CONTINUE, now)
    db.commit()
    _queue_research(db, now)
    return True


def stop_research(db: Session, run_id: int, now: datetime) -> bool:
    """End a paused run as stopped, saving the `stop` choice. False, changing nothing, for
    a run that isn't paused."""
    row = db.execute(
        select(ResearchRun.status, ResearchRun.rounds_done).where(
            ResearchRun.id == run_id
        )
    ).first()
    if row is None or row.status != PAUSED:
        return False
    message = STOPPED_AFTER.format(n=row.rounds_done)
    return end_run(db, run_id, STOPPED, message, now, was=PAUSED, choice=STOP)


def cancel_research(db: Session, run_id: int, now: datetime) -> bool:
    """Cancel a run, saving the `cancel` choice.

    A paused run is cancelled at once. A running one is asked to (`cancel_requested`), and
    its job ends it at its next check, keeping what the judge accepted before. Returns
    False, changing nothing, for any other run, and for a running one already asked.
    """
    asked = db.execute(
        update(ResearchRun)
        .where(
            ResearchRun.id == run_id,
            ResearchRun.status == RUNNING,
            ResearchRun.cancel_requested.is_(False),
        )
        .values(cancel_requested=True)
    )
    if asked.rowcount == 1:
        _add_choice(db, run_id, CANCEL, now)
        db.commit()
        return True
    # Still in the same write transaction, so the job can't pause the run in between. One
    # it paused just before is cancelled here.
    return end_run(
        db, run_id, CANCELLED, CANCELLED_AT_ONCE, now, was=PAUSED, choice=CANCEL
    )


def end_run(
    db: Session,
    run_id: int,
    status: str,
    message: str,
    now: datetime,
    *,
    was: str = RUNNING,
    choice: str | None = None,
) -> bool:
    """Give the run its new `status` and add `message` to its log: the one place a run's
    ending is written, a pause included.

    It sets `finished_at` unless the run is only paused, and `message` as the `error` of a
    failed run. `choice`, when given, is the user's choice that ended it, saved with it. The
    log line's step is the new status.

    Only a run whose status is `was` changes, and a run asked to cancel is never paused or
    finished: its caller cancels it instead. Check and change are one conditional update,
    so a click on another thread can't slip in between. Returns whether the run changed.
    When it didn't, the session is rolled back and nothing is saved.
    """
    criteria = [ResearchRun.id == run_id, ResearchRun.status == was]
    if status in (PAUSED, FINISHED):
        criteria.append(ResearchRun.cancel_requested.is_(False))
    values: dict[str, object] = {"status": status}
    if status != PAUSED:
        values["finished_at"] = now
    if status == FAILED:
        values["error"] = message
    ended = db.execute(update(ResearchRun).where(*criteria).values(**values))
    if ended.rowcount != 1:
        db.rollback()
        return False
    if choice is not None:
        _add_choice(db, run_id, choice, now)
    add_event(db, run_id, status, message, now)  # commits it all
    return True


def recover_interrupted_research(db: Session, now: datetime) -> int:
    """Settle each run that a restart left running, and return how many there were.

    One asked to cancel is cancelled. Any other is paused with no rounds left
    (`rounds_allowed = rounds_done`), so that one more round carries it on. Call it after
    `recover_interrupted_jobs`. Nothing changes while a research job is queued: that job
    never started, and it will pick the run up.
    """
    queued = db.scalar(
        select(Job.id).where(Job.kind == RESEARCH_KIND, Job.status == QUEUED).limit(1)
    )
    if queued is not None:
        return 0
    runs = list(
        db.scalars(
            select(ResearchRun)
            .where(ResearchRun.status == RUNNING)
            .order_by(ResearchRun.id)
        )
    )
    for run in runs:
        if run.cancel_requested:
            end_run(db, run.id, CANCELLED, CANCELLED_KEPT, now)
        else:
            run.rounds_allowed = run.rounds_done  # saved by end_run's commit
            end_run(db, run.id, PAUSED, INTERRUPTED, now)
    return len(runs)


def _queue_research(db: Session, now: datetime) -> None:
    """Queue a research job, unless one is already queued, which will pick the run up.

    A research job still marked running doesn't count. A run is started or continued only
    while no run is running, so that job is done with its run and only waiting to be marked
    done. Joining it would leave the run with no job.
    """
    enqueue_job(db, RESEARCH_KIND, REQUESTED_BY_USER, now, behind_running=True)


def _add_choice(db: Session, run_id: int, choice: str, now: datetime) -> None:
    db.add(ResearchChoice(run_id=run_id, choice=choice, lane=None, made_at=now))


def add_event(
    db: Session,
    run_id: int,
    step: str,
    message: str,
    now: datetime,
    *,
    round: int | None = None,
    lane: Lane | None = None,
) -> None:
    """Add a line to the run's progress log."""
    db.add(
        ResearchEvent(
            run_id=run_id,
            round=round,
            lane=None if lane is None else Lane(lane).value,
            step=step,
            message=message,
            created_at=now,
        )
    )
    db.commit()


def lanes_short(db: Session, run: ResearchRun) -> list[Lane]:
    """The lanes in the run, in lane order, that haven't reached their target yet."""
    counts = recommended_counts(db)
    return [
        target.lane
        for target in run_targets(run)
        if target.target is not None
        and target.note is None
        and counts[target.lane] < target.target
    ]


@dataclass
class RunSummary:
    """A run with how its candidates were judged. `rejected` counts every verdict that isn't
    accepted, and `added` the accepted ones that went into the inventory."""

    run: ResearchRun
    accepted: int
    rejected: int
    added: int


def past_runs(db: Session, limit: int = 20) -> list[RunSummary]:
    """The newest `limit` runs, newest first, each with its counts: two queries."""
    runs = list(
        db.scalars(
            select(ResearchRun)
            .order_by(ResearchRun.created_at.desc(), ResearchRun.id.desc())
            .limit(limit)
        )
    )
    if not runs:
        return []
    counts = {
        run_id: (accepted, rejected, added)
        for run_id, accepted, rejected, added in db.execute(
            select(
                Candidate.run_id,
                func.sum(case((Candidate.verdict == ACCEPTED, 1), else_=0)),
                func.sum(case((Candidate.verdict != ACCEPTED, 1), else_=0)),
                func.sum(case((Candidate.added.is_(True), 1), else_=0)),
            )
            .where(Candidate.run_id.in_([run.id for run in runs]))
            .group_by(Candidate.run_id)
        )
    }
    return [RunSummary(run, *counts.get(run.id, (0, 0, 0))) for run in runs]
