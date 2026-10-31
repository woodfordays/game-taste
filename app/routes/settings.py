"""Settings: AI and search, Steam, people and groups, the schedule and the account.

Each section is a card with a form of its own. A form posts to its own route, which
answers with the whole page, or with just that section when htmx made the request so the
other sections (and anything typed in them) stay as they are. Every answer has status
200, a refusal included, because htmx doesn't swap an error status; the section carries
its own errors. Stored keys and passwords are never sent back to the page.
"""

from dataclasses import dataclass, field
from typing import Annotated

from argon2.exceptions import InvalidHashError
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.models import NamedGroup, Person
from app.routes.onboarding import ai_provider_row
from app.services.ai_setup import (
    AiSubmitResult,
    ai_form_view,
    saved_key_hint,
    stored_ai_setup,
    submit_ai_form,
)
from app.services.library import save_steam_settings, steam_credentials
from app.services.people import (
    PICK_PEOPLE,
    PeopleError,
    add_group,
    add_person,
    list_groups,
    list_people,
    parse_member_ids,
    rename_group,
    rename_person,
    set_group_members,
)
from app.services.secrets import hash_password, new_password_problem, verify_password
from app.services.settings import (
    DEFAULT_REFRESH_TIME,
    DEFAULT_STORE_REGION,
    DEFAULT_TIMEZONE,
    REFRESH_TIME_SETTING,
    STORE_REGION_SETTING,
    TIMEZONE_SETTING,
    get_setting,
    known_timezones,
    parse_refresh_time,
    parse_region,
    parse_timezone,
    set_setting,
)
from app.services.steam import SteamClient
from app.web import is_htmx, read_form, render, typed

router = APIRouter()

SETTINGS_PATH = "/settings"
AI_PATH = "/settings/ai"
STEAM_PATH = "/settings/steam"
SCHEDULE_PATH = "/settings/schedule"
PASSWORD_PATH = "/settings/password"

# SQLite stores integers in 64 bits, so a larger id can't be looked up. It is refused as a
# bad request rather than reaching the database.
MAX_ROW_ID = 2**63 - 1
RowId = Annotated[int, Path(ge=1, le=MAX_ROW_ID)]

# Fixed copy.
REGION_INVALID = "Enter a two-letter country code, like us or gb."
TIME_INVALID = "Enter a time like 04:00."
TIMEZONE_INVALID = "Pick a timezone from the list."
TIMEZONE_CHOOSE = "Choose a timezone"
WRONG_CURRENT_PASSWORD = "Your current password didn't match."
NO_SUCH_PERSON = "No such person."
NO_SUCH_GROUP = "No such group."

# The partial each section is swapped in with.
PARTIALS = {
    "ai": "partials/ai_form.html",
    "steam": "partials/settings_steam.html",
    "people": "partials/settings_people.html",
    "schedule": "partials/settings_schedule.html",
    "password": "partials/settings_password.html",
}


# --- what each section shows --------------------------------------------------
#
# Every section has a builder that takes the request, the database and what a submit
# found (nothing, for a plain visit) and returns the template context for its partial.


@dataclass
class SteamView:
    steam_id: str | None  # the saved SteamID64, when connected
    key_hint: str  # "Saved key ending in abcd", or "" when no key is saved
    key: str = ""  # typed this time; never the saved key
    profile: str = ""  # typed this time
    region: str = DEFAULT_STORE_REGION
    key_error: str | None = None
    profile_error: str | None = None
    region_error: str | None = None
    error: str | None = None  # what Steam said when it refused
    saved: str | None = None  # what a save did, in a sentence


@dataclass
class ScheduleView:
    refresh_time: str
    timezone: str
    timezone_options: list[tuple[str, str]]
    timezone_placeholder: str | None  # set when `timezone` is not on the list
    time_error: str | None = None
    timezone_error: str | None = None
    saved: bool = False


@dataclass
class AccountView:
    current_error: str | None = None
    new_error: str | None = None
    confirm_error: str | None = None
    saved: bool = False


@dataclass
class Refusal:
    """A people form that was refused: which one, what was typed and what to tell the user.

    `form` is "add_person", "rename_person", "add_group", "rename_group" or "members";
    `target` is the id of the person or group the form belongs to, if it has one.
    `on_members` says the message belongs under the member list rather than the name.
    """

    form: str
    target: int | None
    message: str
    name: str = ""
    members: list[int] = field(default_factory=list)
    on_members: bool = False


@dataclass
class RosterView:
    people: list[Person]
    groups: list[tuple[NamedGroup, list[Person]]]
    refused: Refusal | None = None


