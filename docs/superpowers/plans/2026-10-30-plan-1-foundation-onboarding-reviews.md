# Game Taste v1, plan 1 of 3: foundation, onboarding, library and reviews

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user runs `docker compose up -d`, finishes all setup in the browser, imports their Steam library, and writes lane-by-lane reviews with drafts, tags and version history. Every piece of data the design says to capture for these areas is stored from the first day.

**Architecture:** One FastAPI process serves Jinja2 pages styled with a vendored copy of Ember, and uses htmx for dialogs and partial refreshes. SQLite holds everything through SQLAlchemy 2, with Alembic migrations run at startup. External services (Steam, AI providers, Tavily) sit behind small service modules that the app factory receives as injectable `Services`, so tests run with fakes and no network.

**Tech stack:**
- **App:** Python 3.12, FastAPI, uvicorn, Jinja2, python-multipart, htmx 2 (one vendored JS file).
- **Data:** SQLAlchemy 2, Alembic.
- **Security:** cryptography (Fernet), argon2-cffi, itsdangerous (sessions).
- **External services:** httpx, openai, anthropic, google-genai, tavily-python. tzdata.
- **Tests:** pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-game-taste-v1-design.md`. Read it before starting any task.

**Later plans:**
- Plan 2 will add profiles and the scheduled refresh.
- Plan 3 will add research, the Recommendations queue and the recommender score.
- This plan leaves Profiles, Recommendations and Research out of the side nav, and they're not built here.

## Global constraints

Every task's requirements include these.

**Stack and runtime**
- Python 3.12. FastAPI with server-rendered Jinja2 templates and htmx. No JavaScript build step, no single-page app, no Node.
- SQLite through SQLAlchemy 2. Alembic migrations run automatically at startup. The engine uses `check_same_thread=False`, and `PRAGMA foreign_keys=ON` runs on every connection.
- One container. All state lives in one data folder: `/data` in the container, `./data` on the host. The host port is 8420, and the container listens on 8000.

**Secrets and login**
- Secrets are encrypted with Fernet. The key lives in `<data>/secret.key` and the session secret in `<data>/session.secret`.
- The password is hashed with argon2.
- Stored keys are never shown back in full. Settings shows "Saved key ending in `abcd`", and a blank field keeps the stored key.

**Data rules**
- The user's words are stored exactly as written. Review text and group text are never trimmed or altered.
- Never delete data the user entered. When something changes state, add a record of the change rather than overwriting it.
- Times are stored in UTC and shown in the user's `timezone` setting.
- Model names are plain text. Model lists are never fetched.

**Nothing tailored to the owner**
- No preloaded data and no built-in people or group names.
- Tests and fixtures use sample data only: games "Sample Quest" (appid 10), "Test Tactics" (appid 20) and "Demo Dungeon" (appid 30), and people "Sam", "Alex" and "Riley".

**Lanes (exact values)**

| Enum value | Label | Head count |
|---|---|---|
| `solo` | Solo | 1 |
| `two` | Two players | 2 |
| `three_four` | Three to four | 3 to 4 |
| `five_plus` | Five or more | 5 and up |

**Ember**
- Vendored at `app/static/ember/`. Templates use Ember's `em-` classes.
- For class names and states, read each component's TSX in `C:\Users\david\home\Ember\components\src\` (for example `em-btn em-btn--primary em-btn--md`, `em-seg__item is-checked`). For rules, read its `README.md`.
- At most one primary button per view.
- Sentence case, with buttons that start with a verb.
- No emoji and no exclamation marks.
- Errors say what happened, then what to do.

**Theme**
- `<html data-theme="dark|light">` comes from the cookie `theme`. Dark is the default, and any other value counts as dark.
- The toggle sets the cookie with `path=/; max-age=31536000; samesite=lax`.

**Code style**
- Each public service function that writes ends with `db.commit()`.
- Routes get "now" from `request.app.state.services.clock()`.
- Tests are pytest, with every Steam, AI and Tavily call faked. No network in tests.
- App functions and classes never start with `test`/`Test`. pytest would collect them when they're imported into test files, which is why the connection checks are named `check_*`.

## Review focus

These are the failure modes most likely to bite a real user that the design implies but doesn't spell out. Each one has a test in the task that owns the code, marked "(Review focus N)".

1. **Steam failures during import or re-sync:** a private profile, a refused key, an unknown vanity name, a timeout or a 5xx. Each shows a plain message saying what to do, keeps the typed values, and still offers retry or skip. Nothing crashes. (Tasks 8, 9)
2. **A partial AI or Tavily test failure:** each item is reported, nothing is saved, and the typed values stay in the form. Local works with an empty key, and a failing Local test hints at `/v1`. (Tasks 6, 7)
3. **A missing or expired session on an htmx request:** the whole page goes to login through `HX-Redirect`, never a login form inside the dialog. (Task 5)
4. **Leaving and coming back mid-onboarding:** setup resumes at the saved step. Onboarding URLs send a finished user home. The password step can't be run again once a password exists. (Task 5)
5. **Review text with HTML, leading or trailing spaces, line breaks or emoji:** it's stored byte-for-byte, and shown escaped with its line breaks kept. (Tasks 12, 13)

---

## File structure

```
app/
  __init__.py
  config.py            Paths (data-folder file locations)
  main.py              Services, create_app(): migrations, secrets, middleware, routers
  asgi.py              app = create_app(Path(os.environ.get("GAME_TASTE_DATA", "/data")))
  db.py                engine, session factory, run_migrations, get_db
  models.py            all SQLAlchemy tables
  web.py               templates, render(), redirect(), Jinja filters when/hours/lane_label
  auth.py              auth and onboarding guard middleware, onboarding step helpers
  migrations/          Alembic env.py, script.py.mako, versions/
  services/
    secrets.py         Vault, key and session-secret files, password hashing
    settings.py        get/set settings, get/set secrets
    ai.py              ProviderKind, Job, adapters, call_and_log, check_model
    ai_setup.py        parse, test and save the "Connect your AI" form (onboarding and Settings)
    search.py          Tavily client, search_and_log, check_search
    steam.py           SteamClient, SteamError
    library.py         sync_library, set_currently_playing, add_store_game, library queries, artwork_url
    people.py          people and named groups
    tags.py            normalize_tags
    reviews.py         Lane, review forms, drafts, publishing, history, summaries
  routes/
    auth.py, onboarding.py, home.py, library.py, reviews.py, settings.py
  templates/
    base.html, login.html
    onboarding/        password.html, ai.html, steam.html, people.html, tour.html
    home.html, library.html, reviews.html, settings.html, history.html
    partials/          card.html, review_dialog.html, people_options.html, provider_row.html,
                       ai_form.html, ai_results.html, library_row.html, store_results.html
  static/
    ember/             tokens.css, bundle.css, fonts/, icons/, SOURCE.md
    vendor/htmx.min.js
    app.css            small app-specific layout on top of Ember
tests/
  conftest.py, fakes.py, test_*.py
Dockerfile, docker-compose.yml, .dockerignore, .gitignore, pyproject.toml, README.md
```

---

### Task 1: Project skeleton and container

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/config.py`, `app/main.py`, `app/asgi.py`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.gitignore`, `tests/conftest.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Produces:
  - `Paths(data_dir: Path)` with properties `db_path` (`game-taste.db`), `key_path` (`secret.key`), `session_secret_path` (`session.secret`) and `reset_flag_path` (`reset-password`).
  - `@dataclass Services(clock: Callable[[], datetime])`. Later tasks add fields.
  - `default_services() -> Services`, with `clock` returning `datetime.now(timezone.utc)`.
  - `create_app(data_dir: Path, *, services: Services | None = None) -> FastAPI`. It creates `data_dir` if missing and sets `app.state.paths` and `app.state.services`.
  - The route `GET /healthz` returns `ok`.
  - Test fixtures: `FIXED_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)`, plus `services`, `app` (`create_app(tmp_path, services=...)`) and `client` (`TestClient(app)`).

- [ ] **Step 1: Write `pyproject.toml`.**
  - Name `game-taste`, `requires-python = ">=3.12"`.
  - Dependencies: fastapi, uvicorn[standard], jinja2, python-multipart, sqlalchemy>=2, alembic, cryptography, argon2-cffi, itsdangerous, httpx, openai, anthropic, google-genai, tavily-python, tzdata.
  - Optional `dev`: pytest.
  - Setuptools: `packages = find: include app*`, plus package-data `app = ["templates/**/*", "static/**/*", "migrations/**/*", "prompts/**/*"]`.

- [ ] **Step 2: Write the failing test.**

```python
def test_healthz_and_data_dir(tmp_path, services):
    data = tmp_path / "data"
    client = TestClient(create_app(data, services=services))
    assert client.get("/healthz").text == "ok"
    assert data.is_dir()
```

- [ ] **Step 3: Run `pytest tests/test_app.py -v`.** Expected: FAIL (import error).

- [ ] **Step 4: Implement** `Paths`, `Services`, `default_services` and `create_app` in `app/config.py` and `app/main.py`, and `app/asgi.py` as shown in the file structure.

- [ ] **Step 5: Write the container files.**
  - **Dockerfile:** `python:3.12-slim`, `WORKDIR /srv`, `COPY . .`, `pip install --no-cache-dir .`, `ENV GAME_TASTE_DATA=/data`, `EXPOSE 8000`, `CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]`.
  - **docker-compose.yml:**

    ```yaml
    services:
      game-taste:
        build: .
        ports: ["8420:8000"]
        volumes: ["./data:/data"]
        restart: unless-stopped
    ```

  - **.gitignore:** `data/`, `__pycache__/`, `.venv/`, `*.egg-info/`, `.pytest_cache/`.
  - **.dockerignore:** the same entries, plus `.git`.

- [ ] **Step 6: Run `pytest -v`.** Expected: PASS. If Docker is installed, also run `docker compose build`. Expected: the build succeeds.

- [ ] **Step 7: Commit:** `feat: project skeleton, app factory and container`.

---

### Task 2: Database, models and migrations at startup

