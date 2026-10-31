"""Refresh now and the refresh status: the button, the status that polls itself while a refresh is
pending, and the event that tells the Profiles pages to load what the refresh made."""

import html
import re
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FIXED_NOW
from fakes import profile_reply
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.jobs import enqueue_job, recover_interrupted_jobs, run_pending_jobs
from app.models import Job, RefreshRun
from app.services.refresh_status import refresh_status
from app.services.reviews import Lane, ReviewForm, publish
from app.services.secrets import hash_password
from app.services.settings import set_setting

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"
TRIGGER = "profiles-refreshed"


# --- helpers ----------------------------------------------------------------


def set_job(db, job, **values):
    db.execute(update(Job).where(Job.id == job.id).values(**values))
    db.commit()
    db.expire_all()


def queued_job(db):
    return enqueue_job(db, "refresh", "user", FIXED_NOW)


def running_job(db):
    job = queued_job(db)
    set_job(db, job, status="running", started_at=FIXED_NOW)
    return job


def finished_job(db, *, status="done", error=None, finished_at=FIXED_NOW):
    job = queued_job(db)
    set_job(db, job, status=status, error=error, finished_at=finished_at)
    return job


def detail(label, outcome, versions=(), message="Learned from new reviews."):
    return {
        "key": f"lane:{label.lower()}",
        "label": label,
        "outcome": outcome,
        "versions": list(versions),
        "message": message,
    }


def add_run(db, job, details=(), steam_summary="Steam synced: 0 new, 2 total."):
    db.add(
        RefreshRun(
            job_id=job.id,
            started_at=FIXED_NOW,
            finished_at=FIXED_NOW,
            steam_summary=steam_summary,
            # The stored counters are not what the status shows (see the double-count test).
            profiles_updated=99,
            profiles_failed=99,
            details=list(details),
        )
    )
    db.commit()


def status_text(client, url="/refresh/status"):
    return html.unescape(client.get(url).text)


def root(text):
    """The opening tag of the status area's root element."""
    return re.search(r'<div[^>]*id="refresh-status"[^>]*>', text)[0]


def button(text):
    return re.search(r"<button[^>]*>\s*<span[^>]*>Refresh now</span>", text)[0]


def is_disabled(tag):
    """Whether the button tag has the `disabled` attribute (`hx-disabled-elt` is not it)."""
    return re.search(r"\sdisabled[\s=>]", tag) is not None


# --- the brief's cases ------------------------------------------------------


def test_refresh_now_enqueues_one_job(onboarded_client, db):  # Review focus 3
    onboarded_client.post("/refresh")
    r = onboarded_client.post("/refresh")
    assert r.status_code == 200 and "Refresh queued." in r.text
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_status_polls_while_pending_and_triggers_when_done(
    onboarded_client, app, db, games, saved_ai_setup, fake_ai
):
    v = publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = profile_reply(
        loved=[{"text": "ok", "tag": "stated", "reviews": [v.id], "games": [10]}]
    )
    assert 'hx-trigger="every 2s"' in onboarded_client.post("/refresh").text
    run_pending_jobs(app)
    r = onboarded_client.get("/refresh/status?was=queued")
    assert r.headers.get(
        "HX-Trigger"
    ) == "profiles-refreshed" and "1 profile updated." in html.unescape(r.text)
    assert "every 2s" not in r.text


def test_failed_profiles_are_explained(
    onboarded_client, app, db, games, saved_ai_setup, fake_ai
):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = "nope"
    onboarded_client.post("/refresh")
    run_pending_jobs(app)
    text = html.unescape(onboarded_client.get("/refresh/status").text)
    assert "1 couldn't be updated." in text
    assert (
        "The model's reply wasn't in the expected format. "
        "Try again, or pick a different Everyday tasks model in Settings." in text
    )


def test_interrupted_job_message_shows(onboarded_client, db):  # Review focus 2
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    db.execute(update(Job).where(Job.id == job.id).values(status="running"))
    db.commit()
    recover_interrupted_jobs(db, FIXED_NOW)
    assert "Interrupted by a restart." in onboarded_client.get("/refresh/status").text


def test_refresh_now_on_settings_and_profiles(onboarded_client):
    for path in ("/settings", "/profiles", "/profiles/lane/solo"):
        assert "Refresh now" in onboarded_client.get(path).text


def test_refresh_needs_login(app):
    assert TestClient(app).post("/refresh", follow_redirects=False).status_code == 303


# --- the status in each state -----------------------------------------------


