"""Page rendering: templates, the render()/redirect() helpers and the display filters."""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.datastructures import FormData

from app.services.library import artwork_url
from app.services.reviews import LANE_LABELS
from app.services.settings import get_setting

APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# httpx logs every request's URL at INFO, and a Steam call carries the API key in its URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

# SQLite integers are 64-bit, so an id past this could not be a row's.
MAX_ID = 2**63 - 1
_ID = re.compile(r"[0-9]{1,19}")  # MAX_ID has 19 digits

# Starlette creates the Jinja environment with autoescape on.
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# The Ember badge for each evidence tag (partials/tag_badge.html). The badge carries the word as
# well, so the color is never the only signal.
TAG_TONES = {"stated": "neutral", "observed": "info", "guess": "warning"}

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _zone(tz: str) -> ZoneInfo | timezone:
    """The user's time zone, or UTC when the saved name isn't a known zone."""
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return timezone.utc


def when(dt: datetime, now: datetime, tz: str) -> str:
    """Relative time under a day ("4 min ago"), absolute after ("Oct 8, 9:42 AM").

    `dt` and `now` are timezone-aware. The absolute form is in `tz` and gains the
    year ("Oct 8, 2025, 9:42 AM") when it isn't `now`'s year.
    """
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"

    zone = _zone(tz)
    local = dt.astimezone(zone)
    # Built by hand: strftime flags for unpadded numbers (%-d, %#d) differ by platform.
    year = f", {local.year}" if local.year != now.astimezone(zone).year else ""
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{_MONTHS[local.month - 1]} {local.day}{year}, {hour}:{local.minute:02d} {meridiem}"


def hours_value(minutes: int | None) -> str:
    """Playtime in hours with one decimal and a thousands separator ("1,099.9")."""
    return f"{(minutes or 0) / 60:,.1f}"


def hours(minutes: int | None) -> str:
    """Playtime in hours with its unit ("1,099.9 h")."""
    return f"{hours_value(minutes)} h"


def lane_label(lane: str) -> str:
    key = getattr(lane, "value", lane)
    return LANE_LABELS.get(key, key)


@pass_context
def _when_filter(context, dt: datetime) -> str:
    return when(dt, context["now"], context["tz"])


templates.env.filters["when"] = _when_filter
templates.env.filters["hours"] = hours
templates.env.filters["hours_value"] = hours_value
templates.env.filters["lane_label"] = lane_label
templates.env.globals["artwork_url"] = artwork_url


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    """Render a template with the shell's context.

    Adds `theme` (from the cookie; anything but "light" is dark), `nav` (the current
    section: "home", "library", "reviews", "profiles", "recommendations", "research" or
    "settings"; None by default),
    `tz` (the timezone setting, UTC by default) and `now` (from the app clock). Values in
    `ctx` win.
    """
    app = request.app
    with app.state.session_factory() as db:
        tz = get_setting(db, "timezone", "UTC") or "UTC"
    context = {
        "theme": "light" if request.cookies.get("theme") == "light" else "dark",
        "nav": None,
        "tz": tz,
        "now": app.state.services.clock(),
    }
    context.update(ctx)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def parse_id(value: object) -> int | None:
    """A database id from posted or typed text: a whole number from 1 to 2**63 - 1.

    Surrounding whitespace is ignored. Anything else is None: not text, blank, signs,
    decimals, digits other than 0-9, zero, or a number the database could not hold
    (including text so long it would not be worth converting). Casting ids with `int()`
    instead lets a huge one through to SQLite, which raises, and a request answers 500.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _ID.fullmatch(text):
        return None
    number = int(text)
    return number if 1 <= number <= MAX_ID else None


def is_htmx(request: Request) -> bool:
    """Whether htmx made the request, so the route can answer with just a fragment."""
    return request.headers.get("hx-request") == "true"


def typed(form: FormData, name: str) -> str:
    """One field's text with the whitespace a paste leaves around it removed.

    A missing field, or a file, is empty text.
    """
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


async def read_form(request: Request) -> FormData:
    """The posted form. Read here so the route itself can be a plain function.

    A route that blocks, such as one calling Steam or an AI provider, has to run in a
    worker thread; an `async def` route would hold up the whole server.
    """
    return await request.form()


def redirect(request: Request, url: str) -> Response:
    """Send the browser to `url`: 303 for a normal request, HX-Redirect for htmx.

    An htmx request would otherwise follow a 303 and swap the target page into a
    fragment; HX-Redirect makes it load the whole page instead.
    """
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)
