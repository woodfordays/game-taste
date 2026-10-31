"""The refresh routine: sync Steam, then bring each taste profile up to date.

`run_refresh` is the handler for "refresh" jobs (see app/jobs.py). It writes a `RefreshRun`
saying what it did, step by step, so the status shown to the user is a record and not a
guess:

1. Steam sync, when Steam is connected. A Steam problem is recorded and the refresh carries on.
2. For each profile with review versions it hasn't learned from, the Everyday tasks model gets
   the current profile and up to 20 of those reviews and sends back the whole new profile.
   `validate_update` checks the evidence rules before anything is saved. A profile that
   fails keeps its old version, and the next profile goes on. A model that isn't set up
   stops the refresh, since every profile would fail the same way.

A failure that the routine expects is recorded in the run. One it doesn't expect fails that
profile (or that step) with the exception's type and a log entry, never its message, which
might hold a secret.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs import JobContext
from app.models import AiCall, GroupMember, Person, RefreshRun
from app.prompts import Prompt, load_prompt
from app.services.ai import Job, StructuredReplyError, complete_json
from app.services.library import steam_credentials, sync_library
from app.services.profiles import (
    ProfileTarget,
    ProfileUpdate,
    current_version,
    ensure_profile,
    play_history,
    profile_sources,
    review_input,
    save_version,
    targets_with_reviews,
    unprocessed_versions,
    validate_update,
)
from app.services.settings import DEFAULT_TIMEZONE, TIMEZONE_SETTING, get_setting
from app.services.steam import SteamClient, SteamError

log = logging.getLogger("game_taste.refresh")

PROMPT_NAME = "profile_update"
# How many review versions one model call learns from.
BATCH_SIZE = 20

UPDATED = "updated"
FAILED = "failed"

STEAM_NOT_CONNECTED = "Steam isn't connected."
STEAM_SYNCED = "Steam synced: {added} new, {total} total."
STEAM_STOPPED = "The Steam sync stopped unexpectedly: {kind}. Try again."
CHECKS_FAILED = "The model's update didn't pass the checks, so this profile wasn't changed: {problem}"
PROFILE_STOPPED = "This profile stopped unexpectedly: {kind}. Try again."


@dataclass
class _ProfileResult:
    """What happened to one profile. `outcome` stays None while there was nothing to learn."""

    key: str
    label: str
    outcome: str | None = None
    versions: list[int] = field(default_factory=list)
    message: str = ""

    def detail(self) -> dict:
        """A new dict each time, so a saved copy is never changed by later work."""
        return {
            "key": self.key,
            "label": self.label,
            "outcome": self.outcome,
            "versions": list(self.versions),
            "message": self.message,
        }


@dataclass
class _Run:
    """The refresh so far, kept in step with its `RefreshRun` row."""

    id: int
    steam_summary: str = ""
    results: list[_ProfileResult] = field(default_factory=list)

    def save(self, ctx: JobContext, *, finished: bool = False) -> None:
        """Write what has happened so far, so a refresh cut short still shows its progress."""
        with ctx.session_factory() as db:
            row = db.get(RefreshRun, self.id)
            row.steam_summary = self.steam_summary
            row.details = [result.detail() for result in self.results]
            row.profiles_updated = sum(1 for result in self.results if result.versions)
            row.profiles_failed = sum(
                1 for result in self.results if result.outcome == FAILED
            )
            if finished:
                row.finished_at = ctx.services.clock()
            db.commit()


def run_refresh(ctx: JobContext, job_id: int) -> None:
    """Run one refresh job. Whatever happens, the run row ends with a finish time."""
    run = _start_run(ctx, job_id)
    try:
        run.steam_summary = _sync_steam(ctx)
        run.save(ctx)
        _update_profiles(ctx, run)
    finally:
        run.save(ctx, finished=True)


def _start_run(ctx: JobContext, job_id: int) -> _Run:
    with ctx.session_factory() as db:
        row = RefreshRun(job_id=job_id, started_at=ctx.services.clock())
        db.add(row)
        db.commit()
        return _Run(row.id)


# --- Steam -------------------------------------------------------------------------------


def _sync_steam(ctx: JobContext) -> str:
    """Sync the library if Steam is connected. Returns the line for the run's summary."""
    try:
        with ctx.session_factory() as db:
            credentials = steam_credentials(db, ctx.vault)
            if credentials is None:
                return STEAM_NOT_CONNECTED
            api_key, steam_id = credentials
            synced = sync_library(
                db,
                SteamClient(ctx.services.steam_http),
                api_key,
                steam_id,
                ctx.services.clock(),
            )
    except SteamError as err:
        return str(err)  # Said for the user to read.
    except Exception as exc:
        log.exception("The Steam sync stopped unexpectedly.")
        return STEAM_STOPPED.format(kind=type(exc).__name__)
    return STEAM_SYNCED.format(added=synced.added, total=synced.total)


