# Game Taste v1, plan 2 of 3: profiles and the daily refresh

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every lane and every named group gets a taste profile. Profiles build themselves from published reviews, in a background refresh that runs daily and on request. The user can read each profile, with its evidence tags, its versions and a changelog.

**Architecture:**
- **Background work:** one in-process worker thread runs queued jobs from a `job` table, one at a time. A scheduler thread enqueues the daily refresh.
- **The refresh:** syncs Steam, then for each profile with unprocessed review versions asks the Everyday tasks model for an updated profile as JSON. The reply is validated against the evidence rules, and the profile is saved as a new version linked to the review versions it learned from.
- **Pages and prompts:** profile pages are server-rendered like the rest of the app. Prompts are text files in `app/prompts/`.

**Tech stack:** as plan 1. No new dependencies. pydantic (already installed through FastAPI) validates model replies. Jinja2 renders prompts.

**Spec:** `docs/superpowers/specs/2026-10-30-game-taste-v1-design.md`. Sections 4, 6.6, 6.9, 7.3 and 8 cover this plan. Plan 1 (`docs/superpowers/plans/2026-10-30-plan-1-foundation-onboarding-reviews.md`) built everything this plan builds on.

**Plan 3** adds game facts, research, the Recommendations queue, the judge and the score. Two refresh steps from spec 7.3 wait for it: refreshing facts for inventory games (step 5) and re-ranking the queue (step 6). So does feeding declines into profiles, because declines don't exist yet.

## Global constraints

Plan 1's global constraints all still apply: stack, data rules, secrets, Ember, copy voice, theme, code style and lanes. They are additive to these.

**Prompts and AI**
- Every AI call goes through `call_and_log`, which records the prompt name and the prompt version.
- Prompts are Jinja2 text files in `app/prompts/`.
- A prompt's version is the sha256 hex of its file's bytes.

**What profiles learn from**
- Profiles are derived data. Nothing in this plan changes a review, a review version or anything else the user wrote.
- Review text goes to the model exactly as stored.

**Profile content (designer's call, replacing the spec's "rendered from Markdown")**
- A profile version is stored as structured JSON with five sections:
  - `loved`
  - `dealbreakers`
  - `patterns`
  - `hunches`
  - `open_questions`
- Every line in the first four sections has `text`, `tag`, `games` (appids) and `reviews` (review version ids).
- `tag` is one of `stated`, `observed` or `guess`.
- Open questions are plain strings with no tag, because they're questions, not claims.

**Evidence rules (checked in code, not trusted to the model)**
- A `stated` line cites at least one review version the profile has learned from.
- An `observed` line names at least two different reviewed games.
- Every `hunches` line is a `guess`.
- Games cited anywhere must be reviewed games, or, for `guess` lines only, games from the play-history summary.
- If a check fails, the old profile stays and the failure is recorded.

**Which profiles exist**
- One profile per lane and one per named group.
- A profile is first created when its lane or group has at least one published review version. Before that, its page shows the empty state.
- A version belongs to its lane's profile, and also to its named group's profile when it's tagged with one.

**Background threads**
- They start only when `Services.background` is true.
- The test fixture sets it to false. Tests run jobs with `run_pending_jobs(app)`.

**Migrations and copy**
- New NOT NULL columns get a `server_default`.
- New migrations chain from the latest revision: `0003`, `0004` and so on.
- Copy follows Ember's voice:
  - errors say what happened, then what to do
  - no exclamation marks
  - one primary button per view

## Review focus

1. **A model reply that isn't usable JSON:** wrapped in code fences, chatty, truncated, or valid JSON in the wrong shape. Fences and surrounding text are tolerated. One retry with the error. Then that profile fails cleanly and the old version stays. The worker keeps running and the status shows why. (Tasks 2, 5)
2. **A restart while a refresh is running:** the job is marked failed at startup with a clear message, and the next refresh, manual or scheduled, runs normally. No job stays "running" forever. (Task 3)
3. **Double requests:** two quick "Refresh now" clicks, or a manual refresh around the scheduled time, give one job. A refresh at or after today's scheduled time counts as today's scheduled refresh. (Task 3)
4. **A model that breaks the evidence rules:** it invents a game or a review id, cites one game on an `observed` line, or puts a `stated` line in hunches. The update is refused, the old profile stays, and the failure names the rule. (Tasks 4, 5)
5. **Timezones and DST:** the refresh time is in the user's timezone, including on DST-change days (a time that doesn't exist or happens twice) and after the timezone changes. At most one scheduled refresh runs per day. (Task 3)

---

## File structure (new and changed)

```
app/
  main.py               Services.background; startup job recovery; lifespan starts worker and scheduler
  db.py                 WAL + busy_timeout on connect
  web.py                parse_id() helper; httpx logger level
  jobs.py               Job queue, worker, scheduler, run_pending_jobs, JobContext
  prompts/
    __init__.py         load_prompt(), Prompt
    profile_update.md   the profile prompt
  services/
    ai.py               Completion.stop_reason/call_id; prompt_version; job_config(); complete_json()
    profiles.py         profile rows, content schema, sources, validation, saving, listing
  routines/
    __init__.py
    refresh.py          run_refresh() job handler
  routes/
    profiles.py         /profiles pages
    refresh.py          POST /refresh, GET /refresh/status
  models.py             AiCall.prompt_version; Job, Profile, ProfileVersion, ProfileVersionSource, RefreshRun
  migrations/versions/  0003_prompt_version.py, 0004_jobs.py, 0005_profiles.py, 0006_refresh_runs.py
  templates/
    base.html           Profiles in the side nav; toast region above dialogs; htmx error toast
    profiles.html, profile.html
    partials/refresh_status.html, partials/profile_lines.html
    partials/settings_schedule.html   (Refresh now)
    partials/review_dialog.html       (options from service constants)
tests/
  fakes.py              FakeAdapterFactory replies as lists/callables, stop_reasons; profile_reply()
  test_followups.py, test_structured_ai.py, test_jobs.py, test_profiles.py, test_refresh.py,
  test_profiles_pages.py, test_refresh_routes.py
README.md
```

---

### Task 1: Plan 1 follow-ups

These are the fixes the plan 1 final review queued for the start of plan 2.

**Files:**
- Modify: `app/web.py`, `app/routes/reviews.py`, `app/db.py`, `app/main.py`, `app/templates/partials/review_dialog.html`, `app/templates/base.html`, `tests/test_home.py:294`
- Test: `tests/test_followups.py`

