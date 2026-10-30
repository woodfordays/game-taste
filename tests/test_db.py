import warnings
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from conftest import FIXED_NOW
from fastapi import Depends
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import create_app
from app.models import (
    Base,
    Game,
    NamedGroup,
    Person,
    PlaytimeSnapshot,
    Review,
    ReviewVersion,
    Setting,
)

PLAN1_TABLES = {
    "setting",
    "provider",
    "job_assignment",
    "person",
    "named_group",
    "group_member",
    "game",
    "playtime_snapshot",
    "currently_playing_change",
    "review",
    "review_version",
    "review_version_person",
    "review_draft",
}


def test_tables_created_at_startup(app):
    assert PLAN1_TABLES <= set(sqlalchemy.inspect(app.state.engine).get_table_names())


def test_startup_twice_is_safe(tmp_path, services):
    first = create_app(tmp_path, services=services)
    with first.state.session_factory() as session:
        session.add(Setting(key="timezone", value="UTC"))
        session.commit()
    first.state.engine.dispose()

    second = create_app(tmp_path, services=services)
    with second.state.session_factory() as session:
        assert session.get(Setting, "timezone").value == "UTC"
    second.state.engine.dispose()


def test_data_folder_with_spaces_and_percent_signs(tmp_path, services):
    app = create_app(tmp_path / "100% sample data", services=services)
    assert PLAN1_TABLES <= set(sqlalchemy.inspect(app.state.engine).get_table_names())
    app.state.engine.dispose()


def test_models_match_the_migration(app):
    # Fails when a model changes without a new migration.
    with app.state.engine.connect() as conn:
        with warnings.catch_warnings():
            # SQLite can't reflect the lower(name) indexes, which Alembic reports as a warning.
            warnings.filterwarnings("ignore", message=".*expression-based index.*")
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_foreign_keys_enforced(db):
    db.add(PlaytimeSnapshot(appid=999, playtime_minutes=1, taken_at=FIXED_NOW))
    with pytest.raises(IntegrityError):
        db.commit()


def test_person_names_are_unique_ignoring_case(db):
    db.add(Person(name="Sam", created_at=FIXED_NOW))
    db.commit()
    db.add(Person(name="sAM", created_at=FIXED_NOW))
    with pytest.raises(IntegrityError):
        db.commit()


def test_group_names_are_unique_ignoring_case(db):
    db.add(NamedGroup(name="Game night", created_at=FIXED_NOW))
    db.commit()
    db.add(NamedGroup(name="GAME NIGHT", created_at=FIXED_NOW))
    with pytest.raises(IntegrityError):
        db.commit()


def test_one_review_per_game_and_lane(db):
    db.add(
        Game(
            appid=10,
            name="Sample Quest",
            owned=True,
            source="steam_library",
            created_at=FIXED_NOW,
        )
    )
    db.add(Review(appid=10, lane="solo", created_at=FIXED_NOW))
    db.add(Review(appid=10, lane="two", created_at=FIXED_NOW))
    db.commit()
    db.add(Review(appid=10, lane="solo", created_at=FIXED_NOW))
    with pytest.raises(IntegrityError):
        db.commit()


def test_version_numbers_are_unique_within_a_review(db):
    db.add(
        Game(
            appid=10,
            name="Sample Quest",
            owned=True,
            source="steam_library",
            created_at=FIXED_NOW,
        )
    )
    review = Review(appid=10, lane="solo", created_at=FIXED_NOW)
    db.add(review)
    db.commit()

    def version(number: int) -> ReviewVersion:
        return ReviewVersion(
            review_id=review.id,
            version=number,
            text="",
            group_text="",
            tags=[],
            session_length="",
            published_at=FIXED_NOW,
        )

    db.add_all([version(1), version(2)])
    db.commit()
    db.add(version(2))
    with pytest.raises(IntegrityError):
        db.commit()


def test_column_defaults(db):
    db.add(Setting(key="timezone", value="UTC"))
    db.add(
        Game(
            appid=10,
            name="Sample Quest",
            owned=False,
            source="store_search",
            created_at=FIXED_NOW,
        )
    )
    db.commit()
    assert db.get(Setting, "timezone").encrypted is False
    assert db.get(Game, 10).currently_playing is False


def test_json_columns_round_trip(db):
    db.add(
        Game(
            appid=10,
            name="Sample Quest",
            owned=True,
            source="steam_library",
            created_at=FIXED_NOW,
        )
    )
    review = Review(appid=10, lane="solo", created_at=FIXED_NOW)
    db.add(review)
    db.commit()
    db.add(
        ReviewVersion(
            review_id=review.id,
            version=1,
            text="",
            group_text="",
            tags=["co-op", "cozy"],
            session_length="",
            published_at=FIXED_NOW,
        )
    )
    db.commit()
    db.expire_all()
    assert db.scalars(select(ReviewVersion)).one().tags == ["co-op", "cozy"]


def test_datetimes_read_back_as_utc_aware(db):
    played = datetime(2026, 1, 10, 8, 30, tzinfo=timezone.utc)
    db.add(
        Game(
            appid=10,
            name="Sample Quest",
            owned=True,
            source="steam_library",
            last_played_at=played,
            created_at=FIXED_NOW,
        )
    )
    db.commit()
    db.expire_all()
    game = db.get(Game, 10)
    assert game.created_at == FIXED_NOW
    assert game.created_at.utcoffset() == timedelta(0)
    assert game.last_played_at == played
    assert game.last_played_at.utcoffset() == timedelta(0)


def test_other_offsets_are_stored_as_utc(db):
    five_hours_behind = timezone(timedelta(hours=-5))
    db.add(
        Game(
            appid=10,
            name="Sample Quest",
            owned=True,
            source="steam_library",
            created_at=datetime(2026, 1, 15, 7, 0, tzinfo=five_hours_behind),
        )
    )
    db.commit()
    assert (
        db.execute(text("select created_at from game"))
        .scalar_one()
        .startswith("2026-01-15 12:00:00")
    )
    db.expire_all()
    assert db.get(Game, 10).created_at == FIXED_NOW


def test_datetimes_compare_in_queries(db):
    db.add(
        Game(
            appid=10,
            name="Sample Quest",
            owned=True,
            source="steam_library",
            created_at=FIXED_NOW,
        )
    )
    db.add(
        Game(
            appid=20,
            name="Test Tactics",
            owned=True,
            source="steam_library",
            created_at=FIXED_NOW + timedelta(hours=1),
        )
    )
    db.commit()
    later = db.scalars(select(Game.appid).where(Game.created_at > FIXED_NOW)).all()
    assert later == [20]


def test_naive_datetimes_are_rejected(db):
    db.add(
        Game(
            appid=10,
            name="Sample Quest",
            owned=True,
            source="steam_library",
            created_at=datetime(2026, 1, 15, 12, 0),
        )
    )
    with pytest.raises(StatementError, match="timezone-aware"):
        db.commit()


def test_get_db_gives_a_session_in_a_request(app, onboarded_client, db):
    @app.get("/_probe")
    def probe(session: Session = Depends(get_db)) -> int:
        return session.scalar(select(func.count()).select_from(Setting))

    # Logging in saved some settings, so compare with a count taken outside the request.
    expected = db.scalar(select(func.count()).select_from(Setting))
    assert expected > 0
    assert onboarded_client.get("/_probe").json() == expected
