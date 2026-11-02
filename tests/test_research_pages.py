"""The Research page: starting a run from the dialog, the live log and its polling, the pause
choices, the past runs, and the side nav entries."""

import html
import re
from pathlib import Path

import pytest
from fakes import FIXED_NOW, fill_lane, verdicts
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from app.jobs import run_pending_jobs
from app.models import JobAssignment, ResearchChoice, ResearchRun
from app.services.research_runs import (
    add_event,
    cancel_research,
    research_status,
    run_targets,
)
from app.services.reviews import Lane

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"
NO_PROFILE = "No profile yet. Publish a review played at this head count first."


# --- helpers ----------------------------------------------------------------


def paused_run(client, app, db, research_world):
    """A run paused after round 3 with Solo still short (the judge rejects everything)."""
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    client.post("/research", data={})
    run_pending_jobs(app)
    db.expire_all()
    return db.scalar(select(ResearchRun))


def start_run(client, app, db):
    """Start a run through the page and return it, queued and not yet run."""
    client.post("/research", data={})
    return db.scalar(select(ResearchRun))


def page_text(client, url="/research"):
    return html.unescape(client.get(url).text)


def past_runs_part(text):
    """The Past runs block of the page."""
    return text[text.index('id="past-runs"') :]


def status_url(text):
    """The URL the status partial polls, as the browser would read it from the markup."""
    [url] = re.findall(r'hx-get="(/research/status[^"]*)"', text)
    return html.unescape(url)


# --- the side nav -----------------------------------------------------------


def test_nav_has_recommendations_then_research(onboarded_client):
    text = onboarded_client.get("/").text
    assert (
        text.index('href="/profiles"')
        < text.index('href="/recommendations"')
        < text.index('href="/research"')
        < text.index('href="/settings"')
    )


def test_nav_marks_research_as_current(onboarded_client):
    text = onboarded_client.get("/research").text
    link = re.search(r'<a href="/research"[^>]*>', text).group(0)
    assert "is-current" in link and 'aria-current="page"' in link


# --- the start dialog -------------------------------------------------------


def test_start_dialog_shows_counts_and_the_top_up_box(
    onboarded_client, db, research_world, solo_version
):
    fill_lane(db, Lane.SOLO, 20, solo_version)
    text = html.unescape(onboarded_client.get("/research/start").text)
    assert (
        "20 recommended" in text
        and 'name="add_five" value="solo"' in text
        and "Add 5 more to Solo" in text
    )
    assert NO_PROFILE in text
    assert "checked" not in text.split('value="solo"')[1].split(">")[0]


def test_start_dialog_says_a_lane_under_the_target_fills(
    onboarded_client, db, research_world, solo_version
):
    text = page_text(onboarded_client, "/research/start")
    assert "0 recommended" in text and "Fills to 20." in text
    assert 'name="add_five"' not in text


def test_a_lane_with_no_profile_gets_no_top_up_box_even_when_full(
    onboarded_client, db, research_world, solo_version
):
    fill_lane(db, Lane.TWO, 20, solo_version)
    text = page_text(onboarded_client, "/research/start")
    assert 'value="two"' not in text
    assert text.count(NO_PROFILE) == 3


def test_start_dialog_is_a_dialog_with_one_primary_button(
    onboarded_client, research_world
):
    text = onboarded_client.get("/research/start").text
    assert text.lstrip().startswith("<dialog")
    assert text.count("em-btn--primary") == 1
    assert "Start research" in text and "Cancel" in text
    assert 'aria-labelledby="research-start-title"' in text


def test_start_dialog_lists_every_blocker_and_disables_start(onboarded_client, db):
    text = page_text(onboarded_client, "/research/start")
    assert "Set up the Recommender model in Settings, then run research again." in text
    assert "Set up the Review judge model in Settings, then run research again." in text
    assert (
        "Set up the Everyday tasks model in Settings, then run research again." in text
    )
    assert "Add a Tavily key in Settings, then run research again." in text
    start = re.search(r"<button[^>]*>\s*<span[^>]*>Start research", text).group(0)
    assert " disabled" in start


def test_start_dialog_enables_start_when_nothing_blocks(
    onboarded_client, research_world
):
    text = page_text(onboarded_client, "/research/start")
    assert "Set up the" not in text and "Tavily" not in text
    start = re.search(r"<button[^>]*>\s*<span[^>]*>Start research", text).group(0)
    assert " disabled" not in start


