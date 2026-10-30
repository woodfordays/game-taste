import hashlib
import socket
from types import SimpleNamespace

import pytest
from markupsafe import Markup
from sqlalchemy import select

from app.main import Services, default_services
from app.models import AiCall
from app.services.ai import (
    JOB_LABELS,
    MAX_OUTPUT_TOKENS,
    PROVIDER_LABELS,
    AIError,
    AnthropicAdapter,
    CheckResult,
    Completion,
    GeminiAdapter,
    Job,
    OpenAICompatibleAdapter,
    ProviderConfig,
    ProviderKind,
    call_and_log,
    check_model,
    make_adapter,
)
from app.services.search import make_search_client
from conftest import FIXED_NOW


def test_enum_values_and_labels():
    assert [k.value for k in ProviderKind] == [
        "local",
        "openrouter",
        "openai",
        "anthropic",
        "gemini",
    ]
    assert PROVIDER_LABELS == {
        "local": "Local",
        "openrouter": "OpenRouter",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "gemini": "Gemini",
    }
    assert [j.value for j in Job] == ["recommender", "judge", "everyday"]
    assert JOB_LABELS == {
        "recommender": "Recommender",
        "judge": "Review judge",
        "everyday": "Everyday tasks",
    }


def test_make_adapter_routes_providers():
    assert (
        make_adapter(
            ProviderConfig(ProviderKind.OPENROUTER, "or-sample", None)
        ).base_url
        == "https://openrouter.ai/api/v1"
    )
    local = make_adapter(
        ProviderConfig(ProviderKind.LOCAL, None, "http://localhost:11434/v1/")
    )
    assert (
        local.api_key == "not-needed" and local.base_url == "http://localhost:11434/v1"
    )  # Review focus 2
    assert (
        make_adapter(ProviderConfig(ProviderKind.OPENAI, "sk-sample", None)).base_url
        is None
    )
    assert isinstance(
        make_adapter(ProviderConfig(ProviderKind.ANTHROPIC, "ant-sample", None)),
        AnthropicAdapter,
    )
    assert isinstance(
        make_adapter(ProviderConfig(ProviderKind.GEMINI, "gem-sample", None)),
        GeminiAdapter,
    )


def test_local_keeps_a_key_when_one_is_given():
    local = make_adapter(
        ProviderConfig(ProviderKind.LOCAL, "local-sample", "http://localhost:1234/v1")
    )
    assert local.api_key == "local-sample"


@pytest.mark.parametrize(
    "kind",
    [
        ProviderKind.OPENROUTER,
        ProviderKind.OPENAI,
        ProviderKind.ANTHROPIC,
        ProviderKind.GEMINI,
    ],
)
def test_hosted_providers_need_a_key(kind):
    for blank in (None, ""):
        with pytest.raises(ValueError, match="needs an API key"):
            make_adapter(ProviderConfig(kind, blank, None))


def test_local_needs_a_url():
    with pytest.raises(ValueError, match="needs a server URL"):
        make_adapter(ProviderConfig(ProviderKind.LOCAL, None, None))


def test_adapters_build_their_real_sdk_clients_without_network(monkeypatch):
    # Constructing an adapter, and the SDK client behind it, must never touch the network.
    def no_network(*args, **kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    for cfg in (
        ProviderConfig(ProviderKind.OPENROUTER, "or-sample", None),
        ProviderConfig(ProviderKind.LOCAL, None, "http://localhost:11434/v1"),
        ProviderConfig(ProviderKind.OPENAI, "sk-sample", None),
        ProviderConfig(ProviderKind.ANTHROPIC, "ant-sample", None),
        ProviderConfig(ProviderKind.GEMINI, "gem-sample", None),
    ):
        assert make_adapter(cfg)._get_client() is not None


class _RecordingSDK:
    """Records the keyword arguments of the one SDK method it stands in for."""

    def __init__(self, reply):
        self.reply = reply
        self.kwargs: dict | None = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.reply

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return self.reply


def test_openai_adapter_call_shape():
    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
    )
    sdk = _RecordingSDK(reply)
    adapter = OpenAICompatibleAdapter("sk-sample", None)
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=sdk))

    assert adapter.complete("sample-model", "Say hi") == Completion("hello", 11, 3)
    assert sdk.kwargs == {
        "model": "sample-model",
        "messages": [{"role": "user", "content": "Say hi"}],
    }


def test_openai_adapter_copes_with_a_server_that_omits_usage_and_content():
    for reply in (
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))], usage=None
        ),
        SimpleNamespace(choices=[], usage=None),
    ):
        adapter = OpenAICompatibleAdapter("sk-sample", None)
        adapter._client = SimpleNamespace(
            chat=SimpleNamespace(completions=_RecordingSDK(reply))
        )
        assert adapter.complete("sample-model", "Say hi") == Completion("", None, None)


