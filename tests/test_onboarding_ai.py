import html
import re

import pytest
from cryptography.fernet import Fernet
from fakes import VALID
from fastapi import Request
from sqlalchemy import select
from starlette.datastructures import FormData

from app.auth import advance_step
from app.models import AiCall, JobAssignment, Provider
from app.services.ai import Job, ProviderConfig, ProviderKind
from app.services.ai_setup import (
    AiSetup,
    CheckLine,
    ai_form_view,
    parse_ai_form,
    run_ai_checks,
    save_ai_setup,
    stored_ai_setup,
    submit_ai_form,
)
from app.services.secrets import Vault
from app.services.settings import get_secret, get_setting, set_secret
from app.web import render
from conftest import FIXED_NOW

UNREADABLE = "Saved key can't be read. Enter it again."


def form_of(data: dict) -> FormData:
    """A posted form: a list value becomes a repeated field."""
    items = []
    for name, value in data.items():
        for item in value if isinstance(value, list) else [value]:
            items.append((name, item))
    return FormData(items)


def blank_setup() -> AiSetup:
    return AiSetup(providers={}, jobs={}, tavily_key="")


def save_valid(db, app) -> None:
    """Store the VALID setup the way a finished onboarding step would."""
    setup, errors = parse_ai_form(form_of(VALID), blank_setup())
    assert errors == []
    save_ai_setup(db, app.state.vault, setup, now=FIXED_NOW)


def ai_page(client) -> str:
    return html.unescape(client.get("/onboarding/ai").text)


def store_with_other_key(db) -> None:
    """Leave saved keys that this app's key file can't read, as after replacing secret.key."""
    other = Vault(Fernet.generate_key())
    db.add(
        Provider(
            kind="anthropic",
            base_url=None,
            api_key_enc=other.encrypt("ant-old-key"),
            updated_at=FIXED_NOW,
        )
    )
    db.commit()
    set_secret(db, other, "tavily_api_key", "tvly-old-key")


# --- the page ---------------------------------------------------------------


def test_page_lists_providers_jobs_and_tavily(logged_in_client):
    text = logged_in_client.get("/onboarding/ai").text
    for label in [
        "Local",
        "OpenRouter",
        "OpenAI",
        "Anthropic",
        "Gemini",
        "Recommender",
        "Review judge",
        "Everyday tasks",
        "Tavily",
    ]:
        assert label in text


def test_page_shows_the_step_and_has_no_back_button(logged_in_client):
    text = ai_page(logged_in_client)
    assert "Step 2 of 5" in text and "Test and continue" in text
    # Step 1 can't be revisited once a password exists, so there is nowhere to go back to.
    assert "Back" not in text


def test_first_visit_starts_with_no_provider_rows(logged_in_client):
    text = ai_page(logged_in_client)
    assert 'name="providers"' not in text and 'name="key_' not in text
    assert 'id="provider-rows"' in text and 'id="add-provider"' in text


def test_tavily_link_opens_in_a_new_tab(logged_in_client):
    text = ai_page(logged_in_client)
    link = re.search(r"<a [^>]*href=\"https://app\.tavily\.com\"[^>]*>", text)
    assert link and 'target="_blank"' in link[0] and 'rel="noopener"' in link[0]
    assert "Where do I get a key?" in text


def test_test_button_shows_a_loading_state(logged_in_client):
    text = ai_page(logged_in_client)
    assert "hx-disabled-elt" in text and "htmx-indicator" in text
    assert "Testing connections..." in text


def test_jobs_offer_all_five_providers_and_a_blank_choice(logged_in_client):
    text = ai_page(logged_in_client)
    select = re.search(
        r"<select[^>]*name=\"job_judge_provider\".*?</select>", text, re.S
    )[0]
    for kind in ProviderKind:
        assert f'value="{kind.value}"' in select
    assert "Choose a provider" in select


# --- provider rows ----------------------------------------------------------


