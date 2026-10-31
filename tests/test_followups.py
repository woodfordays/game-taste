"""Plan 1 follow-ups: bounded ids, SQLite pragmas, quiet httpx logs, the review dialog's
options, and the toast region (above dialogs, with an error toast for failed requests)."""

import logging
import re

import pytest

from app.services.reviews import (
    CHOICE_OPTIONS,
    COMPLEXITIES,
    STATUSES,
    VERDICTS,
    WALK_AWAYS,
)
from app.web import parse_id


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12", 12),
        (" 7 ", 7),
        ("0", None),
        ("-1", None),
        ("", None),
        ("1.5", None),
        ("٣", None),
        ("9" * 5000, None),
        (str(2**63), None),
        (str(2**63 - 1), 2**63 - 1),
        (None, None),
    ],
)
def test_parse_id(value, expected):
    assert parse_id(value) == expected


@pytest.mark.parametrize("value", [12, 1.5, b"12", ["12"], True])
def test_parse_id_only_reads_text(value):
    assert parse_id(value) is None


@pytest.mark.parametrize(
    "path",
    [
        "/people/options?named_group_id=99999999999999999999999&lane=two",
    ],
)
def test_huge_ids_never_500(onboarded_client, games, path):
    assert onboarded_client.get(path).status_code in (200, 404, 422)


def test_huge_ids_in_dialog_posts_are_refused(onboarded_client, games):
    r = onboarded_client.post(
        "/games/10/review/two",
        data={
            "action": "draft",
            "text": "x",
            "named_group_id": "9" * 30,
            "people": ["9" * 30],
        },
    )
    assert r.status_code == 200 and "Pick people from your list." in r.text


@pytest.mark.parametrize(
    "data",
    [
        {"named_group_id": "9" * 30},
        {"people": ["9" * 30]},
        {"people": ["0"]},
        {"people": ["-1"]},
        {"named_group_id": "0"},
        {"people": ["9" * 5000]},
        {"named_group_id": "9" * 5000},
    ],
)
def test_each_bad_id_is_refused_in_the_dialog(onboarded_client, games, data):
    r = onboarded_client.post(
        "/games/10/review/two", data={"action": "draft", "text": "x", **data}
    )
    assert r.status_code == 200 and "Pick people from your list." in r.text


def test_a_huge_id_in_a_lane_switch_is_refused(onboarded_client, games):
    r = onboarded_client.post(
        "/games/10/review/two/switch",
        data={"to": "solo", "dirty": "1", "people": ["9" * 30]},
    )
    assert r.status_code == 200 and "Pick people from your list." in r.text


def test_people_options_still_refuses_what_is_not_a_number(onboarded_client, games):
    assert onboarded_client.get("/people/options?lane=two&people=x").status_code == 400
    huge = "/people/options?lane=two&people=" + "9" * 30
    assert onboarded_client.get(huge).status_code == 400
    plain = "/people/options?lane=two&named_group_id=x"
    assert onboarded_client.get(plain).status_code == 400


def test_a_group_no_database_could_hold_is_an_unknown_group(
    onboarded_client, games, people
):
    for group_id in ("0", "9" * 30):
        r = onboarded_client.get(f"/people/options?lane=two&named_group_id={group_id}")
        assert r.status_code == 200 and "counts as played" not in r.text


def test_the_rating_keeps_its_own_message(onboarded_client, games):
    for rating in ("x", "9" * 5000):
        r = onboarded_client.post(
            "/games/10/review/solo", data={"action": "draft", "rating": rating}
        )
        assert r.status_code == 200 and "Pick a rating from 1 to 10." in r.text


def test_sqlite_uses_wal_and_busy_timeout(app):
    with app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000


def test_sqlite_still_enforces_foreign_keys(app):
    with app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_httpx_logs_nothing_below_warning(app):
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_httpx_logger_is_set_by_the_app_itself():
    # Its own level, not the root's: the root level can change, and httpx logs request URLs
    # (which carry the Steam key) at INFO.
    assert logging.getLogger("httpx").level == logging.WARNING


def test_dialog_options_come_from_the_service(onboarded_client, games):
    text = onboarded_client.get("/games/10/review?lane=solo").text
    for value in STATUSES + VERDICTS + WALK_AWAYS + COMPLEXITIES:
        assert f'value="{value}"' in text


