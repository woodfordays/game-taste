"""The review dialog: opening it, saving drafts, publishing, switching lanes and the helper routes."""

import html
import json
import re
from datetime import timedelta

import pytest
from starlette.datastructures import FormData

from app.models import Game
from app.routes.reviews import FormInputError, form_from_request
from app.services.reviews import (
    Lane,
    ReviewForm,
    history,
    load_form,
    publish,
    save_draft,
)
from conftest import FIXED_NOW

LATER = FIXED_NOW + timedelta(minutes=5)


def dialog(client, appid: int = 10, lane: str | None = None) -> str:
    """The dialog's markup as served (character references still encoded)."""
    url = f"/games/{appid}/review" + (f"?lane={lane}" if lane else "")
    r = client.get(url)
    assert r.status_code == 200
    return r.text


def lane_group(raw: str) -> str:
    found = re.search(r'<div[^>]*id="review-lanes".*?</div>', raw, re.S)
    assert found, "no lane switcher"
    return found[0]


def lane_buttons(raw: str) -> dict[str, str]:
    """Each lane switcher button's visible text, by label."""
    buttons = re.findall(r"<button[^>]*>.*?</button>", lane_group(raw), re.S)
    out = {}
    for button in buttons:
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", button)).split())
        label = next(
            name
            for name in ("Two players", "Three to four", "Five or more", "Solo")
            if text.startswith(name)
        )
        out[label] = button
    return out


def checked_lane(raw: str) -> str:
    checked = [
        label
        for label, button in lane_buttons(raw).items()
        if 'aria-checked="true"' in button
    ]
    assert len(checked) == 1
    return checked[0]


def textarea(raw: str, name: str) -> str:
    """A textarea's value as a browser reads it: the first newline is dropped."""
    found = re.search(rf'<textarea[^>]*name="{name}"[^>]*>(.*?)</textarea>', raw, re.S)
    assert found, f"no textarea {name}"
    assert found[1].startswith("\n"), "no newline after the opening tag"
    return html.unescape(found[1][1:])


def input_tag(raw: str, name: str) -> str:
    found = re.search(rf'<input[^>]*name="{name}"[^>]*>', raw)
    assert found, f"no input {name}"
    return found[0]


def input_value(raw: str, name: str) -> str:
    found = re.search(r'value="([^"]*)"', input_tag(raw, name))
    return html.unescape(found[1]) if found else ""


def selected_option(raw: str, name: str) -> str | None:
    select = re.search(rf'<select[^>]*name="{name}".*?</select>', raw, re.S)
    assert select, f"no select {name}"
    chosen = re.search(r'<option value="([^"]*)" selected', select[0])
    return chosen[1] if chosen else None


def checked_people(raw: str) -> list[int]:
    return sorted(
        int(re.search(r'value="(\d+)"', tag)[1])
        for tag in re.findall(r'<input[^>]*name="people"[^>]*>', raw)
        if " checked" in tag
    )


def alert(raw: str) -> str:
    """The danger alert's text, or "" when the dialog shows none."""
    found = re.search(r"<div[^>]*em-alert--danger.*?</p>", raw, re.S)
    return html.unescape(found[0]) if found else ""


def post(client, lane: str = "solo", appid: int = 10, **data):
    return client.post(f"/games/{appid}/review/{lane}", data=data)


# --- the brief's tests ----------------------------------------------------------


def test_dialog_opens_on_draft(onboarded_client, db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="Half written"), FIXED_NOW)
    assert "Half written" in onboarded_client.get("/games/10/review").text


def test_publish_closes_and_triggers_card(onboarded_client, db, games):
    r = onboarded_client.post(
        "/games/10/review/solo",
        data={"action": "publish", "rating": "7", "text": "Fine"},
    )
    assert r.text == "" and r.headers["HX-Trigger"] == "card-updated-10"
    assert history(db, 10, Lane.SOLO)[0].rating == 7


def test_publish_error_keeps_values(onboarded_client, games):
    text = onboarded_client.post(
        "/games/10/review/solo",
        data={"action": "publish", "text": " ", "tags": "keep me"},
    ).text
    assert (
        "Add a rating or a few words before publishing." in text
        and 'value="keep me"' in text
    )


def test_lane_switch_prompts_when_dirty(onboarded_client, db, games):
    data = {"to": "two", "dirty": "1", "text": "Unsaved words"}
    assert (
        "You have unsaved changes in Solo."
        in onboarded_client.post("/games/10/review/solo/switch", data=data).text
    )
    onboarded_client.post(
        "/games/10/review/solo/switch", data={**data, "choice": "save"}
    )
    assert load_form(db, 10, Lane.SOLO)[0].text == "Unsaved words"


def test_solo_hides_coop_fields(onboarded_client, games):
    assert (
        "How it went for the group"
        not in onboarded_client.get("/games/10/review?lane=solo").text
    )
    assert (
        "How it went for the group"
        in onboarded_client.get("/games/10/review?lane=three_four").text
    )