**Interfaces:**
- Produces:
  - `parse_id(value: object) -> int | None` in `app/web.py`. It returns an int in `1..2**63-1` for an ASCII-digit string within that range, and None for anything else: blank, non-digits, out of range, or too long.
  - `make_engine` runs `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on each connection, next to the foreign-keys pragma.

- [ ] **Step 1: Write the failing tests.**

```python
@pytest.mark.parametrize("value,expected", [("12", 12), (" 7 ", 7), ("0", None), ("-1", None), ("", None),
    ("1.5", None), ("٣", None), ("9" * 5000, None), (str(2**63), None), (str(2**63 - 1), 2**63 - 1), (None, None)])
def test_parse_id(value, expected):
    assert parse_id(value) == expected

@pytest.mark.parametrize("path", [
    "/people/options?named_group_id=99999999999999999999999&lane=two",
])
def test_huge_ids_never_500(onboarded_client, games, path):
    assert onboarded_client.get(path).status_code in (200, 404, 422)

def test_huge_ids_in_dialog_posts_are_refused(onboarded_client, games):
    r = onboarded_client.post("/games/10/review/two", data={"action": "draft", "text": "x",
                              "named_group_id": "9" * 30, "people": ["9" * 30]})
    assert r.status_code == 200 and "Pick people from your list." in r.text

def test_sqlite_uses_wal_and_busy_timeout(app):
    with app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000

def test_httpx_logs_nothing_below_warning(app):
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING

def test_dialog_options_come_from_the_service(onboarded_client, games):
    text = onboarded_client.get("/games/10/review?lane=solo").text
    for value in STATUSES + VERDICTS + WALK_AWAYS + COMPLEXITIES:
        assert f'value="{value}"' in text

def test_base_has_an_error_toast_hook(onboarded_client):
    text = onboarded_client.get("/").text
    assert "htmx:responseError" in text and "htmx:sendError" in text
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_followups.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **`parse_id` callers:** use it everywhere `app/routes/reviews.py` turns form or query ids into ints (around lines 87, 113, 122 and 387). The rating keeps its own check. A refused id gives the existing "Pick people from your list." re-render (200), or a 404 for a path.
  - **WAL and busy_timeout:** add both pragmas in `make_engine`.
  - **httpx logging:** set `logging.getLogger("httpx").setLevel(logging.WARNING)` at import of `app/web.py`.
  - **Dialog options:** the dialog's status, verdict, walk-away and complexity options come from the service constants, passed in with their labels.
  - **Error toast:** add a small script in `base.html`. On `htmx:responseError` or `htmx:sendError` it adds a danger toast, "Something went wrong. Try again.", to `#toasts`.
  - **Toasts above dialogs:** make `#toasts` Ember's ToastRegion structure (`section.em-toast-region[popover="manual"]` holding `div.em-toast-region__list[aria-live="polite"]`, as in `C:\Users\david\home\Ember\components\src\feedback.tsx`). Re-show the popover each time a toast is added, so it sits above an open modal dialog. Keep existing toast swaps working by targeting the list.
  - **Broken test:** fix `tests/test_home.py:294`. Replace the literal backspace with `\b`, and check that the assertion still passes.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `fix: plan 1 follow-ups (bounded ids, WAL, logging, dialog options, error toasts)`.

---

### Task 2: Structured AI calls

**Files:**
- Create: `app/prompts/__init__.py`, `app/migrations/versions/0003_prompt_version.py`
- Modify: `app/services/ai.py`, `app/models.py`, `tests/fakes.py`
- Test: `tests/test_structured_ai.py`

**Interfaces:**
- Consumes: `call_and_log`, `make_adapter`, `ProviderConfig`, `Job` (plan 1, `app/services/ai.py`); `stored_ai_setup` (`app/services/ai_setup.py`).
- Produces:
  - `Completion` gains `stop_reason: str | None = None` (`"stop"`, `"length"` or None) and `call_id: int | None = None`.
    - `call_and_log` sets `call_id` to the logged row's id.
    - The adapters normalise their stop reasons: OpenAI `finish_reason == "length"`, Anthropic `stop_reason == "max_tokens"` and Gemini `finish_reason` MAX_TOKENS all become `"length"`. Any other value becomes `"stop"`.
  - `AiCall.prompt_version: str` (NOT NULL, `server_default=""`), via migration `0003`. `call_and_log` gains `prompt_version: str = ""`.
  - `@dataclass(frozen=True) Prompt(name: str, version: str, source: str)` with `render(**context) -> str`. It uses Jinja2 with `StrictUndefined`, no autoescape, and `keep_trailing_newline`.
  - `load_prompt(name: str) -> Prompt` reads `app/prompts/{name}.md`. `version` is the sha256 hex of the file's bytes.
  - `job_config(db, vault, job: Job) -> tuple[ProviderConfig, str] | None` returns the stored provider and model for `job`. It returns None if the job isn't assigned, its provider isn't saved, or (for a non-Local provider) the key is missing or unreadable.
  - `class StructuredReplyError(AIError)` with `.reason`, one of `"not_configured"`, `"truncated"`, `"unusable"` or `"call_failed"`. Its user-facing message is in `.message`:
    - `not_configured`: `"Set up the {Job label} model in Settings, then refresh again."`
    - `truncated`: `"The model's reply was cut off. Pick a model with a larger output limit in Settings."`
    - `unusable`: `"The model's reply wasn't in the expected format."`
    - `call_failed`: `"The model didn't answer: {error, first 200 chars}"`
  - `complete_json(db, vault, job, prompt: Prompt, context: dict, schema: type[BaseModel], *, services, run_ref: str | None, now: datetime) -> tuple[BaseModel, int]`. It returns the parsed model and the `ai_call` id of the call that produced it. Steps:
    1. Get the config with `job_config`, or raise `not_configured`.
    2. Render the prompt and call `call_and_log` with `prompt_name` set to `prompt.name` and `prompt_version` set to `prompt.version`.
    3. If `stop_reason == "length"`, raise `truncated` (no retry).
    4. Extract JSON: strip a code fence if present, then take the text from the first `{` to the matching last `}`. Load it and validate it with `schema.model_validate`.
    5. If that fails, call once more with the prompt plus `"\n\nYour previous reply could not be used: {first 300 chars of the error}. Reply again with only the JSON object."`. If the second reply also fails, raise `unusable`.
    6. A provider failure on either call raises `call_failed`.
    7. Every call is logged.
  - In `tests/fakes.py`, `FakeAdapterFactory.replies[model]` may be a `str`, a `list[str]` (used in order, and the last one repeats), or a callable `(prompt) -> str`. `.stop_reasons[model]` sets `Completion.stop_reason`.

