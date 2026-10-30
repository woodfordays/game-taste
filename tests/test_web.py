import io
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FIXED_NOW, fake_request
from starlette.datastructures import FormData, UploadFile
from starlette.responses import Response

from app.services.settings import set_setting
from app.web import hours, lane_label, redirect, render, templates, typed, when


def test_static_assets_served(client):
    assert "--ember-500" in client.get("/static/ember/tokens.css").text
    assert client.get("/static/ember/bundle.css").status_code == 200
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert (
        client.get("/static/ember/fonts/InstrumentSans-Variable.woff2").status_code
        == 200
    )
    assert client.get("/static/ember/icons/moon.svg").status_code == 200


def test_htmx_is_javascript_of_plausible_size(client):
    body = client.get("/static/vendor/htmx.min.js").content
    assert 40_000 < len(body) < 80_000
    assert b"htmx" in body


@pytest.mark.parametrize(
    "cookie,expected", [(None, "dark"), ("light", "light"), ("bogus", "dark")]
)
def test_theme_from_cookie(app, cookie, expected):
    html = render(
        fake_request(app, cookies={"theme": cookie} if cookie else {}), "base.html"
    ).body.decode()
    assert f'data-theme="{expected}"' in html


def test_theme_toggle_label_follows_theme(app):
    dark = render(fake_request(app), "base.html").body.decode()
    light = render(
        fake_request(app, cookies={"theme": "light"}), "base.html"
    ).body.decode()
    assert 'aria-label="Switch to light theme"' in dark
    assert 'aria-label="Switch to dark theme"' in light


def test_theme_toggle_is_an_inline_ghost_icon_button(app):
    html = render(fake_request(app), "base.html").body.decode()
    assert "em-btn--ghost" in html and "em-btn--icon" in html
    # The moon is inlined so it takes currentColor, not loaded as an <img>.
    assert 'stroke="currentColor"' in html
    assert "M19.5 14.2A7.8 7.8 0 1 1 9.8 4.5a6.3 6.3 0 0 0 9.7 9.7z" in html
    assert "<img" not in html
    assert "document.documentElement.dataset.theme" in html
    assert "path=/; max-age=31536000; samesite=lax" in html


def test_base_shell_structure(app):
    html = render(fake_request(app), "base.html").body.decode()
    assert '<div id="dialog"></div>' in html
    assert 'id="toasts"' in html
    for asset in (
        "/static/ember/tokens.css",
        "/static/ember/bundle.css",
        "/static/app.css",
        "/static/vendor/htmx.min.js",
    ):
        assert asset in html
    assert "hx-headers=" in html
    assert "Game Taste" in html and "text-subtitle" in html


def test_side_nav_lists_four_destinations(app):
    html = render(fake_request(app), "base.html").body.decode()
    for href, label in [
        ("/", "Home"),
        ("/library", "Library"),
        ("/reviews", "Reviews"),
        ("/settings", "Settings"),
    ]:
        assert f'href="{href}"' in html
        assert f">{label}<" in html
    assert 'aria-current="page"' not in html


@pytest.mark.parametrize(
    "section,href",
    [
        ("home", "/"),
        ("library", "/library"),
        ("reviews", "/reviews"),
        ("settings", "/settings"),
    ],
)
def test_side_nav_marks_current_section(app, section, href):
    html = render(fake_request(app), "base.html", nav=section).body.decode()
    assert html.count('aria-current="page"') == 1
    current = html.split('aria-current="page"')[0].rsplit("<a ", 1)[1]
    assert f'href="{href}"' in current
    assert "is-current" in current


def test_render_adds_context_and_honors_status(app, db):
    set_setting(db, "timezone", "America/New_York")
    response = render(
        fake_request(app, cookies={"theme": "light"}),
        "base.html",
        status_code=404,
        nav="library",
    )
    assert response.status_code == 404
    assert response.context["theme"] == "light"
    assert response.context["nav"] == "library"
    assert response.context["tz"] == "America/New_York"
    assert response.context["now"] == FIXED_NOW


def test_render_defaults(app):
    response = render(fake_request(app), "base.html")
    assert response.status_code == 200
    assert response.context["theme"] == "dark"
    assert response.context["nav"] is None
    assert response.context["tz"] == "UTC"
    assert response.context["now"] == FIXED_NOW


def test_render_closes_its_session(app):
    # A short-lived session: render() must not leave a connection checked out.
    render(fake_request(app), "base.html")
    assert app.state.engine.pool.checkedout() == 0


def test_redirect_is_303_for_plain_requests(app):
    response = redirect(fake_request(app), "/library")
    assert response.status_code == 303
    assert response.headers["location"] == "/library"


def test_redirect_uses_hx_redirect_for_htmx(app):
    response = redirect(fake_request(app, headers={"HX-Request": "true"}), "/login")
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == "/login"
    assert "location" not in response.headers
    assert isinstance(response, Response)