def test_html_in_review_is_escaped(onboarded_client, db, games):  # Review focus 5
    publish(db, 10, Lane.SOLO, ReviewForm(text="<script>alert(1)</script>"), FIXED_NOW)
    text = onboarded_client.get("/games/10/review?lane=solo").text
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;" in text


# --- opening the dialog ---------------------------------------------------------


def test_dialog_is_a_native_dialog_titled_with_the_game(onboarded_client, games):
    raw = dialog(onboarded_client)
    assert "<dialog" in raw and 'class="em-dialog' in raw
    title = re.search(
        r'<h2[^>]*class="em-dialog__title[^"]*"[^>]*>(.*?)</h2>', raw, re.S
    )
    assert title and title[1].strip() == "Sample Quest"
    assert "<html" not in raw  # a fragment for #dialog, not a page


def test_dialog_opens_itself_and_closes_by_clearing_the_holder(onboarded_client, games):
    raw = dialog(onboarded_client)
    assert "showModal()" in raw
    assert "document.getElementById('dialog')" in raw


def test_dialog_escapes_the_game_name(onboarded_client, db):
    db.add(
        Game(
            appid=77,
            name="<i>Odd</i> & Co",
            owned=True,
            playtime_minutes=None,
            last_played_at=None,
            source="steam_library",
            created_at=FIXED_NOW,
        )
    )
    db.commit()
    raw = dialog(onboarded_client, 77)
    assert "<i>Odd</i>" not in raw and "&lt;i&gt;Odd&lt;/i&gt;" in raw


def test_dialog_unknown_game_is_404(onboarded_client, games):
    assert onboarded_client.get("/games/999/review").status_code == 404


def test_dialog_buttons(onboarded_client, games):
    raw = dialog(onboarded_client)
    assert "Cancel" in raw and "Save draft" in raw and "Publish review" in raw
    publish_button = re.search(
        r"<button[^>]*>(?:(?!</button>).)*Publish review", raw, re.S
    )[0]
    assert "em-btn--primary" in publish_button
    assert raw.count("em-btn--primary") == 1  # one primary per view


def test_submit_buttons_disable_themselves_while_saving(onboarded_client, games):
    raw = dialog(onboarded_client)
    for value in ("draft", "publish"):
        tag = re.search(rf'<button[^>]*name="action"[^>]*value="{value}"[^>]*>', raw)
        assert tag and 'hx-disabled-elt="this"' in tag[0]
        assert 'hx-post="/games/10/review/solo"' in tag[0]


def test_dialog_starts_clean(onboarded_client, games):
    raw = dialog(onboarded_client)
    assert input_value(raw, "dirty") == "0"
    assert "this.elements.dirty.value" in raw  # an input listener marks it dirty


def button_tag(raw: str, label: str) -> str:
    """The opening tag of the button whose text contains `label`, unescaped."""
    found = re.search(
        rf"<button[^>]*>(?=(?:(?!</button>).)*{re.escape(label)})", raw, re.S
    )
    assert found, f"no button {label}"
    return html.unescape(found[0])


def test_close_prompt_is_ready_and_hidden(onboarded_client, games):
    raw = dialog(onboarded_client)
    prompt = re.search(r'<div[^>]*id="close-prompt"[^>]*>', raw)
    assert prompt and re.search(r"\bhidden\b", prompt[0])  # nothing to ask yet
    assert "You have unsaved changes." in raw
    for label in ("Save draft and close", "Discard changes", "Keep writing"):
        assert label in raw
    assert "em-alert--warning" in raw
    assert raw.count("em-btn--primary") == 1  # the prompt adds no second primary


def test_close_prompt_buttons(onboarded_client, games):
    raw = dialog(onboarded_client)
    save = button_tag(raw, "Save draft and close")
    assert 'hx-post="/games/10/review/solo"' in save
    assert re.search(r'"action":\s*"draft"', save) and 'hx-disabled-elt="this"' in save
    assert ".close()" in button_tag(raw, "Discard changes")  # closes without saving
    assert "data-keep" in button_tag(raw, "Keep writing")


def test_close_prompt_follows_the_lane(onboarded_client, games):
    save = button_tag(dialog(onboarded_client, lane="three_four"), "Save draft and close")
    assert 'hx-post="/games/10/review/three_four"' in save


def test_x_and_cancel_ask_before_closing_a_dirty_dialog(onboarded_client, games):
    raw = dialog(onboarded_client)
    for tag in (button_tag(raw, "Cancel"), re.search(r"<button[^>]*Close[^>]*>", raw)[0]):
        assert "data-close" in tag
        assert "hx-on:click" not in tag  # they no longer close at once
    # One script decides: a dialog with changes asks, one without closes at once.
    assert 'form.elements.dirty.value === "1"' in raw
    assert "dialog.close()" in raw
    assert 'addEventListener("cancel"' in raw  # Escape and the back gesture
    assert "event.preventDefault()" in raw


def test_the_prompt_survives_an_error_and_a_lane_switch(onboarded_client, games):
    errored = post(onboarded_client, action="publish", text=" ").text
    assert 'id="close-prompt"' in errored
    switched = switch(onboarded_client, "two", dirty="0").text
    assert 'id="close-prompt"' in switched
    assert 'hx-post="/games/10/review/two"' in button_tag(
        switched, "Save draft and close"
    )


