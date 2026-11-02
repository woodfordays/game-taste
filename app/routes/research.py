"""The Research page: start a run from a dialog, watch its log, choose at a pause, and read
the past runs.

The status area (`#research-status`) is one partial that every state of the page swaps for
itself. While a run is running it polls `GET /research/status` every two seconds, and a poll
that would show what the page already does (`shown`) answers 204, which htmx doesn't swap, so
the log isn't redrawn for nothing. The answer that first finds nothing active, or that ends
the run (stop or cancel), carries the `research-finished` event, which the Past runs table
listens for. Starting a run answers with an empty body, which clears `#dialog`, and the
`research-started` event, which the idle status listens for.

Every answer to a click is status 200, because htmx doesn't swap an error status: a refused
start comes back as the dialog again with the reason, and a click on a run that has moved on
just shows how it stands. Only a run that doesn't exist is a 404.
"""

from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.jobs import notify_worker
from app.models import ResearchRun
from app.services.profiles import current_version, find_profile, lane_key
from app.services.queue import TARGET, TOP_UP, recommended_counts
from app.services.research_runs import (
    ACTIVE,
    RUN_STATUS_LABELS,
    ResearchError,
    ResearchStatus,
    cancel_research,
    continue_research,
    past_runs,
    research_blockers,
    research_status,
    start_research,
    stop_research,
)
from app.services.reviews import LANE_LABELS, Lane
from app.web import parse_id, read_form, render

router = APIRouter()

STATUS_PARTIAL = "partials/research_status.html"
START_PARTIAL = "partials/research_start.html"

# Sent when a run is started, and when the page finds that nothing is active any more.
STARTED_EVENT = "research-started"
FINISHED_EVENT = "research-finished"

# What the start dialog says about a lane, which is one of these.
NO_PROFILE = "no_profile"
FULL = "full"
FILLS = "fills"


@dataclass
class LaneRow:
    """A lane in the start dialog: how many recommended games it has, and what a run does
    about it: `state` is NO_PROFILE (it can't be researched yet), FULL (it can grow by 5
    when ticked) or FILLS (it is taken up to the target)."""

    lane: Lane
    label: str
    count: int
    state: str


def _has_profile_version(db: Session, lane: Lane) -> bool:
    profile = find_profile(db, lane_key(lane))
    return profile is not None and current_version(db, profile) is not None


def _lane_rows(db: Session) -> list[LaneRow]:
    counts = recommended_counts(db)
    rows = []
    for lane in Lane:
        if not _has_profile_version(db, lane):
            state = NO_PROFILE
        elif counts[lane] >= TARGET:
            state = FULL
        else:
            state = FILLS
        rows.append(LaneRow(lane, LANE_LABELS[lane], counts[lane], state))
    return rows


def _dialog(
    request: Request,
    db: Session,
    *,
    error: str | None = None,
    ticked: AbstractSet[Lane] = frozenset(),
    still: bool = False,
):
    """The start dialog. `error` is shown above the lanes as a danger alert, `ticked` is the
    lanes whose "add 5 more" box is ticked, and `still` stops the entrance animation
    because the dialog is already on screen."""
    return render(
        request,
        START_PARTIAL,
        rows=_lane_rows(db),
        blockers=research_blockers(db, request.app.state.vault),
        error=error,
        ticked=ticked,
        still=still,
        target=TARGET,
        top_up=TOP_UP,
    )


def _status_response(request: Request, status: ResearchStatus, *, was_active: bool):
    """The status partial for `status`. When the caller was waiting on a run (`was_active`)
    and none is running or paused now, the answer also carries the `research-finished`
    event."""
    response = render(
        request, STATUS_PARTIAL, status=status, result_labels=RUN_STATUS_LABELS
    )
    if was_active and not status.active:
        response.headers["HX-Trigger"] = FINISHED_EVENT
    return response


def _ticked_lanes(data: FormData) -> set[Lane]:
    """The lanes whose "add 5 more" box was ticked. A value that isn't a lane is ignored."""
    ticked = set()
    for value in data.getlist("add_five"):
        try:
            ticked.add(Lane(value))
        except ValueError:
            continue
    return ticked


def _known_run(db: Session, run_id: str) -> int:
    """The id of the run with this id in the URL. An id no run could have is a 404, like
    any other run that doesn't exist, and never reaches the database."""
    parsed = parse_id(run_id)
    if (
        parsed is None
        or db.scalar(select(ResearchRun.id).where(ResearchRun.id == parsed)) is None
    ):
        raise HTTPException(status_code=404, detail="No such research run.")
    return parsed


@router.get("/research")
def research_page(request: Request, db: Session = Depends(get_db)):
    """The status of the run, then the past runs. The run that is active is in the status,
    so the table lists the rest."""
    return render(
        request,
        "research.html",
        nav="research",
        status=research_status(db),
        past=[summary for summary in past_runs(db) if summary.run.status not in ACTIVE],
        result_labels=RUN_STATUS_LABELS,
    )


@router.get("/research/status")
def research_status_page(
    request: Request,
    was: str | None = None,
    shown: str | None = None,
    db: Session = Depends(get_db),
):
    """The status. `was` is the state the polling page last showed: when it was running or
    paused and nothing is active now, the answer carries the `research-finished` event.
    `shown` is the key of the run it showed: when that is still what an active run would
    show, the answer is 204 and the page keeps what it has."""
    status = research_status(db)
    if shown is not None and status.active and status.key == shown:
        return Response(status_code=204)
    return _status_response(request, status, was_active=was in ACTIVE)


@router.get("/research/start")
def start_dialog(request: Request, db: Session = Depends(get_db)):
    """The dialog that starts a run: each lane with what a run does about it, and what has
    to be set up first."""
    return _dialog(request, db)


@router.post("/research")
def start(
    request: Request,
    data: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    """Start a run, taking each ticked lane 5 higher. A run that can't start answers with
    the dialog again, with the reason. One that starts wakes the worker and answers with an
    empty body, which closes the dialog."""
    app = request.app
    ticked = _ticked_lanes(data)
    try:
        start_research(db, app.state.vault, ticked, app.state.services.clock())
    except ResearchError as error:
        return _dialog(request, db, error=error.message, ticked=ticked, still=True)
    notify_worker(app)
    return HTMLResponse("", headers={"HX-Trigger": STARTED_EVENT})


@router.post("/research/{run_id}/continue")
def continue_run(run_id: str, request: Request, db: Session = Depends(get_db)):
    """Give a paused run one more round, and show the run."""
    app = request.app
    if continue_research(db, _known_run(db, run_id), app.state.services.clock()):
        notify_worker(app)
    return _status_response(request, research_status(db), was_active=True)


@router.post("/research/{run_id}/stop")
def stop_run(run_id: str, request: Request, db: Session = Depends(get_db)):
    """End a paused run here, and show how things stand."""
    stop_research(db, _known_run(db, run_id), request.app.state.services.clock())
    return _status_response(request, research_status(db), was_active=True)


@router.post("/research/{run_id}/cancel")
def cancel_run(run_id: str, request: Request, db: Session = Depends(get_db)):
    """Cancel a run: a paused one ends at once, a running one at its next check."""
    cancel_research(db, _known_run(db, run_id), request.app.state.services.clock())
    return _status_response(request, research_status(db), was_active=True)
