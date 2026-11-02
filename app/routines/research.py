"""Research: the job that takes a run through its rounds (`run_research`), and one lane
through one round (`research_lane`).

A run's job works through one segment of the run: the whole run up to its pause, or the
one more round a continue allows. The first segment refreshes the profiles first and notes
the lanes the run leaves out (see `run_research`).

A round takes a lane through these steps, in order, and writes a line to the run's log as
it goes:

1. The brief: the lane's current profile, how many games it still needs, the head counts
   seen in it and what this run has already rejected for it.
2. Candidate history: earlier candidates from other runs that `may_return` lets back in.
3. The plan: the Recommender picks owned backlog games and writes web searches.
4. Web research: the searches, the games the results name, and each name's Steam app.
5. Facts: fresh Steam facts. A game Steam has no game entry for leaves the pool, and one
   Steam fails to refresh stays on its last saved facts, with a line in the log.
6. Players: a co-op lane looks up player counts, and then every lane leaves out the games
   that can't seat it.
7. The Recommender proposes from the pool, and the code checks every pick.
8. The judge rules on the checked picks.
9. The rulings are saved, with as many accepted games as the lane has room for.

Earlier candidates come back only one way. A game with a candidate in the lane from another
run enters the pool through the candidate history, when `may_return` allows it, or not at
all: the backlog and the web skip it, so a game the judge rejected can't walk back in through
a search with nothing changed. An owned game played for `BACKLOG_MAX_MINUTES` or more enters
no pool at all, since the user already knows it. The run is checked for a cancel before
every step and before saving, and a cancel saves nothing of the lane's round.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.jobs import JobContext
from app.models import (
    Candidate,
    Game,
    GroupMember,
    InventoryEntry,
    NamedGroup,
    Profile,
    ProfileVersion,
    ProfileVersionSource,
    RefreshRun,
    ResearchRun,
    Review,
    ReviewVersion,
    ReviewVersionPerson,
)
from app.routines import recommender
from app.routines.recommender import (
    CheckedProposal,
    FoundGame,
    LaneBrief,
    PoolGame,
    Ruling,
    apply_verdicts,
    check_proposals,
    number_lines,
)
from app.routines.refresh import FAILED as PROFILE_FAILED
from app.routines.refresh import local_today, run_refresh
from app.services.ai import StructuredReplyError
from app.services.ai_setup import stored_ai_setup
from app.services.game_facts import (
    FACTS_MAX_AGE,
    MAX_STEAM_FAILURES_IN_A_ROW,
    FactsRefresh,
    current_facts,
    ensure_game,
    refresh_store_facts,
)
from app.services.players import (
    LANE_HEADS,
    lane_mismatch,
    lookup_player_counts,
    needs_player_counts,
)
from app.services.profiles import (
    current_version,
    find_profile,
    game_names,
    lane_key,
    review_input,
)
from app.services.queue import (
    ACCEPTED,
    CANDIDATE_HISTORY,
    OWNED_BACKLOG,
    WEB_RESEARCH,
    JudgedProposal,
    earlier_candidates,
    excluded_appids,
    may_return,
    recommended_counts,
    rejected_in_run,
    save_judged,
)
from app.services.research_runs import (
    CANCELLED,
    CANCELLED_KEPT,
    FAILED,
    FINISHED,
    PAUSED,
    RUNNING,
    add_event,
    end_run,
    lanes_short,
    model_blocker,
    research_blockers,
    run_targets,
)
from app.services.reviews import LANE_LABELS, Lane
from app.services.search import (
    SearchClient,
    SearchError,
    SearchResult,
    search_and_log,
)
from app.services.settings import (
    DEFAULT_STORE_REGION,
    STORE_REGION_SETTING,
    get_setting,
)
from app.services.steam import SteamClient, SteamError, StoreHit

# Proposals asked for beyond the lane's gap, so a few rejections still leave it filled.
PROPOSAL_EXTRA = 3
# An owned game with less playtime than this is backlog, and the plan sees at most this many.
BACKLOG_MAX_MINUTES = 120
BACKLOG_LIST_LIMIT = 300
# Earlier candidates looked at, newest first, and how many of them may join the pool.
HISTORY_SCAN = 40
HISTORY_LIMIT = 15
# Names of games the round already has, which the Recommender isn't to look for again.
KNOWN_LIMIT = 100
# Results read from each web search.
SEARCH_RESULTS = 5
# Reviews the judge reads, newest first.
JUDGE_REVIEWS_LIMIT = 40

# The `step` of each line written to the run's log: the job's own first (the line that
# ends a run has the run's new status, see `end_run`), then the lane's.
STEP_REFRESH = "refresh"
STEP_ROUND = "round"
STEP_HISTORY = "history"
STEP_PLAN = "plan"
STEP_SEARCH = "search"
STEP_FACTS = "facts"
STEP_PLAYERS = "players"
STEP_PROPOSE = "propose"
STEP_JUDGE = "judge"
STEP_SAVED = "saved"
STEP_SKIPPED = "skipped"
STEP_PROBLEM = "problem"

REFRESHING = "Refreshing profiles first."
REFRESHED = "Profiles are up to date."
PROFILE_NOT_UPDATED = (
    "Couldn't update the {label} profile, so research uses its last version. {message}"
)
ROUND = "Round {round}."
EVERY_LANE_FILLED = "Every lane is filled."
NO_LANE_RESEARCHED = "No lane could be researched this time."
PAUSED_SHORT = "Paused after round {n}. Still short: {lanes}."
RUN_STOPPED = "Research stopped unexpectedly: {kind}. Try again."

NO_PROFILE = (
    "No profile yet for {lane}. Publish a review played at this head count first."
)
HISTORY_BACK = "{m} of {n} earlier candidates are back for another look."
PLANNING = "Asking the Recommender where to look."
SEARCHING = "Searching the web: {query}"
SEARCH_FAILED = "The web search failed ({message}). Carrying on without it."
NOT_ON_STEAM = "Couldn't find these on Steam: {names}."
DETAILS_MISSING = "Steam's details are missing for: {names}."
DETAILS_STALE = (
    "Couldn't refresh Steam's details for: {names}. Using the last ones saved."
)
DETAILS_PARTLY = (
    "Some of Steam's details didn't load for: {names}. "
    "Their picks may show no review score or patch date."
)
LEFT_OUT = "Left out {name}: {phrase}."
NO_CANDIDATES = "No candidates for {lane} this round."
PROPOSING = "The Recommender is choosing games."
JUDGING = "The judge is checking {n} games."
SAVED = "{lane}: {added} added, {rejected} rejected."
LANE_STOPPED = "{lane} stopped this round: {message}"

# Copy that counts games, as (the copy for 1, the copy for any other number); see `_counted`.
FOUND_ON_WEB = ("Found 1 game on the web.", "Found {n} games on the web.")
CHECKING_FACTS = ("Checking Steam for 1 game.", "Checking Steam for {n} games.")
LOOKING_UP_PLAYERS = (
    "Looking up player counts for 1 game.",
    "Looking up player counts for {n} games.",
)
LANE_FULL = (
    " 1 more was accepted, but the lane is full.",
    " {n} more were accepted, but the lane is full.",
)

# The `StructuredReplyError` reason that stops the whole run: every lane would fail alike.
NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class LaneOutcome:
    """What a lane's round did: the games added to the inventory, the proposals the judge
    didn't accept, and whether a cancel ended the round before anything was saved."""

    added: int
    rejected: int
    cancelled: bool


