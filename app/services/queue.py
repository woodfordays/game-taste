"""The inventory of recommended games for each lane, and the history of every proposal.

Research proposes games for a lane and a judge rules on them. Each proposal is kept as a
`Candidate`, with the verdict and the facts it was judged on, whether or not it was
accepted. An accepted one goes into the lane's inventory (`InventoryEntry`) while the lane
has room for it. A candidate is never edited, and neither is a status change: an entry's
status moves, and each move is a new `InventoryStatusChange`.

The functions here take a fixed number of queries however many games they handle.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, union
from sqlalchemy.orm import Session, aliased

from app.models import (
    Candidate,
    Game,
    InventoryEntry,
    InventoryStatusChange,
    Review,
    ReviewVersion,
)
from app.services.game_facts import GameFacts, GameVersion, current_facts
from app.services.reviews import Lane

# A lane with this many recommended games is full. A full lane grows only when the user asks
# for more, and then by TOP_UP.
TARGET = 20
TOP_UP = 5

RECOMMENDED = "recommended"
IN_PROGRESS = "in_progress"
PLAYED = "played"
DECLINED = "declined"

STATUS_LABELS = {
    RECOMMENDED: "Recommended",
    IN_PROGRESS: "In progress",
    PLAYED: "Played",
    DECLINED: "Declined",
}

# Where a proposed game came from.
WEB_RESEARCH = "web_research"
CANDIDATE_HISTORY = "candidate_history"
OWNED_BACKLOG = "owned_backlog"

SOURCE_LABELS = {
    WEB_RESEARCH: "Found by web research",
    CANDIDATE_HISTORY: "From the candidate history",
    OWNED_BACKLOG: "From your backlog",
}

# What the judge said about a proposal. A proposal it said nothing about is NO_VERDICT.
ACCEPTED = "accepted"
REJECTED = "rejected"
NO_VERDICT = "no_verdict"

VERDICTS = (ACCEPTED, REJECTED, NO_VERDICT)


@dataclass
class JudgedProposal:
    """A proposed game and the judge's ruling on it, ready to be saved as a `Candidate`.

    `fits` is a list of `{"section", "text", "tag", "how"}`, one for each line of the lane's
    profile the proposal cites. `version` and `facts` are what was known of the game when it
    was judged (`GameFacts.version` and `GameFacts.snapshot()`). `list_rejected` is as the
    judge's ruling set it: true when the judge rejected the whole batch at once.
    """

    appid: int
    source: str
    fits: list[dict]
    risk: str
    version: GameVersion
    facts: dict
    verdict: str
    reason: str
    list_rejected: bool
    recommender_call_id: int | None
    judge_call_id: int | None


def save_judged(
    db: Session,
    judged: list[JudgedProposal],
    *,
    run_id: int,
    round: int,
    lane: Lane,
    room: int,
    profile_version_id: int,
    now: datetime,
) -> list[Candidate]:
    """Save a `Candidate` for each of `judged`, in order, and put the accepted ones into the
    lane's inventory, in one commit.

    The first `room` accepted proposals are added: each gets an `InventoryEntry` with status
    recommended, ranked after the lane's highest rank so far (0 for a lane with none), and a
    status change that names its candidate. Later accepted ones are kept with `added` false.
    A game the lane already has an entry for (a declined one that came back) moves back to
    recommended on its new candidate, with a new rank and a new status change. Nothing is
    saved if anything fails, and `ValueError` is raised for a verdict or source that isn't
    one of the allowed values. The candidates come back loaded, so reading them costs no
    more queries.
    """
    lane_name = Lane(lane).value
    for item in judged:
        if item.verdict not in VERDICTS:
            raise ValueError(f"Not a verdict: {item.verdict!r}")
        if item.source not in SOURCE_LABELS:
            raise ValueError(f"Not a source: {item.source!r}")
    if not judged:
        return []
    candidates = []
    room_left = room
    for item in judged:
        added = item.verdict == ACCEPTED and room_left > 0
        if added:
            room_left -= 1
        candidates.append(
            Candidate(
                run_id=run_id,
                round=round,
                lane=lane_name,
                appid=item.appid,
                source=item.source,
                fits=item.fits,
                risk=item.risk,
                version=item.version.as_json(),
                facts=item.facts,
                profile_version_id=profile_version_id,
                verdict=item.verdict,
                reason=item.reason,
                list_rejected=item.list_rejected,
                added=added,
                recommender_call_id=item.recommender_call_id,
                judge_call_id=item.judge_call_id,
                created_at=now,
            )
        )
    try:
        db.add_all(candidates)
        db.flush()  # gives the candidates their ids
        # Read now: a commit expires the objects, and each one read afterwards costs a query.
        saved_ids = [candidate.id for candidate in candidates]
        _add_to_inventory(db, lane_name, [c for c in candidates if c.added], now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    # One query loads them all again, rather than one for each when they are read.
    list(db.scalars(select(Candidate).where(Candidate.id.in_(saved_ids))))
    return candidates


def _add_to_inventory(
    db: Session, lane_name: str, accepted: list[Candidate], now: datetime
) -> None:
    """Make each accepted candidate's game recommended in the lane, ranked in order after
    the lane's highest rank, and record the change."""
    if not accepted:
        return
    highest = db.scalar(
        select(func.max(InventoryEntry.rank)).where(InventoryEntry.lane == lane_name)
    )
    rank = 0 if highest is None else highest + 1
    entries = {
        entry.appid: entry
        for entry in db.scalars(
            select(InventoryEntry).where(
                InventoryEntry.lane == lane_name,
                InventoryEntry.appid.in_({c.appid for c in accepted}),
            )
        )
    }
    changed: list[tuple[InventoryEntry, Candidate]] = []
    for candidate in accepted:
        entry = entries.get(candidate.appid)
        if entry is None:
            entry = InventoryEntry(
                lane=lane_name,
                appid=candidate.appid,
                status=RECOMMENDED,
                rank=rank,
                candidate_id=candidate.id,
                created_at=now,
            )
            db.add(entry)
            entries[candidate.appid] = entry
        else:
            entry.status = RECOMMENDED
            entry.rank = rank
            entry.candidate_id = candidate.id
        rank += 1
        changed.append((entry, candidate))
    db.flush()  # gives the new entries their ids
    db.add_all(
        InventoryStatusChange(
            entry_id=entry.id,
            status=RECOMMENDED,
            reason=None,
            candidate_id=candidate.id,
            changed_at=now,
        )
        for entry, candidate in changed
    )


