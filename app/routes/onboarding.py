"""Onboarding pages after the password: connect your AI (step 2), Steam (step 3), who you
play with (step 4) and the quick tour (step 5), which ends setup."""

from collections.abc import Iterable
from typing import NamedTuple

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.auth import advance_step
from app.db import get_db
from app.services.ai import ProviderKind
from app.services.ai_setup import (
    AiSetup,
    CheckLine,
    ai_form_view,
    provider_row,
    stored_ai_setup,
    submit_ai_form,
)
from app.services.library import KEY_MISSING, PROFILE_MISSING, connect_steam
from app.services.people import (
    PICK_PEOPLE,
    PeopleError,
    add_group,
    add_person,
    list_groups,
    list_people,
    parse_member_ids,
)
from app.services.steam import SteamClient, SteamError
from app.web import is_htmx, read_form, redirect, render, typed

router = APIRouter()

AI_PATH = "/onboarding/ai"
STEAM_PATH = "/onboarding/steam"
PEOPLE_PATH = "/onboarding/people"
TOUR_PATH = "/onboarding/tour"
FINISH_PATH = "/onboarding/finish"
HOME_PATH = "/"


def _ai_response(
    request: Request,
    setup: AiSetup,
    stored: AiSetup,
    *,
    errors: list[str] | None = None,
    checks: list[CheckLine] | None = None,
):
    """The page, or just the form when htmx will swap it in. Always status 200:
    htmx doesn't swap an error status, and the form carries the errors itself.
    """
    template = "partials/ai_form.html" if is_htmx(request) else "onboarding/ai.html"
    return render(
        request,
        template,
        mode="onboarding",
        form_action=AI_PATH,
        form=ai_form_view(setup, stored, errors=errors or (), checks=checks),
    )


@router.get(AI_PATH)
def ai_page(request: Request, db: Session = Depends(get_db)):
    stored = stored_ai_setup(db, request.app.state.vault)
    return _ai_response(request, stored, stored)


@router.get(f"{AI_PATH}/provider-row")
def ai_provider_row(
    kind: ProviderKind, request: Request, db: Session = Depends(get_db)
):
    """One provider's inputs, for the "Add a provider" button to append to the form."""
    stored = stored_ai_setup(db, request.app.state.vault)
    return render(
        request, "partials/provider_row.html", row=provider_row(kind, stored, stored)
    )


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
    if not result.saved:
        return _ai_response(
            request,
            result.setup,
            result.stored,
            errors=result.errors,
            checks=result.checks,
        )
    advance_step(db, "steam")
    return redirect(request, STEAM_PATH)


# --- step 3: connect Steam ------------------------------------------------------


def _steam_response(
    request: Request,
    *,
    key: str = "",
    profile: str = "",
    error: str | None = None,
    key_error: str | None = None,
    profile_error: str | None = None,
):
    """The page, or just the form when htmx will swap it in. Always status 200, like the
    AI step: htmx doesn't swap an error status, and the form carries the errors itself.
    """
    template = (
        "partials/steam_form.html" if is_htmx(request) else "onboarding/steam.html"
    )
    return render(
        request,
        template,
        steam_key=key,
        steam_profile=profile,
        error=error,
        key_error=key_error,
        profile_error=profile_error,
    )


@router.get(STEAM_PATH)
def steam_page(request: Request):
    return _steam_response(request)