def test_when_and_hours():
    tz = "America/New_York"
    assert when(FIXED_NOW - timedelta(seconds=30), FIXED_NOW, tz) == "just now"
    assert when(FIXED_NOW - timedelta(minutes=4), FIXED_NOW, tz) == "4 min ago"
    assert when(FIXED_NOW - timedelta(hours=3), FIXED_NOW, tz) == "3 h ago"
    assert (
        when(datetime(2026, 1, 8, 14, 42, tzinfo=timezone.utc), FIXED_NOW, tz)
        == "Jan 8, 9:42 AM"
    )
    assert (
        when(datetime(2025, 10, 8, 13, 42, tzinfo=timezone.utc), FIXED_NOW, tz)
        == "Oct 8, 2025, 9:42 AM"
    )
    assert (
        hours(65994) == "1,099.9 h"
        and hours(600) == "10.0 h"
        and hours(None) == "0.0 h"
    )


def test_when_boundaries():
    tz = "UTC"
    assert when(FIXED_NOW - timedelta(seconds=59), FIXED_NOW, tz) == "just now"
    assert when(FIXED_NOW - timedelta(seconds=60), FIXED_NOW, tz) == "1 min ago"
    assert (
        when(FIXED_NOW - timedelta(minutes=59, seconds=59), FIXED_NOW, tz)
        == "59 min ago"
    )
    assert when(FIXED_NOW - timedelta(hours=1), FIXED_NOW, tz) == "1 h ago"
    assert (
        when(FIXED_NOW - timedelta(hours=23, minutes=59), FIXED_NOW, tz) == "23 h ago"
    )
    assert when(FIXED_NOW - timedelta(hours=24), FIXED_NOW, tz) == "Jan 14, 12:00 PM"


def test_when_is_not_zero_padded_and_uses_12_hour_clock():
    tz = "UTC"
    assert (
        when(datetime(2026, 1, 3, 0, 5, tzinfo=timezone.utc), FIXED_NOW, tz)
        == "Jan 3, 12:05 AM"
    )
    assert (
        when(datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc), FIXED_NOW, tz)
        == "Jan 3, 12:00 PM"
    )
    assert (
        when(datetime(2026, 1, 3, 23, 59, tzinfo=timezone.utc), FIXED_NOW, tz)
        == "Jan 3, 11:59 PM"
    )
    assert (
        when(datetime(2026, 1, 3, 1, 7, tzinfo=timezone.utc), FIXED_NOW, tz)
        == "Jan 3, 1:07 AM"
    )


def test_when_shows_the_year_in_the_users_timezone():
    # 2026-01-01 03:00 UTC is still 2025 in New York.
    dt = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert when(dt, FIXED_NOW, "America/New_York") == "Dec 31, 2025, 10:00 PM"
    assert when(dt, FIXED_NOW, "UTC") == "Jan 1, 3:00 AM"


def test_when_falls_back_to_utc_for_an_unknown_timezone():
    dt = datetime(2026, 1, 8, 14, 42, tzinfo=timezone.utc)
    assert when(dt, FIXED_NOW, "Not/AZone") == "Jan 8, 2:42 PM"


def test_hours_rounding_and_separators():
    assert hours(0) == "0.0 h"
    assert hours(90) == "1.5 h"
    assert hours(6_000_000) == "100,000.0 h"


def test_lane_label():
    assert lane_label("solo") == "Solo"
    assert lane_label("two") == "Two players"
    assert lane_label("three_four") == "Three to four"
    assert lane_label("five_plus") == "Five or more"


def test_filters_are_registered_and_use_context_now_and_tz():
    template = templates.env.from_string(
        "{{ dt|when }} | {{ m|hours }} | {{ lane|lane_label }}"
    )
    out = template.render(
        dt=FIXED_NOW - timedelta(minutes=4),
        now=FIXED_NOW,
        tz="America/New_York",
        m=600,
        lane="two",
    )
    assert out == "4 min ago | 10.0 h | Two players"
    absolute = templates.env.from_string("{{ dt|when }}").render(
        dt=datetime(2026, 1, 8, 14, 42, tzinfo=timezone.utc),
        now=FIXED_NOW,
        tz="America/New_York",
    )
    assert absolute == "Jan 8, 9:42 AM"


def test_templates_autoescape():
    out = templates.env.from_string("{{ text }}").render(text="<b>Sam & Alex</b>")
    assert out == "&lt;b&gt;Sam &amp; Alex&lt;/b&gt;"


def test_typed_strips_the_whitespace_a_paste_leaves():
    form = FormData([("name", "  Sam \n"), ("empty", ""), ("inner", "a  b")])
    assert typed(form, "name") == "Sam"
    assert typed(form, "inner") == "a  b"
    assert typed(form, "empty") == "" and typed(form, "missing") == ""


def test_typed_ignores_a_file_upload():
    upload = UploadFile(file=io.BytesIO(b"x"), filename="x.txt")
    assert typed(FormData([("name", upload)]), "name") == ""