- [ ] **Step 1: Write the failing tests.**

```python
class Answer(BaseModel):
    value: int

def test_load_prompt_versions_by_file(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    (tmp_path / "hello.md").write_text("Hi {{ name }}\n", encoding="utf-8")
    p = load_prompt("hello")
    assert p.render(name="Sam") == "Hi Sam\n" and p.version == hashlib.sha256(b"Hi {{ name }}\n").hexdigest()
    with pytest.raises(jinja2.UndefinedError):
        p.render()

@pytest.mark.parametrize("reply", ['{"value": 3}', '```json\n{"value": 3}\n```', 'Sure! {"value": 3} Hope that helps.'])
def test_complete_json_tolerates_wrapping(db, app, services, fake_ai, saved_ai_setup, reply):
    fake_ai.replies["sample-local"] = reply
    answer, call_id = complete_json(db, app.state.vault, Job.EVERYDAY, PROMPT, {}, Answer,
                                    services=services, run_ref="test:1", now=FIXED_NOW)
    row = db.get(AiCall, call_id)
    assert answer.value == 3 and row.prompt_name == PROMPT.name and row.prompt_version == PROMPT.version and row.run_ref == "test:1"

def test_complete_json_retries_once_then_gives_up(db, app, services, fake_ai, saved_ai_setup):  # Review focus 1
    fake_ai.replies["sample-local"] = ["not json", '{"value": "x"}']
    with pytest.raises(StructuredReplyError) as err:
        complete_json(db, app.state.vault, Job.EVERYDAY, PROMPT, {}, Answer, services=services, run_ref=None, now=FIXED_NOW)
    assert err.value.reason == "unusable" and len(fake_ai.calls) == 2
    assert "Your previous reply could not be used" in fake_ai.calls[1][2]

def test_complete_json_truncated_does_not_retry(db, app, services, fake_ai, saved_ai_setup):  # Review focus 1
    fake_ai.replies["sample-local"] = '{"value": 3'
    fake_ai.stop_reasons["sample-local"] = "length"
    with pytest.raises(StructuredReplyError) as err:
        complete_json(db, app.state.vault, Job.EVERYDAY, PROMPT, {}, Answer, services=services, run_ref=None, now=FIXED_NOW)
    assert err.value.reason == "truncated" and len(fake_ai.calls) == 1

def test_complete_json_not_configured(db, app, services):
    with pytest.raises(StructuredReplyError) as err:
        complete_json(db, app.state.vault, Job.EVERYDAY, PROMPT, {}, Answer, services=services, run_ref=None, now=FIXED_NOW)
    assert err.value.reason == "not_configured" and err.value.message == "Set up the Everyday tasks model in Settings, then refresh again."

@pytest.mark.parametrize("raw,expected", [("length", "length"), ("stop", "stop"), ("content_filter", "stop"), (None, None)])
def test_openai_stop_reason(monkeypatch, raw, expected):
    def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="x"), finish_reason=raw)], usage=None)
    monkeypatch.setattr(OpenAICompletions, "create", create)
    assert OpenAICompatibleAdapter("k", None).complete("m", "p").stop_reason == expected

@pytest.mark.parametrize("raw,expected", [("max_tokens", "length"), ("end_turn", "stop"), (None, None)])
def test_anthropic_stop_reason(monkeypatch, raw, expected):
    def create(self, **kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="x")], stop_reason=raw,
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(AnthropicMessages, "create", create)
    assert AnthropicAdapter("k").complete("m", "p").stop_reason == expected

@pytest.mark.parametrize("raw,expected", [("MAX_TOKENS", "length"), ("STOP", "stop"), (None, None)])
def test_gemini_stop_reason(monkeypatch, raw, expected):
    reason = None if raw is None else getattr(genai_types.FinishReason, raw)
    def generate(self, **kwargs):
        return SimpleNamespace(text="x", usage_metadata=None, candidates=[SimpleNamespace(finish_reason=reason)])
    monkeypatch.setattr(GeminiModels, "generate_content", generate)
    assert GeminiAdapter("k").complete("m", "p").stop_reason == expected
```

