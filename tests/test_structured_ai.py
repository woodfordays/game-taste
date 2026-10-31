import hashlib
from types import SimpleNamespace

import jinja2
import pytest
from alembic import command
from alembic.config import Config
from anthropic.resources.messages import Messages as AnthropicMessages
from conftest import FIXED_NOW
from fakes import FakeAdapterFactory
from google.genai import types as genai_types
from google.genai.models import Models as GeminiModels
from openai.resources.chat.completions import Completions as OpenAICompletions
from pydantic import BaseModel, create_model
from sqlalchemy import create_engine, select, text

from app import prompts
from app.db import MIGRATIONS_DIR
from app.models import AiCall, Provider
from app.prompts import Prompt, load_prompt
from app.services import ai_setup
from app.services.ai import (
    AIError,
    AnthropicAdapter,
    GeminiAdapter,
    Job,
    OpenAICompatibleAdapter,
    ProviderConfig,
    ProviderKind,
    StructuredReplyError,
    call_and_log,
    complete_json,
    job_config,
)
from app.services.ai_setup import AiSetup

PROMPT = Prompt("test_prompt", "v1", 'Answer with {"value": 3}.')


class Answer(BaseModel):
    value: int


def ask(db, app, services, *, run_ref=None, prompt=PROMPT, context=None):
    return complete_json(
        db,
        app.state.vault,
        Job.EVERYDAY,
        prompt,
        context or {},
        Answer,
        services=services,
        run_ref=run_ref,
        now=FIXED_NOW,
    )


def logged_calls(db) -> list[AiCall]:
    db.expire_all()
    return list(db.scalars(select(AiCall).order_by(AiCall.id)))


# --- prompts -------------------------------------------------------------------


def test_load_prompt_versions_by_file(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    # newline="\n": the file's bytes are the same on every platform, so is the version.
    (tmp_path / "hello.md").write_text(
        "Hi {{ name }}\n", encoding="utf-8", newline="\n"
    )
    p = load_prompt("hello")
    assert (
        p.render(name="Sam") == "Hi Sam\n"
        and p.version == hashlib.sha256(b"Hi {{ name }}\n").hexdigest()
    )
    with pytest.raises(jinja2.UndefinedError):
        p.render()


def test_a_prompt_is_named_for_its_file_and_changes_version_with_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    (tmp_path / "hello.md").write_bytes(b"Hi\n")
    first = load_prompt("hello")
    (tmp_path / "hello.md").write_bytes(b"Hi there\n")
    second = load_prompt("hello")
    assert first.name == second.name == "hello"
    assert first.version != second.version


def test_prompts_do_not_escape_text_or_drop_the_final_newline():
    p = Prompt("t", "v", "Review: {{ text }}\n")
    assert p.render(text='<b>fun</b> & "quoted"') == 'Review: <b>fun</b> & "quoted"\n'


def test_a_missing_prompt_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_prompt("nope")


# --- complete_json -------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        '{"value": 3}',
        '```json\n{"value": 3}\n```',
        'Sure! {"value": 3} Hope that helps.',
        '```\n{"value": 3}\n```',
        'Here is the {format} you wanted:\n```json\n{"value": 3}\n```\nAnything else?',
        '  \n{"value": 3}\n\n',
    ],
)
def test_complete_json_tolerates_wrapping(
    db, app, services, fake_ai, saved_ai_setup, reply
):
    fake_ai.replies["sample-local"] = reply
    answer, call_id = complete_json(
        db,
        app.state.vault,
        Job.EVERYDAY,
        PROMPT,
        {},
        Answer,
        services=services,
        run_ref="test:1",
        now=FIXED_NOW,
    )
    row = db.get(AiCall, call_id)
    assert (
        answer.value == 3
        and row.prompt_name == PROMPT.name
        and row.prompt_version == PROMPT.version
        and row.run_ref == "test:1"
    )


