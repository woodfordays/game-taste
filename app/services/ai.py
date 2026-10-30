"""The AI providers behind one small interface, and the log every call goes through."""

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.models import AiCall

MAX_OUTPUT_TOKENS = 8192
# A connection check gives up after this many seconds, and does not retry.
CHECK_TIMEOUT_SECONDS = 30
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CONNECTION_TEST_PROMPT = "Reply with the single word: ok"
LOCAL_URL_HINT = "Check the URL. Most local servers end in /v1."


class ProviderKind(StrEnum):
    LOCAL = "local"
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


PROVIDER_LABELS = {
    ProviderKind.LOCAL: "Local",
    ProviderKind.OPENROUTER: "OpenRouter",
    ProviderKind.OPENAI: "OpenAI",
    ProviderKind.ANTHROPIC: "Anthropic",
    ProviderKind.GEMINI: "Gemini",
}


class Job(StrEnum):
    RECOMMENDER = "recommender"
    JUDGE = "judge"
    EVERYDAY = "everyday"


JOB_LABELS = {
    Job.RECOMMENDER: "Recommender",
    Job.JUDGE: "Review judge",
    Job.EVERYDAY: "Everyday tasks",
}


@dataclass(frozen=True)
class ProviderConfig:
    kind: ProviderKind
    api_key: str | None
    base_url: str | None


@dataclass
class Completion:
    text: str
    input_tokens: int | None
    output_tokens: int | None


class Adapter(Protocol):
    def complete(
        self, model: str, prompt: str, *, timeout: float | None = None
    ) -> Completion:
        """Ask the model. A timeout (seconds) also turns off the SDK's retries for the call.

        `None` keeps the SDK's own timeout and retries, which suit long real work.
        """
        ...


class AIError(Exception):
    """A provider call failed. The message is the text saved in the call log."""


@dataclass
class CheckResult:
    ok: bool
    message: str


# The adapters build their SDK client on first use, so making one never touches the network.


class OpenAICompatibleAdapter:
    """OpenAI, OpenRouter and local servers all speak the chat completions API."""

    def __init__(self, api_key: str, base_url: str | None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def complete(
        self, model: str, prompt: str, *, timeout: float | None = None
    ) -> Completion:
        client = self._get_client()
        if timeout is not None:
            client = client.with_options(timeout=timeout, max_retries=0)
        response = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}]
        )
        message = response.choices[0].message if response.choices else None
        usage = response.usage
        return Completion(
            text=(message.content if message else None) or "",
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )


class AnthropicAdapter:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=self.api_key)
        return self._client

    def complete(
        self, model: str, prompt: str, *, timeout: float | None = None
    ) -> Completion:
        client = self._get_client()
        if timeout is not None:
            client = client.with_options(timeout=timeout, max_retries=0)
        response = client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        # The reply is a list of blocks. Only the text blocks are the answer.
        text = "".join(block.text for block in response.content if block.type == "text")
        return Completion(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


class GeminiAdapter:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def complete(
        self, model: str, prompt: str, *, timeout: float | None = None
    ) -> Completion:
        extra: dict[str, Any] = {}
        if timeout is not None:
            from google.genai import types

            # Gemini's timeout is in milliseconds. One attempt means no retries.
            extra["config"] = types.GenerateContentConfig(
                http_options=types.HttpOptions(
                    timeout=round(timeout * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                )
            )
        response = self._get_client().models.generate_content(
            model=model, contents=prompt, **extra
        )
        usage = response.usage_metadata
        output_tokens = None
        if usage is not None:
            # Gemini reports thinking tokens apart from the answer, but bills them as output.
            parts = [usage.candidates_token_count, usage.thoughts_token_count]
            if any(part is not None for part in parts):
                output_tokens = sum(part or 0 for part in parts)
        return Completion(
            text=response.text or "",
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=output_tokens,
        )


def make_adapter(cfg: ProviderConfig) -> Adapter:
    if cfg.kind is ProviderKind.LOCAL:
        if not cfg.base_url:
            raise ValueError("Local needs a server URL.")
        return OpenAICompatibleAdapter(
            cfg.api_key or "not-needed", cfg.base_url.rstrip("/")
        )
    if not cfg.api_key:
        raise ValueError(f"{PROVIDER_LABELS[cfg.kind]} needs an API key.")
    if cfg.kind is ProviderKind.OPENROUTER:
        return OpenAICompatibleAdapter(cfg.api_key, OPENROUTER_BASE_URL)
    if cfg.kind is ProviderKind.OPENAI:
        return OpenAICompatibleAdapter(cfg.api_key, None)
    if cfg.kind is ProviderKind.ANTHROPIC:
        return AnthropicAdapter(cfg.api_key)
    return GeminiAdapter(cfg.api_key)


def describe_error(exc: BaseException) -> str:
    """The text saved in a call log for a failure: the exception's type, then its message."""
    return f"{type(exc).__name__}: {exc}"


def call_and_log(
    db: Session,
    cfg: ProviderConfig,
    model: str,
    prompt: str,
    *,
    job: Job | None,
    prompt_name: str,
    run_ref: str | None = None,
    adapter_factory=make_adapter,
    now: datetime | None = None,
    timeout: float | None = None,
) -> Completion:
    """Ask the model and record the call, whether it worked or not.

    A `timeout` in seconds is passed to the adapter (see `Adapter.complete`).
    On failure the error is logged and an `AIError` carrying the same text is raised.
    """
    row = AiCall(
        job=job.value if job else None,
        provider_kind=cfg.kind.value,
        model=model,
        prompt_name=prompt_name,
        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        request=prompt,
        run_ref=run_ref,
        created_at=now or datetime.now(timezone.utc),
    )
    started = time.perf_counter()
    failure: Exception | None = None
    completion: Completion | None = None
    try:
        completion = adapter_factory(cfg).complete(model, prompt, timeout=timeout)
    except Exception as exc:
        failure = exc
    row.duration_ms = round((time.perf_counter() - started) * 1000)
    if completion is not None:
        row.response = completion.text
        row.input_tokens = completion.input_tokens
        row.output_tokens = completion.output_tokens
    error_text = describe_error(failure) if failure is not None else None
    row.error = error_text
    db.add(row)
    db.commit()
    if failure is not None:
        raise AIError(error_text) from failure
    return completion


def check_model(
    db: Session,
    cfg: ProviderConfig,
    model: str,
    *,
    job: Job | None,
    adapter_factory=make_adapter,
    now: datetime | None = None,
) -> CheckResult:
    """Send a tiny prompt to see whether the provider, key and model name work."""
    try:
        completion = call_and_log(
            db,
            cfg,
            model,
            CONNECTION_TEST_PROMPT,
            job=job,
            prompt_name="connection_test",
            adapter_factory=adapter_factory,
            now=now,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except AIError as err:
        cause = err.__cause__ or err
        message = f"Didn't connect: {type(cause).__name__}: {str(cause)[:200]}"
    else:
        if completion.text.strip():
            return CheckResult(True, "Connected")
        message = "Didn't connect: the model sent an empty reply. Check the model name."
    if cfg.kind is ProviderKind.LOCAL:
        if not message.endswith((".", "!", "?")):
            message += "."
        message += f" {LOCAL_URL_HINT}"
    return CheckResult(False, message)
