"""The Recommender's and the review judge's steps in a research round.

The Recommender proposes games for a lane from a pool the app gathered, and the Review judge
accepts or rejects each one. The models write the replies and the code checks them, so the
two roles keep to their parts whatever a model says:

- The Recommender can only propose games that are in the pool, and every pick has to cite a
  real line of the lane's profile and state its main risk (`check_proposals`).
- The judge can only rule on the games that were proposed, so it can never add one. A
  proposal it says nothing about is `no_verdict` (`apply_verdicts`).

The replies are read leniently: a schema checks only the shape, and these checks do the rest.
This module holds the schemas, those checks and one function for each model call. The loop
that gathers the pool, calls these in rounds and saves the rulings is the research routine's.
The prompts are Jinja2 text files, `research_plan`, `research_extract`, `research_propose`
and `research_judge`, in `app/prompts/`, so they can be tuned without touching this code.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models import Game
from app.prompts import load_prompt
from app.services.ai import Job, complete_json
from app.services.game_facts import GameFacts
from app.services.profiles import LINE_SECTIONS, TAG_LABELS
from app.services.queue import ACCEPTED, NO_VERDICT, REJECTED, SOURCE_LABELS
from app.services.reviews import Lane
from app.services.search import SearchResult
from app.services.secrets import Vault

if TYPE_CHECKING:
    from app.main import Services

# How much of each search result the extraction step reads.
RESULT_CHARS = 600

NO_VERDICT_REASON = "The judge gave no verdict on this game."

# --- the shape of the replies -----------------------------------------------------------


class ResearchPlan(BaseModel):
    """The Recommender's plan for a round: owned games to look at, and web searches."""

    backlog: list[int] = Field(default=[], max_length=10)  # appids
    queries: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default=[], max_length=4
    )


