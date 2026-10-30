import html
import re

import pytest

from app.auth import advance_step
from app.routes.onboarding import TOUR_SCREENS
from app.services.people import add_person, list_groups, list_people
from app.services.settings import get_setting
from conftest import FIXED_NOW

# --- the brief's test --------------------------------------------------------


def test_people_step_and_finish(client_at_people_step, db):
    assert (
        "Sam"
        in client_at_people_step.post(
            "/onboarding/people/person", data={"name": "Sam"}
        ).text
    )
    assert (
        client_at_people_step.post(
            "/onboarding/people/done", follow_redirects=False
        ).headers["location"]
        == "/onboarding/tour"
    )
    for page in range(1, 5):
        assert (
            client_at_people_step.get(f"/onboarding/tour?page={page}").status_code
            == 200
        )
    assert (
        client_at_people_step.post(
            "/onboarding/finish", follow_redirects=False
        ).headers["location"]
        == "/"
    )
    assert get_setting(db, "onboarding_step") == "done"


# --- helpers -----------------------------------------------------------------


@pytest.fixture
def client_at_tour_step(logged_in_client, db):
    advance_step(db, "tour")
    return logged_in_client


def tour(client, page) -> str:
    return html.unescape(client.get(f"/onboarding/tour?page={page}").text)


def heading(text: str) -> str:
    return re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S)[1].strip()


def link(text: str, label: str) -> str | None:
    """The href of the link whose visible label is `label`, or None."""
    for match in re.finditer(r"<a [^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>", text, re.S):
        if re.sub(r"<[^>]+>", "", match[2]).strip() == label:
            return match[1]
    return None


# --- the tour ----------------------------------------------------------------


def test_the_tour_is_four_screens():
    assert len(TOUR_SCREENS) == 4


@pytest.mark.parametrize("page", [1, 2, 3, 4])
def test_each_screen_shows_the_step_its_position_and_its_own_title(
    client_at_tour_step, page
):
    text = tour(client_at_tour_step, page)
    assert "Step 5 of 5" in text
    assert f"{page} of 4" in text
    assert heading(text) == TOUR_SCREENS[page - 1].title
    for paragraph in TOUR_SCREENS[page - 1].body:
        assert paragraph in text


def test_the_screens_walk_through_the_loop_in_order(client_at_tour_step):
    # Review, profiles, queue, decline with a reason.
    keywords = ["review", "profile", "queue", "reason"]
    for page, keyword in enumerate(keywords, start=1):
        screen = TOUR_SCREENS[page - 1]
        assert keyword in (screen.title + " ".join(screen.body)).lower(), page
    assert "your own words" in TOUR_SCREENS[0].title
    assert "decline" in TOUR_SCREENS[3].title.lower()


def test_the_first_screen_goes_back_to_the_people_step(client_at_tour_step):
    text = tour(client_at_tour_step, 1)
    assert link(text, "Back") == "/onboarding/people"
    assert link(text, "Next") == "/onboarding/tour?page=2"


@pytest.mark.parametrize("page", [2, 3])
def test_middle_screens_have_back_and_next(client_at_tour_step, page):
    text = tour(client_at_tour_step, page)
    assert link(text, "Back") == f"/onboarding/tour?page={page - 1}"
    assert link(text, "Next") == f"/onboarding/tour?page={page + 1}"


def test_the_last_screen_goes_home(client_at_tour_step):
    text = tour(client_at_tour_step, 4)
    assert link(text, "Back") == "/onboarding/tour?page=3"
    assert link(text, "Next") is None
    form = re.search(
        r"<form[^>]*method=\"post\"[^>]*action=\"/onboarding/finish\"[^>]*>(.*?)</form>",
        text,
        re.S,
    )
    assert form
    primary = re.search(r"<button[^>]*em-btn--primary[^>]*>.*?</button>", form[1], re.S)
    assert primary and "Go to Home" in primary[0]


@pytest.mark.parametrize("page", [1, 2, 3, 4])
def test_every_screen_has_one_primary_button(client_at_tour_step, page):
    text = tour(client_at_tour_step, page)
    assert text.count("em-btn--primary") == 1
    label = "Go to Home" if page == 4 else "Next"
    primary = re.search(
        r"<(?:a|button)[^>]*em-btn--primary[^>]*>.*?</(?:a|button)>", text, re.S
    )
    assert label in primary[0]