# --- the helpers ----------------------------------------------------------------------


def head_counts_seen(db: Session, lane: Lane) -> list[int]:
    """The group sizes the user has played the lane with, counting themselves: sorted,
    each once, and none for Solo.

    Each published version in the lane counts 1 plus the people tagged on it, raised to
    the lane's lowest head count, since people who weren't tagged may have played. Each
    named group whose size (1 plus its members) the lane seats counts too.
    """
    lane = Lane(lane)
    if lane is Lane.SOLO:
        return []
    low, high = LANE_HEADS[lane]
    tagged = db.scalars(
        select(func.count(ReviewVersionPerson.person_id))
        .select_from(ReviewVersion)
        .join(Review, Review.id == ReviewVersion.review_id)
        .outerjoin(
            ReviewVersionPerson, ReviewVersionPerson.version_id == ReviewVersion.id
        )
        .where(Review.lane == lane.value)
        .group_by(ReviewVersion.id)
    )
    seen = {max(1 + people, low) for people in tagged}
    members = db.scalars(
        select(func.count(GroupMember.person_id))
        .select_from(NamedGroup)
        .outerjoin(GroupMember, GroupMember.group_id == NamedGroup.id)
        .group_by(NamedGroup.id)
    )
    for count in members:
        heads = 1 + count
        if heads >= low and (high is None or heads <= high):
            seen.add(heads)
    return sorted(seen)


_MARKS = str.maketrans("", "", "™®©")
_NOT_A_LETTER_OR_DIGIT = re.compile(r"[\W_]+")


def normalize_name(name: str) -> str:
    """A game's name for comparing: case folded, without ™ ® ©, each run of characters
    that aren't letters or digits made one space, and stripped."""
    folded = name.translate(_MARKS).casefold()
    return _NOT_A_LETTER_OR_DIGIT.sub(" ", folded).strip()