**Files:**
- Create: `app/db.py`, `app/models.py`, `app/migrations/env.py`, `app/migrations/script.py.mako`, `app/migrations/versions/0001_initial.py`
- Modify: `app/main.py` (run migrations, build the engine and session factory)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `make_engine(db_path: Path) -> Engine`
  - `make_session_factory(engine) -> sessionmaker[Session]`
  - `run_migrations(db_path: Path) -> None`. This builds an Alembic `Config` in code, with `script_location` set to `app/migrations` and `sqlalchemy.url` set to `sqlite:///<db_path>`, then runs `upgrade head`. `env.py` uses `Base.metadata` and `render_as_batch=True`.
  - `get_db(request) -> Iterator[Session]`, a FastAPI dependency using `request.app.state.session_factory`.
  - `create_app` sets `app.state.engine` and `app.state.session_factory`.
  - Fixture `db`: a session from `app.state.session_factory`.
  - Models (`Base = DeclarativeBase`). Timestamps are UTC, columns are not nullable unless noted, and enum-like columns are strings checked in the service layer:

| Table (class) | Columns |
|---|---|
| `setting` (Setting) | `key` str PK, `value` text, `encrypted` bool default False |
| `provider` (Provider) | `kind` str PK, `base_url` str null, `api_key_enc` text null, `updated_at` |
| `job_assignment` (JobAssignment) | `job` str PK, `provider_kind` FK provider.kind, `model` str, `updated_at` |
| `person` (Person) | `id` PK, `name` str, `created_at`; unique index on `lower(name)` |
| `named_group` (NamedGroup) | `id` PK, `name` str, `created_at`; unique index on `lower(name)` |
| `group_member` (GroupMember) | `group_id` FK, `person_id` FK, PK on both |
| `game` (Game) | `appid` int PK, `name`, `owned` bool, `playtime_minutes` int null, `last_played_at` null, `currently_playing` bool default False, `source` str (`steam_library` or `store_search`), `created_at` |
| `playtime_snapshot` (PlaytimeSnapshot) | `id` PK, `appid` FK, `playtime_minutes` int, `last_played_at` null, `taken_at` |
| `currently_playing_change` (CurrentlyPlayingChange) | `id` PK, `appid` FK, `playing` bool, `changed_at` |
| `review` (Review) | `id` PK, `appid` FK, `lane` str, `created_at`; unique (`appid`, `lane`) |
| `review_version` (ReviewVersion) | `id` PK, `review_id` FK, `version` int, `rating` int null, `text` text, `group_text` text, `tags` JSON list[str], `status` null, `verdict` null, `walk_away` null, `session_length` text, `complexity` null, `named_group_id` FK null, `steam_hours` float null, `published_at`; unique (`review_id`, `version`) |
| `review_version_person` (ReviewVersionPerson) | `version_id` FK, `person_id` FK, PK on both |
| `review_draft` (ReviewDraft) | `review_id` PK FK, `rating` null, `text`, `group_text`, `tags_raw`, `status` null, `verdict` null, `walk_away` null, `session_length`, `complexity` null, `named_group_id` null, `people` JSON list[int], `updated_at` |

Allowed values:
- `status`: `finished`, `playing`, `demo`, `dropped`
- `verdict`: `loved`, `liked`, `mixed`, `disliked`
- `walk_away`: `yes`, `partly`, `no`
- `complexity`: `too_light`, `about_right`, `too_much`

- [ ] **Step 1: Write the failing tests.**

```python
PLAN1_TABLES = {"setting", "provider", "job_assignment", "person", "named_group", "group_member",
                "game", "playtime_snapshot", "currently_playing_change", "review", "review_version",
                "review_version_person", "review_draft"}

def test_tables_created_at_startup(app):
    assert PLAN1_TABLES <= set(sqlalchemy.inspect(app.state.engine).get_table_names())

def test_startup_twice_is_safe(tmp_path, services):
    create_app(tmp_path, services=services)
    create_app(tmp_path, services=services)

def test_foreign_keys_enforced(db):
    db.add(PlaytimeSnapshot(appid=999, playtime_minutes=1, taken_at=FIXED_NOW))
    with pytest.raises(IntegrityError):
        db.commit()
```

- [ ] **Step 2: Run `pytest tests/test_db.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** `app/db.py` and `app/models.py`. Generate `0001_initial.py` with `alembic revision --autogenerate -m initial`, using a scratch `alembic.ini` or the in-code config, then rename it. Check that the unique index on `lower(name)` is in the migration, and add it by hand if autogenerate missed it.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: database models and startup migrations`.

---

### Task 3: Secrets and the settings store

**Files:**
- Create: `app/services/__init__.py`, `app/services/secrets.py`, `app/services/settings.py`
- Modify: `app/main.py` (load or create the key and session secret, and set `app.state.vault`)
- Test: `tests/test_secrets.py`

**Interfaces:**
- Produces:
  - `load_or_create_key(path: Path) -> bytes`. Uses `Fernet.generate_key()` on first call, writes the file, and calls `chmod 0o600` (any `OSError` is ignored on Windows).
  - `load_or_create_session_secret(path: Path) -> str`. Uses `secrets.token_urlsafe(32)`.
  - `class Vault(key: bytes)` with `encrypt(plain: str) -> str` and `decrypt(token: str) -> str`.
  - `hash_password(pw: str) -> str` and `verify_password(hashed: str, pw: str) -> bool` (argon2 `PasswordHasher`; returns False on mismatch).
  - `get_setting(db, key: str, default: str | None = None) -> str | None` and `set_setting(db, key: str, value: str) -> None`.
  - `get_secret(db, vault, key: str) -> str | None` and `set_secret(db, vault, key: str, value: str) -> None` (stored with `encrypted=True`).
  - `delete_setting(db, key: str) -> None`.
  - The settings keys used across all three plans:
    - `password_hash`
    - `timezone`
    - `onboarding_step`
    - `steam_api_key` (secret)
    - `steam_id`
    - `store_region` (default `us`)
    - `tavily_api_key` (secret)
    - `refresh_time` (default `04:00`)
    - `score_weight_accept` and `score_weight_outcome` (default `0.5` each)

- [ ] **Step 1: Write the failing tests.**

```python
def test_vault_roundtrip_and_key_reused(tmp_path):
    token = Vault(load_or_create_key(tmp_path / "secret.key")).encrypt("sample-key")
    assert token != "sample-key"
    assert Vault(load_or_create_key(tmp_path / "secret.key")).decrypt(token) == "sample-key"

def test_secret_setting_encrypted_at_rest(app, db):
    set_secret(db, app.state.vault, "tavily_api_key", "tvly-sample")
    row = db.get(Setting, "tavily_api_key")
    assert row.encrypted and "tvly-sample" not in row.value
    assert get_secret(db, app.state.vault, "tavily_api_key") == "tvly-sample"

def test_secrets_survive_restart(tmp_path, services):
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        set_secret(s, app1.state.vault, "steam_api_key", "steam-sample")
    app2 = create_app(tmp_path, services=services)
    with app2.state.session_factory() as s:
        assert get_secret(s, app2.state.vault, "steam_api_key") == "steam-sample"

def test_password_hashing():
    h = hash_password("sample-password")
    assert verify_password(h, "sample-password") and not verify_password(h, "wrong-password")
```

- [ ] **Step 2: Run `pytest tests/test_secrets.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** both modules, and wire the key and session secret into `create_app`.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: encrypted secrets and settings store`.

---

### Task 4: Web shell with Ember, theme and display filters

**Files:**
- Create:
  - `app/static/ember/` (copied in, see Step 3), `app/static/ember/SOURCE.md`
  - `app/static/vendor/htmx.min.js`, `app/static/app.css`
  - `app/web.py`, `app/templates/base.html`
- Modify: `app/main.py` (mount `/static`)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces:
  - `templates: Jinja2Templates` (autoescape on).
  - `render(request, name: str, status_code: int = 200, **ctx) -> HTMLResponse`. It adds:
    - `theme` from the cookie
    - `nav` (the current section, from `ctx` or `None`)
    - `tz` from the `timezone` setting (default `UTC`)
    - `now` from the clock
  - `redirect(request, url: str) -> Response`. Returns 303 normally, or `200` with an `HX-Redirect: url` header when the request has `HX-Request: true`.
  - `when(dt: datetime, now: datetime, tz: str) -> str`, registered as a filter that uses the context's `now` and `tz`.
  - `hours(minutes: int | None) -> str`.
  - `lane_label(lane: str) -> str`.
  - `base.html`:
    - An Ember side nav with "Game Taste" set in `text-subtitle` and the items Home `/`, Library `/library`, Reviews `/reviews` and Settings `/settings`.
    - A theme toggle at the bottom.
    - An empty `<div id="dialog"></div>`, and a toast region `<div id="toasts"></div>`.
    - It loads `tokens.css`, `bundle.css`, `app.css` and `htmx.min.js`.
    - The body sets `hx-headers='{"HX-Request": "true"}'`, which htmx already sends.

- [ ] **Step 1: Write the failing tests.**

```python
def test_static_assets_served(client):
    assert "--ember-500" in client.get("/static/ember/tokens.css").text
    assert client.get("/static/ember/bundle.css").status_code == 200
    assert client.get("/static/vendor/htmx.min.js").status_code == 200

@pytest.mark.parametrize("cookie,expected", [(None, "dark"), ("light", "light"), ("bogus", "dark")])
def test_theme_from_cookie(app, cookie, expected):
    html = render(fake_request(app, cookies={"theme": cookie} if cookie else {}), "base.html").body.decode()
    assert f'data-theme="{expected}"' in html

def test_when_and_hours():
    tz = "America/New_York"
    assert when(FIXED_NOW - timedelta(seconds=30), FIXED_NOW, tz) == "just now"
    assert when(FIXED_NOW - timedelta(minutes=4), FIXED_NOW, tz) == "4 min ago"
    assert when(FIXED_NOW - timedelta(hours=3), FIXED_NOW, tz) == "3 h ago"
    assert when(datetime(2026, 1, 8, 14, 42, tzinfo=timezone.utc), FIXED_NOW, tz) == "Jan 8, 9:42 AM"
    assert when(datetime(2025, 10, 8, 13, 42, tzinfo=timezone.utc), FIXED_NOW, tz) == "Oct 8, 2025, 9:42 AM"
    assert hours(65994) == "1,099.9 h" and hours(600) == "10.0 h" and hours(None) == "0.0 h"
```

