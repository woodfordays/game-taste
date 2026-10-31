"""Settings: AI and search, Steam, people and groups, schedule and account."""

import html
import re

import httpx
import pytest
from cryptography.fernet import Fernet
from fakes import VALID
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Game, GroupMember, JobAssignment, NamedGroup, Person, Provider
from app.services.ai_setup import KEY_UNREADABLE
from app.services.library import (
    KEY_MISSING,
    PROFILE_MISSING,
    StoredSteam,
    save_steam_settings,
    steam_credentials,
    stored_steam,
)
from app.services.people import add_group, add_person, group_member_ids, list_groups
from app.services.secrets import Vault, verify_password
from app.services.settings import (
    get_secret,
    get_setting,
    parse_refresh_time,
    parse_region,
    parse_timezone,
    set_secret,
    set_setting,
)
from conftest import FIXED_NOW

HX = {"HX-Request": "true"}
STEAMID = "76561198000000001"
OTHER_STEAMID = "76561198000000002"

REGION_INVALID = "Enter a two-letter country code, like us or gb."
TIME_INVALID = "Enter a time like 04:00."
TIMEZONE_INVALID = "Pick a timezone from the list."
WRONG_CURRENT = "Your current password didn't match. Try again."


def page(client) -> str:
    return html.unescape(client.get("/settings").text)


def post(client, path: str, data: dict | None = None, **kwargs) -> str:
    """The text an answer shows, with HTML entities (the apostrophes) unescaped."""
    return html.unescape(client.post(path, data=data or {}, **kwargs).text)


def steam_form(**fields) -> dict:
    return {"steam_key": "", "steam_profile": "", "store_region": "us", **fields}


def counts(db) -> tuple[int, int, int]:
    """Rows in person, named_group and group_member, to show a refusal saved nothing."""
    db.expire_all()
    return tuple(
        db.scalar(select(func.count()).select_from(model))
        for model in (Person, NamedGroup, GroupMember)
    )


def game_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Game))


# --- the brief's tests -------------------------------------------------------


def test_ai_settings_blank_key_keeps_stored(
    onboarded_client, db, app, fake_ai, saved_ai_setup
):
    data = {**VALID, "key_anthropic": ""}
    onboarded_client.post("/settings/ai", data=data)
    assert (
        app.state.vault.decrypt(db.get(Provider, "anthropic").api_key_enc)
        == "ant-sample"
    )
    page = onboarded_client.get("/settings").text
    assert "ant-sample" not in page and "Saved key ending in mple" in page


def test_ai_settings_failure_saves_nothing(
    onboarded_client, db, fake_ai, saved_ai_setup
):  # Review focus 2
    fake_ai.fail("other-model", RuntimeError("unknown model"))
    onboarded_client.post(
        "/settings/ai", data={**VALID, "job_judge_model": "other-model"}
    )
    assert db.get(JobAssignment, "judge").model == "sample-local"


def test_schedule_validation(onboarded_client, db):
    assert (
        "Enter a time like 04:00."
        in onboarded_client.post(
            "/settings/schedule", data={"refresh_time": "4am", "timezone": "UTC"}
        ).text
    )
    assert (
        "Pick a timezone from the list."
        in onboarded_client.post(
            "/settings/schedule",
            data={"refresh_time": "05:30", "timezone": "Mars/Base"},
        ).text
    )
    onboarded_client.post(
        "/settings/schedule",
        data={"refresh_time": "05:30", "timezone": "Europe/London"},
    )
    assert (get_setting(db, "refresh_time"), get_setting(db, "timezone")) == (
        "05:30",
        "Europe/London",
    )


def test_change_password(onboarded_client, db):
    # The apostrophe is escaped in the markup, so compare the text a reader sees.
    assert WRONG_CURRENT in post(
        onboarded_client,
        "/settings/password",
        {"current": "wrong-pass", "new": "another-pass", "confirm": "another-pass"},
    )
    onboarded_client.post(
        "/settings/password",
        data={
            "current": "sample-password",
            "new": "another-pass",
            "confirm": "another-pass",
        },
    )
    assert verify_password(get_setting(db, "password_hash"), "another-pass")


def test_region_validation(onboarded_client, db):
    assert (
        REGION_INVALID
        in onboarded_client.post(
            "/settings/steam",
            data={"steam_key": "", "steam_profile": "", "store_region": "USA"},
        ).text
    )


def test_rename_person(onboarded_client, db):
    sam = add_person(db, "Sam", FIXED_NOW)
    onboarded_client.post(
        f"/settings/people/{sam.id}/rename", data={"name": "Samantha"}
    )
    db.expire_all()  # the request used its own session; read the row afresh
    assert db.get(Person, sam.id).name == "Samantha"


# --- the page ----------------------------------------------------------------


def test_the_page_has_the_five_sections_in_order(onboarded_client):
    text = page(onboarded_client)
    titles = [
        "settings-ai-title",
        "settings-steam-title",
        "settings-people-title",
        "settings-schedule-title",
        "settings-account-title",
    ]
    positions = [text.index(f'id="{title}"') for title in titles]
    assert positions == sorted(positions)
    for heading in [
        "AI and search",
        "Steam",
        "People and groups",
        "Schedule",
        "Account",
    ]:
        assert re.search(rf">\s*{heading}\s*</h2>", text), heading
    assert re.search(r"<h1[^>]*>\s*Settings\s*</h1>", text)


def test_the_side_nav_marks_settings_as_current(onboarded_client):
    text = page(onboarded_client)
    link = re.search(r"<a href=\"/settings\"[^>]*>", text)[0]
    assert "is-current" in link and 'aria-current="page"' in link


def test_every_save_button_is_secondary_and_there_is_no_primary_one(onboarded_client):
    text = page(onboarded_client)
    assert "em-btn--primary" not in text
    for label in [
        "Test and save",
        "Save Steam settings",
        "Add person",
        "Save schedule",
        "Change password",
        "Log out",
    ]:
        button = re.search(
            rf"<button[^>]*>\s*<span class=\"em-btn__label\">{label}</span>", text
        )
        assert button and "em-btn--secondary" in button[0], label


def test_each_section_has_its_own_form_posting_to_its_own_route(onboarded_client):
    text = page(onboarded_client)
    for action in [
        "/settings/ai",
        "/settings/steam",
        "/settings/people/person",
        "/settings/groups/group",
        "/settings/schedule",
        "/settings/password",
        "/logout",
    ]:
        assert f'action="{action}"' in text, action
    # No form posts to onboarding: that is closed once setup is finished.
    assert "/onboarding/" not in text


def test_a_fresh_install_shows_every_section_without_saved_data(onboarded_client):
    r = onboarded_client.get("/settings")
    assert r.status_code == 200
    text = html.unescape(r.text)
    assert "Steam isn't connected" in text
    assert "No one yet" in text
    assert 'id="provider-rows"' in text and 'name="key_' not in text


def test_the_page_needs_a_login(onboarded_client, app):
    r = TestClient(app).get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = TestClient(app).get("/settings", headers=HX, follow_redirects=False)
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/login"


