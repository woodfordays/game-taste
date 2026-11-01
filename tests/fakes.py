"""Stand-ins for the AI providers, Tavily and Steam, so no test touches the network."""

import copy
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AiCall,
    GameFact,
    ProfileVersion,
    ResearchEvent,
    ResearchRun,
    Review,
    ReviewVersion,
)
from app.services.ai import Completion, ProviderConfig, ProviderKind
from app.services.game_facts import (
    NEWS,
    PLAYERS,
    REVIEWS,
    STORE,
    Fact,
    GameFacts,
    GameVersion,
    ensure_game,
    store_fact_data,
)
from app.services.profiles import (
    ProfileTarget,
    ProfileUpdate,
    ensure_profile,
    lane_key,
    save_version,
)
from app.services.queue import JudgedProposal, save_judged
from app.services.reviews import LANE_LABELS, Lane, ReviewForm, publish
from app.services.steam import parse_app_details

# The moment every test's clock stands at, and two patch dates before it.
FIXED_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
T1 = FIXED_NOW - timedelta(days=10)
T2 = FIXED_NOW - timedelta(days=1)


class _FakeAdapter:
    def __init__(self, factory: "FakeAdapterFactory", cfg: ProviderConfig) -> None:
        self._factory = factory
        self._cfg = cfg

    def complete(
        self,
        model: str,
        prompt: str,
        *,
        timeout: float | None = None,
        retries: bool = True,
    ) -> Completion:
        self._factory.calls.append((self._cfg.kind, model, prompt))
        self._factory.timeouts.append(timeout)
        self._factory.retries.append(retries)
        failure = self._factory.failures.get(model)
        if failure is not None:
            raise failure
        text = self._factory.reply_for(model, prompt)
        return Completion(
            text=text,
            input_tokens=5,
            output_tokens=2,
            stop_reason=self._factory.stop_reasons.get(model),
        )


