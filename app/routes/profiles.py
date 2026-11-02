"""The Profiles pages: a card for every lane and named group, and a page for each profile
with its tagged lines, the games behind them, its earlier versions and a changelog.

A profile page is read only. `?version=` shows an earlier version; a number that isn't a
version of the profile is a 404, like an unknown lane or group. htmx asks for just the body
below the title (the version line, the version select and the sections), so choosing a
version doesn't reload the page. Its history restore wants the whole page, and the two
answers share a URL, so `Vary` keeps a cache from handing one out for the other.
"""

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import NamedGroup, ProfileVersion
from app.services.profiles import (
    LINE_SECTIONS,
    SECTION_LABELS,
    TAG_LABELS,
    ProfileContent,
    ProfileSummary,
    ProfileTarget,
    VersionStamp,
    find_profile,
    game_names,
    group_key,
    lane_key,
    list_profiles,
    profile_version,
    targets_with_reviews,
    version_history,
)
from app.services.refresh_status import refresh_context
from app.services.reviews import LANE_LABELS, Lane
from app.web import TAG_TONES, is_htmx, parse_id, render

router = APIRouter()

# What a profile with no version says, by kind of profile. A profile is built by the refresh,
# not when a review is published, so a lane or group with published reviews and no version
# yet is waiting for the next refresh, which is a different thing from having no reviews.
EMPTY_MESSAGES = {
    "lane": "No reviews in this lane yet. Profiles fill in after you publish reviews.",
    "group": "No reviews tagged with this group yet.",
}
WAITING_MESSAGES = {
    "lane": (
        "Your reviews in this lane are waiting for the next refresh. "
        "The profile builds then."
    ),
    "group": (
        "Reviews tagged with this group are waiting for the next refresh. "
        "The profile builds then."
    ),
}

UNKNOWN_GAME = "Unknown game"


def _no_version_message(db: Session, key: str, kind: str) -> str:
    """What to say for the profile with this key and no version: the reviews are waiting for
    the refresh when it has published reviews (a draft is not one), else there are none."""
    waiting = any(target.key == key for target in targets_with_reviews(db))
    return (WAITING_MESSAGES if waiting else EMPTY_MESSAGES)[kind]


def _href(target: ProfileTarget) -> str:
    if target.lane is not None:
        return f"/profiles/lane/{target.lane.value}"
    return f"/profiles/group/{target.group_id}"


@dataclass
class ProfileCard:
    """One card on the list."""

    summary: ProfileSummary
    href: str
    empty: str  # what to say when the profile has no version yet: no reviews, or waiting


@router.get("/profiles")
def profiles_page(request: Request, db: Session = Depends(get_db)):
    """A card for each of the four lanes, then for each named group by name."""
    waiting = {target.key for target in targets_with_reviews(db)}
    cards = [
        ProfileCard(
            summary,
            _href(summary.target),
            (WAITING_MESSAGES if summary.target.key in waiting else EMPTY_MESSAGES)[
                summary.target.kind
            ],
        )
        for summary in list_profiles(db)
    ]
    return render(
        request, "profiles.html", nav="profiles", cards=cards, **refresh_context(db)
    )


@dataclass
class LineView:
    text: str
    tag_label: str
    tone: str
    games: list[str]  # names, each game once, in the order the line cites them


@dataclass
class SectionView:
    key: str
    label: str
    lines: list[LineView]


@dataclass
class ShownVersion:
    """The version on screen and what the page needs to draw it."""

    stamp: VersionStamp
    sections: list[SectionView]  # the sections that have lines, in section order
    questions: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.sections and not self.questions


def _shown(db: Session, version: ProfileVersion) -> ShownVersion:
    """The version's sections with the names of the games behind each line.

    Every cited game is looked up in one query, so the cost doesn't grow with the lines.
    A game that is no longer in the table is shown as "Unknown game".
    """
    content = ProfileContent.model_validate(version.content)
    names = game_names(
        db,
        (
            appid
            for section in LINE_SECTIONS
            for line in getattr(content, section)
            for appid in line.games
        ),
    )
    sections = []
    for key in LINE_SECTIONS:
        lines = [
            LineView(
                text=line.text,
                tag_label=TAG_LABELS[line.tag],
                tone=TAG_TONES[line.tag],
                games=[
                    names.get(appid, UNKNOWN_GAME)
                    for appid in dict.fromkeys(line.games)
                ],
            )
            for line in getattr(content, key)
        ]
        if lines:
            sections.append(SectionView(key, SECTION_LABELS[key], lines))
    return ShownVersion(
        VersionStamp(version.version, version.created_at, version.changelog),
        sections,
        content.open_questions,
    )


def _profile_page(
    request: Request,
    db: Session,
    *,
    key: str,
    kind: str,
    label: str,
    base_url: str,
    version: str | None,
):
    """The page for the profile with this key, showing `version` (the newest without one)."""
    wanted = None
    if version is not None:
        wanted = parse_id(version)
        if wanted is None:
            raise HTTPException(status_code=404, detail="No such version.")

    profile = find_profile(db, key)
    history = version_history(db, profile) if profile is not None else []
    if wanted is None and history:
        wanted = history[0].version
    elif wanted is not None and wanted not in {stamp.version for stamp in history}:
        raise HTTPException(status_code=404, detail="No such version.")

    shown = None
    if profile is not None and wanted is not None:
        version_row = profile_version(db, profile, wanted)
        shown = _shown(db, version_row) if version_row is not None else None

    # Only a page with no version needs to know whether reviews are waiting for the refresh.
    empty = _no_version_message(db, key, kind) if shown is None else ""
    fragment = (
        is_htmx(request) and request.headers.get("hx-history-restore-request") != "true"
    )
    whole_page = not (fragment and shown)
    response = render(
        request,
        "profile.html" if whole_page else "partials/profile_lines.html",
        nav="profiles",
        label=label,
        base_url=base_url,
        empty=empty,
        history=history,
        shown=shown,
        questions_label=SECTION_LABELS["open_questions"],
        # The status sits in the page's header, which the body alone doesn't have.
        **(refresh_context(db) if whole_page else {}),
    )
    response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
    return response


@router.get("/profiles/lane/{lane}")
def lane_profile(
    lane: str,
    request: Request,
    version: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        chosen = Lane(lane)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such lane.") from None
    return _profile_page(
        request,
        db,
        key=lane_key(chosen),
        kind="lane",
        label=LANE_LABELS[chosen],
        base_url=f"/profiles/lane/{chosen.value}",
        version=version,
    )


@router.get("/profiles/group/{group_id}")
def group_profile(
    group_id: str,
    request: Request,
    version: str | None = None,
    db: Session = Depends(get_db),
):
    # The id is parsed here, not by FastAPI, so that a number no row could have is a 404
    # like any other group that doesn't exist, and never reaches the database.
    parsed = parse_id(group_id)
    group = None if parsed is None else db.get(NamedGroup, parsed)
    if group is None:
        raise HTTPException(status_code=404, detail="No such group.")
    return _profile_page(
        request,
        db,
        key=group_key(group.id),
        kind="group",
        label=group.name,
        base_url=f"/profiles/group/{group.id}",
        version=version,
    )
