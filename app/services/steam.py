"""Steam Web API and store search.

Every failure becomes a `SteamError` whose message is shown to the user as is. The
messages never include the API key or the request URL (the key travels in the query
string), and the exceptions underneath are dropped rather than chained.
"""

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