- `PROMPT` is a module-level `Prompt("test_prompt", "v1", "Answer with {\"value\": 3}.")`.
- `OpenAICompletions`, `AnthropicMessages` and `GeminiModels` are the SDK classes `tests/test_ai.py` already patches.
- `genai_types` is `google.genai.types`.
- For OpenAI, Anthropic and Gemini alike, any other non-empty reason becomes `"stop"`, and a missing one stays None.

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_structured_ai.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the changes and migration `0003`. Autogenerate it against a scratch DB at `0002`, check it, and confirm the drift test in `tests/test_db.py` still passes. Check the Gemini finish-reason enum in the installed `google-genai` source rather than from memory.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: structured AI calls with prompt versions, stop reasons and one retry`.

---

### Task 3: Job queue, worker and daily schedule

**Files:**
- Create: `app/jobs.py`, `app/migrations/versions/0004_jobs.py`
- Modify: `app/models.py`, `app/main.py`, `tests/conftest.py` (`Services(..., background=False)`)
- Test: `tests/test_jobs.py`

**Interfaces:**
- Produces:
  - Model `Job` (table `job`):
    - `id` PK
    - `kind` str (`"refresh"`; plan 3 adds `"research"`)
    - `status` str: `queued`, `running`, `done` or `failed`
    - `requested_by` str: `user` or `schedule`
    - `created_at`
    - `started_at` null, `finished_at` null
    - `error` Text null
  - `Services` gains `background: bool = True`.
  - `@dataclass JobContext(session_factory, vault, services)`, and `job_context(app) -> JobContext`.
  - `app.state.job_handlers: dict[str, Callable[[JobContext, int], None]]`. The handler gets the job id. `create_app` starts it empty, and Task 5 registers `"refresh"`.
  - `enqueue_job(db, kind: str, requested_by: str, now) -> Job`. It returns the existing queued or running job of that `kind` if there is one; otherwise it creates one. It commits.
  - `run_pending_jobs(app) -> int` processes queued jobs one at a time, oldest first, and returns how many it ran. For each job:
    1. Claim it: an atomic `UPDATE job SET status='running', started_at=:now WHERE id=:id AND status='queued'`.
    2. Run its handler in a fresh session.
    3. On success, set `done` and `finished_at`.
    4. On an exception, set `failed` with `error` = `"The {kind} stopped unexpectedly: {ExceptionType}. Try again."` and log the traceback with `logging.getLogger("game_taste.jobs").exception`.
    5. A kind with no handler fails with `"Nothing can run a {kind} job yet."`.
  - `recover_interrupted_jobs(db, now) -> int` marks `running` jobs `failed`, with error `"Interrupted by a restart. Start it again, or wait for the next scheduled refresh."`. `create_app` calls it at startup.
  - `last_scheduled_time(now: datetime, refresh_time: str, tz: str) -> datetime` is the most recent moment at or before `now` when the daily refresh was due, in UTC.
    - Local `refresh_time` today in `tz`, or yesterday's if today's hasn't come yet.
    - It uses `ZoneInfo` with `fold=0`. A time that doesn't exist maps forward, the way zoneinfo does with fold=0; a time that happens twice uses the first occurrence.
  - `scheduled_refresh_due(db, now) -> bool` is true when setup is finished (`setup_finished`) and no `refresh` job of any status or requester was created at or after `last_scheduled_time(now, refresh_time_setting or "04:00", timezone_setting or "UTC")`.
  - Lifespan: when `services.background` is true, `create_app` registers a lifespan that starts two daemon threads and stops them on shutdown.
    - Worker: loops on `run_pending_jobs(app)`, then waits on `app.state.job_wakeup` (a `threading.Event`) for up to 5 s.
    - Scheduler: every 60 s, if `scheduled_refresh_due`, enqueues `("refresh", "schedule")` and sets the wakeup.
  - `notify_worker(app)` sets the wakeup event. Routes call it after enqueueing.

- [ ] **Step 1: Write the failing tests.**

```python
def test_enqueue_is_idempotent_while_pending(db):  # Review focus 3
    a = enqueue_job(db, "refresh", "user", FIXED_NOW)
    b = enqueue_job(db, "refresh", "schedule", FIXED_NOW + timedelta(seconds=1))
    assert a.id == b.id and db.scalar(select(func.count()).select_from(Job)) == 1

def test_run_pending_jobs_runs_handler_and_records_outcome(app, db):
    seen = []
    app.state.job_handlers["refresh"] = lambda ctx, job_id: seen.append(job_id)
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    assert run_pending_jobs(app) == 1 and seen == [job.id]
    db.expire_all()
    assert db.get(Job, job.id).status == "done" and db.get(Job, job.id).finished_at is not None

def test_handler_exception_fails_job_without_stopping(app, db):
    def boom(ctx, job_id): raise RuntimeError("sk-secret-in-message")
    app.state.job_handlers["refresh"] = boom
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    run_pending_jobs(app)
    db.expire_all()
    row = db.get(Job, job.id)
    assert row.status == "failed" and row.error == "The refresh stopped unexpectedly: RuntimeError. Try again."
    assert "sk-secret" not in row.error

def test_restart_marks_running_jobs_failed(tmp_path, services):  # Review focus 2
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        job = enqueue_job(s, "refresh", "user", FIXED_NOW)
        s.execute(update(Job).where(Job.id == job.id).values(status="running"))
        s.commit()
    app2 = create_app(tmp_path, services=services)
    with app2.state.session_factory() as s:
        row = s.get(Job, job.id)
        assert row.status == "failed" and row.error.startswith("Interrupted by a restart.")
        assert enqueue_job(s, "refresh", "user", FIXED_NOW).id != job.id