class FoundGame(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = ""  # the search result that names it


class FoundGames(BaseModel):
    """The games a set of search results recommends."""

    games: list[FoundGame] = Field(default=[], max_length=12)


class Fit(BaseModel):
    line: str = Field(max_length=10)  # a profile line id, "L3"
    how: str = Field(default="", max_length=300)


class Proposal(BaseModel):
    appid: int
    fits: list[Fit] = Field(default=[], max_length=5)
    risk: str = Field(default="", max_length=300)


class Proposals(BaseModel):
    proposals: list[Proposal] = Field(default=[], max_length=25)


class Verdict(BaseModel):
    appid: int
    accept: bool
    reason: str = Field(default="", max_length=300)


class Verdicts(BaseModel):
    verdicts: list[Verdict] = Field(default=[], max_length=25)
    reject_all: str | None = Field(default=None, max_length=300)


# --- what the prompts are shown ---------------------------------------------------------


@dataclass(frozen=True)
class ProfileLineRef:
    """One line of a lane's profile, with the id the models cite it by."""

    id: str  # "L1", "L2", ...
    section: str
    text: str
    tag: str  # stated, observed or guess


def number_lines(content: dict) -> list[ProfileLineRef]:
    """The lines of a profile's content numbered `L1`, `L2`, ... across `LINE_SECTIONS` in
    order. Open questions are questions, not claims, so they aren't lines."""
    lines: list[ProfileLineRef] = []
    for section in LINE_SECTIONS:
        for line in content.get(section, []):
            lines.append(
                ProfileLineRef(f"L{len(lines) + 1}", section, line["text"], line["tag"])
            )
    return lines


@dataclass
class PoolGame:
    """A game in the pool a round proposes from.

    `source` is where it came from (a key of `SOURCE_LABELS`). `history` holds its earlier
    verdicts in this lane, newest first, each `{"when": ISO date, "verdict", "reason"}`.
    """

    game: Game
    facts: GameFacts
    source: str
    history: list[dict] = field(default_factory=list)


def _day(moment: datetime | None) -> str | None:
    return moment.date().isoformat() if moment else None


def _pool_row(item: PoolGame) -> dict:
    game, facts = item.game, item.facts
    patch = facts.version.latest_patch_at
    return {
        "appid": game.appid,
        "name": game.name,
        "owned": game.owned,
        "hours": (
            round(game.playtime_minutes / 60, 1)
            if game.owned and game.playtime_minutes is not None
            else None
        ),
        "source": SOURCE_LABELS[item.source],
        "price": facts.price_text,
        "early_access": facts.early_access,
        "reviews": facts.review_text,
        "release_date": facts.release_date,
        "description": facts.short_description,
        "categories": facts.categories,
        "up_to_players": facts.coop_max,
        "latest_patch": (
            {
                "title": facts.news.data.get("latest_patch_title"),
                "date": _day(patch),
            }
            if patch is not None and facts.news is not None
            else None
        ),
        "facts_checked": _day(facts.store.fetched_at) if facts.store else None,
        "history": list(item.history),
    }


def pool_input(items: list[PoolGame]) -> list[dict]:
    """What the prompts show of each game: its name, whether it is owned (and the hours
    played if so), where it came from, and the facts with the dates they were checked,
    followed by the game's earlier verdicts in the lane."""
    return [_pool_row(item) for item in items]


@dataclass(frozen=True)
class LaneBrief:
    """What the steps of one lane's round are told about the lane.

    `head_counts` are the group sizes the user has played in the lane (none for Solo), and
    `gap` is how many more recommended games the lane needs. `rejected` holds this run's
    rejections and refusals for the lane, each `{"game", "reason"}`.
    """

    lane: Lane
    label: str
    profile_version_id: int
    lines: list[ProfileLineRef]
    head_counts: list[int]
    gap: int
    rejected: list[dict]
    today: str  # an ISO date


def _brief_context(brief: LaneBrief) -> dict:
    return {
        "lane": brief.label,
        "solo": Lane(brief.lane) is Lane.SOLO,
        "head_counts": brief.head_counts,
        "lines": [
            {
                "id": line.id,
                "section": line.section,
                "text": line.text,
                "tag": TAG_LABELS.get(line.tag, line.tag),
            }
            for line in brief.lines
        ],
        "gap": brief.gap,
        "rejected": brief.rejected,
        "today": brief.today,
    }


# --- the Recommender's picks, checked ---------------------------------------------------


@dataclass(frozen=True)
class CheckedProposal:
    """A pick that passed the checks. `fits` are the lines it cites, resolved from the
    profile to `{"section", "text", "tag", "how"}`, so a line on a pick is always a real
    line of the profile."""

    item: PoolGame
    fits: list[dict]
    risk: str


def check_proposals(
    proposals: Proposals,
    pool: dict[int, PoolGame],
    lines: list[ProfileLineRef],
    count: int,
) -> tuple[list[CheckedProposal], list[str]]:
    """Keep the proposals that are real, in the order given, up to `count`.

    A proposal counts only if its appid is in `pool`, it cites at least one line id that is
    in `lines`, and it states a risk. A fit that cites an unknown line id is dropped. Only
    the first proposal for an appid is looked at, and a later one is dropped without a note.
    Returns the kept proposals and a note for each one refused, in words fit for the run's
    log.
    """
    by_id = {line.id: line for line in lines}
    kept: list[CheckedProposal] = []
    refused: list[str] = []
    seen: set[int] = set()
    for proposal in proposals.proposals:
        if len(kept) >= count:
            break
        if proposal.appid in seen:
            continue
        seen.add(proposal.appid)
        item = pool.get(proposal.appid)
        if item is None:
            refused.append(
                f"App {proposal.appid} wasn't one of the candidates, so it was left out."
            )
            continue
        fits = [
            {
                "section": by_id[fit.line].section,
                "text": by_id[fit.line].text,
                "tag": by_id[fit.line].tag,
                "how": fit.how.strip(),
            }
            for fit in proposal.fits
            if fit.line in by_id
        ]
        if not fits:
            refused.append(
                f"{item.game.name} was left out: it didn't name a line of the profile it fits."
            )
            continue
        risk = proposal.risk.strip()
        if not risk:
            refused.append(
                f"{item.game.name} was left out: it didn't say its main risk."
            )
            continue
        kept.append(CheckedProposal(item, fits, risk))
    return kept, refused


# --- the judge's verdicts, checked ------------------------------------------------------


@dataclass(frozen=True)
class Ruling:
    """What became of one proposal. `verdict` is `accepted`, `rejected` or `no_verdict`, and
    `list_rejected` is true when the judge rejected the whole list at once."""

    proposal: CheckedProposal
    verdict: str
    reason: str
    list_rejected: bool


def apply_verdicts(
    verdicts: Verdicts, proposals: list[CheckedProposal]
) -> list[Ruling]:
    """One ruling for each proposal, in order.

    A `reject_all` that isn't blank rejects every proposal with that reason. Otherwise the
    first verdict for a proposal's appid counts, and one without a reason counts as no
    verdict, as does the lack of one. A verdict for an appid that wasn't proposed is
    ignored, so the judge can't add a game.
    """
    reject_all = (verdicts.reject_all or "").strip()
    if reject_all:
        return [Ruling(p, REJECTED, reject_all, True) for p in proposals]
    first: dict[int, Verdict] = {}
    for verdict in verdicts.verdicts:
        first.setdefault(verdict.appid, verdict)
    rulings = []
    for proposal in proposals:
        verdict = first.get(proposal.item.game.appid)
        reason = verdict.reason.strip() if verdict else ""
        if verdict is None or not reason:
            rulings.append(Ruling(proposal, NO_VERDICT, NO_VERDICT_REASON, False))
        else:
            rulings.append(
                Ruling(
                    proposal, ACCEPTED if verdict.accept else REJECTED, reason, False
                )
            )
    return rulings


# --- the four calls ---------------------------------------------------------------------


def plan_research(
    db: Session,
    vault: Vault,
    brief: LaneBrief,
    backlog: list[dict],
    known: list[str],
    *,
    services: "Services",
    run_ref: str | None,
    now: datetime,
) -> tuple[ResearchPlan, int]:
    """The Recommender picks owned backlog games worth a look and writes web searches.

    `backlog` is the owned games it may pick from, each `{"appid", "name", "hours"}`, and
    `known` the names of games the round already has, which it is told not to look for
    again. Returns the plan and the id of the logged AI call.
    """
    return complete_json(
        db,
        vault,
        Job.RECOMMENDER,
        load_prompt("research_plan"),
        {**_brief_context(brief), "backlog": backlog, "known": known},
        ResearchPlan,
        services=services,
        run_ref=run_ref,
        now=now,
    )


def extract_games(
    db: Session,
    vault: Vault,
    brief: LaneBrief,
    results: list[SearchResult],
    known: list[str],
    *,
    services: "Services",
    run_ref: str | None,
    now: datetime,
) -> tuple[FoundGames, int]:
    """The Recommender lists the PC games on Steam that search `results` recommend, leaving
    out the `known` ones. Each result's text is cut to `RESULT_CHARS`."""
    return complete_json(
        db,
        vault,
        Job.RECOMMENDER,
        load_prompt("research_extract"),
        {
            **_brief_context(brief),
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "content": result.content[:RESULT_CHARS],
                }
                for result in results
            ],
            "known": known,
        },
        FoundGames,
        services=services,
        run_ref=run_ref,
        now=now,
    )