def test_anthropic_adapter_call_shape_joins_text_blocks():
    reply = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="hmm"),
            SimpleNamespace(type="text", text="Hello, "),
            SimpleNamespace(type="text", text="world"),
        ],
        usage=SimpleNamespace(input_tokens=9, output_tokens=4),
    )
    sdk = _RecordingSDK(reply)
    adapter = AnthropicAdapter("ant-sample")
    adapter._client = SimpleNamespace(messages=sdk)

    assert adapter.complete("sample-model", "Say hi") == Completion(
        "Hello, world", 9, 4
    )
    assert sdk.kwargs == {
        "model": "sample-model",
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [{"role": "user", "content": "Say hi"}],
    }
    assert MAX_OUTPUT_TOKENS == 8192


def test_gemini_adapter_call_shape():
    reply = SimpleNamespace(
        text="hello",
        usage_metadata=SimpleNamespace(
            prompt_token_count=8, candidates_token_count=3, thoughts_token_count=4
        ),
    )
    sdk = _RecordingSDK(reply)
    adapter = GeminiAdapter("gem-sample")
    adapter._client = SimpleNamespace(models=sdk)

    # Thinking tokens are billed as output, so they are counted with it.
    assert adapter.complete("sample-model", "Say hi") == Completion("hello", 8, 7)
    assert sdk.kwargs == {"model": "sample-model", "contents": "Say hi"}


def test_gemini_adapter_copes_with_no_text_or_usage():
    for usage in (
        None,
        SimpleNamespace(
            prompt_token_count=None,
            candidates_token_count=None,
            thoughts_token_count=None,
        ),
    ):
        adapter = GeminiAdapter("gem-sample")
        adapter._client = SimpleNamespace(
            models=_RecordingSDK(SimpleNamespace(text=None, usage_metadata=usage))
        )
        assert adapter.complete("sample-model", "Say hi") == Completion("", None, None)


def test_call_and_log_success_and_failure(db, fake_ai):
    cfg = ProviderConfig(ProviderKind.ANTHROPIC, "ant-sample", None)
    call_and_log(
        db,
        cfg,
        "sample-model",
        "Say ok",
        job=Job.JUDGE,
        prompt_name="connection_test",
        adapter_factory=fake_ai,
    )
    row = db.scalars(select(AiCall)).one()
    assert (
        row.prompt_hash == hashlib.sha256(b"Say ok").hexdigest()
        and row.response == "ok"
        and row.job == "judge"
        and row.error is None
    )
    fake_ai.fail("broken-model", RuntimeError("model not found"))
    with pytest.raises(AIError):
        call_and_log(
            db,
            cfg,
            "broken-model",
            "Say ok",
            job=None,
            prompt_name="connection_test",
            adapter_factory=fake_ai,
        )
    assert (
        "model not found"
        in db.scalars(select(AiCall).order_by(AiCall.id.desc())).first().error
    )


def test_call_and_log_records_the_whole_call(db, fake_ai):
    cfg = ProviderConfig(ProviderKind.OPENAI, "sk-sample", None)
    completion = call_and_log(
        db,
        cfg,
        "sample-model",
        "Tell me about <Sample Quest>",
        job=Job.RECOMMENDER,
        prompt_name="sample_prompt",
        run_ref="run-1",
        adapter_factory=fake_ai,
        now=FIXED_NOW,
    )
    assert completion == Completion("ok", 5, 2)
    assert fake_ai.calls == [
        (ProviderKind.OPENAI, "sample-model", "Tell me about <Sample Quest>")
    ]
    db.expire_all()
    row = db.scalars(select(AiCall)).one()
    assert row.provider_kind == "openai"
    assert row.model == "sample-model"
    assert row.prompt_name == "sample_prompt"
    assert row.request == "Tell me about <Sample Quest>"
    assert (row.input_tokens, row.output_tokens) == (5, 2)
    assert isinstance(row.duration_ms, int) and row.duration_ms >= 0
    assert row.run_ref == "run-1"
    assert row.created_at == FIXED_NOW


def test_call_and_log_without_a_job_or_clock_still_logs(db, fake_ai):
    call_and_log(
        db,
        ProviderConfig(ProviderKind.OPENAI, "sk-sample", None),
        "sample-model",
        "Say ok",
        job=None,
        prompt_name="connection_test",
        adapter_factory=fake_ai,
    )
    row = db.scalars(select(AiCall)).one()
    assert row.job is None and row.run_ref is None
    assert row.created_at.tzinfo is not None


