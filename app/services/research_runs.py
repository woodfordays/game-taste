"""Research runs: what a run set out to do, what the user chose and how it went.

A run has a target for each lane (`LaneTarget`), a log of what it did (`ResearchEvent`) and the
user's choices (`ResearchChoice`, never edited). Only one run is active at a time, which is
one that is running or paused. The work itself is done elsewhere; this module keeps the
records and the plan.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Candidate, ResearchChoice, ResearchEvent, ResearchRun
from app.services.queue import ACCEPTED, TARGET, TOP_UP, recommended_counts
from app.services.reviews import LANE_LABELS, Lane

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