def test_never_run_says_when_the_daily_refresh_runs(onboarded_client, db):
    text = status_text(onboarded_client)
    assert "No refresh yet. The daily refresh runs at 04:00." in text
    set_setting(db, "refresh_time", "22:15")
    assert "The daily refresh runs at 22:15." in status_text(onboarded_client)


def test_queued_and_running_copy_and_polling(onboarded_client, db):
    job = queued_job(db)
    r = onboarded_client.get("/refresh/status")
    assert "Refresh queued." in r.text and "Refreshing profiles." not in r.text
    tag = root(r.text)
    assert 'hx-get="/refresh/status?was=queued"' in tag
    assert 'hx-trigger="every 2s"' in tag and 'hx-swap="outerHTML"' in tag
    set_job(db, job, status="running", started_at=FIXED_NOW)
    r = onboarded_client.get("/refresh/status?was=queued")
    assert "Refreshing profiles." in r.text and "Refresh queued." not in r.text
    assert 'hx-get="/refresh/status?was=running"' in root(r.text)


def test_idle_status_carries_no_polling_and_keeps_its_id(onboarded_client, db):
    finished_job(db)
    tag = root(onboarded_client.get("/refresh/status").text)
    assert "hx-get" not in tag and "hx-trigger" not in tag and "every" not in tag
    # The id is the same in every state, so an outerHTML swap always finds its target.
    queued = root(onboarded_client.post("/refresh").text)
    assert 'id="refresh-status"' in queued


@pytest.mark.parametrize(
    ("updated", "expected"),
    [
        (0, "Last refresh just now: 0 profiles updated."),
        (1, "Last refresh just now: 1 profile updated."),
        (2, "Last refresh just now: 2 profiles updated."),
    ],
)
def test_done_run_counts_the_profiles_updated(onboarded_client, db, updated, expected):
    add_run(
        db,
        finished_job(db),
        [detail(f"Lane {n}", "updated", [n + 1]) for n in range(updated)],
    )
    text = status_text(onboarded_client)
    assert expected in text and "couldn't be updated" not in text
    assert (
        'class="refresh-status__failures"'
        not in onboarded_client.get("/refresh/status").text
    )


def test_done_run_shows_the_steam_summary_as_a_caption(onboarded_client, db):
    add_run(db, finished_job(db), [], steam_summary="Steam synced: 3 new, 7 total.")
    text = onboarded_client.get("/refresh/status").text
    caption = re.search(r"<p[^>]*text-caption[^>]*>([^<]*)</p>", text)
    assert caption and caption[1].strip() == "Steam synced: 3 new, 7 total."


def test_a_done_job_without_a_run_still_reads_calmly(onboarded_client, db):
    finished_job(db)
    text = status_text(onboarded_client)
    assert "Last refresh just now: 0 profiles updated." in text


def test_failed_profiles_are_listed_with_their_messages(onboarded_client, db):
    add_run(
        db,
        finished_job(db),
        [
            detail("Solo", "updated", [2]),
            detail(
                "Two players",
                "failed",
                [],
                "The model's update didn't pass the checks.",
            ),
        ],
    )
    text = status_text(onboarded_client)
    assert "Last refresh just now: 1 profile updated. 1 couldn't be updated." in text
    assert "Two players: The model's update didn't pass the checks." in text
    assert "Versions up to" not in text


def test_a_failed_profile_that_saved_versions_counts_once_as_failed(
    onboarded_client, db
):
    """The first batch saved versions 2 and 3, the next failed: failed, not also updated."""
    add_run(
        db,
        finished_job(db),
        [detail("Solo", "failed", [2, 3], "The reply wasn't usable.")],
    )
    text = status_text(onboarded_client)
    assert "Last refresh just now: 0 profiles updated. 1 couldn't be updated." in text
    assert "Solo: The reply wasn't usable. Versions up to 3 were saved." in text


def test_the_stored_counters_are_not_what_the_status_shows(onboarded_client, db):
    add_run(db, finished_job(db), [detail("Solo", "updated", [2])])
    assert "1 profile updated." in status_text(onboarded_client)
    assert "99" not in status_text(onboarded_client)


def test_failed_job_says_when_and_why(onboarded_client, db):
    finished_job(
        db,
        status="failed",
        error="The refresh stopped unexpectedly: OSError. Try again.",
    )
    text = status_text(onboarded_client)
    assert (
        "The last refresh failed just now. "
        "The refresh stopped unexpectedly: OSError. Try again."
    ) in text
    assert "Last refresh" not in text


