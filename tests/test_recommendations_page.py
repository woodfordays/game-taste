"""The Recommendations page: a lane control, status tabs with counts, and a pick for each
entry that explains itself (the profile lines it fits, its main risk, where it was found and
its facts, each with the date it was checked)."""

import html
import re
from pathlib import Path

import pytest
from fakes import FIXED_NOW, T1, T2, judged, save_facts
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.models import GameFact, InventoryEntry, InventoryStatusChange
from app.services.game_facts import PLAYERS, REVIEWS, ensure_game
from app.services.queue import DECLINED, IN_PROGRESS, PLAYED, save_judged
from app.services.reviews import Lane
from app.services.secrets import hash_password
from app.services.settings import set_setting

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"


# --- helpers ----------------------------------------------------------------


@pytest.fixture
def solo_picks(db, games, solo_version, research_run):
    save_facts(db, 40, "Cozy Crawl", price_cents=999, early_access=True)
    save_facts(db, 20, "Test Tactics")
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    save_judged(
        db,
        [judged(40, risk="Light on depth."), judged(20, source="owned_backlog")],
        run_id=research_run.id,
        round=1,
        lane=Lane.SOLO,
        room=5,
        profile_version_id=solo_version.id,
        now=FIXED_NOW,
    )


def tab(label, count):
    """A status tab's label followed by its count, whatever markup sits between them."""
    return re.compile(rf">\s*{label}\s*(?:<[^>]*>\s*)*{count}\s*<")


def put(db, run, version, *proposals, lane=Lane.SOLO, round=1, room=20):
    """Save the proposals for the lane, as research does."""
    return save_judged(
        db,
        list(proposals),
        run_id=run.id,
        round=round,
        lane=lane,
        room=room,
        profile_version_id=version.id,
        now=FIXED_NOW,
    )


def move(db, appid, status, lane="solo", when=FIXED_NOW):
    """What a later plan's status change will do: move the entry and add a record."""
    entry = db.scalar(
        select(InventoryEntry).where(
            InventoryEntry.appid == appid, InventoryEntry.lane == lane
        )
    )
    entry.status = status
    db.add(
        InventoryStatusChange(
            entry_id=entry.id,
            status=status,
            reason=None,
            candidate_id=None,
            changed_at=when,
        )
    )
    db.commit()


def visible(markup):
    """The words of some markup as a reader sees them: tags gone, entities decoded, runs of
    white space one space."""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", markup)).split())


def pick(text, appid):
    """The markup of one pick."""
    for chunk in text.split('<li id="pick-')[1:]:
        if chunk.startswith(f'{appid}"'):
            return chunk.split("</main>")[0]
    raise AssertionError(f"No pick for {appid}")


def page(client, lane="solo", status="recommended"):
    return client.get(f"/recommendations?lane={lane}&status={status}").text


def count_queries(app, work):
    """How many SQL statements `work()` runs, and what it returns."""
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        result = work()
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)
    return len(seen), result


# --- the page -----------------------------------------------------------------


