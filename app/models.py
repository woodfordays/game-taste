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
    false,
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
    source: Mapped[str]  # steam_library, store_search or research
    created_at: Mapped[datetime]


class GameFact(Base):
    """One dated snapshot of something known about a game, never edited. The latest one of
    each kind is the current fact. `kind` is store, reviews, news or players, and `data`
    is that kind's JSON (see `app.services.game_facts`). `source_url` is the page the
    numbers came from, for players, and None for the Steam kinds."""

    __tablename__ = "game_fact"
    __table_args__ = (
        Index("ix_game_fact_appid_kind_fetched_at", "appid", "kind", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    appid: Mapped[int] = mapped_column(ForeignKey("game.appid"))
    kind: Mapped[str]
    data: Mapped[dict] = mapped_column(JSON)
    source_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime]


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
    # The version of the prompt file (see app/prompts). Blank for a call with no prompt file.
    prompt_version: Mapped[str] = mapped_column(server_default="")
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


class Job(Base):
    """One piece of background work: queued, picked up by the worker, then done or failed."""

    __tablename__ = "job"
    __table_args__ = (Index("ix_job_kind_status", "kind", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]  # refresh (a later plan adds research)
    status: Mapped[str]  # queued, running, done or failed
    requested_by: Mapped[str]  # user or schedule
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # What to tell the user when the job failed. Never an exception's own message, which
    # might hold a secret.
    error: Mapped[str | None] = mapped_column(Text)


class Profile(Base):
    """A taste profile: one per lane and one per named group. Its versions hold the content."""

    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(unique=True)  # "lane:solo" or "group:3"
    kind: Mapped[str]  # lane or group
    lane: Mapped[str | None]
    group_id: Mapped[int | None] = mapped_column(ForeignKey("named_group.id"))
    created_at: Mapped[datetime]


class ProfileVersion(Base):
    """One version of a profile, never edited once saved. The model, provider and prompt
    that wrote it are kept beside it, and `profile_version_source` lists the review versions
    it learned from."""

    __tablename__ = "profile_version"
    __table_args__ = (UniqueConstraint("profile_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id"))
    version: Mapped[int]
    # The five sections (see `app.services.profiles.ProfileContent`).
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    changelog: Mapped[str] = mapped_column(Text, default="")
    ai_call_id: Mapped[int | None] = mapped_column(ForeignKey("ai_call.id"))
    # The refresh run that made this version.
    refresh_run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_run.id"))
    model: Mapped[str]
    provider_kind: Mapped[str]
    prompt_name: Mapped[str]
    prompt_version: Mapped[str]
    created_at: Mapped[datetime]


class ProfileVersionSource(Base):
    """A review version that a profile version learned from."""

    __tablename__ = "profile_version_source"

    profile_version_id: Mapped[int] = mapped_column(
        ForeignKey("profile_version.id"), primary_key=True
    )
    review_version_id: Mapped[int] = mapped_column(
        ForeignKey("review_version.id"), primary_key=True
    )


class RefreshRun(Base):
    """What one refresh did: the Steam sync and, for each profile with new reviews, whether it
    was updated. `details` is a list of `{"key", "label", "outcome", "versions", "message"}`:
    the outcome is "updated", "failed" or "skipped" (the refresh doesn't use "skipped" yet),
    `versions` are the version numbers the refresh saved, and `message` is the changelog line
    of the newest one, or why the profile wasn't updated.

    `profiles_total`, `profiles_checked` and `current_profile` are its progress while it runs:
    how many profiles it will look at (None until it has counted them, after the Steam sync),
    how many it has finished with, those that had nothing new included, and the label of the
    one it is on (None between profiles and once it is done)."""

    __tablename__ = "refresh_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"), unique=True)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    steam_summary: Mapped[str] = mapped_column(Text, server_default="")
    profiles_updated: Mapped[int] = mapped_column(server_default="0")
    profiles_failed: Mapped[int] = mapped_column(server_default="0")
    details: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    profiles_total: Mapped[int | None]
    profiles_checked: Mapped[int] = mapped_column(server_default="0")
    current_profile: Mapped[str | None] = mapped_column(Text)