def test_save_draft_and_close_saves_and_closes(onboarded_client, db, games):
    raw = dialog(onboarded_client)
    save = button_tag(raw, "Save draft and close")
    url = re.search(r'hx-post="([^"]+)"', save)[1]
    vals = json.loads(re.search(r"hx-vals='([^']+)'", save)[1])
    r = onboarded_client.post(
        url, data={**vals, "dirty": "1", "text": "Half  written ", "rating": "4"}
    )
    assert r.status_code == 200 and r.text == ""  # empty: #dialog is cleared
    assert r.headers["HX-Trigger"] == "card-updated-10"
    form, source = load_form(db, 10, Lane.SOLO)
    assert source == "draft" and (form.text, form.rating) == ("Half  written ", 4)
    assert history(db, 10, Lane.SOLO) == []  # saved as a draft, not published


def test_save_draft_and_close_that_cannot_save_keeps_the_dialog(
    onboarded_client, db, games
):
    r = post(onboarded_client, action="draft", dirty="1", text="keep me", status="nope")
    assert "<dialog" in r.text and "HX-Trigger" not in r.headers
    assert "Pick one of the listed options." in alert(r.text)
    assert textarea(r.text, "text") == "keep me"
    assert load_form(db, 10, Lane.SOLO)[1] == "new"


def test_dialog_opens_on_solo_for_a_new_game(onboarded_client, games):
    assert checked_lane(dialog(onboarded_client)) == "Solo"


def test_dialog_opens_on_the_most_recent_lane(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(rating=6), LATER)
    assert checked_lane(dialog(onboarded_client)) == "Three to four"


def test_dialog_takes_an_explicit_lane(onboarded_client, db, games):
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(rating=6), LATER)
    assert checked_lane(dialog(onboarded_client, lane="five_plus")) == "Five or more"
    assert checked_lane(dialog(onboarded_client, lane="solo")) == "Solo"


def test_dialog_ignores_an_unknown_lane(onboarded_client, db, games):
    publish(db, 10, Lane.TWO, ReviewForm(rating=6), LATER)
    assert checked_lane(dialog(onboarded_client, lane="bogus")) == "Two players"


def test_lane_switcher_marks_lanes_with_a_review_or_draft(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.TWO, ReviewForm(text="wip"), FIXED_NOW)
    buttons = lane_buttons(dialog(onboarded_client, lane="solo"))
    assert list(buttons) == ["Solo", "Two players", "Three to four", "Five or more"]
    assert "Reviewed" in buttons["Solo"] and "Draft" not in buttons["Solo"]
    assert (
        "Draft" in buttons["Two players"] and "Reviewed" not in buttons["Two players"]
    )
    assert (
        "Draft" not in buttons["Three to four"]
        and "Reviewed" not in buttons["Three to four"]
    )
    assert (
        "Draft" not in buttons["Five or more"]
        and "Reviewed" not in buttons["Five or more"]
    )
    assert "lane-mark__dot" in buttons["Solo"]


def test_lane_with_a_draft_over_a_published_version_says_draft(
    onboarded_client, db, games
):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=6), LATER)
    solo = lane_buttons(dialog(onboarded_client))["Solo"]
    assert "Draft" in solo and "Reviewed" not in solo


def test_lane_buttons_switch_to_their_lane(onboarded_client, games):
    buttons = lane_buttons(dialog(onboarded_client))
    two = buttons["Two players"]
    assert 'hx-post="/games/10/review/solo/switch"' in two
    assert '"to": "two"' in html.unescape(two) or '"to":"two"' in html.unescape(two)
    assert "hx-post" not in buttons["Solo"]  # the lane you are on does nothing


def test_dialog_shows_the_published_version_when_there_is_no_draft(
    onboarded_client, db, games
):
    publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(
            rating=8,
            text="Published words",
            tags_raw="Deck Builder, fun",
            status="finished",
            verdict="liked",
            walk_away="partly",
            complexity="about_right",
            session_length="1 h",
        ),
        FIXED_NOW,
    )
    raw = dialog(onboarded_client)
    assert textarea(raw, "text") == "Published words"
    assert input_value(raw, "tags") == "deck_builder, fun"
    assert input_value(raw, "rating") == "8"
    assert selected_option(raw, "status") == "finished"
    assert selected_option(raw, "verdict") == "liked"
    assert selected_option(raw, "walk_away") == "partly"
    assert selected_option(raw, "complexity") == "about_right"
    assert input_value(raw, "session_length") == "1 h"
    assert "<details" in raw and "More details" in raw
    assert re.search(r"<details[^>]*\bopen\b", raw)  # it has values, so it opens


def test_details_start_closed_for_a_new_review(onboarded_client, games):
    raw = dialog(onboarded_client)
    assert "More details" in raw
    assert not re.search(r"<details[^>]*\bopen\b", raw)