def resolve_name(steam: SteamClient, name: str, region: str) -> StoreHit | None:
    """The first store-search hit whose name is `name` once both are normalized, or None.
    A near match is never taken. A `SteamError` is left to the caller."""
    wanted = normalize_name(name)
    if not wanted:
        return None
    for hit in steam.store_search(name, region):
        if normalize_name(hit.name) == wanted:
            return hit
    return None


def _counted(copy: tuple[str, str], n: int) -> str:
    """The singular copy when `n` is 1, else the plural copy with `n` filled in."""
    one, many = copy
    return one if n == 1 else many.format(n=n)


def _distinct(results: list[SearchResult]) -> list[SearchResult]:
    """The results in order, with each URL once (results without one are all kept)."""
    seen: set[str] = set()
    kept = []
    for result in results:
        if result.url and result.url in seen:
            continue
        seen.add(result.url)
        kept.append(result)
    return kept


# --- the run's job ----------------------------------------------------------------------


def run_research(ctx: JobContext, job_id: int) -> None:
    """The handler for research jobs: take the running run through its next segment.

    1. It finds the run that is running, if there is one. One already asked to cancel is
       cancelled before anything else.
    2. In the run's first segment, it refreshes the profiles as this job's own refresh,
       and logs each profile the refresh couldn't update, which research then uses as it
       was. Then it notes each lane the run leaves out, in lane order: a full lane (its
       note from `plan_targets`) and one with no profile yet. That lane keeps its target
       with the note, so it is never short, and never researched or reported again.
    3. A model or the Tavily key not set up fails the run with the first blocker.
    4. Rounds, while the run has rounds left and lanes short: each short lane, in lane
       order, through `research_lane`.
    5. The end: finished when no lane is short, else paused until the user picks one more
       round, stop or cancel.

    Every ending goes through `end_run`. A cancel that a lane finds ends the run as
    cancelled, keeping what the judge accepted before it, and so does one found at the end.
    A model found not set up mid-run fails the run with that model's blocker line (see
    `research_blockers`), and the job is done. Any other exception fails
    the run with its type only, since the message might hold a secret, and is raised again
    so that the job fails too.
    """
    with ctx.session_factory() as db:
        run = db.scalar(
            select(ResearchRun)
            .where(ResearchRun.status == RUNNING)
            .order_by(ResearchRun.id.desc())
            .limit(1)
        )
        if run is None:
            return
        run_id, cancel_requested = run.id, run.cancel_requested
    if cancel_requested:
        _end_run(ctx, run_id, CANCELLED, CANCELLED_KEPT)
        return
    try:
        status, message = _run_segment(ctx, run_id, job_id)
    except Exception as exc:
        if isinstance(exc, StructuredReplyError) and exc.reason == NOT_CONFIGURED:
            _end_run(ctx, run_id, FAILED, _unset_model_message(ctx, exc))
            return
        _end_run(ctx, run_id, FAILED, RUN_STOPPED.format(kind=type(exc).__name__))
        raise
    _end_run(ctx, run_id, status, message)


def _run_segment(ctx: JobContext, run_id: int, job_id: int) -> tuple[str, str]:
    """Steps 2 to 5 of `run_research`. Returns the status and the log line to end the run
    with."""
    with ctx.session_factory() as db:
        first = db.get(ResearchRun, run_id).refresh_run_id is None
    if first:
        _first_segment(ctx, run_id, job_id)
    with ctx.session_factory() as db:
        blockers = research_blockers(db, ctx.vault)
    if blockers:
        return FAILED, blockers[0]
    while True:
        with ctx.session_factory() as db:
            run = db.get(ResearchRun, run_id)
            short = lanes_short(db, run)
            if run.rounds_done >= run.rounds_allowed or not short:
                break
            number = run.rounds_done + 1
            add_event(
                db,
                run_id,
                STEP_ROUND,
                ROUND.format(round=number),
                ctx.services.clock(),
                round=number,
            )
        for lane in short:
            if research_lane(ctx, run_id, number, lane).cancelled:
                return CANCELLED, CANCELLED_KEPT
        with ctx.session_factory() as db:
            db.get(ResearchRun, run_id).rounds_done += 1
            db.commit()
    with ctx.session_factory() as db:
        run = db.get(ResearchRun, run_id)
        short = lanes_short(db, run)
        if short:
            lanes = ", ".join(LANE_LABELS[lane] for lane in short)
            return PAUSED, PAUSED_SHORT.format(n=run.rounds_done, lanes=lanes)
        researched = any(
            target.target is not None and target.note is None
            for target in run_targets(run)
        )
        return FINISHED, EVERY_LANE_FILLED if researched else NO_LANE_RESEARCHED


