import html
import re

import httpx
import pytest
from sqlalchemy import func, select

from app.auth import advance_step
from app.models import Game, Setting
from app.services.library import SyncResult, connect_steam
from app.services.settings import get_secret, get_setting, set_secret, set_setting
from app.services.steam import SteamClient, SteamError
from conftest import FIXED_NOW

KEY = "steam-sample"
STEAMID = "76561198000000001"
FORM = {"steam_key": KEY, "steam_profile": STEAMID}


def steam_page(client) -> str:
    return html.unescape(client.get("/onboarding/steam").text)


def post_steam(client, data=FORM, **kwargs):
    return client.post("/onboarding/steam", data=data, **kwargs)


def game_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Game))


def assert_nothing_saved(db) -> None:
    db.expire_all()
    assert get_setting(db, "steam_api_key") is None
    assert get_setting(db, "steam_id") is None
    assert game_count(db) == 0


# --- the page ---------------------------------------------------------------


def test_page_shows_the_step_and_both_buttons(client_at_steam_step):
    text = steam_page(client_at_steam_step)
    assert "Step 3 of 5" in text
    assert "Import my library" in text and "Skip for now" in text
    # One primary button, and it is the import.
    assert text.count("em-btn--primary") == 1
    primary = re.search(r"<button[^>]*em-btn--primary[^>]*>.*?</button>", text, re.S)[0]
    assert "Import my library" in primary


def test_page_has_both_fields_and_hides_the_key(client_at_steam_step):
    text = steam_page(client_at_steam_step)
    key = re.search(r"<input[^>]*name=\"steam_key\"[^>]*>", text)[0]
    assert 'type="password"' in key and "value=" not in key
    profile = re.search(r"<input[^>]*name=\"steam_profile\"[^>]*>", text)[0]
    assert 'type="text"' in profile and "value=" not in profile


def test_key_link_opens_in_a_new_tab(client_at_steam_step):
    text = steam_page(client_at_steam_step)
    link = re.search(
        r"<a [^>]*href=\"https://steamcommunity\.com/dev/apikey\"[^>]*>", text
    )
    assert link and 'target="_blank"' in link[0] and 'rel="noopener"' in link[0]
    assert "Where do I get a key?" in text


def test_page_says_game_details_must_be_public(client_at_steam_step):
    text = steam_page(client_at_steam_step)
    assert "Game details" in text and "Public" in text


def test_page_has_a_back_button_to_the_ai_step(client_at_steam_step):
    text = steam_page(client_at_steam_step)
    back = re.search(r"<a [^>]*href=\"/onboarding/ai\"[^>]*>.*?</a>", text, re.S)
    assert back and "Back" in back[0]


def test_import_button_shows_a_loading_state(client_at_steam_step):
    text = steam_page(client_at_steam_step)
    assert "hx-disabled-elt" in text and "htmx-indicator" in text
    assert "Importing your library..." in text


def test_skip_is_a_separate_post_that_the_key_form_cannot_trigger(
    client_at_steam_step,
):
    # Skip sits in the button row but belongs to its own form, so pressing Enter in a
    # field imports instead of skipping.
    text = steam_page(client_at_steam_step)
    assert re.search(r"<form[^>]*id=\"steam-skip-form\"[^>]*>", text)
    skip = re.search(
        r"<button[^>]*form=\"steam-skip-form\"[^>]*>.*?</button>", text, re.S
    )
    assert skip and "Skip for now" in skip[0]
    assert 'action="/onboarding/steam/skip"' in text


def test_page_is_reachable_from_a_later_step(logged_in_client, db):
    advance_step(db, "people")
    r = logged_in_client.get("/onboarding/steam", follow_redirects=False)
    assert r.status_code == 200 and "Step 3 of 5" in r.text


# --- importing --------------------------------------------------------------