def recommended_counts(db: Session) -> dict[Lane, int]:
    """How many recommended games each lane has, for all four lanes."""
    counts = {lane: 0 for lane in Lane}
    rows = db.execute(
        select(InventoryEntry.lane, func.count())
        .where(InventoryEntry.status == RECOMMENDED)
        .group_by(InventoryEntry.lane)
    )
    for lane_name, count in rows:
        counts[Lane(lane_name)] = count
    return counts


def status_counts(db: Session, lane: Lane) -> dict[str, int]:
    """How many games the lane has in each of the four statuses."""
    counts = {status: 0 for status in STATUS_LABELS}
    rows = db.execute(
        select(InventoryEntry.status, func.count())
        .where(InventoryEntry.lane == Lane(lane).value)
        .group_by(InventoryEntry.status)
    )
    for status, count in rows:
        counts[status] = count
    return counts


def excluded_appids(db: Session, lane: Lane) -> set[int]:
    """The games research leaves out when it looks for new ones for the lane: those with an
    inventory entry in the lane (whatever its status), those currently playing, and those
    with a published review in the lane."""
    lane_name = Lane(lane).value
    return set(
        db.scalars(
            union(
                select(InventoryEntry.appid).where(InventoryEntry.lane == lane_name),
                select(Game.appid).where(Game.currently_playing.is_(True)),
                select(Review.appid)
                .join(ReviewVersion, ReviewVersion.review_id == Review.id)
                .where(Review.lane == lane_name),
            )
        )
    )