def test_dialog_rating_row(onboarded_client, db, games):
    raw = dialog(onboarded_client)
    group = re.search(r'<div[^>]*id="review-rating".*?</div>', raw, re.S)[0]
    assert 'role="radiogroup"' in group and "em-seg" in group
    assert re.findall(r'data-value="(\d+)"', group) == [str(n) for n in range(1, 11)]
    assert "is-checked" not in group and input_value(raw, "rating") == ""
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=7), FIXED_NOW)
    raw = dialog(onboarded_client)
    group = re.search(r'<div[^>]*id="review-rating".*?</div>', raw, re.S)[0]
    checked = re.findall(r'<button[^>]*aria-checked="true"[^>]*>', group)
    assert len(checked) == 1 and 'data-value="7"' in checked[0]
    assert input_value(raw, "rating") == "7"


def test_tags_input_previews_chips(onboarded_client, db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(tags_raw="Co op, #Fun"), FIXED_NOW)
    raw = dialog(onboarded_client)
    tag = input_tag(raw, "tags")
    assert 'hx-get="/tags/preview"' in tag and 'hx-target="#tag-chips"' in tag
    chips = re.search(r'<div[^>]*id="tag-chips"[^>]*>(.*?)</div>', raw, re.S)[1]
    assert re.findall(r">(\w+)</span>", chips) == ["co_op", "fun"]


def test_dialog_keeps_text_byte_for_byte(onboarded_client, db, games):  # Review focus 5
    text = '\n  <b>Hi</b> & "quotes" 🎮\n\n\ttabbed  \n'
    save_draft(db, 10, Lane.SOLO, ReviewForm(text=text), FIXED_NOW)
    assert textarea(dialog(onboarded_client), "text") == text


def test_dialog_textarea_emits_a_newline_after_the_tag(onboarded_client, games):
    raw = dialog(onboarded_client)
    assert re.search(r'<textarea[^>]*name="text"[^>]*>\n', raw)


# --- co-op fields ---------------------------------------------------------------


def test_coop_lanes_have_who_played_a_group_and_group_text(
    onboarded_client, db, games, people
):
    sam, alex, crew = people
    for lane in ("two", "three_four", "five_plus"):
        raw = dialog(onboarded_client, lane=lane)
        assert "Who played" in raw and "Sam" in raw and "Alex" in raw
        assert "Sample Crew" in raw and "How it went for the group" in raw
        assert 'name="named_group_id"' in raw and 'name="group_text"' in raw


def test_solo_has_no_people_or_group_inputs(onboarded_client, games, people):
    raw = dialog(onboarded_client, lane="solo")
    assert "Who played" not in raw and "Sample Crew" not in raw
    assert 'name="people"' not in raw and 'name="group_text"' not in raw


def test_coop_draft_values_come_back(onboarded_client, db, games, people):
    sam, alex, crew = people
    save_draft(
        db,
        10,
        Lane.TWO,
        ReviewForm(
            people=[sam.id], named_group_id=crew.id, group_text="  Crew words\n"
        ),
        FIXED_NOW,
    )
    raw = dialog(onboarded_client, lane="two")
    assert checked_people(raw) == [sam.id, alex.id]  # the group's members count too
    assert selected_option(raw, "named_group_id") == str(crew.id)
    assert textarea(raw, "group_text") == "  Crew words\n"
    assert re.search(r"<details[^>]*\bopen\b", raw)


def test_head_count_hint_in_the_dialog(onboarded_client, db, games, people):
    sam, alex, crew = people
    save_draft(db, 10, Lane.TWO, ReviewForm(people=[sam.id, alex.id]), FIXED_NOW)
    assert "This looks like the Three to four lane." in dialog(
        onboarded_client, lane="two"
    )
    assert "This looks like" not in dialog(onboarded_client, lane="three_four")


def test_coop_lane_without_people_points_to_settings(onboarded_client, games):
    raw = dialog(onboarded_client, lane="two")
    assert "Who played" in raw and "Settings" in raw
    assert 'name="people"' not in raw


# --- saving ---------------------------------------------------------------------


def test_save_draft_closes_and_triggers_the_card(onboarded_client, db, games):
    r = post(onboarded_client, action="draft", text="Half", rating="4", tags="a, b")
    assert r.status_code == 200 and r.text == ""
    assert r.headers["HX-Trigger"] == "card-updated-10"
    form, source = load_form(db, 10, Lane.SOLO)
    assert source == "draft"
    assert (form.text, form.rating, form.tags_raw) == ("Half", 4, "a, b")
    assert history(db, 10, Lane.SOLO) == []


def test_a_draft_needs_nothing(onboarded_client, db, games):
    r = post(onboarded_client, action="draft")
    assert r.text == "" and r.headers["HX-Trigger"] == "card-updated-10"
    assert load_form(db, 10, Lane.SOLO)[1] == "draft"


