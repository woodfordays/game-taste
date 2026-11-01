"""Steam Web API, store search, and the store's details, reviews and patch news.

Every failure becomes a `SteamError` whose message is shown to the user as is. The
messages never include the API key or the request URL (the key travels in the query
string), and the exceptions underneath are dropped rather than chained.
"""

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

API_BASE = "https://api.steampowered.com"
STORE_BASE = "https://store.steampowered.com"

REFUSED_KEY = "Steam refused the API key. Check the key and try again."
NO_GAMES = (
    "Steam returned no games. In Steam, open your profile's Privacy Settings, "
    "set Game details to Public, then try again."
)
UNKNOWN_PROFILE = (
    "Couldn't find a Steam profile called '{name}'. "
    "Paste your profile link or SteamID64 and try again."
)
NO_RESPONSE = "Steam didn't respond. Try again in a minute."


class SteamError(Exception):
    """Something went wrong talking to Steam. The message is shown to the user as is."""


@dataclass
class OwnedGame:
    appid: int
    name: str
    playtime_minutes: int
    last_played_at: datetime | None


@dataclass
class StoreHit:
    appid: int
    name: str


@dataclass(frozen=True)
class StoreDetails:
    """What the store says about one app, in the store region's currency."""

    appid: int
    type: str  # Steam's type: "game", "dlc", "demo", ...
    name: str
    short_description: str
    is_free: bool
    price_cents: int | None  # price_overview.final; None when free or not on sale
    currency: str | None
    price_text: str | None  # price_overview.final_formatted, such as "$19.99"
    release_date: str  # Steam's own text, such as "21 Oct, 2024"; "" when missing
    coming_soon: bool
    early_access: bool  # a genre with id "70" or description "Early Access"
    categories: list[str]  # descriptions, such as "Single-player", "Online Co-op"
    genres: list[str]
    developers: list[str]


@dataclass(frozen=True)
class ReviewSummary:
    score_desc: str  # "Very Positive"; Steam says "No user reviews" when there are none
    total_positive: int
    total_negative: int
    total_reviews: int


@dataclass(frozen=True)
class PatchNews:
    latest_at: datetime | None  # the newest post tagged "patchnotes", in UTC
    title: str | None


class SteamClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def resolve_steamid(self, api_key: str, raw: str) -> str:
        """Turn what the user pasted into a SteamID64.

        Accepts the 17-digit ID, a `/profiles/<id>` link, a vanity name, or an
        `/id/<name>` link. Only a vanity name costs a call to Steam.
        """
        text = raw.strip()
        found = re.search(r"/profiles/(\d{17})", text)
        if found:
            return found.group(1)
        if re.fullmatch(r"\d{17}", text):
            return text
        found = re.search(r"/id/([^/?#]+)", text)
        vanity = (
            found.group(1) if found else text.split("?")[0].split("#")[0].rstrip("/")
        )
        if not vanity:
            raise SteamError(UNKNOWN_PROFILE.format(name=vanity))
        data = self._get_json(
            f"{API_BASE}/ISteamUser/ResolveVanityURL/v1/",
            {"key": api_key, "vanityurl": vanity},
            uses_key=True,
        )
        response = data.get("response")
        if not isinstance(response, dict):
            raise SteamError(NO_RESPONSE)
        steamid = response.get("steamid")
        if response.get("success") != 1 or not isinstance(steamid, str):
            raise SteamError(UNKNOWN_PROFILE.format(name=vanity))
        return steamid

    def owned_games(self, api_key: str, steamid: str) -> list[OwnedGame]:
        data = self._get_json(
            f"{API_BASE}/IPlayerService/GetOwnedGames/v1/",
            {
                "key": api_key,
                "steamid": steamid,
                "include_appinfo": 1,
                "include_played_free_games": 1,
            },
            uses_key=True,
        )
        response = data.get("response")
        if not isinstance(response, dict):
            raise SteamError(NO_RESPONSE)
        # A private profile comes back as an empty `response`, with no `games` at all.
        raw_games = response.get("games")
        if not raw_games:
            raise SteamError(NO_GAMES)
        if not isinstance(raw_games, list):
            raise SteamError(NO_RESPONSE)
        games = [game for item in raw_games if (game := _parse_owned(item))]
        if not games:
            raise SteamError(NO_RESPONSE)
        return games

    def store_search(self, term: str, region: str) -> list[StoreHit]:
        data = self._get_json(
            f"{STORE_BASE}/api/storesearch/",
            {"term": term, "cc": region, "l": "english"},
            uses_key=False,
        )
        items = data.get("items")
        if not isinstance(items, list):
            raise SteamError(NO_RESPONSE)
        hits = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                hits.append(StoreHit(int(item["id"]), str(item["name"])))
            except (KeyError, TypeError, ValueError):
                continue
        return hits

    def app_details(self, appid: int, region: str) -> StoreDetails | None:
        """The store's entry for `appid`, priced in `region`.

        None means the store has nothing to show for it there: a delisted app, or one
        not sold in that region.
        """
        data = self._get_json(
            f"{STORE_BASE}/api/appdetails",
            {"appids": appid, "cc": region, "l": "english"},
            uses_key=False,
        )
        return parse_app_details(appid, data)

    def review_summary(self, appid: int) -> ReviewSummary:
        data = self._get_json(
            f"{STORE_BASE}/appreviews/{appid}",
            {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
            uses_key=False,
        )
        summary = data.get("query_summary")
        if data.get("success") != 1 or not isinstance(summary, dict):
            raise SteamError(NO_RESPONSE)
        try:
            return ReviewSummary(
                score_desc=str(summary["review_score_desc"]),
                total_positive=int(summary["total_positive"]),
                total_negative=int(summary["total_negative"]),
                total_reviews=int(summary["total_reviews"]),
            )
        except (KeyError, TypeError, ValueError):
            raise SteamError(NO_RESPONSE) from None

    def patch_news(self, appid: int) -> PatchNews:
        """The newest post tagged "patchnotes" among the app's last 20 announcements."""
        data = self._get_json(
            f"{API_BASE}/ISteamNews/GetNewsForApp/v2/",
            {
                "appid": appid,
                "count": 20,
                "maxlength": 1,
                "feeds": "steam_community_announcements",
            },
            uses_key=False,
        )
        appnews = data.get("appnews")
        items = appnews.get("newsitems") if isinstance(appnews, dict) else None
        if not isinstance(items, list):
            raise SteamError(NO_RESPONSE)
        newest = PatchNews(None, None)
        for item in items:
            if not isinstance(item, dict):
                continue
            # `tags` is missing from most posts that aren't patch notes.
            if "patchnotes" not in (item.get("tags") or []):
                continue
            try:
                posted = datetime.fromtimestamp(int(item["date"]), tz=timezone.utc)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
            if newest.latest_at is None or posted > newest.latest_at:
                newest = PatchNews(posted, str(item.get("title") or "") or None)
        return newest

    def _get_json(self, url: str, params: dict, *, uses_key: bool) -> dict:
        """GET `url` and return its JSON object, or raise a `SteamError`.

        `uses_key` says the call carries the user's API key, so a 401 or 403 means
        the key was refused. The store takes no key, so there it is just a failure.
        """
        try:
            response = self._http.get(url, params=params)
        except httpx.RequestError:
            # Timeouts and connection errors. Dropped, not chained: the exception
            # holds the request, and with it the URL and the key.
            raise SteamError(NO_RESPONSE) from None
        if uses_key and response.status_code in (401, 403):
            raise SteamError(REFUSED_KEY)
        if not response.is_success:
            raise SteamError(NO_RESPONSE)
        try:
            data = response.json()
        except ValueError:
            raise SteamError(NO_RESPONSE) from None
        if not isinstance(data, dict):
            raise SteamError(NO_RESPONSE)
        return data


def parse_app_details(appid: int, payload: dict) -> StoreDetails | None:
    """Read an `api/appdetails` reply for `appid`.

    None means the store says it has no such app (`success` is false). A reply that
    isn't in the shape Steam sends, or that has no name, is a `SteamError`.
    """
    entry = payload.get(str(appid))
    if not isinstance(entry, dict):
        raise SteamError(NO_RESPONSE)
    if "success" in entry and not entry["success"]:
        return None
    data = entry.get("data")
    if not isinstance(data, dict) or not data.get("name"):
        raise SteamError(NO_RESPONSE)
    is_free = bool(data.get("is_free"))
    price = data.get("price_overview")
    # A free game has no price, and neither has one that isn't out yet.
    if is_free or not isinstance(price, dict):
        price = {}
    release = data.get("release_date")
    if not isinstance(release, dict):
        release = {}
    genres = _items(data.get("genres"))
    return StoreDetails(
        appid=appid,
        type=str(data.get("type") or ""),
        name=str(data["name"]),
        # Steam sends it HTML-escaped, and templates would escape it a second time.
        short_description=html.unescape(str(data.get("short_description") or "")),
        is_free=is_free,
        price_cents=_to_int(price.get("final")),
        currency=str(price["currency"]) if price.get("currency") else None,
        price_text=(
            str(price["final_formatted"]) if price.get("final_formatted") else None
        ),
        release_date=str(release.get("date") or ""),
        coming_soon=bool(release.get("coming_soon")),
        early_access=any(
            str(genre.get("id")) == "70" or genre.get("description") == "Early Access"
            for genre in genres
        ),
        categories=_descriptions(_items(data.get("categories"))),
        genres=_descriptions(genres),
        developers=[str(d) for d in data.get("developers") or [] if d],
    )


def _items(value: object) -> list[dict]:
    """The dicts in a list Steam sent, or nothing if it isn't a list."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _descriptions(items: list[dict]) -> list[str]:
    return [str(item["description"]) for item in items if item.get("description")]


def _to_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_owned(item: object) -> OwnedGame | None:
    if not isinstance(item, dict):
        return None
    try:
        appid = int(item["appid"])
        minutes = int(item.get("playtime_forever") or 0)
        last_played = int(item.get("rtime_last_played") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    return OwnedGame(
        appid=appid,
        name=str(item.get("name") or f"App {appid}"),
        playtime_minutes=minutes,
        # Steam sends 0 for "never played".
        last_played_at=(
            datetime.fromtimestamp(last_played, tz=timezone.utc)
            if last_played
            else None
        ),
    )