def test_lanes_and_tabs_with_counts(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations").text
    for label in ("Solo", "Two players", "Three to four", "Five or more"):
        assert label in text
    for label, count in (
        ("Recommended", 2),
        ("In progress", 0),
        ("Played", 0),
        ("Declined", 0),
    ):
        assert tab(label, count).search(text)


def test_a_pick_explains_itself(onboarded_client, solo_picks):
    text = html.unescape(onboarded_client.get("/recommendations?lane=solo").text)
    assert (
        "Likes short, tight sessions" in text
        and "Stated" in text
        and "Main risk: Light on depth." in text
    )
    assert (
        "Found by web research" in text
        and "$9.99" in text
        and "Early Access" in text
        and "checked" in text
    )


def test_owned_games_come_first_and_show_no_price(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations?lane=solo").text
    assert (
        text.index("Test Tactics") < text.index("Cozy Crawl")
        and "Owned" in text
        and "$19.99" not in text
    )


def test_coop_picks_show_up_to_players(
    onboarded_client, db, games, research_run, solo_version
):
    save_facts(db, 40, "Cozy Crawl", players=((1, 4), None))
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    save_judged(
        db,
        [judged(40)],
        run_id=research_run.id,
        round=1,
        lane=Lane.TWO,
        room=5,
        profile_version_id=solo_version.id,
        now=FIXED_NOW,
    )
    assert "Up to 4 players" in onboarded_client.get("/recommendations?lane=two").text


def test_an_empty_lane_points_to_research(onboarded_client):
    text = html.unescape(onboarded_client.get("/recommendations?lane=five_plus").text)
    assert (
        "No recommendations for Five or more yet. Run research to find some." in text
        and 'href="/research"' in text
    )


def test_unknown_lane_or_status_404(onboarded_client):
    assert onboarded_client.get("/recommendations?lane=bogus").status_code == 404
    assert onboarded_client.get("/recommendations?status=bogus").status_code == 404


def test_pick_text_is_escaped(onboarded_client, db, games, research_run, solo_version):
    save_facts(db, 40, "Cozy Crawl")
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    save_judged(
        db,
        [judged(40, risk="<script>x</script>")],
        run_id=research_run.id,
        round=1,
        lane=Lane.SOLO,
        room=5,
        profile_version_id=solo_version.id,
        now=FIXED_NOW,
    )
    text = onboarded_client.get("/recommendations").text
    assert "<script>x" not in text and "&lt;script&gt;" in text


# --- more of the page -----------------------------------------------------------


def test_the_defaults_are_solo_and_recommended(onboarded_client, solo_picks):
    assert (
        onboarded_client.get("/recommendations").text
        == onboarded_client.get("/recommendations?lane=solo&status=recommended").text
    )


def test_an_empty_parameter_is_a_404_not_a_default(onboarded_client):
    assert onboarded_client.get("/recommendations?lane=").status_code == 404
    assert onboarded_client.get("/recommendations?status=").status_code == 404


def test_the_page_needs_login(app, db):
    set_setting(db, "password_hash", hash_password("sample-password"))
    response = TestClient(app).get("/recommendations", follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert "Likes" not in response.text


def test_the_page_is_titled_and_the_nav_marks_it(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations").text
    assert re.search(r"<h1[^>]*>\s*Recommendations\s*</h1>", text)
    assert "<title>Recommendations - Game Taste</title>" in text
    current = re.findall(
        r'<a href="([^"]*)" class="[^"]*is-current"[^>]*aria-current="page"', text
    )
    assert current == ["/recommendations"]


def test_the_lane_control_and_tabs_are_links_that_mark_the_current_ones(
    onboarded_client, solo_picks
):
    text = page(onboarded_client, lane="two", status="declined")
    lanes = re.search(r'<nav[^>]*class="em-seg[^"]*"[^>]*>(.*?)</nav>', text, re.S)
    links = re.findall(r"<a ([^>]*)>(.*?)</a>", lanes.group(1), re.S)
    assert [visible(label) for _, label in links] == [
        "Solo",
        "Two players",
        "Three to four",
        "Five or more",
    ]
    hrefs = [re.search(r'href="([^"]*)"', attrs).group(1) for attrs, _ in links]
    # Choosing a lane keeps the status that is on screen.
    assert hrefs == [
        f"/recommendations?lane={lane}&amp;status=declined"
        for lane in ("solo", "two", "three_four", "five_plus")
    ]
    current = [
        visible(label) for attrs, label in links if 'aria-current="page"' in attrs
    ]
    assert current == ["Two players"]
    assert all(
        ("is-checked" in attrs) == ('aria-current="page"' in attrs)
        for attrs, _ in links
    )

    tabs = re.search(r'<div[^>]*class="em-tabs__list"[^>]*>(.*?)</nav>', text, re.S)
    links = re.findall(r"<a ([^>]*)>(.*?)</a>", tabs.group(1), re.S)
    assert [visible(label) for _, label in links] == [
        "Recommended 0",
        "In progress 0",
        "Played 0",
        "Declined 0",
    ]
    hrefs = [re.search(r'href="([^"]*)"', attrs).group(1) for attrs, _ in links]
    # Choosing a status keeps the lane.
    assert hrefs == [
        f"/recommendations?lane=two&amp;status={status}"
        for status in ("recommended", "in_progress", "played", "declined")
    ]
    current = [
        visible(label) for attrs, label in links if 'aria-current="page"' in attrs
    ]
    assert current == ["Declined 0"]
    assert all(
        ("is-selected" in attrs) == ('aria-current="page"' in attrs)
        for attrs, _ in links
    )


def test_the_tabs_count_each_status_of_the_lane(
    onboarded_client, db, games, solo_version, research_run, solo_picks
):
    ensure_game(db, 30, "Demo Dungeon", FIXED_NOW)
    put(
        db,
        research_run,
        solo_version,
        judged(10),
        judged(30),
        lane=Lane.TWO,
        round=2,
    )
    move(db, 40, IN_PROGRESS)
    move(db, 20, PLAYED)
    text = onboarded_client.get("/recommendations").text
    for label, count in (
        ("Recommended", 0),
        ("In progress", 1),
        ("Played", 1),
        ("Declined", 0),
    ):
        assert tab(label, count).search(text)
    # Another lane has its own counts.
    text = page(onboarded_client, lane="two")
    assert tab("Recommended", 2).search(text) and tab("In progress", 0).search(text)


def test_each_status_lists_its_own_entries(onboarded_client, db, solo_picks):
    move(db, 40, DECLINED)
    move(db, 20, IN_PROGRESS)
    recommended = page(onboarded_client)
    assert "Cozy Crawl" not in recommended and "Test Tactics" not in recommended
    declined = page(onboarded_client, status="declined")
    assert "Cozy Crawl" in declined and "Test Tactics" not in declined
    progress = page(onboarded_client, status="in_progress")
    assert "Test Tactics" in progress and "Cozy Crawl" not in progress
    # An entry that was moved is on the tab it moved to and nowhere else.
    assert 'id="pick-40"' in declined and 'id="pick-40"' not in progress


def test_a_lane_lists_only_its_own_games(onboarded_client, db, solo_picks):
    assert "Cozy Crawl" in page(onboarded_client, lane="solo")
    assert "Cozy Crawl" not in page(onboarded_client, lane="two")


@pytest.mark.parametrize(
    "status, message",
    [
        (
            "recommended",
            "No recommendations for Two players yet. Run research to find some.",
        ),
        ("in_progress", "Nothing in progress in Two players."),
        ("played", "Nothing played from Two players's recommendations yet."),
        ("declined", "Nothing declined in Two players."),
    ],
)
def test_empty_states(onboarded_client, status, message):
    text = html.unescape(page(onboarded_client, lane="two", status=status))
    assert message in text
    # Only an empty Recommended tab sends the user to Research, with a secondary button.
    # (The side nav has a link to Research as well, so look for the button.)
    link = re.search(r'<a class="em-btn[^"]*"[^>]*href="/research"[^>]*>', text)
    if status == "recommended":
        assert link and "em-btn--secondary" in link.group(0)
        assert "Go to Research" in visible(text)
    else:
        assert link is None and "Go to Research" not in text


def test_a_lane_with_picks_has_no_empty_state(onboarded_client, solo_picks):
    text = html.unescape(onboarded_client.get("/recommendations").text)
    assert "No recommendations for" not in text and "Go to Research" not in text


def test_the_page_has_no_primary_button(onboarded_client, solo_picks):
    # Reading is all it does in this plan, and the only buttons are links to other places.
    for lane in ("solo", "five_plus"):
        assert "em-btn--primary" not in page(onboarded_client, lane=lane)


# --- a pick -------------------------------------------------------------------


def test_a_pick_shows_its_name_art_and_source(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations").text
    cozy = pick(text, 40)
    assert re.search(r"<h2[^>]*>\s*Cozy Crawl\s*</h2>", cozy)
    assert "/40/" in cozy  # the artwork of that game
    assert "Found by web research" in visible(cozy)
    assert "From your backlog" in visible(pick(text, 20))


def test_the_source_label_is_the_picks_own(
    onboarded_client, db, solo_version, research_run
):
    save_facts(db, 30, "Demo Dungeon")
    save_facts(db, 40, "Cozy Crawl")
    put(
        db,
        research_run,
        solo_version,
        judged(30, source="candidate_history"),
        judged(40, source="web_research"),
    )
    text = onboarded_client.get("/recommendations").text
    assert "From the candidate history" in visible(pick(text, 30))
    assert "Found by web research" in visible(pick(text, 40))


def test_owned_games_have_an_owned_badge_and_others_do_not(
    onboarded_client, solo_picks
):
    text = onboarded_client.get("/recommendations").text
    badge = re.compile(r'<span class="em-badge[^"]*"[^>]*>\s*Owned\s*</span>')
    assert badge.search(pick(text, 20))
    assert not badge.search(pick(text, 40))


def test_the_early_access_badge_follows_the_facts(
    onboarded_client, db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl", early_access=True)
    save_facts(db, 50, "Party Pile", early_access=False)
    put(db, research_run, solo_version, judged(40), judged(50))
    text = onboarded_client.get("/recommendations").text
    badge = re.compile(
        r'<span class="em-badge em-badge--warning[^"]*"[^>]*>\s*Early Access\s*</span>'
    )
    assert badge.search(pick(text, 40))
    assert "Early Access" not in pick(text, 50)


def test_an_owned_game_shows_no_price_row(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations").text
    owned, bought = visible(pick(text, 20)), visible(pick(text, 40))
    assert "Price" not in owned and "$19.99" not in owned
    assert re.search(r"Price \$9\.99 checked", bought)


def test_free_and_coming_soon_games_say_so(
    onboarded_client, db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl", is_free=True)
    save_facts(db, 50, "Party Pile", price_cents=None, coming_soon=True)
    put(db, research_run, solo_version, judged(40), judged(50))
    text = onboarded_client.get("/recommendations").text
    assert re.search(r"Price Free checked", visible(pick(text, 40)))
    assert re.search(r"Price Coming soon checked", visible(pick(text, 50)))


def test_a_game_with_no_price_in_the_facts_has_no_price_row(
    onboarded_client, db, games, solo_version, research_run
):
    # Not free, not coming soon, and Steam gave no price: nothing is made up.
    save_facts(db, 40, "Cozy Crawl", price_cents=None)
    put(db, research_run, solo_version, judged(40))
    shown = visible(pick(onboarded_client.get("/recommendations").text, 40))
    assert "Price" not in shown and "Free" not in shown and "Coming soon" not in shown


def test_each_fact_has_its_own_checked_date(
    onboarded_client, db, games, solo_version, research_run
):
    # The store facts are 10 days old, the reviews a day, the player counts just now.
    save_facts(
        db, 40, "Cozy Crawl", price_cents=999, players=((1, 4), None), fetched_at=T1
    )
    db.add_all(
        [
            GameFact(
                appid=40,
                kind=REVIEWS,
                data={
                    "score_desc": "Overwhelmingly Positive",
                    "total_positive": 1900,
                    "total_negative": 100,
                    "total_reviews": 2000,
                },
                source_url=None,
                fetched_at=T2,
            ),
            GameFact(
                appid=40,
                kind=PLAYERS,
                data={
                    "online_coop": {"min": 1, "max": 6},
                    "local_coop": None,
                    "competitive": None,
                },
                source_url="https://example.com/players",
                fetched_at=FIXED_NOW,
            ),
        ]
    )
    db.commit()
    put(db, research_run, solo_version, judged(40), lane=Lane.TWO)
    cozy = pick(page(onboarded_client, lane="two"), 40)
    shown = visible(cozy)
    # Each value is followed by when its own fact was fetched, and the newest snapshot is current.
    assert "Players Up to 6 players checked just now" in shown
    assert "Price $9.99 checked Jan 5, 12:00 PM" in shown
    assert (
        "Reviews Overwhelmingly Positive (2,000 reviews) checked Jan 14, 12:00 PM"
        in shown
    )
    assert "Release 1 Jan, 2025 checked Jan 5, 12:00 PM" in shown
    assert shown.count("checked") == 4


def test_a_value_is_never_shown_without_its_checked_date(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations").text
    for appid, rows in ((40, 3), (20, 2)):  # price, reviews, release; owned: no price
        shown = visible(pick(text, appid))
        assert shown.count("checked") == rows


def test_a_pick_with_no_facts_yet_still_shows(
    onboarded_client, db, games, solo_version, research_run
):
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    put(db, research_run, solo_version, judged(40))
    response = onboarded_client.get("/recommendations")
    assert response.status_code == 200
    shown = visible(pick(response.text, 40))
    assert "Cozy Crawl" in shown and "Main risk:" in shown
    assert "checked" not in shown and "Price" not in shown and "Reviews" not in shown


def test_a_store_entry_that_is_not_available_shows_no_store_facts(
    onboarded_client, db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl")
    fact = db.scalar(
        select(GameFact).where(GameFact.appid == 40, GameFact.kind == "store")
    )
    fact.data = {"available": False, "region": "us"}
    db.commit()
    put(db, research_run, solo_version, judged(40))
    shown = visible(pick(onboarded_client.get("/recommendations").text, 40))
    assert (
        "Price" not in shown and "Release" not in shown and "Early Access" not in shown
    )
    assert "Reviews Very Positive (100 reviews) checked" in shown


def test_up_to_players_is_for_co_op_lanes_only(
    onboarded_client, db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl", players=((1, 4), None))
    put(db, research_run, solo_version, judged(40), lane=Lane.SOLO)
    put(db, research_run, solo_version, judged(40), lane=Lane.THREE_FOUR)
    assert "Up to" not in visible(pick(page(onboarded_client, lane="solo"), 40))
    assert "Players Up to 4 players checked" in visible(
        pick(page(onboarded_client, lane="three_four"), 40)
    )


def test_a_co_op_pick_with_no_player_count_has_no_players_row(
    onboarded_client, db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl")
    put(db, research_run, solo_version, judged(40), lane=Lane.TWO)
    assert "Players" not in visible(pick(page(onboarded_client, lane="two"), 40))


def test_up_to_one_player_is_singular(
    onboarded_client, db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl", players=((1, 1), None))
    put(db, research_run, solo_version, judged(40), lane=Lane.TWO)
    shown = visible(pick(page(onboarded_client, lane="two"), 40))
    assert "Up to 1 player checked" in shown


def test_why_it_fits_shows_each_fit_with_its_tag_and_how(
    onboarded_client, db, games, solo_version, research_run
):
    save_facts(db, 40, "Cozy Crawl")
    fits = [
        {
            "section": "loved",
            "text": "Likes short sessions",
            "tag": "stated",
            "how": "Runs are short.",
        },
        {
            "section": "patterns",
            "text": "Drops long games",
            "tag": "observed",
            "how": "It ends in an hour.",
        },
        {
            "section": "hunches",
            "text": "May enjoy puzzles",
            "tag": "guess",
            "how": "It has some.",
        },
    ]
    put(db, research_run, solo_version, judged(40, fits=fits))
    cozy = pick(onboarded_client.get("/recommendations").text, 40)
    assert "Why it fits" in visible(cozy)
    # Each fit is its tag in the badge variant the profile page uses, the line's text and how.
    for tone, word, line, how in (
        ("neutral", "Stated", "Likes short sessions", "Runs are short."),
        ("info", "Observed", "Drops long games", "It ends in an hour."),
        ("warning", "Guess", "May enjoy puzzles", "It has some."),
    ):
        assert re.search(
            rf'<span class="em-badge em-badge--{tone} em-badge--sm text-caption">{word}</span>',
            cozy,
        )
        assert re.search(rf"{word} {line} {how}", visible(cozy))
    assert visible(cozy).index("Likes short") < visible(cozy).index("Drops long")


def test_the_main_risk_is_one_line(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations").text
    assert "Main risk: Light on depth." in visible(pick(text, 40))
    assert "Main risk: Might be too light." in visible(pick(text, 20))


def test_every_piece_of_a_picks_text_is_escaped(
    onboarded_client, db, games, solo_version, research_run
):
    bad = "<img src=x onerror=alert(1)>"
    save_facts(db, 40, bad)
    fits = [{"section": "loved", "text": bad, "tag": "stated", "how": bad}]
    put(db, research_run, solo_version, judged(40, fits=fits, risk=bad))
    text = onboarded_client.get("/recommendations").text
    assert "<img src=x" not in text
    assert (
        text.count("&lt;img src=x onerror=alert(1)&gt;") >= 4
    )  # name, text, how and risk


def test_the_templates_mark_nothing_as_safe():
    for name in ("recommendations.html", "partials/pick.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe|Markup\(|autoescape\s+false", source), name


def test_the_page_uses_a_fixed_number_of_queries(
    app, onboarded_client, db, games, solo_version, research_run
):
    for appid in range(100, 112):
        save_facts(db, appid, f"Crate {appid}", players=((1, 4), None))
    put(db, research_run, solo_version, judged(100), lane=Lane.TWO)
    onboarded_client.get("/recommendations?lane=two")  # settles anything done once
    few, response = count_queries(
        app, lambda: onboarded_client.get("/recommendations?lane=two")
    )
    assert response.text.count('<li id="pick-') == 1
    put(
        db,
        research_run,
        solo_version,
        *[judged(appid) for appid in range(101, 112)],
        lane=Lane.TWO,
        round=2,
    )
    many, response = count_queries(
        app, lambda: onboarded_client.get("/recommendations?lane=two")
    )
    assert response.text.count('<li id="pick-') == 12
    assert many == few
    # Other tabs cost the same.
    move(db, 100, PLAYED, lane="two")
    other, response = count_queries(
        app, lambda: onboarded_client.get("/recommendations?lane=two&status=played")
    )
    assert response.text.count('<li id="pick-') == 1
    assert other == few