def test_start_refused_says_what_to_set_up(onboarded_client, db, solo_version):
    r = onboarded_client.post("/research", data={})
    assert (
        r.status_code == 200
        and "Set up the Recommender model in Settings, then run research again."
        in html.unescape(r.text)
    )
    assert "em-alert--danger" in r.text and "HX-Trigger" not in r.headers
    # The same line isn't also listed as a blocker.
    assert (
        html.unescape(r.text).count(
            "Set up the Recommender model in Settings, then run research again."
        )
        == 1
    )


def test_start_refused_while_running_shows_why(
    onboarded_client, db, research_world, research_run
):
    r = onboarded_client.post("/research", data={})
    assert r.status_code == 200 and "em-alert--danger" in r.text
    assert (
        "Research is already running. Wait for it to pause or finish, or cancel it."
        in html.unescape(r.text)
    )
    assert db.scalar(select(func.count()).select_from(ResearchRun)) == 1


def test_start_with_every_lane_full_asks_for_a_top_up(
    onboarded_client, db, research_world, solo_version
):
    for lane in Lane:
        fill_lane(db, lane, 20, solo_version)
    r = onboarded_client.post("/research", data={})
    assert r.status_code == 200
    assert (
        "Every lane already has 20 recommended games. Tick a lane to add 5 more."
        in html.unescape(r.text)
    )


def test_refused_start_keeps_the_ticked_boxes(
    onboarded_client, db, research_world, research_run, solo_version
):
    fill_lane(db, Lane.SOLO, 20, solo_version)
    r = onboarded_client.post("/research", data={"add_five": "solo"})
    assert "em-alert--danger" in r.text
    assert "checked" in r.text.split('value="solo"')[1].split(">")[0]


def test_start_twice_queues_one_run(onboarded_client, db, research_world):
    r = onboarded_client.post("/research", data={})
    assert r.text == "" and r.headers["HX-Trigger"] == "research-started"
    onboarded_client.post("/research", data={})
    assert db.scalar(select(func.count()).select_from(ResearchRun)) == 1


def test_start_wakes_the_worker(onboarded_client, app, research_world):
    app.state.job_wakeup.clear()
    onboarded_client.post("/research", data={})
    assert app.state.job_wakeup.is_set()


def test_start_reads_the_ticked_lanes_and_ignores_unknown_ones(
    onboarded_client, db, research_world, solo_version
):
    fill_lane(db, Lane.SOLO, 20, solo_version)
    r = onboarded_client.post(
        "/research", data={"add_five": ["solo", "bogus", "", "two"]}
    )
    assert r.text == ""
    run = db.scalar(select(ResearchRun).order_by(ResearchRun.id.desc()))
    choices = list(
        db.scalars(
            select(ResearchChoice.lane).where(ResearchChoice.choice == "add_five")
        )
    )
    assert choices == ["solo", "two"]
    targets = {target.lane: target.target for target in run_targets(run)}
    assert targets[Lane.SOLO] == 25 and targets[Lane.TWO] == 20


def test_a_refused_start_does_not_wake_the_worker(onboarded_client, app, db):
    app.state.job_wakeup.clear()
    onboarded_client.post("/research", data={})
    assert not app.state.job_wakeup.is_set()


def test_research_needs_login(app):
    assert TestClient(app).post("/research", follow_redirects=False).status_code == 303
    assert TestClient(app).get("/research", follow_redirects=False).status_code == 303
    assert (
        TestClient(app).get("/research/status", follow_redirects=False).status_code
        == 303
    )


# --- the status area --------------------------------------------------------


def test_the_page_with_no_runs_offers_to_run_research(onboarded_client, db):
    text = page_text(onboarded_client)
    assert "<h1" in text and "Research" in text
    assert 'id="research-status"' in text
    assert 'hx-get="/research/start"' in text and 'hx-target="#dialog"' in text
    assert "Run research" in text and "Last research" not in text
    assert "research-started from:body" in text
    assert "every 2s" not in text
    assert "Past runs" in text and "No research runs yet." in text
    assert text.index('id="research-status"') < text.index("Past runs")


def test_idle_status_has_one_primary_button(onboarded_client, db):
    text = onboarded_client.get("/research").text
    assert text.count("em-btn--primary") == 1