def test_provider_row_partial(logged_in_client):
    assert (
        'name="key_gemini"'
        in logged_in_client.get("/onboarding/ai/provider-row?kind=gemini").text
    )


def test_local_row_takes_a_url_and_an_optional_key(logged_in_client):
    text = ai_page_row(logged_in_client, "local")
    assert 'name="url_local"' in text and 'name="key_local"' in text
    assert "Optional" in text


def ai_page_row(client, kind: str) -> str:
    return html.unescape(client.get(f"/onboarding/ai/provider-row?kind={kind}").text)


def test_provider_row_for_an_unknown_kind_is_refused(logged_in_client):
    assert (
        logged_in_client.get("/onboarding/ai/provider-row?kind=bogus").status_code
        == 422
    )


def test_provider_row_shows_a_saved_key_hint(logged_in_client, db, app):
    save_valid(db, app)
    text = ai_page_row(logged_in_client, "anthropic")
    assert "Saved key ending in mple" in text and "ant-sample" not in text


def test_provider_row_is_a_fragment_and_marks_the_row(logged_in_client):
    text = logged_in_client.get("/onboarding/ai/provider-row?kind=openai").text
    assert "<html" not in text
    assert 'id="provider-openai"' in text and 'value="openai"' in text


# --- submitting -------------------------------------------------------------


