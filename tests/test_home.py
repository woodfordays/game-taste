"""Home: the Currently playing cards, how one refreshes, and the empty state."""

import html
import re
from datetime import timedelta
from pathlib import Path

from app.models import Game
from app.services.library import add_store_game, artwork_url, set_currently_playing
from app.services.reviews import Lane, ReviewForm, history, publish, save_draft
from conftest import FIXED_NOW


def page(client, url: str = "/") -> str:
    """The page as the user reads it: character references decoded."""
    return html.unescape(client.get(url).text)


def card(text: str, appid: int) -> str:
    found = re.search(rf'<li[^>]*id="card-{appid}".*?</li>', text, re.S)
    assert found, f"no card for app id {appid}"
    return found[0]


def card_ids(text: str) -> list[int]:
    return [int(n) for n in re.findall(r'<li[^>]*id="card-(\d+)"', text)]


def add_game(db, appid: int, name: str, **fields) -> Game:
    values = {
        "owned": True,
        "playtime_minutes": 60,
        "last_played_at": None,
        "source": "steam_library",
        "created_at": FIXED_NOW,
    }
    game = Game(appid=appid, name=name, **{**values, **fields})
    db.add(game)
    db.commit()
    return game


# --- the brief's tests ----------------------------------------------------------


def test_home_cards(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=8, tags_raw="co op"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="wip"), FIXED_NOW)
    text = onboarded_client.get("/").text
    assert (
        "Sample Quest" in text
        and "8 / 10" in text
        and "Two players" in text
        and "co_op" in text
        and "Draft saved" in text
    )


def test_home_empty_state_lists_recent_games(onboarded_client, games):
    text = onboarded_client.get("/").text
    assert (
        "Tell me about a game you played recently" in text and "Write a review" in text
    )


# --- cards ----------------------------------------------------------------------


def test_home_is_the_current_page_in_the_nav(onboarded_client, games):
    text = onboarded_client.get("/").text
    assert re.search(r'href="/"[^>]*aria-current="page"', text)


def test_card_wiring(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    one = card(onboarded_client.get("/").text, 10)
    # The card refreshes itself when its review changes.
    assert 'hx-get="/games/10/card"' in one
    assert 'hx-trigger="card-updated-10 from:body"' in one
    assert 'hx-swap="outerHTML"' in one
    # Clicking it opens the dialog. The inherited outerHTML swap is overridden.
    opener = re.search(r"<button[^>]*hx-get=\"/games/10/review\"[^>]*>", one)
    assert opener
    assert 'hx-target="#dialog"' in opener[0] and 'hx-swap="innerHTML"' in opener[0]
    assert f'src="{artwork_url(10)}"' in one


def test_card_without_a_review(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    one = card(onboarded_client.get("/").text, 10)
    assert "Not rated" in one
    assert "/ 10" not in one
    assert "Draft saved" not in one
    for label in ("Solo", "Two players", "Three to four", "Five or more"):
        assert label not in one


def test_card_with_text_only_review_is_not_rated_but_has_a_lane(
    onboarded_client, db, games
):
    set_currently_playing(db, 10, True, FIXED_NOW)
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(text="Words only"), FIXED_NOW)
    one = card(onboarded_client.get("/").text, 10)
    assert "Not rated" in one and "Three to four" in one


def test_card_with_only_a_draft(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=6, text="wip"), FIXED_NOW)
    one = card(onboarded_client.get("/").text, 10)
    assert "Draft saved" in one and "Not rated" in one


def test_card_shows_the_latest_published_lane(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=9), FIXED_NOW + timedelta(minutes=1))
    one = card(onboarded_client.get("/").text, 10)
    assert "9 / 10" in one and "Two players" in one and "5 / 10" not in one


def test_only_playing_games_get_cards(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    text = page(onboarded_client)
    assert card_ids(text) == [10]
    assert "Test Tactics" not in text
    assert "Tell me about a game you played recently" not in text


def test_cards_are_most_recently_played_first(onboarded_client, db, games):
    add_game(db, 30, "Demo Dungeon", last_played_at=FIXED_NOW - timedelta(hours=2))
    add_game(db, 40, "Never Played")
    for appid in (10, 20, 30, 40):
        set_currently_playing(db, appid, True, FIXED_NOW)
    # Sample Quest 1 day ago, Test Tactics 2 days ago, Demo Dungeon 2 hours ago.
    assert card_ids(page(onboarded_client)) == [30, 10, 20, 40]


def test_unowned_game_can_be_a_card(onboarded_client, db):
    add_store_game(db, 50, "Borrowed Game", FIXED_NOW)
    set_currently_playing(db, 50, True, FIXED_NOW)
    assert card_ids(page(onboarded_client)) == [50]


def test_card_escapes_the_name_and_tags(onboarded_client, db):
    add_game(db, 60, "<b>Bold</b> & Co", last_played_at=FIXED_NOW)
    set_currently_playing(db, 60, True, FIXED_NOW)
    publish(db, 60, Lane.SOLO, ReviewForm(rating=3, tags_raw="<i>x</i>"), FIXED_NOW)
    raw = card(onboarded_client.get("/").text, 60)
    assert "<b>Bold</b>" not in raw and "&lt;b&gt;Bold&lt;/b&gt;" in raw
    assert "<i>x</i>" not in raw and "&lt;i&gt;x&lt;/i&gt;" in raw


# --- the card route -------------------------------------------------------------


def test_card_route_returns_the_card(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=7), FIXED_NOW)
    r = onboarded_client.get("/games/10/card")
    assert r.status_code == 200
    assert 'id="card-10"' in r.text and "7 / 10" in r.text
    assert "<html" not in r.text  # a fragment, not a page