@pytest.mark.parametrize("now,time,tz,expected", [  # Review focus 5
    (datetime(2026, 1, 15, 12, 0, tzinfo=UTC), "04:00", "America/New_York", datetime(2026, 1, 15, 9, 0, tzinfo=UTC)),
    (datetime(2026, 1, 15, 8, 0, tzinfo=UTC), "04:00", "America/New_York", datetime(2026, 1, 14, 9, 0, tzinfo=UTC)),
    (datetime(2026, 3, 8, 12, 0, tzinfo=UTC), "02:30", "America/New_York", datetime(2026, 3, 8, 7, 30, tzinfo=UTC)),
    (datetime(2026, 11, 1, 12, 0, tzinfo=UTC), "01:30", "America/New_York", datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
    (datetime(2026, 1, 15, 12, 0, tzinfo=UTC), "23:59", "UTC", datetime(2026, 1, 14, 23, 59, tzinfo=UTC)),
])
def test_last_scheduled_time(now, time, tz, expected):
    assert last_scheduled_time(now, time, tz) == expected

def test_scheduled_refresh_due_once_per_day(onboarded_client, db):  # Review focus 3 and 5
    set_setting(db, "refresh_time", "04:00"); set_setting(db, "timezone", "UTC")
    now = datetime(2026, 1, 15, 5, 0, tzinfo=UTC)
    assert scheduled_refresh_due(db, now)
    enqueue_job(db, "refresh", "user", datetime(2026, 1, 15, 4, 30, tzinfo=UTC))   # a manual refresh after 04:00 counts
    assert not scheduled_refresh_due(db, now)
    assert scheduled_refresh_due(db, datetime(2026, 1, 16, 4, 0, tzinfo=UTC))

def test_not_due_before_setup_is_finished(logged_in_client, db):
    assert not scheduled_refresh_due(db, FIXED_NOW)

def test_background_threads_only_when_enabled(tmp_path, services):
    app = create_app(tmp_path, services=services)          # services.background is False in tests
    with TestClient(app):
        assert not any(t.name.startswith("game-taste-") for t in threading.enumerate())
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_jobs.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** `app/jobs.py`, the model, migration `0004`, and the `create_app` wiring.
  - Name the threads `game-taste-worker` and `game-taste-scheduler`.
  - The scheduler and worker each use their own sessions.
  - Exceptions inside the loops are logged and never end the loop.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: background job queue, worker and daily refresh schedule`.

---

### Task 4: Profile data and evidence rules

**Files:**
- Create: `app/services/profiles.py`, `app/migrations/versions/0005_profiles.py`
- Modify: `app/models.py`, `tests/fakes.py` (`profile_reply`)
- Test: `tests/test_profiles.py`

**Interfaces:**
- Consumes: `Lane`, `LANE_LABELS`, `ReviewVersion`, `Review`, `Game`, `NamedGroup`, `Person` (plan 1).
- Produces:
  - Models:
    - `Profile` (table `profile`): `id` PK, `key` str unique (`"lane:solo"`, `"group:3"`), `kind` str (`lane` or `group`), `lane` str null, `group_id` FK `named_group.id` null, `created_at`.
    - `ProfileVersion` (table `profile_version`), unique (`profile_id`, `version`). Columns:
      - `id` PK, `profile_id` FK, `version` int
      - `content` JSON, `changelog` Text
      - `ai_call_id` FK `ai_call.id` null, `refresh_run_id` int null (the FK is added in Task 5's migration)
      - `model` str, `provider_kind` str, `prompt_name` str, `prompt_version` str
      - `created_at`
    - `ProfileVersionSource` (table `profile_version_source`): `profile_version_id` FK, `review_version_id` FK, PK on both.
  - Pydantic schema:

```python
class ProfileLine(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    tag: Literal["stated", "observed", "guess"]
    games: list[int] = []
    reviews: list[int] = []

class ProfileContent(BaseModel):
    loved: list[ProfileLine] = Field(default=[], max_length=15)
    dealbreakers: list[ProfileLine] = Field(default=[], max_length=15)
    patterns: list[ProfileLine] = Field(default=[], max_length=15)
    hunches: list[ProfileLine] = Field(default=[], max_length=15)
    open_questions: list[str] = Field(default=[], max_length=10)

class ProfileUpdate(BaseModel):
    profile: ProfileContent
    changelog: str = Field(min_length=1, max_length=300)
```

  - Keys and labels:
    - `SECTION_LABELS`: `{"loved": "What you love", "dealbreakers": "Dealbreakers", "patterns": "Patterns", "hunches": "Hunches to test", "open_questions": "Open questions"}`.
    - `TAG_LABELS`: `{"stated": "Stated", "observed": "Observed", "guess": "Guess"}`.
    - `lane_key(lane) -> str` and `group_key(group_id) -> str`.
  - `@dataclass ProfileTarget(key: str, kind: str, lane: Lane | None, group_id: int | None, label: str)`.
  - `targets_with_reviews(db) -> list[ProfileTarget]`: every lane with at least one published version, plus every named group tagged on at least one version. Lanes come first in lane order, then groups by name.
  - `ensure_profile(db, target, now) -> Profile` (commits).
  - `unprocessed_versions(db, profile, limit: int = 20) -> list[ReviewVersion]`:
    - Which versions: those belonging to the profile (its lane, or tagged with its group) and not yet linked to any of its versions.
    - Order: oldest first by `published_at`, then `id`.
  - `profile_sources(db, profile) -> tuple[set[int], set[int]]`: every review version id linked to any version of the profile, and those versions' appids.
  - `current_version(db, profile) -> ProfileVersion | None`.
  - `review_input(db, versions) -> list[dict]`. Each dict has:
    - `id`, `ref` (`"R{id}"`)
    - `appid`, `game` (name), `lane` (label)
    - `version`, `published` (ISO date)
    - `rating`, `verdict`, `status`, `walk_away`, `session_length`, `complexity`, `tags`
    - `played_with` (names), `group` (name or None)
    - `text` and `group_text`, both exactly as stored
    - `steam_hours`
    - `earlier` (`{"rating", "verdict"}` of the previous version of the same review, or None)
  - `play_history(db, limit: int = 25) -> list[dict]`: owned games with playtime over 0, most hours first. Each has `appid`, `game`, `hours` (one decimal) and `last_played` (ISO date or None).
  - `validate_update(update, *, review_ids: set[int], reviewed_appids: set[int], history_appids: set[int]) -> list[str]` returns every problem, in this exact copy:
    - `"{Section}: a stated line must cite a review it learned from."`
    - `"{Section}: an observed line must name at least two reviewed games."`
    - `"Hunches to test: every line must be a guess."`
    - `"{Section}: a line cites a game that isn't in your reviews."`. Guess lines may also cite games from `history_appids`.
    - `"{Section}: a line cites a review it didn't learn from."`
  - `save_version(db, profile, update, *, source_ids: list[int], call: AiCall, refresh_run_id: int | None, now) -> ProfileVersion`. It sets `version` to the previous maximum plus 1, stores `content=update.profile.model_dump()`, adds the source rows, and commits.
  - `@dataclass ProfileSummary(target: ProfileTarget, profile: Profile | None, latest: ProfileVersion | None, version_count: int)`, and `list_profiles(db) -> list[ProfileSummary]`: all four lanes always, then every named group by name, with None when no profile exists yet.
  - `profile_reply(changelog: str = "Learned from new reviews.", **sections) -> str` in `tests/fakes.py` builds a JSON reply string in the `ProfileUpdate` shape. Missing sections are empty lists.

- [ ] **Step 1: Write the failing tests.**

```python
def test_targets_follow_published_versions(db, games, people):
    sam, alex, crew = people
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="draft only"), FIXED_NOW)
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(rating=9, named_group_id=crew.id, text="fun"), FIXED_NOW)
    assert [t.key for t in targets_with_reviews(db)] == ["lane:three_four", f"group:{crew.id}"]

