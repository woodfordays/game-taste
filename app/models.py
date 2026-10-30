from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """A timezone-aware datetime that is stored as UTC.

    SQLite keeps no offset, so values would otherwise come back naive. This converts
    on the way in and attaches UTC on the way out, so every value read is aware and
    compares correctly with `datetime.now(timezone.utc)`.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(timezone=True)

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                f"Expected a timezone-aware datetime, got a naive one: {value!r}"
            )
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


# Named constraints let later migrations alter them in SQLite batch mode.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: UtcDateTime,
        list[str]: JSON,
        list[int]: JSON,
    }


# Enum-like columns (lane, source, status, verdict, walk_away, complexity) are plain strings.
# The service layer checks them against the allowed values.


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    encrypted: Mapped[bool] = mapped_column(default=False)


class Provider(Base):
    __tablename__ = "provider"

    kind: Mapped[str] = mapped_column(primary_key=True)
    base_url: Mapped[str | None]
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime]


class JobAssignment(Base):
    __tablename__ = "job_assignment"

    job: Mapped[str] = mapped_column(primary_key=True)
    provider_kind: Mapped[str] = mapped_column(ForeignKey("provider.kind"))
    model: Mapped[str]
    updated_at: Mapped[datetime]


class Person(Base):
    __tablename__ = "person"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    created_at: Mapped[datetime]


Index("ix_person_name_lower", func.lower(Person.name), unique=True)


class NamedGroup(Base):
    __tablename__ = "named_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    created_at: Mapped[datetime]


Index("ix_named_group_name_lower", func.lower(NamedGroup.name), unique=True)


class GroupMember(Base):
    __tablename__ = "group_member"

    group_id: Mapped[int] = mapped_column(
        ForeignKey("named_group.id"), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), primary_key=True)


class Game(Base):
    __tablename__ = "game"

    # Steam's own app id, so it is never generated here.
    appid: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str]
    owned: Mapped[bool]
    playtime_minutes: Mapped[int | None]
    last_played_at: Mapped[datetime | None]
    currently_playing: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str]  # steam_library or store_search
    created_at: Mapped[datetime]


class PlaytimeSnapshot(Base):
    __tablename__ = "playtime_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    appid: Mapped[int] = mapped_column(ForeignKey("game.appid"))
    playtime_minutes: Mapped[int]
    last_played_at: Mapped[datetime | None]
    taken_at: Mapped[datetime]


class CurrentlyPlayingChange(Base):
    __tablename__ = "currently_playing_change"

    id: Mapped[int] = mapped_column(primary_key=True)
    appid: Mapped[int] = mapped_column(ForeignKey("game.appid"))
    playing: Mapped[bool]
    changed_at: Mapped[datetime]


class Review(Base):
    __tablename__ = "review"
    __table_args__ = (UniqueConstraint("appid", "lane"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    appid: Mapped[int] = mapped_column(ForeignKey("game.appid"))
    lane: Mapped[str]
    created_at: Mapped[datetime]


class ReviewVersion(Base):
    __tablename__ = "review_version"
    __table_args__ = (UniqueConstraint("review_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("review.id"))
    version: Mapped[int]
    rating: Mapped[int | None]
    text: Mapped[str] = mapped_column(Text)
    group_text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]]
    status: Mapped[str | None]
    verdict: Mapped[str | None]
    walk_away: Mapped[str | None]
    session_length: Mapped[str] = mapped_column(Text)
    complexity: Mapped[str | None]
    named_group_id: Mapped[int | None] = mapped_column(ForeignKey("named_group.id"))
    steam_hours: Mapped[float | None] = mapped_column(Float)
    published_at: Mapped[datetime]

    # Who played on this version, by person id. Loaded with the version, so a list of
    # versions costs one extra query rather than one per version.
    people: Mapped[list["ReviewVersionPerson"]] = relationship(
        order_by="ReviewVersionPerson.person_id", lazy="selectin"
    )


class ReviewVersionPerson(Base):
    __tablename__ = "review_version_person"

    version_id: Mapped[int] = mapped_column(
        ForeignKey("review_version.id"), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), primary_key=True)


class ReviewDraft(Base):
    __tablename__ = "review_draft"

    review_id: Mapped[int] = mapped_column(ForeignKey("review.id"), primary_key=True)
    rating: Mapped[int | None]
    text: Mapped[str] = mapped_column(Text)
    group_text: Mapped[str] = mapped_column(Text)
    tags_raw: Mapped[str] = mapped_column(Text)
    status: Mapped[str | None]
    verdict: Mapped[str | None]
    walk_away: Mapped[str | None]
    session_length: Mapped[str] = mapped_column(Text)
    complexity: Mapped[str | None]
    named_group_id: Mapped[int | None] = mapped_column(ForeignKey("named_group.id"))
    people: Mapped[list[int]]
    updated_at: Mapped[datetime]


class AiCall(Base):
    """One call to an AI provider, kept whether it worked or not."""

    __tablename__ = "ai_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str | None]
    provider_kind: Mapped[str]
    model: Mapped[str]
    prompt_name: Mapped[str]
    prompt_hash: Mapped[str]  # sha256 hex of the prompt
    request: Mapped[str] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    duration_ms: Mapped[int]
    error: Mapped[str | None] = mapped_column(Text)
    run_ref: Mapped[str | None]
    created_at: Mapped[datetime]


class SearchCall(Base):
    """One Tavily search, kept whether it worked or not."""

    __tablename__ = "search_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str]
    # none_as_null: a failed search stores SQL NULL, not the JSON text "null".
    raw_results: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    error: Mapped[str | None] = mapped_column(Text)
    run_ref: Mapped[str | None]
    created_at: Mapped[datetime]