def test_import_success(client_at_steam_step, db, app, fake_steam):
    fake_steam.owned = [
        {
            "appid": 10,
            "name": "Sample Quest",
            "playtime_forever": 600,
            "rtime_last_played": 1768000000,
        }
    ]
    r = client_at_steam_step.post(
        "/onboarding/steam",
        data={"steam_key": "steam-sample", "steam_profile": "76561198000000001"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/onboarding/people"
    assert (
        get_secret(db, app.state.vault, "steam_api_key") == "steam-sample"
        and get_setting(db, "steam_id") == "76561198000000001"
    )
    assert db.get(Game, 10).owned


def test_success_moves_on_to_the_people_step(client_at_steam_step, db):
    post_steam(client_at_steam_step, follow_redirects=False)
    assert get_setting(db, "onboarding_step") == "people"


def test_importing_again_from_an_earlier_visit_never_rewinds(logged_in_client, db):
    advance_step(db, "tour")
    r = post_steam(logged_in_client, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/people"
    assert get_setting(db, "onboarding_step") == "tour"


def test_a_vanity_link_is_resolved_before_the_import(
    client_at_steam_step, db, fake_steam
):
    fake_steam.vanity = {"sample-vanity": STEAMID}
    data = {
        "steam_key": KEY,
        "steam_profile": "https://steamcommunity.com/id/sample-vanity/",
    }
    r = post_steam(client_at_steam_step, data, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/people"
    assert get_setting(db, "steam_id") == STEAMID


def test_pasted_whitespace_is_dropped(client_at_steam_step, db, app):
    data = {"steam_key": f"  {KEY}\n", "steam_profile": f" {STEAMID} "}
    r = post_steam(client_at_steam_step, data, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/people"
    assert get_secret(db, app.state.vault, "steam_api_key") == KEY
    assert get_setting(db, "steam_id") == STEAMID


def test_the_key_is_stored_encrypted(client_at_steam_step, db):
    post_steam(client_at_steam_step)
    row = db.get(Setting, "steam_api_key")
    assert row.encrypted and KEY not in row.value


def test_htmx_success_redirects_the_whole_page(client_at_steam_step):
    r = post_steam(
        client_at_steam_step,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/onboarding/people"


# --- failures ---------------------------------------------------------------


def break_steam(fake_steam, how: str) -> None:
    """Make Steam fail the way `how` says. "unknown vanity" needs no setup: the fake
    knows no vanity names."""
    if how == "private profile":
        fake_steam.owned = []
    elif how == "refused key":
        fake_steam.fail_with(403)
    elif how == "timeout":
        fake_steam.fail_with(httpx.ConnectTimeout("slow"))
    elif how == "server error":
        fake_steam.fail_with(503)


FAILURES = [
    ("private profile", STEAMID, "set Game details to Public"),
    ("refused key", STEAMID, "Steam refused the API key. Check the key and try again."),
    (
        "unknown vanity",
        "sample-vanity",
        "Couldn't find a Steam profile called 'sample-vanity'.",
    ),
    ("timeout", STEAMID, "Steam didn't respond. Try again in a minute."),
    ("server error", STEAMID, "Steam didn't respond. Try again in a minute."),
]


@pytest.mark.parametrize(
    "how, profile, message", FAILURES, ids=[f[0] for f in FAILURES]
)
def test_every_steam_failure_is_explained_and_recoverable(
    client_at_steam_step, db, fake_steam, how, profile, message
):  # Review focus 1
    break_steam(fake_steam, how)
    r = post_steam(client_at_steam_step, {"steam_key": KEY, "steam_profile": profile})
    text = html.unescape(r.text)
    assert r.status_code == 200
    assert message in text
    # Both typed values stay in the form, and retry and skip are still there.
    assert f'value="{KEY}"' in text and f'value="{profile}"' in text
    assert "Import my library" in text and "Skip for now" in text
    assert_nothing_saved(db)
    assert get_setting(db, "onboarding_step") == "steam"


def test_import_failure_keeps_values_and_offers_skip(
    client_at_steam_step, db, fake_steam
):  # Review focus 1
    fake_steam.owned = []
    text = html.unescape(
        client_at_steam_step.post(
            "/onboarding/steam",
            data={"steam_key": "steam-sample", "steam_profile": "76561198000000001"},
        ).text
    )
    assert (
        "set Game details to Public" in text
        and 'value="76561198000000001"' in text
        and "Skip for now" in text
    )
    assert (
        get_setting(db, "onboarding_step") == "steam"
        and get_setting(db, "steam_id") is None
    )


def test_failure_says_nothing_was_saved_and_what_to_do(
    client_at_steam_step, fake_steam
):
    fake_steam.fail_with(503)
    text = html.unescape(post_steam(client_at_steam_step).text)
    assert 'role="alert"' in text
    assert "Nothing was saved." in text and "skip this step" in text


def test_failure_leaves_an_earlier_connection_alone(
    client_at_steam_step, db, app, fake_steam
):
    set_secret(db, app.state.vault, "steam_api_key", "steam-old")
    set_setting(db, "steam_id", "76561198000000009")
    fake_steam.fail_with(403)
    post_steam(client_at_steam_step)
    db.expire_all()
    assert get_secret(db, app.state.vault, "steam_api_key") == "steam-old"
    assert get_setting(db, "steam_id") == "76561198000000009"


def test_text_from_the_user_in_an_error_is_escaped(client_at_steam_step):
    # The unknown-vanity message quotes what was typed.
    name = "<script>alert(1)</script>"
    text = post_steam(
        client_at_steam_step, {"steam_key": KEY, "steam_profile": name}
    ).text
    assert "<script>alert(1)" not in text
    assert "&lt;script&gt;alert(1)" in text


@pytest.mark.parametrize(
    "data, message",
    [
        ({"steam_key": "", "steam_profile": STEAMID}, "Enter your Steam Web API key."),
        (
            {"steam_key": KEY, "steam_profile": ""},
            "Enter your Steam profile link or SteamID64.",
        ),
        (
            {"steam_key": "   ", "steam_profile": "  "},
            "Enter your Steam Web API key.",
        ),
        ({}, "Enter your Steam profile link or SteamID64."),
    ],
)
def test_missing_details_are_caught_before_asking_steam(
    client_at_steam_step, db, fake_steam, data, message
):
    r = post_steam(client_at_steam_step, data)
    assert r.status_code == 200 and message in html.unescape(r.text)
    assert fake_steam.requests == []
    assert_nothing_saved(db)


def test_a_missing_detail_keeps_the_other_one(client_at_steam_step):
    text = post_steam(
        client_at_steam_step, {"steam_key": KEY, "steam_profile": ""}
    ).text
    assert f'value="{KEY}"' in text


def test_htmx_failure_swaps_just_the_form_with_status_200(
    client_at_steam_step, fake_steam
):
    fake_steam.fail_with(503)
    r = post_steam(client_at_steam_step, headers={"HX-Request": "true"})
    text = html.unescape(r.text)
    assert r.status_code == 200
    assert "<html" not in text and 'id="steam-form"' in text
    assert "Steam didn't respond." in text and 'value="76561198000000001"' in text


def test_htmx_missing_details_swap_just_the_form_with_status_200(
    client_at_steam_step,
):
    r = post_steam(
        client_at_steam_step,
        {"steam_key": "", "steam_profile": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and 'id="steam-form"' in r.text
    assert "<html" not in r.text


# --- skipping ---------------------------------------------------------------


def test_skip(client_at_steam_step, db):
    assert (
        client_at_steam_step.post(
            "/onboarding/steam/skip", follow_redirects=False
        ).headers["location"]
        == "/onboarding/people"
    )
    assert get_setting(db, "steam_api_key") is None


def test_skip_moves_on_without_touching_steam(client_at_steam_step, db, fake_steam):
    client_at_steam_step.post("/onboarding/steam/skip")
    assert get_setting(db, "onboarding_step") == "people"
    assert fake_steam.requests == [] and game_count(db) == 0


def test_skip_from_an_earlier_step_goes_back_to_that_step(logged_in_client, db):
    # Still on the AI step: the guard keeps the later step out of reach.
    r = logged_in_client.post("/onboarding/steam/skip", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/ai"
    assert get_setting(db, "onboarding_step") in (None, "ai")


def test_skip_never_rewinds(logged_in_client, db):
    advance_step(db, "tour")
    r = logged_in_client.post("/onboarding/steam/skip", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/people"
    assert get_setting(db, "onboarding_step") == "tour"


def test_htmx_skip_redirects_the_whole_page(client_at_steam_step):
    r = client_at_steam_step.post(
        "/onboarding/steam/skip",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/onboarding/people"


# --- connect_steam ----------------------------------------------------------


def test_connect_steam_imports_then_saves(db, app, steam_client):
    result = connect_steam(db, app.state.vault, steam_client, KEY, STEAMID, FIXED_NOW)
    assert result == SyncResult(added=2, updated=0, total=2)
    assert get_secret(db, app.state.vault, "steam_api_key") == KEY
    assert get_setting(db, "steam_id") == STEAMID
    assert game_count(db) == 2


def test_connect_steam_saves_the_resolved_id_for_a_vanity_name(
    db, app, steam_client, fake_steam
):
    fake_steam.vanity = {"sample-vanity": STEAMID}
    connect_steam(db, app.state.vault, steam_client, KEY, "sample-vanity", FIXED_NOW)
    assert get_setting(db, "steam_id") == STEAMID
    owned_requests = [
        r for r in fake_steam.requests if r.url.path.endswith("GetOwnedGames/v1/")
    ]
    assert owned_requests[0].url.params["steamid"] == STEAMID


def test_connect_steam_leaves_the_store_region_alone(db, app, steam_client):
    set_setting(db, "store_region", "gb")
    connect_steam(db, app.state.vault, steam_client, KEY, STEAMID, FIXED_NOW)
    assert get_setting(db, "store_region") == "gb"


def test_connect_steam_strips_the_key(db, app, steam_client, fake_steam):
    connect_steam(db, app.state.vault, steam_client, f" {KEY}\n", STEAMID, FIXED_NOW)
    assert get_secret(db, app.state.vault, "steam_api_key") == KEY
    assert fake_steam.requests[-1].url.params["key"] == KEY


@pytest.mark.parametrize("profile", [STEAMID, "unknown-name"])
def test_connect_steam_saves_nothing_when_steam_fails(
    db, app, steam_client, fake_steam, profile
):
    # An unknown name fails while resolving the profile, a refused key while syncing.
    fake_steam.fail_with(403 if profile == STEAMID else None)
    with pytest.raises(SteamError):
        connect_steam(db, app.state.vault, steam_client, KEY, profile, FIXED_NOW)
    assert_nothing_saved(db)


def test_connect_steam_never_puts_the_key_in_the_error(
    db, app, steam_client, fake_steam
):
    fake_steam.fail_with(403)
    with pytest.raises(SteamError) as caught:
        connect_steam(db, app.state.vault, steam_client, KEY, STEAMID, FIXED_NOW)
    assert KEY not in str(caught.value)