class FakeAdapterFactory:
    """Stands in for `make_adapter`.

    `.calls` holds one (kind, model, prompt) per `complete()`, and `.timeouts` and
    `.retries` the timeout and the retries setting each of those calls was given.
    Every reply is "ok" unless `.replies[model]` says otherwise, and a model passed to
    `.fail()` raises.
    `.replies[model]` is a str, a list of str (used in order, the last one repeating; the
    place in the list is counted per model over the factory's life), or a function that
    takes the prompt and returns the reply. `.stop_reasons[model]` is the `stop_reason`
    of that model's replies ("stop", "length" or None, the default).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[ProviderKind, str, str]] = []
        self.timeouts: list[float | None] = []
        self.retries: list[bool] = []
        self.replies: dict[str, str | list[str] | Callable[[str], str]] = {}
        self.stop_reasons: dict[str, str | None] = {}
        self.failures: dict[str, Exception] = {}
        self._replies_used: dict[str, int] = {}

    def reply_for(self, model: str, prompt: str) -> str:
        reply = self.replies.get(model, "ok")
        if callable(reply):
            return reply(prompt)
        if isinstance(reply, list):
            used = self._replies_used.get(model, 0)
            self._replies_used[model] = used + 1
            return reply[min(used, len(reply) - 1)]
        return reply

    def __call__(self, cfg: ProviderConfig) -> _FakeAdapter:
        return _FakeAdapter(self, cfg)

    def fail(self, model: str, exc: Exception) -> None:
        self.failures[model] = exc


def by_prompt(
    default: str | Callable[[str], str] = "ok",
    **replies: str | Callable[[str], str],
) -> Callable[[str], str]:
    """A reply function for `FakeAdapterFactory.replies` that picks the reply by the
    prompt's first line, `# <name>`.

    Each keyword is a prompt name and its reply, a str or a function of the prompt.
    Any other prompt gets `default`, which is the same kind of value.
    """

    def reply(prompt: str) -> str:
        first_line = prompt.partition("\n")[0].strip()
        name = first_line.removeprefix("# ") if first_line.startswith("# ") else None
        chosen = replies.get(name, default) if name is not None else default
        return chosen(prompt) if callable(chosen) else chosen

    return reply


class _FakeSearchClient:
    def __init__(self, factory: "FakeSearchFactory") -> None:
        self._factory = factory

    def search(self, query: str, max_results: int) -> dict:
        self._factory.calls.append((query, max_results))
        if self._factory.failure is not None:
            raise self._factory.failure
        if self._factory.responder is not None:
            return self._factory.responder(query)
        return copy.deepcopy(self._factory.payload)


class FakeSearchFactory:
    """Stands in for `make_search_client`.

    `.payload` is shaped like Tavily's reply. `.calls` holds one (query, max_results)
    per `search()`, and `.fail()` makes every search raise. When `.responder` is set, a
    search returns `responder(query)` instead of `.payload`.
    """

    def __init__(self) -> None:
        self.responder: Callable[[str], dict] | None = None
        self.payload: dict = {
            "query": "co-op games",
            "results": [
                {
                    "title": "Sample Quest review",
                    "url": "https://example.com/sample-quest",
                    "content": "A cozy co-op adventure for two.",
                    "score": 0.9,
                },
                {
                    "title": "Test Tactics guide",
                    "url": "https://example.com/test-tactics",
                    "content": "Turn-based tactics for a small group.",
                    "score": 0.7,
                },
            ],
        }
        self.calls: list[tuple[str, int]] = []
        self.failure: Exception | None = None

    def __call__(self, api_key: str) -> _FakeSearchClient:
        return _FakeSearchClient(self)

    def fail(self, exc: Exception) -> None:
        self.failure = exc


class FakeSteam:
    """Stands in for Steam. Its `.handler` is an `httpx.MockTransport` handler that
    routes by URL path, so nothing leaves the process.

    `.owned` is the `games` list GetOwnedGames returns (empty means a private
    profile, which Steam answers with an empty `response`). `.vanity` maps a vanity
    name to a SteamID64, and `.store_hits` is the store search `items` list.
    `.details` maps an appid to the `data` object of its store entry (see
    `store_data`); None, or an appid left out, is an entry the store doesn't have.
    `.review_summaries` maps an appid to its review `query_summary`, and `.news` an
    appid to its `newsitems`. An appid left out of those gets 100 mostly good
    reviews, and no news.
    `.requests` holds every request the client made. `.fail_with(401)` answers every
    request with that status, `.fail_with(exc)` raises `exc` from every request, and
    `.fail_with(None)` clears it.
    """

    def __init__(self) -> None:
        self.owned: list[dict] = [
            {
                "appid": 10,
                "name": "Sample Quest",
                "playtime_forever": 600,
                "rtime_last_played": 1768000000,
            },
            {
                "appid": 20,
                "name": "Test Tactics",
                "playtime_forever": 120,
                "rtime_last_played": 0,
            },
        ]
        self.vanity: dict[str, str] = {}
        self.store_hits: list[dict] = []
        self.details: dict[int, dict | None] = {}
        self.review_summaries: dict[int, dict] = {}
        self.news: dict[int, list[dict]] = {}
        self.requests: list[httpx.Request] = []
        self._failure: int | Exception | None = None

    def fail_with(self, failure: int | Exception | None) -> None:
        self._failure = failure

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self._failure, Exception):
            raise self._failure
        if isinstance(self._failure, int):
            return httpx.Response(self._failure)
        path = request.url.path
        if path == "/ISteamUser/ResolveVanityURL/v1/":
            steamid = self.vanity.get(request.url.params.get("vanityurl", ""))
            if steamid is None:
                return httpx.Response(
                    200, json={"response": {"success": 42, "message": "No match"}}
                )
            return httpx.Response(
                200, json={"response": {"success": 1, "steamid": steamid}}
            )
        if path == "/IPlayerService/GetOwnedGames/v1/":
            if not self.owned:
                return httpx.Response(200, json={"response": {}})
            return httpx.Response(
                200,
                json={"response": {"game_count": len(self.owned), "games": self.owned}},
            )
        if path == "/api/storesearch/":
            return httpx.Response(
                200, json={"total": len(self.store_hits), "items": self.store_hits}
            )
        if path == "/api/appdetails":
            appid = int(request.url.params["appids"])
            data = self.details.get(appid)
            entry = (
                {"success": False} if data is None else {"success": True, "data": data}
            )
            return httpx.Response(200, json={str(appid): entry})
        if path.startswith("/appreviews/"):
            appid = int(path.removeprefix("/appreviews/"))
            summary = self.review_summaries.get(appid, DEFAULT_REVIEW_SUMMARY)
            return httpx.Response(200, json={"success": 1, "query_summary": summary})
        if path == "/ISteamNews/GetNewsForApp/v2/":
            appid = int(request.url.params["appid"])
            items = self.news.get(appid, [])
            return httpx.Response(
                200,
                json={
                    "appnews": {"appid": appid, "newsitems": items, "count": len(items)}
                },
            )
        return httpx.Response(404)


DEFAULT_REVIEW_SUMMARY = {
    "review_score_desc": "Very Positive",
    "total_positive": 90,
    "total_negative": 10,
    "total_reviews": 100,
}


def store_data(
    name: str,
    *,
    categories: tuple[str, ...] = ("Single-player",),
    early_access: bool = False,
    is_free: bool = False,
    price_cents: int | None = 1999,
    type: str = "game",
    coming_soon: bool = False,
    release_date: str = "1 Jan, 2025",
) -> dict:
    """A store entry's `data` object, in the shape `api/appdetails` sends.

    There is no `price_overview` when the game is free or `price_cents` is None, as for
    a game that isn't out yet. Early Access is a genre, as Steam has it.
    """
    genres = [{"id": "1", "description": "Action"}]
    if early_access:
        genres.append({"id": "70", "description": "Early Access"})
    data = {
        "type": type,
        "name": name,
        "is_free": is_free,
        "short_description": f"About {name}.",
        "developers": ["Sample Studio"],
        "categories": [
            {"id": index, "description": description}
            for index, description in enumerate(categories, start=1)
        ],
        "genres": genres,
        "release_date": {"coming_soon": coming_soon, "date": release_date},
    }
    if not is_free and price_cents is not None:
        data["price_overview"] = {
            "currency": "USD",
            "initial": price_cents,
            "final": price_cents,
            "discount_percent": 0,
            "initial_formatted": "",
            "final_formatted": f"${price_cents / 100:.2f}",
        }
    return data


# A complete, valid "Connect your AI" form: two providers, three jobs and a Tavily key.
# Local needs no key, so `key_local` is empty. Shared by the onboarding and Settings tests.
VALID = {
    "providers": ["anthropic", "local"],
    "key_anthropic": "ant-sample",
    "url_local": "http://localhost:1234/v1",
    "key_local": "",
    "job_recommender_provider": "anthropic",
    "job_recommender_model": "sample-big",
    "job_judge_provider": "local",
    "job_judge_model": "sample-local",
    "job_everyday_provider": "local",
    "job_everyday_model": "sample-local",
    "tavily_key": "tvly-sample",
}


PROFILE_SECTIONS = ("loved", "dealbreakers", "patterns", "hunches", "open_questions")


def profile_reply(changelog: str = "Learned from new reviews.", **sections) -> str:
    """A model's reply in the `ProfileUpdate` shape, as JSON text.

    Each keyword is one of the five profile sections (`loved`, `dealbreakers`, `patterns`,
    `hunches`, `open_questions`) and holds its lines. A section left out is an empty list.
    """
    unknown = set(sections) - set(PROFILE_SECTIONS)
    if unknown:
        raise TypeError(f"Not profile sections: {sorted(unknown)}")
    profile = {name: sections.get(name, []) for name in PROFILE_SECTIONS}
    return json.dumps({"profile": profile, "changelog": changelog})


def facts_for(
    appid: int,
    name: str,
    *,
    players: tuple[tuple[int, int] | None, tuple[int, int] | None] | None = None,
    fetched_at: datetime = FIXED_NOW,
    **store_kwargs,
) -> GameFacts:
    """Game facts built in memory, as `current_facts` would give them.

    The store fact is `store_data(name, **store_kwargs)` read the way the app reads it
    (region "us"), the reviews are "Very Positive" over 100, and the news has no patch.
    `players` is the (min, max) of online and local co-op, each None for no count, and
    adds a players fact from "https://example.com/players".
    """
    details = parse_app_details(
        appid, {str(appid): {"success": True, "data": store_data(name, **store_kwargs)}}
    )

    def fact(data: dict, source_url: str | None = None) -> Fact:
        return Fact(data=data, fetched_at=fetched_at, source_url=source_url)

    def counts(pair: tuple[int, int] | None) -> dict | None:
        return None if pair is None else {"min": pair[0], "max": pair[1]}

    return GameFacts(
        appid=appid,
        store=fact(store_fact_data(details, "us")),
        reviews=fact(
            {
                "score_desc": "Very Positive",
                "total_positive": 90,
                "total_negative": 10,
                "total_reviews": 100,
            }
        ),
        news=fact({"latest_patch_at": None, "latest_patch_title": None}),
        players=(
            None
            if players is None
            else fact(
                {
                    "online_coop": counts(players[0]),
                    "local_coop": counts(players[1]),
                    "competitive": None,
                },
                "https://example.com/players",
            )
        ),
    )


def save_facts(db: Session, appid: int, name: str, **same) -> GameFacts:
    """Write the snapshots `facts_for(appid, name, **same)` builds to `game_fact`, adding
    the game first (through `ensure_game`) when there is no row for it. Returns the facts."""
    facts = facts_for(appid, name, **same)
    ensure_game(db, appid, name, facts.store.fetched_at)
    for kind in (STORE, REVIEWS, NEWS, PLAYERS):
        fact = getattr(facts, kind)
        if fact is not None:
            db.add(
                GameFact(
                    appid=appid,
                    kind=kind,
                    data=fact.data,
                    source_url=fact.source_url,
                    fetched_at=fact.fetched_at,
                )
            )
    db.commit()
    return facts


# --- research -----------------------------------------------------------------------


def counts_reply(url="https://example.com/sample-quest", online=(1, 4), local=None):
    """A `player_counts` reply, as JSON text: online and local co-op as (min, max) or
    None, no competitive count, and `url` as the source."""
    rng = lambda r: {"min": r[0], "max": r[1]} if r else None
    return json.dumps(
        {
            "online_coop": rng(online),
            "local_coop": rng(local),
            "competitive": None,
            "source_url": url,
        }
    )


def judged(appid, verdict="accepted", reason="Fits short sessions.", **over):
    """A `JudgedProposal` found by web research that cites the Solo line, with the
    verdict and reason given. Any field can be changed by keyword."""
    base = {
        "appid": appid,
        "source": "web_research",
        "fits": [
            {
                "section": "loved",
                "text": "Likes short, tight sessions",
                "tag": "stated",
                "how": "Short runs.",
            }
        ],
        "risk": "Might be too light.",
        "version": GameVersion(False, None),
        "facts": {},
        "verdict": verdict,
        "reason": reason,
        "list_rejected": False,
        "recommender_call_id": None,
        "judge_call_id": None,
    }
    return JudgedProposal(**(base | over))


def verdicts(accept=(), reject=None) -> dict:
    """A `research_judge` reply: each appid in `accept` accepted for "Fits L1.", and each
    one in `reject` (appid to reason) rejected for its reason."""
    return {
        "verdicts": [
            {"appid": appid, "accept": True, "reason": "Fits L1."} for appid in accept
        ]
        + [
            {"appid": appid, "accept": False, "reason": reason}
            for appid, reason in (reject or {}).items()
        ],
        "reject_all": None,
    }


def fill_lane(db: Session, lane: Lane, n: int, profile_version: ProfileVersion) -> None:
    """Give the lane `n` recommended games: games 100 and up, not owned, each with an
    accepted and added candidate from a finished run of its own, a month before
    FIXED_NOW."""
    then = FIXED_NOW - timedelta(days=30)
    run = ResearchRun(status="finished", targets=[], created_at=then)
    db.add(run)
    db.commit()
    appids = range(100, 100 + n)
    for appid in appids:
        ensure_game(db, appid, f"Sample Filler {appid}", then)
    save_judged(
        db,
        [judged(appid) for appid in appids],
        run_id=run.id,
        round=1,
        lane=lane,
        room=n,
        profile_version_id=profile_version.id,
        now=then,
    )


def messages(db: Session, run: ResearchRun) -> list[str]:
    """The run's progress log, oldest first."""
    return list(
        db.scalars(
            select(ResearchEvent.message)
            .where(ResearchEvent.run_id == run.id)
            .order_by(ResearchEvent.id)
        )
    )