def _first_segment(ctx: JobContext, run_id: int, job_id: int) -> None:
    """Refresh the profiles, then note the lanes the run leaves out (see `run_research`)."""
    with ctx.session_factory() as db:
        add_event(db, run_id, STEP_REFRESH, REFRESHING, ctx.services.clock())
    refresh_run_id = run_refresh(ctx, job_id)
    with ctx.session_factory() as db:
        run = db.get(ResearchRun, run_id)
        targets = run_targets(run)
        for target in targets:
            if (
                target.target is not None
                and target.note is None
                and not _has_profile_version(db, target.lane)
            ):
                target.note = NO_PROFILE.format(lane=LANE_LABELS[target.lane])
        run.targets = [target.as_json() for target in targets]
        run.refresh_run_id = refresh_run_id
        failed = [
            detail
            for detail in db.get(RefreshRun, refresh_run_id).details
            if detail["outcome"] == PROFILE_FAILED
        ]
        now = ctx.services.clock()
        # The first line's commit saves the notes and the refresh with it, so a later
        # segment neither refreshes again nor researches a lane left out.
        for detail in failed:
            add_event(
                db,
                run_id,
                STEP_PROBLEM,
                PROFILE_NOT_UPDATED.format(
                    label=detail["label"], message=detail["message"]
                ),
                now,
            )
        if not failed:
            add_event(db, run_id, STEP_REFRESH, REFRESHED, now)
        for target in targets:
            if target.note is not None:
                add_event(db, run_id, STEP_SKIPPED, target.note, now, lane=target.lane)


def _unset_model_message(ctx: JobContext, err: StructuredReplyError) -> str:
    """What a run says when a model turns out not to be set up mid-run: the same line as
    the blocker for the job whose call found it. An error that doesn't name its job gets
    the first blocker found now, or its own message if setup has been fixed since."""
    if err.job is not None:
        return model_blocker(err.job)
    with ctx.session_factory() as db:
        blockers = research_blockers(db, ctx.vault)
    return blockers[0] if blockers else err.message


def _has_profile_version(db: Session, lane: Lane) -> bool:
    profile = find_profile(db, lane_key(lane))
    return profile is not None and current_version(db, profile) is not None


def _end_run(ctx: JobContext, run_id: int, status: str, message: str) -> None:
    """End the run through `end_run`. A pause or a finish that `end_run` refuses, because
    the run was asked to cancel meanwhile, becomes the cancel."""
    with ctx.session_factory() as db:
        now = ctx.services.clock()
        ended = end_run(db, run_id, status, message, now)
        if not ended and status in (PAUSED, FINISHED):
            end_run(db, run_id, CANCELLED, CANCELLED_KEPT, now)


# --- one lane, one round ----------------------------------------------------------------


def research_lane(ctx: JobContext, run_id: int, round: int, lane: Lane) -> LaneOutcome:
    """Run one lane through one round of research (see the module docstring).

    Every AI call and web search carries `run_ref=f"research:{run_id}"`. A cancel found
    before a step or before saving ends the round with nothing saved and
    `LaneOutcome(0, 0, True)`. A `StructuredReplyError` from the plan, the extraction, the
    proposals or the judge ends the lane for this round with a line in the log, unless
    its reason is `not_configured`: then it is raised, because the whole run must stop.
    """
    lane = Lane(lane)
    # One session for the lane's reads and writes. It commits often (each log line, each
    # fact, each AI call) and edits none of what it reads, so its objects are kept loaded
    # across commits rather than read again one by one.
    with ctx.session_factory(expire_on_commit=False) as db:
        work = _LaneRound(ctx, db, run_id, round, lane)
        try:
            return work.run()
        except _Cancelled:
            return LaneOutcome(0, 0, True)
        except StructuredReplyError as err:
            if err.reason == NOT_CONFIGURED:
                raise
            work.event(
                STEP_PROBLEM, LANE_STOPPED.format(lane=work.label, message=err.message)
            )
            return LaneOutcome(0, 0, False)


class _Cancelled(Exception):
    """The run was asked to cancel, so the lane stops with nothing saved."""


