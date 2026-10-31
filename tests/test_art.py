"""Game artwork: every place that shows it gets a frame with a placeholder for missing art."""

import re
from pathlib import Path

from app.services.library import artwork_url, set_currently_playing
from conftest import FIXED_NOW

CSS = (Path(__file__).parent.parent / "app" / "static" / "app.css").read_text(
    encoding="utf-8"
)
HANDLER = "this.parentNode.classList.add('is-missing'); this.remove()"


def art_frames(text: str) -> list[str]:
    return re.findall(r'<span class="game-art [^"]*">.*?</span>', text, re.S)


def check_frame(frame: str, size_class: str, appid: int) -> None:
    assert f'class="game-art {size_class}"' in frame
    assert f'src="{artwork_url(appid)}"' in frame
    assert f'onerror="{HANDLER}"' in frame
    # The placeholder is drawn in the frame, hidden from assistive tech like the image.
    assert re.search(
        r'<svg class="game-art__placeholder"[^>]*aria-hidden="true"', frame
    )
    assert 'alt=""' in frame


def test_home_cards_have_an_art_frame(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    frames = art_frames(onboarded_client.get("/").text)
    assert len(frames) == 1
    check_frame(frames[0], "game-card__art", 10)


def test_recently_played_rows_have_an_art_frame(onboarded_client, games):
    frames = art_frames(onboarded_client.get("/").text)
    assert len(frames) == 2
    check_frame(frames[0], "library-row__art", 10)


def test_library_rows_have_an_art_frame(onboarded_client, games):
    frames = art_frames(onboarded_client.get("/library").text)
    assert len(frames) == 2
    for frame in frames:
        assert 'class="game-art library-row__art"' in frame
    check_frame(frames[0], "library-row__art", 10)


def test_no_image_is_left_without_a_frame(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    for url in ("/", "/library"):
        text = onboarded_client.get(url).text
        assert text.count("<img ") == len(art_frames(text))


def test_the_handler_carries_no_game_text(onboarded_client, db):
    """The handler is the same fixed script for every game, so no name or id can break out of it."""
    from app.models import Game

    db.add(
        Game(
            appid=30,
            name="\"'); alert(1); ('",
            owned=True,
            playtime_minutes=60,
            last_played_at=FIXED_NOW,
            source="steam_library",
            created_at=FIXED_NOW,
        )
    )
    db.commit()
    text = onboarded_client.get("/library").text
    handlers = re.findall(r'onerror="([^"]*)"', text)
    assert handlers == [HANDLER]


def test_the_placeholder_shows_only_when_the_art_is_missing():
    assert re.search(r"\.game-art__placeholder\s*\{\s*display:\s*none;", CSS)
    assert re.search(
        r"\.game-art\.is-missing \.game-art__placeholder\s*\{\s*display:\s*block;", CSS
    )