def test_running_status_shows_the_log_and_polls(
    onboarded_client, db, research_world, research_run
):
    add_event(
        db,
        research_run.id,
        "search",
        "Searching the web: <b>cozy</b>",
        FIXED_NOW,
        round=1,
        lane="solo",
    )
    text = onboarded_client.get("/research").text
    assert (
        'hx-trigger="every 2s"' in text
        and "Round 1 · Solo · Searching the web: &lt;b&gt;cozy&lt;/b&gt;" in text
    )
    assert "<b>cozy</b>" not in text
    key = research_status(db).key
    assert (
        onboarded_client.get(f"/research/status?was=running&shown={key}").status_code
        == 204
    )


def test_the_polling_url_is_the_one_the_page_would_get_204_from(
    onboarded_client, db, research_world, research_run
):
    add_event(db, research_run.id, "round", "Round 1.", FIXED_NOW, round=1)
    url = status_url(onboarded_client.get("/research").text)
    assert url.startswith("/research/status?was=running&shown=")
    assert onboarded_client.get(url).status_code == 204
    add_event(db, research_run.id, "history", "Looking.", FIXED_NOW, round=1)
    changed = onboarded_client.get(url)
    assert changed.status_code == 200 and "Looking." in changed.text
    assert "HX-Trigger" not in changed.headers


def test_running_status_has_a_progress_bar_and_the_round(
    onboarded_client, db, research_world, research_run
):
    add_event(
        db, research_run.id, "search", "Searching.", FIXED_NOW, round=2, lane="solo"
    )
    db.execute(update(ResearchRun).values(rounds_done=1))
    db.commit()
    text = onboarded_client.get("/research").text
    assert 'role="progressbar"' in text and "is-indeterminate" in text
    assert re.search(r"em-progress__label[^>]*>Searching\.<", text)
    assert re.search(r"em-progress__value[^>]*>Round 2<", text)


def test_a_run_with_no_events_yet_says_it_is_queued(
    onboarded_client, db, research_world, research_run
):
    text = onboarded_client.get("/research").text
    assert "Research queued." in text and "Round 1" in text


def test_log_lines_leave_out_the_parts_that_are_missing(
    onboarded_client, db, research_world, research_run
):
    add_event(db, research_run.id, "refresh", "Refreshing profiles first.", FIXED_NOW)
    add_event(db, research_run.id, "round", "Round 1.", FIXED_NOW, round=1)
    add_event(db, research_run.id, "skipped", "No profile yet.", FIXED_NOW, lane="two")
    text = page_text(onboarded_client)
    items = re.findall(r"<li[^>]*>\s*([^<]*?)\s*</li>", text)
    assert items == [
        "Refreshing profiles first.",
        "Round 1 · Round 1.",
        "Two players · No profile yet.",
    ]


def test_the_log_shows_the_last_fifty_events_oldest_first(
    onboarded_client, db, research_world, research_run
):
    for n in range(1, 61):
        add_event(db, research_run.id, "search", f"event {n}.", FIXED_NOW, round=1)
    text = page_text(onboarded_client)
    items = re.findall(r"<li[^>]*>\s*([^<]*?)\s*</li>", text)
    assert items == [f"Round 1 · event {n}." for n in range(11, 61)]


def test_event_text_and_lanes_are_escaped(
    onboarded_client, db, research_world, research_run
):
    add_event(
        db,
        research_run.id,
        "skipped",
        '<script>alert("x")</script> & more',
        FIXED_NOW,
        round=1,
        lane="solo",
    )
    text = onboarded_client.get("/research").text
    assert "<script>alert" not in text
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt; &amp; more" in text


def test_running_status_offers_cancel_and_nothing_else(
    onboarded_client, db, research_world, research_run
):
    text = page_text(onboarded_client)
    assert "Cancel research" in text and "Cancelling" not in text
    assert f'hx-post="/research/{research_run.id}/cancel"' in text
    assert "Run one more round" not in text and "Run research" not in text
    assert text.count("em-btn--primary") == 0


def test_a_cancel_in_progress_is_shown_and_disabled(
    onboarded_client, db, research_world, research_run
):
    assert cancel_research(db, research_run.id, FIXED_NOW)
    text = page_text(onboarded_client)
    assert "Cancelling" in text and "Cancel research" not in text
    button = re.search(r"<button[^>]*>\s*<span[^>]*>Cancelling", text).group(0)
    assert " disabled" in button
    # The cancel changes what a poll would show, so the page is redrawn.
    assert (
        onboarded_client.get(
            "/research/status?was=running&shown=1:0:running:False"
        ).status_code
        == 200
    )