class ResearchRun(Base):
    """One run of research: which lanes it works on and how many games each should reach
    (`targets`, one `LaneTarget.as_json()` per lane in lane order), how far it has got, and how
    it ended. `status` is running, paused, finished, stopped, cancelled or failed. The user's
    choices and the progress log are kept beside it (`ResearchChoice`, `ResearchEvent`).
    `refresh_run_id` is the refresh the run started with, if it needed one."""

    __tablename__ = "research_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str]
    targets: Mapped[list[dict]] = mapped_column(JSON)
    rounds_allowed: Mapped[int] = mapped_column(default=3, server_default="3")
    rounds_done: Mapped[int] = mapped_column(default=0, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(
        default=False, server_default=false()
    )
    refresh_run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_run.id"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class ResearchChoice(Base):
    """One thing the user chose in a run, never edited: `start`, `add_five` (with the lane
    it was ticked for), `continue`, `stop` or `cancel`."""

    __tablename__ = "research_choice"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_run.id"))
    choice: Mapped[str]
    lane: Mapped[str | None]
    made_at: Mapped[datetime]


class ResearchEvent(Base):
    """One line of a run's progress log, never edited."""

    __tablename__ = "research_event"
    __table_args__ = (Index("ix_research_event_run_id", "run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_run.id"))
    round: Mapped[int | None]
    lane: Mapped[str | None]
    step: Mapped[str]
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]


class Candidate(Base):
    """One game research proposed for a lane, and what the judge made of it, never edited.

    `source` is where the game came from (web_research, candidate_history or owned_backlog).
    `fits` is a list of `{"section", "text", "tag", "how"}`: the lines of the lane's profile
    the proposal cites and how the game meets each. `version` and `facts` are what was known
    of the game when it was judged (`GameVersion.as_json()` and `GameFacts.snapshot()`), and
    `profile_version_id` the version of the lane's profile it was judged against. `verdict` is
    accepted, rejected or no_verdict. `added` says whether it went into the inventory, which
    an accepted game does only while the lane has room."""

    __tablename__ = "candidate"
    __table_args__ = (Index("ix_candidate_lane_appid", "lane", "appid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_run.id"))
    round: Mapped[int]
    lane: Mapped[str]
    appid: Mapped[int] = mapped_column(ForeignKey("game.appid"))
    source: Mapped[str]
    fits: Mapped[list[dict]] = mapped_column(JSON)
    risk: Mapped[str] = mapped_column(Text)
    version: Mapped[dict] = mapped_column(JSON)
    facts: Mapped[dict] = mapped_column(JSON)
    profile_version_id: Mapped[int] = mapped_column(ForeignKey("profile_version.id"))
    verdict: Mapped[str]
    reason: Mapped[str] = mapped_column(Text)
    list_rejected: Mapped[bool] = mapped_column(default=False, server_default=false())
    added: Mapped[bool] = mapped_column(default=False, server_default=false())
    recommender_call_id: Mapped[int | None] = mapped_column(ForeignKey("ai_call.id"))
    judge_call_id: Mapped[int | None] = mapped_column(ForeignKey("ai_call.id"))
    created_at: Mapped[datetime]


class InventoryEntry(Base):
    """A game in a lane's inventory. `status` is recommended, in_progress, played or
    declined, and each change of it is kept as an `InventoryStatusChange`. `candidate_id` is
    the accepted proposal behind the game's latest Recommended."""

    __tablename__ = "inventory_entry"
    __table_args__ = (UniqueConstraint("lane", "appid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lane: Mapped[str]
    appid: Mapped[int] = mapped_column(ForeignKey("game.appid"))
    status: Mapped[str]
    rank: Mapped[int]
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"))
    created_at: Mapped[datetime]


class InventoryStatusChange(Base):
    """One change of an inventory entry's status, never edited."""

    __tablename__ = "inventory_status_change"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("inventory_entry.id"))
    status: Mapped[str]
    reason: Mapped[str | None] = mapped_column(Text)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"))
    changed_at: Mapped[datetime]