def rejected_in_run(db: Session, run_id: int, lane: Lane) -> list[Candidate]:
    """The run's candidates in the lane that the judge didn't accept, oldest first."""
    return list(
        db.scalars(
            select(Candidate)
            .where(
                Candidate.run_id == run_id,
                Candidate.lane == Lane(lane).value,
                Candidate.verdict != ACCEPTED,
            )
            .order_by(Candidate.created_at, Candidate.id)
        )
    )


def earlier_candidates(db: Session, lane: Lane, run_id: int) -> list[Candidate]:
    """The newest candidate of each game in the lane, from the runs other than `run_id`,
    newest first. Where two share a time, the one saved later is newer."""
    newest_first = (
        func.row_number()
        .over(
            partition_by=Candidate.appid,
            order_by=(Candidate.created_at.desc(), Candidate.id.desc()),
        )
        .label("newest")
    )
    ranked = (
        select(Candidate, newest_first)
        .where(Candidate.lane == Lane(lane).value, Candidate.run_id != run_id)
        .subquery()
    )
    latest = aliased(Candidate, ranked)
    return list(
        db.scalars(
            select(latest)
            .where(ranked.c.newest == 1)
            .order_by(latest.created_at.desc(), latest.id.desc())
        )
    )


def may_return(
    candidate: Candidate, version_now: GameVersion, profile_version_id: int | None
) -> bool:
    """Whether the game of an earlier candidate may be proposed again.

    An accepted one that wasn't added (the lane had no room) may, and one that was added is
    in the inventory, so it may not. One that was rejected, or that the judge said nothing
    about, may once something has changed: the game was updated since (see
    `GameVersion.updated_since`), or the lane's profile (`profile_version_id`, None for no
    profile) is newer than the version it was judged against.
    """
    if candidate.verdict == ACCEPTED:
        return not candidate.added
    if version_now.updated_since(GameVersion.from_json(candidate.version)):
        return True
    return (
        profile_version_id is not None
        and profile_version_id > candidate.profile_version_id
    )


@dataclass
class EntryView:
    """An inventory entry with what a card shows: the game, the accepted proposal behind
    it and the game's current facts."""

    entry: InventoryEntry
    game: Game
    candidate: Candidate
    facts: GameFacts

    @property
    def costs_money(self) -> bool:
        """True when the game isn't owned and isn't known to be free."""
        return not self.game.owned and self.facts.is_free is not True


def lane_entries(db: Session, lane: Lane, status: str) -> list[EntryView]:
    """The lane's entries with this status, in two queries.

    Recommended puts the games that cost nothing to get (owned or free) before those that
    cost money, each group in rank order. Any other status comes with the most recently
    changed first.
    """
    if status not in STATUS_LABELS:
        raise ValueError(f"Not a status: {status!r}")
    statement = (
        select(InventoryEntry, Game, Candidate)
        .join(Game, Game.appid == InventoryEntry.appid)
        .join(Candidate, Candidate.id == InventoryEntry.candidate_id)
        .where(InventoryEntry.lane == Lane(lane).value, InventoryEntry.status == status)
    )
    if status == RECOMMENDED:
        statement = statement.order_by(InventoryEntry.rank, InventoryEntry.id)
    else:
        last_change = (
            select(
                InventoryStatusChange.entry_id,
                func.max(InventoryStatusChange.changed_at).label("changed_at"),
                func.max(InventoryStatusChange.id).label("change_id"),
            )
            .group_by(InventoryStatusChange.entry_id)
            .subquery()
        )
        statement = statement.outerjoin(
            last_change, last_change.c.entry_id == InventoryEntry.id
        ).order_by(last_change.c.changed_at.desc(), last_change.c.change_id.desc())
    rows = db.execute(statement).all()
    facts = current_facts(db, [game.appid for _, game, _ in rows])
    views = [
        EntryView(entry, game, candidate, facts[game.appid])
        for entry, game, candidate in rows
    ]
    if status == RECOMMENDED:
        # Python's sort is stable, so each group keeps its rank order.
        views.sort(key=lambda view: view.costs_money)
    return views
