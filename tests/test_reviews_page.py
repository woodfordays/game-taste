"""The Reviews page: the table of every review with its filters, and a lane's version history."""

import html
import re
from datetime import timedelta

from markupsafe import escape
from sqlalchemy import event

from app.models import Game
from app.services.reviews import Lane, ReviewForm, publish, save_draft
from conftest import FIXED_NOW

LATER = FIXED_NOW + timedelta(hours=1)


def add_game(db, appid: int, name: str) -> Game:
    game = Game(
        appid=appid,
        name=name,
        owned=True,
        playtime_minutes=60,
        last_played_at=None,
        source="steam_library",
        created_at=FIXED_NOW,
    )
    db.add(game)
    db.commit()
    return game


def row_ids(text: str) -> list[str]:
    """The table's rows in order, as "<appid>-<lane>"."""
    return re.findall(r'<tr[^>]*id="review-(\d+-\w+)"', text)


def row(text: str, appid: int, lane: str) -> str:
    found = re.search(rf'<tr[^>]*id="review-{appid}-{lane}".*?</tr>', text, re.S)
    assert found, f"no row for {appid} {lane}"
    return found[0]


def cells(markup: str) -> list[str]:
    """The visible text of each cell of a row."""
    return [
        " ".join(html.unescape(re.sub(r"<[^>]+>", " ", td)).split())
        for td in re.findall(r"<td[^>]*>(.*?)</td>", markup, re.S)
    ]


def options(text: str, name: str) -> list[tuple[str, bool, str]]:
    """A select's options as (value, selected, label)."""
    found = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', text, re.S)
    assert found, f"no select {name}"
    return [
        (value, bool(selected), label)
        for value, selected, label in re.findall(
            r'<option value="([^"]*)"( selected)?>([^<]*)</option>', found[1]
        )
    ]