`fake_request(app, cookies)` is a test helper in `conftest.py`. It builds a `starlette.requests.Request` with `app` in its scope.

- [ ] **Step 2: Run `pytest tests/test_web.py -v`.** Expected: FAIL.

- [ ] **Step 3: Vendor Ember.** Copy these from `C:\Users\david\home\Ember`, keeping `fonts/` next to `tokens.css` so its relative `url("fonts/...")` paths resolve:
  - `tokens.css`, to `app/static/ember/tokens.css`
  - `components/bundle.css`, to `app/static/ember/bundle.css`
  - `fonts/*`, including `OFL.txt`, to `app/static/ember/fonts/`
  - `assets/Icons/*.svg`, to `app/static/ember/icons/`

  `SOURCE.md` notes where the files came from and the date they were copied.

- [ ] **Step 4: Vendor htmx.** Ask the user for permission before downloading, giving the file name, source and size. Then download htmx 2.0.x `dist/htmx.min.js` from `https://unpkg.com/htmx.org@2/dist/htmx.min.js` into `app/static/vendor/`, and record the exact version in `SOURCE.md`.

- [ ] **Step 5: Implement** `app/web.py` and `base.html`.
  - The theme toggle is a ghost icon button using the `moon` icon, labelled "Switch to light theme" or "Switch to dark theme".
  - Its inline script flips `document.documentElement.dataset.theme` and writes the cookie.
  - `when`: under 1 minute is "just now", under 1 hour is "N min ago", and under 24 hours is "N h ago". After that it's `"Mon D, H:MM AM"` in `tz`, with the year added after the day (`"Oct 8, 2025, 9:42 AM"`) when it isn't `now`'s year.

- [ ] **Step 6: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 7: Commit:** `feat: Ember web shell, theme toggle and display filters`.

---

### Task 5: Login, first-run password, onboarding guard and password reset

**Files:**
- Create: `app/auth.py`, `app/routes/__init__.py`, `app/routes/auth.py`, `app/templates/login.html`, `app/templates/onboarding/password.html`
- Modify: `app/main.py` (SessionMiddleware, guard middleware, router, reset-flag check at startup)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `get_setting`, `set_setting`, `delete_setting`, `hash_password`, `verify_password` (Task 3), and `render`, `redirect` (Task 4).
- Produces:
  - `ONBOARDING_STEPS = ["ai", "steam", "people", "tour", "done"]`
  - `current_step(db) -> str`. Returns `"password"` when there's no `password_hash`, otherwise the `onboarding_step` setting, or `"ai"` if that's unset.
  - `advance_step(db, to: str) -> None`. It only moves forward in `ONBOARDING_STEPS`, so going back to an earlier step and submitting it again never rewinds progress.
  - Session: Starlette `SessionMiddleware` with the session secret, `same_site="lax"` and `max_age=30 days`. A logged-in session has `request.session["user"] = True`.
  - Routes:
    - `GET/POST /onboarding/password`. Form fields `password`, `confirm` and `timezone`. The page fills `timezone` from `Intl.DateTimeFormat().resolvedOptions().timeZone`, and saves `UTC` if it's empty or not in `zoneinfo.available_timezones()`.
    - `GET/POST /login`, with the field `password`.
    - `POST /logout`.
  - Guard middleware rules, applied in order:
    1. `/static/*` and `/healthz` always pass.
    2. If there's no password, `/onboarding/password` passes and everything else redirects there.
    3. Once a password exists, `/onboarding/password` redirects to `/login` when logged out, or `/` when logged in.
    4. When logged out, `/login` passes and everything else redirects to `/login`.
    5. When logged in with a step that isn't `done`, paths outside `/onboarding/` redirect to `/onboarding/<step>`. So does an onboarding page for a step later than the current one. Earlier steps stay reachable, for Back buttons.
    6. When logged in with step `done`, `/onboarding/*` and `/login` redirect to `/`.
    - All redirects go through `redirect()`, so htmx requests get `HX-Redirect`.
  - Reset: at startup, if `paths.reset_flag_path` exists, `create_app` deletes `password_hash` and then the flag file.
  - Fixtures:
    - `logged_in_client(client, db)`: sets the password `sample-password` and posts `/login`. The step is `ai`.
    - `onboarded_client`: like `logged_in_client`, plus `advance_step(db, "done")`.

Copy (exact):
- "Use at least 8 characters."
- "The two passwords don't match."
- "That password didn't match. Try again."

- [ ] **Step 1: Write the failing tests.**

```python
def test_first_visit_goes_to_password_step(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/password"

def test_set_password_logs_in_and_starts_onboarding(client, db):
    r = client.post("/onboarding/password", data={"password": "sample-password", "confirm": "sample-password",
                    "timezone": "America/Chicago"}, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/ai"
    assert get_setting(db, "timezone") == "America/Chicago" and get_setting(db, "onboarding_step") == "ai"
    assert client.get("/library", follow_redirects=False).headers["location"] == "/onboarding/ai"

def test_password_rules(client):
    assert "Use at least 8 characters." in client.post("/onboarding/password", data={"password": "short", "confirm": "short", "timezone": "UTC"}).text
    assert "The two passwords don't match." in client.post("/onboarding/password", data={"password": "sample-password", "confirm": "other-password", "timezone": "UTC"}).text

def test_password_step_locked_once_set(logged_in_client, app):  # Review focus 4
    fresh = TestClient(app)
    assert fresh.get("/onboarding/password", follow_redirects=False).headers["location"] == "/login"

def test_login_wrong_and_right(logged_in_client, app):
    fresh = TestClient(app)
    assert "That password didn't match. Try again." in fresh.post("/login", data={"password": "nope-nope"}).text
    assert fresh.post("/login", data={"password": "sample-password"}, follow_redirects=False).status_code == 303

def test_htmx_without_session_redirects_whole_page(logged_in_client, app):  # Review focus 3
    r = TestClient(app).get("/library", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/login"

def test_onboarding_order_is_enforced(logged_in_client):  # Review focus 4
    assert logged_in_client.get("/onboarding/people", follow_redirects=False).headers["location"] == "/onboarding/ai"

def test_finished_onboarding_goes_home(onboarded_client):  # Review focus 4
    assert onboarded_client.get("/onboarding/ai", follow_redirects=False).headers["location"] == "/"

def test_reset_flag_clears_password_keeps_secrets(tmp_path, services):
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        set_setting(s, "password_hash", hash_password("sample-password"))
        set_secret(s, app1.state.vault, "tavily_api_key", "tvly-sample")
    (tmp_path / "reset-password").touch()
    app2 = create_app(tmp_path, services=services)
    assert not (tmp_path / "reset-password").exists()
    with app2.state.session_factory() as s:
        assert get_setting(s, "password_hash") is None
        assert get_secret(s, app2.state.vault, "tavily_api_key") == "tvly-sample"
    assert TestClient(app2).get("/", follow_redirects=False).headers["location"] == "/onboarding/password"
```

- [ ] **Step 2: Run `pytest tests/test_auth.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** `app/auth.py`, the routes and both templates.
  - The password page is the first-run screen. It shows "Step 1 of 5", its title uses `text-display`, and its single primary button is "Create login".
  - The login page's primary button is "Log in".

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: login, first-run password, onboarding guard and reset flag`.

---

### Task 6: AI providers, Tavily search and call logging

**Files:**
- Create: `app/services/ai.py`, `app/services/search.py`, `app/migrations/versions/0002_call_logs.py`, `tests/fakes.py`
- Modify: `app/models.py` (add two tables), `app/main.py` (add `Services` fields)
- Test: `tests/test_ai.py`, `tests/test_search.py`

