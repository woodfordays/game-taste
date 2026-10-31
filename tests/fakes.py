"""Stand-ins for the AI providers, Tavily and Steam, so no test touches the network."""

import copy
import json
from collections.abc import Callable

import httpx

from app.services.ai import Completion, ProviderConfig, ProviderKind


class _FakeAdapter:
    def __init__(self, factory: "FakeAdapterFactory", cfg: ProviderConfig) -> None:
        self._factory = factory
        self._cfg = cfg

    def complete(
        self, model: str, prompt: str, *, timeout: float | None = None
    ) -> Completion:
        self._factory.calls.append((self._cfg.kind, model, prompt))
        self._factory.timeouts.append(timeout)
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

    `.calls` holds one (kind, model, prompt) per `complete()`, and `.timeouts` the
    timeout each of those calls was given. Every reply is "ok" unless
    `.replies[model]` says otherwise, and a model passed to `.fail()` raises.
    `.replies[model]` is a str, a list of str (used in order, the last one repeating; the
    place in the list is counted per model over the factory's life), or a function that
    takes the prompt and returns the reply. `.stop_reasons[model]` is the `stop_reason`
    of that model's replies ("stop", "length" or None, the default).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[ProviderKind, str, str]] = []
        self.timeouts: list[float | None] = []
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


class _FakeSearchClient:
    def __init__(self, factory: "FakeSearchFactory") -> None:
        self._factory = factory

    def search(self, query: str, max_results: int) -> dict:
        self._factory.calls.append((query, max_results))
        if self._factory.failure is not None:
            raise self._factory.failure
        return copy.deepcopy(self._factory.payload)


class FakeSearchFactory:
    """Stands in for `make_search_client`.

    `.payload` is shaped like Tavily's reply. `.calls` holds one (query, max_results)
    per `search()`, and `.fail()` makes every search raise.
    """

    def __init__(self) -> None:
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
        return httpx.Response(404)


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
