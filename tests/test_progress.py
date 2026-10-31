"""The moving bars shown while a slow request is out: model checks, Steam imports, syncs and searches."""

import html
import re
from pathlib import Path

CSS = (Path(__file__).parent.parent / "app" / "static" / "app.css").read_text(
    encoding="utf-8"
)


def busy_notes(text: str) -> list[str]:
    return re.findall(
        r'<div(?: id="[^"]*")? class="htmx-indicator busy-note[^"]*" role="status">.*?</div>\s*</div>\s*</div>',
        html.unescape(text),
        re.S,
    )


def note_with(text: str, label: str) -> str:
    for note in busy_notes(text):
        if label in note:
            return note
    raise AssertionError(f"no busy note saying {label!r}")


def check_moving_bar(note: str, label: str) -> None:
    assert 'class="em-progress em-progress--ember is-indeterminate"' in note
    assert 'role="progressbar"' in note and f'aria-label="{label}"' in note
    assert "aria-valuenow" not in note
    assert f'<span class="em-progress__label text-label">{label}</span>' in note


def test_the_ai_check_shows_a_moving_bar_in_onboarding(logged_in_client):
    label = "Testing connections... Each check can take up to 30 seconds."
    text = logged_in_client.get("/onboarding/ai").text
    check_moving_bar(note_with(text, label), label)


def test_the_steam_import_shows_a_moving_bar_in_onboarding(client_at_steam_step):
    label = "Importing your library... A large library can take a few seconds."
    text = client_at_steam_step.get("/onboarding/steam").text
    check_moving_bar(note_with(text, label), label)


def test_settings_shows_moving_bars_for_the_ai_check_and_steam(onboarded_client):
    text = onboarded_client.get("/settings").text
    for label in (
        "Testing connections... Each check can take up to 30 seconds.",
        "Saving... Importing a large library can take a few seconds.",
    ):
        check_moving_bar(note_with(text, label), label)


def test_the_library_sync_and_store_search_show_moving_bars(
    onboarded_client, synced_games
):
    text = onboarded_client.get("/library").text
    sync = note_with(text, "Syncing your library...")
    search = note_with(text, "Searching the Steam store...")
    assert sync.startswith('<div id="library-syncing"')
    assert search.startswith('<div id="store-searching"')
    check_moving_bar(sync, "Syncing your library...")
    check_moving_bar(search, "Searching the Steam store...")
    # The buttons name the notes, so htmx reveals them while the request is out.
    assert 'hx-indicator="#library-syncing"' in text
    assert 'hx-indicator="#store-searching"' in text


def test_a_note_is_hidden_until_its_request_is_out():
    assert re.search(r"\.busy-note\s*\{\s*display:\s*none;", CSS)
    assert re.search(
        r"\.htmx-request \.busy-note,\s*\.busy-note\.htmx-request\s*\{\s*display:\s*block;",
        CSS,
    )


def test_no_spinner_notes_are_left():
    templates = Path(__file__).parent.parent / "app" / "templates"
    for path in templates.rglob("*.html"):
        assert "em-spinner" not in path.read_text(encoding="utf-8"), path.name
