"""Parse, test and save the "Connect your AI" form.

Onboarding step 2 and Settings share this: the same form, the same checks, the same save.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JobAssignment, Provider
from app.services.ai import (
    JOB_LABELS,
    PROVIDER_LABELS,
    CheckResult,
    Job,
    ProviderConfig,
    ProviderKind,
    check_model,
)
from app.services.search import check_search
from app.services.secrets import Vault
from app.services.settings import get_secret, set_secret

if TYPE_CHECKING:
    from app.main import Services

TAVILY_SETTING = "tavily_api_key"
# How `AiSetup.unreadable` names the Tavily key. Providers use their kind's value.
TAVILY = "tavily"
TAVILY_LABEL = "Tavily web search"

# Fixed copy. None of it holds user text, so none needs escaping beyond what Jinja does.
KEY_UNREADABLE = "Saved key can't be read. Enter it again."
LOCAL_URL_MISSING = "Enter the server URL for Local."
TAVILY_MISSING = "Add a Tavily key. All game research uses it."

JOB_HINTS = {
    Job.RECOMMENDER: "Proposes games.",
    Job.JUDGE: "Checks the proposals.",
    Job.EVERYDAY: "Updates profiles from reviews, refreshes, and re-ranks the queue.",
}

# A key shorter than this is not worth a "ending in" hint: the hint would give most of it away.
_MIN_HINT_KEY_LENGTH = 8


@dataclass
class AiSetup:
    """Everything the form sets: providers, a (provider, model) per job, and the Tavily key.

    `jobs` holds `None` for a provider the user hasn't chosen yet. That only happens while
    a form is being fixed; a setup that passed `parse_ai_form` with no errors has none.
    `unreadable` names the saved keys that couldn't be decrypted: provider kinds (by value)
    and "tavily". Those keys count as not set.
    """

    providers: dict[ProviderKind, ProviderConfig]
    jobs: dict[Job, tuple[ProviderKind | None, str]]
    tavily_key: str = field(repr=False)
    unreadable: frozenset[str] = frozenset()


@dataclass
class CheckLine:
    label: str
    result: CheckResult


def stored_ai_setup(db: Session, vault: Vault) -> AiSetup:
    """The saved setup, or a blank one when nothing is saved.

    A key that can't be decrypted (the key file was replaced) comes back as not set and is
    named in `unreadable`, so the form can ask for it again instead of failing.
    """
    unreadable: set[str] = set()
    providers: dict[ProviderKind, ProviderConfig] = {}
    for row in db.scalars(select(Provider)):
        try:
            kind = ProviderKind(row.kind)
        except ValueError:
            continue
        api_key = None
        if row.api_key_enc:
            try:
                api_key = vault.decrypt(row.api_key_enc)
            except InvalidToken:
                unreadable.add(kind.value)
        providers[kind] = ProviderConfig(kind, api_key, row.base_url)

    jobs: dict[Job, tuple[ProviderKind | None, str]] = {}
    for row in db.scalars(select(JobAssignment)):
        try:
            jobs[Job(row.job)] = (ProviderKind(row.provider_kind), row.model)
        except ValueError:
            continue

    try:
        tavily_key = get_secret(db, vault, TAVILY_SETTING) or ""
    except InvalidToken:
        tavily_key = ""
        unreadable.add(TAVILY)

    return AiSetup(
        providers={kind: providers[kind] for kind in ProviderKind if kind in providers},
        jobs=jobs,
        tavily_key=tavily_key,
        unreadable=frozenset(unreadable),
    )


def _text(form: Mapping[str, Any], name: str) -> str:
    """One field's text with the whitespace a paste leaves around it removed."""
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _texts(form: Mapping[str, Any], name: str) -> list[str]:
    """All values of a repeated field. Takes a posted form or a plain dict of lists."""
    if hasattr(form, "getlist"):
        values = form.getlist(name)
    else:
        value = form.get(name, [])
        values = [value] if isinstance(value, str) else list(value)
    return [value.strip() for value in values if isinstance(value, str)]


def _kind_or_none(value: str) -> ProviderKind | None:
    try:
        return ProviderKind(value)
    except ValueError:
        return None