def test_publish_takes_every_field(onboarded_client, db, games, people):
    sam, alex, crew = people
    r = onboarded_client.post(
        "/games/10/review/two",
        data={
            "action": "publish",
            "rating": "9",
            "text": "  A long\nreview  ",
            "tags": "Co op, #Fun",
            "status": "finished",
            "verdict": "loved",
            "walk_away": "partly",
            "complexity": "about_right",
            "session_length": " 2 h ",
            "people": [str(sam.id)],
            "named_group_id": str(crew.id),
            "group_text": " Crew words ",
        },
    )
    assert r.text == ""
    version = history(db, 10, Lane.TWO)[0]
    assert (version.rating, version.text, version.tags) == (
        9,
        "  A long\nreview  ",
        ["co_op", "fun"],
    )
    assert (version.status, version.verdict, version.walk_away, version.complexity) == (
        "finished",
        "loved",
        "partly",
        "about_right",
    )
    assert (version.session_length, version.group_text) == (" 2 h ", " Crew words ")
    assert version.named_group_id == crew.id
    assert [p.person_id for p in version.people] == sorted([sam.id, alex.id])


def test_empty_choices_are_stored_as_none(onboarded_client, db, games):
    post(
        onboarded_client,
        action="publish",
        rating="5",
        status="",
        verdict="",
        walk_away="",
        complexity="",
    )
    version = history(db, 10, Lane.SOLO)[0]
    assert (version.status, version.verdict, version.walk_away, version.complexity) == (
        None,
        None,
        None,
        None,
    )


def test_publishing_again_adds_a_version(onboarded_client, db, games):
    post(onboarded_client, action="publish", rating="5")
    post(onboarded_client, action="publish", rating="8")
    assert [v.rating for v in history(db, 10, Lane.SOLO)] == [8, 5]


def test_publishing_removes_the_draft(onboarded_client, db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="wip"), FIXED_NOW)
    post(onboarded_client, action="publish", text="done")
    db.expire_all()
    assert load_form(db, 10, Lane.SOLO)[1] == "published"


def test_user_text_is_stored_exactly(onboarded_client, db, games):  # Review focus 5
    text = '  <b>Hi</b> & "q" 🎮\n\n\ttabbed  \n'
    group_text = "\n lead and trail \n"
    for action in ("draft", "publish"):
        post(
            onboarded_client,
            "two",
            action=action,
            text=text,
            group_text=group_text,
            session_length="  45 min  ",
        )
    version = history(db, 10, Lane.TWO)[0]
    assert (version.text, version.group_text, version.session_length) == (
        text,
        group_text,
        "  45 min  ",
    )
    db.expire_all()
    post(onboarded_client, "two", action="draft", text=text, group_text=group_text)
    form, _ = load_form(db, 10, Lane.TWO)
    assert (form.text, form.group_text) == (text, group_text)


def test_text_round_trips_through_the_dialog(
    onboarded_client, db, games
):  # Review focus 5
    text = "\n  <script>alert(1)</script> 🎮 \n\n  end  "
    post(onboarded_client, action="draft", text=text)
    raw = dialog(onboarded_client)
    assert textarea(raw, "text") == text
    assert "<script>alert(1)</script>" not in raw


def test_solo_drops_coop_fields(onboarded_client, db, games, people):
    sam, alex, crew = people
    post(
        onboarded_client,
        action="publish",
        rating="5",
        people=[str(sam.id)],
        named_group_id=str(crew.id),
        group_text="not for solo",
    )
    version = history(db, 10, Lane.SOLO)[0]
    assert version.people == [] and version.named_group_id is None
    assert version.group_text == ""


def test_clearing_the_rating_in_a_draft(onboarded_client, db, games):
    post(onboarded_client, action="draft", rating="6")
    post(onboarded_client, action="draft", rating="")
    db.expire_all()
    assert load_form(db, 10, Lane.SOLO)[0].rating is None


def test_post_to_an_unknown_game(onboarded_client, games):
    r = post(onboarded_client, appid=999, action="draft")
    assert r.status_code == 404


def test_post_needs_a_known_action_and_lane(onboarded_client, games):
    assert post(onboarded_client, action="remove").status_code == 400
    assert post(onboarded_client).status_code == 400
    assert post(onboarded_client, "everyone", action="draft").status_code == 422


# --- errors ---------------------------------------------------------------------


def test_error_rerender_is_a_dialog_with_status_200(onboarded_client, games):
    r = post(onboarded_client, action="publish", text=" ")
    assert r.status_code == 200 and "<dialog" in r.text
    assert "HX-Trigger" not in r.headers
    assert "Add a rating or a few words before publishing." in alert(r.text)


def test_error_rerender_keeps_everything_typed(onboarded_client, db, games, people):
    sam, alex, crew = people
    r = onboarded_client.post(
        "/games/10/review/three_four",
        data={
            "action": "publish",
            "text": " ",
            "tags": "keep me",
            "group_text": "  group words ",
            "session_length": "45 min",
            "status": "playing",
            "verdict": "mixed",
            "walk_away": "no",
            "complexity": "too_much",
            "people": [str(alex.id)],
        },
    )
    raw = r.text
    assert textarea(raw, "text") == " "
    assert input_value(raw, "tags") == "keep me"
    assert textarea(raw, "group_text") == "  group words "
    assert input_value(raw, "session_length") == "45 min"
    assert selected_option(raw, "status") == "playing"
    assert selected_option(raw, "verdict") == "mixed"
    assert selected_option(raw, "walk_away") == "no"
    assert selected_option(raw, "complexity") == "too_much"
    assert checked_people(raw) == [alex.id]
    assert checked_lane(raw) == "Three to four"
    assert history(db, 10, Lane.THREE_FOUR) == []