def test_the_newest_refresh_decides_the_status(onboarded_client, db):
    add_run(db, finished_job(db), [detail("Solo", "updated", [2])])
    finished_job(db, status="failed", error="Interrupted by a restart.")
    text = status_text(onboarded_client)
    assert "The last refresh failed" in text and "profile updated" not in text


def test_old_times_are_shown_in_the_users_timezone(onboarded_client, db):
    set_setting(db, "timezone", "UTC")
    finished_job(db, finished_at=FIXED_NOW - timedelta(days=3))
    assert "Last refresh Jan 12, 12:00 PM:" in status_text(onboarded_client)


def test_status_text_is_escaped(onboarded_client, db):
    evil = "<script>alert(1)</script>"
    add_run(db, finished_job(db), [detail(evil, "failed", [], evil)])
    text = onboarded_client.get("/refresh/status").text
    assert evil not in text
    assert text.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 2


def test_a_failed_jobs_error_is_escaped(onboarded_client, db):
    finished_job(db, status="failed", error="<b>bold</b>")
    text = onboarded_client.get("/refresh/status").text
    assert "<b>" not in text and "&lt;b&gt;bold&lt;/b&gt;" in text


def test_templates_never_mark_text_as_safe():
    for name in ("partials/refresh_status.html", "profiles.html", "profile.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe|Markup\(|autoescape\s+false", source), name


# --- the button -------------------------------------------------------------


def test_the_button_is_secondary_and_posts_to_refresh(onboarded_client):
    tag = button(onboarded_client.get("/refresh/status").text)
    assert "em-btn--secondary" in tag and "em-btn--primary" not in tag
    assert 'hx-post="/refresh"' in tag
    assert 'hx-target="#refresh-status"' in tag and 'hx-swap="outerHTML"' in tag
    assert 'type="button"' in tag


@pytest.mark.parametrize("make", [queued_job, running_job])
def test_the_button_is_disabled_while_a_refresh_is_pending(onboarded_client, db, make):
    make(db)
    tag = button(onboarded_client.get("/refresh/status").text)
    assert is_disabled(tag) and 'aria-disabled="true"' in tag


def test_the_button_is_enabled_when_idle(onboarded_client, db):
    for make in (
        lambda: None,  # never run
        lambda: finished_job(db),
        lambda: finished_job(db, status="failed", error="Interrupted by a restart."),
    ):
        make()
        tag = button(onboarded_client.get("/refresh/status").text)
        assert not is_disabled(tag) and "aria-disabled" not in tag


def test_pressing_the_button_returns_a_disabled_button_and_wakes_the_worker(
    onboarded_client, app
):
    app.state.job_wakeup.clear()
    r = onboarded_client.post("/refresh")
    assert r.status_code == 200 and "Refresh queued." in r.text
    assert 'aria-disabled="true"' in button(r.text)
    assert app.state.job_wakeup.is_set()


def test_pressing_it_again_while_running_shows_the_running_refresh(
    onboarded_client, db
):
    running_job(db)
    r = onboarded_client.post("/refresh")
    assert r.status_code == 200 and "Refreshing profiles." in r.text
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_refresh_now_records_who_asked(onboarded_client, db):
    onboarded_client.post("/refresh")
    job = db.scalars(select(Job)).one()
    assert (job.kind, job.status, job.requested_by) == ("refresh", "queued", "user")


# --- the event that reloads the profiles ------------------------------------


@pytest.mark.parametrize("was", ["queued", "running"])
def test_idle_after_pending_sends_the_event(onboarded_client, db, was):
    finished_job(db)
    r = onboarded_client.get(f"/refresh/status?was={was}")
    assert r.headers.get("HX-Trigger") == TRIGGER


def test_failing_after_pending_sends_the_event_too(onboarded_client, db):
    finished_job(db, status="failed", error="Interrupted by a restart.")
    assert (
        onboarded_client.get("/refresh/status?was=running").headers.get("HX-Trigger")
        == TRIGGER
    )


@pytest.mark.parametrize("was", ["", "?was=idle", "?was=nonsense", "?was="])
def test_idle_without_a_pending_before_sends_no_event(onboarded_client, db, was):
    finished_job(db)
    r = onboarded_client.get(f"/refresh/status{was}")
    assert "HX-Trigger" not in r.headers


@pytest.mark.parametrize("make", [queued_job, running_job])
def test_still_pending_sends_no_event(onboarded_client, db, make):
    make(db)
    r = onboarded_client.get("/refresh/status?was=queued")
    assert "HX-Trigger" not in r.headers