def test_paused_run_offers_one_more_round_or_stop(
    onboarded_client, app, db, research_world
):
    run = paused_run(onboarded_client, app, db, research_world)
    text = html.unescape(onboarded_client.get("/research").text)
    assert "Research paused after round 3." in text and "Solo: 0 of 20." in text
    assert "Run one more round" in text and "Stop here" in text
    assert "Cancel research" in text
    assert f'hx-post="/research/{run.id}/continue"' in text
    assert f'hx-post="/research/{run.id}/stop"' in text
    assert f'hx-post="/research/{run.id}/cancel"' in text
    assert "every 2s" not in text


def test_paused_status_has_one_primary_button_and_a_ghost_cancel(
    onboarded_client, app, db, research_world
):
    paused_run(onboarded_client, app, db, research_world)
    text = onboarded_client.get("/research/status").text
    assert text.count("em-btn--primary") == 1
    primary = re.search(
        r'class="[^"]*em-btn--primary[^"]*"[^>]*>\s*<span[^>]*>([^<]*)', text
    )
    assert primary.group(1) == "Run one more round"
    assert re.search(r"em-btn--secondary[^>]*[^>]*>\s*<span[^>]*>Stop here", text)
    assert re.search(r"em-btn--ghost[^>]*[^>]*>\s*<span[^>]*>Cancel research", text)


def test_paused_status_has_no_past_run_row_for_the_active_run(
    onboarded_client, app, db, research_world
):
    paused_run(onboarded_client, app, db, research_world)
    text = page_text(onboarded_client)
    assert "No research runs yet." in past_runs_part(text)
    assert "Paused" not in past_runs_part(text)


def test_the_status_answers_with_the_partial_only(
    onboarded_client, db, research_world, research_run
):
    text = onboarded_client.get("/research/status").text
    assert text.lstrip().startswith("<div") and 'id="research-status"' in text
    assert "<html" not in text


def test_a_poll_that_finds_the_same_state_is_204_only_while_active(
    onboarded_client, db, research_world, research_run
):
    key = research_status(db).key
    assert onboarded_client.get(f"/research/status?shown={key}").status_code == 204
    other = onboarded_client.get("/research/status?was=running&shown=nope")
    assert other.status_code == 200 and "HX-Trigger" not in other.headers


def test_continue_twice_adds_one_round(onboarded_client, app, db, research_world):
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    onboarded_client.post("/research", data={})
    run_pending_jobs(app)
    run = db.scalar(select(ResearchRun))
    onboarded_client.post(f"/research/{run.id}/continue")
    onboarded_client.post(f"/research/{run.id}/continue")
    db.expire_all()
    assert db.get(ResearchRun, run.id).rounds_allowed == 4


def test_continue_answers_with_the_running_status_and_wakes_the_worker(
    onboarded_client, app, db, research_world
):
    run = paused_run(onboarded_client, app, db, research_world)
    app.state.job_wakeup.clear()
    r = onboarded_client.post(f"/research/{run.id}/continue")
    assert r.status_code == 200 and 'hx-trigger="every 2s"' in r.text
    assert "HX-Trigger" not in r.headers
    assert 'id="research-status"' in r.text
    assert app.state.job_wakeup.is_set()


def test_stop_ends_the_run_and_tells_the_page(
    onboarded_client, app, db, research_world
):
    run = paused_run(onboarded_client, app, db, research_world)
    r = onboarded_client.post(f"/research/{run.id}/stop")
    assert r.status_code == 200 and r.headers["HX-Trigger"] == "research-finished"
    text = html.unescape(r.text)
    assert "Run research" in text and "Last research" in text and "Stopped" in text
    assert "every 2s" not in text
    db.expire_all()
    assert db.get(ResearchRun, run.id).status == "stopped"
    # The table lists it now.
    assert "Stopped" in past_runs_part(page_text(onboarded_client))


def test_cancel_from_a_pause_ends_the_run_and_tells_the_page(
    onboarded_client, app, db, research_world
):
    run = paused_run(onboarded_client, app, db, research_world)
    r = onboarded_client.post(f"/research/{run.id}/cancel")
    assert r.headers["HX-Trigger"] == "research-finished"
    assert "Cancelled" in r.text and "Run research" in r.text
    db.expire_all()
    assert db.get(ResearchRun, run.id).status == "cancelled"


