"""The Recommendations page: each lane's inventory by status, one pick for each game.

A pick explains itself: the profile lines it fits with their evidence tags, its main risk,
where it was found and its facts, each with the date it was checked. The page only reads in
this plan; a pick has no actions yet.

The lane and the status are in the URL (`?lane=solo&status=recommended`, those being the
defaults), so a tab can be linked to and the lane control and the tabs are plain links. A lane
or status that doesn't exist is a 404. The page costs a fixed number of queries however many
picks it lists (`status_counts` and `lane_entries`, which is two).
"""

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Game
from app.services.profiles import TAG_LABELS
from app.services.queue import (
    DECLINED,
    IN_PROGRESS,
    PLAYED,
    RECOMMENDED,
    SOURCE_LABELS,
    STATUS_LABELS,
    EntryView,
    lane_entries,
    status_counts,
)
from app.services.reviews import LANE_LABELS, Lane
from app.web import TAG_TONES, render

router = APIRouter()

DEFAULT_LANE = Lane.SOLO
DEFAULT_STATUS = RECOMMENDED

# What a tab with nothing in it says, by status, for the lane's label.
EMPTY_MESSAGES = {
    RECOMMENDED: "No recommendations for {lane} yet. Run research to find some.",
    IN_PROGRESS: "Nothing in progress in {lane}.",
    PLAYED: "Nothing played from {lane}'s recommendations yet.",
    DECLINED: "Nothing declined in {lane}.",
}

COMING_SOON = "Coming soon"


@dataclass
class Choice:
    """A link in the lane control or in the status tabs. `count` is the tab's number of
    games, and None for a lane."""

    label: str
    href: str
    current: bool
    count: int | None = None


@dataclass
class FactRow:
    """One fact in a pick: its name, what it says, and when that was checked."""

    label: str
    value: str
    fetched_at: datetime


@dataclass
class FitView:
    """A line of the lane's profile that a pick fits: its tag as a badge, its text, and how
    the game meets it (empty when the proposal didn't say)."""

    tag_label: str
    tone: str
    text: str
    how: str


@dataclass
class PickView:
    """A pick as the page draws it (partials/pick.html)."""

    game: Game
    early_access: bool
    source: str
    facts: list[FactRow]
    fits: list[FitView]
    risk: str


def _href(lane: Lane, status: str) -> str:
    return f"/recommendations?lane={lane.value}&status={status}"


def _fact_rows(view: EntryView, lane: Lane) -> list[FactRow]:
    """The facts a pick shows, in order. Each row is dated by the snapshot it was read from,
    so a value is never shown without when it was checked, and a fact that isn't known is
    left out instead of being guessed.

    - Players: the co-op maximum, in the lanes that have other players.
    - Price: Steam's own text, or "Coming soon" for a game with no price that isn't out yet.
      A game the user owns has no price to pay, so it has no row.
    - Reviews: Steam's score and how many reviews it rests on.
    - Release: the release date as the store gives it.
    """
    facts = view.facts
    rows = []
    most = facts.coop_max
    if lane is not Lane.SOLO and facts.players is not None and most is not None:
        rows.append(
            FactRow(
                "Players",
                f"Up to {most} player{'' if most == 1 else 's'}",
                facts.players.fetched_at,
            )
        )
    if facts.store is not None and not view.game.owned:
        price = facts.price_text or (COMING_SOON if facts.coming_soon else None)
        if price:
            rows.append(FactRow("Price", price, facts.store.fetched_at))
    if facts.reviews is not None and facts.review_text:
        rows.append(FactRow("Reviews", facts.review_text, facts.reviews.fetched_at))
    if facts.store is not None and facts.release_date:
        rows.append(FactRow("Release", facts.release_date, facts.store.fetched_at))
    return rows


def _fit_view(fit: dict) -> FitView:
    tag = fit.get("tag")
    return FitView(
        tag_label=TAG_LABELS.get(tag, str(tag or "")),
        tone=TAG_TONES.get(tag, "neutral"),
        text=fit.get("text") or "",
        how=fit.get("how") or "",
    )


def pick_view(view: EntryView, lane: Lane) -> PickView:
    """What the page needs to draw one entry of `lane`."""
    candidate = view.candidate
    return PickView(
        game=view.game,
        early_access=view.facts.early_access is True,
        source=SOURCE_LABELS.get(candidate.source, candidate.source),
        facts=_fact_rows(view, lane),
        fits=[_fit_view(fit) for fit in candidate.fits],
        risk=candidate.risk,
    )


@router.get("/recommendations")
def recommendations_page(
    request: Request,
    lane: str = DEFAULT_LANE.value,
    status: str = DEFAULT_STATUS,
    db: Session = Depends(get_db),
):
    """The picks in one lane with one status, under the lane control and the status tabs."""
    try:
        chosen = Lane(lane)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such lane.") from None
    if status not in STATUS_LABELS:
        raise HTTPException(status_code=404, detail="No such status.")

    counts = status_counts(db, chosen)
    picks = [pick_view(view, chosen) for view in lane_entries(db, chosen, status)]
    lane_label = LANE_LABELS[chosen]
    return render(
        request,
        "recommendations.html",
        nav="recommendations",
        # The lane control keeps the status on screen, and the tabs keep the lane.
        lanes=[
            Choice(LANE_LABELS[each], _href(each, status), each is chosen)
            for each in Lane
        ],
        tabs=[
            Choice(label, _href(chosen, key), key == status, counts[key])
            for key, label in STATUS_LABELS.items()
        ],
        picks=picks,
        empty=EMPTY_MESSAGES[status].format(lane=lane_label),
        # Only an empty Recommended tab has research to point to.
        to_research=status == RECOMMENDED,
    )