def test_complete_json_returns_the_model_and_the_id_of_the_call_that_made_it(
    db, app, services, fake_ai, saved_ai_setup
):
    fake_ai.replies["sample-local"] = '{"value": 3}'
    answer, call_id = ask(db, app, services)
    assert isinstance(answer, Answer)
    [row] = logged_calls(db)
    assert call_id == row.id and row.job == "everyday" and row.model == "sample-local"


def test_complete_json_renders_the_prompt_with_its_context(
    db, app, services, fake_ai, saved_ai_setup
):
    fake_ai.replies["sample-local"] = '{"value": 3}'
    ask(
        db,
        app,
        services,
        prompt=Prompt("greet", "v1", "Hello {{ who }}"),
        context={"who": "Sam"},
    )
    assert fake_ai.calls == [(ProviderKind.LOCAL, "sample-local", "Hello Sam")]


def test_complete_json_retries_once_then_gives_up(
    db, app, services, fake_ai, saved_ai_setup
):  # Review focus 1
    fake_ai.replies["sample-local"] = ["not json", '{"value": "x"}']
    with pytest.raises(StructuredReplyError) as err:
        complete_json(
            db,
            app.state.vault,
            Job.EVERYDAY,
            PROMPT,
            {},
            Answer,
            services=services,
            run_ref=None,
            now=FIXED_NOW,
        )
    assert err.value.reason == "unusable" and len(fake_ai.calls) == 2
    assert "Your previous reply could not be used" in fake_ai.calls[1][2]


def test_the_retry_repeats_the_prompt_and_says_what_was_wrong(
    db, app, services, fake_ai, saved_ai_setup
):
    fake_ai.replies["sample-local"] = ["not json", '{"value": "x"}']
    with pytest.raises(StructuredReplyError):
        ask(db, app, services)
    first, second = fake_ai.calls[0][2], fake_ai.calls[1][2]
    assert first == PROMPT.source
    assert second.startswith(
        PROMPT.source + "\n\nYour previous reply could not be used: "
    )
    assert second.endswith(". Reply again with only the JSON object.")
    # The reply wasn't JSON at all, so the error says so; it isn't the validation error.
    assert "validation error" not in second


def test_the_retry_names_the_validation_error_when_the_json_is_the_wrong_shape(
    db, app, services, fake_ai, saved_ai_setup
):
    fake_ai.replies["sample-local"] = ['{"value": "x"}', '{"value": 3}']
    answer, _ = ask(db, app, services)
    assert answer.value == 3
    assert "validation error" in fake_ai.calls[1][2]


def test_the_error_in_the_retry_is_cut_to_300_characters(
    db, app, services, fake_ai, saved_ai_setup
):
    # Thirty missing fields make a validation error far longer than 300 characters.
    Many = create_model("Many", **{f"field_{n}": (int, ...) for n in range(30)})
    fake_ai.replies["sample-local"] = "{}"
    with pytest.raises(StructuredReplyError):
        complete_json(
            db,
            app.state.vault,
            Job.EVERYDAY,
            PROMPT,
            {},
            Many,
            services=services,
            run_ref=None,
            now=FIXED_NOW,
        )
    head = PROMPT.source + "\n\nYour previous reply could not be used: "
    tail = ". Reply again with only the JSON object."
    retry = fake_ai.calls[1][2]
    assert retry.startswith(head) and retry.endswith(tail)
    assert len(retry) - len(head) - len(tail) == 300


def test_complete_json_retry_that_works_returns_the_second_call(
    db, app, services, fake_ai, saved_ai_setup
):
    fake_ai.replies["sample-local"] = ["Sorry, I can't.", '{"value": 7}']
    answer, call_id = ask(db, app, services, run_ref="run:9")
    first, second = logged_calls(db)
    assert answer.value == 7 and len(fake_ai.calls) == 2
    assert call_id == second.id != first.id
    # Both calls are logged, under the same job, prompt, version and run.
    for row in (first, second):
        assert (row.job, row.prompt_name, row.prompt_version, row.run_ref) == (
            "everyday",
            PROMPT.name,
            PROMPT.version,
            "run:9",
        )
    assert first.response == "Sorry, I can't." and first.error is None