def test_out_of_range_rating_is_refused(onboarded_client, games):
    raw = post(onboarded_client, action="publish", rating="11", text="x").text
    assert "Pick a rating from 1 to 10." in alert(raw)  # the service refuses 11
    assert textarea(raw, "text") == "x"


def test_error_rerender_is_dirty(onboarded_client, games):
    raw = post(onboarded_client, action="publish", text=" ").text
    assert input_value(raw, "dirty") == "1"


def test_error_rerender_does_not_animate_again(onboarded_client, games):
    first = dialog(onboarded_client)
    again = post(onboarded_client, action="publish", text=" ").text
    assert "is-still" not in first and "is-still" in again


def test_non_numeric_rating(onboarded_client, db, games):
    r = post(
        onboarded_client, action="publish", rating="abc", text="keep this", tags="t1"
    )
    assert r.status_code == 200
    assert "Pick a rating from 1 to 10." in alert(r.text)
    assert textarea(r.text, "text") == "keep this"
    assert input_value(r.text, "tags") == "t1"
    assert history(db, 10, Lane.SOLO) == []


def test_non_numeric_rating_in_a_draft(onboarded_client, db, games):
    r = post(onboarded_client, action="draft", rating="1.5", text="keep this")
    assert "Pick a rating from 1 to 10." in alert(r.text)
    assert load_form(db, 10, Lane.SOLO)[1] == "new"


@pytest.mark.parametrize(
    "extra",
    [{"people": ["abc"]}, {"named_group_id": "abc"}, {"people": ["1", "x"]}],
)
def test_non_numeric_people_or_group(onboarded_client, db, games, people, extra):
    r = post(
        onboarded_client, "two", action="publish", rating="5", text="keep this", **extra
    )
    assert r.status_code == 200
    assert "Pick people from your list." in alert(r.text)
    assert textarea(r.text, "text") == "keep this"
    assert history(db, 10, Lane.TWO) == []


def test_unknown_person_or_group(onboarded_client, db, games, people):
    r = post(onboarded_client, "two", action="publish", rating="5", people=["9999"])
    assert "Pick people from your list." in alert(r.text)
    r = post(onboarded_client, "two", action="draft", named_group_id="9999")
    assert "Pick people from your list." in alert(r.text)


def test_unknown_choice(onboarded_client, db, games):
    r = post(onboarded_client, action="publish", rating="5", status="nope")
    assert r.status_code == 200
    assert "Pick one of the listed options." in alert(r.text)
    assert re.search(
        r"<details[^>]*\bopen\b", r.text
    )  # the error is inside the details
    assert history(db, 10, Lane.SOLO) == []


# --- form_from_request ----------------------------------------------------------


def test_form_from_request_maps_every_field():
    data = FormData(
        [
            ("action", "publish"),
            ("dirty", "1"),
            ("rating", "7"),
            ("text", " hi\n"),
            ("tags", "a, b"),
            ("group_text", " g "),
            ("session_length", " 1 h "),
            ("status", ""),
            ("verdict", "loved"),
            ("walk_away", ""),
            ("complexity", "too_much"),
            ("people", "2"),
            ("people", "1"),
            ("people", ""),
            ("named_group_id", "3"),
        ]
    )
    assert form_from_request(data) == ReviewForm(
        rating=7,
        text=" hi\n",
        tags_raw="a, b",
        group_text=" g ",
        session_length=" 1 h ",
        status=None,
        verdict="loved",
        walk_away=None,
        complexity="too_much",
        people=[2, 1],
        named_group_id=3,
    )


def test_form_from_request_defaults():
    assert form_from_request(FormData([])) == ReviewForm()
    assert (
        form_from_request(FormData([("rating", ""), ("named_group_id", "")]))
        == ReviewForm()
    )


def test_form_from_request_never_alters_text():
    text = "\r\n  <b>é</b> 🎮 \t\n"
    data = FormData([("text", text), ("group_text", text), ("tags", text)])
    form = form_from_request(data)
    assert (form.text, form.group_text, form.tags_raw) == (text, text, text)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ([("rating", "abc")], "Pick a rating from 1 to 10."),
        ([("rating", "7.5")], "Pick a rating from 1 to 10."),
        ([("people", "x")], "Pick people from your list."),
        ([("named_group_id", "x")], "Pick people from your list."),
    ],
)
def test_form_from_request_refuses_non_numbers(fields, message):
    with pytest.raises(FormInputError) as caught:
        form_from_request(FormData([("text", " keep "), *fields]))
    assert str(caught.value) == message
    assert caught.value.form.text == " keep "  # what was typed is not lost


# --- switching lanes ------------------------------------------------------------


def switch(client, to: str, lane: str = "solo", **data):
    return client.post(f"/games/10/review/{lane}/switch", data={"to": to, **data})


