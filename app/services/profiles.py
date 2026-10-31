"""Taste profiles: what the app has learned from published reviews, kept as versions.

There is one profile per lane and one per named group. A profile is first created when its
lane or group has a published review version. A review version belongs to its lane's
profile, and also to its named group's profile when it is tagged with a group
(`ReviewVersion.named_group_id`).

A profile version is structured JSON (`ProfileContent`): four sections of tagged lines and a
list of open questions. Profiles are derived data. Nothing here changes a review, and
review text goes to the model exactly as stored. Every version records the review versions it
learned from (`profile_version_source`) and the model and prompt that wrote it.

`validate_update` holds the evidence rules, so they are checked in code and never trusted to
the model. These functions run a fixed number of queries however many items they handle.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AiCall,
    Game,
    NamedGroup,
    Person,
    Profile,
    ProfileVersion,
    ProfileVersionSource,
    Review,
    ReviewVersion,
)
from app.services.reviews import LANE_LABELS, Lane

# --- the shape of a profile -------------------------------------------------------

Tag = Literal["stated", "observed", "guess"]


class ProfileLine(BaseModel):
    """One claim about the player's taste, with the evidence behind it."""

    # A misspelled name is an error (and gets the model's one retry) rather than a field
    # that is quietly dropped along with its lines.
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=400)
    tag: Tag
    games: list[int] = []  # appids
    reviews: list[int] = []  # review version ids


class ProfileContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loved: list[ProfileLine] = Field(default=[], max_length=15)
    dealbreakers: list[ProfileLine] = Field(default=[], max_length=15)
    patterns: list[ProfileLine] = Field(default=[], max_length=15)
    hunches: list[ProfileLine] = Field(default=[], max_length=15)
    # Plain questions, not claims, so they have no tag and no evidence.
    open_questions: list[str] = Field(default=[], max_length=10)


class ProfileUpdate(BaseModel):
    """What the model sends back: the whole new profile and a note on what changed."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileContent
    changelog: str = Field(min_length=1, max_length=300)


SECTION_LABELS = {
    "loved": "What you love",
    "dealbreakers": "Dealbreakers",
    "patterns": "Patterns",
    "hunches": "Hunches to test",
    "open_questions": "Open questions",
}

TAG_LABELS = {"stated": "Stated", "observed": "Observed", "guess": "Guess"}

# The sections whose lines carry a tag, games and reviews, in the order they are checked.
LINE_SECTIONS = ("loved", "dealbreakers", "patterns", "hunches")


def lane_key(lane: Lane | str) -> str:
    return f"lane:{Lane(lane).value}"


def group_key(group_id: int) -> str:
    return f"group:{group_id}"


# --- which profiles exist -----------------------------------------------------------


@dataclass
class ProfileTarget:
    """A profile that does or could exist: a lane or a named group, by the label to show."""

    key: str
    kind: str  # lane or group
    lane: Lane | None
    group_id: int | None
    label: str


@dataclass
class ProfileSummary:
    target: ProfileTarget
    profile: Profile | None
    latest: ProfileVersion | None
    version_count: int


def _lane_target(lane: Lane) -> ProfileTarget:
    return ProfileTarget(lane_key(lane), "lane", lane, None, LANE_LABELS[lane])


def _group_target(group: NamedGroup) -> ProfileTarget:
    return ProfileTarget(group_key(group.id), "group", None, group.id, group.name)


def _groups_by_name(db: Session, *conditions) -> list[NamedGroup]:
    return list(
        db.scalars(
            select(NamedGroup)
            .where(*conditions)
            .order_by(func.lower(NamedGroup.name), NamedGroup.id)
        )
    )


def targets_with_reviews(db: Session) -> list[ProfileTarget]:
    """The profiles that should exist: each lane with a published version, then each named
    group tagged on one. Lanes come in lane order, groups by name (ignoring case).

    A draft is not published, so it creates nothing. A group's label is its current name.
    """
    lanes_found = set(
        db.scalars(
            select(Review.lane)
            .join(ReviewVersion, ReviewVersion.review_id == Review.id)
            .distinct()
        )
    )
    tagged = (
        select(ReviewVersion.named_group_id)
        .where(ReviewVersion.named_group_id.is_not(None))
        .distinct()
    )
    return [_lane_target(lane) for lane in Lane if lane.value in lanes_found] + [
        _group_target(group) for group in _groups_by_name(db, NamedGroup.id.in_(tagged))
    ]


def ensure_profile(db: Session, target: ProfileTarget, now: datetime) -> Profile:
    """The profile for `target`, created (and saved) if it doesn't exist yet."""
    profile = db.scalar(select(Profile).where(Profile.key == target.key))
    if profile is not None:
        return profile
    profile = Profile(
        key=target.key,
        kind=target.kind,
        lane=target.lane.value if target.lane is not None else None,
        group_id=target.group_id,
        created_at=now,
    )
    db.add(profile)
    try:
        db.commit()
    except IntegrityError:
        # Another request made it between the lookup and the save.
        db.rollback()
        existing = db.scalar(select(Profile).where(Profile.key == target.key))
        if existing is None:
            raise
        return existing
    return profile


