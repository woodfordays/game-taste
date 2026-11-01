"""What the app knows about a game, kept as dated snapshots that are never edited.

There are four kinds of fact. Three come from Steam: `store` (the store details),
`reviews` (the review summary) and `news` (the newest patch-notes post). `players` comes
from research, with the page its counts came from in `source_url`. The latest snapshot of
each kind is the current fact, and older ones stay as history.
"""

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models import Game, GameFact
from app.services.steam import (
    NO_RESPONSE,
    PatchNews,
    ReviewSummary,
    SteamClient,
    SteamError,
    StoreDetails,
)

STORE = "store"
REVIEWS = "reviews"
NEWS = "news"
PLAYERS = "players"

# The kinds the refresh gets from Steam, in the order it asks for them: the store entry
# decides whether the other two are worth asking for.
STEAM_KINDS = (STORE, REVIEWS, NEWS)

# A fact older than this is fetched again before anything uses it.
FACTS_MAX_AGE = timedelta(days=7)

# The `Game.source` of a game that research brought in. It stays out of the Library until
# the user adds it (see `add_store_game`).
SOURCE_RESEARCH = "research"

# Steam calls that may fail one after another before a refresh gives up on the rest.
MAX_STEAM_FAILURES_IN_A_ROW = 3


def store_fact_data(details: StoreDetails | None, region: str) -> dict:
    """The `data` of a `store` fact: the details without the app id, with the region they
    were priced for. None (the store has nothing for the app there) is `available: False`."""
    if details is None:
        return {"available": False, "region": region}
    data = asdict(details)
    del data["appid"]
    return {**data, "available": True, "region": region}


def reviews_fact_data(summary: ReviewSummary) -> dict:
    """The `data` of a `reviews` fact."""
    return asdict(summary)


def news_fact_data(news: PatchNews) -> dict:
    """The `data` of a `news` fact, with the patch date as ISO text for the JSON column."""
    return {
        "latest_patch_at": news.latest_at.isoformat() if news.latest_at else None,
        "latest_patch_title": news.title,
    }


def _parse_time(value: object) -> datetime | None:
    """A datetime from the ISO text a fact keeps, or None if there is none to read."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Fact:
    """The current snapshot of one kind."""

    data: dict
    fetched_at: datetime
    source_url: str | None


@dataclass(frozen=True)
class GameVersion:
    """What changed a game for a player: whether it is in Early Access, and when it was
    last patched. A None is something not known."""

    early_access: bool | None
    latest_patch_at: datetime | None

    def as_json(self) -> dict:
        return {
            "early_access": self.early_access,
            "latest_patch_at": (
                self.latest_patch_at.isoformat() if self.latest_patch_at else None
            ),
        }

    @classmethod
    def from_json(cls, data: dict) -> "GameVersion":
        early_access = data.get("early_access")
        return cls(
            early_access=None if early_access is None else bool(early_access),
            latest_patch_at=_parse_time(data.get("latest_patch_at")),
        )

    def updated_since(self, earlier: "GameVersion") -> bool:
        """True when the game left or entered Early Access since `earlier`, or has a patch
        that `earlier` hadn't seen."""
        if (
            self.early_access is not None
            and earlier.early_access is not None
            and self.early_access != earlier.early_access
        ):
            return True
        if self.latest_patch_at is None:
            return False
        return (
            earlier.latest_patch_at is None
            or self.latest_patch_at > earlier.latest_patch_at
        )


@dataclass(frozen=True)
class GameFacts:
    """The current facts of one game, each kind a `Fact` or None when there is none.

    The properties read the facts and give back what they say. A store entry that is
    unavailable (the store has nothing for the game) reads as unknown throughout.
    """

    appid: int
    store: Fact | None = None
    reviews: Fact | None = None
    news: Fact | None = None
    players: Fact | None = None

    def _from_store(self, key: str, default: Any = None) -> Any:
        if self.store is None or not self.store.data.get("available"):
            return default
        return self.store.data.get(key, default)

    @property
    def is_game(self) -> bool:
        return self._from_store("type") == "game"

    @property
    def early_access(self) -> bool | None:
        return self._from_store("early_access")

    @property
    def is_free(self) -> bool | None:
        return self._from_store("is_free")

    @property
    def coming_soon(self) -> bool | None:
        return self._from_store("coming_soon")

    @property
    def categories(self) -> list[str]:
        return list(self._from_store("categories", []))

    @property
    def release_date(self) -> str:
        return self._from_store("release_date", "")

    @property
    def short_description(self) -> str:
        return self._from_store("short_description", "")

    @property
    def price_text(self) -> str | None:
        """Steam's own price text, such as "$19.99", or "Free" for a free game."""
        if self.is_free:
            return "Free"
        return self._from_store("price_text") or None

    @property
    def review_text(self) -> str | None:
        """Steam's score and how many reviews it rests on, such as "Very Positive (1,234
        reviews)"."""
        if self.reviews is None:
            return None
        score = self.reviews.data["score_desc"]
        total = self.reviews.data["total_reviews"]
        return f"{score} ({total:,} reviews)" if total else score

    @property
    def coop_max(self) -> int | None:
        """The most players a co-op mode takes, online or local, whichever is larger."""
        if self.players is None:
            return None
        most = [
            counts["max"]
            for kind in ("online_coop", "local_coop")
            if (counts := self.players.data.get(kind)) and counts.get("max") is not None
        ]
        return max(most) if most else None

    @property
    def version(self) -> GameVersion:
        patch = (
            _parse_time(self.news.data.get("latest_patch_at")) if self.news else None
        )
        return GameVersion(early_access=self.early_access, latest_patch_at=patch)

    def snapshot(self) -> dict:
        """The facts as plain data, for keeping with a decision: for each kind present,
        its data, when it was fetched (ISO text) and its source URL."""
        return {
            kind: {
                "data": fact.data,
                "fetched_at": fact.fetched_at.isoformat(),
                "source_url": fact.source_url,
            }
            for kind, fact in (
                (STORE, self.store),
                (REVIEWS, self.reviews),
                (NEWS, self.news),
                (PLAYERS, self.players),
            )
            if fact is not None
        }


