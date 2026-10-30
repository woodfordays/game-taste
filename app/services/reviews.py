"""Reviews: one per game per lane, written as a draft and published as numbered versions.

Publishing never overwrites anything. Each publish adds a version (1, 2, 3 ...) to the
lane's history, and every earlier version stays exactly as it was, including who played.
A draft is separate work in progress: saving one never touches a published version, and
it is removed only when the same form is published.

Text the user wrote (`text`, `group_text`, `session_length`, `tags_raw`) is stored as
given, never trimmed or altered. The only thing that looks at it is the emptiness check
before publishing, which strips a copy.

Ties in "most recent" (two things with the same timestamp) are broken the same way
everywhere: the higher `ReviewVersion.id` wins, and between lanes the later one in the
order solo, two, three_four, five_plus wins.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Literal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import (
    Game,
    NamedGroup,
    Person,
    Review,
    ReviewDraft,
    ReviewVersion,
    ReviewVersionPerson,
)
from app.services.people import PICK_PEOPLE, group_member_ids
from app.services.tags import normalize_tags


class Lane(StrEnum):
    SOLO = "solo"
    TWO = "two"
    THREE_FOUR = "three_four"
    FIVE_PLUS = "five_plus"


LANE_LABELS = {
    Lane.SOLO: "Solo",
    Lane.TWO: "Two players",
    Lane.THREE_FOUR: "Three to four",
    Lane.FIVE_PLUS: "Five or more",
}

# The most players a lane holds, you included. Five or more has no limit.
LANE_MAX: dict[Lane, int | None] = {
    Lane.SOLO: 1,
    Lane.TWO: 2,
    Lane.THREE_FOUR: 4,
    Lane.FIVE_PLUS: None,
}

_LANE_INDEX = {lane: index for index, lane in enumerate(Lane)}

STATUSES = ("finished", "playing", "demo", "dropped")
VERDICTS = ("loved", "liked", "mixed", "disliked")
WALK_AWAYS = ("yes", "partly", "no")
COMPLEXITIES = ("too_light", "about_right", "too_much")

_CHOICES = {
    "status": STATUSES,
    "verdict": VERDICTS,
    "walk_away": WALK_AWAYS,
    "complexity": COMPLEXITIES,
}

RATING_INVALID = "Pick a rating from 1 to 10."
NOTHING_TO_PUBLISH = "Add a rating or a few words before publishing."
OPTION_INVALID = "Pick one of the listed options."

# SQLite integers are 64-bit; a draft rating beyond that could not be stored at all.
_INT_LIMIT = 2**63

FormSource = Literal["draft", "published", "new"]


class ReviewError(Exception):
    """Something the user can fix. The message says what happened and what to do."""


@dataclass
class ReviewForm:
    rating: int | None = None
    text: str = ""
    tags_raw: str = ""
    group_text: str = ""
    session_length: str = ""
    status: str | None = None
    verdict: str | None = None
    walk_away: str | None = None
    complexity: str | None = None
    people: list[int] = field(default_factory=list)
    named_group_id: int | None = None


@dataclass
class LaneState:
    has_published: bool
    has_draft: bool


@dataclass
class CardSummary:
    rating: int | None
    tags: list[str]
    lane: Lane | None
    has_draft: bool


@dataclass
class ReviewRow:
    game: Game
    lane: Lane
    latest: ReviewVersion | None
    has_draft: bool
    updated_at: datetime


# --- checks ---------------------------------------------------------------------


def _whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_game(db: Session, appid: int) -> Game:
    game = db.get(Game, appid)
    if game is None:
        raise LookupError(f"No game with app id {appid}")
    return game


def _clean(db: Session, lane: Lane, form: ReviewForm) -> ReviewForm:
    """A copy of `form` that is fit to store, or `ReviewError` if it isn't.

    Checks the choice fields and the people and group ids, and drops what the lane
    can't hold: Solo has no other players, so it loses `people`, `named_group_id` and
    `group_text` (which also means stale ids on a Solo form are no reason to refuse it).
    Repeated people are listed once, in the order given. Nothing else is changed.
    """
    for name, allowed in _CHOICES.items():
        value = getattr(form, name)
        if value is not None and value not in allowed:
            raise ReviewError(OPTION_INVALID)
    people = list(dict.fromkeys(form.people))
    group_id, group_text = form.named_group_id, form.group_text
    if lane is Lane.SOLO:
        people, group_id, group_text = [], None, ""
    # Ids are compared with the saved ones, not looked up, so a huge or odd id is
    # just unknown rather than an error from the database.
    if people and not set(people) <= set(db.scalars(select(Person.id))):
        raise ReviewError(PICK_PEOPLE)
    if group_id is not None and group_id not in set(db.scalars(select(NamedGroup.id))):
        raise ReviewError(PICK_PEOPLE)
    return replace(form, people=people, named_group_id=group_id, group_text=group_text)


# --- rows -----------------------------------------------------------------------


def _find_review(db: Session, appid: int, lane: Lane) -> Review | None:
    return db.scalar(
        select(Review).where(Review.appid == appid, Review.lane == lane.value)
    )


def _review_for_write(db: Session, appid: int, lane: Lane, now: datetime) -> Review:
    review = _find_review(db, appid, lane)
    if review is None:
        review = Review(appid=appid, lane=lane.value, created_at=now)
        db.add(review)
        db.flush()  # gives the review its id
    return review


def _latest_version(db: Session, review_id: int) -> ReviewVersion | None:
    return db.scalar(
        select(ReviewVersion)
        .where(ReviewVersion.review_id == review_id)
        .order_by(ReviewVersion.version.desc())
        .limit(1)
    )


def _latest_versions(db: Session, review_ids: list[int]) -> dict[int, ReviewVersion]:
    """The newest published version of each of the reviews, by review id."""
    newest = (
        select(
            ReviewVersion.review_id, func.max(ReviewVersion.version).label("version")
        )
        .where(ReviewVersion.review_id.in_(review_ids))
        .group_by(ReviewVersion.review_id)
        .subquery()
    )
    versions = db.scalars(
        select(ReviewVersion).join(
            newest,
            and_(
                ReviewVersion.review_id == newest.c.review_id,
                ReviewVersion.version == newest.c.version,
            ),
        )
    )
    return {version.review_id: version for version in versions}


@dataclass
class _LaneReview:
    """A lane review that has something to show: a draft, a published version or both."""

    review: Review
    latest: ReviewVersion | None
    draft: ReviewDraft | None

    @property
    def lane(self) -> Lane:
        return Lane(self.review.lane)

    @property
    def updated_at(self) -> datetime:
        times = []
        if self.draft is not None:
            times.append(self.draft.updated_at)
        if self.latest is not None:
            times.append(self.latest.published_at)
        return max(times)


def _lane_reviews(
    db: Session, *, appid: int | None = None, lane: Lane | None = None
) -> list[_LaneReview]:
    statement = select(Review)
    if appid is not None:
        statement = statement.where(Review.appid == appid)
    if lane is not None:
        statement = statement.where(Review.lane == lane.value)
    reviews = list(db.scalars(statement))
    if not reviews:
        return []
    ids = [review.id for review in reviews]
    drafts = {
        draft.review_id: draft
        for draft in db.scalars(
            select(ReviewDraft).where(ReviewDraft.review_id.in_(ids))
        )
    }
    latest = _latest_versions(db, ids)
    return [
        _LaneReview(review, latest.get(review.id), drafts.get(review.id))
        for review in reviews
        if review.id in drafts or review.id in latest
    ]


# --- forms ----------------------------------------------------------------------


def load_form(db: Session, appid: int, lane: Lane) -> tuple[ReviewForm, FormSource]:
    """The form to show for a lane, and where it came from.

    The draft if there is one, otherwise the latest published version (its tags joined
    with ", "), otherwise an empty form. Each call returns a form of its own.
    """
    lane = Lane(lane)
    review = _find_review(db, appid, lane)
    if review is None:
        return ReviewForm(), "new"
    draft = db.get(ReviewDraft, review.id)
    if draft is not None:
        return (
            ReviewForm(
                rating=draft.rating,
                text=draft.text,
                tags_raw=draft.tags_raw,
                group_text=draft.group_text,
                session_length=draft.session_length,
                status=draft.status,
                verdict=draft.verdict,
                walk_away=draft.walk_away,
                complexity=draft.complexity,
                people=list(draft.people),
                named_group_id=draft.named_group_id,
            ),
            "draft",
        )
    version = _latest_version(db, review.id)
    if version is None:
        return ReviewForm(), "new"
    return (
        ReviewForm(
            rating=version.rating,
            text=version.text,
            tags_raw=", ".join(version.tags),
            group_text=version.group_text,
            session_length=version.session_length,
            status=version.status,
            verdict=version.verdict,
            walk_away=version.walk_away,
            complexity=version.complexity,
            people=[person.person_id for person in version.people],
            named_group_id=version.named_group_id,
        ),
        "published",
    )


def save_draft(
    db: Session, appid: int, lane: Lane, form: ReviewForm, now: datetime
) -> None:
    """Save `form` as the lane's draft, replacing the one it had.

    A draft is work in progress, so it needs no rating and no words. It is still
    refused for a choice that isn't on the list, an unknown person or group, or a
    rating that isn't a whole number. Raises `LookupError` for an unknown app id,
    with nothing written.
    """
    lane = Lane(lane)
    _require_game(db, appid)
    if form.rating is not None and not (
        _whole_number(form.rating) and abs(form.rating) < _INT_LIMIT
    ):
        raise ReviewError(RATING_INVALID)
    clean = _clean(db, lane, form)
    fields = {
        "rating": clean.rating,
        "text": clean.text,
        "group_text": clean.group_text,
        "tags_raw": clean.tags_raw,
        "status": clean.status,
        "verdict": clean.verdict,
        "walk_away": clean.walk_away,
        "complexity": clean.complexity,
        "session_length": clean.session_length,
        "named_group_id": clean.named_group_id,
        # A new list each time: JSON columns don't see in-place changes.
        "people": list(clean.people),
        "updated_at": now,
    }
    try:
        review = _review_for_write(db, appid, lane, now)
        draft = db.get(ReviewDraft, review.id)
        if draft is None:
            db.add(ReviewDraft(review_id=review.id, **fields))
        else:
            for name, value in fields.items():
                setattr(draft, name, value)
        db.commit()
    except Exception:
        db.rollback()
        raise


def publish(
    db: Session, appid: int, lane: Lane, form: ReviewForm, now: datetime
) -> ReviewVersion:
    """Publish `form` as the lane's next version and remove the lane's draft.

    The version, the people on it and the draft's removal are saved together, so a
    failure leaves the draft and every earlier version as they were. Raises
    `ReviewError` for a rating that isn't 1 to 10, for no rating and no words, and for
    the same bad choices and ids a draft is refused for. Raises `LookupError` for an
    unknown app id.
    """
    lane = Lane(lane)
    game = _require_game(db, appid)
    if form.rating is not None and not (
        _whole_number(form.rating) and 1 <= form.rating <= 10
    ):
        raise ReviewError(RATING_INVALID)
    if form.rating is None and form.text.strip() == "":
        raise ReviewError(NOTHING_TO_PUBLISH)
    clean = _clean(db, lane, form)  # Solo loses its co-op fields here
    try:
        review = _review_for_write(db, appid, lane, now)
        last = db.scalar(
            select(func.max(ReviewVersion.version)).where(
                ReviewVersion.review_id == review.id
            )
        )
        version = ReviewVersion(
            review_id=review.id,
            version=(last or 0) + 1,
            rating=clean.rating,
            text=clean.text,
            group_text=clean.group_text,
            tags=normalize_tags(clean.tags_raw),
            status=clean.status,
            verdict=clean.verdict,
            walk_away=clean.walk_away,
            session_length=clean.session_length,
            complexity=clean.complexity,
            named_group_id=clean.named_group_id,
            steam_hours=(
                None if game.playtime_minutes is None else game.playtime_minutes / 60
            ),
            published_at=now,
            people=[
                ReviewVersionPerson(person_id=person_id)
                for person_id in effective_people(
                    db, clean.people, clean.named_group_id
                )
            ],
        )
        db.add(version)
        draft = db.get(ReviewDraft, review.id)
        if draft is not None:
            db.delete(draft)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return version


def effective_people(
    db: Session, people: list[int], named_group_id: int | None
) -> list[int]:
    """Everyone who played: `people` plus the named group's members, once each, sorted."""
    everyone = set(people)
    if named_group_id is not None:
        everyone.update(group_member_ids(db, named_group_id))
    return sorted(everyone)