def test_posting_sends_no_event_while_the_refresh_is_pending(onboarded_client):
    assert "HX-Trigger" not in onboarded_client.post("/refresh").headers


def test_posting_sends_the_event_when_the_refresh_is_already_over(
    onboarded_client, app, monkeypatch
):
    """A refresh with nothing to do can end before the answer is drawn. Nothing polls for it
    then, so the answer says so itself."""
    monkeypatch.setattr(
        "app.routes.refresh.notify_worker", lambda app: run_pending_jobs(app)
    )
    r = onboarded_client.post("/refresh")
    assert r.status_code == 200 and "Last refresh just now" in html.unescape(r.text)
    assert "every 2s" not in r.text
    assert r.headers.get("HX-Trigger") == TRIGGER


def test_the_status_routes_need_login(app, db):
    set_setting(db, "password_hash", hash_password("sample-password"))
    client = TestClient(app)
    for response in (
        client.get("/refresh/status", follow_redirects=False),
        client.post("/refresh", follow_redirects=False),
    ):
        assert response.status_code == 303 and response.headers["location"] == "/login"
    assert db.scalar(select(func.count()).select_from(Job)) == 0


# --- the service ------------------------------------------------------------


def test_refresh_status_is_idle_with_nothing(app, db):
    status = refresh_status(db)
    assert (status.state, status.job, status.run) == ("idle", None, None)


def test_refresh_status_states(app, db):
    job = queued_job(db)
    assert refresh_status(db).state == "queued" and refresh_status(db).job.id == job.id
    set_job(db, job, status="running")
    assert refresh_status(db).state == "running"
    set_job(db, job, status="done", finished_at=FIXED_NOW)
    status = refresh_status(db)
    assert (status.state, status.job.id, status.run) == ("idle", job.id, None)


def test_refresh_status_pairs_the_newest_job_with_its_run(app, db):
    old = finished_job(db)
    add_run(db, old, [detail("Solo", "updated", [2])])
    new = finished_job(db)
    add_run(db, new, [detail("Two players", "failed", [], "No.")])
    status = refresh_status(db)
    assert status.job.id == new.id and status.run.job_id == new.id
    assert (status.updated, status.failed) == (0, 1)


def test_refresh_status_ignores_other_kinds_of_job(app, db):
    enqueue_job(db, "research", "user", FIXED_NOW)
    assert refresh_status(db).state == "idle"
    assert refresh_status(db).job is None


# --- where it is -------------------------------------------------------------


def test_settings_has_one_status_area_below_the_schedule_form(onboarded_client):
    text = onboarded_client.get("/settings").text
    assert (
        text.count('id="refresh-status"') == 1 and text.count('id="refresh-area"') == 1
    )
    schedule = text.index('id="settings-schedule"')
    assert (
        schedule
        < text.index('id="refresh-area"')
        < text.index("settings-account-title")
    )
    assert "em-btn--primary" not in text


def test_saving_the_schedule_does_not_swap_the_status_area(onboarded_client, db):
    queued_job(db)
    r = onboarded_client.post(
        "/settings/schedule",
        data={"refresh_time": "05:30", "timezone": "UTC"},
        headers={"HX-Request": "true"},
    )
    assert "Schedule saved." in r.text and "refresh-status" not in r.text


def test_settings_shows_a_pending_refresh_and_keeps_polling(onboarded_client, db):
    running_job(db)
    text = onboarded_client.get("/settings").text
    assert "Refreshing profiles." in text
    assert 'hx-get="/refresh/status?was=running"' in root(text)


def test_profile_pages_show_the_status_inside_the_refresh_area(onboarded_client, db):
    queued_job(db)
    for url in ("/profiles", "/profiles/lane/solo", "/profiles/lane/two"):
        text = onboarded_client.get(url).text
        assert text.count('id="refresh-area"') == text.count('id="refresh-status"') == 1
        area = text.index('id="refresh-area"')
        # The status, its message and its button come in this order, all after the area opens.
        assert area < text.index('id="refresh-status"'), url
        assert text.index('id="refresh-status"') < text.index("Refresh queued."), url
        assert text.index("Refresh queued.") < text.index('aria-disabled="true"'), url


def test_the_profiles_list_reloads_its_cards_on_the_event(onboarded_client):
    text = onboarded_client.get("/profiles").text
    cards = re.search(r'<ul[^>]*id="profile-cards"[^>]*>', text)[0]
    assert 'hx-get="/profiles"' in cards
    assert 'hx-trigger="profiles-refreshed from:body"' in cards
    assert 'hx-select="#profile-cards"' in cards and 'hx-swap="outerHTML"' in cards


