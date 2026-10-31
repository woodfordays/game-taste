"""The numbers on Home: what the library and the published reviews add up to.

Only published reviews count. A draft is work in progress, so it is left out until it is
published. A review counts once per lane, as it is stored, and its rating is the one in its
newest version. Play time is Steam's, so only owned games have any.
"""

from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import Game, Review, ReviewVersion


@dataclass(frozen=True)
class LibraryStats:
    games_owned: int
    games_reviewed: int  # games with at least one published review, owned or not
    reviews_published: int  # one per game per lane
    play_minutes: int  # across the owned games
    played_games: int  # owned games with some play time
    average_rating: (
        float | None
    )  # over the rated reviews' newest versions; None with none
    rated_reviews: int
    most_played: Game | None  # the owned game with the most play time, if any has some

    @property
    def is_empty(self) -> bool:
        """Nothing to count yet: no games and no reviews."""
        return self.games_owned == 0 and self.reviews_published == 0


def library_stats(db: Session) -> LibraryStats:
    owned = Game.owned.is_(True)
    games_owned, play_minutes = db.execute(
        select(func.count(), func.coalesce(func.sum(Game.playtime_minutes), 0)).where(
            owned
        )
    ).one()
    played_games = (
        db.scalar(select(func.count()).where(owned, Game.playtime_minutes > 0)) or 0
    )
    most_played = db.scalars(
        select(Game)
        .where(owned, Game.playtime_minutes > 0)
        .order_by(Game.playtime_minutes.desc(), func.lower(Game.name), Game.name)
        .limit(1)
    ).first()

    # Each published review's newest version. A review with no version has only a draft.
    newest = (
        select(
            ReviewVersion.review_id, func.max(ReviewVersion.version).label("version")
        )
        .group_by(ReviewVersion.review_id)
        .subquery()
    )
    latest = (
        select(Review.appid, ReviewVersion.rating)
        .join(newest, newest.c.review_id == Review.id)
        .join(
            ReviewVersion,
            and_(
                ReviewVersion.review_id == newest.c.review_id,
                ReviewVersion.version == newest.c.version,
            ),
        )
        .subquery()
    )
    reviews_published, games_reviewed, rated_reviews, average = db.execute(
        select(
            func.count(),
            func.count(func.distinct(latest.c.appid)),
            func.count(latest.c.rating),
            func.avg(latest.c.rating),
        )
    ).one()

    return LibraryStats(
        games_owned=games_owned,
        games_reviewed=games_reviewed,
        reviews_published=reviews_published,
        play_minutes=play_minutes,
        played_games=played_games,
        average_rating=float(average) if average is not None else None,
        rated_reviews=rated_reviews,
        most_played=most_played,
    )
