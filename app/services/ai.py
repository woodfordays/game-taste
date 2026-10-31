"""The AI providers behind one small interface, and the log every call goes through."""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.models import AiCall
from app.prompts import Prompt

if TYPE_CHECKING:
    from app.main import Services
    from app.services.secrets import Vault

MAX_OUTPUT_TOKENS = 8192
# A connection check gives up after this many seconds, and does not retry.
CHECK_TIMEOUT_SECONDS = 30
# A job's model call (a refresh, for one) gives up after this many seconds, so a call that
# never returns can't hold the worker for good. It matches the OpenAI and Anthropic SDK
# defaults. Gemini's SDK has no timeout of its own. The SDK's retries stay on.
JOB_CALL_TIMEOUT_SECONDS = 600
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
    # Left out of repr, so no log line or traceback prints the key.
    api_key: str | None = field(repr=False)
    base_url: str | None


@dataclass
class Completion:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    # Why the model stopped: "length" (it hit the output limit, so the text is cut off),
    # "stop" (any other reason), or None when the provider didn't say.
    stop_reason: str | None = None
    # The id of the `ai_call` row `call_and_log` wrote for this reply. Not part of equality:
    # two replies with the same content are equal whichever rows they were logged in.
    call_id: int | None = field(default=None, compare=False)


class Adapter(Protocol):
    def complete(
        self,
        model: str,
        prompt: str,
        *,
        timeout: float | None = None,
        retries: bool = True,
    ) -> Completion:
        """Ask the model.

        A `timeout` (seconds) is how long the call may take. `None` keeps the SDK's own
        timeout. `retries=False` turns the SDK's retries off for the call, so a failure
        comes back at once. `retries=True` keeps the SDK's own retry behavior.
        """
        ...


class AIError(Exception):
    """A provider call failed. The message is the text saved in the call log."""


def _stop_reason(raw: object, length_value: str) -> str | None:
    """A provider's stop reason as "length", "stop" or None (it didn't give one).

    `length_value` is the provider's word for "hit the output limit". Every other
    reason, such as a normal finish or a content filter, is "stop". Enums are read by value.
    """
    if raw is None:
        return None
    text = str(getattr(raw, "value", raw))
    if not text:
        return None
    return "length" if text.casefold() == length_value.casefold() else "stop"


@dataclass
class CheckResult:
    ok: bool
    message: str


# The adapters build their SDK client on first use, so making one never touches the network.


