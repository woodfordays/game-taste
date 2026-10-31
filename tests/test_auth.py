import json
from base64 import b64encode

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.auth import ONBOARDING_STEPS, advance_step, current_step
from app.main import create_app
from app.services.reviews import Lane, load_form
from app.services.secrets import hash_password, verify_password
from app.services.settings import (
    bump_session_epoch,
    get_secret,
    get_session_epoch,
    get_setting,
    set_secret,
    set_setting,
)


def set_password(db, password="sample-password"):
    set_setting(db, "password_hash", hash_password(password))


# --- the brief's tests -------------------------------------------------------


def test_first_visit_goes_to_password_step(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/password"


def test_set_password_logs_in_and_starts_onboarding(client, db):
    r = client.post(
        "/onboarding/password",
        data={
            "password": "sample-password",
            "confirm": "sample-password",
            "timezone": "America/Chicago",
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/onboarding/ai"
    assert (
        get_setting(db, "timezone") == "America/Chicago"
        and get_setting(db, "onboarding_step") == "ai"
    )
    assert (
        client.get("/library", follow_redirects=False).headers["location"]
        == "/onboarding/ai"
    )


def test_password_rules(client):
    assert (
        "Use at least 8 characters."
        in client.post(
            "/onboarding/password",
            data={"password": "short", "confirm": "short", "timezone": "UTC"},
        ).text
    )
    assert (
        "The two passwords don't match."
        in client.post(
            "/onboarding/password",
            data={
                "password": "sample-password",
                "confirm": "other-password",
                "timezone": "UTC",
            },
        ).text
    )


def test_password_step_locked_once_set(logged_in_client, app):  # Review focus 4
    fresh = TestClient(app)
    assert (
        fresh.get("/onboarding/password", follow_redirects=False).headers["location"]
        == "/login"
    )


def test_login_wrong_and_right(logged_in_client, app):
    fresh = TestClient(app)
    assert (
        "That password didn't match. Try again."
        in fresh.post("/login", data={"password": "nope-nope"}).text
    )
    assert (
        fresh.post(
            "/login", data={"password": "sample-password"}, follow_redirects=False
        ).status_code
        == 303
    )


def test_htmx_without_session_redirects_whole_page(
    logged_in_client, app
):  # Review focus 3
    r = TestClient(app).get(
        "/library", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/login"


def test_onboarding_order_is_enforced(logged_in_client):  # Review focus 4
    assert (
        logged_in_client.get("/onboarding/people", follow_redirects=False).headers[
            "location"
        ]
        == "/onboarding/ai"
    )


def test_finished_onboarding_goes_home(onboarded_client):  # Review focus 4
    assert (
        onboarded_client.get("/onboarding/ai", follow_redirects=False).headers[
            "location"
        ]
        == "/"
    )


def test_reset_flag_clears_password_keeps_secrets(tmp_path, services):
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        set_setting(s, "password_hash", hash_password("sample-password"))
        set_secret(s, app1.state.vault, "tavily_api_key", "tvly-sample")
    (tmp_path / "reset-password").touch()
    app2 = create_app(tmp_path, services=services)
    assert not (tmp_path / "reset-password").exists()
    with app2.state.session_factory() as s:
        assert get_setting(s, "password_hash") is None
        assert get_secret(s, app2.state.vault, "tavily_api_key") == "tvly-sample"
    assert (
        TestClient(app2).get("/", follow_redirects=False).headers["location"]
        == "/onboarding/password"
    )


# --- onboarding step helpers -------------------------------------------------


def test_step_order():
    assert ONBOARDING_STEPS == ["ai", "steam", "people", "tour", "done"]


def test_current_step_without_password_is_password(db):
    assert current_step(db) == "password"
    # Progress saved before a reset doesn't matter until a password exists again.
    set_setting(db, "onboarding_step", "people")
    assert current_step(db) == "password"


def test_current_step_with_password_defaults_to_ai(db):
    set_password(db)
    assert current_step(db) == "ai"
    set_setting(db, "onboarding_step", "people")
    assert current_step(db) == "people"


def test_advance_step_only_moves_forward(db):
    set_password(db)
    advance_step(db, "steam")
    assert get_setting(db, "onboarding_step") == "steam"
    advance_step(db, "ai")
    assert get_setting(db, "onboarding_step") == "steam"
    advance_step(db, "steam")
    assert get_setting(db, "onboarding_step") == "steam"
    advance_step(db, "done")
    assert current_step(db) == "done"
    advance_step(db, "tour")
    assert current_step(db) == "done"


def test_advance_step_saves_the_first_step_when_none_is_stored(db):
    advance_step(db, "ai")
    assert get_setting(db, "onboarding_step") == "ai"


def test_advance_step_rejects_unknown_steps(db):
    with pytest.raises(ValueError):
        advance_step(db, "password")
    with pytest.raises(ValueError):
        advance_step(db, "bogus")


# --- first-run password page -------------------------------------------------


def test_password_page_is_the_first_run_screen(client):
    r = client.get("/onboarding/password")
    assert r.status_code == 200
    assert "Step 1 of 5" in r.text
    assert "text-display" in r.text
    assert r.text.count("em-btn--primary") == 1 and "Create login" in r.text
    assert r.text.count('type="password"') == 2
    for name in ("password", "confirm", "timezone"):
        assert f'name="{name}"' in r.text
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in r.text


def test_password_page_has_no_side_nav(client):
    r = client.get("/onboarding/password")
    assert "em-sidenav" not in r.text
    assert 'id="dialog"' in r.text and 'id="toasts"' in r.text


def test_password_is_hashed_and_user_is_logged_in(client, db):
    client.post(
        "/onboarding/password",
        data={
            "password": "sample-password",
            "confirm": "sample-password",
            "timezone": "UTC",
        },
    )
    stored = get_setting(db, "password_hash")
    assert stored.startswith("$argon2") and "sample-password" not in stored
    assert verify_password(stored, "sample-password")
    # The session is already set, so the password page is closed and sends the user on.
    r = client.get("/onboarding/password", follow_redirects=False)
    assert r.headers["location"] == "/"


@pytest.mark.parametrize("zone", ["", "   ", "Not/AZone", "../etc/passwd"])
def test_unknown_or_empty_timezone_is_saved_as_utc(client, db, zone):
    client.post(
        "/onboarding/password",
        data={
            "password": "sample-password",
            "confirm": "sample-password",
            "timezone": zone,
        },
    )
    assert get_setting(db, "timezone") == "UTC"


def test_missing_timezone_field_is_saved_as_utc(client, db):
    client.post(
        "/onboarding/password",
        data={"password": "sample-password", "confirm": "sample-password"},
    )
    assert get_setting(db, "timezone") == "UTC"


def test_password_errors_save_nothing_and_keep_the_form(client, db):
    r = client.post(
        "/onboarding/password",
        data={
            "password": "sample-password",
            "confirm": "other-password",
            "timezone": "America/Chicago",
        },
    )
    assert "The two passwords don't match." in r.text
    assert "Create login" in r.text
    assert "America/Chicago" in r.text
    # Passwords are never echoed back into the page.
    assert "sample-password" not in r.text and "other-password" not in r.text
    assert get_setting(db, "password_hash") is None
    assert get_setting(db, "timezone") is None
    assert (
        client.get("/", follow_redirects=False).headers["location"]
        == "/onboarding/password"
    )


def test_a_short_password_reports_length_before_mismatch(client):
    r = client.post(
        "/onboarding/password",
        data={"password": "short", "confirm": "different", "timezone": "UTC"},
    )
    assert "Use at least 8 characters." in r.text
    assert "The two passwords don't match." not in r.text


def test_missing_fields_show_the_length_message_not_a_422(client):
    r = client.post("/onboarding/password", data={})
    assert "Use at least 8 characters." in r.text


def test_password_post_cannot_replace_an_existing_password(logged_in_client, app, db):
    fresh = TestClient(app)
    r = fresh.post(
        "/onboarding/password",
        data={
            "password": "attacker-password",
            "confirm": "attacker-password",
            "timezone": "UTC",
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/login"
    assert verify_password(get_setting(db, "password_hash"), "sample-password")


# --- login and logout --------------------------------------------------------


def test_login_page(logged_in_client, app):
    r = TestClient(app).get("/login")
    assert r.status_code == 200
    assert r.text.count("em-btn--primary") == 1 and "Log in" in r.text
    assert 'type="password"' in r.text and 'name="password"' in r.text
    assert "em-sidenav" not in r.text


def test_login_success_goes_home_and_guard_resumes_onboarding(logged_in_client, app):
    fresh = TestClient(app)
    r = fresh.post(
        "/login", data={"password": "sample-password"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    # The unfinished user is sent on to their step.
    assert (
        fresh.get("/", follow_redirects=False).headers["location"] == "/onboarding/ai"
    )


def test_wrong_password_does_not_log_in(logged_in_client, app):
    fresh = TestClient(app)
    r = fresh.post("/login", data={"password": "nope-nope"})
    assert "That password didn't match. Try again." in r.text
    assert "Log in" in r.text
    assert fresh.get("/", follow_redirects=False).headers["location"] == "/login"


def test_empty_login_is_a_wrong_password(logged_in_client, app):
    fresh = TestClient(app)
    assert (
        "That password didn't match. Try again." in fresh.post("/login", data={}).text
    )


def test_login_before_a_password_exists_goes_to_the_password_step(client):
    r = client.post("/login", data={"password": "whatever"}, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/password"


def test_session_cookie_is_lax_and_lasts_30_days(logged_in_client, app):
    fresh = TestClient(app)
    r = fresh.post(
        "/login", data={"password": "sample-password"}, follow_redirects=False
    )
    cookie = r.headers["set-cookie"].lower()
    assert "samesite=lax" in cookie
    assert f"max-age={30 * 24 * 60 * 60}" in cookie


def test_logout_clears_the_session(onboarded_client):
    r = onboarded_client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (
        onboarded_client.get("/", follow_redirects=False).headers["location"]
        == "/login"
    )


def test_logout_works_in_the_middle_of_onboarding(logged_in_client):
    r = logged_in_client.post("/logout", follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert (
        logged_in_client.get("/", follow_redirects=False).headers["location"]
        == "/login"
    )


# --- guard rules -------------------------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/static/app.css"])
def test_healthz_and_static_always_pass(client, db, path):
    assert client.get(path, follow_redirects=False).status_code == 200  # no password
    set_password(db)
    assert client.get(path, follow_redirects=False).status_code == 200  # logged out


def test_no_password_sends_everything_to_the_password_step(client):
    for path in ("/", "/login", "/onboarding/ai", "/onboarding/password/", "/settings"):
        r = client.get(path, follow_redirects=False)
        assert r.headers["location"] == "/onboarding/password", path


def test_htmx_without_a_password_redirects_whole_page(client):
    r = client.get("/library", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == "/onboarding/password"


def test_logged_out_user_may_only_see_login(logged_in_client, app):
    fresh = TestClient(app)
    assert fresh.get("/login", follow_redirects=False).status_code == 200
    for path in ("/", "/library", "/onboarding/ai", "/onboarding/finish", "/logout"):
        r = fresh.get(path, follow_redirects=False)
        assert r.headers["location"] == "/login", path
    assert fresh.post("/logout", follow_redirects=False).headers["location"] == "/login"


def test_logged_in_user_is_sent_home_from_the_password_step(logged_in_client):
    r = logged_in_client.get("/onboarding/password", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_unfinished_user_is_held_inside_onboarding(logged_in_client):
    for path in ("/", "/library", "/settings", "/login"):
        r = logged_in_client.get(path, follow_redirects=False)
        assert r.headers["location"] == "/onboarding/ai", path


def test_htmx_outside_onboarding_redirects_whole_page(logged_in_client):
    r = logged_in_client.get(
        "/library", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/onboarding/ai"


def test_current_and_earlier_steps_stay_reachable(app, logged_in_client, db):
    advance_step(db, "people")
    # Probe routes under each step's path, so the test doesn't depend on which step pages
    # exist yet. A probe answers only if the guard let the request through.
    for step in ("ai", "steam", "people"):

        @app.get(f"/onboarding/{step}/_probe")
        def probe() -> str:
            return "reached"

    for step in ("ai", "steam", "people"):
        path = f"/onboarding/{step}/_probe"
        r = logged_in_client.get(path, follow_redirects=False)
        assert "location" not in r.headers, path
        assert r.status_code == 200 and r.json() == "reached", path


def test_later_steps_redirect_to_the_current_step(logged_in_client, db):
    advance_step(db, "steam")
    for path in ("/onboarding/people", "/onboarding/tour", "/onboarding/done"):
        r = logged_in_client.get(path, follow_redirects=False)
        assert r.headers["location"] == "/onboarding/steam", path


def test_onboarding_subpaths_belong_to_their_first_segment(logged_in_client):
    ok = logged_in_client.get("/onboarding/ai/provider-row", follow_redirects=False)
    assert "location" not in ok.headers
    late = logged_in_client.get("/onboarding/steam/import", follow_redirects=False)
    assert late.headers["location"] == "/onboarding/ai"


def test_finish_belongs_to_the_tour_step(logged_in_client, db):
    assert (
        logged_in_client.get("/onboarding/finish", follow_redirects=False).headers[
            "location"
        ]
        == "/onboarding/ai"
    )
    advance_step(db, "tour")
    r = logged_in_client.get("/onboarding/finish", follow_redirects=False)
    assert "location" not in r.headers


def test_unknown_onboarding_paths_do_not_pass_an_unfinished_user(logged_in_client):
    for path in ("/onboarding", "/onboarding/", "/onboarding/bogus"):
        r = logged_in_client.get(path, follow_redirects=False)
        assert r.headers["location"] == "/onboarding/ai", path


def test_finished_user_leaves_onboarding_and_login_alone(onboarded_client):
    for path in (
        "/onboarding/ai",
        "/onboarding/finish",
        "/onboarding/password",
        "/onboarding",
        "/login",
    ):
        r = onboarded_client.get(path, follow_redirects=False)
        assert r.headers["location"] == "/", path


def test_finished_user_reaches_other_pages(app, onboarded_client):
    # A probe route outside onboarding answers only if the guard let the request through.
    @app.get("/library/_probe")
    def probe() -> str:
        return "reached"

    r = onboarded_client.get("/library/_probe", follow_redirects=False)
    assert r.status_code == 200 and r.json() == "reached"
    assert "location" not in r.headers


def test_htmx_finished_user_on_onboarding_url_goes_home(onboarded_client):
    r = onboarded_client.get(
        "/onboarding/ai", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/"


def test_a_session_from_before_a_reset_cannot_skip_the_new_password(tmp_path, services):
    first = create_app(tmp_path, services=services)
    with first.state.session_factory() as s:
        set_password(s)
    client = TestClient(first)
    client.post("/login", data={"password": "sample-password"})
    (tmp_path / "reset-password").touch()
    second = create_app(tmp_path, services=services)
    client = TestClient(second, cookies=client.cookies)
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/password"


def test_reset_keeps_onboarding_progress(tmp_path, services):
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        set_password(s)
        advance_step(s, "done")
    (tmp_path / "reset-password").touch()
    app2 = create_app(tmp_path, services=services)
    client = TestClient(app2)
    r = client.post(
        "/onboarding/password",
        data={
            "password": "new-password",
            "confirm": "new-password",
            "timezone": "UTC",
        },
        follow_redirects=False,
    )
    # Finished before the reset, so the user goes straight home, not back to setup.
    assert r.headers["location"] == "/"
    assert client.get("/", follow_redirects=False).status_code == 200
    assert (
        client.get("/onboarding/ai", follow_redirects=False).headers["location"] == "/"
    )
    with app2.state.session_factory() as s:
        assert get_setting(s, "onboarding_step") == "done"


def test_no_reset_flag_leaves_the_password(tmp_path, services):
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        set_password(s)
    app2 = create_app(tmp_path, services=services)
    with app2.state.session_factory() as s:
        assert get_setting(s, "password_hash") is not None


def test_the_guard_closes_its_database_session(logged_in_client, app):
    logged_in_client.get("/library", follow_redirects=False)
    TestClient(app).get("/library", follow_redirects=False)
    assert app.state.engine.pool.checkedout() == 0


# --- sessions end with the password ------------------------------------------


def sign_in_to(app, password="sample-password"):
    """A new client logged in with `password`."""
    client = TestClient(app)
    r = client.post("/login", data={"password": password}, follow_redirects=False)
    assert r.status_code == 303
    return client


def test_the_session_cookie_is_named_for_the_app(logged_in_client, app):
    assert "game_taste_session" in logged_in_client.cookies
    assert "session" not in logged_in_client.cookies
    r = TestClient(app).post(
        "/login", data={"password": "sample-password"}, follow_redirects=False
    )
    assert r.headers["set-cookie"].startswith("game_taste_session=")


def test_session_epoch_starts_at_one_and_counts_up(db):
    assert get_session_epoch(db) == "1"
    assert get_setting(db, "session_epoch") is None  # reading doesn't write
    assert bump_session_epoch(db) == "2"
    assert get_session_epoch(db) == "2" and get_setting(db, "session_epoch") == "2"
    assert bump_session_epoch(db) == "3"


def test_a_damaged_session_epoch_counts_as_the_first(db):
    for damaged in ("", "abc", "1.5"):
        set_setting(db, "session_epoch", damaged)
        assert get_session_epoch(db) == "1"
        assert bump_session_epoch(db) == "2"


def test_login_stamps_the_current_epoch(logged_in_client, app, db):
    advance_step(db, "done")
    bump_session_epoch(db)
    r = logged_in_client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/login"  # stamped before the bump
    assert sign_in_to(app).get("/", follow_redirects=False).status_code == 200


def test_first_run_password_stamps_the_epoch(client, db):
    client.post(
        "/onboarding/password",
        data={
            "password": "sample-password",
            "confirm": "sample-password",
            "timezone": "UTC",
        },
    )
    assert client.get("/onboarding/ai", follow_redirects=False).status_code == 200
    bump_session_epoch(db)
    r = client.get("/onboarding/ai", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_a_session_without_an_epoch_is_logged_out(onboarded_client, app):
    # What an older version of the app left in the cookie: logged in, no epoch.
    signer = TimestampSigner(str(app.state.session_secret))
    data = b64encode(json.dumps({"user": True}).encode("utf-8"))
    old = TestClient(app, cookies={"game_taste_session": signer.sign(data).decode()})
    assert old.get("/", follow_redirects=False).headers["location"] == "/login"
    r = old.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.headers["HX-Redirect"] == "/login"
    # The same cookie with the current epoch is fine.
    data = b64encode(json.dumps({"user": True, "epoch": "1"}).encode("utf-8"))
    good = TestClient(app, cookies={"game_taste_session": signer.sign(data).decode()})
    assert good.get("/", follow_redirects=False).status_code == 200


def test_an_old_cookie_does_not_work_after_a_reset(tmp_path, services):
    first = create_app(tmp_path, services=services)
    with first.state.session_factory() as s:
        set_password(s)
        advance_step(s, "done")
    old = sign_in_to(first)
    assert old.get("/", follow_redirects=False).status_code == 200

    (tmp_path / "reset-password").touch()
    second = create_app(tmp_path, services=services)
    fresh = TestClient(second)
    r = fresh.post(
        "/onboarding/password",
        data={"password": "new-password", "confirm": "new-password", "timezone": "UTC"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # The new password is set, yet the cookie from before the reset stays out...
    stale = TestClient(second, cookies=old.cookies)
    assert stale.get("/", follow_redirects=False).headers["location"] == "/login"
    # ...and the browser that set the new password is in.
    assert fresh.get("/", follow_redirects=False).status_code == 200


def test_a_reset_ends_sessions_even_with_the_same_password(tmp_path, services):
    first = create_app(tmp_path, services=services)
    with first.state.session_factory() as s:
        set_password(s)
        advance_step(s, "done")
        before = get_session_epoch(s)
    (tmp_path / "reset-password").touch()
    second = create_app(tmp_path, services=services)
    with second.state.session_factory() as s:
        assert int(get_session_epoch(s)) > int(before)


# --- the password page after a reset -----------------------------------------


RESET_FORM = {
    "password": "new-password",
    "confirm": "new-password",
    "timezone": "Europe/London",
}


def reset_app(tmp_path, services, step="done"):
    """An app whose password was reset after setup got as far as `step`."""
    first = create_app(tmp_path, services=services)
    with first.state.session_factory() as s:
        set_password(s)
        set_setting(s, "timezone", "America/Chicago")
        advance_step(s, step)
    (tmp_path / "reset-password").touch()
    return create_app(tmp_path, services=services)


def test_reset_of_a_finished_setup_asks_for_a_new_password(tmp_path, services):
    page = TestClient(reset_app(tmp_path, services)).get("/onboarding/password")
    assert page.status_code == 200
    assert "Set a new password" in page.text
    assert "Step 1 of 5" not in page.text and "Welcome to Game Taste" not in page.text
    assert "Create login" not in page.text and "Set password" in page.text
    assert page.text.count("em-btn--primary") == 1
    assert page.text.count('type="password"') == 2
    assert "<title>Set a new password - Game Taste</title>" in page.text


def test_first_run_still_welcomes_the_user(client):
    page = client.get("/onboarding/password").text
    assert "Step 1 of 5" in page and "Welcome to Game Taste" in page
    assert "Set a new password" not in page and "Create login" in page


def test_reset_mid_setup_keeps_the_first_run_page_and_resumes(tmp_path, services):
    client = TestClient(reset_app(tmp_path, services, step="steam"))
    page = client.get("/onboarding/password").text
    assert "Step 1 of 5" in page and "Set a new password" not in page
    r = client.post("/onboarding/password", data=RESET_FORM, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/ai"
    # The guard picks up where setup stopped.
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"


def test_the_new_password_after_a_reset_goes_home(tmp_path, services):
    app = reset_app(tmp_path, services)
    client = TestClient(app)
    r = client.post("/onboarding/password", data=RESET_FORM, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    with app.state.session_factory() as s:
        assert verify_password(get_setting(s, "password_hash"), "new-password")
        assert get_setting(s, "onboarding_step") == "done"
    login = TestClient(app).post(
        "/login", data={"password": "new-password"}, follow_redirects=False
    )
    assert login.headers["location"] == "/"


def test_a_reset_keeps_the_time_zone_that_was_chosen(tmp_path, services):
    app = reset_app(tmp_path, services)
    TestClient(app).post("/onboarding/password", data=RESET_FORM)
    with app.state.session_factory() as s:
        assert get_setting(s, "timezone") == "America/Chicago"  # not the browser's


def test_a_refused_new_password_after_a_reset_keeps_the_reset_page(tmp_path, services):
    client = TestClient(reset_app(tmp_path, services))
    r = client.post(
        "/onboarding/password",
        data={"password": "short", "confirm": "short", "timezone": "UTC"},
    )
    assert r.status_code == 422
    assert "Use at least 8 characters." in r.text and "Set a new password" in r.text
    assert "Welcome to Game Taste" not in r.text


# --- the origin check --------------------------------------------------------

DRAFT_URL = "/games/10/review/solo"
DRAFT = {"action": "draft", "text": "Sent from a test"}


def test_a_post_from_another_origin_is_refused(onboarded_client, db, games):
    r = onboarded_client.post(
        DRAFT_URL, data=DRAFT, headers={"Origin": "http://localhost:3000"}
    )
    assert r.status_code == 403
    assert "another site" in r.text and "<html" not in r.text  # short and plain
    assert r.headers["content-type"].startswith("text/plain")
    assert load_form(db, 10, Lane.SOLO)[1] == "new"  # nothing was saved


def test_a_post_from_the_same_host_is_accepted(onboarded_client, db, games):
    r = onboarded_client.post(
        DRAFT_URL, data=DRAFT, headers={"Origin": "http://testserver"}
    )
    assert r.status_code == 200
    assert load_form(db, 10, Lane.SOLO)[0].text == "Sent from a test"


def test_a_post_with_no_origin_or_referer_is_accepted(onboarded_client, games):
    assert onboarded_client.post(DRAFT_URL, data=DRAFT).status_code == 200


def test_the_referer_is_checked_when_there_is_no_origin(onboarded_client, games):
    foreign = {"Referer": "http://evil.example/page"}
    assert (
        onboarded_client.post(DRAFT_URL, data=DRAFT, headers=foreign).status_code == 403
    )
    same = {"Referer": "http://testserver/library?x=1"}
    assert onboarded_client.post(DRAFT_URL, data=DRAFT, headers=same).status_code == 200


def test_the_origin_wins_over_the_referer(onboarded_client, games):
    right = {"Origin": "http://testserver", "Referer": "http://evil.example/"}
    assert (
        onboarded_client.post(DRAFT_URL, data=DRAFT, headers=right).status_code == 200
    )
    wrong = {"Origin": "http://evil.example", "Referer": "http://testserver/"}
    assert (
        onboarded_client.post(DRAFT_URL, data=DRAFT, headers=wrong).status_code == 403
    )


def test_the_port_must_match_too(onboarded_client, games):
    other_port = {"Origin": "http://testserver:8420"}
    r = onboarded_client.post(DRAFT_URL, data=DRAFT, headers=other_port)
    assert r.status_code == 403
    # What counts is the Host the request was sent to.
    same = {"Origin": "http://localhost:8420", "Host": "localhost:8420"}
    assert onboarded_client.post(DRAFT_URL, data=DRAFT, headers=same).status_code == 200


def test_an_origin_that_is_not_a_url_is_refused(onboarded_client, games):
    for origin in ("null", "http://[::1", "not a url", ""):
        r = onboarded_client.post(DRAFT_URL, data=DRAFT, headers={"Origin": origin})
        assert r.status_code == 403, origin


def test_every_unsafe_method_is_checked_and_reading_is_not(onboarded_client, client):
    evil = {"Origin": "http://evil.example"}
    for method in ("post", "put", "patch", "delete"):
        r = getattr(onboarded_client, method)("/settings/region", headers=evil)
        assert r.status_code == 403, method
    for method in ("get", "head", "options"):
        r = getattr(onboarded_client, method)("/healthz", headers=evil)
        assert r.status_code != 403, method
    assert client.get("/healthz", headers=evil).text == "ok"


def test_login_and_logout_posts_from_another_origin_are_refused(logged_in_client, app):
    evil = {"Origin": "http://evil.example"}
    fresh = TestClient(app)
    r = fresh.post("/login", data={"password": "sample-password"}, headers=evil)
    assert r.status_code == 403 and "game_taste_session" not in fresh.cookies
    assert logged_in_client.post("/logout", headers=evil).status_code == 403
    # Still logged in, since the logout never ran.
    r = logged_in_client.get("/onboarding/ai", follow_redirects=False)
    assert r.status_code == 200


def test_the_origin_check_comes_before_the_first_run_page(client):
    r = client.post(
        "/onboarding/password",
        data={"password": "sample-password", "confirm": "sample-password"},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403


# --- no generated API pages --------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_off(onboarded_client, path):
    assert onboarded_client.get(path, follow_redirects=False).status_code == 404