class _LaneRound:
    """One lane's round, step by step. `pool` holds the games the Recommender may propose,
    with their facts, and `found` the backlog and web games still waiting for theirs.
    `skip` is every game the backlog and the web leave out (see the module docstring)."""

    # Set by `make_brief`.
    profile: Profile
    version: ProfileVersion
    brief: LaneBrief
    gap: int
    rejected_ids: set[int]
    search: SearchClient
    # Set by `plan`.
    known: list[str]

    def __init__(
        self, ctx: JobContext, db: Session, run_id: int, round: int, lane: Lane
    ) -> None:
        self.ctx = ctx
        self.db = db
        self.run_id = run_id
        self.round = round
        self.lane = lane
        self.label = LANE_LABELS[lane]
        self.run_ref = f"research:{run_id}"
        self.steam = SteamClient(ctx.services.steam_http)
        self.region = get_setting(db, STORE_REGION_SETTING, DEFAULT_STORE_REGION)
        self.pool: dict[int, PoolGame] = {}
        self.found: dict[int, tuple[Game, str]] = {}
        self.skip: set[int] = set()

    def run(self) -> LaneOutcome:
        self.check_cancel()
        if not self.make_brief():
            self.event(STEP_SKIPPED, NO_PROFILE.format(lane=self.label))
            return LaneOutcome(0, 0, False)
        self.check_cancel()
        self.gather_history()
        self.check_cancel()
        queries = self.plan()
        self.check_cancel()
        self.search_web(queries)
        self.check_cancel()
        self.check_facts()
        self.check_cancel()
        self.check_players()
        self.check_cancel()
        if not self.pool:
            self.event(STEP_SKIPPED, NO_CANDIDATES.format(lane=self.label))
            return LaneOutcome(0, 0, False)
        proposals, recommender_call = self.propose()
        rulings: list[Ruling] = []
        judge_call = None
        if proposals:
            self.check_cancel()
            rulings, judge_call = self.judge(proposals)
        self.check_cancel()
        return self.save(rulings, recommender_call, judge_call)

    # --- plumbing ---------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self.ctx.services.clock()

    def event(self, step: str, message: str) -> None:
        add_event(
            self.db,
            self.run_id,
            step,
            message,
            self.now,
            round=self.round,
            lane=self.lane,
        )

    def check_cancel(self) -> None:
        """Raise `_Cancelled` if the run has been asked to cancel. A query each time, so a
        cancel saved by another session is seen."""
        if self.db.scalar(
            select(ResearchRun.cancel_requested).where(ResearchRun.id == self.run_id)
        ):
            raise _Cancelled

    def ai(self) -> dict:
        """The keyword arguments every AI call of the round takes."""
        return {"services": self.ctx.services, "run_ref": self.run_ref, "now": self.now}

    # --- 1. the brief -----------------------------------------------------------------

    def make_brief(self) -> bool:
        """Read what the steps are told about the lane. False when it has no profile yet."""
        db = self.db
        profile = find_profile(db, lane_key(self.lane))
        version = current_version(db, profile) if profile else None
        if version is None:
            return False
        self.profile, self.version = profile, version
        run = db.get(ResearchRun, self.run_id)
        target = next((t.target for t in run_targets(run) if t.lane is self.lane), None)
        if target is None:
            raise ValueError(f"The run has no target for {self.label}.")
        self.gap = target - recommended_counts(db)[self.lane]
        rejected = rejected_in_run(db, self.run_id, self.lane)
        self.rejected_ids = {candidate.appid for candidate in rejected}
        names = game_names(db, self.rejected_ids)
        self.brief = LaneBrief(
            lane=self.lane,
            label=self.label,
            profile_version_id=version.id,
            lines=number_lines(version.content),
            head_counts=head_counts_seen(db, self.lane),
            gap=self.gap,
            rejected=[
                {"game": names[candidate.appid], "reason": candidate.reason}
                for candidate in rejected
            ],
            today=local_today(db, self.now),
        )
        self.search = self.ctx.services.search_factory(
            stored_ai_setup(db, self.ctx.vault).tavily_key
        )
        return True

    # --- 2. candidate history ---------------------------------------------------------

    def gather_history(self) -> None:
        """Put the earlier candidates that may come back into the pool, and work out the
        skip set: the excluded games, the owned games played a lot, this run's rejections,
        and every other earlier candidate."""
        db = self.db
        earlier = earlier_candidates(db, self.lane, self.run_id)
        self.skip = (
            excluded_appids(db, self.lane) | self.played_a_lot() | self.rejected_ids
        )
        scan = [c for c in earlier if c.appid not in self.skip][:HISTORY_SCAN]
        if scan:
            appids = [candidate.appid for candidate in scan]
            refreshed = refresh_store_facts(
                db, self.steam, appids, self.region, self.now
            )
            facts = current_facts(db, appids)
            kept = [
                candidate
                for candidate in scan
                if may_return(
                    candidate, facts[candidate.appid].version, self.version.id
                )
            ][:HISTORY_LIMIT]
            kept_ids = [candidate.appid for candidate in kept]
            games = {
                game.appid: game
                for game in db.scalars(select(Game).where(Game.appid.in_(kept_ids)))
            }
            history = self.history_of(kept_ids)
            for appid in kept_ids:
                self.pool[appid] = PoolGame(
                    games[appid], facts[appid], CANDIDATE_HISTORY, history[appid]
                )
            self.event(STEP_HISTORY, HISTORY_BACK.format(m=len(kept), n=len(scan)))
            self.note_stale(refreshed, [self.pool[appid] for appid in kept_ids])
        self.skip |= {candidate.appid for candidate in earlier} - set(self.pool)

    def played_a_lot(self) -> set[int]:
        """The owned games played for `BACKLOG_MAX_MINUTES` or more. No pool takes them,
        reviewed in the lane or not: the user already knows them, and the profile learns
        about them from reviews, not from picks."""
        return set(
            self.db.scalars(
                select(Game.appid).where(
                    Game.owned.is_(True), Game.playtime_minutes >= BACKLOG_MAX_MINUTES
                )
            )
        )

    def history_of(self, appids: list[int]) -> dict[int, list[dict]]:
        """Each game's earlier verdicts in the lane, from the other runs, newest first."""
        history: dict[int, list[dict]] = {appid: [] for appid in appids}
        rows = self.db.scalars(
            select(Candidate)
            .where(
                Candidate.lane == self.lane.value,
                Candidate.appid.in_(appids),
                Candidate.run_id != self.run_id,
            )
            .order_by(Candidate.created_at.desc(), Candidate.id.desc())
        )
        for row in rows:
            history[row.appid].append(
                {
                    "when": row.created_at.date().isoformat(),
                    "verdict": row.verdict,
                    "reason": row.reason,
                }
            )
        return history

    # --- 3. the plan ------------------------------------------------------------------

    def plan(self) -> list[str]:
        """Ask the Recommender for backlog picks and web searches. The picks it was
        offered wait for their facts in `found`. Returns the searches."""
        self.event(STEP_PLAN, PLANNING)
        offered = self.backlog()
        self.known = self.known_names()
        plan, _call_id = recommender.plan_research(
            self.db,
            self.ctx.vault,
            self.brief,
            [
                {
                    "appid": game.appid,
                    "name": game.name,
                    "hours": round((game.playtime_minutes or 0) / 60, 1),
                }
                for game in offered.values()
            ],
            self.known,
            **self.ai(),
        )
        for appid in dict.fromkeys(plan.backlog):
            if appid in offered:  # one it wasn't offered is dropped
                self.found[appid] = (offered[appid], OWNED_BACKLOG)
        return list(dict.fromkeys(plan.queries))

    def backlog(self) -> dict[int, Game]:
        """The owned games under `BACKLOG_MAX_MINUTES` that aren't skipped or in the pool,
        fewest minutes first, then by name, at most `BACKLOG_LIST_LIMIT`."""
        minutes = func.coalesce(Game.playtime_minutes, 0)
        games = self.db.scalars(
            select(Game)
            .where(
                Game.owned.is_(True),
                minutes < BACKLOG_MAX_MINUTES,
                Game.appid.not_in(self.skip | set(self.pool)),
            )
            .order_by(minutes, func.lower(Game.name), Game.name, Game.appid)
            .limit(BACKLOG_LIST_LIMIT)
        )
        return {game.appid: game for game in games}

    def known_names(self) -> list[str]:
        """The names of the history pool's games and the lane's inventory games (newest
        first), each once, at most `KNOWN_LIMIT`."""
        inventory = self.db.scalars(
            select(Game.name)
            .join(InventoryEntry, InventoryEntry.appid == Game.appid)
            .where(InventoryEntry.lane == self.lane.value)
            .order_by(InventoryEntry.id.desc())
            .limit(KNOWN_LIMIT)
        )
        names = [item.game.name for item in self.pool.values()] + list(inventory)
        return list(dict.fromkeys(names))[:KNOWN_LIMIT]

    # --- 4. web research --------------------------------------------------------------

    def search_web(self, queries: list[str]) -> None:
        """Run the searches, have the Recommender name the games in the results, and add
        each one Steam knows by that exact name, unless it is skipped or already in hand.
        A failed search is logged and the others go on."""
        results: list[SearchResult] = []
        for query in queries:
            self.event(STEP_SEARCH, SEARCHING.format(query=query))
            try:
                results += search_and_log(
                    self.db,
                    self.search,
                    query,
                    max_results=SEARCH_RESULTS,
                    run_ref=self.run_ref,
                    now=self.now,
                )
            except SearchError as err:
                self.event(STEP_PROBLEM, SEARCH_FAILED.format(message=err))
        if not results:
            return
        found, _call_id = recommender.extract_games(
            self.db,
            self.ctx.vault,
            self.brief,
            _distinct(results),
            self.known,
            **self.ai(),
        )
        added, missing = self.add_web_games(found.games)
        self.event(STEP_SEARCH, _counted(FOUND_ON_WEB, added))
        if missing:
            self.event(STEP_PROBLEM, NOT_ON_STEAM.format(names=", ".join(missing)))

    def add_web_games(self, games: list[FoundGame]) -> tuple[int, list[str]]:
        """Look each name up in the store and put the new games in `found`. Returns how
        many were added and the names with no exact match.

        A `SteamError` counts as no match. After `MAX_STEAM_FAILURES_IN_A_ROW` of them in a
        row the store isn't asked again, so an outage costs a few calls and not one per
        name.
        """
        added, missing = 0, []
        looked_up: set[str] = set()
        failures = 0
        for game in games:
            key = normalize_name(game.name)
            if key in looked_up:
                continue
            looked_up.add(key)
            hit = None
            if failures < MAX_STEAM_FAILURES_IN_A_ROW:
                try:
                    hit = resolve_name(self.steam, game.name, self.region)
                    failures = 0
                except SteamError:
                    failures += 1
            if hit is None:
                missing.append(game.name)
                continue
            if (
                hit.appid in self.skip
                or hit.appid in self.pool
                or hit.appid in self.found
            ):
                continue
            self.found[hit.appid] = (
                ensure_game(self.db, hit.appid, hit.name, self.now),
                WEB_RESEARCH,
            )
            added += 1
        return added, missing

    # --- 5. facts ---------------------------------------------------------------------

    def check_facts(self) -> None:
        """Fetch the facts the backlog and web games are missing, move them into the pool,
        and leave out every game Steam has no game entry for. A game whose refresh fails
        stays on its last saved facts if they are a game's."""
        db = self.db
        refreshed = FactsRefresh()
        if self.found:
            self.event(STEP_FACTS, _counted(CHECKING_FACTS, len(self.found)))
            refreshed = refresh_store_facts(
                db, self.steam, list(self.found), self.region, self.now
            )
        facts = current_facts(db, [*self.pool, *self.found])
        for appid, item in self.pool.items():
            item.facts = facts[appid]
        for appid, (game, source) in self.found.items():
            self.pool[appid] = PoolGame(game, facts[appid], source)
        self.found = {}
        missing = [item for item in self.pool.values() if not item.facts.is_game]
        for item in missing:
            del self.pool[item.game.appid]
        self.note_stale(refreshed, self.pool.values())
        if missing:
            names = ", ".join(item.game.name for item in missing)
            self.event(STEP_PROBLEM, DETAILS_MISSING.format(names=names))

    def note_stale(self, refreshed: FactsRefresh, items: Iterable[PoolGame]) -> None:
        """Log the games among `items` that Steam failed to refresh but whose last saved
        facts keep them in the pool: in one line those whose store details are still old
        or missing, and in another those whose store details are fresh but whose review
        summary or news didn't load. A game that drops out has its own line."""
        stale, partly = [], []
        for item in items:
            if item.game.appid not in refreshed.problems or not item.facts.is_game:
                continue
            store = item.facts.store
            if store is None or self.now - store.fetched_at > FACTS_MAX_AGE:
                stale.append(item.game.name)
            else:
                partly.append(item.game.name)
        if stale:
            self.event(STEP_PROBLEM, DETAILS_STALE.format(names=", ".join(stale)))
        if partly:
            self.event(STEP_PROBLEM, DETAILS_PARTLY.format(names=", ".join(partly)))

    # --- 6. players -------------------------------------------------------------------

    def check_players(self) -> None:
        """In a co-op lane, look up the player counts that are missing or old. Then, in
        every lane, leave out each game that can't seat it."""
        if self.lane is not Lane.SOLO:
            self.look_up_players()
        for item in list(self.pool.values()):
            phrase = lane_mismatch(item.facts, self.lane)
            if phrase is not None:
                del self.pool[item.game.appid]
                self.event(
                    STEP_PLAYERS, LEFT_OUT.format(name=item.game.name, phrase=phrase)
                )

    def look_up_players(self) -> None:
        """A failed search or an unusable reply leaves that game without a count for this
        round. A model that isn't set up stops the run."""
        wanted = [
            item
            for item in self.pool.values()
            if needs_player_counts(item.facts, self.now)
        ]
        if not wanted:
            return
        self.event(STEP_PLAYERS, _counted(LOOKING_UP_PLAYERS, len(wanted)))
        for item in wanted:
            self.check_cancel()
            try:
                lookup_player_counts(
                    self.db, self.ctx.vault, self.search, item.game, **self.ai()
                )
            except SearchError:
                pass  # this game has no count this round
            except StructuredReplyError as err:
                if err.reason == NOT_CONFIGURED:
                    raise  # every lane would fail the same way, so the run stops
        facts = current_facts(self.db, [item.game.appid for item in wanted])
        for item in wanted:
            item.facts = facts[item.game.appid]

    # --- 7. propose -------------------------------------------------------------------

    def propose(self) -> tuple[list[CheckedProposal], int]:
        """Ask the Recommender for picks from the pool and keep the ones that pass the
        checks, logging each refusal. Returns the kept picks and the call's id."""
        self.event(STEP_PROPOSE, PROPOSING)
        wanted = self.gap + PROPOSAL_EXTRA
        count = min(wanted, len(self.pool))
        proposals, call_id = recommender.propose(
            self.db,
            self.ctx.vault,
            self.brief,
            list(self.pool.values()),
            count,
            **self.ai(),
        )
        # Read up to `wanted` picks, not `count`. The picks kept are different pool games,
        # so there are never more of them than `count` anyway, and a pick after the pool's
        # last game (one the model made up) still gets its refusal in the log.
        kept, refused = check_proposals(proposals, self.pool, self.brief.lines, wanted)
        for note in refused:
            self.event(STEP_PROBLEM, note)
        return kept, call_id

    # --- 8. judge ---------------------------------------------------------------------

    def judge(self, proposals: list[CheckedProposal]) -> tuple[list[Ruling], int]:
        """Have the judge rule on the checked picks, with the reviews behind the profile."""
        self.event(STEP_JUDGE, JUDGING.format(n=len(proposals)))
        verdicts, call_id = recommender.judge(
            self.db,
            self.ctx.vault,
            self.brief,
            review_input(self.db, self.judge_reviews()),
            proposals,
            **self.ai(),
        )
        return apply_verdicts(verdicts, proposals), call_id

    def judge_reviews(self) -> list[ReviewVersion]:
        """The latest version the profile learned from of each review behind it, newest
        first, at most `JUDGE_REVIEWS_LIMIT`."""
        learned = (
            select(ProfileVersionSource.review_version_id)
            .join(
                ProfileVersion,
                ProfileVersion.id == ProfileVersionSource.profile_version_id,
            )
            .where(ProfileVersion.profile_id == self.profile.id)
        )
        newest = (
            select(
                ReviewVersion.id,
                func.row_number()
                .over(
                    partition_by=ReviewVersion.review_id,
                    order_by=ReviewVersion.version.desc(),
                )
                .label("newest"),
            )
            .where(ReviewVersion.id.in_(learned))
            .subquery()
        )
        return list(
            self.db.scalars(
                select(ReviewVersion)
                .join(newest, newest.c.id == ReviewVersion.id)
                .where(newest.c.newest == 1)
                .order_by(ReviewVersion.published_at.desc(), ReviewVersion.id.desc())
                .limit(JUDGE_REVIEWS_LIMIT)
            )
        )

    # --- 9. save ----------------------------------------------------------------------

    def save(
        self, rulings: list[Ruling], recommender_call: int, judge_call: int | None
    ) -> LaneOutcome:
        """Save every ruling as a candidate, adding accepted games while the lane has room,
        and log what came of the round."""
        saved = save_judged(
            self.db,
            [
                JudgedProposal(
                    appid=ruling.proposal.item.game.appid,
                    source=ruling.proposal.item.source,
                    fits=ruling.proposal.fits,
                    risk=ruling.proposal.risk,
                    version=ruling.proposal.item.facts.version,
                    facts=ruling.proposal.item.facts.snapshot(),
                    verdict=ruling.verdict,
                    reason=ruling.reason,
                    list_rejected=ruling.list_rejected,
                    recommender_call_id=recommender_call,
                    judge_call_id=judge_call,
                )
                for ruling in rulings
            ],
            run_id=self.run_id,
            round=self.round,
            lane=self.lane,
            room=self.gap,
            profile_version_id=self.version.id,
            now=self.now,
        )
        added = sum(1 for candidate in saved if candidate.added)
        rejected = sum(1 for candidate in saved if candidate.verdict != ACCEPTED)
        message = SAVED.format(lane=self.label, added=added, rejected=rejected)
        if surplus := len(saved) - added - rejected:
            message += _counted(LANE_FULL, surplus)
        self.event(STEP_SAVED, message)
        return LaneOutcome(added, rejected, False)