def test_the_page_is_closed_until_setup_is_finished(logged_in_client):
    r = logged_in_client.get("/settings", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/ai"
    r = logged_in_client.post("/settings/schedule", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/ai"


def test_no_stored_key_is_ever_rendered(
    onboarded_client, db, app, saved_ai_setup, synced_games
):
    text = onboarded_client.get("/settings").text
    for secret in ["ant-sample", "tvly-sample", "steam-sample"]:
        assert secret not in text
    assert text.count("Saved key ending in mple") == 3  # Anthropic, Tavily and Steam


def test_every_id_on_the_page_is_unique(
    onboarded_client, saved_ai_setup, synced_games, people
):
    ids = re.findall(r"\sid=\"([^\"]+)\"", page(onboarded_client))
    assert len(ids) > 40
    assert sorted(i for i in set(ids) if ids.count(i) > 1) == []


def test_every_field_has_a_label(onboarded_client, saved_ai_setup, synced_games, people):
    text = page(onboarded_client)
    labelled = set(re.findall(r"<label[^>]*\sfor=\"([^\"]+)\"", text))
    fields = re.findall(r"<(?:input|select)(?=[\s>])[^>]*>", text)
    shown = [f for f in fields if 'type="hidden"' not in f]
    assert len(shown) > 20
    for tag in shown:
        field_id = re.search(r"\sid=\"([^\"]+)\"", tag)
        assert field_id and field_id[1] in labelled, tag


def test_the_copy_has_no_exclamation_marks_or_emoji(
    onboarded_client, saved_ai_setup, synced_games, people
):
    text = page(onboarded_client)
    visible = re.sub(
        r"<[^>]*>", " ", re.sub(r"<(script|style).*?</\1>", "", text, flags=re.S)
    )
    assert "!" not in visible
    assert not any(
        0x1F000 <= ord(c) <= 0x1FFFF or 0x2600 <= ord(c) <= 0x27BF or ord(c) == 0xFE0F
        for c in visible
    )


# --- AI and search -----------------------------------------------------------


def test_the_ai_form_shows_what_is_saved(onboarded_client, saved_ai_setup):
    text = page(onboarded_client)
    assert 'action="/settings/ai"' in text
    assert 'hx-get="/settings/ai/provider-row"' in text
    assert 'name="url_local"' in text and 'value="http://localhost:1234/v1"' in text
    assert 'value="sample-big"' in text and 'value="sample-local"' in text
    for job, kind in [("recommender", "anthropic"), ("judge", "local")]:
        select = re.search(
            rf"<select[^>]*name=\"job_{job}_provider\".*?</select>", text, re.S
        )[0]
        assert re.search(rf"<option value=\"{kind}\" selected>", select), job
    assert "Test and save" in text and "Test and continue" not in text


def test_saving_the_ai_form_tests_and_shows_every_result(
    onboarded_client, db, app, fake_ai
):
    changed = {**VALID, "job_judge_model": "sample-newer"}
    r = onboarded_client.post("/settings/ai", data=changed)
    text = html.unescape(r.text)
    assert r.status_code == 200 and "<title>Settings" in text  # the whole page
    assert "Every connection worked." in text
    assert "sample-newer on Local" in text and "Tavily web search" in text
    assert [call[1] for call in fake_ai.calls] == [
        "sample-big",
        "sample-newer",
        "sample-local",
    ]
    db.expire_all()
    assert db.get(JobAssignment, "judge").model == "sample-newer"
    assert db.get(Provider, "anthropic").updated_at == FIXED_NOW


def test_saving_does_not_touch_onboarding(onboarded_client, db):
    r = onboarded_client.post("/settings/ai", data=VALID, follow_redirects=False)
    assert r.status_code == 200 and "location" not in r.headers
    assert get_setting(db, "onboarding_step") == "done"


def test_a_failed_test_reports_each_item_and_keeps_the_typed_values(
    onboarded_client, db, fake_ai, fake_search, saved_ai_setup
):  # Review focus 2
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    fake_search.fail(RuntimeError("quota used up"))
    data = {
        **VALID,
        "job_judge_model": "typed-model",
        "key_anthropic": "ant-typed-9999",
    }
    text = post(onboarded_client, "/settings/ai", data)
    assert "Some connections failed" in text and "Nothing was saved." in text
    assert "Didn't connect: RuntimeError: unknown model" in text
    assert "quota used up" in text
    assert 'value="typed-model"' in text
    assert "Saved key ending in mple" in text  # the saved key, not the typed one
    db.expire_all()
    assert db.get(JobAssignment, "judge").model == "sample-local"
    assert (
        get_secret(db, onboarded_client.app.state.vault, "tavily_api_key")
        == "tvly-sample"
    )


def test_missing_details_are_listed_and_nothing_is_tested(
    onboarded_client, fake_ai, fake_search
):
    text = post(
        onboarded_client, "/settings/ai", {**VALID, "tavily_key": "", "url_local": ""}
    )
    assert "Some details need fixing" in text
    assert "Enter the server URL for Local." in text
    assert "Add a Tavily key. All game research uses it." in text
    assert fake_ai.calls == [] and fake_search.calls == []


def test_blank_tavily_key_keeps_the_saved_one(
    onboarded_client, db, app, saved_ai_setup, fake_search
):
    onboarded_client.post("/settings/ai", data={**VALID, "tavily_key": ""})
    assert get_secret(db, app.state.vault, "tavily_api_key") == "tvly-sample"
    assert fake_search.calls  # and the saved key was the one tested


def test_a_typed_key_replaces_the_saved_one_and_is_then_masked(
    onboarded_client, db, app, saved_ai_setup
):
    r = onboarded_client.post(
        "/settings/ai", data={**VALID, "key_anthropic": "ant-rotated-1234"}
    )
    assert "ant-rotated-1234" not in r.text and "Saved key ending in 1234" in r.text
    db.expire_all()
    assert app.state.vault.decrypt(db.get(Provider, "anthropic").api_key_enc) == (
        "ant-rotated-1234"
    )


def test_htmx_swaps_just_the_form_with_status_200(onboarded_client, fake_ai):
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    r = onboarded_client.post("/settings/ai", data=VALID, headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text and 'id="ai-form"' in r.text
    assert "unknown model" in r.text


def test_htmx_success_swaps_the_form_with_the_saved_notice(onboarded_client):
    r = onboarded_client.post("/settings/ai", data=VALID, headers=HX)
    assert r.status_code == 200 and "HX-Redirect" not in r.headers
    assert 'id="ai-form"' in r.text and "Every connection worked." in r.text
    assert "<html" not in r.text


def test_saved_providers_have_no_remove_button(onboarded_client, saved_ai_setup):
    text = page(onboarded_client)
    assert "data-provider-row" in text
    assert "Remove" not in text


def test_a_provider_added_this_time_can_be_removed_before_saving(
    onboarded_client, saved_ai_setup, fake_ai
):
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    data = {
        **VALID,
        "providers": [*VALID["providers"], "openai"],
        "key_openai": "oai-typed",
    }
    text = post(onboarded_client, "/settings/ai", data)
    row = re.search(r"<div id=\"provider-openai\".*?</div>\s*</div>", text, re.S)[0]
    assert 'aria-label="Remove OpenAI"' in row
    # The saved ones still have none.
    assert 'aria-label="Remove Anthropic"' not in text
    assert 'aria-label="Remove Local"' not in text


def test_the_add_provider_row_comes_from_settings(onboarded_client, saved_ai_setup):
    r = onboarded_client.get("/settings/ai/provider-row?kind=gemini")
    assert r.status_code == 200
    assert "<html" not in r.text and 'name="key_gemini"' in r.text
    assert 'aria-label="Remove Gemini"' in r.text
    # A saved provider shows its key hint, never the key.
    text = html.unescape(
        onboarded_client.get("/settings/ai/provider-row?kind=anthropic").text
    )
    assert "Saved key ending in mple" in text and "ant-sample" not in text


def test_the_add_provider_row_refuses_an_unknown_kind(onboarded_client):
    assert (
        onboarded_client.get("/settings/ai/provider-row?kind=bogus").status_code == 422
    )


def test_unreadable_saved_keys_ask_for_the_key_again(onboarded_client, db):
    other = Vault(Fernet.generate_key())
    db.add(
        Provider(
            kind="anthropic",
            base_url=None,
            api_key_enc=other.encrypt("ant-old-key"),
            updated_at=FIXED_NOW,
        )
    )
    db.commit()
    set_secret(db, other, "tavily_api_key", "tvly-old-key")
    r = onboarded_client.get("/settings")
    assert r.status_code == 200 and html.unescape(r.text).count(KEY_UNREADABLE) == 2


# --- Steam -------------------------------------------------------------------


def test_the_steam_section_says_it_is_not_connected(onboarded_client):
    text = page(onboarded_client)
    key = re.search(r"<input[^>]*name=\"steam_key\"[^>]*>", text)[0]
    assert (
        'type="password"' in key and "value=" not in key and "placeholder=" not in key
    )
    profile = re.search(r"<input[^>]*name=\"steam_profile\"[^>]*>", text)[0]
    assert "value=" not in profile
    region = re.search(r"<input[^>]*name=\"store_region\"[^>]*>", text)[0]
    assert 'value="us"' in region  # the default


def test_the_steam_section_shows_the_saved_connection(
    onboarded_client, db, synced_games
):
    set_setting(db, "store_region", "gb")
    text = page(onboarded_client)
    assert f"Connected as {STEAMID}" in text
    key = re.search(r"<input[^>]*name=\"steam_key\"[^>]*>", text)[0]
    assert 'placeholder="Saved key ending in mple"' in key and "value=" not in key
    assert "steam-sample" not in text
    region = re.search(r"<input[^>]*name=\"store_region\"[^>]*>", text)[0]
    assert 'value="gb"' in region


def test_a_short_saved_steam_key_gives_no_hint_of_its_ending(onboarded_client, db, app):
    set_secret(db, app.state.vault, "steam_api_key", "abcdefg")
    set_setting(db, "steam_id", STEAMID)
    text = page(onboarded_client)
    assert 'placeholder="Saved key"' in text and "ending in defg" not in text


def test_connecting_imports_the_library_and_saves_key_id_and_region(
    onboarded_client, db, app
):
    text = post(
        onboarded_client,
        "/settings/steam",
        steam_form(steam_key="steam-new1234", steam_profile=STEAMID, store_region="gb"),
    )
    assert f"Connected as {STEAMID}" in text
    assert "Saved key ending in 1234" in text and "steam-new1234" not in text
    assert "Imported 2 games" in text
    db.expire_all()
    assert get_secret(db, app.state.vault, "steam_api_key") == "steam-new1234"
    assert get_setting(db, "steam_id") == STEAMID
    assert get_setting(db, "store_region") == "gb"
    assert game_count(db) == 2


def test_a_profile_link_or_name_is_resolved(onboarded_client, db, fake_steam):
    fake_steam.vanity["sample-name"] = OTHER_STEAMID
    onboarded_client.post(
        "/settings/steam",
        data=steam_form(
            steam_key="steam-new1234",
            steam_profile="https://steamcommunity.com/id/sample-name/",
        ),
    )
    db.expire_all()
    assert get_setting(db, "steam_id") == OTHER_STEAMID


def test_a_blank_key_keeps_the_saved_one(
    onboarded_client, db, app, synced_games, fake_steam
):
    fake_steam.requests.clear()
    text = post(
        onboarded_client, "/settings/steam", steam_form(steam_profile=OTHER_STEAMID)
    )
    assert {r.url.params.get("key") for r in fake_steam.requests} == {"steam-sample"}
    assert f"Connected as {OTHER_STEAMID}" in text
    db.expire_all()
    assert get_secret(db, app.state.vault, "steam_api_key") == "steam-sample"
    assert get_setting(db, "steam_id") == OTHER_STEAMID


def test_a_blank_profile_keeps_the_saved_steam_id(
    onboarded_client, db, app, synced_games, fake_steam
):
    fake_steam.requests.clear()
    onboarded_client.post("/settings/steam", data=steam_form(steam_key="steam-rotated"))
    assert {r.url.params.get("steamid") for r in fake_steam.requests} == {STEAMID}
    db.expire_all()
    assert get_secret(db, app.state.vault, "steam_api_key") == "steam-rotated"
    assert get_setting(db, "steam_id") == STEAMID


def test_with_both_blank_only_the_region_is_saved(
    onboarded_client, db, app, synced_games, fake_steam
):
    fake_steam.requests.clear()
    text = post(onboarded_client, "/settings/steam", steam_form(store_region="de"))
    assert fake_steam.requests == []  # nothing was imported or checked
    assert "Store region saved." in text
    db.expire_all()
    assert get_setting(db, "store_region") == "de"
    assert get_secret(db, app.state.vault, "steam_api_key") == "steam-sample"
    assert get_setting(db, "steam_id") == STEAMID


def test_the_region_is_saved_even_when_steam_is_not_configured(
    onboarded_client, db, fake_steam
):
    text = post(onboarded_client, "/settings/steam", steam_form(store_region="gb"))
    assert "Store region saved." in text and "Steam isn't connected" in text
    db.expire_all()
    assert get_setting(db, "store_region") == "gb"
    assert get_setting(db, "steam_id") is None and fake_steam.requests == []


@pytest.mark.parametrize("typed", ["GB", " gb ", "Gb\n"])
def test_a_region_is_taken_in_any_case(onboarded_client, db, typed):
    onboarded_client.post("/settings/steam", data=steam_form(store_region=typed))
    db.expire_all()
    assert get_setting(db, "store_region") == "gb"


@pytest.mark.parametrize("bad", ["USA", "u", "", "u1", "1a", "u s", "é", "us\nx"])
def test_a_bad_region_is_refused_and_nothing_is_saved(
    onboarded_client, db, fake_steam, bad
):
    set_setting(db, "store_region", "gb")
    text = post(
        onboarded_client,
        "/settings/steam",
        steam_form(steam_key="steam-new1234", steam_profile=STEAMID, store_region=bad),
    )
    assert REGION_INVALID in text
    assert fake_steam.requests == []  # the whole section is held back
    db.expire_all()
    assert (
        get_setting(db, "store_region") == "gb" and get_setting(db, "steam_id") is None
    )
    # What was typed stays in the fields.
    assert 'value="steam-new1234"' in text and f'value="{STEAMID}"' in text


def test_a_profile_with_no_key_anywhere_asks_for_the_key(
    onboarded_client, db, fake_steam
):
    text = post(
        onboarded_client,
        "/settings/steam",
        steam_form(steam_profile=STEAMID, store_region="gb"),
    )
    assert KEY_MISSING in text and PROFILE_MISSING not in text
    assert f'value="{STEAMID}"' in text
    db.expire_all()
    assert fake_steam.requests == [] and get_setting(db, "store_region") is None


def test_a_key_with_no_profile_anywhere_asks_for_the_profile(
    onboarded_client, db, fake_steam
):
    text = post(
        onboarded_client, "/settings/steam", steam_form(steam_key="steam-new1234")
    )
    assert PROFILE_MISSING in text and KEY_MISSING not in text
    assert 'value="steam-new1234"' in text
    assert fake_steam.requests == []


@pytest.mark.parametrize(
    "failure, message",
    [
        (401, "Steam refused the API key. Check the key and try again."),
        (500, "Steam didn't respond. Try again in a minute."),
    ],
)
def test_a_steam_failure_saves_nothing_keeps_the_typed_values_and_the_connection(
    onboarded_client, db, app, synced_games, fake_steam, failure, message
):  # Review focus 1
    set_setting(db, "store_region", "gb")
    fake_steam.fail_with(failure)
    text = post(
        onboarded_client,
        "/settings/steam",
        steam_form(
            steam_key="steam-typed-5678", steam_profile=OTHER_STEAMID, store_region="fr"
        ),
    )
    assert message in text and "Nothing was saved." in text
    assert 'value="steam-typed-5678"' in text and f'value="{OTHER_STEAMID}"' in text
    assert 'value="fr"' in text  # still the region as typed
    db.expire_all()
    assert get_secret(db, app.state.vault, "steam_api_key") == "steam-sample"
    assert get_setting(db, "steam_id") == STEAMID
    assert get_setting(db, "store_region") == "gb"


def test_a_timeout_is_reported_not_raised(
    onboarded_client, db, fake_steam
):  # Review focus 1
    fake_steam.fail_with(httpx.ReadTimeout("slow"))
    r = onboarded_client.post(
        "/settings/steam",
        data=steam_form(steam_key="steam-new1234", steam_profile=STEAMID),
    )
    assert r.status_code == 200
    assert "Steam didn't respond." in html.unescape(r.text)
    assert get_setting(db, "steam_id") is None


def test_a_private_profile_says_what_to_change(
    onboarded_client, db, fake_steam
):  # Review focus 1
    fake_steam.owned = []
    text = post(
        onboarded_client,
        "/settings/steam",
        steam_form(steam_key="steam-new1234", steam_profile=STEAMID),
    )
    assert "Game details to Public" in text and "Nothing was saved." in text
    assert get_setting(db, "steam_id") is None and game_count(db) == 0


def test_an_unknown_vanity_name_names_what_was_typed_and_is_escaped(
    onboarded_client, fake_steam
):
    r = onboarded_client.post(
        "/settings/steam",
        data=steam_form(steam_key="steam-new1234", steam_profile='x"><b>y</b>'),
    )
    text = html.unescape(r.text)
    assert "Couldn't find a Steam profile called" in text
    assert "<b>y</b>" not in r.text


def test_the_saved_key_typed_again_is_not_echoed(
    onboarded_client, synced_games, fake_steam
):
    fake_steam.fail_with(401)
    r = onboarded_client.post(
        "/settings/steam",
        data=steam_form(steam_key="steam-sample", steam_profile=OTHER_STEAMID),
    )
    assert "steam-sample" not in r.text and "Saved key ending in mple" in r.text


def unreadable_steam_key(db) -> None:
    """Leave a saved Steam key this app's key file can't read, as after replacing secret.key."""
    set_secret(db, Vault(Fernet.generate_key()), "steam_api_key", "steam-old")
    set_setting(db, "steam_id", STEAMID)


def key_input(text: str) -> str:
    return re.search(r"<input[^>]*name=\"steam_key\"[^>]*>", text)[0]


def test_an_unreadable_saved_key_is_named_and_the_connection_is_kept(
    onboarded_client, db
):
    unreadable_steam_key(db)
    r = onboarded_client.get("/settings")
    text = html.unescape(r.text)
    assert r.status_code == 200
    assert KEY_UNREADABLE in text
    assert f"Connected as {STEAMID}" in text and "Steam isn't connected" not in text
    # The message belongs to the key field, and showing the page doesn't grab the focus.
    assert 'aria-invalid="true"' in key_input(text)
    assert "autofocus" not in key_input(text)
    assert "steam-old" not in text


def test_a_new_key_with_a_blank_profile_uses_the_saved_steam_id(
    onboarded_client, db, app, fake_steam
):
    unreadable_steam_key(db)
    text = post(
        onboarded_client, "/settings/steam", steam_form(steam_key="steam-new1234")
    )
    assert {r.url.params.get("steamid") for r in fake_steam.requests} == {STEAMID}
    assert f"Connected as {STEAMID}" in text and "Saved key ending in 1234" in text
    assert KEY_UNREADABLE not in text and PROFILE_MISSING not in text
    db.expire_all()
    assert get_secret(db, app.state.vault, "steam_api_key") == "steam-new1234"


def test_a_profile_with_an_unreadable_saved_key_asks_for_the_key_again(
    onboarded_client, db, fake_steam
):
    unreadable_steam_key(db)
    text = post(
        onboarded_client,
        "/settings/steam",
        steam_form(steam_profile=OTHER_STEAMID, store_region="gb"),
    )
    assert KEY_UNREADABLE in text and KEY_MISSING not in text
    assert f'value="{OTHER_STEAMID}"' in text  # what was typed stays
    assert fake_steam.requests == []
    db.expire_all()
    assert get_setting(db, "steam_id") == STEAMID
    assert get_setting(db, "store_region") is None


def test_the_region_alone_still_saves_with_an_unreadable_saved_key(
    onboarded_client, db, fake_steam
):
    unreadable_steam_key(db)
    text = post(onboarded_client, "/settings/steam", steam_form(store_region="gb"))
    assert "Store region saved." in text and fake_steam.requests == []
    assert KEY_UNREADABLE in text  # the key still needs entering
    db.expire_all()
    assert get_setting(db, "store_region") == "gb"


def test_a_typed_key_that_fails_is_not_also_called_unreadable(
    onboarded_client, db, fake_steam
):
    unreadable_steam_key(db)
    fake_steam.fail_with(401)
    text = post(
        onboarded_client, "/settings/steam", steam_form(steam_key="steam-typed-5678")
    )
    assert "Steam refused the API key" in text and KEY_UNREADABLE not in text
    assert 'value="steam-typed-5678"' in text


def test_an_unreadable_saved_key_still_counts_as_not_connected_elsewhere(
    onboarded_client, db, app, fake_steam
):
    unreadable_steam_key(db)
    assert steam_credentials(db, app.state.vault) is None  # what Library relies on
    text = html.unescape(onboarded_client.get("/library").text)
    assert "Connect Steam in Settings to import your library." in text
    assert "Re-sync library" not in text


def test_stored_steam_reads_the_key_and_the_id_separately(db, app):
    vault = app.state.vault
    assert stored_steam(db, vault) == StoredSteam(None, None, False)
    set_setting(db, "steam_id", STEAMID)
    assert stored_steam(db, vault) == StoredSteam(None, STEAMID, False)
    set_secret(db, vault, "steam_api_key", "steam-sample")
    assert stored_steam(db, vault) == StoredSteam("steam-sample", STEAMID, False)
    unreadable_steam_key(db)
    assert stored_steam(db, vault) == StoredSteam(None, STEAMID, True)
    # A value saved as plain text can't be decrypted either.
    set_setting(db, "steam_api_key", "not-encrypted")
    assert stored_steam(db, vault) == StoredSteam(None, STEAMID, True)


def test_steam_htmx_swaps_just_the_section_with_status_200(
    onboarded_client, fake_steam
):
    fake_steam.fail_with(401)
    r = onboarded_client.post(
        "/settings/steam",
        data=steam_form(steam_key="steam-new1234", steam_profile=STEAMID),
        headers=HX,
    )
    assert r.status_code == 200 and "<html" not in r.text
    assert 'id="settings-steam"' in r.text and "Steam refused the API key" in r.text
    ok = onboarded_client.post("/settings/steam", data=steam_form(), headers=HX)
    assert ok.status_code == 200 and 'id="settings-steam"' in ok.text


def test_save_steam_settings_with_both_blank_writes_only_the_region(
    db, app, steam_client, fake_steam
):
    result = save_steam_settings(
        db,
        app.state.vault,
        steam_client,
        api_key="",
        profile="",
        region="gb",
        now=FIXED_NOW,
    )
    assert result.ok and result.synced is None
    assert get_setting(db, "store_region") == "gb" and fake_steam.requests == []


def test_save_steam_settings_reports_each_missing_field(db, app, steam_client):
    both = save_steam_settings(
        db,
        app.state.vault,
        steam_client,
        api_key="k",
        profile="",
        region="us",
        now=FIXED_NOW,
    )
    assert (both.key_error, both.profile_error) == (None, PROFILE_MISSING)
    other = save_steam_settings(
        db,
        app.state.vault,
        steam_client,
        api_key="",
        profile="p",
        region="us",
        now=FIXED_NOW,
    )
    assert (other.key_error, other.profile_error) == (KEY_MISSING, None)
    assert not both.ok and not other.ok and get_setting(db, "store_region") is None


# --- people and groups -------------------------------------------------------


def test_an_empty_install_has_no_built_in_people_or_groups(onboarded_client, db):
    text = page(onboarded_client)
    assert "No one yet" in text and 'type="checkbox"' not in text
    assert "Add people first, then group them." in text


def test_each_person_has_a_rename_form(onboarded_client, people):
    sam, alex, _ = people
    text = page(onboarded_client)
    for person in (sam, alex):
        form = re.search(
            rf"<form[^>]*action=\"/settings/people/{person.id}/rename\"[^>]*>(.*?)</form>",
            text,
            re.S,
        )
        assert form, person.name
        assert f'value="{person.name}"' in form[1] and "Save name" in form[1]


def test_each_group_has_a_rename_form_and_a_member_checklist(
    onboarded_client, db, people
):
    sam, alex, crew = people
    add_person(db, "Riley", FIXED_NOW)
    text = page(onboarded_client)
    rename = re.search(
        rf"<form[^>]*action=\"/settings/groups/{crew.id}/rename\"[^>]*>(.*?)</form>",
        text,
        re.S,
    )
    assert rename and 'value="Sample Crew"' in rename[1]
    members = re.search(
        rf"<form[^>]*action=\"/settings/groups/{crew.id}/members\"[^>]*>(.*?)</form>",
        text,
        re.S,
    )[1]
    boxes = re.findall(r"<input[^>]*type=\"checkbox\"[^>]*>", members)
    assert len(boxes) == 3  # everyone is offered, whoever is a member
    ticked = [b for b in boxes if " checked" in b]
    assert sorted(re.search(r'value="(\d+)"', b)[1] for b in ticked) == sorted(
        [str(sam.id), str(alex.id)]
    )
    assert "Save members" in members


def test_the_groups_show_who_is_in_them(onboarded_client, people):
    text = page(onboarded_client)
    assert "Sample Crew" in text and "Alex, Sam" in text


def test_a_group_with_no_members_says_so(onboarded_client, db):
    add_group(db, "Sample Crew", [], FIXED_NOW)
    assert "No members yet." in page(onboarded_client)


def test_adding_a_person_lists_them(onboarded_client, db):
    r = onboarded_client.post("/settings/people/person", data={"name": "  Riley "})
    assert r.status_code == 200 and "Riley" in r.text
    assert [p.name for p in db.scalars(select(Person))] == ["Riley"]


def test_adding_a_person_saves_the_app_clock(onboarded_client, db):
    onboarded_client.post("/settings/people/person", data={"name": "Riley"})
    assert db.scalar(select(Person.created_at)) == FIXED_NOW


@pytest.mark.parametrize(
    "name, message",
    [
        ("   ", "Enter a name."),
        ("sam", "You already have someone named Sam."),
        ("x" * 61, "Use 60 characters or fewer."),
    ],
)
def test_a_refused_person_is_explained_and_the_typed_name_stays(
    onboarded_client, db, people, name, message
):
    before = counts(db)
    text = post(onboarded_client, "/settings/people/person", {"name": name})
    assert message in text
    if name.strip():
        assert f'value="{name.strip()}"' in text
    assert counts(db) == before


def test_renaming_a_person_changes_it_everywhere(onboarded_client, db, people):
    sam, _, crew = people
    text = post(
        onboarded_client, f"/settings/people/{sam.id}/rename", {"name": " Samantha "}
    )
    assert "Samantha" in text and 'value="Samantha"' in text
    db.expire_all()
    assert db.get(Person, sam.id).name == "Samantha"
    assert [p.name for p in list_groups(db)[0][1]] == ["Alex", "Samantha"]


def test_renaming_a_person_to_a_new_spelling_of_their_own_name_is_fine(
    onboarded_client, db, people
):
    sam = people[0]
    onboarded_client.post(f"/settings/people/{sam.id}/rename", data={"name": "SAM"})
    db.expire_all()
    assert db.get(Person, sam.id).name == "SAM"


def test_a_refused_edit_opens_its_own_row_and_no_other(onboarded_client, people):
    sam, alex, crew = people

    def open_rows(text):
        return re.findall(r"<details[^>]*\sid=\"([a-z-]+-\d+)\"[^>]*\sopen[\s>]", text)

    assert open_rows(page(onboarded_client)) == []
    text = post(onboarded_client, f"/settings/people/{sam.id}/rename", {"name": "alex"})
    assert open_rows(text) == [f"person-row-{sam.id}"]
    text = post(onboarded_client, f"/settings/groups/{crew.id}/members", {"members": ["x"]})
    assert open_rows(text) == [f"group-row-{crew.id}"]
    text = post(onboarded_client, f"/settings/groups/{crew.id}/rename", {"name": ""})
    assert open_rows(text) == [f"group-row-{crew.id}"]


def test_a_refused_rename_is_explained_on_that_person_and_changes_nothing(
    onboarded_client, db, people
):
    sam, alex, _ = people
    text = post(onboarded_client, f"/settings/people/{sam.id}/rename", {"name": "alex"})
    assert "You already have someone named Alex." in text
    assert 'value="alex"' in text  # what was typed
    assert text.count("You already have someone named Alex.") == 1
    db.expire_all()
    assert (
        db.get(Person, sam.id).name == "Sam" and db.get(Person, alex.id).name == "Alex"
    )
    # An empty name is refused the same way.
    assert "Enter a name." in post(
        onboarded_client, f"/settings/people/{sam.id}/rename", {"name": ""}
    )


def test_a_rename_with_a_missing_field_is_an_empty_name(onboarded_client, people):
    sam = people[0]
    assert "Enter a name." in post(
        onboarded_client, f"/settings/people/{sam.id}/rename"
    )


def test_adding_a_group_with_members(onboarded_client, db, people):
    sam, alex, _ = people
    r = onboarded_client.post(
        "/settings/groups/group",
        data={"name": " Weekend Crew ", "members": [str(sam.id)]},
    )
    assert r.status_code == 200 and "Weekend Crew" in r.text
    db.expire_all()
    [(group, members)] = [g for g in list_groups(db) if g[0].name == "Weekend Crew"]
    assert [p.name for p in members] == ["Sam"] and group.created_at == FIXED_NOW


@pytest.mark.parametrize(
    "name, members, message",
    [
        ("", ["sam"], "Enter a name."),
        ("sample crew", [], "You already have a group named Sample Crew."),
        ("New", ["999999"], "Pick people from your list."),
        ("New", ["x"], "Pick people from your list."),
        ("New", [str(2**80)], "Pick people from your list."),
        ("New", ["9" * 5000], "Pick people from your list."),
    ],
)
def test_a_refused_group_is_explained_and_keeps_what_was_typed(
    onboarded_client, db, people, name, members, message
):
    sam = people[0]
    members = [str(sam.id) if m == "sam" else m for m in members]
    before = counts(db)
    text = post(onboarded_client, "/settings/groups/group", {"name": name, "members": members})
    assert message in text
    if name:
        assert f'value="{name}"' in text
    assert counts(db) == before


def test_a_refused_group_keeps_the_ticked_members_ticked(onboarded_client, db, people):
    sam, alex, _ = people
    text = post(
        onboarded_client,
        "/settings/groups/group",
        {"name": "sample crew", "members": [str(alex.id)]},
    )
    boxes = re.findall(r"<input id=\"member-\d+\"[^>]*>", text)
    ticked = [b for b in boxes if " checked" in b]
    assert len(ticked) == 1 and f'value="{alex.id}"' in ticked[0]


def test_renaming_a_group(onboarded_client, db, people):
    crew = people[2]
    text = post(
        onboarded_client, f"/settings/groups/{crew.id}/rename", {"name": " Game Night "}
    )
    assert 'value="Game Night"' in text
    db.expire_all()
    assert db.get(NamedGroup, crew.id).name == "Game Night"
    assert sorted(group_member_ids(db, crew.id)) == sorted(
        [people[0].id, people[1].id]
    )  # members stay


def test_a_refused_group_rename_is_explained_on_that_group(
    onboarded_client, db, people
):
    sam, _, crew = people
    other = add_group(db, "Other Crew", [sam.id], FIXED_NOW)
    text = post(
        onboarded_client, f"/settings/groups/{other.id}/rename", {"name": "sample crew"}
    )
    assert "You already have a group named Sample Crew." in text
    assert 'value="sample crew"' in text
    db.expire_all()
    assert db.get(NamedGroup, other.id).name == "Other Crew"


def test_setting_a_groups_members_replaces_them(onboarded_client, db, people):
    sam, alex, crew = people
    riley = add_person(db, "Riley", FIXED_NOW)
    other = add_group(db, "Other Crew", [sam.id], FIXED_NOW)
    text = post(
        onboarded_client,
        f"/settings/groups/{crew.id}/members",
        {"members": [str(alex.id), str(riley.id)]},
    )
    assert "Alex, Riley" in text
    db.expire_all()
    assert sorted(group_member_ids(db, crew.id)) == sorted([alex.id, riley.id])
    assert group_member_ids(db, other.id) == [sam.id]  # another group is untouched


def test_unticking_everyone_empties_the_group(onboarded_client, db, people):
    crew = people[2]
    # A browser sends no `members` field at all when no box is ticked.
    text = post(onboarded_client, f"/settings/groups/{crew.id}/members")
    assert "No members yet." in text
    db.expire_all()
    assert group_member_ids(db, crew.id) == []


@pytest.mark.parametrize("bad", ["999999", "x", "-1", str(2**80), "", "9" * 5000])
def test_a_refused_member_list_is_explained_and_changes_nothing(
    onboarded_client, db, people, bad
):
    sam, alex, crew = people
    text = post(
        onboarded_client,
        f"/settings/groups/{crew.id}/members",
        {"members": [str(sam.id), bad]},
    )
    assert "Pick people from your list." in text
    db.expire_all()
    assert sorted(group_member_ids(db, crew.id)) == sorted([sam.id, alex.id])


def test_unknown_people_and_groups_are_not_found(onboarded_client, db, people):
    for path in [
        "/settings/people/999/rename",
        "/settings/groups/999/rename",
        "/settings/groups/999/members",
    ]:
        assert onboarded_client.post(path, data={"name": "X"}).status_code == 404, path


@pytest.mark.parametrize(
    "bad_id", ["0", "-1", str(2**63), str(2**70), "x", "1.5", "9" * 5000]
)
def test_an_id_that_cannot_be_one_is_refused_not_a_server_error(
    onboarded_client, bad_id
):
    for path in ["people/{}/rename", "groups/{}/rename", "groups/{}/members"]:
        r = onboarded_client.post(
            f"/settings/{path.format(bad_id)}", data={"name": "X"}
        )
        assert r.status_code == 422, (path, bad_id, r.status_code)


def test_the_largest_possible_id_is_not_found_rather_than_an_error(onboarded_client):
    r = onboarded_client.post(
        f"/settings/people/{2**63 - 1}/rename", data={"name": "X"}
    )
    assert r.status_code == 404


def test_names_are_escaped(onboarded_client, db):
    onboarded_client.post("/settings/people/person", data={"name": "<b>Sam</b>"})
    person = db.scalar(select(Person))
    add_group(db, '"><i>x</i>', [person.id], FIXED_NOW)
    text = onboarded_client.get("/settings").text
    assert "<b>Sam</b>" not in text and "<i>x</i>" not in text
    assert "&lt;b&gt;Sam&lt;/b&gt;" in text


def test_people_htmx_swaps_just_the_section_with_status_200(
    onboarded_client, db, people
):
    sam = people[0]
    for path, data in [
        ("/settings/people/person", {"name": "sam"}),
        (f"/settings/people/{sam.id}/rename", {"name": ""}),
        ("/settings/groups/group", {"name": ""}),
        (f"/settings/groups/{people[2].id}/rename", {"name": ""}),
        (f"/settings/groups/{people[2].id}/members", {"members": ["x"]}),
    ]:
        r = onboarded_client.post(path, data=data, headers=HX)
        assert r.status_code == 200, path
        assert "<html" not in r.text and 'id="settings-people"' in r.text, path
        assert 'id="settings-steam"' not in r.text, path
    ok = onboarded_client.post(
        "/settings/people/person", data={"name": "Riley"}, headers=HX
    )
    assert ok.status_code == 200 and "Riley" in ok.text


def test_the_person_and_group_forms_swap_the_people_section(onboarded_client, people):
    text = page(onboarded_client)
    assert text.count('hx-target="#settings-people"') >= 4  # add and rename forms
    assert 'id="people-section"' not in text  # onboarding's id


def test_a_new_person_appears_in_the_group_checklists(onboarded_client, db, people):
    text = post(onboarded_client, "/settings/people/person", {"name": "Riley"})
    assert text.count("Riley") >= 3  # the list, the add-group form and the group's list
    riley = db.scalar(select(Person).where(Person.name == "Riley"))
    assert text.count(f'value="{riley.id}"') >= 2


# --- schedule ----------------------------------------------------------------


def test_the_schedule_shows_the_defaults(onboarded_client):
    text = page(onboarded_client)
    time = re.search(r"<input[^>]*name=\"refresh_time\"[^>]*>", text)[0]
    assert 'value="04:00"' in time
    select = re.search(r"<select[^>]*name=\"timezone\".*?</select>", text, re.S)[0]
    assert re.search(r"<option value=\"UTC\" selected>", select)


def test_the_timezone_list_is_every_zone_with_the_saved_one_selected(
    onboarded_client, db
):
    import zoneinfo

    set_setting(db, "timezone", "America/Chicago")
    text = page(onboarded_client)
    select = re.search(r"<select[^>]*name=\"timezone\".*?</select>", text, re.S)[0]
    options = re.findall(r"<option value=\"([^\"]*)\"", select)
    assert options == sorted(zoneinfo.available_timezones())
    assert re.search(r"<option value=\"America/Chicago\" selected>", select)
    assert select.count(" selected") == 1


def test_a_saved_timezone_that_is_not_a_zone_asks_for_a_choice(onboarded_client, db):
    set_setting(db, "timezone", "Mars/Base")
    select = re.search(
        r"<select[^>]*name=\"timezone\".*?</select>", page(onboarded_client), re.S
    )[0]
    assert "Choose a timezone" in select
    assert not re.search(r"<option value=\"[A-Za-z]+[^\"]*\" selected>", select)


def test_the_saved_schedule_is_shown_back(onboarded_client, db):
    set_setting(db, "refresh_time", "22:15")
    time = re.search(r"<input[^>]*name=\"refresh_time\"[^>]*>", page(onboarded_client))[
        0
    ]
    assert 'value="22:15"' in time


@pytest.mark.parametrize("good", ["00:00", "04:00", "09:05", "12:59", "19:30", "23:59"])
def test_valid_times_are_saved(onboarded_client, db, good):
    text = post(
        onboarded_client,
        "/settings/schedule",
        {"refresh_time": good, "timezone": "UTC"},
    )
    assert "Schedule saved." in text and TIME_INVALID not in text
    db.expire_all()
    assert get_setting(db, "refresh_time") == good


@pytest.mark.parametrize(
    "bad",
    [
        "4am",
        "4:00",
        "24:00",
        "12:60",
        "7:5",
        "",
        "0400",
        "04:00:00",
        "04.00",
        "٠٤:٠٠",
        "04:00pm",
    ],
)
def test_other_times_are_refused(onboarded_client, db, bad):
    text = post(
        onboarded_client, "/settings/schedule", {"refresh_time": bad, "timezone": "UTC"}
    )
    assert TIME_INVALID in text and "Schedule saved." not in text
    db.expire_all()
    assert get_setting(db, "refresh_time") is None


def test_spaces_around_a_time_are_ignored(onboarded_client, db):
    onboarded_client.post(
        "/settings/schedule", data={"refresh_time": " 05:30\n", "timezone": "UTC"}
    )
    db.expire_all()
    assert get_setting(db, "refresh_time") == "05:30"


@pytest.mark.parametrize(
    "bad", ["Mars/Base", "", "utc ", "europe/london", "../etc/passwd"]
)
def test_a_timezone_must_be_on_the_list(onboarded_client, db, bad):
    text = post(
        onboarded_client,
        "/settings/schedule",
        {"refresh_time": "05:30", "timezone": bad},
    )
    assert TIMEZONE_INVALID in text and TIME_INVALID not in text
    db.expire_all()
    assert get_setting(db, "timezone") is None


def test_a_refused_schedule_saves_neither_field_and_keeps_what_was_typed(
    onboarded_client, db
):
    set_setting(db, "refresh_time", "03:00")
    text = post(
        onboarded_client,
        "/settings/schedule",
        {"refresh_time": "05:30", "timezone": "Mars/Base"},
    )
    assert TIMEZONE_INVALID in text and 'value="05:30"' in text
    db.expire_all()
    assert get_setting(db, "refresh_time") == "03:00"  # not even the good field
    both = post(
        onboarded_client,
        "/settings/schedule",
        {"refresh_time": "5", "timezone": "Mars/Base"},
    )
    assert TIME_INVALID in both and TIMEZONE_INVALID in both and 'value="5"' in both


def test_the_new_timezone_is_used_for_times_shown_after_saving(onboarded_client, db):
    onboarded_client.post(
        "/settings/schedule", data={"refresh_time": "05:30", "timezone": "Asia/Tokyo"}
    )
    text = page(onboarded_client)
    assert re.search(r"<option value=\"Asia/Tokyo\" selected>", text)


def test_the_schedule_has_no_refresh_now_button_yet(onboarded_client):
    assert "Refresh now" not in page(onboarded_client)


def test_schedule_htmx_swaps_just_the_section_with_status_200(onboarded_client):
    bad = onboarded_client.post(
        "/settings/schedule", data={"refresh_time": "x", "timezone": "UTC"}, headers=HX
    )
    assert bad.status_code == 200 and "<html" not in bad.text
    assert 'id="settings-schedule"' in bad.text and TIME_INVALID in bad.text
    good = onboarded_client.post(
        "/settings/schedule",
        data={"refresh_time": "05:30", "timezone": "UTC"},
        headers=HX,
    )
    assert good.status_code == 200 and "Schedule saved." in good.text


def test_parsers_for_the_schedule_and_region():
    assert parse_region("gb") == "gb" and parse_region(" GB\n") == "gb"
    assert [parse_region(x) for x in ["", "g", "gbr", "g1", "é", "us\nx"]] == [None] * 6
    assert (
        parse_refresh_time(" 04:00 ") == "04:00" and parse_refresh_time("24:00") is None
    )
    assert (
        parse_refresh_time("٠٤:٠٠") is None and parse_refresh_time("04:00\n9") is None
    )
    assert parse_timezone("Europe/London") == "Europe/London"
    assert parse_timezone(" UTC ") == "UTC" and parse_timezone("Europe/london") is None


# --- account -----------------------------------------------------------------


def change(client, current="sample-password", new="another-pass", confirm=None):
    return post(
        client,
        "/settings/password",
        {
            "current": current,
            "new": new,
            "confirm": new if confirm is None else confirm,
        },
    )


def test_changing_the_password_confirms_and_keeps_the_session(
    onboarded_client, db, app
):
    text = change(onboarded_client)
    assert "Password changed." in text
    # Still logged in: the page opens without a login.
    assert onboarded_client.get("/settings", follow_redirects=False).status_code == 200
    # The old password no longer works for a new login, the new one does.
    fresh = TestClient(app)
    assert fresh.post("/login", data={"password": "sample-password"}).status_code == 401
    assert (
        fresh.post(
            "/login", data={"password": "another-pass"}, follow_redirects=False
        ).status_code
        == 303
    )


def test_the_password_is_stored_hashed(onboarded_client, db):
    change(onboarded_client)
    db.expire_all()
    stored = get_setting(db, "password_hash")
    assert "another-pass" not in stored and stored.startswith("$argon2")


def test_a_wrong_current_password_changes_nothing(onboarded_client, db):
    before = get_setting(db, "password_hash")
    text = change(onboarded_client, current="wrong-pass")
    assert WRONG_CURRENT in text and "Password changed." not in text
    db.expire_all()
    assert get_setting(db, "password_hash") == before


def test_a_short_new_password_is_refused_under_its_field(onboarded_client, db):
    before = get_setting(db, "password_hash")
    text = change(onboarded_client, new="short")
    assert "Use at least 8 characters." in text and WRONG_CURRENT not in text
    db.expire_all()
    assert get_setting(db, "password_hash") == before


def test_a_mismatched_confirmation_is_refused_under_its_field(onboarded_client, db):
    before = get_setting(db, "password_hash")
    text = change(onboarded_client, confirm="different-pass")
    assert "The two passwords don't match." in text and "Use at least 8" not in text
    db.expire_all()
    assert get_setting(db, "password_hash") == before


def test_every_problem_is_shown_at_once(onboarded_client):
    text = change(onboarded_client, current="wrong-pass", new="short")
    assert WRONG_CURRENT in text and "Use at least 8 characters." in text


def test_passwords_are_never_trimmed(onboarded_client, db):
    onboarded_client.post(
        "/settings/password",
        data={
            "current": "sample-password",
            "new": "  spaced out  ",
            "confirm": "  spaced out  ",
        },
    )
    db.expire_all()
    assert verify_password(get_setting(db, "password_hash"), "  spaced out  ")
    # And the current password is not trimmed either.
    assert WRONG_CURRENT in change(
        onboarded_client, current=" spaced out", new="third-pass"
    )


def test_passwords_are_never_sent_back_to_the_page(onboarded_client):
    r = onboarded_client.post(
        "/settings/password",
        data={
            "current": "wrong-pass",
            "new": "tryagain-pass",
            "confirm": "no-match-pass",
        },
    )
    assert "wrong-pass" not in r.text and "tryagain-pass" not in r.text
    assert "no-match-pass" not in r.text
    for name in ["current", "new", "confirm"]:
        field = re.search(rf"<input[^>]*name=\"{name}\"[^>]*>", r.text)[0]
        assert 'type="password"' in field and "value=" not in field


def test_the_password_fields_are_marked_for_password_managers(onboarded_client):
    text = page(onboarded_client)
    current = re.search(r"<input[^>]*name=\"current\"[^>]*>", text)[0]
    assert 'autocomplete="current-password"' in current
    for name in ["new", "confirm"]:
        field = re.search(rf"<input[^>]*name=\"{name}\"[^>]*>", text)[0]
        assert 'autocomplete="new-password"' in field


def test_a_missing_or_broken_stored_hash_is_a_mismatch_not_a_server_error(
    onboarded_client, db
):
    set_setting(db, "password_hash", "not-a-hash")
    assert WRONG_CURRENT in change(onboarded_client)


def test_a_missing_field_is_an_empty_password(onboarded_client):
    r = onboarded_client.post("/settings/password")
    assert r.status_code == 200 and "Your current password didn&#39;t match." in r.text


def test_password_htmx_swaps_just_the_section_with_status_200(onboarded_client):
    r = onboarded_client.post(
        "/settings/password",
        data={
            "current": "wrong-pass",
            "new": "another-pass",
            "confirm": "another-pass",
        },
        headers=HX,
    )
    assert r.status_code == 200 and "<html" not in r.text
    assert 'id="settings-password"' in r.text and "didn&#39;t match" in r.text
    ok = onboarded_client.post(
        "/settings/password",
        data={
            "current": "sample-password",
            "new": "another-pass",
            "confirm": "another-pass",
        },
        headers=HX,
    )
    assert ok.status_code == 200 and "Password changed." in ok.text


def test_log_out_posts_to_logout_and_ends_the_session(onboarded_client):
    text = page(onboarded_client)
    form = re.search(r"<form[^>]*action=\"/logout\"[^>]*>(.*?)</form>", text, re.S)
    assert form and 'method="post"' in form[0] and "Log out" in form[1]
    assert "hx-post" not in form[0]  # a plain post: the whole page changes
    r = onboarded_client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert onboarded_client.get("/settings", follow_redirects=False).status_code == 303


def test_the_changed_password_works_after_logging_out(onboarded_client, app):
    change(onboarded_client)
    onboarded_client.post("/logout")
    r = onboarded_client.post(
        "/login", data={"password": "another-pass"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_settings_stays_reachable_after_every_section_is_used(onboarded_client):
    # A smoke test of the full page after each section answers with its own full page.
    for path, data in [
        ("/settings/ai", VALID),
        ("/settings/steam", steam_form()),
        ("/settings/people/person", {"name": "Sam"}),
        ("/settings/groups/group", {"name": "Crew"}),
        ("/settings/schedule", {"refresh_time": "05:30", "timezone": "UTC"}),
        ("/settings/password", {"current": "x", "new": "y", "confirm": "z"}),
    ]:
        r = onboarded_client.post(path, data=data)
        assert r.status_code == 200 and "<h1" in r.text and "Settings" in r.text, path
        for title in ["settings-ai-title", "settings-account-title"]:
            assert f'id="{title}"' in r.text, (path, title)


def test_changing_the_password_logs_out_every_other_session(
    onboarded_client, db, app
):
    other = TestClient(app)
    other.post("/login", data={"password": "sample-password"})
    assert other.get("/settings", follow_redirects=False).status_code == 200

    assert "Password changed." in change(onboarded_client)

    # The other browser is sent to log in, as a page and as an htmx request...
    r = other.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = other.get(
        "/settings", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert r.headers["HX-Redirect"] == "/login"
    # ...while the one that changed it stays logged in, again and again.
    assert onboarded_client.get("/settings", follow_redirects=False).status_code == 200
    assert "Password changed." in change(
        onboarded_client, current="another-pass", new="third-pass"
    )
    assert onboarded_client.get("/settings", follow_redirects=False).status_code == 200
    # The other browser can log in again with the new password.
    assert (
        other.post(
            "/login", data={"password": "third-pass"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert other.get("/settings", follow_redirects=False).status_code == 200


def test_a_refused_password_change_keeps_every_session(onboarded_client, db, app):
    other = TestClient(app)
    other.post("/login", data={"password": "sample-password"})
    change(onboarded_client, current="wrong-pass")
    change(onboarded_client, new="short")
    assert get_setting(db, "session_epoch") is None
    assert other.get("/settings", follow_redirects=False).status_code == 200