def test_dialog_option_labels_are_the_ones_it_always_showed(onboarded_client, games):
    text = onboarded_client.get("/games/10/review?lane=solo").text

    def options(name):
        select = re.search(rf'<select[^>]*name="{name}".*?</select>', text, re.S)[0]
        return re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select)

    assert options("status") == [
        ("", "Not set"),
        ("finished", "Finished"),
        ("playing", "Playing"),
        ("demo", "Demo"),
        ("dropped", "Dropped"),
    ]
    assert options("verdict") == [
        ("", "Not set"),
        ("loved", "Loved"),
        ("liked", "Liked"),
        ("mixed", "Mixed"),
        ("disliked", "Disliked"),
    ]
    assert options("walk_away") == [
        ("", "Not set"),
        ("yes", "Yes"),
        ("partly", "Partly"),
        ("no", "No"),
    ]
    assert options("complexity") == [
        ("", "Not set"),
        ("too_light", "Too light"),
        ("about_right", "About right"),
        ("too_much", "Too much"),
    ]


def test_dialog_options_are_whatever_the_service_lists(
    onboarded_client, games, monkeypatch
):
    # The template holds no option list of its own.
    monkeypatch.setitem(CHOICE_OPTIONS, "status", [("paused", "Paused")])
    text = onboarded_client.get("/games/10/review?lane=solo").text
    assert '<option value="paused">Paused</option>' in text
    assert 'value="finished"' not in text


def test_every_choice_the_service_accepts_has_a_label():
    from app.services.reviews import _CHOICES

    for name, values in _CHOICES.items():
        assert [value for value, _label in CHOICE_OPTIONS[name]] == list(values)
        assert all(label for _value, label in CHOICE_OPTIONS[name])


def test_base_has_an_error_toast_hook(onboarded_client):
    text = onboarded_client.get("/").text
    assert "htmx:responseError" in text and "htmx:sendError" in text


def test_the_error_toast_says_what_happened_and_what_to_do(onboarded_client):
    text = onboarded_client.get("/").text
    assert 'addToast("Something went wrong. Try again.", "danger")' in text
    # It is made when a request fails, so a page that comes with no danger toast has none.
    assert "em-toast--danger" not in text


def test_the_toast_region_is_embers_popover_region(onboarded_client):
    text = onboarded_client.get("/").text
    region = re.search(r"<section\b[^>]*>(.*?)</section>", text, re.S)
    opening = re.search(r"<section\b[^>]*>", text)[0]
    assert 'class="em-toast-region"' in opening and 'popover="manual"' in opening
    assert 'aria-label="Notifications"' in opening
    # The list is #toasts, which is where out-of-band toasts are appended.
    assert re.search(
        r'<div id="toasts" class="em-toast-region__list" aria-live="polite" '
        r'aria-relevant="additions">',
        region[1],
    )


def test_the_script_puts_the_region_back_on_top_when_a_toast_arrives(
    onboarded_client,
):
    text = onboarded_client.get("/").text
    # Hidden and shown again, so it is the last thing in the top layer: above an open modal.
    assert "hidePopover" in text and "showPopover" in text
    assert "MutationObserver" in text


def test_the_region_goes_back_on_top_when_a_dialog_opens(onboarded_client):
    # A toast added before a dialog opens would otherwise end up under it. showModal() sets the
    # dialog's open attribute, so the script watches that.
    text = onboarded_client.get("/").text
    assert 'attributeFilter: ["open"]' in text and '"DIALOG"' in text


def test_a_toast_over_a_dialog_catches_the_click_meant_for_it(onboarded_client):
    # A modal dialog makes the toast inert, so without this a click on its Dismiss button would
    # land on the dialog's own button underneath, such as Publish review.
    text = onboarded_client.get("/").text
    assert "dialog:modal" in text and "stopPropagation" in text
    # Pointer presses are caught like the click, so the button under the toast isn't focused.
    assert '"pointerdown"' in text and '"mousedown"' in text and '"click"' in text


def test_the_click_guard_leaves_alone_what_it_cannot_be_covering(onboarded_client):
    text = onboarded_client.get("/").text
    # Only while the region is open (shown, so above the dialog), and not for a click from the
    # keyboard or a script, which has no pointer position (detail is 0).
    assert 'region.matches(":popover-open")' in text
    assert 'event.type === "click" && event.detail === 0' in text
    # Without popovers the toast is under the dialog and nothing is covered, so no guard at all.
    assert "if (popovers) {" in text


def test_a_failed_request_gets_one_toast_not_two(onboarded_client):
    text = onboarded_client.get("/").text
    # htmx can report one failure as a response error and a send error; both carry the same
    # request, so the script remembers which requests already have a toast.
    assert "WeakSet" in text


def test_pages_without_the_shell_still_have_the_region(client):
    text = client.get("/onboarding/password").text
    assert 'popover="manual"' in text and 'id="toasts"' in text
    assert "htmx:responseError" in text