def current_facts(db: Session, appids: Iterable[int]) -> dict[int, GameFacts]:
    """The current facts of each game in `appids`, from one query.

    Every app id asked for has an entry, which is empty for a game with no facts yet.
    Where two snapshots of a kind share a time, the one saved later is current.
    """
    wanted = list(dict.fromkeys(appids))
    found = {appid: {} for appid in wanted}
    if wanted:
        newest_first = (
            func.row_number()
            .over(
                partition_by=(GameFact.appid, GameFact.kind),
                order_by=(GameFact.fetched_at.desc(), GameFact.id.desc()),
            )
            .label("newest")
        )
        ranked = (
            select(GameFact, newest_first).where(GameFact.appid.in_(wanted)).subquery()
        )
        latest = aliased(GameFact, ranked)
        for row in db.scalars(select(latest).where(ranked.c.newest == 1)):
            found[row.appid][row.kind] = Fact(row.data, row.fetched_at, row.source_url)
    return {appid: GameFacts(appid, **facts) for appid, facts in found.items()}


def ensure_game(db: Session, appid: int, name: str, now: datetime) -> Game:
    """The game with this app id, added if the app doesn't know it yet.

    A known game is returned as it is. A new one is not owned and has no playtime, and its
    source is `research`, so it stays out of the Library until the user adds it.
    """
    game = db.get(Game, appid)
    if game is None:
        game = Game(
            appid=appid,
            name=name,
            owned=False,
            playtime_minutes=None,
            last_played_at=None,
            currently_playing=False,
            source=SOURCE_RESEARCH,
            created_at=now,
        )
        db.add(game)
    db.commit()
    return game


@dataclass
class FactsRefresh:
    """What a facts refresh did: how many snapshots it saved, and for each game it
    couldn't finish, why."""

    fetched: int = 0
    problems: dict[int, str] = field(default_factory=dict)


def _is_stale(fact: Fact | None, now: datetime) -> bool:
    return fact is None or now - fact.fetched_at > FACTS_MAX_AGE


def _needs_fetch(facts: GameFacts, now: datetime) -> bool:
    """Whether a refresh would ask Steam for anything for this game: its store fact is
    missing or old, or the store has the game and its reviews or news are."""
    if facts.store is None or _is_stale(facts.store, now):
        return True
    if facts.store.data.get("available") is False:
        return False
    return _is_stale(facts.reviews, now) or _is_stale(facts.news, now)


def _fetch(steam: SteamClient, kind: str, appid: int, region: str) -> dict:
    if kind == STORE:
        return store_fact_data(steam.app_details(appid, region), region)
    if kind == REVIEWS:
        return reviews_fact_data(steam.review_summary(appid))
    return news_fact_data(steam.patch_news(appid))


def refresh_store_facts(
    db: Session,
    steam: SteamClient,
    appids: Iterable[int],
    region: str,
    now: datetime,
) -> FactsRefresh:
    """Fetch the Steam facts that are missing or older than `FACTS_MAX_AGE`.

    Goes through `appids` in order, once each, and adds a snapshot for each of store,
    reviews and news that needs one. A store entry the store doesn't have is saved as a
    `store` fact that says so, and then that game's reviews and news aren't asked for.
    When Steam fails a call, the game's old snapshot stays current and the reason goes in
    `problems`. After `MAX_STEAM_FAILURES_IN_A_ROW` failed calls in a row it stops
    asking, and every game left that still needed something gets the "didn't respond"
    problem (one whose facts are all current has nothing to report), so an outage costs a
    few calls and not one for every game. Commits after each game.
    """
    wanted = list(dict.fromkeys(appids))
    current = current_facts(db, wanted)
    result = FactsRefresh()
    failures = 0
    for position, appid in enumerate(wanted):
        if failures >= MAX_STEAM_FAILURES_IN_A_ROW:
            for left in wanted[position:]:
                if _needs_fetch(current[left], now):
                    result.problems[left] = NO_RESPONSE
            break
        facts = current[appid]
        # None until a store fact says. An unavailable entry has no reviews or news.
        available = facts.store.data.get("available") if facts.store else None
        for kind in STEAM_KINDS:
            if kind != STORE and available is False:
                break
            if not _is_stale(getattr(facts, kind), now):
                continue
            try:
                data = _fetch(steam, kind, appid, region)
            except SteamError as failure:
                failures += 1
                result.problems.setdefault(appid, str(failure))
                if failures >= MAX_STEAM_FAILURES_IN_A_ROW:
                    break
                continue
            failures = 0
            db.add(
                GameFact(
                    appid=appid, kind=kind, data=data, source_url=None, fetched_at=now
                )
            )
            result.fetched += 1
            if kind == STORE:
                available = data["available"]
        db.commit()
    return result


def save_player_counts(
    db: Session, appid: int, data: dict, source_url: str | None, now: datetime
) -> None:
    """Save research's player counts for a game as a new `players` snapshot.

    `data` is `{"online_coop", "local_coop", "competitive"}`, each `{"min", "max"}` or
    None, and `source_url` the page the counts came from.
    """
    db.add(
        GameFact(
            appid=appid, kind=PLAYERS, data=data, source_url=source_url, fetched_at=now
        )
    )
    db.commit()