def test_clean_switch_opens_the_other_lane(onboarded_client, db, games):
    save_draft(db, 10, Lane.TWO, ReviewForm(text="Two words"), FIXED_NOW)
    r = switch(onboarded_client, "two", dirty="0", text="ignored")
    assert r.status_code == 200 and "<dialog" in r.text
    assert checked_lane(r.text) == "Two players"
    assert textarea(r.text, "text") == "Two words"
    assert "unsaved changes in" not in r.text  # the lane switch's question
    assert "HX-Trigger" not in r.headers
    assert "is-still" in r.text  # the dialog is already open, so it doesn't rise again


def test_dirty_switch_asks_first(onboarded_client, db, games):
    r = switch(
        onboarded_client, "two", dirty="1", text="Unsaved words", tags="x", rating="4"
    )
    assert "You have unsaved changes in Solo." in r.text
    for label in ("Save draft and switch", "Discard changes and switch", "Stay here"):
        assert label in r.text
    assert checked_lane(r.text) == "Solo"  # still on the lane being edited
    assert textarea(r.text, "text") == "Unsaved words"
    assert input_value(r.text, "tags") == "x" and input_value(r.text, "rating") == "4"
    assert input_value(r.text, "dirty") == "1"
    assert load_form(db, 10, Lane.SOLO)[1] == "new"  # nothing saved yet


def test_prompt_buttons_post_the_choice(onboarded_client, games):
    raw = switch(onboarded_client, "three_four", dirty="1", text="x").text
    save = re.search(
        r"<button[^>]*>(?:(?!</button>).)*Save draft and switch", raw, re.S
    )[0]
    discard = re.search(
        r"<button[^>]*>(?:(?!</button>).)*Discard changes and switch", raw, re.S
    )[0]
    for tag, choice in ((save, "save"), (discard, "discard")):
        tag = html.unescape(tag)
        assert 'hx-post="/games/10/review/solo/switch"' in tag
        assert re.search(r'"to":\s*"three_four"', tag) and re.search(
            rf'"choice":\s*"{choice}"', tag
        )


def test_missing_dirty_counts_as_dirty(onboarded_client, games):
    assert "You have unsaved changes" in switch(onboarded_client, "two", text="x").text


def test_save_and_switch(onboarded_client, db, games):
    r = switch(
        onboarded_client,
        "two",
        dirty="1",
        choice="save",
        text="Unsaved words",
        rating="6",
    )
    assert checked_lane(r.text) == "Two players"
    assert r.headers["HX-Trigger"] == "card-updated-10"  # the card now has a draft
    form, source = load_form(db, 10, Lane.SOLO)
    assert source == "draft" and (form.text, form.rating) == ("Unsaved words", 6)
    assert load_form(db, 10, Lane.TWO)[1] == "new"
    assert "unsaved changes in" not in r.text  # the lane switch's question


def test_discard_and_switch(onboarded_client, db, games):
    r = switch(onboarded_client, "two", dirty="1", choice="discard", text="Lost words")
    assert checked_lane(r.text) == "Two players"
    assert textarea(r.text, "text") == ""
    assert "HX-Trigger" not in r.headers
    assert load_form(db, 10, Lane.SOLO)[1] == "new"


def test_discard_keeps_the_saved_draft(onboarded_client, db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="Saved earlier"), FIXED_NOW)
    switch(onboarded_client, "two", dirty="1", choice="discard", text="Lost words")
    db.expire_all()
    assert load_form(db, 10, Lane.SOLO)[0].text == "Saved earlier"


def test_save_and_switch_that_cannot_save_stays_put(onboarded_client, db, games):
    r = switch(
        onboarded_client, "two", dirty="1", choice="save", text="x", status="nope"
    )
    assert checked_lane(r.text) == "Solo"
    assert "Pick one of the listed options." in alert(r.text)
    assert textarea(r.text, "text") == "x"
    assert "HX-Trigger" not in r.headers
    assert load_form(db, 10, Lane.SOLO)[1] == "new"


def test_switch_with_an_unreadable_rating_shows_the_error(onboarded_client, games):
    r = switch(onboarded_client, "two", dirty="1", text="keep", rating="abc")
    assert checked_lane(r.text) == "Solo"
    assert "Pick a rating from 1 to 10." in alert(r.text)
    assert textarea(r.text, "text") == "keep"


def test_switch_to_the_same_lane_keeps_what_was_typed(onboarded_client, games):
    r = switch(onboarded_client, "solo", dirty="1", text="still here")
    assert checked_lane(r.text) == "Solo" and textarea(r.text, "text") == "still here"
    assert "unsaved changes in" not in r.text  # the lane switch's question


def test_switch_rejects_unknown_values(onboarded_client, games):
    assert switch(onboarded_client, "nobody", dirty="0").status_code == 400
    assert switch(onboarded_client, "two", dirty="1", choice="maybe").status_code == 400
    assert (
        switch(onboarded_client, "two", lane="everyone", dirty="0").status_code == 422
    )
    r = onboarded_client.post(
        "/games/999/review/solo/switch", data={"to": "two", "dirty": "0"}
    )
    assert r.status_code == 404