def test_unprocessed_versions_oldest_first_and_once(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    v2 = publish(db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW + timedelta(minutes=1))
    profile = ensure_profile(db, targets_with_reviews(db)[0], FIXED_NOW)
    assert [v.id for v in unprocessed_versions(db, profile)] == [v1.id, v2.id]
    save_version(db, profile, ok_update(reviews=[v1.id], games=[10]), source_ids=[v1.id], call=fake_call(db), refresh_run_id=None, now=FIXED_NOW)
    assert [v.id for v in unprocessed_versions(db, profile)] == [v2.id]

def test_review_input_is_verbatim(db, games):
    raw = "  <b>Loved</b> it 🎲\n\nsecond  "
    v = publish(db, 10, Lane.SOLO, ReviewForm(text=raw, rating=8), FIXED_NOW)
    [item] = review_input(db, [v])
    assert item["text"] == raw and item["ref"] == f"R{v.id}" and item["game"] == "Sample Quest"

@pytest.mark.parametrize("line,section,problem", [  # Review focus 4
    ({"text": "x", "tag": "stated", "reviews": []}, "loved", "What you love: a stated line must cite a review it learned from."),
    ({"text": "x", "tag": "observed", "games": [10]}, "patterns", "Patterns: an observed line must name at least two reviewed games."),
    ({"text": "x", "tag": "stated", "reviews": [1]}, "hunches", "Hunches to test: every line must be a guess."),
    ({"text": "x", "tag": "guess", "games": [999]}, "hunches", "Hunches to test: a line cites a game that isn't in your reviews."),
    ({"text": "x", "tag": "stated", "reviews": [77]}, "loved", "What you love: a line cites a review it didn't learn from."),
])
def test_validate_update_rules(line, section, problem):
    update = ProfileUpdate.model_validate({"profile": {section: [line]}, "changelog": "c"})
    assert problem in validate_update(update, review_ids={1}, reviewed_appids={10, 20}, history_appids={30})

def test_guess_may_cite_play_history():
    update = ProfileUpdate.model_validate({"profile": {"hunches": [{"text": "x", "tag": "guess", "games": [30]}]}, "changelog": "c"})
    assert validate_update(update, review_ids=set(), reviewed_appids=set(), history_appids={30}) == []

def test_list_profiles_always_shows_four_lanes(db, people):
    keys = [s.target.key for s in list_profiles(db)]
    assert keys[:4] == ["lane:solo", "lane:two", "lane:three_four", "lane:five_plus"] and keys[4:] == [f"group:{people[2].id}"]
```

`ok_update` and `fake_call` are small helpers in the test module. `fake_call` inserts an `AiCall` row.

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_profiles.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the service, the models and migration `0005` (checked against the drift test). Queries must stay a fixed number per call, so use no per-version lookups in loops.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: taste profile records, sources and evidence rules`.

---

### Task 5: The refresh routine

**Files:**
- Create: `app/routines/__init__.py`, `app/routines/refresh.py`, `app/prompts/profile_update.md`, `app/migrations/versions/0006_refresh_runs.py`
- Modify: `app/models.py`, `app/main.py` (register the handler)
- Test: `tests/test_refresh.py`

**Interfaces:**
- Consumes:
  - Task 2: `complete_json`, `load_prompt`, `StructuredReplyError`.
  - Task 3: `JobContext`, `app.state.job_handlers`, `run_pending_jobs`.
  - Task 4: everything in `profiles.py`.
  - Plan 1: `steam_credentials`, `sync_library`, `SteamClient`, `SteamError`.
- Produces:
  - Model `RefreshRun` (table `refresh_run`):
    - `id` PK
    - `job_id` FK `job.id` unique
    - `started_at`, `finished_at` null
    - `steam_summary` Text (server_default `""`)
    - `profiles_updated` int (server_default `0`), `profiles_failed` int (server_default `0`)
    - `details` JSON (server_default `'[]'`): a list of `{"key", "label", "outcome": "updated" | "failed" | "skipped", "versions": [int], "message": str}`
  - Migration `0006` also adds the `profile_version.refresh_run_id` foreign key.
  - `run_refresh(ctx: JobContext, job_id: int) -> None`, registered as `app.state.job_handlers["refresh"]`. Steps:
    1. Create the `RefreshRun`.
    2. **Steam:** if `steam_credentials` is set, call `sync_library` with `SteamClient(ctx.services.steam_http)`. Record `"Steam synced: {added} new, {total} total."`, or the `SteamError` message, and carry on either way. Without Steam, record `"Steam isn't connected."`.
    3. **Profiles:** for each target in `targets_with_reviews`, `ensure_profile`. Then, while `unprocessed_versions(limit=20)` is non-empty:
       1. Build the context:
          - `profile_label`, `kind`
          - `members` (group member names, for groups)
          - `current` (the current content as JSON, or None)
          - `reviews` (`review_input`)
          - `play_history` (lanes only; empty for groups)
          - `today` (ISO date in the user's timezone)
       2. Call `complete_json(Job.EVERYDAY, load_prompt("profile_update"), context, ProfileUpdate, run_ref=f"refresh:{run.id}")`.
       3. Validate with the profile's sources plus this batch. `history_appids` comes from the play history sent.
       4. On success, call `save_version` with this batch as sources and `call=db.get(AiCall, call_id)`, which supplies the model, provider and prompt name and version.
       5. On a `StructuredReplyError` or a validation failure, record `failed` with the message. For a validation failure the message is `"The model's update didn't pass the checks, so this profile wasn't changed: {first problem}"`. Stop this profile; its remaining versions wait for the next refresh.
    4. **Not configured:** a `not_configured` error stops the whole refresh after recording it, because every profile would fail the same way.
    5. Set the counts and `finished_at`.
  - `app/prompts/profile_update.md` is the prompt. Its wording is the builder's call, but it must:
    - explain the five sections, the three tags and the evidence rules, as stated in Global constraints;
    - show each review with its `ref` (`R{id}`), and require JSON only, in the `ProfileUpdate` shape, citing reviews by their integer `id` and games by `appid`;
    - say to keep and refine the current profile rather than start over;
    - say to use the user's own words where they fit, and never to invent games or facts;
    - say to use play history only for guesses and open questions, and that idle-game hours are inflated (principle 9);
    - for groups, say to weigh "how it went for the group" text, which is the user's read of the group;
    - ask for one plain changelog line describing what changed and why.

- [ ] **Step 1: Write the failing tests.**

```python
def refresh(app, db):
    enqueue_job(db, "refresh", "user", FIXED_NOW)
    run_pending_jobs(app)
    db.expire_all()
    return db.scalars(select(RefreshRun).order_by(RefreshRun.id.desc())).first()

def test_refresh_builds_lane_and_group_profiles(app, db, games, people, saved_ai_setup, fake_ai):
    sam, alex, crew = people
    v = publish(db, 10, Lane.THREE_FOUR, ReviewForm(rating=9, named_group_id=crew.id, text="Great nights", group_text="They loved it"), FIXED_NOW)
    fake_ai.replies["sample-local"] = profile_reply(loved=[{"text": "Great nights", "tag": "stated", "reviews": [v.id], "games": [10]}])
    run = refresh(app, db)
    assert run.profiles_updated == 2 and run.profiles_failed == 0
    for key in ("lane:three_four", f"group:{crew.id}"):
        profile = db.scalar(select(Profile).where(Profile.key == key))
        latest = current_version(db, profile)
        assert latest.version == 1 and latest.prompt_name == "profile_update" and latest.refresh_run_id == run.id
        assert [s.review_version_id for s in latest_sources(db, latest)] == [v.id]
    assert "They loved it" in fake_ai.calls[0][2]          # the user's words reach the model as written

def test_second_refresh_with_nothing_new_calls_no_model(app, db, games, saved_ai_setup, fake_ai):
    v = publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = profile_reply(loved=[{"text": "ok", "tag": "stated", "reviews": [v.id], "games": [10]}])
    refresh(app, db); calls = len(fake_ai.calls)
    run = refresh(app, db)
    assert len(fake_ai.calls) == calls and run.profiles_updated == 0

def test_rule_breaking_reply_keeps_old_profile(app, db, games, saved_ai_setup, fake_ai):  # Review focus 4
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = profile_reply(patterns=[{"text": "x", "tag": "observed", "games": [10]}])
    run = refresh(app, db)
    assert run.profiles_failed == 1 and "observed line must name at least two reviewed games" in run.details[0]["message"]
    assert db.scalar(select(func.count()).select_from(ProfileVersion)) == 0
    assert db.get(Job, run.job_id).status == "done"

def test_unusable_reply_fails_profile_not_worker(app, db, games, saved_ai_setup, fake_ai):  # Review focus 1
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = "I can't do JSON today."
    run = refresh(app, db)
    assert run.details[0]["outcome"] == "failed" and run.details[0]["message"] == "The model's reply wasn't in the expected format."

def test_not_configured_stops_refresh_with_message(app, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    run = refresh(app, db)
    assert run.details[0]["message"] == "Set up the Everyday tasks model in Settings, then refresh again."

def test_steam_failure_does_not_stop_profiles(app, db, games, saved_ai_setup, synced_games, fake_ai, fake_steam):
    fake_steam.fail_with(500)
    v = publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = profile_reply(loved=[{"text": "ok", "tag": "stated", "reviews": [v.id], "games": [10]}])
    run = refresh(app, db)
    assert run.steam_summary == "Steam didn't respond. Try again in a minute." and run.profiles_updated == 1

def test_batches_of_twenty(app, db, saved_ai_setup, fake_ai):
    for appid in range(100, 125):
        db.add(Game(appid=appid, name=f"Sample {appid}", owned=True, playtime_minutes=60, last_played_at=None,
                    source="steam_library", created_at=FIXED_NOW))
    db.commit()
    ids = [publish(db, a, Lane.SOLO, ReviewForm(rating=7, text=f"r{a}"), FIXED_NOW + timedelta(seconds=a)).id
           for a in range(100, 125)]
    def reply(prompt):
        cited = min(i for i in ids if re.search(rf"\bR{i}\b", prompt))
        return profile_reply(loved=[{"text": "ok", "tag": "stated", "reviews": [cited], "games": []}])
    fake_ai.replies["sample-local"] = reply
    run = refresh(app, db)
    versions = db.scalars(select(ProfileVersion).order_by(ProfileVersion.version)).all()
    assert [len(latest_sources(db, v)) for v in versions] == [20, 5]
    assert run.profiles_updated == 1 and run.details[0]["versions"] == [1, 2]
```

- `latest_sources` is a one-line helper in the test module that reads `ProfileVersionSource` rows.
- `profiles_updated` counts profiles that gained at least one version, not versions.

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_refresh.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the routine, the prompt, the model and migration `0006`, and register the handler in `create_app`.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: profile refresh routine and prompt`.

---

### Task 6: Profiles pages

**Files:**
- Create: `app/routes/profiles.py`, `app/templates/profiles.html`, `app/templates/profile.html`, `app/templates/partials/profile_lines.html`
- Modify: `app/templates/base.html` (nav), `app/main.py` (router), `app/static/app.css`
- Test: `tests/test_profiles_pages.py`

**Interfaces:**
- Consumes: `list_profiles`, `current_version`, `SECTION_LABELS`, `TAG_LABELS`, `ProfileContent` (Task 4).
- Produces:
  - **Side nav:** "Profiles" (`/profiles`) between Reviews and Settings.
  - **`GET /profiles`:** a card per `ProfileSummary` with its label, then either:
    - `"Version {n} · updated {when}"` and a "View profile" link (a secondary button), or
    - `"No reviews in this lane yet. Profiles fill in after you publish reviews."` for an empty lane, or `"No reviews tagged with this group yet."` for an empty group.
    - The page header holds the refresh status area (Task 7).
  - **`GET /profiles/lane/{lane}` and `GET /profiles/group/{group_id}`:** the profile page.
    - Unknown lane or group gives 404. A missing profile shows the empty-state copy.
    - `?version=n` picks an earlier version. An unknown version gives 404.
    - Layout:
      - title: the label
      - "Version {n} of {count} · updated {when}"
      - a version select of Ember Select options, newest first, that loads `?version=`
      - the four sections in `SECTION_LABELS` order, each line with an Ember Badge for its tag and "From: {game names}"
      - open questions as a plain list
      - a "Changelog" list of every version newest first: `"Version {n} · {when}: {changelog}"`
    - Empty sections are omitted, and the page says "Nothing here yet." when all are empty.
  - **Badges:** Stated is the neutral badge, Observed the info badge and Guess the warning badge (Ember Badge variants in `C:\Users\david\home\Ember\components\src\feedback.tsx` or `data.tsx`). A badge's color is never its only signal, because the badge carries the word.
  - **Text:** all profile text is autoescaped and never passed through `|safe`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_nav_has_profiles(onboarded_client):
    assert 'href="/profiles"' in onboarded_client.get("/").text

def test_list_shows_empty_lanes(onboarded_client):
    text = onboarded_client.get("/profiles").text
    for label in ("Solo", "Two players", "Three to four", "Five or more"):
        assert label in text
    assert "No reviews in this lane yet. Profiles fill in after you publish reviews." in text

def test_profile_page_shows_lines_tags_and_changelog(onboarded_client, db, built_solo_profile):
    text = html.unescape(onboarded_client.get("/profiles/lane/solo").text)
    assert "What you love" in text and "Stated" in text and "From: Sample Quest" in text
    assert "Version 1 · " in text and "Learned from new reviews." in text

def test_versions_are_selectable(onboarded_client, db, built_solo_profile_two_versions):
    assert "Version 1 of 2" in onboarded_client.get("/profiles/lane/solo?version=1").text
    assert onboarded_client.get("/profiles/lane/solo?version=9").status_code == 404

def test_unknown_targets_404(onboarded_client):
    assert onboarded_client.get("/profiles/lane/bogus").status_code == 404
    assert onboarded_client.get("/profiles/group/999").status_code == 404

def test_profile_text_is_escaped(onboarded_client, db, built_solo_profile_with_html):
    text = onboarded_client.get("/profiles/lane/solo").text
    assert "<script>" not in text and "&lt;script&gt;" in text
```

The `built_*` fixtures save versions directly with `save_version`, so these tests need no refresh.

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_profiles_pages.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the routes and templates.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: profiles pages with versions and changelog`.

---

### Task 7: Refresh now and refresh status

**Files:**
- Create: `app/routes/refresh.py`, `app/templates/partials/refresh_status.html`
- Modify: `app/templates/partials/settings_schedule.html`, `app/templates/profiles.html`, `app/templates/profile.html`, `app/main.py`
- Test: `tests/test_refresh_routes.py`

**Interfaces:**
- Consumes: `enqueue_job`, `notify_worker` (Task 3); `RefreshRun`, `Job` (Tasks 3 and 5).
- Produces:
  - `refresh_status(db) -> RefreshStatus(state: Literal["idle", "queued", "running"], job: Job | None, run: RefreshRun | None)`. `job` is the newest refresh job and `run` is its `RefreshRun`, if any.
  - `POST /refresh` calls `enqueue_job(db, "refresh", "user", now)` and `notify_worker(app)`, then returns the status partial (200).
  - `GET /refresh/status?was={state}` returns the status partial.
    - While queued or running, the partial polls itself every 2 s (`hx-get="/refresh/status?was=<state>" hx-trigger="every 2s" hx-swap="outerHTML"`).
    - When `was` was queued or running and the state is now idle, the response sends `HX-Trigger: profiles-refreshed`. The profiles list and the profile page listen for it (`from:body`) and re-fetch their content.
  - Partial copy (exact):
    - Idle, never run: `"No refresh yet. The daily refresh runs at {refresh_time}."`
    - Queued: `"Refresh queued."`
    - Running: `"Refreshing profiles."`
    - Last run done: `"Last refresh {when}: {n} profile updated."` or `"... {n} profiles updated."`.
      - If `profiles_failed > 0`, add `" {m} couldn't be updated."`.
      - Show a details list of failed profiles with their messages.
      - Show `steam_summary` as a caption.
    - Last job failed: `"The last refresh failed {when}. {error}"`.
  - **The button:** "Refresh now" (secondary) sits in the Settings Schedule card and on the profiles list and profile pages. It is disabled while a refresh is queued or running.

- [ ] **Step 1: Write the failing tests.**

```python
def test_refresh_now_enqueues_one_job(onboarded_client, db):  # Review focus 3
    onboarded_client.post("/refresh"); r = onboarded_client.post("/refresh")
    assert r.status_code == 200 and "Refresh queued." in r.text
    assert db.scalar(select(func.count()).select_from(Job)) == 1

def test_status_polls_while_pending_and_triggers_when_done(onboarded_client, app, db, games, saved_ai_setup, fake_ai):
    v = publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = profile_reply(loved=[{"text": "ok", "tag": "stated", "reviews": [v.id], "games": [10]}])
    assert 'hx-trigger="every 2s"' in onboarded_client.post("/refresh").text
    run_pending_jobs(app)
    r = onboarded_client.get("/refresh/status?was=queued")
    assert r.headers.get("HX-Trigger") == "profiles-refreshed" and "1 profile updated." in html.unescape(r.text)
    assert 'every 2s' not in r.text

def test_failed_profiles_are_explained(onboarded_client, app, db, games, saved_ai_setup, fake_ai):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="ok"), FIXED_NOW)
    fake_ai.replies["sample-local"] = "nope"
    onboarded_client.post("/refresh"); run_pending_jobs(app)
    text = html.unescape(onboarded_client.get("/refresh/status").text)
    assert "1 couldn't be updated." in text and "The model's reply wasn't in the expected format." in text