def _ai_context(
    request: Request, db: Session, *, result: AiSubmitResult | None = None
) -> dict:
    if result is None:
        stored = stored_ai_setup(db, request.app.state.vault)
        form = ai_form_view(stored, stored)
    else:
        form = ai_form_view(
            result.setup,
            result.stored,
            errors=result.errors,
            checks=result.checks,
            saved=result.saved,
        )
    # Onboarding's addresses are closed once setup is finished, so Settings gives its own.
    return {
        "mode": "settings",
        "form_action": AI_PATH,
        "row_url": f"{AI_PATH}/provider-row",
        "form": form,
    }


def _steam_context(
    request: Request, db: Session, *, key: str = "", region: str | None = None, **state
) -> dict:
    saved_key, steam_id = steam_credentials(db, request.app.state.vault) or (None, None)
    if region is None:
        region = (
            get_setting(db, STORE_REGION_SETTING, DEFAULT_STORE_REGION)
            or DEFAULT_STORE_REGION
        )
    view = SteamView(
        steam_id=steam_id,
        key_hint=saved_key_hint(saved_key),
        # The saved key typed again is the saved key, which is never shown.
        key="" if key == saved_key else key,
        region=region,
        **state,
    )
    return {"steam": view}


def _roster_context(
    request: Request, db: Session, *, refused: Refusal | None = None
) -> dict:
    return {
        "roster": RosterView(list_people(db), list_groups(db), refused),
    }


