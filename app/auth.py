"""Who may see what: the onboarding steps, the sign-in session and the guard middleware."""

from datetime import timedelta

from fastapi import FastAPI, Request
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.services.settings import get_session_epoch, get_setting, set_setting
from app.web import redirect

# The steps after the password, in the order the user walks through them. "done" means
# onboarding is finished. The password step comes before all of these and isn't listed:
# it is the current step whenever no password exists.
ONBOARDING_STEPS = ["ai", "steam", "people", "tour", "done"]

PASSWORD_STEP = "password"
PASSWORD_PATH = "/onboarding/password"
SESSION_MAX_AGE = int(timedelta(days=30).total_seconds())
# Named for the app so it doesn't collide with another app's "session" cookie on the same
# host (a different port shares cookies).
SESSION_COOKIE = "game_taste_session"


def _rank(step: str) -> int:
    """Position of `step` in ONBOARDING_STEPS. Unknown names rank after every real step."""
    if step in ONBOARDING_STEPS:
        return ONBOARDING_STEPS.index(step)
    return len(ONBOARDING_STEPS)


def current_step(db: Session) -> str:
    """Return "password" while no password exists, else the saved step ("ai" if none is saved)."""
    if not get_setting(db, "password_hash"):
        return PASSWORD_STEP
    saved = get_setting(db, "onboarding_step")
    return saved if saved in ONBOARDING_STEPS else ONBOARDING_STEPS[0]


def setup_finished(db: Session) -> bool:
    """Whether onboarding was finished, which stays true through a password reset.

    A reset drops only the password, so a no-password visit to a finished setup is a user
    who is back to choose a new one, not someone setting the app up.
    """
    return get_setting(db, "onboarding_step") == "done"


def advance_step(db: Session, to: str) -> None:
    """Save `to` as the current step, unless the user is already at or past it.

    Going back to an earlier step and submitting it again therefore never rewinds
    progress. Raises ValueError for a name that isn't in ONBOARDING_STEPS.
    """
    if to not in ONBOARDING_STEPS:
        raise ValueError(f"Unknown onboarding step: {to!r}")
    saved = get_setting(db, "onboarding_step")
    if saved not in ONBOARDING_STEPS or _rank(to) > _rank(saved):
        set_setting(db, "onboarding_step", to)


def sign_in(request: Request, db: Session) -> None:
    """Mark the request's session as logged in, stamped with the current session epoch.

    The epoch is what lets a password change or reset end every other session: the guard
    treats a session carrying an older epoch as logged out.
    """
    request.session["user"] = True
    request.session["epoch"] = get_session_epoch(db)


def is_signed_in(request: Request, db: Session) -> bool:
    """Whether the request's session is logged in and carries the current epoch."""
    session = request.session
    return session.get("user") is True and session.get("epoch") == get_session_epoch(db)


def step_url(step: str) -> str:
    return f"/onboarding/{step}"


def _is_open(path: str) -> bool:
    """Static files and the health check are open to everyone."""
    return path == "/healthz" or path.startswith("/static/")


def _in_onboarding(path: str) -> bool:
    return path == "/onboarding" or path.startswith("/onboarding/")


def _step_of_path(path: str) -> str:
    """The step an onboarding path belongs to: the segment after /onboarding/.

    "/onboarding/ai/provider-row" belongs to "ai" and "/onboarding/finish" to "tour".
    Any other name comes back as is, and `_rank` puts it after every real step.
    """
    segment = path.removeprefix("/onboarding").lstrip("/").split("/", 1)[0]
    return "tour" if segment == "finish" else segment


def redirect_target(path: str, *, logged_in: bool, step: str) -> str | None:
    """Where to send a request for `path`, or None to let it through.

    `step` is `current_step()`. The rules apply in order and the first match wins.
    Rule 1 (open paths) is `_is_open`, which the guard checks before it reads `step`.
    """
    # 2. With no password, the first-run page is the only page.
    if step == PASSWORD_STEP:
        return None if path == PASSWORD_PATH else PASSWORD_PATH

    # 3. Once a password exists that page is closed for good.
    if path == PASSWORD_PATH:
        return "/" if logged_in else "/login"

    # 4. Logged out: the login page is the only page.
    if not logged_in:
        return None if path == "/login" else "/login"

    # 5. Mid-onboarding: the current step or an earlier one stays reachable, for Back
    # buttons. Everything else, the login page included, goes back to the current step.
    if step != "done":
        if _in_onboarding(path):
            return step_url(step) if _rank(_step_of_path(path)) > _rank(step) else None
        # Logging out stays possible, so a user can leave in the middle of setup.
        return None if path == "/logout" else step_url(step)

    # 6. Onboarding finished: the setup pages and the login page have nothing left to do.
    return "/" if path == "/login" or _in_onboarding(path) else None


async def guard(request: Request, call_next):
    """Send each request to the page its visitor may see, following `redirect_target`."""
    path = request.url.path
    if _is_open(path):
        return await call_next(request)
    with request.app.state.session_factory() as db:
        step = current_step(db)
        logged_in = is_signed_in(request, db)
    target = redirect_target(path, logged_in=logged_in, step=step)
    if target is None:
        return await call_next(request)
    return redirect(request, target)


def install_auth(app: FastAPI) -> None:
    """Add the guard and the session middleware.

    Starlette makes the middleware added last the outermost one, so the session goes
    on after the guard. That way `request.session` is already loaded when the guard runs.
    """
    app.middleware("http")(guard)
    app.add_middleware(
        SessionMiddleware,
        secret_key=app.state.session_secret,
        session_cookie=SESSION_COOKIE,
        same_site="lax",
        max_age=SESSION_MAX_AGE,
    )
