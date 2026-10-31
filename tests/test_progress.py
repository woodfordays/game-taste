"""The moving bars shown while a slow request is out: model checks, Steam imports, syncs and searches."""

import html
import re
from pathlib import Path

CSS = (Path(__file__).parent.parent / "app" / "static" / "app.css").read_text(
    encoding="utf-8"
)


def busy_notes(text: str) -> list[str]:
    return re.findall(
        r'<div id="[^"]*" class="htmx-indicator busy-note" role="status">.*?</div>\s*</div>\s*</div>',
        html.unescape(text),
        re.S,
    )


def note_with(text: str, label: str) -> str:
    for note in busy_notes(text):
        if label in note:
            return note
    raise AssertionError(f"no busy note saying {label!r}")


def check_moving_bar(note: str, note_id: str, label: str) -> None:
    """Ember's Progress, moving: the visible label names the track, which is the progressbar."""
    assert note.startswith(
        f'<div id="{note_id}" class="htmx-indicator busy-note" role="status">'
    )
    assert (
        'class="em-progress em-progress--ember em-progress--md is-indeterminate"'
        in note
    )
    assert (
        f'<span id="{note_id}-bar-label" class="em-progress__label text-label">{label}</span>'
        in note
    )
    track = re.search(r'<div class="em-progress__track"[^>]*>', note)[0]
    assert 'role="progressbar"' in track
    assert f'aria-labelledby="{note_id}-bar-label"' in track
    assert "aria-valuenow" not in track


def test_the_ai_check_shows_a_moving_bar_in_onboarding(logged_in_client):
    label = "Testing connections... Each check can take up to 30 seconds."
    text = logged_in_client.get("/onboarding/ai").text
    check_moving_bar(note_with(text, label), "ai-testing", label)


def test_the_steam_import_shows_a_moving_bar_in_onboarding(client_at_steam_step):
    label = "Importing your library... A large library can take a few seconds."
    text = client_at_steam_step.get("/onboarding/steam").text
    check_moving_bar(note_with(text, label), "steam-importing", label)


def test_settings_shows_moving_bars_for_the_ai_check_and_steam(onboarded_client):
    text = onboarded_client.get("/settings").text
    check_moving_bar(
        note_with(text, "Testing connections"),
        "ai-testing",
        "Testing connections... Each check can take up to 30 seconds.",
    )
    check_moving_bar(
        note_with(text, "Saving..."),
        "steam-saving",
        "Saving... Importing a large library can take a few seconds.",
    )


def test_the_library_sync_and_store_search_show_moving_bars(
    onboarded_client, synced_games
):
    text = onboarded_client.get("/library").text
    check_moving_bar(
        note_with(text, "Syncing your library..."),
        "library-syncing",
        "Syncing your library...",
    )
    check_moving_bar(
        note_with(text, "Searching the Steam store..."),
        "store-searching",
        "Searching the Steam store...",
    )
    # The buttons name the notes, so htmx reveals them while the request is out.
    assert 'hx-indicator="#library-syncing"' in text
    assert 'hx-indicator="#store-searching"' in text


def test_every_label_id_on_a_page_is_unique(onboarded_client, synced_games):
    for url in ("/settings", "/library"):
        ids = re.findall(r'\sid="([^"]+)"', onboarded_client.get(url).text)
        assert len(ids) == len(set(ids)), url


def test_a_note_is_hidden_until_its_request_is_out():
    assert re.search(r"\.busy-note\s*\{\s*display:\s*none;", CSS)
    assert re.search(
        r"\.htmx-request \.busy-note,\s*\.busy-note\.htmx-request\s*\{\s*display:\s*block;",
        CSS,
    )


def test_a_note_in_a_column_is_only_as_tall_as_its_bar():
    """A flex basis on the note would be its height in a column (the Steam import card, the
    store search panel), so only notes beside a button in a row get one."""
    base = re.search(r"\.busy-note\s*\{([^}]*)\}", CSS)[1]
    assert "flex" not in base
    row = re.search(
        r":is\(\.ai-actions, \.settings-actions, \.library-head__actions\) > \.busy-note\s*\{([^}]*)\}",
        CSS,
    )
    assert row and "flex: 1 1 14rem;" in row[1]


def test_no_spinner_notes_are_left():
    templates = Path(__file__).parent.parent / "app" / "templates"
    for path in templates.rglob("*.html"):
        assert "em-spinner" not in path.read_text(encoding="utf-8"), path.name
