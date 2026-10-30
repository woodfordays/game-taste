"""Stand-ins for the AI providers and Tavily, so no test touches the network."""

import copy

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
        text = self._factory.replies.get(model, "ok")
        return Completion(text=text, input_tokens=5, output_tokens=2)


class FakeAdapterFactory:
    """Stands in for `make_adapter`.

    `.calls` holds one (kind, model, prompt) per `complete()`, and `.timeouts` the
    timeout each of those calls was given. Every reply is "ok" unless
    `.replies[model]` says otherwise, and a model passed to `.fail()` raises.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[ProviderKind, str, str]] = []
        self.timeouts: list[float | None] = []
        self.replies: dict[str, str] = {}
        self.failures: dict[str, Exception] = {}

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
