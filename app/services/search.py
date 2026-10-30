"""Tavily web search, with every search recorded."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.orm import Session
from tavily import TavilyClient

from app.models import SearchCall
from app.services.ai import CheckResult, describe_error

CONNECTION_TEST_QUERY = "best co-op games on Steam"


@dataclass
class SearchResult:
    title: str
    url: str
    content: str


class SearchClient(Protocol):
    def search(self, query: str, max_results: int) -> dict: ...


class SearchError(Exception):
    """A search failed. The message is the text saved in the search log."""


class TavilySearchClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client: Any = None

    def search(self, query: str, max_results: int) -> dict:
        # Built on first use, so making a client never touches the network.
        if self._client is None:
            self._client = TavilyClient(self.api_key)
        return self._client.search(query, max_results=max_results)


def make_search_client(api_key: str) -> SearchClient:
    return TavilySearchClient(api_key)


def search_and_log(
    db: Session,
    client: SearchClient,
    query: str,
    *,
    max_results: int = 5,
    run_ref: str | None = None,
    now: datetime | None = None,
) -> list[SearchResult]:
    """Search and record the call, whether it worked or not.

    On failure the error is logged and a `SearchError` carrying the same text is raised.
    """
    row = SearchCall(
        query=query, run_ref=run_ref, created_at=now or datetime.now(timezone.utc)
    )
    failure: Exception | None = None
    payload: dict = {}
    try:
        payload = client.search(query, max_results)
    except Exception as exc:
        failure = exc
    error_text = describe_error(failure) if failure is not None else None
    row.error = error_text
    row.raw_results = payload if failure is None else None
    db.add(row)
    db.commit()
    if failure is not None:
        raise SearchError(error_text) from failure
    return [
        SearchResult(
            title=item.get("title") or "",
            url=item.get("url") or "",
            content=item.get("content") or "",
        )
        for item in payload.get("results") or []
    ]


def check_search(
    db: Session, client: SearchClient, *, now: datetime | None = None
) -> CheckResult:
    """Run one small search to see whether the Tavily key works."""
    try:
        search_and_log(db, client, CONNECTION_TEST_QUERY, max_results=1, now=now)
    except SearchError as err:
        cause = err.__cause__ or err
        return CheckResult(
            False, f"Didn't connect: {type(cause).__name__}: {str(cause)[:200]}"
        )
    return CheckResult(True, "Connected")