def test_cancel_on_a_running_run_shows_cancelling(
    onboarded_client, db, research_world, research_run
):
    r = onboarded_client.post(f"/research/{research_run.id}/cancel")
    assert r.status_code == 200 and "Cancelling" in r.text
    assert 'hx-trigger="every 2s"' in r.text and "HX-Trigger" not in r.headers
    db.expire_all()
    assert db.get(ResearchRun, research_run.id).cancel_requested is True


@pytest.mark.parametrize("action", ["continue", "stop", "cancel"])
@pytest.mark.parametrize("run_id", ["99", "0", "abc", "-1", "9" * 30])
def test_an_unknown_run_is_a_404(onboarded_client, db, research_world, action, run_id):
    assert onboarded_client.post(f"/research/{run_id}/{action}").status_code == 404


@pytest.mark.parametrize("action", ["continue", "stop", "cancel"])
def test_run_actions_need_login(app, action):
    r = TestClient(app).post(f"/research/1/{action}", follow_redirects=False)
    assert r.status_code == 303


def test_stop_on_a_running_run_changes_nothing(
    onboarded_client, db, research_world, research_run
):
    r = onboarded_client.post(f"/research/{research_run.id}/stop")
    assert r.status_code == 200 and 'hx-trigger="every 2s"' in r.text
    assert "HX-Trigger" not in r.headers
    db.expire_all()
    assert db.get(ResearchRun, research_run.id).status == "running"


def test_finishing_tells_the_page(
    onboarded_client, app, db, research_world, solo_version
):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    onboarded_client.post("/research", data={})
    run_pending_jobs(app)
    r = onboarded_client.get("/research/status?was=running")
    assert (
        r.headers.get("HX-Trigger") == "research-finished" and "every 2s" not in r.text
    )


def test_the_status_loaded_after_a_start_shows_the_queued_run(
    onboarded_client, db, research_world
):
    # What the idle status asks for when `research-started` arrives: the run is queued and
    # no job has touched it yet, so there is no log and no event to send.
    onboarded_client.post("/research", data={})
    r = onboarded_client.get("/research/status?was=running")
    assert r.status_code == 200 and "HX-Trigger" not in r.headers
    assert 'hx-trigger="every 2s"' in r.text and "Research queued." in r.text
    assert "Cancel research" in r.text and "Run research" not in r.text


def test_a_poll_that_was_not_waiting_sends_no_event(
    onboarded_client, app, db, research_world, solo_version
):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    onboarded_client.post("/research", data={})
    run_pending_jobs(app)
    assert "HX-Trigger" not in onboarded_client.get("/research/status").headers
    assert (
        onboarded_client.get("/research/status?was=paused").headers["HX-Trigger"]
        == "research-finished"
    )


def test_idle_status_says_how_the_last_run_went(
    onboarded_client, app, db, research_world, solo_version
):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    onboarded_client.post("/research", data={})
    run_pending_jobs(app)
    text = page_text(onboarded_client)
    assert re.search(r"Last research [^:]+: Finished\. 1 accepted, 1 rejected\.", text)
    assert "Run research" in text


def test_a_failed_run_says_why(onboarded_client, app, db, research_world):
    run = start_run(onboarded_client, app, db)
    db.execute(delete(JobAssignment).where(JobAssignment.job == "judge"))
    db.commit()
    run_pending_jobs(app)
    db.expire_all()
    assert db.get(ResearchRun, run.id).status == "failed"
    message = "Set up the Review judge model in Settings, then run research again."
    text = page_text(onboarded_client)
    assert "Failed" in text
    assert message in text.split('id="past-runs"')[0]


# --- the past runs ----------------------------------------------------------


def test_past_runs_are_listed(onboarded_client, app, db, research_world, solo_version):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    onboarded_client.post("/research", data={})
    run_pending_jobs(app)
    text = html.unescape(onboarded_client.get("/research").text)
    assert "Past runs" in text and "Finished" in text


def test_past_runs_table_has_its_columns_and_counts(
    onboarded_client, app, db, research_world, solo_version
):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    onboarded_client.post("/research", data={})
    run_pending_jobs(app)
    text = page_text(onboarded_client)
    part = past_runs_part(text)
    headers = re.findall(r"<th[^>]*>([^<]*)</th>", part)
    assert headers == ["Started", "Result", "Rounds", "Accepted", "Rejected"]
    rows = [
        [cell.strip() for cell in re.findall(r"<td[^>]*>([^<]*)</td>", row)]
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", part, re.S)
        if "<td" in row
    ]
    # The newest first: the run just made (one round, one accepted, one rejected), then the
    # filler run's 19.
    assert rows[0][1:] == ["Finished", "1", "1", "1"]
    assert rows[1][1:] == ["Finished", "0", "19", "0"]
    assert len(rows) == 2