def propose(
    db: Session,
    vault: Vault,
    brief: LaneBrief,
    pool: list[PoolGame],
    count: int,
    *,
    services: "Services",
    run_ref: str | None,
    now: datetime,
) -> tuple[Proposals, int]:
    """The Recommender picks up to `count` games from `pool` for the lane, best first, each
    with the profile lines it fits and its main risk. Read the picks with
    `check_proposals`."""
    return complete_json(
        db,
        vault,
        Job.RECOMMENDER,
        load_prompt("research_propose"),
        {**_brief_context(brief), "pool": pool_input(pool), "count": count},
        Proposals,
        services=services,
        run_ref=run_ref,
        now=now,
    )


def judge(
    db: Session,
    vault: Vault,
    brief: LaneBrief,
    reviews: list[dict],
    proposals: list[CheckedProposal],
    *,
    services: "Services",
    run_ref: str | None,
    now: datetime,
) -> tuple[Verdicts, int]:
    """The Review judge accepts or rejects each of the checked `proposals`, or rejects the
    whole list. `reviews` are the review versions behind the profile, as `review_input`
    gives them. Read the verdicts with `apply_verdicts`."""
    shown = [
        {
            **row,
            "fits": [
                {**fit, "tag": TAG_LABELS.get(fit["tag"], fit["tag"])} for fit in p.fits
            ],
            "risk": p.risk,
        }
        for row, p in zip(
            pool_input([p.item for p in proposals]), proposals, strict=True
        )
    ]
    return complete_json(
        db,
        vault,
        Job.JUDGE,
        load_prompt("research_judge"),
        {**_brief_context(brief), "reviews": reviews, "proposals": shown},
        Verdicts,
        services=services,
        run_ref=run_ref,
        now=now,
    )