def list_profiles(db: Session) -> list[ProfileSummary]:
    """All four lanes, then every named group by name, each with its profile and newest
    version, or None where no profile exists yet. Three queries, however many profiles."""
    groups = _groups_by_name(db)
    profiles = {profile.key: profile for profile in db.scalars(select(Profile))}
    newest = (
        select(
            ProfileVersion.profile_id,
            func.max(ProfileVersion.version).label("version"),
            func.count().label("count"),
        )
        .group_by(ProfileVersion.profile_id)
        .subquery()
    )
    latest: dict[int, tuple[ProfileVersion, int]] = {
        version.profile_id: (version, count)
        for version, count in db.execute(
            select(ProfileVersion, newest.c.count).join(
                newest,
                and_(
                    ProfileVersion.profile_id == newest.c.profile_id,
                    ProfileVersion.version == newest.c.version,
                ),
            )
        )
    }
    summaries = []
    for target in [_lane_target(lane) for lane in Lane] + [
        _group_target(group) for group in groups
    ]:
        profile = profiles.get(target.key)
        version, count = latest.get(profile.id if profile else -1, (None, 0))
        summaries.append(ProfileSummary(target, profile, version, count))
    return summaries


# --- what a profile has learned from ------------------------------------------------


def _belongs_to(profile: Profile):
    """The conditions that make a review version belong to `profile`, and whether they
    need the review's row (for its lane)."""
    if profile.kind == "lane":
        return Review.lane == profile.lane, True
    if profile.kind == "group":
        return ReviewVersion.named_group_id == profile.group_id, False
    raise ValueError(f"Unknown profile kind: {profile.kind!r}")


def unprocessed_versions(
    db: Session, profile: Profile, limit: int = 20
) -> list[ReviewVersion]:
    """The profile's review versions that none of its versions has learned from yet, oldest
    first (by publish time, then id), at most `limit`."""
    belongs, needs_review = _belongs_to(profile)
    learned = (
        select(ProfileVersionSource.review_version_id)
        .join(
            ProfileVersion,
            ProfileVersion.id == ProfileVersionSource.profile_version_id,
        )
        .where(ProfileVersion.profile_id == profile.id)
    )
    statement = select(ReviewVersion).where(belongs, ReviewVersion.id.not_in(learned))
    if needs_review:
        statement = statement.join(Review, Review.id == ReviewVersion.review_id)
    return list(
        db.scalars(
            statement.order_by(ReviewVersion.published_at, ReviewVersion.id).limit(
                limit
            )
        )
    )


def profile_sources(db: Session, profile: Profile) -> tuple[set[int], set[int]]:
    """Every review version any version of `profile` learned from, and those versions' appids."""
    rows = db.execute(
        select(ReviewVersion.id, Review.appid)
        .join(Review, Review.id == ReviewVersion.review_id)
        .join(
            ProfileVersionSource,
            ProfileVersionSource.review_version_id == ReviewVersion.id,
        )
        .join(
            ProfileVersion, ProfileVersion.id == ProfileVersionSource.profile_version_id
        )
        .where(ProfileVersion.profile_id == profile.id)
    ).all()
    return {version_id for version_id, _ in rows}, {appid for _, appid in rows}


def current_version(db: Session, profile: Profile) -> ProfileVersion | None:
    return db.scalar(
        select(ProfileVersion)
        .where(ProfileVersion.profile_id == profile.id)
        .order_by(ProfileVersion.version.desc())
        .limit(1)
    )


# --- what goes to the model ---------------------------------------------------------


def _names(db: Session, key, name, ids: set[int]) -> dict[int, str]:
    """{id: name} for the rows whose `key` column is in `ids`: one query, or none if no ids."""
    if not ids:
        return {}
    rows = db.execute(select(key, name).where(key.in_(ids)))
    return {row_id: row_name for row_id, row_name in rows}


def review_input(db: Session, versions: Iterable[ReviewVersion]) -> list[dict]:
    """The review versions as plain dicts for the prompt, in the order given.

    `text` and `group_text` are exactly as stored. Names are current ones (the game, the
    people on the version sorted by name, the named group). `earlier` is the rating and
    verdict of the review's previous version number, or None for the first. Pass versions
    that are loaded (as `unprocessed_versions` returns them): the lookups here take a fixed
    number of queries, but reading a version the session has expired costs one each.
    """
    versions = list(versions)
    if not versions:
        return []
    review_ids = {v.review_id for v in versions}
    reviews = {
        review.id: review
        for review in db.scalars(select(Review).where(Review.id.in_(review_ids)))
    }
    games = _names(db, Game.appid, Game.name, {r.appid for r in reviews.values()})
    person_ids = {link.person_id for v in versions for link in v.people}
    people = _names(db, Person.id, Person.name, person_ids)
    groups = _names(
        db,
        NamedGroup.id,
        NamedGroup.name,
        {v.named_group_id for v in versions if v.named_group_id is not None},
    )
    # Every version of these reviews, but only the columns the comparison needs.
    seen: dict[tuple[int, int], dict] = {
        (review_id, number): {"rating": rating, "verdict": verdict}
        for review_id, number, rating, verdict in db.execute(
            select(
                ReviewVersion.review_id,
                ReviewVersion.version,
                ReviewVersion.rating,
                ReviewVersion.verdict,
            ).where(ReviewVersion.review_id.in_(review_ids))
        )
    }
    items = []
    for v in versions:
        review = reviews[v.review_id]
        earlier = seen.get((v.review_id, v.version - 1))
        items.append(
            {
                "id": v.id,
                "ref": f"R{v.id}",
                "appid": review.appid,
                "game": games[review.appid],
                "lane": LANE_LABELS[Lane(review.lane)],
                "version": v.version,
                "published": v.published_at.date().isoformat(),
                "rating": v.rating,
                "verdict": v.verdict,
                "status": v.status,
                "walk_away": v.walk_away,
                "session_length": v.session_length,
                "complexity": v.complexity,
                "tags": list(v.tags),
                "played_with": sorted(
                    (people[link.person_id] for link in v.people),
                    key=lambda name: (name.casefold(), name),
                ),
                "group": groups.get(v.named_group_id),
                "text": v.text,
                "group_text": v.group_text,
                "steam_hours": v.steam_hours,
                "earlier": dict(earlier) if earlier is not None else None,
            }
        )
    return items