# --- lane hints -----------------------------------------------------------------


def lane_for_head_count(heads: int) -> Lane:
    """The lane for this many players, you included."""
    if heads <= 1:
        return Lane.SOLO
    if heads == 2:
        return Lane.TWO
    if heads <= 4:
        return Lane.THREE_FOUR
    return Lane.FIVE_PLUS


def head_count_hint(lane: Lane, people_count: int) -> str | None:
    """A nudge toward the right lane when you plus the people tagged don't fit.

    None when the lane has room. Fewer players than the lane holds is fine, since
    people who weren't tagged may have played, so only too many gets a hint.
    """
    limit = LANE_MAX[Lane(lane)]
    heads = 1 + people_count
    if limit is None or heads <= limit:
        return None
    label = LANE_LABELS[lane_for_head_count(heads)]
    return (
        f"You and {people_count} others make {heads} players. "
        f"This looks like the {label} lane."
    )


# --- what a game has ------------------------------------------------------------


def lane_states(db: Session, appid: int) -> dict[Lane, LaneState]:
    """For each of the four lanes, in order, whether it has a published version or a draft."""
    states = {lane: LaneState(False, False) for lane in Lane}
    for found in _lane_reviews(db, appid=appid):
        states[found.lane] = LaneState(
            has_published=found.latest is not None, has_draft=found.draft is not None
        )
    return states