def test_a_profile_body_reloads_the_current_page_on_the_event(
    onboarded_client, db, games, saved_ai_setup, fake_ai
):
    body = profile_body_tag(onboarded_client, db, fake_ai)
    # An empty address is the page the browser is on, so a version picked from the select
    # (which pushes its URL) is kept.
    assert 'hx-get=""' in body
    assert 'hx-trigger="profiles-refreshed from:body"' in body
    assert 'hx-swap="outerHTML"' in body


def profile_body_tag(client, db, fake_ai):
    v = publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = profile_reply(
        loved=[{"text": "ok", "tag": "stated", "reviews": [v.id], "games": [10]}]
    )
    client.post("/refresh")
    run_pending_jobs(client.app)
    return re.search(
        r'<div[^>]*id="profile-body"[^>]*>', client.get("/profiles/lane/solo").text
    )[0]


def test_the_version_form_does_not_inherit_the_reload_settings(
    onboarded_client, db, games, saved_ai_setup, fake_ai
):
    profile_body_tag(onboarded_client, db, fake_ai)
    text = onboarded_client.get("/profiles/lane/solo").text
    form = re.search(r"<form[^>]*>", text)[0]
    # The form names its own target, swap and (not) select; history stays off for it.
    assert 'hx-target="#profile-body"' in form and 'hx-swap="outerHTML"' in form
    assert 'hx-history="false"' in form
    assert "hx-select" not in form


