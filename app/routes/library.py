"""The Library page: the list of games, the "Currently playing" switch, re-sync and adding a game."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.models import Game
from app.services.game_facts import SOURCE_RESEARCH
from app.services.library import (
    SORT_KEYS,
    add_store_game,
    list_library,
    set_currently_playing,
    steam_credentials,
    sync_library,
)
from app.services.settings import get_setting
from app.services.steam import SteamClient, SteamError, StoreHit
from app.web import is_htmx, read_form, render

router = APIRouter()

DEFAULT_SORT = "hours"
# Steam app ids are unsigned 32-bit numbers.
MAX_APPID = 2**32 - 1

NOT_CONNECTED = "Connect Steam in Settings to import your library."
TERM_MISSING = "Enter a game name to search."
ADD_INCOMPLETE = (
    "Couldn't add that game. Search for it again and pick it from the results."
)


@dataclass
class Toast:
    """A note shown in the toast region. `message` is plain text, escaped when shown."""

    message: str
    tone: str = "success"  # or "danger"


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _sort(value: str) -> str:
    """`value` if it names a sort, else the default, so a stale link never errors."""
    return value if value in SORT_KEYS else DEFAULT_SORT


def _library_response(
    request: Request,
    db: Session,
    *,
    q: str = "",
    sort: str = DEFAULT_SORT,
    toast: Toast | None = None,
    added: StoreHit | None = None,
):
    """The Library page, or just its list when htmx will swap it in.

    Always status 200: htmx doesn't swap an error status, and a failure is shown as a
    toast. For htmx the toast rides along as an out-of-band swap into #toasts, and
    `added` (a search hit just added) turns its row in the open search results into
    "In your library".
    """
    games = list_library(db, q, sort)
    if is_htmx(request):
        return render(
            request,
            "partials/library_list.html",
            games=games,
            q=q,
            toast=toast,
            added=added,
            oob=True,
        )
    connected = steam_credentials(db, request.app.state.vault) is not None
    return render(
        request,
        "library.html",
        nav="library",
        games=games,
        q=q,
        sort=sort,
        connected=connected,
        toast=toast,
    )


@router.get("/library")
def library_page(
    request: Request,
    q: str = "",
    sort: str = DEFAULT_SORT,
    db: Session = Depends(get_db),
):
    return _library_response(request, db, q=q, sort=_sort(sort))


@router.post("/library/sync")
def library_sync(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    """Re-import the library from Steam. The filters ride along so the list keeps its view."""
    services = request.app.state.services
    credentials = steam_credentials(db, request.app.state.vault)
    if credentials is None:
        toast = Toast(NOT_CONNECTED, "danger")
    else:
        try:
            result = sync_library(
                db, SteamClient(services.steam_http), *credentials, services.clock()
            )
        except SteamError as failure:
            # Plain text, never markup: Steam's message can quote what was typed.
            toast = Toast(str(failure), "danger")
        else:
            toast = Toast(f"Library synced: {result.added} new, {result.total} total.")
    return _library_response(
        request,
        db,
        q=_text(form, "q"),
        sort=_sort(_text(form, "sort")),
        toast=toast,
    )


@router.post("/games/{appid}/playing")
def game_playing(
    appid: Annotated[int, Path(ge=1, le=MAX_APPID)],
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    """Switch "Currently playing". Answers with the game's row and tells the home cards."""
    state = _text(form, "playing")
    if state not in ("0", "1"):
        raise HTTPException(status_code=400, detail='"playing" must be 1 or 0.')
    game = db.get(Game, appid)
    if game is None:
        raise HTTPException(status_code=404, detail="No such game.")
    set_currently_playing(db, appid, state == "1", request.app.state.services.clock())
    response = render(request, "partials/library_row.html", game=game)
    response.headers["HX-Trigger"] = f"card-updated-{appid}"
    return response


@router.get("/library/search")
def library_search(request: Request, term: str = "", db: Session = Depends(get_db)):
    """Search the Steam store by name, for "Add a game". Always a fragment."""
    term = term.strip()
    context: dict = {"term": term, "hits": [], "known": set()}
    if not term:
        context["notice"] = TERM_MISSING
    else:
        region = get_setting(db, "store_region", "us") or "us"
        try:
            hits = SteamClient(request.app.state.services.steam_http).store_search(
                term, region
            )
        except SteamError as failure:
            context["error"] = str(failure)
        else:
            context["hits"] = hits
            # A game research brought in is not in the library until it is added.
            context["known"] = set(
                db.scalars(
                    select(Game.appid).where(
                        Game.appid.in_([h.appid for h in hits]),
                        Game.source != SOURCE_RESEARCH,
                    )
                )
            )
    return render(request, "partials/store_results.html", **context)


def _app_id(value: str) -> int | None:
    """The app id in `value`, or None if it isn't a whole number Steam could use."""
    try:
        appid = int(value)
    except ValueError:
        return None
    return appid if 0 < appid <= MAX_APPID else None


@router.post("/library/games")
def library_add_game(
    request: Request,
    form: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    """Add a game picked from the store search to the library."""
    appid = _app_id(_text(form, "appid"))
    name = _text(form, "name")
    added = None
    if appid is None or not name.strip():
        toast = Toast(ADD_INCOMPLETE, "danger")
    else:
        existing = db.get(Game, appid)
        known = existing is not None and existing.source != SOURCE_RESEARCH
        game = add_store_game(db, appid, name, request.app.state.services.clock())
        added = StoreHit(appid, name)
        toast = Toast(
            f"{game.name} is already in your library."
            if known
            else f"Added {game.name} to your library."
        )
    return _library_response(
        request,
        db,
        q=_text(form, "q"),
        sort=_sort(_text(form, "sort")),
        toast=toast,
        added=added,
    )