def default_lane(db: Session, appid: int) -> Lane:
    """The lane the game's dialog opens on: the one worked on most recently, else Solo.

    A lane's time is the newer of its draft's last save and its latest publish.
    """
    found = _lane_reviews(db, appid=appid)
    if not found:
        return Lane.SOLO
    return max(found, key=lambda f: (f.updated_at, _LANE_INDEX[f.lane])).lane


def card_summary(db: Session, appid: int) -> CardSummary:
    """What a game's card shows: the latest published version across all lanes.

    `has_draft` is true if any lane has a draft, even when nothing is published yet.
    """
    latest = db.execute(
        select(ReviewVersion, Review.lane)
        .join(Review, Review.id == ReviewVersion.review_id)
        .where(Review.appid == appid)
        .order_by(ReviewVersion.published_at.desc(), ReviewVersion.id.desc())
        .limit(1)
    ).first()
    has_draft = (
        db.scalar(
            select(ReviewDraft.review_id)
            .join(Review, Review.id == ReviewDraft.review_id)
            .where(Review.appid == appid)
            .limit(1)
        )
        is not None
    )
    if latest is None:
        return CardSummary(None, [], None, has_draft)
    version, lane = latest
    return CardSummary(version.rating, list(version.tags), Lane(lane), has_draft)