**Interfaces:**
- Produces:
  - `class ProviderKind(StrEnum)`: `LOCAL="local"`, `OPENROUTER="openrouter"`, `OPENAI="openai"`, `ANTHROPIC="anthropic"`, `GEMINI="gemini"`.
  - `PROVIDER_LABELS`: `{local: "Local", openrouter: "OpenRouter", openai: "OpenAI", anthropic: "Anthropic", gemini: "Gemini"}`.
  - `class Job(StrEnum)`: `RECOMMENDER="recommender"`, `JUDGE="judge"`, `EVERYDAY="everyday"`.
  - `JOB_LABELS`: `{recommender: "Recommender", judge: "Review judge", everyday: "Everyday tasks"}`.
  - `@dataclass(frozen=True) ProviderConfig(kind: ProviderKind, api_key: str | None, base_url: str | None)`.
  - `@dataclass Completion(text: str, input_tokens: int | None, output_tokens: int | None)`.
  - `class Adapter(Protocol)` with `complete(self, model: str, prompt: str) -> Completion`.
  - Adapters:
    - `OpenAICompatibleAdapter(api_key: str, base_url: str | None)`, exposing `.api_key` and `.base_url`. It calls `client.chat.completions.create(model=..., messages=[{"role": "user", "content": prompt}])`.
    - `AnthropicAdapter(api_key: str)`. It calls `client.messages.create(model=..., max_tokens=MAX_OUTPUT_TOKENS, messages=[...])` and joins the text blocks. Use the `claude-api` skill when writing this adapter.
    - `GeminiAdapter(api_key: str)`. It calls `genai.Client(api_key=...).models.generate_content(model=..., contents=prompt)`, using `.text` and `usage_metadata`.
    - `MAX_OUTPUT_TOKENS = 8192`.
  - `make_adapter(cfg: ProviderConfig) -> Adapter`:
    - OpenRouter uses `base_url="https://openrouter.ai/api/v1"`.
    - Local uses `cfg.base_url.rstrip("/")`, with `api_key or "not-needed"`.
    - OpenAI uses `base_url=None`.
  - `class AIError(Exception)`.
  - `call_and_log(db, cfg, model: str, prompt: str, *, job: Job | None, prompt_name: str, run_ref: str | None = None, adapter_factory=make_adapter) -> Completion`. It always writes an `AiCall` row: the response on success, or the error and then a raised `AIError` on failure.
  - `@dataclass CheckResult(ok: bool, message: str)`.
  - `check_model(db, cfg, model, *, job: Job | None, adapter_factory=make_adapter) -> CheckResult`:
    - The prompt is `"Reply with the single word: ok"` and `prompt_name` is `"connection_test"`.
    - Any non-empty reply passes with `"Connected"`.
    - A failure returns `"Didn't connect: <ExceptionType>: <message, first 200 chars>"`. For Local it adds `" Check the URL. Most local servers end in /v1."`.
  - Search:
    - `@dataclass SearchResult(title: str, url: str, content: str)`.
    - `class SearchClient(Protocol)` with `search(self, query: str, max_results: int) -> dict`.
    - `make_search_client(api_key: str) -> SearchClient`, which wraps `tavily.TavilyClient(api_key).search(query, max_results=...)`.
    - `search_and_log(db, client, query: str, *, max_results: int = 5, run_ref: str | None = None) -> list[SearchResult]`.
    - `check_search(db, client) -> CheckResult`, using the query `"best co-op games on Steam"` with the same "Connected" and "Didn't connect: ..." copy.
  - New tables:
    - `ai_call` (AiCall): `id`, `job` null, `provider_kind`, `model`, `prompt_name`, `prompt_hash` (sha256 hex of the prompt), `request` text, `response` text null, `input_tokens` null, `output_tokens` null, `duration_ms` int, `error` text null, `run_ref` null, `created_at`.
    - `search_call` (SearchCall): `id`, `query`, `raw_results` JSON null, `error` null, `run_ref` null, `created_at`.
  - `Services` gains `adapter_factory: Callable[[ProviderConfig], Adapter] = make_adapter` and `search_factory: Callable[[str], SearchClient] = make_search_client`.
  - `tests/fakes.py`:
    - `FakeAdapterFactory`: callable. Records `.calls` as (kind, model, prompt). `.fail(model, exc)` makes that model raise, and replies otherwise default to `"ok"`.
    - `FakeSearchFactory`: callable. Has `.payload`, a dict shaped like Tavily's `{"results": [{"title", "url", "content", "score"}]}`, and `.fail(exc)`.
    - The fixtures `fake_ai` and `fake_search` return these factories, and the `services` fixture passes them as `adapter_factory` and `search_factory`, so routes use the fakes too.

- [ ] **Step 1: Write the failing tests.**

```python
def test_make_adapter_routes_providers():
    assert make_adapter(ProviderConfig(ProviderKind.OPENROUTER, "or-sample", None)).base_url == "https://openrouter.ai/api/v1"
    local = make_adapter(ProviderConfig(ProviderKind.LOCAL, None, "http://localhost:11434/v1/"))
    assert local.api_key == "not-needed" and local.base_url == "http://localhost:11434/v1"   # Review focus 2
    assert make_adapter(ProviderConfig(ProviderKind.OPENAI, "sk-sample", None)).base_url is None
    assert isinstance(make_adapter(ProviderConfig(ProviderKind.ANTHROPIC, "ant-sample", None)), AnthropicAdapter)
    assert isinstance(make_adapter(ProviderConfig(ProviderKind.GEMINI, "gem-sample", None)), GeminiAdapter)

def test_call_and_log_success_and_failure(db, fake_ai):
    cfg = ProviderConfig(ProviderKind.ANTHROPIC, "ant-sample", None)
    call_and_log(db, cfg, "sample-model", "Say ok", job=Job.JUDGE, prompt_name="connection_test", adapter_factory=fake_ai)
    row = db.scalars(select(AiCall)).one()
    assert row.prompt_hash == hashlib.sha256(b"Say ok").hexdigest() and row.response == "ok" and row.job == "judge" and row.error is None
    fake_ai.fail("broken-model", RuntimeError("model not found"))
    with pytest.raises(AIError):
        call_and_log(db, cfg, "broken-model", "Say ok", job=None, prompt_name="connection_test", adapter_factory=fake_ai)
    assert "model not found" in db.scalars(select(AiCall).order_by(AiCall.id.desc())).first().error

def test_check_model_messages(db, fake_ai):  # Review focus 2
    ok = check_model(db, ProviderConfig(ProviderKind.OPENAI, "sk", None), "sample-model", job=None, adapter_factory=fake_ai)
    assert ok == CheckResult(True, "Connected")
    fake_ai.fail("local-model", ConnectionError("refused"))
    bad = check_model(db, ProviderConfig(ProviderKind.LOCAL, None, "http://localhost:1234"), "local-model", job=None, adapter_factory=fake_ai)
    assert not bad.ok and bad.message.startswith("Didn't connect: ConnectionError: refused") and "Most local servers end in /v1." in bad.message

def test_search_and_log(db, fake_search):
    results = search_and_log(db, fake_search("tvly-sample"), "co-op games")
    assert results[0].url == fake_search.payload["results"][0]["url"]
    assert db.scalars(select(SearchCall)).one().raw_results == fake_search.payload

def test_check_search_failure(db, fake_search):
    fake_search.fail(RuntimeError("invalid key"))
    assert check_search(db, fake_search("bad")) == CheckResult(False, "Didn't connect: RuntimeError: invalid key")
```

- [ ] **Step 2: Run `pytest tests/test_ai.py tests/test_search.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** both modules, the models, the `0002_call_logs` migration (autogenerate, then check it) and the fakes. `call_and_log` times the call with `time.perf_counter()`.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: AI provider adapters, Tavily search and call logging`.

---

### Task 7: Onboarding step 2, "Connect your AI"

**Files:**
- Create: `app/services/ai_setup.py`, `app/routes/onboarding.py`, `app/templates/onboarding/ai.html`, `app/templates/partials/ai_form.html`, `app/templates/partials/provider_row.html`, `app/templates/partials/ai_results.html`
- Modify: `app/main.py` (router)
- Test: `tests/test_onboarding_ai.py`

**Interfaces:**
- Consumes: `ProviderKind`, `Job`, labels, `ProviderConfig`, `check_model`, `check_search`, `make_search_client` (Task 6); `get_secret`, `set_secret` (Task 3); `advance_step` (Task 5).
- Produces:
  - `@dataclass AiSetup(providers: dict[ProviderKind, ProviderConfig], jobs: dict[Job, tuple[ProviderKind, str]], tavily_key: str)`.
  - `stored_ai_setup(db, vault) -> AiSetup` (blank when nothing is stored).
  - `parse_ai_form(form, stored: AiSetup) -> tuple[AiSetup, list[str]]`:
    - Form fields:
      - `providers`, a repeated list of kinds
      - `key_<kind>`
      - `url_local`
      - `job_<job>_provider` and `job_<job>_model`
      - `tavily_key`
    - A blank key or Tavily key keeps the stored value.
    - Errors (exact copy):
      - `"Add a key for {Provider}, or pick another provider for the {Job label}."` when a job's provider isn't added or (for non-Local providers) has no key.
      - `"Enter a model name for the {Job label}."`
      - `"Enter the server URL for Local."`
      - `"Add a Tavily key. All game research uses it."`
  - `run_ai_checks(db, setup: AiSetup, services) -> list[CheckLine]`, where `@dataclass CheckLine(label: str, result: CheckResult)`. It runs one check per job (label `"{Job label}: {model} on {Provider}"`) and one Tavily check (label `"Tavily web search"`).
  - `save_ai_setup(db, vault, setup: AiSetup) -> None`. Upserts `Provider` rows (keys encrypted with the vault), `JobAssignment` rows and `tavily_api_key`.
  - Routes:
    - `GET /onboarding/ai`
    - `GET /onboarding/ai/provider-row?kind=<kind>`, which returns `provider_row.html` for htmx to append
    - `POST /onboarding/ai`:
      - If there are parse errors, re-render with the errors and run no checks.
      - Otherwise run the checks. If all pass, save, advance to `steam` and redirect to `/onboarding/steam`. If any fail, re-render showing every check line and the values just typed, and save nothing.
  - `ai_form.html` is shared with Settings (Task 15). The parameter `mode` is `"onboarding"` or `"settings"`.
    - When a setup is already stored (Settings, or coming back to this step with Back), the form starts from `stored_ai_setup`.
    - Stored key inputs are empty, with the placeholder "Saved key ending in `abcd`".
  - `VALID`, the sample form below, lives in `tests/fakes.py` so Task 15 can reuse it.

- [ ] **Step 1: Write the failing tests.**

```python
# tests/fakes.py
VALID = {"providers": ["anthropic", "local"], "key_anthropic": "ant-sample", "url_local": "http://localhost:1234/v1",
         "key_local": "", "job_recommender_provider": "anthropic", "job_recommender_model": "sample-big",
         "job_judge_provider": "local", "job_judge_model": "sample-local", "job_everyday_provider": "local",
         "job_everyday_model": "sample-local", "tavily_key": "tvly-sample"}

# tests/test_onboarding_ai.py
def test_page_lists_providers_jobs_and_tavily(logged_in_client):
    text = logged_in_client.get("/onboarding/ai").text
    for label in ["Local", "OpenRouter", "OpenAI", "Anthropic", "Gemini", "Recommender", "Review judge", "Everyday tasks", "Tavily"]:
        assert label in text

def test_all_pass_saves_and_advances(logged_in_client, db, app, fake_ai):
    r = logged_in_client.post("/onboarding/ai", data=VALID, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"
    assert app.state.vault.decrypt(db.get(Provider, "anthropic").api_key_enc) == "ant-sample"
    assert db.get(Provider, "local").api_key_enc is None and db.get(Provider, "local").base_url == "http://localhost:1234/v1"
    assert (db.get(JobAssignment, "judge").provider_kind, db.get(JobAssignment, "judge").model) == ("local", "sample-local")
    assert get_secret(db, app.state.vault, "tavily_api_key") == "tvly-sample" and len(fake_ai.calls) == 3

def test_one_failure_saves_nothing_and_keeps_values(logged_in_client, db, fake_ai):  # Review focus 2
    fake_ai.fail("sample-big", RuntimeError("unknown model"))
    text = logged_in_client.post("/onboarding/ai", data=VALID).text
    assert "Didn't connect: RuntimeError: unknown model" in text and "Connected" in text and 'value="sample-big"' in text
    assert db.get(Provider, "anthropic") is None and get_setting(db, "onboarding_step") == "ai"

def test_job_without_provider_key_is_caught_before_testing(logged_in_client, fake_ai):
    data = {**VALID, "job_judge_provider": "gemini"}
    text = logged_in_client.post("/onboarding/ai", data=data).text
    assert "Add a key for Gemini, or pick another provider for the Review judge." in text and fake_ai.calls == []

def test_provider_row_partial(logged_in_client):
    assert 'name="key_gemini"' in logged_in_client.get("/onboarding/ai/provider-row?kind=gemini").text
```

