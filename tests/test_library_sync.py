from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Game, PlaytimeSnapshot
from app.services.library import SyncResult, artwork_url, sync_library
from app.services.steam import SteamError
from conftest import FIXED_NOW

LAST_PLAYED = 1768000000


def snapshot_count(db) -> int:
    return db.scalar(select(func.count()).select_from(PlaytimeSnapshot))


def test_sync_snapshots_only_on_change(db, steam_client, fake_steam):
    fake_steam.owned = [
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
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW) == SyncResult(
        added=2, updated=0, total=2
    )
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW).added == 0
    assert db.scalar(select(func.count()).select_from(PlaytimeSnapshot)) == 2
    fake_steam.owned[0]["playtime_forever"] = 660
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    assert (
        db.scalar(select(func.count()).select_from(PlaytimeSnapshot)) == 3
        and db.get(Game, 10).playtime_minutes == 660
    )
    assert db.get(Game, 20).last_played_at is None


def test_new_games_are_owned_steam_library_rows_with_a_first_snapshot(
    db, steam_client, fake_steam
):
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    game = db.get(Game, 10)
    assert (game.name, game.owned, game.source, game.currently_playing) == (
        "Sample Quest",
        True,
        "steam_library",
        False,
    )
    assert game.playtime_minutes == 600
    assert game.last_played_at == datetime.fromtimestamp(LAST_PLAYED, tz=timezone.utc)
    assert game.created_at == FIXED_NOW
    snap = db.scalars(
        select(PlaytimeSnapshot).where(PlaytimeSnapshot.appid == 10)
    ).one()
    assert (snap.playtime_minutes, snap.last_played_at, snap.taken_at) == (
        600,
        game.last_played_at,
        FIXED_NOW,
    )


def test_a_changed_game_gets_a_new_snapshot_and_counts_as_updated(
    db, steam_client, fake_steam
):
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    later = FIXED_NOW + timedelta(days=1)
    fake_steam.owned[0]["playtime_forever"] = 660
    fake_steam.owned[0]["rtime_last_played"] = LAST_PLAYED + 3600
    assert sync_library(db, steam_client, "k", "id", later) == SyncResult(
        added=0, updated=1, total=2
    )
    newest = db.scalars(
        select(PlaytimeSnapshot)
        .where(PlaytimeSnapshot.appid == 10)
        .order_by(PlaytimeSnapshot.id.desc())
    ).first()
    assert (newest.playtime_minutes, newest.taken_at) == (660, later)
    assert newest.last_played_at == datetime.fromtimestamp(
        LAST_PLAYED + 3600, tz=timezone.utc
    )
    # The earlier snapshot is kept.
    assert snapshot_count(db) == 3
    assert db.get(Game, 10).created_at == FIXED_NOW


def test_last_played_alone_is_a_change(db, steam_client, fake_steam):
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    fake_steam.owned[1]["rtime_last_played"] = LAST_PLAYED
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW).updated == 1
    assert snapshot_count(db) == 3
    assert db.get(Game, 20).last_played_at is not None


def test_a_rename_updates_the_name_and_counts_but_takes_no_snapshot(
    db, steam_client, fake_steam
):
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    fake_steam.owned[0]["name"] = "Sample Quest: Remastered"
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW) == SyncResult(
        added=0, updated=1, total=2
    )
    assert db.get(Game, 10).name == "Sample Quest: Remastered"
    assert snapshot_count(db) == 2


def test_games_missing_from_the_response_are_left_alone(db, steam_client, fake_steam):
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    fake_steam.owned = [fake_steam.owned[0]]
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW) == SyncResult(
        added=0, updated=0, total=1
    )
    gone = db.get(Game, 20)
    assert (gone.name, gone.owned, gone.playtime_minutes) == ("Test Tactics", True, 120)
    assert snapshot_count(db) == 2


def test_sync_leaves_currently_playing_alone(db, steam_client, fake_steam):
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    db.get(Game, 10).currently_playing = True
    db.commit()
    fake_steam.owned[0]["playtime_forever"] = 700
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    assert db.get(Game, 10).currently_playing is True


def test_a_store_search_game_becomes_owned_once_it_shows_up_in_the_library(
    db, steam_client, fake_steam
):
    db.add(
        Game(
            appid=10,
            name="Sample Quest (demo)",
            owned=False,
            playtime_minutes=None,
            last_played_at=None,
            source="store_search",
            created_at=FIXED_NOW - timedelta(days=3),
        )
    )
    db.commit()
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW) == SyncResult(
        added=1, updated=1, total=2
    )
    game = db.get(Game, 10)
    assert (game.name, game.owned, game.source, game.playtime_minutes) == (
        "Sample Quest",
        True,
        "steam_library",
        600,
    )
    assert game.created_at == FIXED_NOW - timedelta(days=3)
    assert snapshot_count(db) == 2


def test_a_repeated_app_id_in_one_response_is_one_game(db, steam_client, fake_steam):
    fake_steam.owned = [fake_steam.owned[0], dict(fake_steam.owned[0])]
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW) == SyncResult(
        added=1, updated=0, total=1
    )
    assert snapshot_count(db) == 1


@pytest.mark.parametrize(
    "failure",
    [403, 500, httpx.ConnectTimeout("slow")],
)
def test_a_steam_failure_raises_and_changes_nothing(
    db, steam_client, fake_steam, failure
):  # Review focus 1
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    fake_steam.owned[0]["playtime_forever"] = 999
    fake_steam.fail_with(failure)
    with pytest.raises(SteamError):
        sync_library(db, steam_client, "k", "id", FIXED_NOW)
    assert db.get(Game, 10).playtime_minutes == 600
    assert snapshot_count(db) == 2


def test_a_private_profile_raises_and_changes_nothing(db, steam_client, fake_steam):
    fake_steam.owned = []
    with pytest.raises(SteamError, match="set Game details to Public"):
        sync_library(db, steam_client, "k", "id", FIXED_NOW)
    assert db.scalar(select(func.count()).select_from(Game)) == 0


def test_sync_commits(app, db, steam_client):
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    with app.state.session_factory() as other:
        assert other.scalar(select(func.count()).select_from(Game)) == 2
        assert other.scalar(select(func.count()).select_from(PlaytimeSnapshot)) == 2


def test_artwork_url():
    assert (
        artwork_url(10)
        == "https://cdn.cloudflare.steamstatic.com/steam/apps/10/header.jpg"
    )