def refresh_with_a_new_review(client, db, app, fake_ai, appid, changelog):
    """Publish a review of `appid` and refresh, replying with a profile that cites it."""
    v = publish(db, appid, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = profile_reply(
        changelog,
        loved=[{"text": "ok", "tag": "stated", "reviews": [v.id], "games": [appid]}],
    )
    client.post("/refresh")
    run_pending_jobs(app)
    assert client.get("/refresh/status?was=running").headers["HX-Trigger"] == TRIGGER


def changelog_text(client, url):
    """The Changelog as the listener's request gets it: the body fragment of the current URL."""
    r = client.get(url, headers={"HX-Request": "true"})
    assert "<html" not in r.text and 'id="profile-body"' in r.text
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", r.text)).split())


def test_the_reloaded_body_has_the_new_versions_changelog_entry(
    onboarded_client, app, db, games, saved_ai_setup, fake_ai
):
    url = "/profiles/lane/solo"
    refresh_with_a_new_review(onboarded_client, db, app, fake_ai, 10, "First read.")
    first = changelog_text(onboarded_client, url)
    assert "Version 1 of 1" in first and "Changelog" in first
    assert "Version 1 · just now: First read." in first

    refresh_with_a_new_review(onboarded_client, db, app, fake_ai, 20, "Added Tactics.")
    # The page was on the newest version (no ?version=) and moves to the new one, whose entry
    # is in the changelog beside the old one.
    second = changelog_text(onboarded_client, url)
    assert "Version 2 of 2" in second
    assert "Version 2 · just now: Added Tactics." in second
    assert "Version 1 · just now: First read." in second
    assert second.index("Version 2 · just now") < second.index("Version 1 · just now")
    # A page the user had left on version 1 stays there, with the new entry in its changelog.
    kept = changelog_text(onboarded_client, url + "?version=1")
    assert "Version 1 of 2" in kept and "Version 2 · just now: Added Tactics." in kept


def test_the_first_version_brings_its_changelog_to_a_page_that_had_none(
    onboarded_client, app, db, games, saved_ai_setup, fake_ai
):
    """The empty page asks for `#profile-body`; what it gets back now holds the changelog."""
    before = onboarded_client.get("/profiles/lane/solo").text
    assert "Changelog" not in before
    refresh_with_a_new_review(onboarded_client, db, app, fake_ai, 10, "First read.")
    assert "Version 1 · just now: First read." in changelog_text(
        onboarded_client, "/profiles/lane/solo"
    )


def test_the_body_fragment_has_no_status_area(
    onboarded_client, db, games, saved_ai_setup, fake_ai
):
    """Choosing a version swaps just the body; the status in the header is not part of it."""
    profile_body_tag(onboarded_client, db, fake_ai)
    r = onboarded_client.get(
        "/profiles/lane/solo?version=1", headers={"HX-Request": "true"}
    )
    assert 'id="profile-body"' in r.text
    assert "refresh-status" not in r.text and "Refresh now" not in r.text


def test_a_page_with_no_version_reloads_when_the_refresh_ends(onboarded_client):
    text = onboarded_client.get("/profiles/lane/solo").text
    empty = re.search(r'<div[^>]*class="em-empty"[^>]*>', text)[0]
    assert 'id="profile-body"' in empty and 'hx-get=""' in empty
    assert 'hx-trigger="profiles-refreshed from:body"' in empty
    assert 'hx-select="#profile-body"' in empty and 'hx-swap="outerHTML"' in empty


# --- the progress bar ---------------------------------------------------------


def progress_run(db, job, total=None, checked=0, steam_summary=""):
    """The run of a refresh that is still going, as far as it has got."""
    db.add(
        RefreshRun(
            job_id=job.id,
            started_at=FIXED_NOW,
            steam_summary=steam_summary,
            profiles_total=total,
            profiles_checked=checked,
        )
    )
    db.commit()


def progress_bar(text):
    """The progress bar's element, or None when the status shows none."""
    found = re.search(r'<div[^>]*role="progressbar".*?</div>\s*</div>', text, re.S)
    return found[0] if found else None


def bar_text(bar):
    return " ".join(re.sub(r"<[^>]+>", " ", bar).split())


def test_a_queued_refresh_shows_a_bar_that_is_waiting(onboarded_client, db):
    queued_job(db)
    bar = progress_bar(status_text(onboarded_client))
    assert bar and "is-indeterminate" in bar and "aria-valuenow" not in bar
    assert bar_text(bar) == "Refresh queued."


def test_a_refresh_that_just_started_shows_a_moving_bar(onboarded_client, db):
    running_job(db)
    bar = progress_bar(status_text(onboarded_client))
    assert "is-indeterminate" in bar and bar_text(bar) == "Refreshing profiles."


def test_the_steam_sync_shows_a_moving_bar(onboarded_client, db):
    progress_run(db, running_job(db))
    bar = progress_bar(status_text(onboarded_client))
    assert "is-indeterminate" in bar and "aria-valuenow" not in bar
    assert bar_text(bar) == "Syncing your Steam library."


def test_the_profiles_fill_the_bar_as_they_are_checked(onboarded_client, db):
    progress_run(db, running_job(db), total=5, checked=2, steam_summary="Steam synced.")
    bar = progress_bar(status_text(onboarded_client))
    assert "is-indeterminate" not in bar
    assert 'aria-valuemin="0"' in bar and 'aria-valuemax="5"' in bar
    assert 'aria-valuenow="2"' in bar and 'aria-valuetext="2 of 5"' in bar
    assert 'style="width: 40.0%"' in bar
    assert bar_text(bar) == "Updating profiles. 2 of 5"


def test_a_bar_never_runs_past_full(onboarded_client, db):
    progress_run(db, running_job(db), total=2, checked=3)
    bar = progress_bar(status_text(onboarded_client))
    assert 'aria-valuenow="2"' in bar and 'style="width: 100.0%"' in bar


def test_no_profiles_to_check_shows_a_moving_bar(onboarded_client, db):
    progress_run(db, running_job(db), total=0)
    bar = progress_bar(status_text(onboarded_client))
    assert "is-indeterminate" in bar and bar_text(bar) == "Updating profiles."


def test_an_idle_status_has_no_bar(onboarded_client, db):
    assert progress_bar(status_text(onboarded_client)) is None
    job = running_job(db)
    progress_run(db, job, total=3, checked=3)
    set_job(db, job, status="done", finished_at=FIXED_NOW)
    assert progress_bar(status_text(onboarded_client)) is None


def test_the_bar_is_the_ember_progress_component(onboarded_client, db):
    progress_run(db, running_job(db), total=4, checked=1)
    bar = progress_bar(status_text(onboarded_client))
    assert 'class="em-progress em-progress--ember' in bar
    assert '<div class="em-progress__track"><span class="em-progress__fill"' in bar
    assert 'aria-label="Updating profiles."' in bar


def test_every_page_with_the_status_shows_the_bar(onboarded_client, db):
    progress_run(db, running_job(db), total=4, checked=1)
    for url in ("/settings", "/profiles", "/profiles/lane/solo"):
        text = html.unescape(onboarded_client.get(url).text)
        # Settings has other bars (the AI check's, Steam's) before the status area.
        bar = progress_bar(text[text.index('id="refresh-status"') :])
        assert bar and "1 of 4" in bar, url
