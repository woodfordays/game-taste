"""Home: your numbers, a card for each game being played, and what to show before there is one."""

from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Game
from app.routes.reviews import AppId
from app.services.library import SORT_KEYS
from app.services.reviews import CardSummary, card_summary
from app.services.stats import library_stats
from app.web import render

router = APIRouter()

# With no cards, Home offers this many of the games played most recently.
RECENT_COUNT = 5


class HomeCard(NamedTuple):
    game: Game
    summary: CardSummary


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    """The "Currently playing" cards, most recently played first. With none, the empty
    state, which lists the owned games played most recently so a first review is one
    click away."""
    playing = sorted(
        db.scalars(select(Game).where(Game.currently_playing.is_(True))),
        key=SORT_KEYS["last_played"],
    )
    cards = [HomeCard(game, card_summary(db, game.appid)) for game in playing]
    recent = []
    if not cards:
        recent = list(
            db.scalars(
                select(Game)
                .where(Game.owned.is_(True), Game.last_played_at.is_not(None))
                .order_by(Game.last_played_at.desc(), Game.appid)
                .limit(RECENT_COUNT)
            )
        )
    return render(
        request,
        "home.html",
        nav="home",
        stats=library_stats(db),
        cards=cards,
        recent=recent,
    )


@router.get("/games/{appid}/card")
def game_card(appid: AppId, request: Request, db: Session = Depends(get_db)):
    """One game's card, to refresh it in place. Empty when the game is no longer being
    played, which makes the card disappear."""
    game = db.get(Game, appid)
    if game is None:
        raise HTTPException(status_code=404, detail="No such game.")
    if not game.currently_playing:
        return HTMLResponse("")
    return render(
        request, "partials/card.html", game=game, summary=card_summary(db, appid)
    )
