import pytest
from fastapi.testclient import TestClient

from app.auth import ONBOARDING_STEPS, advance_step, current_step
from app.main import create_app
from app.services.secrets import hash_password, verify_password
from app.services.settings import get_secret, get_setting, set_secret, set_setting


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


def test_current_and_earlier_steps_stay_reachable(logged_in_client, db):
    advance_step(db, "people")
    # No page exists for these yet (later tasks add them), so a pass-through is a 404.
    for path in ("/onboarding/ai", "/onboarding/steam", "/onboarding/people"):
        r = logged_in_client.get(path, follow_redirects=False)
        assert "location" not in r.headers, path
        assert r.status_code == 404, path


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


def test_finished_user_reaches_other_pages(onboarded_client):
    # Nothing is mounted at /library yet; a 404 means the guard let the request through.
    r = onboarded_client.get("/library", follow_redirects=False)
    assert r.status_code == 404 and "location" not in r.headers


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
    assert r.headers["location"] == "/onboarding/ai"
    # Finished before the reset, so the guard sends the user straight home.
    assert (
        client.get("/onboarding/ai", follow_redirects=False).headers["location"] == "/"
    )


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