@router.post(STEAM_PATH)
def steam_submit(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    services = request.app.state.services
    key = typed(form, "steam_key")
    profile = typed(form, "steam_profile")
    if not key or not profile:
        return _steam_response(
            request,
            key=key,
            profile=profile,
            key_error=None if key else KEY_MISSING,
            profile_error=None if profile else PROFILE_MISSING,
        )

    try:
        connect_steam(
            db,
            request.app.state.vault,
            SteamClient(services.steam_http),
            key,
            profile,
            services.clock(),
        )
    except SteamError as failure:
        # The message says what happened and what to do. It is plain text, never markup,
        # because it can quote what the user typed.
        return _steam_response(request, key=key, profile=profile, error=str(failure))
    advance_step(db, "people")
    return redirect(request, PEOPLE_PATH)


@router.post(f"{STEAM_PATH}/skip")
def steam_skip(request: Request, db: Session = Depends(get_db)):
    advance_step(db, "people")
    return redirect(request, PEOPLE_PATH)


# --- step 4: who you play with --------------------------------------------------


def _people_response(
    request: Request,
    db: Session,
    *,
    person_name: str = "",
    person_error: str | None = None,
    group_name: str = "",
    group_error: str | None = None,
    member_error: str | None = None,
    selected: Iterable[int] = (),
):
    """The page, or just the people section when htmx will swap it in. Always status 200,
    like the steps before: htmx doesn't swap an error status, and the section carries the
    errors itself. The typed values come back so a failed add doesn't lose them.
    """
    template = (
        "partials/people_section.html" if is_htmx(request) else "onboarding/people.html"
    )
    return render(
        request,
        template,
        people=list_people(db),
        groups=list_groups(db),
        person_name=person_name,
        person_error=person_error,
        group_name=group_name,
        group_error=group_error,
        member_error=member_error,
        selected=list(selected),
    )


@router.get(PEOPLE_PATH)
def people_page(request: Request, db: Session = Depends(get_db)):
    return _people_response(request, db)


@router.post(f"{PEOPLE_PATH}/person")
def people_add_person(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    name = typed(form, "name")
    try:
        add_person(db, name, request.app.state.services.clock())
    except PeopleError as failure:
        # The message can quote a name, so it stays a plain string and is escaped when shown.
        return _people_response(
            request, db, person_name=name, person_error=str(failure)
        )
    return _people_response(request, db)


@router.post(f"{PEOPLE_PATH}/group")
def people_add_group(
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
        message = str(failure)
        # A refused member list is shown by the list, anything else by the name field.
        return _people_response(
            request,
            db,
            group_name=name,
            group_error=None if message == PICK_PEOPLE else message,
            member_error=message if message == PICK_PEOPLE else None,
            selected=members,
        )
    return _people_response(request, db)


@router.post(f"{PEOPLE_PATH}/done")
def people_done(request: Request, db: Session = Depends(get_db)):
    """Continue and Skip for now both land here: nothing needs adding to move on."""
    advance_step(db, "tour")
    return redirect(request, TOUR_PATH)


# --- step 5: the quick tour -----------------------------------------------------


class TourScreen(NamedTuple):
    title: str
    body: tuple[str, ...]  # paragraphs, as plain text


# The loop in four screens: review, profiles, queue, decline with a reason. Written in the
# present tense as what the app does, with no dates, because profiles and recommendations
# arrive in later updates.
TOUR_SCREENS = [
    TourScreen(
        "Write a review in your own words",
        (
            "After you play a game, write down what you thought of it. Say it however "
            "you like: what worked, what didn't, who you played with. Nothing is "
            "required beyond a few words.",
            "A game can have a separate review for each way you play it, such as solo "
            "or with a group. Your words are saved exactly as you wrote them, and a "
            "later review adds a new version instead of replacing the old one.",
        ),
    ),
    TourScreen(
        "Your profiles learn from your reviews",
        (
            "Game Taste reads your reviews and keeps a taste profile for each way you "
            "play: solo, two players, three to four, and five or more. Each named group "
            "you add gets its own profile too.",
            "A profile says in plain language what you like and what puts you off. It "
            "changes as you write more reviews.",
        ),
    ),
    TourScreen(
        "The queue suggests what to play next",
        (
            "For each way you play, Game Taste keeps a queue of games you might try "
            "next. Each pick says which parts of your profile it matches, what the main "
            "risk is, and when its price, review score and release status were last "
            "checked.",
            "Games you already own but haven't played come before anything you would "
            "have to buy.",
        ),
    ),
    TourScreen(
        "Decline a pick, and say why",
        (
            "If a pick isn't for you, decline it and give a reason. The reason counts "
            "as evidence, like a review does: it shapes your profile and what Game "
            "Taste looks for next.",
            "That completes the loop: write, learn, play or decline, then write again. "
            "Home is next, and a good first step there is to write about a game you "
            "played recently.",
        ),
    ),
]


def _tour_number(raw: str) -> int:
    """The screen `raw` asks for, from 1 to the last. Anything that isn't a number is the
    first screen, and a number outside the tour is the nearest end, so a stale link
    never errors."""
    try:
        number = int(raw)
    except ValueError:
        return 1
    return min(max(number, 1), len(TOUR_SCREENS))


@router.get(TOUR_PATH)
def tour_page(request: Request, page: str = "1"):
    number = _tour_number(page)
    return render(
        request,
        "onboarding/tour.html",
        page=number,
        pages=len(TOUR_SCREENS),
        screen=TOUR_SCREENS[number - 1],
        back_url=PEOPLE_PATH if number == 1 else f"{TOUR_PATH}?page={number - 1}",
        next_url=f"{TOUR_PATH}?page={number + 1}",
        last=number == len(TOUR_SCREENS),
    )


@router.post(FINISH_PATH)
def finish(request: Request, db: Session = Depends(get_db)):
    """End setup. The guard only lets this through at the tour step, so setup can't be
    finished from an earlier one."""
    advance_step(db, "done")
    return redirect(request, HOME_PATH)