- [ ] **Step 2: Run `pytest tests/test_onboarding_ai.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** `ai_setup.py`, the routes and the templates, following the approved mockup.
  - Section 1, "Add the providers you'll use": an "Add a provider" select and button that htmx-append a row. A first visit starts with no rows. A re-render after a failed submit, or a stored setup, starts with a row for each provider in it.
  - Section 2: one row per job, with a provider select listing all five kinds and a model text input.
  - Section 3: the Tavily key, with a "Where do I get a key?" link to `https://app.tavily.com`.
  - The primary button is "Test and continue". This step has no Back button, because step 1 can't be revisited once a password exists.
  - Show the step indicator "Step 2 of 5".
  - Steps 3, 4 and 5 each have a Back button to the previous step's page.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: onboarding step 2, connect your AI`.

---

### Task 8: Steam client and library sync

**Files:**
- Create: `app/services/steam.py`, `app/services/library.py`
- Modify: `app/main.py` (add `Services.steam_http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=20))`), `tests/fakes.py` (`FakeSteam`), `tests/conftest.py` (the `services` fixture passes `httpx.Client(transport=httpx.MockTransport(fake_steam.handler))`, and `steam_client = SteamClient(services.steam_http)`)
- Test: `tests/test_steam.py`, `tests/test_library_sync.py`

**Interfaces:**
- Produces:
  - `class SteamError(Exception)`. Its message is shown to the user as is.
  - `@dataclass OwnedGame(appid: int, name: str, playtime_minutes: int, last_played_at: datetime | None)`. `rtime_last_played` of 0 becomes None.
  - `@dataclass StoreHit(appid: int, name: str)`.
  - `class SteamClient(http: httpx.Client)`:
    - `resolve_steamid(api_key: str, raw: str) -> str`. It ports `resolve_steamid` from the prototype `C:\Users\david\home\claude_gaming\scripts\steam_library.py`: a 17-digit ID, a `/profiles/<id>` URL, or a vanity name or `/id/<name>` URL resolved with `ISteamUser/ResolveVanityURL/v1/`.
    - `owned_games(api_key: str, steamid: str) -> list[OwnedGame]`, using `IPlayerService/GetOwnedGames/v1/` with `include_appinfo=1` and `include_played_free_games=1`.
    - `store_search(term: str, region: str) -> list[StoreHit]`, using `https://store.steampowered.com/api/storesearch/?term=&cc=&l=english` and its `items[].id` and `.name`.
  - `SteamError` messages (exact):
    - HTTP 401 or 403: `"Steam refused the API key. Check the key and try again."`
    - No games: `"Steam returned no games. In Steam, open your profile's Privacy Settings, set Game details to Public, then try again."`
    - Unknown vanity name: `"Couldn't find a Steam profile called '{name}'. Paste your profile link or SteamID64 and try again."`
    - Timeout, connection error or 5xx: `"Steam didn't respond. Try again in a minute."`
  - `@dataclass SyncResult(added: int, updated: int, total: int)`.
  - `sync_library(db, steam: SteamClient, api_key: str, steamid: str, now: datetime) -> SyncResult`:
    - It upserts `Game` rows (`owned=True`, `source="steam_library"`, name updated).
    - It writes a `PlaytimeSnapshot` only when a game is new or its `playtime_minutes` or `last_played_at` changed.
    - Games missing from the response are left as they are.
  - `artwork_url(appid: int) -> str` returns `f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"`.
  - `FakeSteam` in `tests/fakes.py` is an `httpx.MockTransport` handler routed by URL path. It has settable `.owned`, `.vanity` and `.store_hits`, and `.fail_with(status_or_exception)`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_resolve_steamid_forms(steam_client, fake_steam):
    fake_steam.vanity = {"sampleuser": "76561198000000002"}
    assert steam_client.resolve_steamid("k", "https://steamcommunity.com/profiles/76561198000000001/") == "76561198000000001"
    assert steam_client.resolve_steamid("k", "76561198000000001") == "76561198000000001"
    assert steam_client.resolve_steamid("k", "https://steamcommunity.com/id/sampleuser/") == "76561198000000002"
    with pytest.raises(SteamError, match="Couldn't find a Steam profile called 'nobody'"):
        steam_client.resolve_steamid("k", "nobody")

@pytest.mark.parametrize("failure,message", [
    (403, "Steam refused the API key. Check the key and try again."),
    (500, "Steam didn't respond. Try again in a minute."),
    (httpx.ConnectTimeout("slow"), "Steam didn't respond. Try again in a minute."),
])
def test_owned_games_failures(steam_client, fake_steam, failure, message):  # Review focus 1
    fake_steam.fail_with(failure)
    with pytest.raises(SteamError, match=re.escape(message)):
        steam_client.owned_games("k", "76561198000000001")

def test_private_profile_message(steam_client, fake_steam):  # Review focus 1
    fake_steam.owned = []
    with pytest.raises(SteamError, match="set Game details to Public"):
        steam_client.owned_games("k", "76561198000000001")

def test_sync_snapshots_only_on_change(db, steam_client, fake_steam):
    fake_steam.owned = [{"appid": 10, "name": "Sample Quest", "playtime_forever": 600, "rtime_last_played": 1768000000},
                        {"appid": 20, "name": "Test Tactics", "playtime_forever": 120, "rtime_last_played": 0}]
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW) == SyncResult(added=2, updated=0, total=2)
    assert sync_library(db, steam_client, "k", "id", FIXED_NOW).added == 0
    assert db.scalar(select(func.count()).select_from(PlaytimeSnapshot)) == 2
    fake_steam.owned[0]["playtime_forever"] = 660
    sync_library(db, steam_client, "k", "id", FIXED_NOW)
    assert db.scalar(select(func.count()).select_from(PlaytimeSnapshot)) == 3 and db.get(Game, 10).playtime_minutes == 660
    assert db.get(Game, 20).last_played_at is None

def test_store_search(steam_client, fake_steam):
    fake_steam.store_hits = [{"id": 30, "name": "Demo Dungeon"}]
    assert steam_client.store_search("demo", "us") == [StoreHit(30, "Demo Dungeon")]
```

- [ ] **Step 2: Run `pytest tests/test_steam.py tests/test_library_sync.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** `steam.py`, `sync_library` and `artwork_url` in `library.py`, and `FakeSteam`. Add the fixtures `fake_steam` and `steam_client` to `conftest.py`.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: Steam client and library sync with playtime snapshots`.

---

### Task 9: Onboarding step 3, "Connect Steam"

**Files:**
- Create: `app/templates/onboarding/steam.html`
- Modify: `app/routes/onboarding.py`
- Test: `tests/test_onboarding_steam.py`

**Interfaces:**
- Consumes: `SteamClient`, `SteamError`, `sync_library` (Task 8); `set_secret`, `set_setting` (Task 3); `advance_step` (Task 5).
- Produces:
  - `connect_steam(db, vault, steam: SteamClient, api_key: str, profile: str, now) -> SyncResult`. It resolves the profile, syncs, then saves `steam_api_key` (secret) and the resolved `steam_id`. It raises `SteamError` before saving anything. Task 15 reuses it.
  - Routes:
    - `GET /onboarding/steam`
    - `POST /onboarding/steam` with fields `steam_key` and `steam_profile`. On success it redirects to `/onboarding/people`. On `SteamError` it shows the message with the typed values.
    - `POST /onboarding/steam/skip`, which redirects to `/onboarding/people`.
  - The page shows "Step 3 of 5". The primary button is "Import my library", and the secondary button is "Skip for now". A "Where do I get a key?" link points to `https://steamcommunity.com/dev/apikey`. A short note says Game details must be public.

- [ ] **Step 1: Write the failing tests.**

```python
def test_import_success(client_at_steam_step, db, app, fake_steam):
    fake_steam.owned = [{"appid": 10, "name": "Sample Quest", "playtime_forever": 600, "rtime_last_played": 1768000000}]
    r = client_at_steam_step.post("/onboarding/steam", data={"steam_key": "steam-sample",
                                  "steam_profile": "76561198000000001"}, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/people"
    assert get_secret(db, app.state.vault, "steam_api_key") == "steam-sample" and get_setting(db, "steam_id") == "76561198000000001"
    assert db.get(Game, 10).owned

def test_import_failure_keeps_values_and_offers_skip(client_at_steam_step, db, fake_steam):  # Review focus 1
    fake_steam.owned = []
    text = client_at_steam_step.post("/onboarding/steam", data={"steam_key": "steam-sample", "steam_profile": "76561198000000001"}).text
    assert "set Game details to Public" in text and 'value="76561198000000001"' in text and "Skip for now" in text
    assert get_setting(db, "onboarding_step") == "steam" and get_setting(db, "steam_id") is None

def test_skip(client_at_steam_step, db):
    assert client_at_steam_step.post("/onboarding/steam/skip", follow_redirects=False).headers["location"] == "/onboarding/people"
    assert get_setting(db, "steam_api_key") is None
```

