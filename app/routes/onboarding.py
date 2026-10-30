"""Onboarding pages after the password: connect your AI (step 2)."""

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
from app.web import redirect, render

router = APIRouter()

AI_PATH = "/onboarding/ai"
STEAM_PATH = "/onboarding/steam"


async def read_form(request: Request) -> FormData:
    """The posted form. Read here so the route itself can be a plain function.

    The connection checks block for up to a minute or more, so the route has to run in a
    worker thread; an `async def` route would hold up the whole server.
    """
    return await request.form()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


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
    template = "partials/ai_form.html" if _is_htmx(request) else "onboarding/ai.html"
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