def lane_version(
    db: Session, lane: Lane, review: ReviewVersion, text: str
) -> ProfileVersion:
    """Save the lane's profile version, learned from `review`, with one stated line under
    `loved` (`text`) that cites the review and its game. The profile is created first if
    there is none."""
    profile = ensure_profile(
        db,
        ProfileTarget(lane_key(lane), "lane", lane, None, LANE_LABELS[lane]),
        FIXED_NOW,
    )
    call = AiCall(
        job="recommender",
        provider_kind="anthropic",
        model="sample-big",
        prompt_name="profile_update",
        prompt_version="abc123",
        prompt_hash="0" * 64,
        request="prompt",
        response="reply",
        duration_ms=5,
        created_at=FIXED_NOW,
    )
    db.add(call)
    db.commit()
    appid = db.scalar(select(Review.appid).where(Review.id == review.review_id))
    update = ProfileUpdate.model_validate(
        {
            "profile": {
                "loved": [
                    {
                        "text": text,
                        "tag": "stated",
                        "games": [appid],
                        "reviews": [review.id],
                    }
                ],
                "dealbreakers": [],
                "patterns": [],
                "hunches": [],
                "open_questions": [],
            },
            "changelog": "Learned from new reviews.",
        }
    )
    return save_version(
        db,
        profile,
        update,
        source_ids=[review.id],
        call=call,
        refresh_run_id=None,
        now=FIXED_NOW,
    )


def two_version(db: Session) -> ProfileVersion:
    """The Two players profile's version 1, learned from a published Two players review of
    Test Tactics (appid 20), with one stated line (L1), "Likes planning turns together"."""
    review = publish(
        db,
        20,
        Lane.TWO,
        ReviewForm(rating=7, text="Planning every turn together is the fun part."),
        FIXED_NOW,
    )
    return lane_version(db, Lane.TWO, review, "Likes planning turns together")