The fixture `client_at_steam_step` is `logged_in_client` with `advance_step(db, "steam")`.

- [ ] **Step 2: Run `pytest tests/test_onboarding_steam.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** `connect_steam` (in `library.py`), the routes and the template.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: onboarding step 3, connect Steam`.

---

### Task 10: Library page

**Files:**
- Create: `app/routes/library.py`, `app/templates/library.html`, `app/templates/partials/library_row.html`, `app/templates/partials/store_results.html`
- Modify: `app/services/library.py`, `app/main.py` (router)
- Test: `tests/test_library_page.py`

**Interfaces:**
- Consumes: `sync_library`, `SteamClient`, `artwork_url` (Task 8); `get_secret`, `get_setting` (Task 3).
- Produces:
  - `list_library(db, q: str = "", sort: str = "hours") -> list[Game]`.
    - It lists owned games, plus games that are currently playing or have any review.
    - `sort` is `hours` (playtime descending, None last), `last_played` (descending, None last) or `name`.
    - `q` is a case-insensitive substring match on the name.
  - `set_currently_playing(db, appid: int, playing: bool, now) -> bool`. It returns False and writes nothing when the state is unchanged. Otherwise it sets `Game.currently_playing` and adds a `CurrentlyPlayingChange`. Plan 3 extends it with lane effects.
  - `add_store_game(db, appid: int, name: str, now) -> Game`. It's idempotent, creating the game with `owned=False` and `source="store_search"` if it's absent.
  - Routes:
    - `GET /library?q=&sort=`
    - `POST /library/sync`. It re-renders the list with a toast `"Library synced: {added} new, {total} total."`, or with the `SteamError` message.
    - `POST /games/{appid}/playing` with field `playing`, `"1"` or `"0"`. It returns `library_row.html` and the header `HX-Trigger: card-updated-{appid}`.
    - `GET /library/search?term=`, which returns `store_results.html`.
    - `POST /library/games` with fields `appid` and `name`. It adds the game and re-renders the list.
  - Each row has artwork, name, `hours` and `when(last_played_at)` (or "Never played"), an Ember switch labelled "Currently playing", and a "Write a review" button that opens the dialog (Task 13: `hx-get="/games/{appid}/review"`).
  - When Steam isn't connected, the page shows "Connect Steam in Settings to import your library." and still offers "Add a game".

- [ ] **Step 1: Write the failing tests.**

```python
def test_list_sort_and_search(onboarded_client, synced_games):
    text = onboarded_client.get("/library").text
    assert text.index("Sample Quest") < text.index("Test Tactics")          # 600 min before 120 min
    assert "Test Tactics" not in onboarded_client.get("/library?q=quest").text

def test_currently_playing_records_changes(onboarded_client, db, synced_games):
    r = onboarded_client.post("/games/10/playing", data={"playing": "1"})
    assert r.headers["HX-Trigger"] == "card-updated-10" and db.get(Game, 10).currently_playing
    onboarded_client.post("/games/10/playing", data={"playing": "1"})
    onboarded_client.post("/games/10/playing", data={"playing": "0"})
    assert [c.playing for c in db.scalars(select(CurrentlyPlayingChange).order_by(CurrentlyPlayingChange.id))] == [True, False]

def test_resync_failure_shows_message(onboarded_client, synced_games, fake_steam):  # Review focus 1
    fake_steam.fail_with(httpx.ConnectTimeout("slow"))
    assert "Steam didn't respond. Try again in a minute." in onboarded_client.post("/library/sync").text

def test_add_game_from_store(onboarded_client, db, fake_steam):
    fake_steam.store_hits = [{"id": 30, "name": "Demo Dungeon"}]
    assert "Demo Dungeon" in onboarded_client.get("/library/search?term=demo").text
    onboarded_client.post("/library/games", data={"appid": "30", "name": "Demo Dungeon"})
    assert db.get(Game, 30).source == "store_search" and "Demo Dungeon" in onboarded_client.get("/library").text

def test_without_steam(onboarded_client):
    assert "Connect Steam in Settings to import your library." in onboarded_client.get("/library").text
```

The fixture `synced_games` saves a Steam key and ID and runs `sync_library` with "Sample Quest" (600 min) and "Test Tactics" (120 min).

- [ ] **Step 2: Run `pytest tests/test_library_page.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the service functions, routes and templates.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: library page with currently playing switch, re-sync and add a game`.

---

### Task 11: People and named groups, onboarding step 4, the tour and finishing

**Files:**
- Create: `app/services/people.py`, `app/templates/onboarding/people.html`, `app/templates/onboarding/tour.html`
- Modify: `app/routes/onboarding.py`
- Test: `tests/test_people.py`, `tests/test_onboarding_finish.py`

**Interfaces:**
- Produces:
  - `class PeopleError(Exception)`.
  - `add_person(db, name: str, now) -> Person`. It strips the name.
    - Empty: `"Enter a name."`
    - A case-insensitive duplicate: `"You already have someone named {existing.name}."`
  - `rename_person(db, person_id: int, name: str) -> Person`, with the same rules.
  - `add_group(db, name: str, member_ids: list[int], now) -> NamedGroup`. A duplicate gives `"You already have a group named {existing.name}."`.
  - `rename_group(db, group_id: int, name: str) -> NamedGroup`.
  - `set_group_members(db, group_id: int, member_ids: list[int]) -> None`.
  - `list_people(db) -> list[Person]` (by name).
  - `list_groups(db) -> list[tuple[NamedGroup, list[Person]]]`.
  - `group_member_ids(db, group_id: int) -> list[int]`.
  - Routes:
    - `GET /onboarding/people`
    - `POST /onboarding/people/person` (field `name`) and `POST /onboarding/people/group` (fields `name` and repeated `members`). Both re-render the page section, with any error shown.
    - `POST /onboarding/people/done`, which advances to `tour`.
    - `GET /onboarding/tour?page=1..4`
    - `POST /onboarding/finish`, which advances to `done` and redirects to `/`.
  - The people page shows "Step 4 of 5", with the buttons "Continue" (primary) and "Skip for now". It ships with no built-in names.
  - The tour (step 5) is four short screens with Back and Next. The content is the builder's call, covering the loop: review, profiles, queue, decline with a reason. The last screen's primary button is "Go to Home".

- [ ] **Step 1: Write the failing tests.**

```python
def test_people_rules(db):
    sam = add_person(db, "  Sam ", FIXED_NOW)
    assert sam.name == "Sam"
    with pytest.raises(PeopleError, match="You already have someone named Sam."):
        add_person(db, "sam", FIXED_NOW)
    with pytest.raises(PeopleError, match="Enter a name."):
        add_person(db, "   ", FIXED_NOW)
    alex = add_person(db, "Alex", FIXED_NOW)
    group = add_group(db, "Sample Crew", [sam.id, alex.id], FIXED_NOW)
    assert sorted(group_member_ids(db, group.id)) == sorted([sam.id, alex.id])

def test_people_step_and_finish(client_at_people_step, db):
    assert "Sam" in client_at_people_step.post("/onboarding/people/person", data={"name": "Sam"}).text
    assert client_at_people_step.post("/onboarding/people/done", follow_redirects=False).headers["location"] == "/onboarding/tour"
    for page in range(1, 5):
        assert client_at_people_step.get(f"/onboarding/tour?page={page}").status_code == 200
    assert client_at_people_step.post("/onboarding/finish", follow_redirects=False).headers["location"] == "/"
    assert get_setting(db, "onboarding_step") == "done"

def test_no_built_in_names(client_at_people_step, db):
    assert list_people(db) == [] and list_groups(db) == []
```

The fixture `client_at_people_step` is `logged_in_client` with `advance_step(db, "people")`.

- [ ] **Step 2: Run `pytest tests/test_people.py tests/test_onboarding_finish.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the service, routes and templates.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: people and named groups, onboarding tour and finish`.

---

### Task 12: Review rules (lanes, tags, drafts, publishing, history)

**Files:**
- Create: `app/services/tags.py`, `app/services/reviews.py`
- Test: `tests/test_tags.py`, `tests/test_reviews.py`

**Interfaces:**
- Consumes: the models (Task 2) and `group_member_ids` (Task 11).
- Produces:
  - `normalize_tags(raw: str) -> list[str]`:
    1. Split on commas and strip each piece.
    2. Remove leading `#` characters, lowercase, and turn each whitespace run into a single `_`.
    3. Drop empty tags and duplicates, keeping the first occurrence.
  - `class Lane(StrEnum)`: `SOLO="solo"`, `TWO="two"`, `THREE_FOUR="three_four"`, `FIVE_PLUS="five_plus"`.
    - `LANE_LABELS` (see Global constraints).
    - `LANE_MAX = {solo: 1, two: 2, three_four: 4, five_plus: None}`.
  - `@dataclass ReviewForm`, with fields:
    - `rating: int | None = None`
    - `text: str = ""`, `tags_raw: str = ""`, `group_text: str = ""`, `session_length: str = ""`
    - `status`, `verdict`, `walk_away` and `complexity`, each `str | None = None`
    - `people: list[int] = []`, `named_group_id: int | None = None`
  - `class ReviewError(Exception)`.
  - `load_form(db, appid: int, lane: Lane) -> tuple[ReviewForm, Literal["draft", "published", "new"]]`. It returns the draft if one exists, otherwise the latest published version (its tags joined with `", "` into `tags_raw`), otherwise an empty form.
  - `save_draft(db, appid, lane, form, now) -> None`. It upserts `ReviewDraft` and creates the `Review` if needed.
  - `publish(db, appid, lane, form, now) -> ReviewVersion`. It enforces these rules:
    1. Rating must be None or 1 to 10, or it raises `ReviewError("Pick a rating from 1 to 10.")`.
    2. If there's no rating and `text.strip() == ""`, it raises `ReviewError("Add a rating or a few words before publishing.")`.
    3. For Solo, `people`, `named_group_id` and `group_text` are cleared.
    4. `people` becomes `effective_people(...)`.
    5. Tags are `normalize_tags(tags_raw)`.
    6. `version` is the previous maximum plus 1.
    7. `steam_hours` is `game.playtime_minutes / 60` (None if unknown).
    8. Text is stored unchanged.
    9. It deletes the draft, and adds `ReviewVersionPerson` rows.
  - `effective_people(db, people: list[int], named_group_id: int | None) -> list[int]`. It's the union with the group's members, sorted.
  - `head_count_hint(lane: Lane, people_count: int) -> str | None`. When `1 + people_count` is more than `LANE_MAX[lane]`, it returns `"You and {n} others make {n + 1} players. This looks like the {label} lane."`, where label is the lane that head count falls in.
  - `@dataclass LaneState(has_published: bool, has_draft: bool)`, and `lane_states(db, appid) -> dict[Lane, LaneState]`.
  - `default_lane(db, appid) -> Lane`. It returns the lane whose draft `updated_at` or latest `published_at` is most recent, otherwise Solo. Plan 3 adds the In progress queue lane.
  - `@dataclass CardSummary(rating: int | None, tags: list[str], lane: Lane | None, has_draft: bool)`, and `card_summary(db, appid) -> CardSummary`. It uses the latest published version across lanes, with `has_draft` true if any lane has a draft.
  - `history(db, appid, lane) -> list[ReviewVersion]`, newest first.
  - `@dataclass ReviewRow(game: Game, lane: Lane, latest: ReviewVersion | None, has_draft: bool, updated_at: datetime)`, and `list_reviews(db, lane: Lane | None, verdict: str | None) -> list[ReviewRow]`. It returns one row per (game, lane) that has a draft or a published version, filters on the latest version's verdict, and sorts by `updated_at` descending.

