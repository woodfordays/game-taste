"""The game library: connecting to Steam, syncing from it, and the artwork for each game."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, PlaytimeSnapshot
from app.services.secrets import Vault
from app.services.settings import set_secret, set_setting
from app.services.steam import SteamClient

STEAM_KEY_SETTING = "steam_api_key"
STEAM_ID_SETTING = "steam_id"


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
    nothing changed, when Steam can't be read.
    """
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
            _snapshot(db, item.appid, item.playtime_minutes, item.last_played_at, now)
            added += 1
            continue
        played_changed = (
            game.playtime_minutes != item.playtime_minutes
            or game.last_played_at != item.last_played_at
        )
        if played_changed or game.name != item.name:
            updated += 1
        if played_changed:
            _snapshot(db, item.appid, item.playtime_minutes, item.last_played_at, now)
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
