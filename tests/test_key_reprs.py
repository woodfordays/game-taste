"""Keys are never printed: not in a repr, so not in a log line, a traceback or a debugger."""

from fakes import VALID

from app.routes.settings import SteamView
from app.services.ai import ProviderConfig, ProviderKind, make_adapter
from app.services.ai_setup import (
    AiSetup,
    KeyField,
    ProviderRow,
    ai_form_view,
    parse_ai_form,
)
from app.services.library import StoredSteam, stored_steam
from app.services.search import make_search_client
from app.services.settings import set_secret, set_setting

PROVIDER_KEY = "sk-sample-provider-key-1234"
TAVILY_KEY = "tvly-sample-search-key-5678"
STEAM_KEY = "steam-sample-key-9012"
TYPED_KEY = "typed-sample-key-3456"


def assert_no_key(value, *keys: str) -> None:
    text = repr(value)
    for key in keys:
        assert key not in text, f"{type(value).__name__} printed a key"


def test_a_provider_config_does_not_print_its_key():
    config = ProviderConfig(ProviderKind.OPENAI, PROVIDER_KEY, None)
    assert_no_key(config, PROVIDER_KEY)
    assert "OPENAI" in repr(config) or "openai" in repr(config)  # the rest still shows
    assert config.api_key == PROVIDER_KEY  # and the key itself is still there to use


def test_an_ai_setup_does_not_print_any_key():
    setup = AiSetup(
        providers={
            ProviderKind.OPENAI: ProviderConfig(
                ProviderKind.OPENAI, PROVIDER_KEY, None
            ),
            ProviderKind.LOCAL: ProviderConfig(
                ProviderKind.LOCAL, TYPED_KEY, "http://host.docker.internal:1234/v1"
            ),
        },
        jobs={},
        tavily_key=TAVILY_KEY,
    )
    assert_no_key(setup, PROVIDER_KEY, TYPED_KEY, TAVILY_KEY)
    assert "host.docker.internal" in repr(setup)  # only the keys are left out


def test_a_parsed_form_does_not_print_the_keys_typed_into_it():
    blank = AiSetup(providers={}, jobs={}, tavily_key="")
    form = {**VALID, "key_anthropic": PROVIDER_KEY, "tavily_key": TAVILY_KEY}
    setup, errors = parse_ai_form(form, blank)
    assert errors == []
    assert setup.providers[ProviderKind.ANTHROPIC].api_key == PROVIDER_KEY
    assert_no_key(setup, PROVIDER_KEY, TAVILY_KEY)


def test_the_form_view_does_not_print_a_typed_key():
    field = KeyField(value=TYPED_KEY, saved="Saved key ending in 1234", error="")
    assert_no_key(field, TYPED_KEY)
    assert "Saved key ending in 1234" in repr(field)
    row = ProviderRow(ProviderKind.OPENAI, "OpenAI", "", field)
    assert_no_key(row, TYPED_KEY)
    assert field.value == TYPED_KEY  # the form still gets it back


def test_the_ai_form_view_does_not_print_any_key():
    setup = AiSetup(
        providers={
            ProviderKind.OPENAI: ProviderConfig(ProviderKind.OPENAI, TYPED_KEY, None)
        },
        jobs={},
        tavily_key=TAVILY_KEY,
    )
    view = ai_form_view(setup, AiSetup(providers={}, jobs={}, tavily_key=""))
    assert view.rows[0].key.value == TYPED_KEY and view.tavily.value == TAVILY_KEY
    assert_no_key(view, TYPED_KEY, TAVILY_KEY)


def test_the_saved_ai_setup_does_not_print_its_keys(saved_ai_setup):
    assert saved_ai_setup.providers[ProviderKind.ANTHROPIC].api_key == "ant-sample"
    assert_no_key(saved_ai_setup, "ant-sample", "tvly-sample")


def test_stored_steam_does_not_print_its_key():
    stored = StoredSteam(STEAM_KEY, "76561198000000001")
    assert_no_key(stored, STEAM_KEY)
    assert "76561198000000001" in repr(stored) and stored.api_key == STEAM_KEY


def test_the_stored_steam_read_from_the_database_does_not_print_its_key(db, app):
    set_secret(db, app.state.vault, "steam_api_key", STEAM_KEY)
    set_setting(db, "steam_id", "76561198000000001")
    stored = stored_steam(db, app.state.vault)
    assert stored.api_key == STEAM_KEY
    assert_no_key(stored, STEAM_KEY)


def test_the_steam_settings_view_does_not_print_a_typed_key():
    view = SteamView(
        steam_id=None, key_hint="", key=TYPED_KEY, profile="sample-profile"
    )
    assert view.key == TYPED_KEY
    assert_no_key(view, TYPED_KEY)
    assert "sample-profile" in repr(view)


def test_the_adapters_and_the_search_client_do_not_print_their_keys():
    for kind in (
        ProviderKind.OPENAI,
        ProviderKind.OPENROUTER,
        ProviderKind.ANTHROPIC,
        ProviderKind.GEMINI,
    ):
        assert_no_key(
            make_adapter(ProviderConfig(kind, PROVIDER_KEY, None)), PROVIDER_KEY
        )
    assert_no_key(make_search_client(TAVILY_KEY), TAVILY_KEY)