def test_interrupted_job_message_shows(onboarded_client, db):  # Review focus 2
    job = enqueue_job(db, "refresh", "user", FIXED_NOW)
    db.execute(update(Job).where(Job.id == job.id).values(status="running")); db.commit()
    recover_interrupted_jobs(db, FIXED_NOW)
    assert "Interrupted by a restart." in onboarded_client.get("/refresh/status").text

def test_refresh_now_on_settings_and_profiles(onboarded_client):
    for path in ("/settings", "/profiles", "/profiles/lane/solo"):
        assert "Refresh now" in onboarded_client.get(path).text

def test_refresh_needs_login(app):
    assert TestClient(app).post("/refresh", follow_redirects=False).status_code == 303
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_refresh_routes.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the routes, the partial and the placements.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Check it in the browser.** The controller does this step after review.
  1. Start a dev server with the sample data.
  2. Set Everyday tasks to a reachable model, or seed profile versions directly.
  3. Press "Refresh now" and watch the status update.
  4. Open a profile and switch versions.
  5. Check the light theme.

- [ ] **Step 6: Commit:** `feat: refresh now and live refresh status`.

---

### Task 8: README and end-to-end check

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the README.**
  - **What works today:** add profiles and the daily refresh.
  - **New section, "Profiles and the daily refresh":**
    - Profiles build from published reviews.
    - The refresh runs daily at the time set in Settings, and on "Refresh now".
    - Each refresh asks the Everyday tasks model once for every 20 new reviews for each profile that has new reviews. Cost depends on the provider you chose.
    - Reviews go to that provider. Change line 3's wording so it no longer says your reviews only stay with you; say they are stored on your computer and sent to the AI provider you pick.

- [ ] **Step 2: Run the full suite.** Run `.venv/Scripts/python.exe -m pytest -W error`. Expected: all pass.

- [ ] **Step 3: Check Docker.**
  1. Run `docker compose build`, then `docker compose up -d`.
  2. `/healthz` returns `ok`.
  3. `docker compose logs` shows no errors from the worker and scheduler threads at startup.
  4. Run `docker compose down`, then delete the `./data` this check created.

- [ ] **Step 4: Commit:** `docs: README for profiles and the daily refresh`.