@pytest.mark.parametrize(
    "param, shown",
    [("", 1), ("?page=0", 1), ("?page=-3", 1), ("?page=abc", 1), ("?page=", 1)]
    + [("?page=5", 4), ("?page=99", 4)],
)
def test_a_page_number_outside_the_tour_lands_on_the_nearest_screen(
    client_at_tour_step, param, shown
):
    r = client_at_tour_step.get(f"/onboarding/tour{param}")
    assert r.status_code == 200
    assert heading(html.unescape(r.text)) == TOUR_SCREENS[shown - 1].title


def test_the_tour_is_closed_before_its_step(client_at_people_step):
    r = client_at_people_step.get("/onboarding/tour?page=1", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/people"


def test_leaving_at_the_tour_resumes_at_the_tour(client_at_tour_step):
    r = client_at_tour_step.get("/", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/tour"


def test_the_people_page_is_still_reachable_from_the_tour(client_at_tour_step):
    r = client_at_tour_step.get("/onboarding/people", follow_redirects=False)
    assert r.status_code == 200 and "Step 4 of 5" in r.text


# --- copy --------------------------------------------------------------------


def has_emoji(text: str) -> bool:
    """Whether `text` holds an emoji: the pictograph planes, the dingbat and symbol block, or the emoji selector."""
    return any(
        0x1F000 <= ord(c) <= 0x1FFFF or 0x2600 <= ord(c) <= 0x27BF or ord(c) == 0xFE0F
        for c in text
    )


def visible_text(page: str) -> str:
    """The page without its scripts and doctype, which hold characters that aren't copy."""
    page = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    return page.replace("<!doctype html>", "")


def test_the_tour_copy_has_no_exclamation_marks_or_emoji():
    for screen in TOUR_SCREENS:
        text = screen.title + " ".join(screen.body)
        assert "!" not in text and not has_emoji(text), screen.title


def test_the_onboarding_pages_have_no_exclamation_marks_or_emoji(
    client_at_tour_step,
):
    pages = [tour(client_at_tour_step, n) for n in range(1, 5)]
    pages.append(html.unescape(client_at_tour_step.get("/onboarding/people").text))
    pages.append(
        html.unescape(
            client_at_tour_step.post(
                "/onboarding/people/person", data={"name": ""}
            ).text
        )
    )
    for text in pages:
        shown = visible_text(text)
        assert "!" not in shown and not has_emoji(shown)


def test_the_tour_makes_no_promise_about_dates(client_at_tour_step):
    for number in range(1, 5):
        text = tour(client_at_tour_step, number).lower()
        assert "coming soon" not in text and "next week" not in text


# --- finishing ---------------------------------------------------------------


def test_go_to_home_finishes_setup(client_at_tour_step, db):
    r = client_at_tour_step.post("/onboarding/finish", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert get_setting(db, "onboarding_step") == "done"


def test_htmx_finish_redirects_the_whole_page(client_at_tour_step, db):
    r = client_at_tour_step.post(
        "/onboarding/finish",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/"
    assert get_setting(db, "onboarding_step") == "done"


def test_finishing_is_closed_before_the_tour(client_at_people_step, db):
    r = client_at_people_step.post("/onboarding/finish", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/people"
    assert get_setting(db, "onboarding_step") == "people"


def test_finishing_twice_is_harmless(client_at_tour_step, db):
    client_at_tour_step.post("/onboarding/finish")
    r = client_at_tour_step.post("/onboarding/finish", follow_redirects=False)
    # Setup is over, so the guard sends the user home without running finish again.
    assert r.headers["location"] == "/"
    assert get_setting(db, "onboarding_step") == "done"


def test_after_finishing_the_onboarding_pages_send_the_user_home(
    client_at_tour_step,
):  # Review focus 4
    client_at_tour_step.post("/onboarding/finish")
    for path in ("/onboarding/tour", "/onboarding/people", "/onboarding/finish"):
        r = client_at_tour_step.get(path, follow_redirects=False)
        assert r.headers["location"] == "/", path


def test_finishing_keeps_what_was_added(client_at_people_step, db):
    sam = add_person(db, "Sam", FIXED_NOW)
    client_at_people_step.post("/onboarding/people/done")
    client_at_people_step.post("/onboarding/finish")
    assert [p.id for p in list_people(db)] == [sam.id]
    assert list_groups(db) == []