- [ ] **Step 1: Write the failing tests.**

```python
def test_normalize_tags():
    assert normalize_tags("#deck builder, Roguelite, number  go up, roguelite, , ##Co Op") == \
        ["deck_builder", "roguelite", "number_go_up", "co_op"]

def test_draft_never_touches_published(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="Good"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=6, text="Rethinking"), FIXED_NOW)
    assert card_summary(db, 10).rating == 8 and card_summary(db, 10).has_draft
    form, source = load_form(db, 10, Lane.SOLO)
    assert (source, form.text) == ("draft", "Rethinking")

def test_publish_versions_and_clears_draft(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="First"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=9, text="Second"), FIXED_NOW)
    v2 = publish(db, 10, Lane.SOLO, ReviewForm(rating=9, text="Second", tags_raw="#Deck Builder"), FIXED_NOW)
    assert v2.version == 2 and v2.tags == ["deck_builder"] and v2.steam_hours == 10.0
    assert load_form(db, 10, Lane.SOLO)[1] == "published"
    assert [v.version for v in history(db, 10, Lane.SOLO)] == [2, 1]

def test_publish_rules(db, games):
    with pytest.raises(ReviewError, match="Add a rating or a few words before publishing."):
        publish(db, 10, Lane.SOLO, ReviewForm(text="   "), FIXED_NOW)
    with pytest.raises(ReviewError, match="Pick a rating from 1 to 10."):
        publish(db, 10, Lane.SOLO, ReviewForm(rating=11), FIXED_NOW)

def test_text_stored_exactly(db, games):  # Review focus 5
    raw = "  <b>Loved</b> it 🎲\n\nsecond line  "
    assert publish(db, 10, Lane.SOLO, ReviewForm(text=raw), FIXED_NOW).text == raw

def test_lanes_are_separate_and_solo_drops_people(db, games, people):
    sam, alex, crew = people
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5, people=[sam.id], group_text="x"), FIXED_NOW)
    v = publish(db, 10, Lane.THREE_FOUR, ReviewForm(rating=9, named_group_id=crew.id, group_text="They loved it"),
                FIXED_NOW + timedelta(minutes=1))
    solo = history(db, 10, Lane.SOLO)[0]
    assert solo.group_text == "" and solo.people == []
    assert sorted(p.person_id for p in v.people) == sorted([sam.id, alex.id])
    set_group_members(db, crew.id, [sam.id])
    assert len(history(db, 10, Lane.THREE_FOUR)[0].people) == 2       # past reviews unchanged
    assert card_summary(db, 10) == CardSummary(9, [], Lane.THREE_FOUR, False)
    assert default_lane(db, 10) == Lane.THREE_FOUR and default_lane(db, 20) == Lane.SOLO

def test_head_count_hint():
    assert head_count_hint(Lane.TWO, 1) is None
    assert head_count_hint(Lane.TWO, 2) == "You and 2 others make 3 players. This looks like the Three to four lane."
    assert head_count_hint(Lane.FIVE_PLUS, 9) is None
```

- The fixture `games` adds two owned games: "Sample Quest" (appid 10, 600 min, last played `FIXED_NOW - 1 day`) and "Test Tactics" (appid 20, 120 min, last played `FIXED_NOW - 2 days`).
- The fixture `people` adds Sam, Alex and a group "Sample Crew" of both.
- `ReviewVersion.people` is a relationship to `ReviewVersionPerson`.

- [ ] **Step 2: Run `pytest tests/test_tags.py tests/test_reviews.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** `tags.py` and `reviews.py`.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: review rules with lanes, drafts, tags and version history`.

---

### Task 13: Home page and the review dialog

**Files:**
- Create: `app/routes/home.py`, `app/routes/reviews.py`, `app/templates/home.html`, `app/templates/partials/card.html`, `app/templates/partials/review_dialog.html`, `app/templates/partials/people_options.html`
- Modify: `app/main.py` (routers), `app/static/app.css` (card grid, tag chips)
- Test: `tests/test_home.py`, `tests/test_review_dialog.py`

**Interfaces:**
- Consumes: everything in Task 12; `list_people` and `list_groups` (Task 11); `artwork_url` (Task 8).
- Produces:
  - `GET /`, which renders "Currently playing" cards.
    - Each card has artwork, name, the rating as `"{n} / 10"` or "Not rated", a lane badge, tag chips and a "Draft saved" badge.
    - Each card has `hx-get="/games/{appid}/card" hx-trigger="card-updated-{appid} from:body" hx-swap="outerHTML"`. Clicking it does `hx-get="/games/{appid}/review" hx-target="#dialog"`.
    - Empty state:
      - Title: "Tell me about a game you played recently".
      - Body: "Games appear here when you switch on Currently playing in your library."
      - If any owned game has `last_played_at`, it lists the five most recent, each with a "Write a review" button.
  - `GET /games/{appid}/card`, which returns `card.html`, or an empty body when the game isn't currently playing (so the card disappears).
  - `GET /games/{appid}/review?lane=`, which returns the dialog for `lane`, or for `default_lane` when it's absent.
    - The dialog title is the game's name.
    - A lane segmented control marks lanes that have a review or draft, using a dot plus the visible text "Draft" or "Reviewed".
    - Fields: a rating row of ten buttons, the review textarea, the tags input with live chips (htmx `GET /tags/preview?raw=` returning chips from `normalize_tags`), and a `<details>` section "More details".
    - The details hold status, verdict and feel. In co-op lanes only, they also hold who played, the named group and "How it went for the group".
    - A hidden `dirty` field is set to `1` by an inline `input` listener.
    - Footer: "Cancel" (closes the dialog client-side by clearing `#dialog`), "Save draft", and "Publish review" (primary).
    - Submit buttons use `hx-disabled-elt="this"`.
  - `POST /games/{appid}/review/{lane}`, with the field `action` set to `draft` or `publish` plus the form fields.
    - On success it returns an empty body with `HX-Trigger: card-updated-{appid}`.
    - On `ReviewError` it re-renders the dialog with the message and the typed values.
  - `POST /games/{appid}/review/{lane}/switch`, with the fields `to`, `dirty`, optional `choice` (`save` or `discard`), and the form fields:
    - `dirty == "0"`: returns the dialog for `to`.
    - `dirty == "1"` with no choice: returns the current lane's dialog with an inline prompt `"You have unsaved changes in {label}."` and the buttons "Save draft and switch", "Discard changes and switch" and "Stay here".
    - `choice == "save"`: saves the draft, then returns the dialog for `to`.
    - `choice == "discard"`: returns the dialog for `to`.
  - `GET /people/options?named_group_id=&people=`, which returns `people_options.html` with the group's members checked, plus the `head_count_hint` for the lane in the query (`lane=`).
  - The review text is shown in the history and Reviews views with Jinja autoescape and `white-space: pre-wrap`. Never use `|safe`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_home_cards(onboarded_client, db, games):
    set_currently_playing(db, 10, True, FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=8, tags_raw="co op"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="wip"), FIXED_NOW)
    text = onboarded_client.get("/").text
    assert "Sample Quest" in text and "8 / 10" in text and "Two players" in text and "co_op" in text and "Draft saved" in text

def test_home_empty_state_lists_recent_games(onboarded_client, games):
    text = onboarded_client.get("/").text
    assert "Tell me about a game you played recently" in text and "Write a review" in text

def test_dialog_opens_on_draft(onboarded_client, db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="Half written"), FIXED_NOW)
    assert "Half written" in onboarded_client.get("/games/10/review").text

def test_publish_closes_and_triggers_card(onboarded_client, db, games):
    r = onboarded_client.post("/games/10/review/solo", data={"action": "publish", "rating": "7", "text": "Fine"})
    assert r.text == "" and r.headers["HX-Trigger"] == "card-updated-10"
    assert history(db, 10, Lane.SOLO)[0].rating == 7

def test_publish_error_keeps_values(onboarded_client, games):
    text = onboarded_client.post("/games/10/review/solo", data={"action": "publish", "text": " ", "tags": "keep me"}).text
    assert "Add a rating or a few words before publishing." in text and 'value="keep me"' in text

def test_lane_switch_prompts_when_dirty(onboarded_client, db, games):
    data = {"to": "two", "dirty": "1", "text": "Unsaved words"}
    assert "You have unsaved changes in Solo." in onboarded_client.post("/games/10/review/solo/switch", data=data).text
    onboarded_client.post("/games/10/review/solo/switch", data={**data, "choice": "save"})
    assert load_form(db, 10, Lane.SOLO)[0].text == "Unsaved words"

