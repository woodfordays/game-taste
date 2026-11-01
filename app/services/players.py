"""How many players a game seats, found by research, and the lane filter that reads it.

Research searches the web for a game's player counts and has the Everyday model read the
results (`lookup_player_counts`). Counts are kept only with a source that was really among
the results. The lane filter (`supports_lane`, `lane_mismatch`) then says which lanes a game
can enter:

- Solo: Steam's store categories include "Single-player".
- Two players: an online or local co-op range includes 2.
- Three to four: an online or local co-op range overlaps 3 to 4.
- Five or more: an online or local co-op max is 5 or more.

"Overlaps" means `max >= low` and `min <= high`, so a co-op that needs 4 players doesn't fit
Two players. A game with no known co-op count fits no co-op lane, and competitive counts
never qualify a game.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.models import Game
from app.prompts import load_prompt
from app.services.ai import Job, complete_json
from app.services.game_facts import (
    FACTS_MAX_AGE,
    GameFacts,
    save_player_counts,
)
from app.services.reviews import Lane
from app.services.search import SearchClient, search_and_log
from app.services.secrets import Vault

if TYPE_CHECKING:
    from app.main import Services

# The head counts a lane seats, as (fewest, most). Five or more has no most.
LANE_HEADS: dict[Lane, tuple[int, int | None]] = {
    Lane.SOLO: (1, 1),
    Lane.TWO: (2, 2),
    Lane.THREE_FOUR: (3, 4),
    Lane.FIVE_PLUS: (5, None),
}

PLAYER_QUERY = '"{name}" number of players co-op multiplayer'

# The results one lookup reads.
PLAYER_SEARCH_RESULTS = 5

# The store category that makes a game Solo.
SINGLE_PLAYER = "Single-player"


class PlayerRange(BaseModel):
    """The fewest and the most players a mode allows."""

    model_config = ConfigDict(extra="forbid")

    min: int = Field(ge=1, le=1000)
    max: int = Field(ge=1, le=1000)

    @model_validator(mode="after")
    def _min_is_not_above_max(self) -> "PlayerRange":
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) is above max ({self.max})")
        return self


class PlayerCounts(BaseModel):
    """The model's reading of the search results. A mode the results don't give is None,
    and `source_url` is the result the counts came from."""

    model_config = ConfigDict(extra="forbid")

    online_coop: PlayerRange | None = None
    local_coop: PlayerRange | None = None
    competitive: PlayerRange | None = None
    source_url: str | None = None


def needs_player_counts(facts: GameFacts, now: datetime) -> bool:
    """Whether research has to look the counts up: there is no players fact, or it is
    older than `FACTS_MAX_AGE`. A lookup that found nothing still counts as one."""
    return facts.players is None or now - facts.players.fetched_at > FACTS_MAX_AGE


def lookup_player_counts(
    db: Session,
    vault: Vault,
    search: SearchClient,
    game: Game,
    *,
    services: "Services",
    run_ref: str,
    now: datetime,
) -> None:
    """Search the web for the game's player counts, have the model read them, and save
    the result as a new `players` fact.

    The counts are kept only when the model's `source_url` is exactly the URL of one of the
    results, so a page it made up can't vouch for them. Otherwise every range is saved as
    None, with no source. A game with no known count is saved too, so it isn't looked up
    again for `FACTS_MAX_AGE`. A `SearchError` or a `StructuredReplyError` is left to the
    caller, and nothing is saved, so the next run tries again.
    """
    results = search_and_log(
        db,
        search,
        PLAYER_QUERY.format(name=game.name),
        max_results=PLAYER_SEARCH_RESULTS,
        run_ref=run_ref,
        now=now,
    )
    counts, _call_id = complete_json(
        db,
        vault,
        Job.EVERYDAY,
        load_prompt("player_counts"),
        {
            "game": game.name,
            "results": [
                {"title": item.title, "url": item.url, "content": item.content}
                for item in results
            ],
        },
        PlayerCounts,
        services=services,
        run_ref=run_ref,
        now=now,
    )
    if counts.source_url not in {item.url for item in results} - {""}:
        counts = PlayerCounts()  # no page among the results vouches for these
    save_player_counts(
        db,
        game.appid,
        counts.model_dump(exclude={"source_url"}),
        counts.source_url,
        now,
    )


def _coop_ranges(facts: GameFacts) -> list[tuple[int, int]]:
    """The (min, max) of each co-op mode, online and local, that has a count."""
    if facts.players is None:
        return []
    ranges = []
    for kind in ("online_coop", "local_coop"):
        counts = facts.players.data.get(kind)
        if counts and counts.get("min") is not None and counts.get("max") is not None:
            ranges.append((counts["min"], counts["max"]))
    return ranges


def supports_lane(facts: GameFacts, lane: Lane) -> bool:
    """Whether the game can enter the lane, by the rules at the top of this module."""
    if lane is Lane.SOLO:
        return SINGLE_PLAYER in facts.categories
    low, high = LANE_HEADS[lane]
    return any(
        most >= low and (high is None or least <= high)
        for least, most in _coop_ranges(facts)
    )


def lane_mismatch(facts: GameFacts, lane: Lane) -> str | None:
    """Why the game doesn't fit the lane, as a phrase that starts in lower case and has no
    full stop, or None when it fits."""
    if supports_lane(facts, lane):
        return None
    if lane is Lane.SOLO:
        return "Steam doesn't list it as single-player"
    ranges = _coop_ranges(facts)
    if not ranges:
        return "no co-op player count is known"
    low, _high = LANE_HEADS[lane]
    most = max(top for _least, top in ranges)
    if most < low:
        return f"its co-op supports up to {most} player{'' if most == 1 else 's'}"
    # Some mode reaches the lane, and each one that does starts above its top head count.
    # The smallest of those starts is the fewest players the game can have there.
    fewest = min(least for least, top in ranges if top >= low)
    return f"its co-op needs at least {fewest} players"