def test_failed_call_logs_the_error_and_raises_with_it(db, fake_ai):
    fake_ai.fail("broken-model", RuntimeError("model not found"))
    cfg = ProviderConfig(ProviderKind.OPENAI, "sk-sample", None)
    with pytest.raises(AIError) as raised:
        call_and_log(
            db,
            cfg,
            "broken-model",
            "Say ok",
            job=Job.EVERYDAY,
            prompt_name="connection_test",
            adapter_factory=fake_ai,
            now=FIXED_NOW,
        )
    row = db.scalars(select(AiCall)).one()
    assert row.error == "RuntimeError: model not found"
    assert str(raised.value) == row.error
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert row.response is None and row.input_tokens is None
    assert row.duration_ms >= 0 and row.created_at == FIXED_NOW
    assert row.request == "Say ok"


def test_a_bad_config_is_logged_too(db):
    # make_adapter itself refuses a Local config with no URL; the failure is still recorded.
    with pytest.raises(AIError, match="needs a server URL"):
        call_and_log(
            db,
            ProviderConfig(ProviderKind.LOCAL, None, None),
            "sample-model",
            "Say ok",
            job=None,
            prompt_name="connection_test",
        )
    assert "needs a server URL" in db.scalars(select(AiCall)).one().error


def test_check_model_messages(db, fake_ai):  # Review focus 2
    ok = check_model(
        db,
        ProviderConfig(ProviderKind.OPENAI, "sk", None),
        "sample-model",
        job=None,
        adapter_factory=fake_ai,
    )
    assert ok == CheckResult(True, "Connected")
    fake_ai.fail("local-model", ConnectionError("refused"))
    bad = check_model(
        db,
        ProviderConfig(ProviderKind.LOCAL, None, "http://localhost:1234"),
        "local-model",
        job=None,
        adapter_factory=fake_ai,
    )
    assert (
        not bad.ok
        and bad.message.startswith("Didn't connect: ConnectionError: refused")
        and "Most local servers end in /v1." in bad.message
    )


def test_check_model_sends_the_connection_test_prompt_and_logs_it(db, fake_ai):
    check_model(
        db,
        ProviderConfig(ProviderKind.GEMINI, "gem-sample", None),
        "sample-model",
        job=Job.JUDGE,
        adapter_factory=fake_ai,
        now=FIXED_NOW,
    )
    assert fake_ai.calls == [
        (ProviderKind.GEMINI, "sample-model", "Reply with the single word: ok")
    ]
    row = db.scalars(select(AiCall)).one()
    assert row.prompt_name == "connection_test" and row.job == "judge"
    assert row.created_at == FIXED_NOW


def test_check_model_failure_text_is_plain_and_capped(db, fake_ai):
    fake_ai.fail("sample-model", ValueError("<b>" + "x" * 300))
    result = check_model(
        db,
        ProviderConfig(ProviderKind.OPENAI, "sk-sample", None),
        "sample-model",
        job=None,
        adapter_factory=fake_ai,
    )
    assert type(result.message) is str and not isinstance(result.message, Markup)
    assert result.message == "Didn't connect: ValueError: <b>" + "x" * 197
    # Only Local gets the URL hint.
    assert "Check the URL" not in result.message
    # The log keeps the full text.
    assert db.scalars(select(AiCall)).one().error == "ValueError: <b>" + "x" * 300


def test_check_model_local_hint_reads_as_a_sentence(db, fake_ai):
    cfg = ProviderConfig(ProviderKind.LOCAL, None, "http://localhost:1234")
    fake_ai.fail("a", ConnectionError("refused"))
    fake_ai.fail("b", ConnectionError("Connection refused."))
    assert check_model(db, cfg, "a", job=None, adapter_factory=fake_ai).message == (
        "Didn't connect: ConnectionError: refused."
        " Check the URL. Most local servers end in /v1."
    )
    assert check_model(db, cfg, "b", job=None, adapter_factory=fake_ai).message == (
        "Didn't connect: ConnectionError: Connection refused."
        " Check the URL. Most local servers end in /v1."
    )


def test_check_model_treats_a_blank_reply_as_not_connected(db, fake_ai):
    fake_ai.replies["quiet-model"] = "  \n"
    result = check_model(
        db,
        ProviderConfig(ProviderKind.OPENAI, "sk-sample", None),
        "quiet-model",
        job=None,
        adapter_factory=fake_ai,
    )
    assert not result.ok
    assert result.message.startswith("Didn't connect: ")
    assert "Check the model name" in result.message


def test_check_model_reports_a_config_problem(db):
    result = check_model(
        db,
        ProviderConfig(ProviderKind.LOCAL, None, None),
        "sample-model",
        job=None,
    )
    assert not result.ok
    assert result.message.startswith("Didn't connect: ValueError: ")


def test_services_carry_the_ai_factory(app, fake_ai):
    assert app.state.services.adapter_factory is fake_ai
    assert isinstance(app.state.services, Services)


def test_default_services_use_the_real_factories():
    defaults = default_services()
    assert defaults.adapter_factory is make_adapter
    assert defaults.search_factory is make_search_client
