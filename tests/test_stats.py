"""The numbers on Home: what the library and the reviews add up to."""

import html
import re

from app.models import Game
from app.services.reviews import Lane, ReviewForm, publish, save_draft
from app.services.stats import library_stats
from conftest import FIXED_NOW


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


def stats_strip(text: str) -> str:
    found = re.search(r'<section[^>]*class="home-stats".*?</section>', text, re.S)
    assert found, "no stats strip"
    return html.unescape(found[0])


def tile(strip: str, label: str) -> str:
    """The text of one stat tile, tags removed and spaces collapsed."""
    found = re.search(
        rf'<li class="home-stat[^"]*">(?:(?!</li>).)*?{re.escape(label)}.*?</li>',
        strip,
        re.S,
    )
    assert found, f"no tile for {label}"
    return " ".join(re.sub(r"<[^>]+>", " ", found[0]).split())


# --- the service ----------------------------------------------------------------


def test_stats_of_an_empty_library(db):
    stats = library_stats(db)
    assert (stats.games_owned, stats.games_reviewed, stats.reviews_published) == (
        0,
        0,
        0,
    )
    assert (stats.play_minutes, stats.played_games, stats.rated_reviews) == (0, 0, 0)
    assert stats.average_rating is None
    assert stats.most_played is None
    assert stats.is_empty


def test_stats_count_games_reviews_and_play_time(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=6), FIXED_NOW)
    save_draft(db, 20, Lane.SOLO, ReviewForm(rating=2, text="wip"), FIXED_NOW)
    stats = library_stats(db)
    assert stats.games_owned == 2
    # Two lane reviews of one game. A draft is not a review yet.
    assert (stats.games_reviewed, stats.reviews_published) == (1, 2)
    assert (stats.average_rating, stats.rated_reviews) == (7.0, 2)
    assert (stats.play_minutes, stats.played_games) == (720, 2)
    assert stats.most_played.name == "Sample Quest"
    assert not stats.is_empty


def test_average_rating_uses_each_reviews_latest_version(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=4), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=9), FIXED_NOW)
    stats = library_stats(db)
    assert (stats.reviews_published, stats.average_rating, stats.rated_reviews) == (
        1,
        9.0,
        1,
    )


def test_a_review_without_a_rating_counts_but_is_not_averaged(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="Fun with friends."), FIXED_NOW)
    stats = library_stats(db)
    assert (stats.games_reviewed, stats.reviews_published) == (1, 1)
    assert (stats.average_rating, stats.rated_reviews) == (None, 0)


def test_play_time_counts_only_owned_games(db, games):
    add_game(
        db,
        30,
        "Borrowed Game",
        owned=False,
        playtime_minutes=900,
        source="store_search",
    )
    add_game(db, 40, "Never Started", playtime_minutes=0)
    add_game(db, 50, "No Playtime Yet", playtime_minutes=None)
    stats = library_stats(db)
    assert stats.games_owned == 4
    assert (stats.play_minutes, stats.played_games) == (720, 2)
    assert stats.most_played.name == "Sample Quest"


def test_a_review_of_a_game_you_dont_own_still_counts(db):
    add_game(
        db,
        30,
        "Borrowed Game",
        owned=False,
        playtime_minutes=None,
        source="store_search",
    )
    publish(db, 30, Lane.TWO, ReviewForm(rating=7), FIXED_NOW)
    stats = library_stats(db)
    assert (stats.games_owned, stats.games_reviewed, stats.average_rating) == (
        0,
        1,
        7.0,
    )
    assert not stats.is_empty


def test_most_played_ties_go_to_the_name_first_alphabetically(db):
    add_game(db, 30, "Zeta Run", playtime_minutes=300)
    add_game(db, 40, "alpha Run", playtime_minutes=300)
    assert library_stats(db).most_played.name == "alpha Run"


# --- Home -----------------------------------------------------------------------


def test_home_shows_the_stats(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8), FIXED_NOW)
    publish(db, 20, Lane.SOLO, ReviewForm(rating=7), FIXED_NOW)
    strip = stats_strip(onboarded_client.get("/").text)
    assert tile(strip, "Games reviewed") == "Games reviewed 2 2 reviews published"
    assert tile(strip, "Play time") == "Play time 12 h across 2 games"
    assert tile(strip, "Average rating") == "Average rating 7.5 / 10 from 2 ratings"
    assert tile(strip, "Most played") == "Most played Sample Quest 10.0 h"


def test_home_stats_use_singular_words_for_one(onboarded_client, db):
    add_game(db, 30, "Only Game", playtime_minutes=90)
    publish(db, 30, Lane.SOLO, ReviewForm(rating=6), FIXED_NOW)
    strip = stats_strip(onboarded_client.get("/").text)
    assert tile(strip, "Games reviewed") == "Games reviewed 1 1 review published"
    assert tile(strip, "Play time") == "Play time 2 h across 1 game"
    assert tile(strip, "Average rating") == "Average rating 6.0 / 10 from 1 rating"


def test_home_stats_before_any_review(onboarded_client, games):
    strip = stats_strip(onboarded_client.get("/").text)
    assert tile(strip, "Games reviewed") == "Games reviewed 0 No reviews published yet"
    assert (
        tile(strip, "Average rating")
        == "Average rating Not rated yet Add a rating to a review"
    )


def test_home_stats_leave_out_most_played_with_no_play_time(onboarded_client, db):
    add_game(db, 30, "Never Started", playtime_minutes=0)
    strip = stats_strip(onboarded_client.get("/").text)
    assert "Most played" not in strip
    assert tile(strip, "Play time") == "Play time 0 h across 0 games"


def test_home_has_no_stats_with_nothing_to_count(onboarded_client):
    assert 'class="home-stats"' not in onboarded_client.get("/").text


def test_home_stats_escape_the_game_name(onboarded_client, db):
    add_game(db, 30, "<b>Loud</b> & Proud", playtime_minutes=90)
    text = onboarded_client.get("/").text
    assert "<b>Loud</b>" not in text
    assert "Most played &lt;b&gt;Loud&lt;/b&gt; &amp; Proud" in " ".join(
        re.sub(r"<[^>]+>", " ", text).split()
    )


def test_home_stats_sit_above_the_cards(onboarded_client, db, games):
    from app.services.library import set_currently_playing

    set_currently_playing(db, 10, True, FIXED_NOW)
    text = onboarded_client.get("/").text
    assert text.index('class="home-stats"') < text.index('id="playing-title"')