def test_solo_hides_coop_fields(onboarded_client, games):
    assert "How it went for the group" not in onboarded_client.get("/games/10/review?lane=solo").text
    assert "How it went for the group" in onboarded_client.get("/games/10/review?lane=three_four").text

def test_html_in_review_is_escaped(onboarded_client, db, games):  # Review focus 5
    publish(db, 10, Lane.SOLO, ReviewForm(text="<script>alert(1)</script>"), FIXED_NOW)
    text = onboarded_client.get("/games/10/review?lane=solo").text
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;" in text
```

- [ ] **Step 2: Run `pytest tests/test_home.py tests/test_review_dialog.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the routes and templates.
  - The rating buttons are an Ember segmented control of 1 to 10 radios named `rating`. Clicking the checked one again clears it.
  - People are checkboxes named `people`, valued with the person id.
  - The form is parsed into `ReviewForm` with one helper, `form_from_request(form) -> ReviewForm`, in `app/routes/reviews.py`.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Check it in a browser.**
  1. Run `uvicorn app.asgi:app --port 8420` with `GAME_TASTE_DATA=./data-dev`.
  2. Go through onboarding with test values. AI tests need a real provider, so use a Local server if one is available, or temporarily seed `job_assignment` rows in `data-dev`.
  3. Write, save as a draft and publish a review in two lanes.
  4. Confirm the card refreshes, the theme toggle persists across reload, and the light theme renders correctly.
  5. Delete `data-dev` afterwards.

- [ ] **Step 6: Commit:** `feat: home cards and lane-by-lane review dialog`.

---

### Task 14: Reviews page and version history

**Files:**
- Create: `app/templates/reviews.html`, `app/templates/history.html`
- Modify: `app/routes/reviews.py`
- Test: `tests/test_reviews_page.py`

**Interfaces:**
- Consumes: `list_reviews`, `history`, `LANE_LABELS` (Task 12).
- Produces:
  - `GET /reviews?lane=&verdict=`, which renders an Ember table with these columns:
    - Game
    - Lane
    - Rating
    - Verdict
    - State: "Draft", "Published", or "Published, draft saved"
    - Updated (`when`)
  - Rows open the dialog for that lane. The lane and verdict filters are selects.
  - Empty state: "No reviews yet. Reviews appear here after you save a draft or publish one."
  - `GET /games/{appid}/review/{lane}/history`, which lists every published version, newest first: version number, `when(published_at)`, rating, tags, and the text (escaped, `pre-wrap`).

- [ ] **Step 1: Write the failing tests.**

```python
def test_reviews_list_and_filters(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked", text="a"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=4, verdict="mixed", text="b"), FIXED_NOW)
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="c"), FIXED_NOW)
    text = onboarded_client.get("/reviews").text
    assert text.count("Sample Quest") == 2 and "Draft" in text
    assert "Test Tactics" not in onboarded_client.get("/reviews?verdict=liked").text
    assert onboarded_client.get("/reviews?lane=solo").text.count("Sample Quest") == 1

def test_history_newest_first(onboarded_client, db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="old words"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(text="new words"), FIXED_NOW + timedelta(hours=1))
    text = onboarded_client.get("/games/10/review/solo/history").text
    assert text.index("new words") < text.index("old words")
```

- [ ] **Step 2: Run `pytest tests/test_reviews_page.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the routes and templates.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: reviews list and version history`.

---

### Task 15: Settings

**Files:**
- Create: `app/routes/settings.py`, `app/templates/settings.html`
- Modify: `app/main.py` (router)
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes:
  - `stored_ai_setup`, `parse_ai_form`, `run_ai_checks`, `save_ai_setup`, `ai_form.html` with `mode="settings"` (Task 7)
  - `connect_steam` (Task 9)
  - the people service (Task 11)
  - `verify_password` and `hash_password` (Task 3)
- Produces `GET /settings`, with these sections in order, each a card with its own form and its own "Save ..." button. There is one primary button per view, so the section buttons are secondary.
  1. **AI and search:** `POST /settings/ai`. It parses with the stored setup, so blank keys keep the stored ones, then runs every test and saves only if all pass. Results show inline.
  2. **Steam:** `POST /settings/steam`, with fields `steam_key` (blank keeps it), `steam_profile` and `store_region`.
     - `store_region` must match `^[a-z]{2}$`. Otherwise: `"Enter a two-letter country code, like us or gb."`
     - Saving runs `connect_steam`. The region is saved even if Steam isn't configured.
  3. **People and groups:** `POST /settings/people/person`, `POST /settings/people/{id}/rename`, `POST /settings/groups/group`, `POST /settings/groups/{id}/rename` and `POST /settings/groups/{id}/members`.
  4. **Schedule:** `POST /settings/schedule`, with fields `refresh_time` and `timezone`.
     - `refresh_time` must match `^([01]\d|2[0-3]):[0-5]\d$`. Otherwise: `"Enter a time like 04:00."`
     - `timezone` must be in `zoneinfo.available_timezones()`. Otherwise: `"Pick a timezone from the list."`
     - It's stored for plan 2's refresh. The "Refresh now" button arrives in plan 2.
  5. **Account:** `POST /settings/password`, with fields `current`, `new` and `confirm`.
     - A wrong current password: `"Your current password didn't match."`
     - Otherwise the same rules as Task 5.
     - "Log out" posts to `/logout`.
- Stored keys are never rendered. Key inputs are empty with the placeholder "Saved key ending in `{last4}`".

- [ ] **Step 1: Write the failing tests.**

```python
def test_ai_settings_blank_key_keeps_stored(onboarded_client, db, app, fake_ai, saved_ai_setup):
    data = {**VALID, "key_anthropic": ""}
    onboarded_client.post("/settings/ai", data=data)
    assert app.state.vault.decrypt(db.get(Provider, "anthropic").api_key_enc) == "ant-sample"
    page = onboarded_client.get("/settings").text
    assert "ant-sample" not in page and "Saved key ending in mple" in page

def test_ai_settings_failure_saves_nothing(onboarded_client, db, fake_ai, saved_ai_setup):  # Review focus 2
    fake_ai.fail("other-model", RuntimeError("unknown model"))
    onboarded_client.post("/settings/ai", data={**VALID, "job_judge_model": "other-model"})
    assert db.get(JobAssignment, "judge").model == "sample-local"

def test_schedule_validation(onboarded_client, db):
    assert "Enter a time like 04:00." in onboarded_client.post("/settings/schedule", data={"refresh_time": "4am", "timezone": "UTC"}).text
    assert "Pick a timezone from the list." in onboarded_client.post("/settings/schedule", data={"refresh_time": "05:30", "timezone": "Mars/Base"}).text
    onboarded_client.post("/settings/schedule", data={"refresh_time": "05:30", "timezone": "Europe/London"})
    assert (get_setting(db, "refresh_time"), get_setting(db, "timezone")) == ("05:30", "Europe/London")

def test_change_password(onboarded_client, db):
    assert "Your current password didn't match." in onboarded_client.post("/settings/password", data={"current": "wrong-pass", "new": "another-pass", "confirm": "another-pass"}).text
    onboarded_client.post("/settings/password", data={"current": "sample-password", "new": "another-pass", "confirm": "another-pass"})
    assert verify_password(get_setting(db, "password_hash"), "another-pass")

def test_region_validation(onboarded_client, db):
    assert "Enter a two-letter country code, like us or gb." in onboarded_client.post("/settings/steam", data={"steam_key": "", "steam_profile": "", "store_region": "USA"}).text

def test_rename_person(onboarded_client, db):
    sam = add_person(db, "Sam", FIXED_NOW)
    onboarded_client.post(f"/settings/people/{sam.id}/rename", data={"name": "Samantha"})
    assert db.get(Person, sam.id).name == "Samantha"
```

The fixture `saved_ai_setup` calls `save_ai_setup` with the setup parsed from `VALID` (in `tests/fakes.py`, Task 7).

- [ ] **Step 2: Run `pytest tests/test_settings.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the routes and template.

- [ ] **Step 4: Run `pytest -v`.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat: settings for AI, Steam, people, schedule and account`.

---

### Task 16: README and end-to-end check in the container

**Files:**
- Create: `README.md`
- Test: manual run in Docker

**Interfaces:** none (documentation and verification).

- [ ] **Step 1: Write `README.md`.**
  - **What it is:** two sentences, linking to `docs/vision.md`.
  - **Quick start:**

    ```bash
    git clone <repo-url> && cd game-taste
    docker compose up -d
    ```

    Then open `http://localhost:8420` and follow setup.
  - **What you'll need during setup:**
    - an AI provider key, or a local server URL
    - a Tavily key, from `https://app.tavily.com`
    - optionally, a Steam Web API key from `https://steamcommunity.com/dev/apikey`, with Game details set to Public
  - **Security note:** until a password is set, whoever opens the page first sets it.
  - **Forgot your password:** the three steps from spec section 5, verbatim.
  - **Backups:** copy the whole `data` folder. It holds the database and the key file, and the key file is needed to read the stored keys.
  - **Upgrading:** `git pull`, then `docker compose up -d --build`.
  - **Development:** `pip install -e .[dev]`, then `pytest`.

- [ ] **Step 2: Run the full test suite.** Run `pytest -v`. Expected: every test passes.

- [ ] **Step 3: Check it end to end in Docker.** This needs Docker. Skip it and say so if Docker isn't available.
  1. Run `docker compose up -d --build`, then open `http://localhost:8420`.
  2. Complete onboarding with real test keys the user supplies in chat for this check. Skip Steam if no key is given.
  3. Publish one review, then reload.
  4. Run `docker compose restart`, then log in again and confirm the review and settings are still there.
  5. Create `data/reset-password`, run `docker compose restart`, and confirm the password step appears and the stored keys still work after setting a new password.
  6. Run `docker compose down`.

- [ ] **Step 4: Commit:** `docs: README with setup, keys, backup and password reset`.
