"""Where the refresh stands, for the status the user sees on Settings and the Profiles pages.

The status is read from the newest refresh job and the `RefreshRun` it wrote, so what is shown
is a record of what happened. The counts come from the run's `details`, one entry for each
profile, and never from its stored counters: a profile that saved a version and then failed
on a later batch is counted once, as failed.
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs import PENDING, REFRESH_KIND
from app.models import Job, RefreshRun
from app.services.settings import (
    DEFAULT_REFRESH_TIME,
    REFRESH_TIME_SETTING,
    get_setting,
)

IDLE = "idle"

# What the progress bar says while a refresh is pending, step by step.
QUEUED_LABEL = "Refresh queued."
STARTING_LABEL = "Refreshing profiles."
STEAM_LABEL = "Syncing your Steam library."
PROFILES_LABEL = "Updating profiles."
PROFILE_LABEL = "Updating {label}."
COUNT = "{number} of {total}"

# The outcomes the refresh records for a profile (see app/routines/refresh.py).
UPDATED = "updated"
FAILED = "failed"


@dataclass(frozen=True)
class FailedProfile:
    """A profile the refresh couldn't update, and why."""

    label: str
    message: str
    saved_through: int | None  # the newest version saved before it failed, if any


@dataclass(frozen=True)
class Progress:
    """The progress bar of a pending refresh: what it is doing (`label`) and, once it has
    counted its profiles, which one it is on ("2 of 5"). The bar keeps moving rather than
    filling, because nearly all the time goes on model calls whose length nothing can tell."""

    label: str
    count: str | None = None

    @property
    def key(self) -> str:
        """What the bar shows, as one string. A poll that would show the same answers with
        no content, so the bar on screen isn't redrawn and its motion doesn't restart."""
        return f"{self.label}|{self.count or ''}"


@dataclass(frozen=True)
class RefreshStatus:
    """`state` is "queued" or "running" while a refresh is pending, else "idle". `job` is the
    newest refresh job and `run` its `RefreshRun`, when it has one."""

    state: Literal["idle", "queued", "running"]
    job: Job | None
    run: RefreshRun | None

    @property
    def busy(self) -> bool:
        return self.state != IDLE

    @property
    def progress(self) -> Progress | None:
        """The bar while a refresh is pending, from the steps its run has recorded; None when
        none is."""
        if self.state == "queued":
            return Progress(QUEUED_LABEL)
        if self.state != "running":
            return None
        if self.run is None:  # picked up, and its run not written yet
            return Progress(STARTING_LABEL)
        total = self.run.profiles_total
        if total is None:  # the Steam sync comes before the profiles are counted
            return Progress(STEAM_LABEL)
        if total == 0:
            return Progress(PROFILES_LABEL)
        checked = min(self.run.profiles_checked, total)
        current = self.run.current_profile
        if current is None:  # between two profiles, or past the last
            return Progress(PROFILES_LABEL, COUNT.format(number=checked, total=total))
        number = min(checked + 1, total)
        return Progress(
            PROFILE_LABEL.format(label=current), COUNT.format(number=number, total=total)
        )

    @property
    def _details(self) -> list[dict]:
        return list(self.run.details) if self.run is not None else []

    @property
    def updated(self) -> int:
        """How many profiles the last run updated."""
        return sum(1 for entry in self._details if entry.get("outcome") == UPDATED)

    @property
    def failed(self) -> int:
        """How many profiles the last run couldn't update."""
        return sum(1 for entry in self._details if entry.get("outcome") == FAILED)

    @property
    def failures(self) -> list[FailedProfile]:
        """The profiles that failed, in the order the run tried them."""
        return [
            FailedProfile(
                label=entry.get("label", ""),
                message=entry.get("message", ""),
                saved_through=max(entry["versions"]) if entry.get("versions") else None,
            )
            for entry in self._details
            if entry.get("outcome") == FAILED
        ]


def refresh_status(db: Session) -> RefreshStatus:
    """The state of the refresh, its newest job and that job's run."""
    job = db.scalars(
        select(Job).where(Job.kind == REFRESH_KIND).order_by(Job.id.desc()).limit(1)
    ).first()
    if job is None:
        return RefreshStatus(IDLE, None, None)
    run = db.scalars(select(RefreshRun).where(RefreshRun.job_id == job.id)).first()
    state = job.status if job.status in PENDING else IDLE
    return RefreshStatus(state, job, run)


def refresh_context(db: Session) -> dict:
    """What `partials/refresh_status.html` needs: the status, and the daily refresh time it
    mentions before any refresh has run."""
    return {
        "refresh": refresh_status(db),
        "refresh_time": get_setting(db, REFRESH_TIME_SETTING, DEFAULT_REFRESH_TIME)
        or DEFAULT_REFRESH_TIME,
    }