def play_history(db: Session, limit: int = 25) -> list[dict]:
    """Owned games with playtime, most hours first: appid, game, hours (one decimal) and
    last_played (an ISO date, or None)."""
    games = db.scalars(
        select(Game)
        .where(Game.owned.is_(True), Game.playtime_minutes > 0)
        .order_by(Game.playtime_minutes.desc(), Game.appid)
        .limit(limit)
    )
    return [
        {
            "appid": game.appid,
            "game": game.name,
            "hours": round(game.playtime_minutes / 60, 1),
            "last_played": (
                game.last_played_at.date().isoformat() if game.last_played_at else None
            ),
        }
        for game in games
    ]


# --- the evidence rules -------------------------------------------------------------


def validate_update(
    update: ProfileUpdate,
    *,
    review_ids: set[int],
    reviewed_appids: set[int],
    history_appids: set[int],
) -> list[str]:
    """Every way `update` breaks the evidence rules, each said once, in section order.

    `review_ids` are the review versions the profile has learned from (with the ones this
    update learns from), `reviewed_appids` their games, and `history_appids` the games in the
    play-history summary. The rules, per line:
    - a stated line cites a review the profile learned from
    - an observed line names at least two different reviewed games
    - every line in hunches is a guess
    - the games a line cites are reviewed games, or for a guess also play-history games
    - a line cites only reviews the profile learned from (any tag)
    Open questions are not checked beyond the schema. An empty list means the update is sound.
    """
    problems: list[str] = []

    def add(problem: str) -> None:
        if problem not in problems:
            problems.append(problem)

    for section in LINE_SECTIONS:
        label = SECTION_LABELS[section]
        for line in getattr(update.profile, section):
            if section == "hunches" and line.tag != "guess":
                add(f"{label}: every line must be a guess.")
            if line.tag == "stated" and not review_ids.intersection(line.reviews):
                add(f"{label}: a stated line must cite a review it learned from.")
            if (
                line.tag == "observed"
                and len(reviewed_appids.intersection(line.games)) < 2
            ):
                add(f"{label}: an observed line must name at least two reviewed games.")
            allowed = (
                reviewed_appids | history_appids
                if line.tag == "guess"
                else reviewed_appids
            )
            if not allowed.issuperset(line.games):
                add(f"{label}: a line cites a game that isn't in your reviews.")
            if not review_ids.issuperset(line.reviews):
                add(f"{label}: a line cites a review it didn't learn from.")
    return problems


# --- saving -------------------------------------------------------------------------


def save_version(
    db: Session,
    profile: Profile,
    update: ProfileUpdate,
    *,
    source_ids: list[int],
    call: AiCall,
    refresh_run_id: int | None,
    now: datetime,
) -> ProfileVersion:
    """Save `update` as the profile's next version, learned from the review versions in
    `source_ids`, in one commit. The model, provider and prompt come from the logged `call`.

    Raises `ValueError` for no sources, since a version must say what it learned from.
    Nothing is saved if anything fails.
    """
    if not source_ids:
        raise ValueError(
            "A profile version must learn from at least one review version."
        )
    try:
        last = db.scalar(
            select(func.max(ProfileVersion.version)).where(
                ProfileVersion.profile_id == profile.id
            )
        )
        version = ProfileVersion(
            profile_id=profile.id,
            version=(last or 0) + 1,
            content=update.profile.model_dump(),
            changelog=update.changelog,
            ai_call_id=call.id,
            refresh_run_id=refresh_run_id,
            model=call.model,
            provider_kind=call.provider_kind,
            prompt_name=call.prompt_name,
            prompt_version=call.prompt_version,
            created_at=now,
        )
        db.add(version)
        db.flush()  # gives the version its id for the source rows
        db.add_all(
            ProfileVersionSource(
                profile_version_id=version.id, review_version_id=review_version_id
            )
            for review_version_id in dict.fromkeys(source_ids)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return version