def parse_ai_form(
    form: Mapping[str, Any], stored: AiSetup
) -> tuple[AiSetup, list[str]]:
    """Read the posted form into a setup, and list what is missing (empty when it is complete).

    A blank API key or Tavily key keeps the stored one, so a saved key never has to be typed
    again. Errors come in page order: the Local URL, then each job, then Tavily. The returned
    setup holds what was typed, so a form with errors can be shown again as it was.
    """
    kinds: list[ProviderKind] = []
    for value in _texts(form, "providers"):
        kind = _kind_or_none(value)
        if kind is not None and kind not in kinds:
            kinds.append(kind)

    unreadable: set[str] = set()
    providers: dict[ProviderKind, ProviderConfig] = {}
    for kind in kinds:
        typed = _text(form, f"key_{kind.value}")
        saved = stored.providers.get(kind)
        api_key = typed or (saved.api_key if saved else None) or None
        if not typed and kind.value in stored.unreadable:
            unreadable.add(kind.value)
        url = _text(form, "url_local") if kind is ProviderKind.LOCAL else ""
        providers[kind] = ProviderConfig(kind, api_key, url or None)

    errors: list[str] = []
    local = providers.get(ProviderKind.LOCAL)
    if local is not None and not local.base_url:
        errors.append(LOCAL_URL_MISSING)

    jobs: dict[Job, tuple[ProviderKind | None, str]] = {}
    for job in Job:
        label = JOB_LABELS[job]
        kind = _kind_or_none(_text(form, f"job_{job.value}_provider"))
        model = _text(form, f"job_{job.value}_model")
        jobs[job] = (kind, model)
        if kind is None:
            errors.append(f"Pick a provider for the {label}.")
        else:
            cfg = providers.get(kind)
            if cfg is None or (kind is not ProviderKind.LOCAL and not cfg.api_key):
                errors.append(
                    f"Add a key for {PROVIDER_LABELS[kind]}, "
                    f"or pick another provider for the {label}."
                )
        if not model:
            errors.append(f"Enter a model name for the {label}.")

    tavily_key = _text(form, "tavily_key")
    if not tavily_key:
        tavily_key = stored.tavily_key
        if TAVILY in stored.unreadable:
            unreadable.add(TAVILY)
    if not tavily_key:
        errors.append(TAVILY_MISSING)

    setup = AiSetup(providers, jobs, tavily_key, frozenset(unreadable))
    return setup, errors


def run_ai_checks(db: Session, setup: AiSetup, services: "Services") -> list[CheckLine]:
    """Test each job's model and the Tavily key, one after another, and report every result.

    `setup` must have come out of `parse_ai_form` with no errors. Each check is bounded
    (see `check_model`), and every one runs even when an earlier one fails.
    """
    lines: list[CheckLine] = []
    for job in Job:
        kind, model = setup.jobs[job]
        if kind is None or kind not in setup.providers:
            raise ValueError(f"The {JOB_LABELS[job]} has no provider set up.")
        result = check_model(
            db,
            setup.providers[kind],
            model,
            job=job,
            adapter_factory=services.adapter_factory,
            now=services.clock(),
        )
        label = f"{JOB_LABELS[job]}: {model} on {PROVIDER_LABELS[kind]}"
        lines.append(CheckLine(label, result))

    if setup.tavily_key:
        search = check_search(
            db, services.search_factory(setup.tavily_key), now=services.clock()
        )
    else:
        # A blank key would put the Tavily SDK in keyless mode, so no client is built.
        search = CheckResult(False, TAVILY_MISSING)
    lines.append(CheckLine(TAVILY_LABEL, search))
    return lines


def save_ai_setup(
    db: Session, vault: Vault, setup: AiSetup, *, now: datetime | None = None
) -> None:
    """Save the providers, the job assignments and the Tavily key, all or nothing.

    Rows are created or updated, never deleted: a provider left out of `setup` stays saved.
    Keys are encrypted with the vault. `now` stamps `updated_at`.
    """
    if not setup.tavily_key:
        raise ValueError(TAVILY_MISSING)
    for job, (kind, _model) in setup.jobs.items():
        if kind is None or kind not in setup.providers:
            raise ValueError(f"The {JOB_LABELS[job]} has no provider set up.")
    now = now or datetime.now(timezone.utc)

    for kind, cfg in setup.providers.items():
        api_key_enc = vault.encrypt(cfg.api_key) if cfg.api_key else None
        row = db.get(Provider, kind.value)
        if row is None:
            db.add(
                Provider(
                    kind=kind.value,
                    base_url=cfg.base_url,
                    api_key_enc=api_key_enc,
                    updated_at=now,
                )
            )
        else:
            row.base_url = cfg.base_url
            row.api_key_enc = api_key_enc
            row.updated_at = now
    for job, (kind, model) in setup.jobs.items():
        row = db.get(JobAssignment, job.value)
        if row is None:
            db.add(
                JobAssignment(
                    job=job.value, provider_kind=kind.value, model=model, updated_at=now
                )
            )
        else:
            row.provider_kind = kind.value
            row.model = model
            row.updated_at = now
    # set_secret commits, which writes the rows staged above in the same transaction.
    set_secret(db, vault, TAVILY_SETTING, setup.tavily_key)
    db.commit()