# --- tag preview ----------------------------------------------------------------


def test_tags_preview_returns_chips(onboarded_client):
    r = onboarded_client.get(
        "/tags/preview", params={"raw": "Co op, #Fun, co_op,  , Deck  Builder"}
    )
    assert r.status_code == 200
    assert re.findall(r'class="tag-chip[^"]*"[^>]*>(\w+)<', r.text) == [
        "co_op",
        "fun",
        "deck_builder",
    ]
    assert "<html" not in r.text


def test_tags_preview_of_nothing_is_empty(onboarded_client):
    assert onboarded_client.get("/tags/preview?raw=").text.strip() == ""
    assert onboarded_client.get("/tags/preview").text.strip() == ""


def test_tags_preview_escapes(onboarded_client):
    raw = onboarded_client.get(
        "/tags/preview", params={"raw": "<script>x</script>"}
    ).text
    assert "<script>" not in raw and "&lt;script&gt;" in raw


def test_tags_preview_reads_the_dialog_input_name_too(onboarded_client):
    assert (
        "tag-chip" in onboarded_client.get("/tags/preview", params={"tags": "one"}).text
    )


# --- who played -----------------------------------------------------------------


def test_people_options_checks_a_groups_members(onboarded_client, games, people):
    sam, alex, crew = people
    r = onboarded_client.get(
        f"/people/options?named_group_id={crew.id}&lane=three_four"
    )
    assert r.status_code == 200
    assert checked_people(r.text) == [sam.id, alex.id]
    assert "This looks like" not in r.text
    assert 'id="people-options"' in r.text and 'id="people-hint"' in r.text


def test_people_options_keeps_what_was_ticked(onboarded_client, games, people):
    sam, alex, crew = people
    r = onboarded_client.get(
        f"/people/options?named_group_id=&people={alex.id}&lane=two"
    )
    assert checked_people(r.text) == [alex.id]


def test_people_options_adds_the_group_to_what_was_ticked(
    onboarded_client, db, games, people
):
    sam, alex, crew = people
    from app.services.people import add_person

    riley = add_person(db, "Riley", FIXED_NOW)
    r = onboarded_client.get(
        f"/people/options?named_group_id={crew.id}&people={riley.id}&lane=five_plus"
    )
    assert checked_people(r.text) == [sam.id, alex.id, riley.id]


def test_people_options_hint_follows_the_lane(onboarded_client, games, people):
    sam, alex, crew = people
    two = onboarded_client.get(
        f"/people/options?named_group_id={crew.id}&lane=two"
    ).text
    assert (
        "You and 2 others make 3 players. This looks like the Three to four lane."
        in two
    )
    solo = onboarded_client.get(f"/people/options?people={sam.id}&lane=solo").text
    assert "This looks like the Two players lane." in solo
    assert (
        "This looks like"
        not in onboarded_client.get(f"/people/options?people={sam.id}&lane=two").text
    )


def test_people_options_without_a_group_or_people(onboarded_client, games, people):
    r = onboarded_client.get("/people/options?named_group_id=&lane=two")
    assert checked_people(r.text) == []


def test_people_options_rewires_itself(onboarded_client, games, people):
    raw = onboarded_client.get("/people/options?lane=two").text
    # Ticking someone re-checks only the hint, so focus stays on the checkbox.
    assert 'hx-get="/people/options?lane=two"' in raw
    assert 'hx-select="#people-hint"' in raw and 'hx-target="#people-hint"' in raw


def test_people_options_unknown_group_ticks_nothing(onboarded_client, games, people):
    r = onboarded_client.get("/people/options?named_group_id=9999&lane=two")
    assert r.status_code == 200 and checked_people(r.text) == []


def test_people_options_rejects_bad_values(onboarded_client, games, people):
    assert onboarded_client.get("/people/options?lane=everyone").status_code == 422
    assert onboarded_client.get("/people/options?lane=two&people=x").status_code == 400
    assert (
        onboarded_client.get("/people/options?lane=two&named_group_id=x").status_code
        == 400
    )


def test_group_select_updates_who_played(onboarded_client, games, people):
    raw = dialog(onboarded_client, lane="two")
    select = re.search(r'<select[^>]*name="named_group_id"[^>]*>', raw)[0]
    assert 'hx-get="/people/options?lane=two"' in select
    assert 'hx-trigger="change"' in select and 'hx-target="#people-options"' in select


# --- everything needs a login ---------------------------------------------------


def test_dialog_routes_need_a_login(client, games):
    for method, url in (
        ("get", "/games/10/review"),
        ("post", "/games/10/review/solo"),
        ("post", "/games/10/review/solo/switch"),
        ("get", "/tags/preview"),
        ("get", "/people/options"),
        ("get", "/games/10/card"),
    ):
        r = getattr(client, method)(url, follow_redirects=False)
        assert r.status_code in (303, 307), url


def test_expired_session_redirects_the_whole_page_not_the_dialog(onboarded_client, games):
    onboarded_client.cookies.clear()
    r = onboarded_client.get(
        "/games/10/review", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/login"
    assert "<dialog" not in r.text
