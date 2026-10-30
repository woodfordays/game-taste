"""Onboarding pages after the password: connect your AI (step 2) and Steam (step 3)."""

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
    parse_ai_form,
    provider_row,
    run_ai_checks,
    save_ai_setup,
    stored_ai_setup,
)
from app.services.library import connect_steam
from app.services.steam import SteamClient, SteamError
from app.web import is_htmx, read_form, redirect, render

router = APIRouter()

AI_PATH = "/onboarding/ai"
STEAM_PATH = "/onboarding/steam"
PEOPLE_PATH = "/onboarding/people"

KEY_MISSING = "Enter your Steam Web API key."
PROFILE_MISSING = "Enter your Steam profile link or SteamID64."


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
    vault = request.app.state.vault
    stored = stored_ai_setup(db, vault)

    setup, errors = parse_ai_form(form, stored)
    if errors:
        return _ai_response(request, setup, stored, errors=errors)

    checks = run_ai_checks(db, setup, services)
    if not all(line.result.ok for line in checks):
        return _ai_response(request, setup, stored, checks=checks)

    save_ai_setup(db, vault, setup, now=services.clock())
    advance_step(db, "steam")
    return redirect(request, STEAM_PATH)


# --- step 3: connect Steam ------------------------------------------------------


def _typed(form: FormData, name: str) -> str:
    """One field's text with the whitespace a paste leaves around it removed."""
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


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
    key = _typed(form, "steam_key")
    profile = _typed(form, "steam_profile")
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