@dataclass
class AiSubmitResult:
    """What submitting the form came to, for the page to show.

    `setup` holds the values to put back in the form: what was typed, or, once `saved`,
    what is now saved. `stored` is what is saved. `errors` lists missing details (nothing
    was tested); `checks` holds one line per connection test and is None when there were
    `errors`. `saved` says every check passed and the setup was saved.
    """

    setup: AiSetup
    stored: AiSetup
    errors: list[str]
    checks: list[CheckLine] | None
    saved: bool


def submit_ai_form(
    db: Session,
    vault: Vault,
    form: Mapping[str, Any],
    services: "Services",
    *,
    now: datetime,
) -> AiSubmitResult:
    """Handle a posted form: parse it, test every connection, and save only if all pass.

    Onboarding step 2 and Settings share this. A blank key keeps the saved one (see
    `parse_ai_form`). Nothing is tested while details are missing, and nothing is saved
    unless every check passed.
    """
    stored = stored_ai_setup(db, vault)
    setup, errors = parse_ai_form(form, stored)
    if errors:
        return AiSubmitResult(setup, stored, errors, None, saved=False)

    checks = run_ai_checks(db, setup, services)
    if not all(line.result.ok for line in checks):
        return AiSubmitResult(setup, stored, [], checks, saved=False)

    save_ai_setup(db, vault, setup, now=now)
    # Read back, so the form shows keys as saved ("Saved key ending in ...") and never the
    # ones just typed, and a key that couldn't be read before is no longer marked.
    saved = stored_ai_setup(db, vault)
    return AiSubmitResult(saved, saved, [], checks, saved=True)


# --- what the form shows ------------------------------------------------------


@dataclass
class KeyField:
    """One key input: what to put in it, and what to say beside it."""

    value: str = (
        ""  # a key typed this time, kept after a failed test; never a saved key
    )
    saved: str = ""  # placeholder shown when a key is saved: "Saved key ending in abcd"
    error: str = ""


@dataclass
class ProviderRow:
    kind: ProviderKind
    label: str
    url: str  # Local only
    key: KeyField


@dataclass
class JobRow:
    job: Job
    label: str
    hint: str
    provider: ProviderKind | None
    model: str


@dataclass
class AiFormView:
    """Everything `partials/ai_form.html` needs to draw the form."""

    rows: list[ProviderRow]
    jobs: list[JobRow]
    tavily: KeyField
    provider_options: list[tuple[str, str]]
    errors: list[str]
    checks: list[CheckLine] | None
    saved: bool


def saved_key_hint(key: str | None) -> str:
    """ "Saved key ending in abcd", or "" when no key is saved."""
    if not key:
        return ""
    if len(key) < _MIN_HINT_KEY_LENGTH:
        return "Saved key"
    return f"Saved key ending in {key[-4:]}"


def _key_field(current: str | None, saved: str | None, unreadable: bool) -> KeyField:
    # A key equal to the saved one isn't "typed": it is the saved key, which is never shown.
    typed = current if current and current != saved else ""
    return KeyField(
        value=typed,
        saved=saved_key_hint(saved),
        error=KEY_UNREADABLE if unreadable else "",
    )


def provider_row(kind: ProviderKind, setup: AiSetup, stored: AiSetup) -> ProviderRow:
    """The row for one provider: `setup` holds the values to show, `stored` the saved ones."""
    cfg = setup.providers.get(kind)
    saved = stored.providers.get(kind)
    return ProviderRow(
        kind=kind,
        label=PROVIDER_LABELS[kind],
        url=(cfg.base_url or "") if cfg else "",
        key=_key_field(
            cfg.api_key if cfg else None,
            saved.api_key if saved else None,
            kind.value in setup.unreadable,
        ),
    )


def ai_form_view(
    setup: AiSetup,
    stored: AiSetup,
    *,
    errors: Sequence[str] = (),
    checks: list[CheckLine] | None = None,
    saved: bool = False,
) -> AiFormView:
    """Build the form's view of `setup`, the values to show, next to `stored`, the saved ones.

    Pass `stored` as `setup` too to show what is saved. `errors` and `checks` are the
    outcome of a submit; `saved` says the checks passed and the setup was saved.
    """
    saved_tavily = stored.tavily_key
    return AiFormView(
        rows=[provider_row(kind, setup, stored) for kind in setup.providers],
        jobs=[
            JobRow(
                job=job,
                label=JOB_LABELS[job],
                hint=JOB_HINTS[job],
                provider=setup.jobs.get(job, (None, ""))[0],
                model=setup.jobs.get(job, (None, ""))[1],
            )
            for job in Job
        ],
        tavily=_key_field(setup.tavily_key, saved_tavily, TAVILY in setup.unreadable),
        provider_options=[(kind.value, PROVIDER_LABELS[kind]) for kind in ProviderKind],
        errors=list(errors),
        checks=checks,
        saved=saved,
    )