def test_complete_json_truncated_does_not_retry(
    db, app, services, fake_ai, saved_ai_setup
):  # Review focus 1
    fake_ai.replies["sample-local"] = '{"value": 3'
    fake_ai.stop_reasons["sample-local"] = "length"
    with pytest.raises(StructuredReplyError) as err:
        complete_json(
            db,
            app.state.vault,
            Job.EVERYDAY,
            PROMPT,
            {},
            Answer,
            services=services,
            run_ref=None,
            now=FIXED_NOW,
        )
    assert err.value.reason == "truncated" and len(fake_ai.calls) == 1
    assert (
        err.value.message
        == "The model's reply was cut off. Pick a model with a larger output limit in Settings."
    )
    assert str(err.value) == err.value.message


def test_a_reply_cut_off_on_the_retry_is_truncated_too(
    db, app, services, fake_ai, saved_ai_setup
):
    calls = []

    def reply(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "not json"
        # The fake reads the stop reason after the reply, so this applies to this call.
        fake_ai.stop_reasons["sample-local"] = "length"
        return '{"value": 3'

    fake_ai.replies["sample-local"] = reply
    with pytest.raises(StructuredReplyError) as err:
        ask(db, app, services)
    assert err.value.reason == "truncated" and len(fake_ai.calls) == 2


def test_complete_json_not_configured(db, app, services):
    with pytest.raises(StructuredReplyError) as err:
        complete_json(
            db,
            app.state.vault,
            Job.EVERYDAY,
            PROMPT,
            {},
            Answer,
            services=services,
            run_ref=None,
            now=FIXED_NOW,
        )
    assert (
        err.value.reason == "not_configured"
        and err.value.message
        == "Set up the Everyday tasks model in Settings, then refresh again."
    )


def test_not_configured_names_the_job_and_calls_nothing(
    db, app, services, fake_ai, saved_ai_setup
):
    db.execute(text("delete from job_assignment where job = 'judge'"))
    db.commit()
    with pytest.raises(StructuredReplyError) as err:
        complete_json(
            db,
            app.state.vault,
            Job.JUDGE,
            PROMPT,
            {},
            Answer,
            services=services,
            run_ref=None,
            now=FIXED_NOW,
        )
    assert err.value.reason == "not_configured"
    assert (
        err.value.message
        == "Set up the Review judge model in Settings, then refresh again."
    )
    assert fake_ai.calls == [] and logged_calls(db) == []


def test_a_failed_call_is_call_failed_with_the_error_cut_to_200_characters(
    db, app, services, fake_ai, saved_ai_setup
):
    fake_ai.fail("sample-local", RuntimeError("boom " + "x" * 500))
    with pytest.raises(StructuredReplyError) as err:
        ask(db, app, services)
    expected = ("RuntimeError: boom " + "x" * 500)[:200]
    assert err.value.reason == "call_failed"
    assert err.value.message == f"The model didn't answer: {expected}"
    assert isinstance(err.value, AIError)
    [row] = logged_calls(db)
    assert (
        row.error.startswith("RuntimeError: boom")
        and row.prompt_version == PROMPT.version
    )
    assert len(fake_ai.calls) == 1


def test_a_failed_call_on_the_retry_is_call_failed_and_both_are_logged(
    db, app, services, fake_ai, saved_ai_setup
):
    calls = []

    def reply(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "not json"
        raise ConnectionError("down")

    fake_ai.replies["sample-local"] = reply
    with pytest.raises(StructuredReplyError) as err:
        ask(db, app, services)
    first, second = logged_calls(db)
    assert err.value.reason == "call_failed"
    assert err.value.message == "The model didn't answer: ConnectionError: down"
    assert first.error is None and second.error == "ConnectionError: down"


def test_replies_can_be_computed_from_the_prompt(
    db, app, services, fake_ai, saved_ai_setup
):
    fake_ai.replies["sample-local"] = lambda prompt: '{"value": %d}' % len(prompt)
    answer, _ = ask(db, app, services)
    assert answer.value == len(PROMPT.source)


# --- the fake ------------------------------------------------------------------


def test_the_fake_uses_list_replies_in_order_and_repeats_the_last():
    fake = FakeAdapterFactory()
    fake.replies["m"] = ["one", "two"]
    fake.stop_reasons["m"] = "length"
    adapter = fake(ProviderConfig(ProviderKind.LOCAL, None, "http://localhost/v1"))
    got = [adapter.complete("m", "p") for _ in range(3)]
    assert [c.text for c in got] == ["one", "two", "two"]
    assert {c.stop_reason for c in got} == {"length"}
    assert adapter.complete("other", "p").stop_reason is None


# --- job_config ----------------------------------------------------------------


def test_job_config_gives_the_saved_provider_and_model(db, app, saved_ai_setup):
    cfg, model = job_config(db, app.state.vault, Job.EVERYDAY)
    assert (cfg.kind, cfg.api_key, cfg.base_url, model) == (
        ProviderKind.LOCAL,
        None,
        "http://localhost:1234/v1",
        "sample-local",
    )
    cfg, model = job_config(db, app.state.vault, Job.RECOMMENDER)
    assert (cfg.kind, cfg.api_key, model) == (
        ProviderKind.ANTHROPIC,
        "ant-sample",
        "sample-big",
    )


def test_job_config_is_none_before_anything_is_saved(db, app):
    for job in Job:
        assert job_config(db, app.state.vault, job) is None


def test_job_config_is_none_for_a_job_nobody_assigned(db, app, saved_ai_setup):
    db.execute(text("delete from job_assignment where job = 'everyday'"))
    db.commit()
    assert job_config(db, app.state.vault, Job.EVERYDAY) is None
    assert job_config(db, app.state.vault, Job.JUDGE) is not None


def test_job_config_is_none_when_the_provider_is_not_saved(db, app, monkeypatch):
    # The database won't hold a job whose provider is missing, so hand job_config that state.
    gap = AiSetup(
        providers={},
        jobs={Job.EVERYDAY: (ProviderKind.LOCAL, "sample-local")},
        tavily_key="",
    )
    monkeypatch.setattr(ai_setup, "stored_ai_setup", lambda db, vault: gap)
    assert job_config(db, app.state.vault, Job.EVERYDAY) is None


def test_job_config_is_none_for_local_without_a_url(db, app, saved_ai_setup):
    db.get(Provider, "local").base_url = None
    db.commit()
    assert job_config(db, app.state.vault, Job.EVERYDAY) is None


def test_job_config_for_local_needs_no_key(db, app, saved_ai_setup):
    assert db.get(Provider, "local").api_key_enc is None
    assert job_config(db, app.state.vault, Job.EVERYDAY) is not None


def test_job_config_is_none_when_a_hosted_key_is_missing(db, app, saved_ai_setup):
    db.get(Provider, "anthropic").api_key_enc = None
    db.commit()
    assert job_config(db, app.state.vault, Job.RECOMMENDER) is None


def test_job_config_is_none_when_a_hosted_key_cannot_be_read(db, app, saved_ai_setup):
    db.get(Provider, "anthropic").api_key_enc = "not-a-token"
    db.commit()
    assert job_config(db, app.state.vault, Job.RECOMMENDER) is None
    # The other providers are not affected.
    assert job_config(db, app.state.vault, Job.EVERYDAY) is not None


# --- call_and_log --------------------------------------------------------------


def test_call_and_log_records_the_prompt_version_and_the_row_id(db, fake_ai):
    cfg = ProviderConfig(ProviderKind.OPENAI, "sk-sample", None)
    completion = call_and_log(
        db,
        cfg,
        "sample-model",
        "Hello",
        job=Job.EVERYDAY,
        prompt_name="sample_prompt",
        prompt_version="abc123",
        adapter_factory=fake_ai,
        now=FIXED_NOW,
    )
    [row] = logged_calls(db)
    assert completion.call_id == row.id and row.prompt_version == "abc123"


def test_call_and_log_without_a_version_logs_a_blank_one(db, fake_ai):
    cfg = ProviderConfig(ProviderKind.OPENAI, "sk-sample", None)
    call_and_log(
        db,
        cfg,
        "sample-model",
        "Hello",
        job=None,
        prompt_name="connection_test",
        adapter_factory=fake_ai,
        now=FIXED_NOW,
    )
    assert logged_calls(db)[0].prompt_version == ""


def test_call_and_log_passes_the_stop_reason_on(db, fake_ai):
    fake_ai.stop_reasons["sample-model"] = "length"
    cfg = ProviderConfig(ProviderKind.OPENAI, "sk-sample", None)
    completion = call_and_log(
        db,
        cfg,
        "sample-model",
        "Hello",
        job=None,
        prompt_name="p",
        adapter_factory=fake_ai,
        now=FIXED_NOW,
    )
    assert completion.stop_reason == "length"


# --- stop reasons in the adapters ---------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("length", "length"), ("stop", "stop"), ("content_filter", "stop"), (None, None)],
)
def test_openai_stop_reason(monkeypatch, raw, expected):
    def create(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="x"), finish_reason=raw)
            ],
            usage=None,
        )

    monkeypatch.setattr(OpenAICompletions, "create", create)
    assert OpenAICompatibleAdapter("k", None).complete("m", "p").stop_reason == expected