def queries(app, client, url: str) -> int:
    """How many SQL statements serving `url` takes."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        assert client.get(url).status_code == 200
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)
    return len(seen)


# --- the brief's tests ----------------------------------------------------------


def test_reviews_list_and_filters(onboarded_client, db, games):
    publish(
        db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked", text="a"), FIXED_NOW
    )
    publish(
        db, 10, Lane.TWO, ReviewForm(rating=4, verdict="mixed", text="b"), FIXED_NOW
    )
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="c"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    assert text.count("Sample Quest") == 2 and "Draft" in text
    assert "Test Tactics" not in onboarded_client.get("/reviews?verdict=liked").text
    assert onboarded_client.get("/reviews?lane=solo").text.count("Sample Quest") == 1


def test_history_newest_first(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="old words"), FIXED_NOW)
    publish(
        db, 10, Lane.SOLO, ReviewForm(text="new words"), FIXED_NOW + timedelta(hours=1)
    )
    text = onboarded_client.get("/games/10/review/solo/history").text
    assert text.index("new words") < text.index("old words")


# --- the table ------------------------------------------------------------------


def test_reviews_is_the_current_page_in_the_nav(onboarded_client, games):
    text = onboarded_client.get("/reviews").text
    assert re.search(r'href="/reviews"[^>]*aria-current="page"', text)


def test_reviews_needs_a_login(client):
    for url in ("/reviews", "/games/10/review/solo/history"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and "reviews" not in r.headers["location"]


def test_table_columns(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="a"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    headers = re.findall(r'<th[^>]*scope="col"[^>]*>(.*?)</th>', text, re.S)
    assert [h.strip() for h in headers] == [
        "Game",
        "Lane",
        "Rating",
        "Verdict",
        "State",
        "Updated",
    ]
    assert re.search(r"<caption[^>]*>[^<]+</caption>", text)


def test_a_published_row(onboarded_client, db, games):
    publish(
        db,
        10,
        Lane.TWO,
        ReviewForm(rating=8, verdict="liked", text="a"),
        FIXED_NOW - timedelta(days=3),
    )
    one = row(onboarded_client.get("/reviews").text, 10, "two")
    assert cells(one) == [
        "Sample Quest",
        "Two players",
        "8 / 10",
        "Liked",
        "Published History",
        "Jan 12, 12:00 PM",
    ]


def test_the_three_states(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5, text="a"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=6, text="b"), FIXED_NOW)
    save_draft(db, 10, Lane.TWO, ReviewForm(rating=7, text="b2"), LATER)
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="c"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    assert cells(row(text, 10, "solo"))[4] == "Published History"
    assert cells(row(text, 10, "two"))[4] == "Published, draft saved History"
    assert cells(row(text, 20, "solo"))[4] == "Draft"


def test_a_draft_only_row_shows_what_is_not_published(onboarded_client, db, games):
    save_draft(db, 20, Lane.SOLO, ReviewForm(rating=9, text="c"), FIXED_NOW)
    one = row(onboarded_client.get("/reviews").text, 20, "solo")
    assert cells(one) == [
        "Test Tactics",
        "Solo",
        "Not rated",
        "Not set",
        "Draft",
        "just now",
    ]
    assert "/history" not in one


def test_a_published_row_without_a_rating_or_verdict(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="only words"), FIXED_NOW)
    one = row(onboarded_client.get("/reviews").text, 10, "solo")
    assert cells(one)[2:4] == ["Not rated", "Not set"]


def test_updated_is_when_the_draft_was_saved_if_newer(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW - timedelta(days=3))
    save_draft(
        db, 10, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW - timedelta(minutes=5)
    )
    one = row(onboarded_client.get("/reviews").text, 10, "solo")
    assert cells(one)[5] == "5 min ago"


def test_rows_are_newest_first(onboarded_client, db, games):
    add_game(db, 30, "Demo Dungeon")
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW - timedelta(days=3))
    publish(db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW - timedelta(days=1))
    save_draft(db, 30, Lane.TWO, ReviewForm(text="c"), FIXED_NOW - timedelta(days=2))
    publish(db, 10, Lane.TWO, ReviewForm(text="d"), FIXED_NOW - timedelta(days=4))
    text = onboarded_client.get("/reviews").text
    assert row_ids(text) == ["20-solo", "30-two", "10-solo", "10-two"]


def test_a_row_opens_the_dialog_for_its_lane(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(text="b"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    for lane in ("solo", "three_four"):
        one = row(text, 10, lane)
        opener = re.search(r"<button[^>]*>Sample Quest</button>", one)
        assert opener, "the game name is a real button"
        assert f'hx-get="/games/10/review?lane={lane}"' in opener[0]
        assert 'hx-target="#dialog"' in opener[0] and 'hx-swap="innerHTML"' in opener[0]
        assert 'type="button"' in opener[0]
        # The lane is read out with the button, since the name alone repeats down the table.
        described_by = re.search(r'aria-describedby="([^"]+)"', opener[0])
        assert described_by
        assert re.search(rf'<td[^>]*id="{described_by[1]}"', one)
    assert "onclick" not in text and "<tr onclick" not in text


def test_history_link_only_for_a_row_with_a_published_version(
    onboarded_client, db, games
):
    publish(db, 10, Lane.TWO, ReviewForm(text="a"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    link = re.search(
        r'<a[^>]*href="/games/10/review/two/history"[^>]*>', row(text, 10, "two")
    )
    assert link
    # Like the game's button, it is told which game it belongs to.
    assert "aria-describedby" in link[0]
    assert "/history" not in row(text, 10, "solo")


def test_game_names_are_escaped(onboarded_client, db, games):
    add_game(db, 30, '<b>Bold</b> & "Co"')
    publish(db, 30, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    assert "<b>Bold</b>" not in text
    assert str(escape('<b>Bold</b> & "Co"')) in text


def test_empty_state(onboarded_client, games):
    text = onboarded_client.get("/reviews").text
    assert (
        "No reviews yet. Reviews appear here after you save a draft or publish one."
        in text
    )
    assert "<td" in text and row_ids(text) == []


def test_games_without_reviews_are_not_listed(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    assert "Test Tactics" not in onboarded_client.get("/reviews").text


def test_the_count(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    assert "1 review<" in onboarded_client.get("/reviews").text
    publish(db, 10, Lane.TWO, ReviewForm(text="b"), FIXED_NOW)
    assert "2 reviews<" in onboarded_client.get("/reviews").text


# --- filters --------------------------------------------------------------------


def test_filter_options(onboarded_client, games):
    text = onboarded_client.get("/reviews").text
    assert options(text, "lane") == [
        ("", True, "All lanes"),
        ("solo", False, "Solo"),
        ("two", False, "Two players"),
        ("three_four", False, "Three to four"),
        ("five_plus", False, "Five or more"),
    ]
    assert options(text, "verdict") == [
        ("", True, "All verdicts"),
        ("loved", False, "Loved"),
        ("liked", False, "Liked"),
        ("mixed", False, "Mixed"),
        ("disliked", False, "Disliked"),
    ]


def test_filters_are_shown_back_selected(onboarded_client, games):
    text = onboarded_client.get("/reviews?lane=two&verdict=mixed").text
    assert [v for v, selected, _ in options(text, "lane") if selected] == ["two"]
    assert [v for v, selected, _ in options(text, "verdict") if selected] == ["mixed"]


def test_filter_form_reloads_the_table(onboarded_client, games):
    text = onboarded_client.get("/reviews").text
    form = re.search(r'<form[^>]*id="reviews-filters"[^>]*>', text)
    assert form
    assert 'action="/reviews"' in form[0] and 'method="get"' in form[0]
    assert 'hx-get="/reviews"' in form[0]
    assert 'hx-target="#reviews-list"' in form[0] and 'hx-swap="outerHTML"' in form[0]
    assert 'hx-push-url="true"' in form[0]
    # Back and Forward ask the server for the page (which reads the filters from the URL),
    # because htmx's snapshot of it would show the selects as they were first rendered.
    assert 'hx-history="false"' in form[0]
    assert re.search(r"<label[^>]*for=\"reviews-lane\"[^>]*>\s*Lane", text)
    assert re.search(r"<label[^>]*for=\"reviews-verdict\"[^>]*>\s*Verdict", text)


def test_filtering_by_lane_and_verdict(onboarded_client, db, games):
    publish(
        db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked", text="a"), FIXED_NOW
    )
    publish(
        db, 10, Lane.TWO, ReviewForm(rating=4, verdict="mixed", text="b"), FIXED_NOW
    )
    publish(
        db, 20, Lane.TWO, ReviewForm(rating=9, verdict="loved", text="c"), FIXED_NOW
    )
    get = onboarded_client.get
    assert sorted(row_ids(get("/reviews?lane=two").text)) == ["10-two", "20-two"]
    assert row_ids(get("/reviews?verdict=mixed").text) == ["10-two"]
    assert row_ids(get("/reviews?lane=two&verdict=loved").text) == ["20-two"]
    assert row_ids(get("/reviews?lane=solo&verdict=loved").text) == []


def test_unknown_filter_values_mean_all(onboarded_client, db, games):
    publish(
        db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked", text="a"), FIXED_NOW
    )
    save_draft(db, 20, Lane.TWO, ReviewForm(text="c"), FIXED_NOW)
    everything = row_ids(onboarded_client.get("/reviews").text)
    assert len(everything) == 2
    for query in (
        "lane=bogus",
        "verdict=bogus",
        "lane=bogus&verdict=bogus",
        "lane=&verdict=",
        "lane=SOLO",
        "lane=%00&verdict=%FF",
    ):
        r = onboarded_client.get(f"/reviews?{query}")
        assert r.status_code == 200, query
        assert row_ids(r.text) == everything, query
    text = onboarded_client.get("/reviews?lane=bogus&verdict=bogus").text
    assert [v for v, selected, _ in options(text, "lane") if selected] == [""]
    assert [v for v, selected, _ in options(text, "verdict") if selected] == [""]


def test_a_filter_with_no_match_says_so(onboarded_client, db, games):
    publish(
        db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked", text="a"), FIXED_NOW
    )
    text = onboarded_client.get("/reviews?lane=five_plus").text
    assert "No reviews match these filters. Pick a different lane or verdict." in text
    assert "No reviews yet" not in text and row_ids(text) == []


# --- htmx -----------------------------------------------------------------------


def test_htmx_gets_just_the_list(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    r = onboarded_client.get("/reviews?lane=solo", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="reviews-list"' in r.text
    assert "<html" not in r.text and 'id="reviews-filters"' not in r.text
    assert row_ids(r.text) == ["10-solo"]


def test_a_history_restore_gets_the_whole_page(onboarded_client, db, games):
    """htmx refetches a pushed URL it has no snapshot for, and expects a whole page."""
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    r = onboarded_client.get(
        "/reviews?lane=solo",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert "<html" in r.text and 'id="reviews-filters"' in r.text


def test_the_answer_depends_on_who_asks(onboarded_client, games):
    """A fragment and a page share a URL, so a cache must not mix them up."""
    for headers in ({}, {"HX-Request": "true"}):
        r = onboarded_client.get("/reviews", headers=headers)
        assert "hx-request" in r.headers["vary"].lower()


def test_the_list_refreshes_when_a_dialog_saves(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    publish(db, 20, Lane.TWO, ReviewForm(text="b"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(text="c"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    watcher = re.search(r"<[^>]*hx-trigger=\"card-updated[^>]*>", text)
    assert watcher, "nothing refreshes the list"
    # One event per game in the list, each game once.
    assert re.findall(r"card-updated-(\d+) from:body", watcher[0]) == ["10", "20"]
    assert 'hx-get="/reviews"' in watcher[0]
    assert 'hx-include="#reviews-filters"' in watcher[0]
    assert 'hx-target="#reviews-list"' in watcher[0]
    assert 'hx-swap="outerHTML"' in watcher[0]
    # It sits inside the list, so the list's own swap renews it, and it is not on screen.
    inside = re.search(r'<div id="reviews-list".*', text, re.S)[0]
    assert watcher[0] in inside and " hidden" in watcher[0]
    # A refresh is not a new page in the browser's history.
    assert "hx-push-url" not in watcher[0]


def test_nothing_to_refresh_when_the_list_is_empty(onboarded_client, games):
    assert "card-updated" not in onboarded_client.get("/reviews").text


def test_the_dialog_buttons_do_not_inherit_the_lists_attributes(
    onboarded_client, db, games
):
    """htmx hands hx-include and hx-target down to children; the opener must not pick up the
    filters (they would be added to its query string, which already names the lane)."""
    publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    assert re.search(r'<div id="reviews-list">', text), "the list sets nothing for htmx"
    assert 'class="em-table-wrap">' in text
    assert "hx-include" not in row(text, 10, "solo")


def test_a_fixed_number_of_queries(app, onboarded_client, db, games):
    publish(
        db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked", text="a"), FIXED_NOW
    )
    one = queries(app, onboarded_client, "/reviews")
    add_game(db, 30, "Demo Dungeon")
    for appid in (10, 20, 30):
        for lane in Lane:
            publish(db, appid, lane, ReviewForm(rating=5, text="x"), LATER)
            save_draft(
                db, appid, lane, ReviewForm(text="y"), LATER + timedelta(hours=1)
            )
    many = queries(app, onboarded_client, "/reviews")
    assert len(row_ids(onboarded_client.get("/reviews").text)) == 12
    assert one == many


# --- history --------------------------------------------------------------------


def test_history_page_frame(onboarded_client, db, games):
    publish(db, 10, Lane.TWO, ReviewForm(text="a"), FIXED_NOW)
    text = onboarded_client.get("/games/10/review/two/history").text
    assert re.search(r"<h1[^>]*>\s*Sample Quest · Two players\s*</h1>", text)
    assert "<title>Sample Quest · Two players" in text
    assert re.search(r'<a[^>]*href="/reviews"[^>]*>[^<]*Back to reviews', text)
    # The side nav is there, with Reviews as the current page.
    assert 'class="em-sidenav' in text
    assert re.search(r'href="/reviews"[^>]*aria-current="page"', text)


def test_history_lists_each_version_with_its_number_and_time(
    onboarded_client, db, games
):
    publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=6, text="first"),
        FIXED_NOW - timedelta(days=3),
    )
    publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=9, text="second"),
        FIXED_NOW - timedelta(minutes=5),
    )
    publish(db, 10, Lane.SOLO, ReviewForm(rating=7, text="third"), FIXED_NOW)
    text = onboarded_client.get("/games/10/review/solo/history").text
    versions = re.findall(r"Version (\d+)", text)
    assert versions == ["3", "2", "1"]
    assert text.index("third") < text.index("second") < text.index("first")
    assert "just now" in text and "5 min ago" in text and "Jan 12, 12:00 PM" in text
    assert (
        text.index("just now")
        < text.index("5 min ago")
        < text.index("Jan 12, 12:00 PM")
    )


def test_history_only_has_this_games_lane(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="solo words"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(text="two words"), FIXED_NOW)
    publish(db, 20, Lane.SOLO, ReviewForm(text="other game"), FIXED_NOW)
    text = onboarded_client.get("/games/10/review/solo/history").text
    assert "solo words" in text
    assert "two words" not in text and "other game" not in text


def test_history_rating_and_verdict(onboarded_client, db, games):
    publish(
        db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="loved", text="a"), FIXED_NOW
    )
    publish(db, 10, Lane.SOLO, ReviewForm(text="b"), LATER)
    text = onboarded_client.get("/games/10/review/solo/history").text
    newer, older = (
        re.split(r"(?=Version 1)", text, maxsplit=1)[0],
        text[text.index("Version 1") :],
    )
    assert "Not rated" in newer and "Loved" not in newer and "Verdict" not in newer
    assert "8 / 10" in older and "Not rated" not in older
    assert re.search(r"Verdict[^A-Za-z]*Loved", older)


def test_history_tags_are_chips(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="a", tags_raw="co op, #cozy"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(text="b"), LATER)
    text = onboarded_client.get("/games/10/review/solo/history").text
    older = text[text.index("Version 1") :]
    chips = re.findall(r'<span class="tag-chip[^"]*">([^<]*)</span>', older)
    assert chips == ["co_op", "cozy"]
    assert "tag-chip" not in text[: text.index("Version 1")]


def test_history_names_who_played_in_a_co_op_lane(onboarded_client, db, games, people):
    sam, alex, crew = people
    publish(db, 10, Lane.TWO, ReviewForm(text="a", people=[sam.id]), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(text="b", named_group_id=crew.id), LATER)
    text = onboarded_client.get("/games/10/review/two/history").text
    newer = text[: text.index("Version 1")]
    older = text[text.index("Version 1") :]
    # Each version keeps who played at the time it was published.
    assert "Sam" in older and "Alex" not in older
    assert "Sam" in newer and "Alex" in newer
    assert re.search(r"Played with[^A-Za-z]*Sam", older)


def test_history_has_no_who_played_for_solo_or_for_nobody(
    onboarded_client, db, games, people
):
    publish(db, 10, Lane.SOLO, ReviewForm(text="a", people=[people[0].id]), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(text="b"), FIXED_NOW)
    assert (
        "Played with" not in onboarded_client.get("/games/10/review/solo/history").text
    )
    assert (
        "Played with" not in onboarded_client.get("/games/10/review/two/history").text
    )


def test_history_shows_group_text(onboarded_client, db, games):
    publish(
        db,
        10,
        Lane.THREE_FOUR,
        ReviewForm(text="mine", group_text="we <b>loved</b> it"),
        FIXED_NOW,
    )
    text = onboarded_client.get("/games/10/review/three_four/history").text
    assert "How it went for the group" in text
    assert '<p class="review-text' in text
    assert "we &lt;b&gt;loved&lt;/b&gt; it" in text and "<b>loved</b>" not in text


def test_history_text_is_escaped_and_exactly_as_written(onboarded_client, db, games):
    words = '  <script>alert("x")</script> & more\n\n  indented line\r\nlast  '
    publish(db, 10, Lane.SOLO, ReviewForm(text=words), FIXED_NOW)
    raw = onboarded_client.get("/games/10/review/solo/history").text
    assert "<script>alert" not in raw
    # Byte for byte: the spaces, both kinds of line break and the markup, escaped.
    assert re.search(
        rf'<p class="review-text[^"]*">{re.escape(str(escape(words)))}</p>', raw
    )


def test_history_text_keeps_emoji(onboarded_client, db, games):
    words = "great \U0001f3ae night"
    publish(db, 10, Lane.SOLO, ReviewForm(text=words), FIXED_NOW)
    assert f">{words}</" in onboarded_client.get("/games/10/review/solo/history").text


def test_history_for_a_lane_with_nothing_published(onboarded_client, db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="not published yet"), FIXED_NOW)
    for lane in ("solo", "five_plus"):
        r = onboarded_client.get(f"/games/10/review/{lane}/history")
        assert r.status_code == 200
        assert "No published versions yet." in r.text
        assert "not published yet" not in r.text and "Version" not in r.text


def test_history_unknown_game_or_lane_is_not_found(onboarded_client, games):
    assert onboarded_client.get("/games/999/review/solo/history").status_code == 404
    assert onboarded_client.get("/games/10/review/bogus/history").status_code == 404


def test_history_has_a_fixed_number_of_queries(
    app, onboarded_client, db, games, people
):
    publish(db, 10, Lane.TWO, ReviewForm(text="a", people=[people[0].id]), FIXED_NOW)
    one = queries(app, onboarded_client, "/games/10/review/two/history")
    for n in range(6):
        publish(
            db,
            10,
            Lane.TWO,
            ReviewForm(text=f"v{n}", people=[people[0].id, people[1].id]),
            LATER + timedelta(minutes=n),
        )
    assert one == queries(app, onboarded_client, "/games/10/review/two/history")