def test_card_route_shows_a_new_publish(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    assert "Not rated" in onboarded_client.get("/games/10/card").text
    onboarded_client.post(
        "/games/10/review/solo", data={"action": "publish", "rating": "9"}
    )
    assert "9 / 10" in onboarded_client.get("/games/10/card").text


def test_card_route_is_empty_when_not_playing(onboarded_client, db, games):
    r = onboarded_client.get("/games/10/card")
    assert r.status_code == 200 and r.text == ""
    set_currently_playing(db, 10, True, FIXED_NOW)
    set_currently_playing(db, 10, False, FIXED_NOW)
    assert onboarded_client.get("/games/10/card").text == ""


def test_card_route_unknown_game(onboarded_client, games):
    assert onboarded_client.get("/games/999/card").status_code == 404


def test_card_route_keeps_reviews_when_game_stops_playing(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=7), FIXED_NOW)
    set_currently_playing(db, 10, False, FIXED_NOW)
    onboarded_client.get("/games/10/card")
    db.expire_all()
    assert len(history(db, 10, Lane.SOLO)) == 1


# --- the empty state ------------------------------------------------------------


def test_empty_state_text(onboarded_client, games):
    text = page(onboarded_client)
    assert "Tell me about a game you played recently" in text
    assert (
        "Games appear here when you switch on Currently playing in your library."
        in text
    )
    assert card_ids(text) == []


def test_empty_state_lists_the_five_most_recent(onboarded_client, db):
    for appid in range(1, 8):  # 1 is the oldest, 7 the newest
        add_game(
            db,
            appid,
            f"Game {appid}",
            last_played_at=FIXED_NOW - timedelta(days=10 - appid),
        )
    text = page(onboarded_client)
    assert text.count("Write a review") >= 5
    listed = re.findall(r'hx-get="/games/(\d+)/review"', text)
    assert listed == ["7", "6", "5", "4", "3"]
    assert "Game 2" not in text


def test_empty_state_buttons_open_the_dialog(onboarded_client, games):
    text = page(onboarded_client)
    button = re.search(r"<button[^>]*hx-get=\"/games/10/review\"[^>]*>", text)
    assert button and 'hx-target="#dialog"' in button[0]


def test_empty_state_skips_games_that_are_not_owned_or_never_played(
    onboarded_client, db
):
    add_game(db, 1, "Never Played")
    add_game(db, 2, "Borrowed", owned=False, last_played_at=FIXED_NOW)
    add_game(db, 3, "Played One", last_played_at=FIXED_NOW)
    text = page(onboarded_client)
    assert "Played One" in text
    assert "Never Played" not in text and "Borrowed" not in text


def test_empty_state_without_played_games_lists_nothing(onboarded_client, db):
    add_game(db, 1, "Never Played")
    text = page(onboarded_client)
    assert "Tell me about a game you played recently" in text
    assert "Write a review" not in text and "Never Played" not in text


def test_empty_state_with_no_games_at_all(onboarded_client):
    text = page(onboarded_client)
    assert "Tell me about a game you played recently" in text
    assert "Write a review" not in text


def test_empty_state_escapes_names(onboarded_client, db):
    add_game(db, 1, "<script>x</script>", last_played_at=FIXED_NOW)
    raw = onboarded_client.get("/").text
    assert "<script>x</script>" not in raw and "&lt;script&gt;x&lt;/script&gt;" in raw


# --- keyboard focus -------------------------------------------------------------


def css_rules(selector_part: str) -> list[tuple[str, str]]:
    """The (selector, declarations) pairs in app.css whose selector contains `selector_part`."""
    css = (Path(__file__).parent.parent / "app" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    return [(s.strip(), d) for s, d in rules if selector_part in s]


def test_card_focus_ring_is_on_the_card_not_clipped():
    # The card clips its overflow, so a ring on the stretched ::after, which fills the card
    # exactly, would be clipped away. This only checks the rules exist; seeing the ring is
    # a browser check.
    assert any("overflow: hidden" in d for s, d in css_rules(".game-card") if s == ".game-card")
    ring = [
        (s, d)
        for s, d in css_rules(".game-card")
        if ":has(" in s and "focus-visible" in s
    ]
    assert ring, "no card-level focus rule"
    declarations = ring[0][1]
    assert "box-shadow: var(--focus-ring)" in declarations
    assert "outline: 2px solid transparent" in declarations  # visible in forced-colors mode


def test_card_focus_rules_never_remove_the_outline():
    focus_rules = [(s, d) for s, d in css_rules(".game-card") if "focus-visible" in s]
    assert focus_rules
    for selector, declarations in focus_rules:
        assert not re.search(r"outline:\s*(none|0)\b", declarations), selector
    # The ::after fills the card, so a shadow there is the spot that gets clipped.
    assert not any("box-shadow" in d for s, d in focus_rules if s.endswith("::after"))
