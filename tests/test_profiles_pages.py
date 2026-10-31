"""The Profiles pages: the list of every lane and named group, and one page for each profile
with its tagged lines, the games behind them, earlier versions and a changelog."""

import html
import re
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FIXED_NOW
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.models import AiCall, Game
from app.services.people import add_group, rename_group
from app.services.profiles import (
    ProfileTarget,
    ProfileUpdate,
    ensure_profile,
    find_profile,
    game_names,
    group_key,
    lane_key,
    profile_version,
    save_version,
    version_history,
)
from app.services.reviews import Lane, ReviewForm, publish
from app.services.secrets import hash_password
from app.services.settings import set_setting

EMPTY_LANE = "No reviews in this lane yet. Profiles fill in after you publish reviews."
EMPTY_GROUP = "No reviews tagged with this group yet."
HTML_TEXT = "<script>alert(1)</script>"
HTML_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"

# --- helpers ----------------------------------------------------------------


def fake_call(db) -> AiCall:
    call = AiCall(
        job="recommender",
        provider_kind="anthropic",
        model="sample-big",
        prompt_name="profile_update",
        prompt_version="abc123",
        prompt_hash="0" * 64,
        request="prompt",
        response="reply",
        duration_ms=5,
        created_at=FIXED_NOW,
    )
    db.add(call)
    db.commit()
    return call


def lane_target(lane: Lane) -> ProfileTarget:
    return ProfileTarget(lane_key(lane), "lane", lane, None, lane.value)


def group_target(group) -> ProfileTarget:
    return ProfileTarget(group_key(group.id), "group", None, group.id, group.name)


def line(text: str, tag: str = "stated", games=(), reviews=()) -> dict:
    return {"text": text, "tag": tag, "games": list(games), "reviews": list(reviews)}


def update(
    *,
    loved=(),
    dealbreakers=(),
    patterns=(),
    hunches=(),
    open_questions=(),
    changelog: str = "Learned from new reviews.",
) -> ProfileUpdate:
    return ProfileUpdate.model_validate(
        {
            "profile": {
                "loved": list(loved),
                "dealbreakers": list(dealbreakers),
                "patterns": list(patterns),
                "hunches": list(hunches),
                "open_questions": list(open_questions),
            },
            "changelog": changelog,
        }
    )


def save(db, profile, review_version, *, now=FIXED_NOW, **sections):
    """Save a profile version that learned from `review_version`."""
    return save_version(
        db,
        profile,
        update(**sections),
        source_ids=[review_version.id],
        call=fake_call(db),
        refresh_run_id=None,
        now=now,
    )


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