# --- profiles ----------------------------------------------------------------------------


def _update_profiles(ctx: JobContext, run: _Run) -> None:
    prompt = load_prompt(PROMPT_NAME)
    with ctx.session_factory() as db:
        targets = targets_with_reviews(db)
    for target in targets:
        result = _ProfileResult(target.key, target.label)
        try:
            stop = _update_profile(ctx, run, prompt, target, result)
        except Exception as exc:
            log.exception("Updating the %s profile stopped unexpectedly.", target.key)
            result.outcome = FAILED
            result.message = PROFILE_STOPPED.format(kind=type(exc).__name__)
            stop = False
        if result.outcome is not None:
            run.results.append(result)
            run.save(ctx)
        if stop:
            return


def _update_profile(
    ctx: JobContext,
    run: _Run,
    prompt: Prompt,
    target: ProfileTarget,
    result: _ProfileResult,
) -> bool:
    """Teach one profile everything new, `BATCH_SIZE` review versions at a time, recording
    what happens in `result`. Returns True when the whole refresh should stop.

    The first batch that fails ends this profile. Its remaining review versions stay
    unlearned, so the next refresh tries them again.
    """
    is_group = target.kind == "group"
    with ctx.session_factory() as db:
        profile = ensure_profile(db, target, ctx.services.clock())
        members = _member_names(db, target.group_id) if is_group else []
        # Hours say nothing about who played, so a group's profile gets none.
        history = [] if is_group else play_history(db)
        history_appids = {game["appid"] for game in history}
        today = _local_today(db, ctx.services.clock())
        while True:
            versions = unprocessed_versions(db, profile, limit=BATCH_SIZE)
            if not versions:
                return False
            reviews = review_input(db, versions)
            batch_ids = [item["id"] for item in reviews]
            batch_appids = {item["appid"] for item in reviews}
            learned_ids, learned_appids = profile_sources(db, profile)
            latest = current_version(db, profile)
            context = {
                "profile_label": target.label,
                "kind": target.kind,
                "members": members,
                "current": _profile_json(latest.content) if latest else None,
                "reviews": reviews,
                "play_history": history,
                "today": today,
            }
            try:
                update, call_id = complete_json(
                    db,
                    ctx.vault,
                    Job.EVERYDAY,
                    prompt,
                    context,
                    ProfileUpdate,
                    services=ctx.services,
                    run_ref=f"refresh:{run.id}",
                    now=ctx.services.clock(),
                )
            except StructuredReplyError as err:
                result.outcome, result.message = FAILED, err.message
                return err.reason == "not_configured"
            problems = validate_update(
                update,
                review_ids=learned_ids | set(batch_ids),
                reviewed_appids=learned_appids | batch_appids,
                history_appids=history_appids,
            )
            if problems:
                result.outcome = FAILED
                result.message = CHECKS_FAILED.format(problem=problems[0])
                return False
            saved = save_version(
                db,
                profile,
                update,
                source_ids=batch_ids,
                call=db.get(AiCall, call_id),
                refresh_run_id=run.id,
                now=ctx.services.clock(),
            )
            result.outcome = UPDATED
            result.versions.append(saved.version)
            result.message = update.changelog


def _member_names(db: Session, group_id: int | None) -> list[str]:
    names = db.scalars(
        select(Person.name)
        .join(GroupMember, GroupMember.person_id == Person.id)
        .where(GroupMember.group_id == group_id)
    )
    return sorted(names, key=lambda name: (name.casefold(), name))


def _local_today(db: Session, now: datetime) -> str:
    """Today's date as ISO text, in the user's timezone (UTC if it isn't a known zone)."""
    name = get_setting(db, TIMEZONE_SETTING) or DEFAULT_TIMEZONE
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = UTC
    return now.astimezone(zone).date().isoformat()


def _profile_json(content: dict) -> str:
    """A profile's content as JSON text with one line per claim: the shape the model is asked
    to reply in, with the same whole-number ids the evidence rules check."""
    sections = []
    for name, lines in content.items():
        body = ",\n".join(
            f"    {json.dumps(line, ensure_ascii=False)}" for line in lines
        )
        sections.append(
            f"  {json.dumps(name)}: [\n{body}\n  ]"
            if lines
            else f"  {json.dumps(name)}: []"
        )
    return "{\n" + ",\n".join(sections) + "\n}"
