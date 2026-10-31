"""The game library: connecting to Steam, syncing from it, and the artwork for each game."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from cryptography.fernet import InvalidToken
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import CurrentlyPlayingChange, Game, PlaytimeSnapshot, Review
from app.services.ai_setup import KEY_UNREADABLE
from app.services.secrets import Vault
from app.services.settings import (
    STORE_REGION_SETTING,
    get_secret,
    get_setting,
    set_secret,
    set_setting,
)
from app.services.steam import SteamClient, SteamError

STEAM_KEY_SETTING = "steam_api_key"
STEAM_ID_SETTING = "steam_id"

SOURCE_STORE_SEARCH = "store_search"

# Fixed copy for a connection form with a field left empty.
KEY_MISSING = "Enter your Steam Web API key."
PROFILE_MISSING = "Enter your Steam profile link or SteamID64."

# One sync at a time, so the refresh's sync and the Library "Re-sync" can't interleave.
_sync_lock = threading.Lock()


@dataclass
class SyncResult:
    added: int
    updated: int
    total: int


def sync_library(
    db: Session, steam: SteamClient, api_key: str, steamid: str, now: datetime
) -> SyncResult:
    """Copy the owned games from Steam into the library.

    New games are added and known ones refreshed (name, playtime, last played). A
    playtime snapshot is written only when a game is new or its playtime or last
    played time changed, so snapshots form a history rather than a copy per sync.
    Games Steam no longer lists are left as they are. Raises `SteamError`, with
    nothing changed, when Steam can't be read. Only one sync runs at a time, so a second
    caller waits for the first to finish.
    """
    with _sync_lock:
        # Keyed by app id, so a repeated entry is one game.
        owned = {game.appid: game for game in steam.owned_games(api_key, steamid)}
        known = {game.appid: game for game in db.scalars(select(Game))}
        added = updated = 0
        for item in owned.values():
            game = known.get(item.appid)
            if game is None:
                db.add(
                    Game(
                        appid=item.appid,
                        name=item.name,
                        owned=True,
                        playtime_minutes=item.playtime_minutes,
                        last_played_at=item.last_played_at,
                        source="steam_library",
                        created_at=now,
                    )
                )
                _snapshot(
                    db, item.appid, item.playtime_minutes, item.last_played_at, now
                )
                added += 1
                continue
            played_changed = (
                game.playtime_minutes != item.playtime_minutes
                or game.last_played_at != item.last_played_at
            )
            if played_changed or game.name != item.name:
                updated += 1
            if played_changed:
                _snapshot(
                    db, item.appid, item.playtime_minutes, item.last_played_at, now
                )
            game.name = item.name
            game.owned = True
            game.source = "steam_library"
            game.playtime_minutes = item.playtime_minutes
            game.last_played_at = item.last_played_at
        db.commit()
        return SyncResult(added=added, updated=updated, total=len(owned))


def connect_steam(
    db: Session,
    vault: Vault,
    steam: SteamClient,
    api_key: str,
    profile: str,
    now: datetime,
) -> SyncResult:
    """Connect Steam: resolve `profile`, import the library, then save the key and ID.

    `profile` is whatever the user pasted: a profile link, a custom URL name or a
    SteamID64. The key (encrypted) and the resolved SteamID64 are saved only once the
    import has worked, so a failure leaves an earlier connection as it was. The store
    region is not touched. Raises `SteamError` with nothing saved.
    """
    api_key = api_key.strip()
    steamid = steam.resolve_steamid(api_key, profile)
    result = sync_library(db, steam, api_key, steamid, now)
    set_secret(db, vault, STEAM_KEY_SETTING, api_key)
    set_setting(db, STEAM_ID_SETTING, steamid)
    return result


@dataclass
class StoredSteam:
    """What is saved about the Steam connection, the key and the SteamID64 apart.

    `api_key` is None when none is saved or the saved one can't be read; `key_unreadable`
    tells those apart. `steam_id` is None when none is saved.
    """

    api_key: str | None = field(repr=False)  # never printed
    steam_id: str | None
    key_unreadable: bool = False


def stored_steam(db: Session, vault: Vault) -> StoredSteam:
    """The saved Steam key and SteamID64, each on its own.

    A key that can't be decrypted, such as one saved under another key file, comes back
    as no key with `key_unreadable` set, and the SteamID64 is still returned: it is a
    plain setting and the user doesn't have to enter it again.
    """
    api_key: str | None
    try:
        api_key = get_secret(db, vault, STEAM_KEY_SETTING) or None
        unreadable = False
    except InvalidToken:
        api_key, unreadable = None, True
    return StoredSteam(api_key, get_setting(db, STEAM_ID_SETTING) or None, unreadable)


def steam_credentials(db: Session, vault: Vault) -> tuple[str, str] | None:
    """The saved Steam API key and SteamID64, or None when Steam isn't connected.

    A key that can't be decrypted, such as one saved under another key file, counts
    as not connected: the user has to enter it again either way, and nothing can be
    synced with it. (Settings reads the two apart with `stored_steam`.)
    """
    saved = stored_steam(db, vault)
    if not saved.api_key or not saved.steam_id:
        return None
    return saved.api_key, saved.steam_id


@dataclass
class SteamSave:
    """What saving the Steam settings came to. Nothing is saved unless `ok`.

    `key_error` and `profile_error` name a field left empty with nothing saved to fall
    back on. `error` is what Steam said when it refused. `synced` is the import, or None
    when only the store region was saved.
    """

    key_error: str | None = None
    profile_error: str | None = None
    error: str | None = None
    synced: SyncResult | None = None

    @property
    def ok(self) -> bool:
        return not (self.key_error or self.profile_error or self.error)


def save_steam_settings(
    db: Session,
    vault: Vault,
    steam: SteamClient,
    *,
    api_key: str,
    profile: str,
    region: str,
    now: datetime,
) -> SteamSave:
    """Save the Steam section of Settings: the store region, and the connection when asked.

    `region` must already be valid (see `parse_region`). A blank `api_key` keeps the
    saved key and a blank `profile` keeps the saved SteamID (which stays usable when the
    saved key can't be read, and then a key must be typed). With both blank only the
    region is saved, whether or not Steam is connected. Otherwise Steam is connected
    again with whatever is typed over what is saved (`connect_steam`), and the region is
    saved with it. If that can't be done, nothing is saved, the region included.
    """
    api_key, profile = api_key.strip(), profile.strip()
    if not api_key and not profile:
        set_setting(db, STORE_REGION_SETTING, region)
        return SteamSave()

    saved = stored_steam(db, vault)
    api_key = api_key or saved.api_key or ""
    profile = profile or saved.steam_id or ""
    if not api_key or not profile:
        return SteamSave(
            # A key that is saved but can't be read says so, rather than "enter your key".
            key_error=(
                None
                if api_key
                else KEY_UNREADABLE
                if saved.key_unreadable
                else KEY_MISSING
            ),
            profile_error=None if profile else PROFILE_MISSING,
        )
    try:
        synced = connect_steam(db, vault, steam, api_key, profile, now)
    except SteamError as failure:
        return SteamSave(error=str(failure))
    set_setting(db, STORE_REGION_SETTING, region)
    return SteamSave(synced=synced)


def _by_name(game: Game) -> tuple[str, int]:
    return (game.name.casefold(), game.appid)


def _by_hours(game: Game) -> tuple:
    played = game.playtime_minutes
    return (played is None, -(played or 0), *_by_name(game))


def _by_last_played(game: Game) -> tuple:
    last = game.last_played_at
    return (last is None, -last.timestamp() if last else 0, *_by_name(game))


# Each key puts a game with no value last and breaks ties by name.
SORT_KEYS: dict[str, Callable[[Game], tuple]] = {
    "hours": _by_hours,
    "last_played": _by_last_played,
    "name": _by_name,
}


def list_library(db: Session, q: str = "", sort: str = "hours") -> list[Game]:
    """The games the Library page lists, from one query.

    These are the owned games, the ones currently playing, the ones with any review,
    and the ones added by name (`add_store_game`), which would otherwise vanish before
    they are played or reviewed. `q` keeps the games whose name contains it, ignoring
    case and the spaces around it. `sort` is "hours" (most played first), "last_played"
    (latest first) or "name"; a game with no value for the sort comes last. Raises
    `ValueError` for any other sort.

    Matching and ordering happen here rather than in SQL because SQLite folds only
    ASCII letters: "Über" would not match "über".
    """
    if sort not in SORT_KEYS:
        raise ValueError(f"Unknown sort: {sort!r}")
    reviewed = select(Review.id).where(Review.appid == Game.appid).exists()
    games = list(
        db.scalars(
            select(Game).where(
                or_(
                    Game.owned.is_(True),
                    Game.currently_playing.is_(True),
                    Game.source == SOURCE_STORE_SEARCH,
                    reviewed,
                )
            )
        )
    )
    needle = q.strip().casefold()
    if needle:
        games = [game for game in games if needle in game.name.casefold()]
    return sorted(games, key=SORT_KEYS[sort])


def set_currently_playing(
    db: Session, appid: int, playing: bool, now: datetime
) -> bool:
    """Switch "Currently playing" for a game and record the change.

    Returns False, writing nothing, when the game is already in that state. Raises
    `LookupError` for an app id that isn't in the library.
    """
    game = db.get(Game, appid)
    if game is None:
        raise LookupError(f"No game with app id {appid}")
    if game.currently_playing == playing:
        return False
    game.currently_playing = playing
    db.add(CurrentlyPlayingChange(appid=appid, playing=playing, changed_at=now))
    db.commit()
    return True


def add_store_game(db: Session, appid: int, name: str, now: datetime) -> Game:
    """Add a game found in the Steam store, for one the user doesn't own.

    A game that is already known is returned as it is. A new one has no playtime
    (None, not 0), so the first Steam sync that finds it owned records its first
    snapshot.
    """
    game = db.get(Game, appid)
    if game is None:
        game = Game(
            appid=appid,
            name=name,
            owned=False,
            playtime_minutes=None,
            last_played_at=None,
            currently_playing=False,
            source=SOURCE_STORE_SEARCH,
            created_at=now,
        )
        db.add(game)
    db.commit()
    return game


def _snapshot(
    db: Session,
    appid: int,
    playtime_minutes: int,
    last_played_at: datetime | None,
    taken_at: datetime,
) -> None:
    db.add(
        PlaytimeSnapshot(
            appid=appid,
            playtime_minutes=playtime_minutes,
            last_played_at=last_played_at,
            taken_at=taken_at,
        )
    )


def artwork_url(appid: int) -> str:
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