@pytest.mark.parametrize(
    "raw,expected", [("max_tokens", "length"), ("end_turn", "stop"), (None, None)]
)
def test_anthropic_stop_reason(monkeypatch, raw, expected):
    def create(self, **kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="x")],
            stop_reason=raw,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(AnthropicMessages, "create", create)
    assert AnthropicAdapter("k").complete("m", "p").stop_reason == expected


@pytest.mark.parametrize(
    "raw,expected", [("MAX_TOKENS", "length"), ("STOP", "stop"), (None, None)]
)
def test_gemini_stop_reason(monkeypatch, raw, expected):
    reason = None if raw is None else getattr(genai_types.FinishReason, raw)

    def generate(self, **kwargs):
        return SimpleNamespace(
            text="x",
            usage_metadata=None,
            candidates=[SimpleNamespace(finish_reason=reason)],
        )

    monkeypatch.setattr(GeminiModels, "generate_content", generate)
    assert GeminiAdapter("k").complete("m", "p").stop_reason == expected


def test_a_reply_with_no_choices_or_candidates_has_no_stop_reason(monkeypatch):
    monkeypatch.setattr(
        OpenAICompletions,
        "create",
        lambda self, **kwargs: SimpleNamespace(choices=[], usage=None),
    )
    monkeypatch.setattr(
        GeminiModels,
        "generate_content",
        lambda self, **kwargs: SimpleNamespace(
            text=None, usage_metadata=None, candidates=None
        ),
    )
    assert OpenAICompatibleAdapter("k", None).complete("m", "p").stop_reason is None
    assert GeminiAdapter("k").complete("m", "p").stop_reason is None


# --- the migration -------------------------------------------------------------


def test_migration_0003_gives_existing_calls_a_blank_prompt_version(tmp_path):
    db_path = tmp_path / "old.db"
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.upgrade(config, "0002")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into ai_call (provider_kind, model, prompt_name, prompt_hash, request,"
                " duration_ms, created_at) values ('local', 'm', 'p', 'h', 'r', 1,"
                " '2026-01-15 12:00:00')"
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert (
            conn.execute(text("select prompt_version from ai_call")).scalar_one() == ""
        )
    engine.dispose()