def test_all_pass_saves_and_advances(logged_in_client, db, app, fake_ai):
    r = logged_in_client.post("/onboarding/ai", data=VALID, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"
    assert (
        app.state.vault.decrypt(db.get(Provider, "anthropic").api_key_enc)
        == "ant-sample"
    )
    assert (
        db.get(Provider, "local").api_key_enc is None
        and db.get(Provider, "local").base_url == "http://localhost:1234/v1"
    )
    assert (
        db.get(JobAssignment, "judge").provider_kind,
        db.get(JobAssignment, "judge").model,
    ) == ("local", "sample-local")
    assert (
        get_secret(db, app.state.vault, "tavily_api_key") == "tvly-sample"
        and len(fake_ai.calls) == 3
    )
    assert get_setting(db, "onboarding_step") == "steam"


def test_saved_rows_carry_the_app_clock(logged_in_client, db):
    logged_in_client.post("/onboarding/ai", data=VALID)
    db.expire_all()
    assert db.get(Provider, "anthropic").updated_at == FIXED_NOW
    assert db.get(JobAssignment, "everyday").updated_at == FIXED_NOW
    calls = db.scalars(select(AiCall)).all()
    assert len(calls) == 3 and {c.created_at for c in calls} == {FIXED_NOW}


def test_one_failure_saves_nothing_and_keeps_values(
    logged_in_client, db, fake_ai
):  # Review focus 2
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    response = logged_in_client.post("/onboarding/ai", data=VALID)
    text = html.unescape(response.text)
    assert (
        "Didn't connect: RuntimeError: unknown model" in text
        and "Connected" in text
        and 'value="sample-big"' in text
    )
    assert response.status_code == 200
    assert db.get(Provider, "anthropic") is None
    assert get_setting(db, "onboarding_step") in (None, "ai")


def test_every_check_is_reported(logged_in_client, fake_ai):
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    text = html.unescape(logged_in_client.post("/onboarding/ai", data=VALID).text)
    for label in [
        "Recommender: sample-big on Anthropic",
        "Review judge: sample-local on Local",
        "Everyday tasks: sample-local on Local",
        "Tavily web search",
    ]:
        assert label in text
    assert text.count("Connected") == 3


def test_failure_keeps_typed_values_in_the_form(logged_in_client, fake_ai):
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    text = html.unescape(logged_in_client.post("/onboarding/ai", data=VALID).text)
    assert 'id="provider-anthropic"' in text and 'id="provider-local"' in text
    assert 'id="provider-gemini"' not in text
    assert 'value="http://localhost:1234/v1"' in text
    assert 'value="ant-sample"' in text and 'value="tvly-sample"' in text
    judge = re.search(r"<select[^>]*job_judge_provider.*?</select>", text, re.S)[0]
    assert re.search(r'value="local"\s+selected', judge)


def test_failing_local_test_hints_at_the_url(
    logged_in_client, fake_ai
):  # Review focus 2
    fake_ai.fail("sample-local", ConnectionError("refused"))
    text = html.unescape(logged_in_client.post("/onboarding/ai", data=VALID).text)
    assert "ConnectionError: refused" in text
    assert "Most local servers end in /v1." in text


def test_tavily_failure_is_reported_and_nothing_is_saved(
    logged_in_client, db, fake_search
):
    fake_search.fail(RuntimeError("invalid api key"))
    text = html.unescape(logged_in_client.post("/onboarding/ai", data=VALID).text)
    assert "Tavily web search" in text
    assert "Didn't connect: RuntimeError: invalid api key" in text
    assert db.get(Provider, "anthropic") is None
    assert get_setting(db, "onboarding_step") in (None, "ai")


def test_job_without_provider_key_is_caught_before_testing(
    logged_in_client, fake_ai, fake_search
):
    data = {**VALID, "job_judge_provider": "gemini"}
    text = html.unescape(logged_in_client.post("/onboarding/ai", data=data).text)
    assert (
        "Add a key for Gemini, or pick another provider for the Review judge." in text
        and fake_ai.calls == []
        and fake_search.calls == []
    )


def test_added_provider_without_a_key_is_caught(logged_in_client, fake_ai):
    data = {**VALID, "providers": ["anthropic", "local", "gemini"]}
    data["job_judge_provider"] = "gemini"
    text = html.unescape(logged_in_client.post("/onboarding/ai", data=data).text)
    assert (
        "Add a key for Gemini, or pick another provider for the Review judge." in text
    )
    assert fake_ai.calls == []


@pytest.mark.parametrize(
    "change, message",
    [
        ({"job_everyday_model": ""}, "Enter a model name for the Everyday tasks."),
        ({"job_recommender_model": "   "}, "Enter a model name for the Recommender."),
        ({"url_local": ""}, "Enter the server URL for Local."),
        ({"tavily_key": ""}, "Add a Tavily key. All game research uses it."),
    ],
)
def test_parse_errors_are_shown_and_nothing_is_tested(
    logged_in_client, db, fake_ai, fake_search, change, message
):
    text = html.unescape(
        logged_in_client.post("/onboarding/ai", data={**VALID, **change}).text
    )
    assert message in text
    assert fake_ai.calls == [] and fake_search.calls == []
    assert db.get(Provider, "anthropic") is None


def test_a_blank_tavily_key_never_builds_a_search_client(logged_in_client, fake_search):
    # A blank key would put the Tavily SDK in keyless mode, so it must stop at parsing.
    built = []
    logged_in_client.app.state.services.search_factory = lambda key: built.append(key)
    logged_in_client.post("/onboarding/ai", data={**VALID, "tavily_key": ""})
    assert built == []


def test_htmx_failure_swaps_the_form_with_status_200(logged_in_client, fake_ai):
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    r = logged_in_client.post(
        "/onboarding/ai", data=VALID, headers={"HX-Request": "true"}
    )
    text = html.unescape(r.text)
    assert r.status_code == 200
    assert "<html" not in text and 'id="ai-form"' in text
    assert "Didn't connect: RuntimeError: unknown model" in text


def test_htmx_parse_errors_swap_the_form_with_status_200(logged_in_client):
    r = logged_in_client.post(
        "/onboarding/ai",
        data={**VALID, "tavily_key": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and 'id="ai-form"' in r.text and "<html" not in r.text


def test_htmx_success_redirects_the_whole_page(logged_in_client):
    r = logged_in_client.post(
        "/onboarding/ai",
        data=VALID,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/onboarding/steam"


def test_provider_error_text_is_escaped(logged_in_client, fake_ai):
    fake_ai.fail("sample-big", RuntimeError("<script>alert(1)</script>"))
    text = logged_in_client.post("/onboarding/ai", data=VALID).text
    assert "<script>alert(1)" not in text
    assert "&lt;script&gt;alert(1)" in text


def test_model_name_is_escaped_in_labels_and_fields(logged_in_client, fake_ai):
    name = '"><b>x</b>'
    fake_ai.fail(name, RuntimeError("no"))
    text = logged_in_client.post(
        "/onboarding/ai", data={**VALID, "job_recommender_model": name}
    ).text
    assert "<b>x</b>" not in text


# --- coming back to the step, and saved keys --------------------------------


def test_blank_keys_keep_the_saved_ones(logged_in_client, db, app, fake_ai):
    save_valid(db, app)
    data = {**VALID, "key_anthropic": "", "tavily_key": ""}
    r = logged_in_client.post("/onboarding/ai", data=data, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"
    db.expire_all()
    assert (
        app.state.vault.decrypt(db.get(Provider, "anthropic").api_key_enc)
        == "ant-sample"
    )
    assert get_secret(db, app.state.vault, "tavily_api_key") == "tvly-sample"
    # The saved key is what got tested.
    assert len(fake_ai.calls) == 3


def test_a_typed_key_replaces_the_saved_one(logged_in_client, db, app):
    save_valid(db, app)
    logged_in_client.post(
        "/onboarding/ai", data={**VALID, "key_anthropic": "ant-newer"}
    )
    db.expire_all()
    assert (
        app.state.vault.decrypt(db.get(Provider, "anthropic").api_key_enc)
        == "ant-newer"
    )


def test_back_to_the_step_shows_saved_values_and_masks_keys(logged_in_client, db, app):
    save_valid(db, app)
    advance_step(db, "steam")
    text = ai_page(logged_in_client)
    assert 'id="provider-anthropic"' in text and 'id="provider-local"' in text
    assert 'id="provider-gemini"' not in text
    assert 'value="sample-big"' in text and 'value="http://localhost:1234/v1"' in text
    assert "Saved key ending in mple" in text
    assert "ant-sample" not in text and "tvly-sample" not in text


def test_failed_retest_with_blank_keys_does_not_echo_saved_keys(
    logged_in_client, db, app, fake_ai
):
    save_valid(db, app)
    fake_ai.fail("new-model", RuntimeError("unknown model"))
    data = {
        **VALID,
        "key_anthropic": "",
        "tavily_key": "",
        "job_recommender_model": "new-model",
    }
    text = html.unescape(logged_in_client.post("/onboarding/ai", data=data).text)
    assert "Didn't connect: RuntimeError: unknown model" in text
    assert "ant-sample" not in text and "tvly-sample" not in text
    assert "Saved key ending in mple" in text
    db.expire_all()
    assert db.get(JobAssignment, "recommender").model == "sample-big"


def test_unreadable_saved_keys_show_a_message_instead_of_crashing(logged_in_client, db):
    store_with_other_key(db)
    r = logged_in_client.get("/onboarding/ai")
    assert r.status_code == 200
    # One message beside the Anthropic key and one beside the Tavily key.
    assert html.unescape(r.text).count(UNREADABLE) == 2
    assert "ant-old-key" not in r.text and "tvly-old-key" not in r.text


def test_unreadable_saved_keys_can_be_replaced(logged_in_client, db, app):
    store_with_other_key(db)
    r = logged_in_client.post("/onboarding/ai", data=VALID, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"
    db.expire_all()
    assert (
        app.state.vault.decrypt(db.get(Provider, "anthropic").api_key_enc)
        == "ant-sample"
    )
    assert get_secret(db, app.state.vault, "tavily_api_key") == "tvly-sample"


def test_blank_field_over_an_unreadable_key_counts_as_no_key(
    logged_in_client, db, fake_ai
):
    store_with_other_key(db)
    data = {**VALID, "key_anthropic": "", "tavily_key": ""}
    text = html.unescape(logged_in_client.post("/onboarding/ai", data=data).text)
    assert (
        "Add a key for Anthropic, or pick another provider for the Recommender." in text
    )
    assert "Add a Tavily key. All game research uses it." in text
    assert text.count(UNREADABLE) == 2
    assert fake_ai.calls == []


# --- the service functions --------------------------------------------------


def test_stored_setup_is_blank_when_nothing_is_saved(db, app):
    setup = stored_ai_setup(db, app.state.vault)
    assert setup == AiSetup(providers={}, jobs={}, tavily_key="")


def test_stored_setup_round_trips(db, app):
    save_valid(db, app)
    setup = stored_ai_setup(db, app.state.vault)
    assert setup.providers == {
        ProviderKind.LOCAL: ProviderConfig(
            ProviderKind.LOCAL, None, "http://localhost:1234/v1"
        ),
        ProviderKind.ANTHROPIC: ProviderConfig(
            ProviderKind.ANTHROPIC, "ant-sample", None
        ),
    }
    assert setup.jobs == {
        Job.RECOMMENDER: (ProviderKind.ANTHROPIC, "sample-big"),
        Job.JUDGE: (ProviderKind.LOCAL, "sample-local"),
        Job.EVERYDAY: (ProviderKind.LOCAL, "sample-local"),
    }
    assert setup.tavily_key == "tvly-sample" and not setup.unreadable


def test_stored_setup_marks_unreadable_keys(db, app):
    store_with_other_key(db)
    setup = stored_ai_setup(db, app.state.vault)
    assert setup.providers[ProviderKind.ANTHROPIC].api_key is None
    assert setup.tavily_key == ""
    assert set(setup.unreadable) == {"anthropic", "tavily"}


def test_parse_valid_form():
    setup, errors = parse_ai_form(form_of(VALID), blank_setup())
    assert errors == []
    assert setup.providers[ProviderKind.LOCAL] == ProviderConfig(
        ProviderKind.LOCAL, None, "http://localhost:1234/v1"
    )
    assert setup.providers[ProviderKind.ANTHROPIC].api_key == "ant-sample"
    assert setup.jobs[Job.JUDGE] == (ProviderKind.LOCAL, "sample-local")
    assert setup.tavily_key == "tvly-sample"


def test_parse_accepts_a_plain_dict():
    setup, errors = parse_ai_form(VALID, blank_setup())
    assert errors == [] and setup.tavily_key == "tvly-sample"


def test_parse_strips_pasted_whitespace():
    data = {
        **VALID,
        "key_anthropic": "  ant-sample\n",
        "job_recommender_model": " sample-big ",
        "url_local": " http://localhost:1234/v1 ",
        "tavily_key": "tvly-sample ",
    }
    setup, errors = parse_ai_form(form_of(data), blank_setup())
    assert errors == []
    assert setup.providers[ProviderKind.ANTHROPIC].api_key == "ant-sample"
    assert setup.providers[ProviderKind.LOCAL].base_url == "http://localhost:1234/v1"
    assert setup.jobs[Job.RECOMMENDER][1] == "sample-big"
    assert setup.tavily_key == "tvly-sample"


def test_parse_enters_each_provider_once_and_ignores_unknown_kinds():
    data = {
        **VALID,
        "providers": ["anthropic", "bogus", "local", "anthropic"],
        "key_gemini": "gem-sample",
    }
    setup, errors = parse_ai_form(form_of(data), blank_setup())
    assert errors == []
    assert list(setup.providers) == [ProviderKind.ANTHROPIC, ProviderKind.LOCAL]


def test_parse_blank_key_keeps_the_stored_one():
    stored = AiSetup(
        providers={
            ProviderKind.ANTHROPIC: ProviderConfig(
                ProviderKind.ANTHROPIC, "ant-old", None
            )
        },
        jobs={},
        tavily_key="tvly-old",
    )
    data = {**VALID, "key_anthropic": "", "tavily_key": ""}
    setup, errors = parse_ai_form(form_of(data), stored)
    assert errors == []
    assert setup.providers[ProviderKind.ANTHROPIC].api_key == "ant-old"
    assert setup.tavily_key == "tvly-old"


def test_parse_local_may_have_no_key():
    setup, errors = parse_ai_form(form_of(VALID), blank_setup())
    assert errors == [] and setup.providers[ProviderKind.LOCAL].api_key is None


def test_parse_reports_every_problem_in_page_order():
    data = {
        "providers": ["local", "openai"],
        "url_local": "",
        "key_openai": "",
        "job_recommender_provider": "openai",
        "job_recommender_model": "",
        "job_judge_provider": "anthropic",
        "job_judge_model": "sample-big",
        "job_everyday_provider": "local",
        "job_everyday_model": "sample-local",
        "tavily_key": "",
    }
    _, errors = parse_ai_form(form_of(data), blank_setup())
    assert errors == [
        "Enter the server URL for Local.",
        "Add a key for OpenAI, or pick another provider for the Recommender.",
        "Enter a model name for the Recommender.",
        "Add a key for Anthropic, or pick another provider for the Review judge.",
        "Add a Tavily key. All game research uses it.",
    ]


def test_parse_a_job_with_no_provider_chosen():
    data = {**VALID, "job_judge_provider": ""}
    setup, errors = parse_ai_form(form_of(data), blank_setup())
    assert errors == ["Pick a provider for the Review judge."]
    # The model that was typed is kept for the re-render.
    assert setup.jobs[Job.JUDGE] == (None, "sample-local")


def test_parse_keeps_the_unreadable_mark_until_a_key_is_typed(db, app):
    store_with_other_key(db)
    stored = stored_ai_setup(db, app.state.vault)
    blank, _ = parse_ai_form(
        form_of({**VALID, "key_anthropic": "", "tavily_key": ""}), stored
    )
    assert set(blank.unreadable) == {"anthropic", "tavily"}
    typed, errors = parse_ai_form(form_of(VALID), stored)
    assert errors == [] and not typed.unreadable


def test_run_checks_labels_every_check_in_order(db, services):
    setup, _ = parse_ai_form(form_of(VALID), blank_setup())
    lines = run_ai_checks(db, setup, services)
    assert [line.label for line in lines] == [
        "Recommender: sample-big on Anthropic",
        "Review judge: sample-local on Local",
        "Everyday tasks: sample-local on Local",
        "Tavily web search",
    ]
    assert all(isinstance(line, CheckLine) and line.result.ok for line in lines)


def test_run_checks_reports_each_failure_separately(db, services, fake_ai, fake_search):
    fake_ai.fail("sample-big", RuntimeError("no such model"))
    fake_search.fail(RuntimeError("bad key"))
    setup, _ = parse_ai_form(form_of(VALID), blank_setup())
    lines = run_ai_checks(db, setup, services)
    assert [line.result.ok for line in lines] == [False, True, True, False]
    assert "RuntimeError: no such model" in lines[0].result.message
    assert "RuntimeError: bad key" in lines[3].result.message


def test_run_checks_refuses_a_blank_tavily_key(db, services, fake_search):
    setup, _ = parse_ai_form(form_of(VALID), blank_setup())
    setup.tavily_key = ""
    lines = run_ai_checks(db, setup, services)
    assert lines[-1].label == "Tavily web search" and not lines[-1].result.ok
    assert fake_search.calls == []


def test_save_updates_existing_rows_and_keeps_others(db, app):
    save_valid(db, app)
    setup = stored_ai_setup(db, app.state.vault)
    setup.providers[ProviderKind.ANTHROPIC] = ProviderConfig(
        ProviderKind.ANTHROPIC, "ant-newer", None
    )
    setup.jobs[Job.JUDGE] = (ProviderKind.ANTHROPIC, "sample-judge")
    setup.tavily_key = "tvly-newer"
    del setup.providers[ProviderKind.LOCAL]
    del setup.jobs[Job.EVERYDAY]
    later = FIXED_NOW.replace(hour=15)
    save_ai_setup(db, app.state.vault, setup, now=later)
    db.expire_all()
    anthropic = db.get(Provider, "anthropic")
    assert app.state.vault.decrypt(anthropic.api_key_enc) == "ant-newer"
    assert anthropic.updated_at == later
    judge = db.get(JobAssignment, "judge")
    assert (judge.provider_kind, judge.model) == ("anthropic", "sample-judge")
    # Rows the new setup leaves out are not deleted.
    assert db.get(Provider, "local") is not None
    assert db.get(JobAssignment, "everyday") is not None
    assert get_secret(db, app.state.vault, "tavily_api_key") == "tvly-newer"


def test_save_encrypts_keys(db, app):
    save_valid(db, app)
    stored = db.get(Provider, "anthropic").api_key_enc
    assert "ant-sample" not in stored


# --- reuse by Settings ------------------------------------------------------


def test_form_renders_in_settings_mode(app, onboarded_client, db):
    save_valid(db, app)

    @app.get("/_settings_probe")
    def probe(request: Request):
        stored = stored_ai_setup(db, app.state.vault)
        return render(
            request,
            "partials/ai_form.html",
            mode="settings",
            form_action="/settings/ai",
            form=ai_form_view(stored, stored),
        )

    text = html.unescape(onboarded_client.get("/_settings_probe").text)
    assert (
        'action="/settings/ai"' in text and 'hx-get="/settings/ai/provider-row"' in text
    )
    assert "Test and save" in text and "Test and continue" not in text
    assert "em-btn--primary" not in text
    assert "Saved key ending in mple" in text and "ant-sample" not in text


# --- the submit Settings shares ---------------------------------------------


def test_submit_with_missing_details_tests_nothing(db, app, services, fake_ai):
    result = submit_ai_form(
        db, app.state.vault, form_of({**VALID, "tavily_key": ""}), services, now=FIXED_NOW
    )
    assert not result.saved and result.checks is None
    assert result.errors == ["Add a Tavily key. All game research uses it."]
    assert fake_ai.calls == [] and db.get(Provider, "anthropic") is None
    # The typed values come back, next to what is saved (nothing yet).
    assert result.setup.jobs[Job.JUDGE] == (ProviderKind.LOCAL, "sample-local")
    assert result.stored.providers == {}


def test_submit_with_a_failed_check_saves_nothing(db, app, services, fake_ai):
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    result = submit_ai_form(
        db, app.state.vault, form_of(VALID), services, now=FIXED_NOW
    )
    assert not result.saved and result.errors == []
    assert [line.result.ok for line in result.checks] == [False, True, True, True]
    assert db.get(Provider, "anthropic") is None
    assert result.setup.providers[ProviderKind.ANTHROPIC].api_key == "ant-sample"
    assert result.stored.providers == {}


def test_submit_saves_and_returns_what_is_saved(db, app, services):
    result = submit_ai_form(
        db, app.state.vault, form_of(VALID), services, now=FIXED_NOW
    )
    assert result.saved and result.errors == []
    assert all(line.result.ok for line in result.checks)
    assert db.get(Provider, "anthropic").updated_at == FIXED_NOW
    # Once saved, what the form should show is what is saved.
    assert result.setup == result.stored
    assert result.stored.tavily_key == "tvly-sample"
    assert result.stored.providers[ProviderKind.ANTHROPIC].api_key == "ant-sample"


def test_submit_blank_keys_keep_the_saved_ones(db, app, services):
    save_valid(db, app)
    data = {**VALID, "key_anthropic": "", "tavily_key": ""}
    result = submit_ai_form(db, app.state.vault, form_of(data), services, now=FIXED_NOW)
    assert result.saved
    assert result.stored.providers[ProviderKind.ANTHROPIC].api_key == "ant-sample"
    assert result.stored.tavily_key == "tvly-sample"