def _with_call_options(client: Any, timeout: float | None, retries: bool) -> Any:
    """An OpenAI or Anthropic client set up for one call: the same one if nothing changes.

    `with_options` makes a copy, so the shared client is never altered.
    """
    options: dict[str, Any] = {}
    if timeout is not None:
        options["timeout"] = timeout
    if not retries:
        options["max_retries"] = 0
    return client.with_options(**options) if options else client


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
        self,
        model: str,
        prompt: str,
        *,
        timeout: float | None = None,
        retries: bool = True,
    ) -> Completion:
        client = _with_call_options(self._get_client(), timeout, retries)
        response = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}]
        )
        choice = response.choices[0] if response.choices else None
        message = choice.message if choice else None
        usage = response.usage
        return Completion(
            text=(message.content if message else None) or "",
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            # Some servers leave finish_reason out.
            stop_reason=_stop_reason(getattr(choice, "finish_reason", None), "length"),
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
        self,
        model: str,
        prompt: str,
        *,
        timeout: float | None = None,
        retries: bool = True,
    ) -> Completion:
        client = _with_call_options(self._get_client(), timeout, retries)
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
            stop_reason=_stop_reason(
                getattr(response, "stop_reason", None), "max_tokens"
            ),
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
        self,
        model: str,
        prompt: str,
        *,
        timeout: float | None = None,
        retries: bool = True,
    ) -> Completion:
        extra: dict[str, Any] = {}
        if timeout is not None or not retries:
            from google.genai import types

            # Gemini's timeout is in milliseconds. One attempt means no retries. Left unset,
            # retry_options leaves the SDK's own retry behavior alone.
            extra["config"] = types.GenerateContentConfig(
                http_options=types.HttpOptions(
                    timeout=None if timeout is None else round(timeout * 1000),
                    retry_options=None
                    if retries
                    else types.HttpRetryOptions(attempts=1),
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
        # A reply with no candidates (blocked before any text) has no finish reason.
        candidates = getattr(response, "candidates", None) or []
        return Completion(
            text=response.text or "",
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=output_tokens,
            stop_reason=_stop_reason(
                getattr(candidates[0], "finish_reason", None) if candidates else None,
                "MAX_TOKENS",
            ),
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
    prompt_version: str = "",
    run_ref: str | None = None,
    adapter_factory=make_adapter,
    now: datetime | None = None,
    timeout: float | None = None,
    retries: bool = True,
) -> Completion:
    """Ask the model and record the call, whether it worked or not.

    `prompt_name` and `prompt_version` say which prompt file made the request (the version
    is blank for a prompt that isn't a file). `timeout` (seconds) and `retries` are passed
    to the adapter (see `Adapter.complete`). The returned completion's `call_id` is the
    logged row's id. On failure the error is logged and an `AIError` carrying the same text
    is raised.
    """
    row = AiCall(
        job=job.value if job else None,
        provider_kind=cfg.kind.value,
        model=model,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        request=prompt,
        run_ref=run_ref,
        created_at=now or datetime.now(timezone.utc),
    )
    started = time.perf_counter()
    failure: Exception | None = None
    completion: Completion | None = None
    try:
        completion = adapter_factory(cfg).complete(
            model, prompt, timeout=timeout, retries=retries
        )
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
    completion.call_id = row.id
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
            retries=False,
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


# --- structured replies -------------------------------------------------------


def job_config(
    db: Session, vault: "Vault", job: Job
) -> tuple[ProviderConfig, str] | None:
    """The saved provider and model for `job`, or None if the job can't be run yet.

    That is: the job isn't assigned, its provider isn't saved, a Local provider has no
    server URL, or any other provider's key is missing or can't be decrypted.
    """
    # Imported here because ai_setup imports this module.
    from app.services.ai_setup import stored_ai_setup

    setup = stored_ai_setup(db, vault)
    assigned = setup.jobs.get(job)
    if assigned is None:
        return None
    kind, model = assigned
    cfg = setup.providers.get(kind) if kind is not None else None
    if cfg is None:
        return None
    if kind is ProviderKind.LOCAL:
        # A local server can do without a key, but not without an address.
        if not cfg.base_url:
            return None
    elif not cfg.api_key or kind.value in setup.unreadable:
        return None
    return cfg, model


class StructuredReplyError(AIError):
    """A job that needs a JSON reply didn't get one.

    `reason` is "not_configured", "truncated", "unusable" or "call_failed". `message`, also
    the text of the exception, says what happened and what to do, in words fit to show.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


_CALL_FAILED_CHARS = 200
_RETRY_NOTE_CHARS = 300
_FENCED = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_REASONING_END = re.compile(r"</think(?:ing)?>", re.IGNORECASE)

_Model = TypeVar("_Model", bound=BaseModel)


def _parse_reply(reply: str, schema: type[_Model]) -> _Model:
    """Read a model's reply as a `schema`. Raises ValueError (or a pydantic one) if it isn't.

    Tolerates what real models put around the JSON:
    - Reasoning text. If the reply has a closing </think> or </thinking> tag, only the text
      after the last one is read. That drops a closed block, and also the reasoning a server
      leaves behind when it strips the opening tag.
    - A code fence, and chatter before or after the JSON, including chatter with braces.
      The first fence that holds a "{" is read first, then the whole reply. In each, an
      object is tried at every "{" in turn, and the first that fits `schema` is the answer.

    The error raised is the first object that decoded but didn't fit `schema`, if there was
    one, since that is the most useful thing to tell the model. Otherwise it is the first
    decode error, or "no JSON object" for a reply with no "{". The text is only ever
    parsed as JSON, never run.
    """
    ends = list(_REASONING_END.finditer(reply))
    if ends:
        reply = reply[ends[-1].end() :]
    areas = [block for block in _FENCED.findall(reply) if "{" in block][:1]
    areas.append(reply)
    decoder = json.JSONDecoder()
    misfit: ValidationError | None = None  # decoded, but not a `schema`
    unreadable: Exception | None = None  # not JSON
    for area in areas:
        start = area.find("{")
        while start != -1:
            try:
                value, _end = decoder.raw_decode(area, start)
                return schema.model_validate(value)
            except ValidationError as exc:
                misfit = misfit or exc
            # json.JSONDecodeError is a ValueError. A very deep object is a RecursionError.
            except (ValueError, RecursionError) as exc:
                unreadable = unreadable or exc
            start = area.find("{", start + 1)
    raise misfit or unreadable or ValueError("the reply has no JSON object in it")


def complete_json(
    db: Session,
    vault: "Vault",
    job: Job,
    prompt: Prompt,
    context: dict,
    schema: type[_Model],
    *,
    services: "Services",
    run_ref: str | None,
    now: datetime,
) -> tuple[_Model, int]:
    """Ask the `job` model for a JSON reply to `prompt` and read it as a `schema`.

    Returns the parsed reply and the id of the logged `ai_call` that produced it. A reply
    that can't be used gets one more try, with the problem added to the prompt. Every call is
    logged, with the prompt's name and version. Raises `StructuredReplyError`:
    "not_configured" (the job has no usable setup), "call_failed" (the provider failed, on
    either call), "truncated" (the model hit its output limit, which a retry can't fix) or
    "unusable" (two replies in a row couldn't be read).
    """
    config = job_config(db, vault, job)
    if config is None:
        raise StructuredReplyError(
            "not_configured",
            f"Set up the {JOB_LABELS[job]} model in Settings, then refresh again.",
        )
    cfg, model = config
    base = prompt.render(**context)

    problem: str | None = None
    for _attempt in range(2):
        asked = base
        if problem is not None:
            asked += (
                f"\n\nYour previous reply could not be used: {problem}. "
                "Reply again with only the JSON object."
            )
        try:
            completion = call_and_log(
                db,
                cfg,
                model,
                asked,
                job=job,
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                run_ref=run_ref,
                adapter_factory=services.adapter_factory,
                now=now,
                timeout=JOB_CALL_TIMEOUT_SECONDS,
            )
        except AIError as err:
            # The provider's own text may end in a period, and the cut may land on one.
            detail = str(err)[:_CALL_FAILED_CHARS].rstrip(". \t\r\n")
            raise StructuredReplyError(
                "call_failed",
                f"The model didn't answer: {detail}. "
                f"Check the {JOB_LABELS[job]} provider in Settings, then try again.",
            ) from err
        if completion.stop_reason == "length":
            raise StructuredReplyError(
                "truncated",
                "The model's reply was cut off. "
                "Pick a model with a larger output limit in Settings.",
            )
        try:
            return _parse_reply(completion.text, schema), completion.call_id
        except (ValueError, RecursionError) as exc:
            problem = str(exc)[:_RETRY_NOTE_CHARS]
    raise StructuredReplyError(
        "unusable",
        "The model's reply wasn't in the expected format. "
        f"Try again, or pick a different {JOB_LABELS[job]} model in Settings.",
    )