def history(db: Session, appid: int, lane: Lane) -> list[ReviewVersion]:
    """Every published version of the lane's review, newest first."""
    return list(
        db.scalars(
            select(ReviewVersion)
            .join(Review, Review.id == ReviewVersion.review_id)
            .where(Review.appid == appid, Review.lane == Lane(lane).value)
            .order_by(ReviewVersion.version.desc())
        )
    )


def list_reviews(
    db: Session, lane: Lane | None, verdict: str | None
) -> list[ReviewRow]:
    """One row per game and lane that has a draft or a published version, newest first.

    A row's time is the newer of its draft's last save and its latest publish. `lane`
    keeps one lane. `verdict` keeps the rows whose latest published version has that
    verdict, so a draft-only row never matches; an empty `verdict` filters nothing.
    """
    found = _lane_reviews(db, lane=None if lane is None else Lane(lane))
    if verdict:
        found = [
            f for f in found if f.latest is not None and f.latest.verdict == verdict
        ]
    if not found:
        return []
    appids = {f.review.appid for f in found}
    games = {
        game.appid: game
        for game in db.scalars(select(Game).where(Game.appid.in_(appids)))
    }
    rows = [
        ReviewRow(
            game=games[f.review.appid],
            lane=f.lane,
            latest=f.latest,
            has_draft=f.draft is not None,
            updated_at=f.updated_at,
        )
        for f in found
    ]
    rows.sort(
        key=lambda row: (
            row.updated_at,
            row.latest.id if row.latest is not None else 0,
            _LANE_INDEX[row.lane],
            row.game.appid,
        ),
        reverse=True,
    )
    return rows