def _schedule_context(
    request: Request,
    db: Session,
    *,
    refresh_time: str | None = None,
    timezone: str | None = None,
    **state,
) -> dict:
    if refresh_time is None:
        refresh_time = (
            get_setting(db, REFRESH_TIME_SETTING, DEFAULT_REFRESH_TIME)
            or DEFAULT_REFRESH_TIME
        )
    if timezone is None:
        timezone = (
            get_setting(db, TIMEZONE_SETTING, DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
        )
    zones = sorted(known_timezones())
    view = ScheduleView(
        refresh_time=refresh_time,
        timezone=timezone,
        timezone_options=[(name, name) for name in zones],
        # A saved name that isn't on the list would leave the first zone showing, and
        # saving would quietly pick it. Ask for a choice instead.
        timezone_placeholder=None if timezone in known_timezones() else TIMEZONE_CHOOSE,
        **state,
    )
    return {"schedule": view}


def _account_context(request: Request, db: Session, **state) -> dict:
    return {"account": AccountView(**state)}


BUILDERS = {
    "ai": _ai_context,
    "steam": _steam_context,
    "people": _roster_context,
    "schedule": _schedule_context,
    "password": _account_context,
}


def _respond(request: Request, db: Session, section: str | None = None, **state):
    """The Settings page, or just `section` when htmx will swap it in.

    `state` is what the section's submit found, for its builder. The other sections
    show what is saved.
    """
    context: dict = {}
    for name, build in BUILDERS.items():
        if name == section:
            context.update(build(request, db, **state))
        elif section is None or not is_htmx(request):
            context.update(build(request, db))
    if section is not None and is_htmx(request):
        return render(request, PARTIALS[section], **context)
    return render(request, "settings.html", nav="settings", **context)


@router.get(SETTINGS_PATH)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return _respond(request, db)


# --- AI and search ------------------------------------------------------------

# Onboarding's handler for "Add provider" doesn't depend on onboarding, so Settings, which
# is out of reach of /onboarding/* once setup is finished, serves it at its own address.
router.add_api_route(f"{AI_PATH}/provider-row", ai_provider_row, methods=["GET"])


@router.post(AI_PATH)
def ai_submit(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    services = request.app.state.services
    result = submit_ai_form(
        db, request.app.state.vault, form, services, now=services.clock()
    )
    return _respond(request, db, "ai", result=result)


# --- Steam --------------------------------------------------------------------


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@router.post(STEAM_PATH)
def steam_submit(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    services = request.app.state.services
    key = typed(form, "steam_key")
    profile = typed(form, "steam_profile")
    region_text = typed(form, "store_region")
    # What was typed goes back into the fields whenever nothing is saved.
    kept = {"key": key, "profile": profile, "region": region_text}

    region = parse_region(region_text)
    if region is None:
        return _respond(request, db, "steam", region_error=REGION_INVALID, **kept)

    outcome = save_steam_settings(
        db,
        request.app.state.vault,
        SteamClient(services.steam_http),
        api_key=key,
        profile=profile,
        region=region,
        now=services.clock(),
    )
    if not outcome.ok:
        # Steam's message is plain text, never markup: it can quote what was typed.
        return _respond(
            request,
            db,
            "steam",
            key_error=outcome.key_error,
            profile_error=outcome.profile_error,
            error=outcome.error,
            **kept,
        )
    if outcome.synced is None:
        saved = "Store region saved."
    else:
        saved = (
            f"Imported {_plural(outcome.synced.total, 'game')} from Steam, "
            f"{outcome.synced.added} new. Store region saved."
        )
    return _respond(request, db, "steam", saved=saved)


# --- people and groups --------------------------------------------------------


def _require(
    db: Session, model: type[Person] | type[NamedGroup], row_id: int, what: str
):
    if db.get(model, row_id) is None:
        raise HTTPException(status_code=404, detail=what)


def _refusal(
    form: str,
    target: int | None,
    failure: PeopleError,
    *,
    name: str = "",
    members: list[int] | None = None,
) -> Refusal:
    message = str(failure)  # may quote a name, so it stays a plain string
    return Refusal(
        form,
        target,
        message,
        name=name,
        members=members or [],
        on_members=message == PICK_PEOPLE,
    )


@router.post("/settings/people/person")
def person_add(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    name = typed(form, "name")
    try:
        add_person(db, name, request.app.state.services.clock())
    except PeopleError as failure:
        refused = _refusal("add_person", None, failure, name=name)
        return _respond(request, db, "people", refused=refused)
    return _respond(request, db, "people")


@router.post("/settings/people/{person_id}/rename")
def person_rename(
    person_id: RowId,
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    _require(db, Person, person_id, NO_SUCH_PERSON)
    name = typed(form, "name")
    try:
        rename_person(db, person_id, name)
    except PeopleError as failure:
        refused = _refusal("rename_person", person_id, failure, name=name)
        return _respond(request, db, "people", refused=refused)
    return _respond(request, db, "people")


@router.post("/settings/groups/group")
def group_add(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    name = typed(form, "name")
    members, clean = parse_member_ids(form.getlist("members"))
    try:
        if not clean:
            raise PeopleError(PICK_PEOPLE)
        add_group(db, name, members, request.app.state.services.clock())
    except PeopleError as failure:
        refused = _refusal("add_group", None, failure, name=name, members=members)
        return _respond(request, db, "people", refused=refused)
    return _respond(request, db, "people")


@router.post("/settings/groups/{group_id}/rename")
def group_rename(
    group_id: RowId,
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    _require(db, NamedGroup, group_id, NO_SUCH_GROUP)
    name = typed(form, "name")
    try:
        rename_group(db, group_id, name)
    except PeopleError as failure:
        refused = _refusal("rename_group", group_id, failure, name=name)
        return _respond(request, db, "people", refused=refused)
    return _respond(request, db, "people")


@router.post("/settings/groups/{group_id}/members")
def group_members(
    group_id: RowId,
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    _require(db, NamedGroup, group_id, NO_SUCH_GROUP)
    members, clean = parse_member_ids(form.getlist("members"))
    try:
        if not clean:
            raise PeopleError(PICK_PEOPLE)
        set_group_members(db, group_id, members)
    except PeopleError as failure:
        refused = _refusal("members", group_id, failure, members=members)
        return _respond(request, db, "people", refused=refused)
    return _respond(request, db, "people")


# --- schedule -----------------------------------------------------------------


@router.post(SCHEDULE_PATH)
def schedule_submit(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    time_text = typed(form, "refresh_time")
    zone_text = typed(form, "timezone")
    refresh_time = parse_refresh_time(time_text)
    timezone = parse_timezone(zone_text)
    if refresh_time is None or timezone is None:
        # Neither is saved, the valid one included, so a refusal leaves the schedule whole.
        return _respond(
            request,
            db,
            "schedule",
            refresh_time=time_text,
            timezone=zone_text,
            time_error=TIME_INVALID if refresh_time is None else None,
            timezone_error=TIMEZONE_INVALID if timezone is None else None,
        )
    set_setting(db, REFRESH_TIME_SETTING, refresh_time)
    set_setting(db, TIMEZONE_SETTING, timezone)
    return _respond(request, db, "schedule", saved=True)


# --- account ------------------------------------------------------------------


def _password(form: FormData, name: str) -> str:
    """A password field exactly as typed: spaces are part of a password."""
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _is_current_password(stored: str | None, typed_password: str) -> bool:
    """Whether `typed_password` is the saved one. No saved password, or one that can't
    be read, matches nothing."""
    if not stored:
        return False
    try:
        return verify_password(stored, typed_password)
    except InvalidHashError:
        return False


@router.post(PASSWORD_PATH)
def password_submit(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    current = _password(form, "current")
    new = _password(form, "new")
    confirm = _password(form, "confirm")

    errors: dict[str, str] = {}
    if not _is_current_password(get_setting(db, "password_hash"), current):
        errors["current_error"] = WRONG_CURRENT_PASSWORD
    problem = new_password_problem(new, confirm)
    if problem:
        field_name, message = problem
        errors["new_error" if field_name == "password" else "confirm_error"] = message
    if errors:
        return _respond(request, db, "password", **errors)

    # The session stays as it is, so this device stays logged in.
    set_setting(db, "password_hash", hash_password(new))
    return _respond(request, db, "password", saved=True)