def visible(markup: str) -> str:
    """The text a reader sees: tags removed, entities decoded, spaces collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def badges(markup: str) -> list[tuple[str, str]]:
    """Every Ember badge as (tone, word)."""
    return re.findall(
        r'<span class="em-badge em-badge--(\w+)[^"]*">\s*(\w+)\s*</span>', markup
    )


def section(markup: str, label: str) -> str:
    """The markup of the profile section titled `label`."""
    found = re.search(
        rf'<section[^>]*class="profile-section"[^>]*>\s*<h2[^>]*>{label}</h2>.*?</section>',
        markup,
        re.S,
    )
    assert found, f"no section {label}"
    return found[0]


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


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def solo_review(db, games):
    """A published solo review of Sample Quest (appid 10)."""
    return publish(db, 10, Lane.SOLO, ReviewForm(rating=9, text="Loved it"), FIXED_NOW)


@pytest.fixture
def solo_profile(db, solo_review):
    return ensure_profile(db, lane_target(Lane.SOLO), FIXED_NOW)


@pytest.fixture
def built_solo_profile(db, solo_review, solo_profile):
    """Solo, version 1: one stated line from Sample Quest."""
    save(
        db,
        solo_profile,
        solo_review,
        loved=[
            line(
                "Likes short, tight sessions",
                games=[10],
                reviews=[solo_review.id],
            )
        ],
    )
    return solo_profile


@pytest.fixture
def built_solo_profile_two_versions(db, solo_review, solo_profile):
    """Solo, two days ago version 1 and now version 2 (which adds a dealbreaker)."""
    save(
        db,
        solo_profile,
        solo_review,
        now=FIXED_NOW - timedelta(days=2),
        loved=[line("Likes short runs", games=[10], reviews=[solo_review.id])],
        changelog="First read of your reviews.",
    )
    save(
        db,
        solo_profile,
        solo_review,
        loved=[line("Likes short runs", games=[10], reviews=[solo_review.id])],
        dealbreakers=[line("Hates long grinds", games=[10], reviews=[solo_review.id])],
        changelog="Added a dealbreaker.",
    )
    return solo_profile


@pytest.fixture
def built_solo_profile_with_html(db, solo_review, solo_profile):
    """Solo, version 1, with markup in every piece of text the model wrote and in the
    name of the game behind the line."""
    add_game(db, 40, f"Evil {HTML_TEXT} Game")
    save(
        db,
        solo_profile,
        solo_review,
        loved=[line(f"Likes {HTML_TEXT}", games=[10, 40], reviews=[solo_review.id])],
        open_questions=[f"Does {HTML_TEXT} matter?"],
        changelog=f"Added {HTML_TEXT}.",
    )
    return solo_profile


@pytest.fixture
def group_review(db, games, people):
    sam, alex, crew = people
    return publish(
        db,
        10,
        Lane.THREE_FOUR,
        ReviewForm(rating=8, named_group_id=crew.id, text="Fun together"),
        FIXED_NOW,
    )


@pytest.fixture
def built_group_profile(db, people, group_review):
    """The group "Sample Crew", version 1."""
    crew = people[2]
    profile = ensure_profile(db, group_target(crew), FIXED_NOW)
    save(
        db,
        profile,
        group_review,
        loved=[line("Likes a team puzzle", games=[10], reviews=[group_review.id])],
        changelog="First group read.",
    )
    return profile


# --- the brief's tests ------------------------------------------------------


def test_nav_has_profiles(onboarded_client):
    assert 'href="/profiles"' in onboarded_client.get("/").text


def test_list_shows_empty_lanes(onboarded_client):
    text = onboarded_client.get("/profiles").text
    for label in ("Solo", "Two players", "Three to four", "Five or more"):
        assert label in text
    assert EMPTY_LANE in text


def test_profile_page_shows_lines_tags_and_changelog(
    onboarded_client, db, built_solo_profile
):
    text = html.unescape(onboarded_client.get("/profiles/lane/solo").text)
    assert "What you love" in text and "Stated" in text and "From: Sample Quest" in text
    assert "Version 1 · " in text and "Learned from new reviews." in text


def test_versions_are_selectable(onboarded_client, db, built_solo_profile_two_versions):
    assert (
        "Version 1 of 2" in onboarded_client.get("/profiles/lane/solo?version=1").text
    )
    assert onboarded_client.get("/profiles/lane/solo?version=9").status_code == 404


def test_unknown_targets_404(onboarded_client):
    assert onboarded_client.get("/profiles/lane/bogus").status_code == 404
    assert onboarded_client.get("/profiles/group/999").status_code == 404


def test_profile_text_is_escaped(onboarded_client, db, built_solo_profile_with_html):
    # Every page carries its own inline scripts, so the check is for the model's markup.
    text = onboarded_client.get("/profiles/lane/solo").text
    assert HTML_TEXT not in text
    assert HTML_ESCAPED in text


# --- the side nav and the list ------------------------------------------------


def test_nav_puts_profiles_between_reviews_and_settings(onboarded_client):
    text = onboarded_client.get("/").text
    links = re.findall(r'<a href="(/[a-z]*)" class="em-sidenav__item', text)
    assert links == ["/", "/library", "/reviews", "/profiles", "/settings"]


def test_nav_marks_profiles_as_current(onboarded_client):
    for url in ("/profiles", "/profiles/lane/solo"):
        text = onboarded_client.get(url).text
        current = re.findall(
            r'<a href="([^"]*)" class="[^"]*is-current"[^>]*aria-current="page"', text
        )
        assert current == ["/profiles"], url


def test_pages_need_login(app, db):
    set_setting(db, "password_hash", hash_password("sample-password"))
    stranger = TestClient(app)
    for url in ("/profiles", "/profiles/lane/solo", "/profiles/group/1"):
        r = stranger.get(url, follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"], url


def test_list_shows_every_lane_in_order_with_no_links_when_empty(onboarded_client):
    text = onboarded_client.get("/profiles").text
    titles = [
        visible(t)
        for t in re.findall(
            r'<h2[^>]*class="[^"]*profile-card__title[^"]*"[^>]*>(.*?)</h2>', text, re.S
        )
    ]
    assert titles == ["Solo", "Two players", "Three to four", "Five or more"]
    assert text.count(EMPTY_LANE) == 4
    assert "View profile" not in text and "/profiles/lane/" not in text


def test_list_shows_the_latest_version_and_a_link(
    onboarded_client, db, built_solo_profile_two_versions
):
    text = onboarded_client.get("/profiles").text
    card = re.search(r'<li[^>]*class="em-card".*?</li>', text, re.S)[0]
    assert "Solo" in visible(card)
    assert "Version 2 · updated just now" in visible(card)
    assert 'href="/profiles/lane/solo"' in card
    assert "em-btn em-btn--secondary" in card and "View profile" in visible(card)
    # The other lanes are still empty.
    assert text.count(EMPTY_LANE) == 3


def test_list_names_each_group_and_says_when_it_is_empty(onboarded_client, people):
    sam, alex, crew = people
    text = onboarded_client.get("/profiles").text
    titles = [
        visible(t)
        for t in re.findall(r"profile-card__title[^>]*>(.*?)</h2>", text, re.S)
    ]
    assert titles == [
        "Solo",
        "Two players",
        "Three to four",
        "Five or more",
        "Sample Crew",
    ]
    assert EMPTY_GROUP in text


def test_list_links_a_group_profile(onboarded_client, db, built_group_profile, people):
    crew = people[2]
    text = onboarded_client.get("/profiles").text
    assert f'href="/profiles/group/{crew.id}"' in text
    assert EMPTY_GROUP not in text
    assert "Version 1 · updated just now" in visible(text)


def test_list_view_profile_links_are_described_by_their_card(
    onboarded_client, db, built_solo_profile
):
    text = onboarded_client.get("/profiles").text
    link = re.search(r'<a[^>]*href="/profiles/lane/solo"[^>]*>', text)[0]
    target = re.search(r'aria-describedby="([^"]+)"', link)[1]
    assert re.search(rf'<h2[^>]*id="{target}"[^>]*>\s*Solo\s*</h2>', text)


def test_list_has_an_empty_refresh_area(onboarded_client):
    text = onboarded_client.get("/profiles").text
    assert re.search(r'<div id="refresh-area"[^>]*></div>', text)


def test_list_escapes_a_group_name(onboarded_client, db, people):
    rename_group(db, people[2].id, HTML_TEXT)
    text = onboarded_client.get("/profiles").text
    assert HTML_TEXT not in text and HTML_ESCAPED in text


def test_list_is_a_fixed_number_of_queries(app, onboarded_client, db, people, games):
    base = queries(app, onboarded_client, "/profiles")
    sam, alex, crew = people
    for index in range(5):
        add_group(db, f"Extra crew {index}", [sam.id], FIXED_NOW)
    review = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    profile = ensure_profile(db, lane_target(Lane.SOLO), FIXED_NOW)
    for number in range(3):
        save(db, profile, review, loved=[line(f"Line {number}")])
    assert queries(app, onboarded_client, "/profiles") == base


# --- a profile page -----------------------------------------------------------


def test_title_is_the_lane_label(onboarded_client, db, built_solo_profile):
    text = onboarded_client.get("/profiles/lane/solo").text
    assert re.search(r"<h1[^>]*>\s*Solo\s*</h1>", text)
    assert "<title>Solo" in text
    assert re.search(r'href="/profiles"[^>]*>\s*Back to profiles', text)


def test_title_is_the_current_group_name(
    onboarded_client, db, built_group_profile, people
):
    crew = people[2]
    url = f"/profiles/group/{crew.id}"
    assert re.search(r"<h1[^>]*>\s*Sample Crew\s*</h1>", onboarded_client.get(url).text)
    rename_group(db, crew.id, "Renamed Crew")
    text = onboarded_client.get(url).text
    assert re.search(r"<h1[^>]*>\s*Renamed Crew\s*</h1>", text)
    assert "Sample Crew" not in text
    assert "Likes a team puzzle" in text and "From: Sample Quest" in visible(text)


def test_version_line_and_select(onboarded_client, db, built_solo_profile_two_versions):
    text = onboarded_client.get("/profiles/lane/solo").text
    assert "Version 2 of 2 · updated just now" in visible(text)
    select = re.search(r'<select[^>]*name="version"[^>]*>(.*?)</select>', text, re.S)
    options = re.findall(
        r'<option value="(\d+)"( selected)?>([^<]*)</option>', select[1]
    )
    assert [(value, bool(chosen)) for value, chosen, _ in options] == [
        ("2", True),
        ("1", False),
    ]
    assert "Version 2" in options[0][2] and "Version 1" in options[1][2]
    # A GET form that loads ?version=, with a label for the select.
    form = re.search(r"<form[^>]*>", text)[0]
    assert 'method="get"' in form and 'action="/profiles/lane/solo"' in form
    assert re.search(r'<label for="[^"]+"[^>]*>\s*Version\s*</label>', text)


def test_older_version_shows_its_own_content(
    onboarded_client, db, built_solo_profile_two_versions
):
    older = onboarded_client.get("/profiles/lane/solo?version=1").text
    newer = onboarded_client.get("/profiles/lane/solo").text
    assert "Version 1 of 2 · updated Jan 13, 12:00 PM" in visible(older)
    assert "Hates long grinds" not in older and "Dealbreakers" not in older
    assert "Hates long grinds" in newer and "Dealbreakers" in newer
    # The select shows the version that is on screen.
    assert re.search(r'<option value="1" selected>', older)
    assert re.search(r'<option value="2" selected>', newer)


def test_changelog_lists_every_version_newest_first(
    onboarded_client, db, built_solo_profile_two_versions
):
    text = visible(onboarded_client.get("/profiles/lane/solo?version=1").text)
    newest = "Version 2 · just now: Added a dealbreaker."
    oldest = "Version 1 · Jan 13, 12:00 PM: First read of your reviews."
    assert "Changelog" in text
    assert newest in text and oldest in text
    assert text.index(newest) < text.index(oldest)


def test_sections_follow_the_section_order_and_skip_empty_ones(
    onboarded_client, db, solo_review, solo_profile
):
    ref = dict(games=[10], reviews=[solo_review.id])
    save(
        db,
        solo_profile,
        solo_review,
        loved=[line("Loved line", **ref)],
        patterns=[line("Pattern line", tag="guess", games=[10, 20])],
        hunches=[line("Hunch line", tag="guess", games=[20])],
        open_questions=["Open question one"],
    )
    text = onboarded_client.get("/profiles/lane/solo").text
    headings = re.findall(r'<h2[^>]*id="profile-section-[^"]*"[^>]*>(.*?)</h2>', text)
    assert headings == [
        "What you love",
        "Patterns",
        "Hunches to test",
        "Open questions",
    ]
    assert "Dealbreakers" not in text
    assert "Nothing here yet." not in text


def test_each_tag_gets_its_badge_with_the_word(
    onboarded_client, db, games, solo_review, solo_profile
):
    save(
        db,
        solo_profile,
        solo_review,
        loved=[line("Stated line", tag="stated", reviews=[solo_review.id])],
        patterns=[line("Observed line", tag="observed", games=[10, 20])],
        hunches=[line("Guess line", tag="guess", games=[20])],
    )
    text = onboarded_client.get("/profiles/lane/solo").text
    assert badges(section(text, "What you love")) == [("neutral", "Stated")]
    assert badges(section(text, "Patterns")) == [("info", "Observed")]
    assert badges(section(text, "Hunches to test")) == [("warning", "Guess")]
    assert badges(text) == [
        ("neutral", "Stated"),
        ("info", "Observed"),
        ("warning", "Guess"),
    ]


def test_from_line_names_each_game_once_and_skips_lines_without_games(
    onboarded_client, db, games, solo_review, solo_profile
):
    save(
        db,
        solo_profile,
        solo_review,
        loved=[
            line("Two games", tag="observed", games=[20, 10, 20]),
            line("No games", tag="stated", reviews=[solo_review.id]),
            line("Gone game", tag="guess", games=[999]),
        ],
    )
    text = onboarded_client.get("/profiles/lane/solo").text
    items = re.findall(r'<li class="profile-line">.*?</li>', text, re.S)
    assert len(items) == 3
    assert "From: Test Tactics, Sample Quest" in visible(items[0])
    assert "From:" not in items[1]
    assert "From: Unknown game" in visible(items[2])


def test_open_questions_are_a_plain_list(
    onboarded_client, db, solo_review, solo_profile
):
    save(
        db,
        solo_profile,
        solo_review,
        open_questions=["Do you like roguelikes?", "How long is too long?"],
    )
    text = onboarded_client.get("/profiles/lane/solo").text
    block = section(text, "Open questions")
    assert re.findall(r"<li[^>]*>(.*?)</li>", block, re.S) == [
        "Do you like roguelikes?",
        "How long is too long?",
    ]
    assert "em-badge" not in block and "From:" not in block


def test_a_version_with_nothing_in_it_says_so(
    onboarded_client, db, solo_review, solo_profile
):
    save(db, solo_profile, solo_review)
    text = onboarded_client.get("/profiles/lane/solo").text
    assert "Nothing here yet." in text
    assert "profile-section" not in text
    assert "Learned from new reviews." in text  # the changelog is still there


def test_empty_lane_page_shows_the_empty_state(onboarded_client):
    r = onboarded_client.get("/profiles/lane/two")
    assert r.status_code == 200
    assert EMPTY_LANE in r.text
    assert re.search(r"<h1[^>]*>\s*Two players\s*</h1>", r.text)
    assert "Changelog" not in r.text and "<select" not in r.text


def test_empty_group_page_shows_the_empty_state(onboarded_client, people):
    r = onboarded_client.get(f"/profiles/group/{people[2].id}")
    assert r.status_code == 200
    assert EMPTY_GROUP in r.text
    assert re.search(r"<h1[^>]*>\s*Sample Crew\s*</h1>", r.text)


def test_a_profile_with_no_version_yet_shows_the_empty_state(
    onboarded_client, db, solo_profile
):
    # The row exists once a refresh starts on the lane, and stays if its first update failed.
    r = onboarded_client.get("/profiles/lane/solo")
    assert r.status_code == 200 and EMPTY_LANE in r.text
    assert onboarded_client.get("/profiles/lane/solo?version=1").status_code == 404


def test_pages_have_an_empty_refresh_area(onboarded_client, db, built_solo_profile):
    for url in ("/profiles/lane/solo", "/profiles/lane/two"):
        text = onboarded_client.get(url).text
        assert re.search(r'<div id="refresh-area"[^>]*></div>', text), url


def test_profile_text_is_escaped_in_every_place(
    onboarded_client, db, built_solo_profile_with_html
):
    text = onboarded_client.get("/profiles/lane/solo").text
    # The line, the game behind it, the open question and the changelog.
    assert text.count(HTML_ESCAPED) == 4
    assert HTML_TEXT not in text


def test_templates_never_mark_text_as_safe():
    for name in ("profile.html", "profiles.html", "partials/profile_lines.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe|Markup\(|autoescape\s+false", source), name


# --- bad addresses --------------------------------------------------------------


@pytest.mark.parametrize(
    "version", ["", "0", "-1", "abc", "1.5", "1e3", "٣", "9", "99999999999999999999999"]
)
def test_a_version_that_isnt_a_listed_number_is_404(
    onboarded_client, db, built_solo_profile_two_versions, version
):
    r = onboarded_client.get("/profiles/lane/solo", params={"version": version})
    assert r.status_code == 404, version


def test_a_bad_version_is_404_on_a_group_too(
    onboarded_client, db, built_group_profile, people
):
    url = f"/profiles/group/{people[2].id}"
    assert onboarded_client.get(url + "?version=1").status_code == 200
    assert onboarded_client.get(url + "?version=2").status_code == 404
    assert onboarded_client.get(url + "?version=x").status_code == 404


@pytest.mark.parametrize(
    "group_id",
    ["0", "-1", "abc", "1.5", "99999999999999999999999", "9223372036854775808", "%20"],
)
def test_a_group_that_cant_exist_is_404_never_500(onboarded_client, group_id):
    assert onboarded_client.get(f"/profiles/group/{group_id}").status_code == 404


def test_the_biggest_group_id_is_404_not_500(onboarded_client):
    assert onboarded_client.get(f"/profiles/group/{2**63 - 1}").status_code == 404


def test_lane_names_must_match_exactly(onboarded_client):
    for lane in ("Solo", "SOLO", "two%20", "three-four", "lane:solo"):
        assert onboarded_client.get(f"/profiles/lane/{lane}").status_code == 404, lane
    for lane in ("solo", "two", "three_four", "five_plus"):
        assert onboarded_client.get(f"/profiles/lane/{lane}").status_code == 200, lane


# --- htmx and queries -------------------------------------------------------------


def test_htmx_gets_just_the_profile_body(
    onboarded_client, db, built_solo_profile_two_versions
):
    r = onboarded_client.get(
        "/profiles/lane/solo?version=1", headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    assert "<html" not in r.text and "em-sidenav" not in r.text
    assert 'id="profile-body"' in r.text
    assert "Version 1 of 2" in r.text and "Hates long grinds" not in r.text
    assert "Changelog" not in r.text
    vary = r.headers["vary"].lower()
    assert "hx-request" in vary and "hx-history-restore-request" in vary


def test_history_restore_gets_the_whole_page(
    onboarded_client, db, built_solo_profile_two_versions
):
    r = onboarded_client.get(
        "/profiles/lane/solo?version=1",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert "<html" in r.text and "em-sidenav" in r.text and "Changelog" in r.text


def test_the_version_form_swaps_the_body_and_pushes_the_url(
    onboarded_client, db, built_solo_profile
):
    text = onboarded_client.get("/profiles/lane/solo").text
    form = re.search(r"<form[^>]*>", text)[0]
    assert 'hx-get="/profiles/lane/solo"' in form
    assert 'hx-target="#profile-body"' in form
    assert 'hx-push-url="true"' in form


def test_a_group_form_loads_the_group_address(
    onboarded_client, db, built_group_profile, people
):
    url = f"/profiles/group/{people[2].id}"
    form = re.search(r"<form[^>]*>", onboarded_client.get(url).text)[0]
    assert f'action="{url}"' in form and f'hx-get="{url}"' in form


def test_profile_page_is_a_fixed_number_of_queries(
    app, onboarded_client, db, games, solo_review, solo_profile
):
    add_game(db, 30, "Demo Dungeon")
    ref = dict(games=[10, 20, 30], reviews=[solo_review.id])
    save(db, solo_profile, solo_review, loved=[line("One line", **ref)])
    small = queries(app, onboarded_client, "/profiles/lane/solo")
    for number in range(4):
        save(
            db,
            solo_profile,
            solo_review,
            loved=[line(f"Loved {n}", **ref) for n in range(10)],
            dealbreakers=[line(f"Dealbreaker {n}", **ref) for n in range(10)],
            patterns=[line(f"Pattern {n}", **ref) for n in range(10)],
            hunches=[
                line(f"Hunch {n}", tag="guess", games=[10, 999]) for n in range(10)
            ],
            open_questions=[f"Question {n}" for n in range(5)],
            changelog=f"Change {number}.",
        )
    assert queries(app, onboarded_client, "/profiles/lane/solo") == small
    assert queries(app, onboarded_client, "/profiles/lane/solo?version=2") == small
    assert queries(app, onboarded_client, "/profiles/lane/solo?version=1") == small


# --- the helpers the pages read through --------------------------------------------


def test_version_history_is_newest_first_and_one_query(
    app, db, built_solo_profile_two_versions
):
    profile = find_profile(db, lane_key(Lane.SOLO))
    assert profile.id  # loaded now, so the one query below is the history's
    stamps = []

    def record(conn, cursor, statement, parameters, context, executemany):
        stamps.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        history = version_history(db, profile)
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)
    assert [(v.version, v.changelog) for v in history] == [
        (2, "Added a dealbreaker."),
        (1, "First read of your reviews."),
    ]
    assert history[1].created_at == FIXED_NOW - timedelta(days=2)
    assert len(stamps) == 1 and "content" not in stamps[0]


def test_find_profile_and_profile_version(db, built_solo_profile_two_versions):
    assert find_profile(db, lane_key(Lane.TWO)) is None
    profile = find_profile(db, lane_key(Lane.SOLO))
    assert profile_version(db, profile, 1).changelog == "First read of your reviews."
    assert profile_version(db, profile, 3) is None


def test_game_names_skips_games_that_are_gone(db, games):
    assert game_names(db, [10, 10, 999]) == {10: "Sample Quest"}
    assert game_names(db, []) == {}
