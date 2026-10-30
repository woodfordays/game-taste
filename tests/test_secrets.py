import os

import pytest
from cryptography.fernet import InvalidToken

from app.main import create_app
from app.models import Setting
from app.services.secrets import (
    Vault,
    hash_password,
    load_or_create_key,
    load_or_create_session_secret,
    new_password_problem,
    verify_password,
)
from app.services.settings import (
    delete_setting,
    get_secret,
    get_setting,
    known_timezones,
    set_secret,
    set_setting,
)


def test_vault_roundtrip_and_key_reused(tmp_path):
    token = Vault(load_or_create_key(tmp_path / "secret.key")).encrypt("sample-key")
    assert token != "sample-key"
    assert (
        Vault(load_or_create_key(tmp_path / "secret.key")).decrypt(token)
        == "sample-key"
    )


def test_secret_setting_encrypted_at_rest(app, db):
    set_secret(db, app.state.vault, "tavily_api_key", "tvly-sample")
    row = db.get(Setting, "tavily_api_key")
    assert row.encrypted and "tvly-sample" not in row.value
    assert get_secret(db, app.state.vault, "tavily_api_key") == "tvly-sample"


def test_secrets_survive_restart(tmp_path, services):
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        set_secret(s, app1.state.vault, "steam_api_key", "steam-sample")
    app1.state.engine.dispose()
    app2 = create_app(tmp_path, services=services)
    with app2.state.session_factory() as s:
        assert get_secret(s, app2.state.vault, "steam_api_key") == "steam-sample"
    app2.state.engine.dispose()


def test_password_hashing():
    h = hash_password("sample-password")
    assert verify_password(h, "sample-password") and not verify_password(
        h, "wrong-password"
    )


def test_password_hash_is_not_the_password():
    assert "sample-password" not in hash_password("sample-password")


def test_key_is_created_once_and_reused(tmp_path):
    path = tmp_path / "secret.key"
    first = load_or_create_key(path)
    assert path.read_bytes() == first
    assert load_or_create_key(path) == first


def test_vault_rejects_a_token_from_another_key(tmp_path):
    token = Vault(load_or_create_key(tmp_path / "a.key")).encrypt("sample-key")
    other = Vault(load_or_create_key(tmp_path / "b.key"))
    with pytest.raises(InvalidToken):
        other.decrypt(token)


def test_key_file_is_written_whole_with_no_temp_file_left(tmp_path):
    load_or_create_key(tmp_path / "secret.key")
    load_or_create_session_secret(tmp_path / "session.secret")
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "secret.key",
        "session.secret",
    ]


def test_failed_write_leaves_neither_key_nor_temp_file(tmp_path, monkeypatch):
    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        load_or_create_key(tmp_path / "secret.key")
    assert list(tmp_path.iterdir()) == []


def test_key_and_secret_files_are_made_private(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "chmod", lambda path, mode: calls.append(mode))
    load_or_create_key(tmp_path / "secret.key")
    load_or_create_session_secret(tmp_path / "session.secret")
    assert calls and set(calls) == {0o600}


def test_chmod_failure_is_ignored(tmp_path, monkeypatch):
    def refuse(path, mode):
        raise OSError("not supported here")

    monkeypatch.setattr(os, "chmod", refuse)
    key = load_or_create_key(tmp_path / "secret.key")
    secret = load_or_create_session_secret(tmp_path / "session.secret")
    assert (tmp_path / "secret.key").read_bytes() == key
    assert (tmp_path / "session.secret").read_text() == secret


def test_session_secret_is_created_once_and_reused(tmp_path):
    path = tmp_path / "session.secret"
    first = load_or_create_session_secret(path)
    assert len(first) >= 32
    assert load_or_create_session_secret(path) == first
    assert load_or_create_session_secret(tmp_path / "other.secret") != first


def test_create_app_keeps_key_and_session_secret_across_restarts(tmp_path, services):
    app1 = create_app(tmp_path, services=services)
    app2 = create_app(tmp_path, services=services)
    app1.state.engine.dispose()
    app2.state.engine.dispose()
    assert app1.state.session_secret == app2.state.session_secret
    assert app1.state.session_secret == app1.state.paths.session_secret_path.read_text()
    assert app1.state.paths.key_path.is_file()


def test_get_setting_default_and_roundtrip(db):
    assert get_setting(db, "store_region") is None
    assert get_setting(db, "store_region", "us") == "us"
    set_setting(db, "store_region", "gb")
    assert get_setting(db, "store_region", "us") == "gb"


def test_set_setting_overwrites_and_commits(app, db):
    set_setting(db, "timezone", "UTC")
    set_setting(db, "timezone", "Europe/London")
    with app.state.session_factory() as other:
        assert get_setting(other, "timezone") == "Europe/London"
        assert other.query(Setting).count() == 1


def test_empty_string_is_a_value_not_a_missing_setting(db):
    set_setting(db, "steam_id", "")
    assert get_setting(db, "steam_id", "fallback") == ""


def test_delete_setting_removes_it_and_ignores_missing_keys(app, db):
    set_setting(db, "onboarding_step", "ai")
    delete_setting(db, "onboarding_step")
    delete_setting(db, "onboarding_step")
    assert get_setting(db, "onboarding_step") is None
    with app.state.session_factory() as other:
        assert get_setting(other, "onboarding_step") is None


def test_get_secret_missing_is_none(app, db):
    assert get_secret(db, app.state.vault, "steam_api_key") is None


def test_set_secret_overwrites_and_commits(app, db):
    set_secret(db, app.state.vault, "steam_api_key", "steam-one")
    set_secret(db, app.state.vault, "steam_api_key", "steam-two")
    with app.state.session_factory() as other:
        assert get_secret(other, app.state.vault, "steam_api_key") == "steam-two"
        assert other.query(Setting).count() == 1


def test_set_setting_over_a_secret_stores_plain_text(app, db):
    set_secret(db, app.state.vault, "steam_api_key", "steam-sample")
    set_setting(db, "steam_api_key", "plain")
    row = db.get(Setting, "steam_api_key")
    assert row.value == "plain" and not row.encrypted


def test_new_password_rules_name_the_field_and_the_fix():
    assert new_password_problem("sample-password", "sample-password") is None
    assert new_password_problem("short", "short") == (
        "password",
        "Use at least 8 characters.",
    )
    assert new_password_problem("sample-password", "other-password") == (
        "confirm",
        "The two passwords don't match.",
    )


def test_new_password_rules_check_the_length_first_and_count_characters():
    # A short password is reported even when the two differ.
    assert new_password_problem("short", "other")[0] == "password"
    assert new_password_problem("12345678", "12345678") is None
    assert new_password_problem("1234567", "1234567")[0] == "password"
    # Spaces are part of a password, not padding.
    assert new_password_problem("        ", "        ") is None


def test_the_new_password_messages_are_plain_text():
    for _field, message in (
        new_password_problem("short", "short"),
        new_password_problem("sample-password", "other-password"),
    ):
        assert type(message) is str


def test_known_timezones():
    zones = known_timezones()
    assert {"UTC", "Europe/London", "America/Chicago"} <= zones
    assert "Mars/Base" not in zones
    assert known_timezones() is zones  # computed once
