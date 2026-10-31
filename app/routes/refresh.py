"""Refresh now, and the status of the refresh.

Both routes answer with the status partial and nothing else, always with status 200: htmx
doesn't swap an error status, and a refresh that is already pending is not an error, the status
just shows it. While a refresh is queued or running the partial polls `GET /refresh/status` and
swaps itself. The poll that first finds it finished answers with the `profiles-refreshed` event,
which the Profiles pages listen for to load what the refresh made. A poll that would show the
same progress as the page already does (`shown`) answers 204 instead, which htmx doesn't swap,
so the moving bar isn't redrawn every two seconds.
"""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs import (
    PENDING,
    REFRESH_KIND,
    REQUESTED_BY_USER,
    enqueue_job,
    notify_worker,
)
from app.services.refresh_status import IDLE, refresh_context, refresh_status
from app.web import render

router = APIRouter()

REFRESHED_EVENT = "profiles-refreshed"
PARTIAL = "partials/refresh_status.html"


def _status_response(request: Request, db: Session, *, was_pending: bool):
    """The status partial. When the caller was waiting on a refresh (`was_pending`) and none is
    pending now, the answer also carries the `profiles-refreshed` event."""
    context = refresh_context(db)
    response = render(request, PARTIAL, **context)
    if was_pending and context["refresh"].state == IDLE:
        response.headers["HX-Trigger"] = REFRESHED_EVENT
    return response


@router.post("/refresh")
def refresh_now(request: Request, db: Session = Depends(get_db)):
    """Queue a refresh and show it, or show the one already queued or running."""
    app = request.app
    enqueue_job(db, REFRESH_KIND, REQUESTED_BY_USER, app.state.services.clock())
    notify_worker(app)
    # A refresh with nothing to do can be over before this answer is drawn, and then nothing
    # will poll for it: the page is told now. The usual answer is "queued" or "running".
    return _status_response(request, db, was_pending=True)


@router.get("/refresh/status")
def refresh_status_page(
    request: Request,
    was: str | None = None,
    shown: str | None = None,
    db: Session = Depends(get_db),
):
    """The status. `was` is the state the polling page last showed: when it was queued or
    running and the refresh is now over, the answer carries the `profiles-refreshed` event.
    `shown` is the key of the progress it showed: when that is still what a pending refresh
    would show, the answer is 204 and the page keeps what it has."""
    progress = refresh_status(db).progress
    if shown is not None and progress is not None and progress.key == shown:
        return Response(status_code=204)
    return _status_response(request, db, was_pending=was in PENDING)
