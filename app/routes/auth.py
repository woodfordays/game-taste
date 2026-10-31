"""First-run password, login and logout."""

from fastapi import APIRouter, Depends, Form, Request
from markupsafe import Markup
from sqlalchemy.orm import Session

from app.auth import PASSWORD_PATH, advance_step, sign_in
from app.db import get_db
from app.services.secrets import hash_password, new_password_problem, verify_password
from app.services.settings import get_setting, known_timezones, set_setting
from app.web import redirect, render

router = APIRouter()

# Fixed copy. Markup keeps the apostrophes as typed instead of turning them into &#39;.
WRONG_PASSWORD = Markup("That password didn't match. Try again.")


def clean_timezone(name: str) -> str:
    """The browser's time zone name if it's a real zone, else "UTC"."""
    name = name.strip()
    return name if name in known_timezones() else "UTC"


@router.get("/onboarding/password")
def password_page(request: Request):
    return render(request, "onboarding/password.html", errors={}, submitted_timezone="")


@router.post("/onboarding/password")
def password_submit(
    request: Request,
    password: str = Form(""),
    confirm: str = Form(""),
    timezone: str = Form(""),
    db: Session = Depends(get_db),
):
    if get_setting(db, "password_hash"):
        # Setup is over; the guard normally stops this before it gets here.
        return redirect(request, "/login")

    timezone = clean_timezone(timezone)
    problem = new_password_problem(password, confirm)
    if problem:
        field, message = problem
        # Markup keeps the apostrophes in the fixed copy as typed instead of &#39;.
        return render(
            request,
            "onboarding/password.html",
            status_code=422,
            errors={field: Markup(message)},
            submitted_timezone=timezone,
        )

    # The password goes in last: until it exists the guard keeps this page open, so a
    # failure part-way can be retried.
    set_setting(db, "timezone", timezone)
    advance_step(db, "ai")
    set_setting(db, "password_hash", hash_password(password))
    sign_in(request, db)
    return redirect(request, "/onboarding/ai")


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", error=None)


@router.post("/login")
def login_submit(
    request: Request, password: str = Form(""), db: Session = Depends(get_db)
):
    stored = get_setting(db, "password_hash")
    if not stored:
        return redirect(request, PASSWORD_PATH)
    if not verify_password(stored, password):
        return render(
            request,
            "login.html",
            status_code=401,
            error=WRONG_PASSWORD,
        )
    sign_in(request, db)
    return redirect(request, "/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect(request, "/login")