def test_the_table_reloads_when_research_finishes(onboarded_client, db):
    text = onboarded_client.get("/research").text
    assert re.search(
        r'<[^>]*id="past-runs"[^>]*hx-trigger="research-finished from:body"', text
    ) or re.search(
        r'<[^>]*hx-trigger="research-finished from:body"[^>]*id="past-runs"', text
    )
    assert 'hx-get="/research"' in text and 'hx-select="#past-runs"' in text


def test_a_finished_page_swaps_only_the_table(
    onboarded_client, app, db, research_world, solo_version
):
    # The table's reload asks for the page and picks #past-runs out of the answer, so the
    # id has to be there in the full page, once.
    fill_lane(db, Lane.SOLO, 19, solo_version)
    onboarded_client.post("/research", data={})
    run_pending_jobs(app)
    assert onboarded_client.get("/research").text.count('id="past-runs"') == 1


def test_past_runs_do_not_list_the_active_run(
    onboarded_client, db, research_world, research_run, solo_version
):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    part = past_runs_part(page_text(onboarded_client))
    assert "Finished" in part and "Running" not in part
    assert len(re.findall(r"<td[^>]*>[^<]*Finished", part)) == 1


# --- research_status --------------------------------------------------------


def test_status_with_no_run(db):
    status = research_status(db)
    assert status.run is None and status.active is False
    assert status.events == [] and status.short == [] and status.summary is None
    assert status.key == ""


def test_status_of_the_active_run(db, research_world, research_run):
    add_event(db, research_run.id, "round", "Round 1.", FIXED_NOW, round=1)
    status = research_status(db)
    assert status.run.id == research_run.id and status.active is True
    assert [event.message for event in status.events] == ["Round 1."]
    assert status.key == f"{research_run.id}:{status.events[-1].id}:running:False"
    # Nothing has run, so no lane has been left out yet and every lane is short.
    assert status.short == [(lane, 0, 20) for lane in Lane]


def test_status_key_changes_with_the_events_the_status_and_a_cancel(
    db, research_world, research_run
):
    seen = {research_status(db).key}
    add_event(db, research_run.id, "round", "Round 1.", FIXED_NOW, round=1)
    seen.add(research_status(db).key)
    cancel_research(db, research_run.id, FIXED_NOW)
    seen.add(research_status(db).key)
    assert len(seen) == 3
    assert research_status(db).key.endswith(":running:True")


def test_status_prefers_the_active_run_to_a_newer_finished_one(
    db, research_world, research_run, solo_version
):
    fill_lane(db, Lane.SOLO, 1, solo_version)
    db.execute(
        update(ResearchRun)
        .where(ResearchRun.id != research_run.id)
        .values(created_at=FIXED_NOW.replace(year=2030))
    )
    db.commit()
    status = research_status(db)
    assert status.run.id == research_run.id and status.active is True


def test_status_falls_back_to_the_newest_run_with_its_summary(
    db, research_world, solo_version
):
    fill_lane(db, Lane.SOLO, 3, solo_version)
    status = research_status(db)
    assert status.run is not None and status.active is False
    assert status.run.status == "finished"
    assert (status.summary.accepted, status.summary.rejected, status.summary.added) == (
        3,
        0,
        3,
    )
    assert status.key.endswith(":finished:False")


def test_status_events_are_the_last_fifty_oldest_first(
    db, research_world, research_run
):
    for n in range(1, 61):
        add_event(db, research_run.id, "search", f"event {n}", FIXED_NOW)
    events = research_status(db).events
    assert [event.message for event in events] == [f"event {n}" for n in range(11, 61)]


def test_status_lists_the_lanes_still_short(onboarded_client, app, db, research_world):
    paused_run(onboarded_client, app, db, research_world)
    status = research_status(db)
    assert status.active is True and status.run.status == "paused"
    assert status.short == [(Lane.SOLO, 0, 20)]


# --- escaping ---------------------------------------------------------------


def test_the_new_templates_never_mark_text_as_safe():
    for name in (
        "research.html",
        "partials/research_status.html",
        "partials/research_start.html",
    ):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe|Markup\(|autoescape\s+false", source), name
