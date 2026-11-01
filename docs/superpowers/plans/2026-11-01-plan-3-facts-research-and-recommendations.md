# Game Taste v1, plan 3 of 4: game facts, research and the Recommendations queue

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The user presses "Run research". Each lane with a profile then fills toward 20 recommended games that the Recommender proposed and the Review judge accepted. Each game comes with the profile lines it fits, its main risk and dated facts. The Recommendations page lists them by lane and status. Every game fact, proposal, verdict and research choice is stored from the first run.

**Architecture:**
- **Game facts:** the Steam client gains store details, the review summary and patch news. Each fetch is saved as a dated snapshot in `game_fact`, and a snapshot older than 7 days is fetched again. Player counts come from one Tavily search per game, read by the Everyday tasks model.
- **Research:** a `research` job runs rounds. In each round, every lane that is still short builds a pool from three places: earlier candidates that may come back, owned games with little playtime, and web research. The pool gets fresh facts and passes the lane's head-count filter. The Recommender proposes from the pool, the judge rules on each proposal, and the results are saved.
- **Pausing:** a run lives in `research_run`, so a pause falls between jobs. After three rounds the run pauses. "Run one more round" queues one more job, which runs exactly one round.
- **Pages:** a Research page (start, live log, pause controls, past runs) and a Recommendations page, which is read-only in this plan.

**Tech stack:** as plans 1 and 2. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-30-game-taste-v1-design.md`. This plan covers sections 6.7 (layout and what each pick shows), 6.8, 7.2, 7.4 and 8. Plans 1 and 2 (`docs/superpowers/plans/2026-10-30-plan-*.md`) built everything this plan builds on.

**Why there is a plan 4:** plan 2 said plan 3 would add everything left. That is more than one plan should carry, so it is split. **Plan 4** adds:
- **The queue actions:**
  - Start playing, and Decline with a required reason.
  - Played when Currently playing is switched off, with the "How did it go?" offer.
  - The lane question when Currently playing is switched on from Library.
  - The review dialog opening on the lane that is In progress.
  - Home's empty-state line about Start playing.
- **Declines as evidence:** declines feed profiles and the candidate history. A declined game stays out of its lane until a version update.
- **Refresh steps 5 and 6:** fresh facts for games in the inventory and games currently playing, and re-ranking the queue.
- **The recommender score:** its weights go in the settings table, and the score is shown for each run on the Research page.

This plan leaves room for all of that:
- `inventory_status_change.reason` will hold decline reasons.
- `inventory_entry.rank` is the order a re-rank will rewrite.
- `may_return` gets a rule for declined games.
- The research handler has a single place where a run ends.

## Global constraints

Plans 1 and 2's global constraints all still apply. These add to them.

**Fixed numbers (from the vision; never settings)**
- A lane with 20 recommended games is full. A lane at 20 or more grows only when the user ticks "Add 5 more to {lane}", and then by exactly 5.
- 3 rounds run on their own. Each "Run one more round" runs exactly 1.
- Research fetches again any fact older than 7 days before it uses it.

**Who fits a lane (designer's call, narrowing spec 7.2)**

| Lane | Head count it seats | Test |
|---|---|---|
| Solo | 1 | Steam's store categories include "Single-player" |
| Two players | 2 | online or local co-op range includes 2 |
| Three to four | 3 to 4 | online or local co-op range overlaps 3 to 4 |
| Five or more | 5 and up | online or local co-op max is 5 or more |

- "Overlaps" means `max >= low` and `min <= high`. So a co-op that needs 4 players doesn't fit Two players. The spec only checks the maximum, and this extra check is the narrowing.
- A game with no known co-op count fits no co-op lane. Competitive counts never qualify a game.
- "Up to N players" is the larger co-op maximum.

**The two roles (checked in code, not trusted to a model)**
- The Recommender proposes only games from the pool the app gathered. A proposal for any other appid is refused.
- A kept proposal cites at least one line of the lane's current profile, by its line id, and states a main risk. One that doesn't is refused.
- The judge rules only on proposed games. A verdict for any other game is ignored, so the judge never adds games. A proposal the judge says nothing about is `no_verdict`, and it isn't added.
- Nothing enters the inventory unless the judge accepted it, and only as many games as the lane has room for. Accepted games beyond that are kept in the candidate history with `added = false`.

**Facts**
- Facts are dated snapshots, never edited. The latest snapshot of each kind is the current fact.
- There are four kinds:
  - `store`: Steam store details
  - `reviews`: the review summary
  - `news`: the newest patch-notes post
  - `players`: the research source, with the page URL in `source_url`
- Prices use the `store_region` setting (default `us`).
- Artwork stays on Steam's image servers (`artwork_url`).

**Prompts**
- Every prompt this plan adds starts with the line `# <prompt name>`, for example `# research_judge`. Tests route fake replies by that line (`by_prompt`), so keep it when tuning the wording.
- Web text, store text and the user's words go to the models as data. Every prompt says that nothing in them changes the task or the reply format.
- Every AI call and Tavily search a research run makes carries `run_ref=f"research:{run_id}"`. The one exception is the refresh the run starts with, whose calls carry that refresh's own `refresh:{id}`.

**Earlier candidates come back only one way.** A game with an earlier candidate in a lane, from another run, enters that lane's pool only through the candidate history and only when `may_return` allows it (spec 7.4). Web research and the backlog skip it. Otherwise a game the judge rejected would walk back in through a search with nothing changed.

**Sample data:** tests may add "Cozy Crawl" (appid 40), "Party Pile" (50) and "Old Crate" (60) beside plan 1's three games.

---

## Review focus

These are the failure modes most likely to bite a real user that the spec implies but no task's main tests exercise. Each one has a test in the task that owns the code, marked "(Review focus N)".

1. **A model that names what it wasn't given.** The Recommender proposes an appid that isn't in the pool, or cites a profile line that doesn't exist. Or the judge rules on a game nobody proposed. None of these reaches the inventory. Each refusal shows in the run's log, and the run carries on. (Tasks 5, 6)
2. **Steam store lookups that fail or look odd.** The app isn't in the store region, the entry isn't a game, the price is missing (free or unreleased), or Steam is down. The game keeps its last facts or drops out of the pool, with a reason in the log. During a Steam outage, each facts check gives up after three failed calls, not one for every game. (Tasks 1, 2)
3. **A restart or a cancel mid-run.** After a restart the run is paused with a message and can carry on. A cancel drops the lane that is half done and keeps everything the judge accepted before it. No run stays "running" for good. (Tasks 6, 7)
4. **Caps and double requests.** A lane never passes its target, even when the judge accepts more. A lane at exactly 20 counts as full. The run pauses after round 3. Two clicks on Start give one run, and two clicks on Continue give one extra round. (Tasks 4, 6, 7, 8)
5. **Player counts a lane can't use.** A count cited from a page that wasn't in the search results is thrown away. A game with no count is left out of co-op lanes, and so is one whose co-op needs more players than the lane seats. A game that isn't single-player is left out of Solo. (Tasks 3, 6)

---

## File structure (new and changed)

```
app/
  main.py                 registers the research handler; recover_interrupted_research at startup; new routers
  jobs.py                 RESEARCH_KIND
  models.py               GameFact; ResearchRun, ResearchChoice, ResearchEvent, Candidate, InventoryEntry, InventoryStatusChange
  migrations/versions/    0008_game_facts.py, 0009_queue_and_research.py
  services/
    steam.py              StoreDetails, ReviewSummary, PatchNews; app_details, review_summary, patch_news
    game_facts.py         snapshots, freshness, GameFacts, GameVersion, ensure_game
    players.py            player counts (Tavily + Everyday), the lane filter
    queue.py              inventory, candidate history, who may come back, listing
    research_runs.py      run records, targets, events, start/continue/stop/cancel, status for the page
    library.py            add_store_game takes over a game research brought in
  routines/
    refresh.py            run_refresh returns its run id
    recommender.py        the Recommender's and the judge's steps: schemas, checks, calls
    research.py           research_lane (one lane in one round), run_research (the job handler)
  prompts/                player_counts.md, research_plan.md, research_extract.md, research_propose.md, research_judge.md
  routes/                 research.py, recommendations.py
  templates/
    base.html             Recommendations and Research in the side nav
    research.html, recommendations.html
    partials/research_start.html, partials/research_status.html, partials/pick.html
  static/app.css
tests/
  fakes.py                FakeSteam store routes, store_data, facts_for, save_facts, by_prompt, FakeSearchFactory.responder
  conftest.py             solo_version, research_run, research_world
  test_steam_store.py, test_game_facts.py, test_players.py, test_queue.py, test_recommender.py,
  test_research_round.py, test_research_runs.py, test_research_pages.py, test_recommendations_page.py
README.md
```

---

### Task 1: Steam store details, review summary and patch news

**Files:**
- Modify: `app/services/steam.py`, `tests/fakes.py`
- Test: `tests/test_steam_store.py`

**Interfaces:**
- Consumes: `SteamClient._get_json`, `SteamError`, `NO_RESPONSE`, `STORE_BASE` and `API_BASE` (plan 1).
- Produces:

```python
@dataclass(frozen=True)
class StoreDetails:
    appid: int
    type: str                  # Steam's type: "game", "dlc", "demo", ...
    name: str
    short_description: str
    is_free: bool
    price_cents: int | None    # price_overview.final; None when free or not on sale
    currency: str | None
    price_text: str | None     # price_overview.final_formatted, such as "$19.99"
    release_date: str          # Steam's own text, such as "21 Oct, 2024"; "" when missing
    coming_soon: bool
    early_access: bool         # a genre with id "70" or description "Early Access"
    categories: list[str]      # descriptions, such as "Single-player", "Online Co-op"
    genres: list[str]
    developers: list[str]

@dataclass(frozen=True)
class ReviewSummary:
    score_desc: str            # "Very Positive"; Steam says "No user reviews" when there are none
    total_positive: int
    total_negative: int
    total_reviews: int

@dataclass(frozen=True)
class PatchNews:
    latest_at: datetime | None # the newest post tagged "patchnotes", in UTC
    title: str | None
```

  - `parse_app_details(appid: int, payload: dict) -> StoreDetails | None` parses an `api/appdetails` reply. It returns None when `payload[str(appid)]["success"]` is false. A reply with no entry for the appid, no `data` or no `name` raises `SteamError(NO_RESPONSE)`. Missing optional fields get the defaults above.
  - `SteamClient.app_details(appid: int, region: str) -> StoreDetails | None` sends GET `{STORE_BASE}/api/appdetails` with `{"appids": appid, "cc": region, "l": "english"}` and parses it with `parse_app_details`.
  - `SteamClient.review_summary(appid: int) -> ReviewSummary` sends GET `{STORE_BASE}/appreviews/{appid}` with `{"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0}` and reads `query_summary`. `success != 1`, or no summary, raises `NO_RESPONSE`.
  - `SteamClient.patch_news(appid: int) -> PatchNews` sends GET `{API_BASE}/ISteamNews/GetNewsForApp/v2/` with `{"appid": appid, "count": 20, "maxlength": 1, "feeds": "steam_community_announcements"}`. Of the items in `appnews.newsitems` whose `tags` include `"patchnotes"`, it takes the one with the largest `date` (Unix seconds). With none, it returns `PatchNews(None, None)`.
  - None of the three uses the API key.
  - In `tests/fakes.py`:
    - `FakeSteam` gains three dicts, routed by path:
      - `.details: dict[int, dict | None]`: the `data` object. None, or an appid not in the dict, answers `success: false`.
      - `.review_summaries: dict[int, dict]`: the `query_summary`. A missing appid gets `{"review_score_desc": "Very Positive", "total_positive": 90, "total_negative": 10, "total_reviews": 100}`.
      - `.news: dict[int, list[dict]]`: `newsitems`. A missing appid gets `[]`.
    - `store_data(name, *, categories=("Single-player",), early_access=False, is_free=False, price_cents=1999, type="game", coming_soon=False, release_date="1 Jan, 2025") -> dict` builds a `data` object in Steam's shape:
      - `price_overview` with `final_formatted` `f"${cents / 100:.2f}"`, left out when `is_free` or `price_cents is None`
      - categories as `{"id", "description"}` items
      - Early Access as the genre `{"id": "70", "description": "Early Access"}`
      - `developers: ["Sample Studio"]`

- [ ] **Step 1: Write the failing tests.**

```python
def test_app_details_parses_store_data(steam_client, fake_steam):
    fake_steam.details[10] = store_data("Sample Quest", categories=("Single-player", "Online Co-op"),
                                        early_access=True, price_cents=1499)
    d = steam_client.app_details(10, "us")
    assert (d.name, d.type, d.early_access, d.price_cents, d.price_text) == ("Sample Quest", "game", True, 1499, "$14.99")
    assert d.categories == ["Single-player", "Online Co-op"]
    assert fake_steam.requests[-1].url.params["cc"] == "us"

def test_unknown_app_is_none(steam_client):  # Review focus 2
    assert steam_client.app_details(999, "us") is None

def test_free_game_has_no_price(steam_client, fake_steam):  # Review focus 2
    fake_steam.details[20] = store_data("Test Tactics", is_free=True)
    d = steam_client.app_details(20, "us")
    assert d.is_free and d.price_cents is None and d.price_text is None

@pytest.mark.parametrize("failure", [500, httpx.ConnectTimeout("slow")])  # Review focus 2
def test_store_failures_are_steam_errors(steam_client, fake_steam, failure):
    fake_steam.fail_with(failure)
    for call in (lambda: steam_client.app_details(10, "us"), lambda: steam_client.review_summary(10),
                 lambda: steam_client.patch_news(10)):
        with pytest.raises(SteamError, match="Steam didn't respond"):
            call()

def test_review_summary(steam_client, fake_steam):
    fake_steam.review_summaries[10] = {"review_score_desc": "Mostly Positive", "total_positive": 70,
                                       "total_negative": 20, "total_reviews": 90}
    assert steam_client.review_summary(10) == ReviewSummary("Mostly Positive", 70, 20, 90)

def test_patch_news_takes_the_newest_patch_notes(steam_client, fake_steam):
    fake_steam.news[10] = [{"title": "Big sale", "date": 1767900000, "tags": []},
                           {"title": "Patch 1.1", "date": 1767800000, "tags": ["patchnotes"]},
                           {"title": "Patch 1.2", "date": 1767850000, "tags": ["patchnotes", "mod_reviewed"]}]
    assert steam_client.patch_news(10) == PatchNews(datetime.fromtimestamp(1767850000, tz=timezone.utc), "Patch 1.2")

def test_no_patch_notes(steam_client):
    assert steam_client.patch_news(10) == PatchNews(None, None)
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_steam_store.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the three calls, `parse_app_details` and the fakes. Before relying on the response shapes above, check one live response for each endpoint by hand (any well-known appid, in a browser). Don't add that check to the tests, which never touch the network.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: Steam store details, review summary and patch news`.

---

### Task 2: Game facts: snapshots, freshness and versions

**Files:**
- Create: `app/services/game_facts.py`, `app/migrations/versions/0008_game_facts.py`
- Modify: `app/models.py`, `app/services/library.py`, `tests/fakes.py`
- Test: `tests/test_game_facts.py`

**Interfaces:**
- Consumes: Task 1; `Game`, `add_store_game`, `list_library` (plan 1).
- Produces:
  - Model `GameFact` (table `game_fact`), indexed on (`appid`, `kind`, `fetched_at`):
    - `id` PK
    - `appid` FK `game.appid`
    - `kind` str: `store`, `reviews`, `news` or `players`
    - `data` JSON
    - `source_url` Text null: for `players`, the page the counts came from
    - `fetched_at`
  - Constants:
    - `STORE`, `REVIEWS`, `NEWS` and `PLAYERS`, the kinds.
    - `FACTS_MAX_AGE = timedelta(days=7)`.
    - `SOURCE_RESEARCH = "research"`, the `Game.source` of a game research brought in.
    - `MAX_STEAM_FAILURES_IN_A_ROW = 3`.
  - The `data` shapes:
    - `store`: `store_fact_data(details: StoreDetails | None, region: str) -> dict`. It is `asdict(details)` without `appid`, plus `{"available": True, "region": region}`. When `details` is None it is `{"available": False, "region": region}`.
    - `reviews`: `{"score_desc", "total_positive", "total_negative", "total_reviews"}`.
    - `news`: `{"latest_patch_at": ISO text or None, "latest_patch_title": str or None}`.
    - `players`: `{"online_coop", "local_coop", "competitive"}`. Each is `{"min", "max"}` or None.
  - `ensure_game(db, appid: int, name: str, now) -> Game`. It returns a known game as it is. Otherwise it adds one with `owned=False`, `playtime_minutes=None` and `source=SOURCE_RESEARCH`. It commits. Such a game stays out of Library until the user adds it.
  - `add_store_game` (in `library.py`): a known game whose source is `research` becomes `store_search`, so adding it puts it in Library.
  - `@dataclass(frozen=True) Fact(data: dict, fetched_at: datetime, source_url: str | None)`.
  - `@dataclass(frozen=True) GameVersion(early_access: bool | None, latest_patch_at: datetime | None)`:
    - It has `as_json() -> dict` and `from_json(dict) -> GameVersion`.
    - `updated_since(earlier) -> bool` is true when either:
      - both Early Access flags are known and they differ; or
      - this version has a patch date, and the earlier one has none or an older one.
  - `@dataclass(frozen=True) GameFacts(appid, store=None, reviews=None, news=None, players=None)`, each field a `Fact | None`. Properties:
    - From the store fact:
      - `is_game`: the store fact is available and its type is "game".
      - `early_access`, `is_free`, `categories`, `release_date`, `coming_soon`, `short_description`.
      - `price_text`: "Free" when free, else Steam's text, else None.
    - From the reviews fact:
      - `review_text`: `"{score_desc} ({total:,} reviews)"`, or just `score_desc` when the total is 0.
    - From the players fact:
      - `coop_max`: the larger of the online and local co-op maximums, or None.
    - Derived:
      - `version`: a `GameVersion` from the store and news facts.
    - `snapshot() -> dict`: `{kind: {"data", "fetched_at" (ISO), "source_url"}}` for the kinds present.
  - `current_facts(db, appids: Iterable[int]) -> dict[int, GameFacts]` makes one query. Every appid asked for has an entry, which may be empty.
  - `@dataclass FactsRefresh(fetched: int = 0, problems: dict[int, str] = field(default_factory=dict))`.
  - `refresh_store_facts(db, steam: SteamClient, appids: Iterable[int], region: str, now) -> FactsRefresh`:
    - It goes through the appids in order, once each. For each, it fetches every kind among store, reviews and news that is missing or older than `FACTS_MAX_AGE`, and adds a snapshot for each.
    - An unavailable store entry is saved as a `store` fact, and that game's reviews and news are skipped.
    - A `SteamError` records `problems[appid] = str(err)` and keeps the old snapshot current.
    - After `MAX_STEAM_FAILURES_IN_A_ROW` failed calls in a row it stops fetching. Every appid left gets the problem `"Steam didn't respond. Try again in a minute."`.
    - It commits after each game.
  - `save_player_counts(db, appid: int, data: dict, source_url: str | None, now) -> None` adds a `players` snapshot and commits.
  - In `tests/fakes.py`:
    - `facts_for(appid, name, *, players=None, fetched_at=FIXED_NOW, **store_kwargs) -> GameFacts` builds facts in memory:
      - the store fact from `store_fact_data(parse_app_details(appid, {str(appid): {"success": True, "data": store_data(name, **store_kwargs)}}), "us")`
      - a reviews fact for "Very Positive" with 100 reviews
      - a news fact with no patch
      - when `players` is `((min, max) | None, (min, max) | None)` for online and local co-op, a players fact with `source_url` `"https://example.com/players"`
    - `save_facts(db, appid, name, **same)` writes the same snapshots to `game_fact`. It adds the game row first, through `ensure_game`, when there isn't one.
    - `FIXED_NOW` moves to `tests/fakes.py`, and `conftest.py` imports it from there. `T1 = FIXED_NOW - timedelta(days=10)` and `T2 = FIXED_NOW - timedelta(days=1)` sit beside it, as two patch dates.

- [ ] **Step 1: Write the failing tests.**

```python
def test_facts_are_fetched_once_a_week(db, steam_client, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest")
    assert refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW) == FactsRefresh(fetched=3)
    calls = len(fake_steam.requests)
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW + timedelta(days=6))
    assert len(fake_steam.requests) == calls
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW + timedelta(days=7, seconds=1))
    assert len(fake_steam.requests) == calls + 3

def test_snapshots_are_kept_and_the_latest_is_current(db, steam_client, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest", early_access=True)
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW)
    fake_steam.details[10] = store_data("Sample Quest", early_access=False)
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW + timedelta(days=8))
    facts = current_facts(db, [10])[10]
    assert facts.early_access is False and facts.store.fetched_at == FIXED_NOW + timedelta(days=8)
    assert db.scalar(select(func.count()).select_from(GameFact).where(GameFact.kind == "store")) == 2

def test_an_unavailable_app_is_recorded_and_is_not_a_game(db, steam_client, games):  # Review focus 2
    assert refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW).fetched == 1
    assert current_facts(db, [10])[10].is_game is False

def test_a_failure_keeps_the_old_facts_and_says_why(db, steam_client, fake_steam, games):  # Review focus 2
    fake_steam.details[10] = store_data("Sample Quest")
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW)
    fake_steam.fail_with(500)
    result = refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW + timedelta(days=8))
    assert result.problems == {10: "Steam didn't respond. Try again in a minute."}
    assert current_facts(db, [10])[10].store.fetched_at == FIXED_NOW

def test_steam_down_costs_three_calls(db, steam_client, fake_steam):  # Review focus 2
    for appid in range(100, 110):
        ensure_game(db, appid, f"Sample {appid}", FIXED_NOW)
    fake_steam.fail_with(500)
    result = refresh_store_facts(db, steam_client, range(100, 110), "us", FIXED_NOW)
    assert len(fake_steam.requests) == 3 and set(result.problems) == set(range(100, 110))

@pytest.mark.parametrize("earlier,now,updated", [
    (GameVersion(True, None), GameVersion(False, None), True),
    (GameVersion(False, T1), GameVersion(False, T2), True),
    (GameVersion(False, T2), GameVersion(False, T2), False),
    (GameVersion(None, None), GameVersion(False, None), False),
    (GameVersion(False, None), GameVersion(False, T1), True),
])
def test_version_updates(earlier, now, updated):
    assert now.updated_since(earlier) is updated
    assert GameVersion.from_json(now.as_json()) == now

def test_price_and_review_text(db, steam_client, fake_steam, games):
    fake_steam.details[10] = store_data("Sample Quest", price_cents=1999)
    fake_steam.review_summaries[10] = {"review_score_desc": "Very Positive", "total_positive": 1200,
                                       "total_negative": 34, "total_reviews": 1234}
    refresh_store_facts(db, steam_client, [10], "us", FIXED_NOW)
    facts = current_facts(db, [10])[10]
    assert facts.price_text == "$19.99" and facts.review_text == "Very Positive (1,234 reviews)"

def test_research_games_stay_out_of_the_library_until_added(db):
    ensure_game(db, 30, "Demo Dungeon", FIXED_NOW)
    assert list_library(db) == []
    add_store_game(db, 30, "Demo Dungeon", FIXED_NOW)
    assert [g.appid for g in list_library(db)] == [30]
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_game_facts.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the model, migration `0008` (checked against the drift test in `tests/test_db.py`), the service and the fakes. `current_facts` takes the latest row per (appid, kind) in one query, and makes no lookups per game in a loop.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: dated game facts from the Steam store`.

---

### Task 3: Player counts and the lane filter

**Files:**
- Create: `app/services/players.py`, `app/prompts/player_counts.md`
- Modify: `tests/fakes.py`
- Test: `tests/test_players.py`

**Interfaces:**
- Consumes: `complete_json`, `StructuredReplyError`, `Job` (plan 2); `load_prompt`; `search_and_log`, `SearchClient`, `SearchError` (plan 1); Task 2's `GameFacts`, `current_facts`, `save_player_counts`, `FACTS_MAX_AGE`; `Lane`.
- Produces:

```python
class PlayerRange(BaseModel):
    min: int = Field(ge=1, le=1000)
    max: int = Field(ge=1, le=1000)      # a model_validator refuses min > max

class PlayerCounts(BaseModel):
    online_coop: PlayerRange | None = None
    local_coop: PlayerRange | None = None
    competitive: PlayerRange | None = None
    source_url: str | None = None
```

  - `LANE_HEADS: dict[Lane, tuple[int, int | None]]` is `{SOLO: (1, 1), TWO: (2, 2), THREE_FOUR: (3, 4), FIVE_PLUS: (5, None)}`.
  - `PLAYER_QUERY = '"{name}" number of players co-op multiplayer'`.
  - `needs_player_counts(facts: GameFacts, now) -> bool` is true when there is no players fact, or it is older than `FACTS_MAX_AGE`.
  - `lookup_player_counts(db, vault, search: SearchClient, game: Game, *, services, run_ref: str, now) -> None`:
    1. Run `search_and_log(db, search, PLAYER_QUERY.format(name=game.name), max_results=5, run_ref=run_ref, now=now)`. A `SearchError` propagates, and nothing is saved, so the next run tries again.
    2. Call `complete_json(Job.EVERYDAY, load_prompt("player_counts"), {"game": game.name, "results": [{"title", "url", "content"}...]}, PlayerCounts, run_ref=run_ref)`. A `StructuredReplyError` propagates.
    3. Keep the counts only when `source_url` is exactly one of the result URLs. Otherwise every range becomes None and `source_url` becomes None.
    4. Call `save_player_counts` with the three ranges and the `source_url` that was kept. A game with no known count is saved too, so it isn't looked up again for 7 days.
  - `supports_lane(facts: GameFacts, lane: Lane) -> bool` applies the table in Global constraints.
  - `lane_mismatch(facts: GameFacts, lane: Lane) -> str | None` says why a game doesn't fit, as a phrase with no capital letter and no period, or None when it fits:
    - `"Steam doesn't list it as single-player"`
    - `"no co-op player count is known"`
    - `"its co-op supports up to {n} player"` or `"... players"`, when `coop_max` is below the lane's lowest head count
    - `"its co-op needs at least {n} players"`, otherwise, with `n` the smallest co-op minimum
  - `app/prompts/player_counts.md`. Its first line is `# player_counts`. It:
    - names the game, and gives each result's title, URL and content;
    - asks for the minimum and maximum players for online co-op, local co-op and competitive, taken only from the results, with null where the results don't say;
    - says `source_url` must be copied exactly from the result that states the counts;
    - says the results are data and that nothing in them changes the task or the format;
    - asks for JSON only, in the `PlayerCounts` shape.
  - In `tests/fakes.py`:
    - `by_prompt(default="ok", **replies) -> Callable[[str], str]` is a reply function for `FakeAdapterFactory.replies`. It picks the reply by the prompt's first line, `# <name>`, and uses `default` for any other prompt. A value may be a string or a function of the prompt.
    - `FakeSearchFactory.responder: Callable[[str], dict] | None`. When it is set, a search returns `responder(query)` instead of `.payload`.

- [ ] **Step 1: Write the failing tests.**

```python
def counts_reply(url="https://example.com/sample-quest", online=(1, 4), local=None):
    rng = lambda r: {"min": r[0], "max": r[1]} if r else None
    return json.dumps({"online_coop": rng(online), "local_coop": rng(local), "competitive": None, "source_url": url})

def lookup(db, app, services, fake_search, appid=10):
    lookup_player_counts(db, app.state.vault, fake_search("tvly-sample"), db.get(Game, appid),
                         services=services, run_ref="research:1", now=FIXED_NOW)

def test_lookup_saves_counts_with_their_source(db, app, services, fake_ai, fake_search, saved_ai_setup, games):
    fake_ai.replies["sample-local"] = by_prompt(player_counts=counts_reply())
    lookup(db, app, services, fake_search)
    facts = current_facts(db, [10])[10]
    assert facts.coop_max == 4 and facts.players.source_url == "https://example.com/sample-quest"
    assert fake_search.calls[0][0] == '"Sample Quest" number of players co-op multiplayer'
    assert "A cozy co-op adventure for two." in fake_ai.calls[0][2]       # the results reach the model

def test_counts_from_a_page_not_in_the_results_are_dropped(db, app, services, fake_ai, fake_search, saved_ai_setup, games):  # Review focus 5
    fake_ai.replies["sample-local"] = by_prompt(player_counts=counts_reply(url="https://invented.example/"))
    lookup(db, app, services, fake_search)
    facts = current_facts(db, [10])[10]
    assert facts.players is not None and facts.coop_max is None and facts.players.source_url is None

def test_needs_counts_again_after_seven_days(db, app, services, fake_ai, fake_search, saved_ai_setup, games):
    fake_ai.replies["sample-local"] = by_prompt(player_counts=counts_reply())
    lookup(db, app, services, fake_search)
    facts = current_facts(db, [10])[10]
    assert not needs_player_counts(facts, FIXED_NOW + timedelta(days=7))
    assert needs_player_counts(facts, FIXED_NOW + timedelta(days=7, seconds=1))

@pytest.mark.parametrize("lane,online,local,fits", [  # Review focus 5
    (Lane.TWO, (1, 4), None, True),
    (Lane.TWO, (4, 8), None, False),
    (Lane.THREE_FOUR, (1, 2), (1, 3), True),
    (Lane.FIVE_PLUS, (1, 4), None, False),
    (Lane.FIVE_PLUS, (2, 64), None, True),
    (Lane.TWO, None, None, False),
])
def test_supports_lane(lane, online, local, fits):
    assert supports_lane(facts_for(10, "Sample Quest", players=(online, local)), lane) is fits

@pytest.mark.parametrize("lane,players,categories,why", [  # Review focus 5
    (Lane.SOLO, None, ("Online Co-op",), "Steam doesn't list it as single-player"),
    (Lane.TWO, None, ("Single-player",), "no co-op player count is known"),
    (Lane.THREE_FOUR, ((1, 2), None), ("Online Co-op",), "its co-op supports up to 2 players"),
    (Lane.TWO, ((4, 8), None), ("Online Co-op",), "its co-op needs at least 4 players"),
    (Lane.SOLO, None, ("Single-player",), None),
])
def test_lane_mismatch(lane, players, categories, why):
    assert lane_mismatch(facts_for(10, "Sample Quest", players=players, categories=categories), lane) == why
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_players.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the service, the prompt and the fakes.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: player counts from research and the lane filter`.

---

### Task 4: Queue and research records

**Files:**
- Create: `app/services/queue.py`, `app/services/research_runs.py`, `app/migrations/versions/0009_queue_and_research.py`
- Modify: `app/models.py`, `tests/conftest.py`
- Test: `tests/test_queue.py`

**Interfaces:**
- Consumes: Task 2's `GameVersion`, `GameFacts`, `current_facts`, `ensure_game`; `Lane`, `LANE_LABELS`; `Game`, `Review`, `ReviewVersion`, `ProfileVersion`, `AiCall`, `RefreshRun`.
- Produces:
  - Models. New NOT NULL columns get a `server_default`.
    - `ResearchRun` (table `research_run`):
      - `id` PK
      - `status` str: `running`, `paused`, `finished`, `stopped`, `cancelled` or `failed`
      - `targets` JSON: one `LaneTarget.as_json()` per lane, in lane order
      - `rounds_allowed` int (default 3), `rounds_done` int (default 0)
      - `cancel_requested` bool (default false)
      - `refresh_run_id` FK `refresh_run.id` null
      - `error` Text null
      - `created_at`, `finished_at` null
    - `ResearchChoice` (table `research_choice`): the user's choices, never edited.
      - `id` PK, `run_id` FK
      - `choice` str: `start`, `add_five`, `continue`, `stop` or `cancel`
      - `lane` str null (for `add_five`)
      - `made_at`
    - `ResearchEvent` (table `research_event`, indexed on `run_id`): the progress log.
      - `id` PK, `run_id` FK
      - `round` int null, `lane` str null
      - `step` str, `message` Text
      - `created_at`
    - `Candidate` (table `candidate`, indexed on (`lane`, `appid`)): one row per proposal, never edited.
      - `id` PK
      - `run_id` FK, `round` int
      - `lane`, `appid` FK `game.appid`
      - `source` str
      - `fits` JSON: a list of `{"section", "text", "tag", "how"}`
      - `risk` Text
      - `version` JSON (`GameVersion.as_json()`), `facts` JSON (`GameFacts.snapshot()`)
      - `profile_version_id` FK `profile_version.id`
      - `verdict` str: `accepted`, `rejected` or `no_verdict`
      - `reason` Text
      - `list_rejected` bool, `added` bool
      - `recommender_call_id` and `judge_call_id`, each FK `ai_call.id` null
      - `created_at`
    - `InventoryEntry` (table `inventory_entry`), unique on (`lane`, `appid`):
      - `id` PK
      - `lane`, `appid` FK
      - `status` str
      - `rank` int
      - `candidate_id` FK `candidate.id`: the accepted proposal behind its latest Recommended
      - `created_at`
    - `InventoryStatusChange` (table `inventory_status_change`): one per change, never edited.
      - `id` PK, `entry_id` FK
      - `status`
      - `reason` Text null
      - `candidate_id` FK null
      - `changed_at`
  - In `queue.py`:
    - Constants:
      - `TARGET = 20`, `TOP_UP = 5`.
      - Statuses `RECOMMENDED`, `IN_PROGRESS`, `PLAYED` and `DECLINED` (`"recommended"`, ...), with `STATUS_LABELS` `{"recommended": "Recommended", "in_progress": "In progress", "played": "Played", "declined": "Declined"}`.
      - Sources `WEB_RESEARCH`, `CANDIDATE_HISTORY` and `OWNED_BACKLOG`, with `SOURCE_LABELS` `{"web_research": "Found by web research", "candidate_history": "From the candidate history", "owned_backlog": "From your backlog"}`.
      - Verdicts `ACCEPTED`, `REJECTED` and `NO_VERDICT`.
    - `@dataclass JudgedProposal`:
      - `appid`, `source`
      - `fits: list[dict]`, `risk: str`
      - `version: GameVersion`, `facts: dict`
      - `verdict`, `reason`, `list_rejected: bool`
      - `recommender_call_id: int | None`, `judge_call_id: int | None`
    - `save_judged(db, judged: list[JudgedProposal], *, run_id, round, lane, room: int, profile_version_id, now) -> list[Candidate]`:
      - It saves one `Candidate` per item, in order.
      - The first `room` accepted items get `added=True`. Each gets an `InventoryEntry` with status `recommended`, ranked after the lane's current highest rank in order (0 for a lane with none), and an `InventoryStatusChange` that carries the candidate id.
      - An existing entry for the game (plan 4: a declined game that came back) moves back to `recommended` and points at the new candidate.
      - It commits once.
    - `recommended_counts(db) -> dict[Lane, int]` (all four lanes) and `status_counts(db, lane) -> dict[str, int]` (all four statuses).
    - `excluded_appids(db, lane) -> set[int]`: games with an inventory entry in the lane, games currently playing, and games with a published review version in the lane.
    - `rejected_in_run(db, run_id, lane) -> list[Candidate]`: this run's candidates in the lane whose verdict isn't accepted, oldest first.
    - `earlier_candidates(db, lane, run_id) -> list[Candidate]`: the newest candidate per appid in the lane, from runs other than `run_id`, newest first. Task 6 decides how many it looks at.
    - `may_return(candidate, version_now: GameVersion, profile_version_id: int | None) -> bool` (designer's call, spec 7.4):
      - accepted but not added: True;
      - accepted and added: False (it is in the inventory);
      - rejected or no_verdict: True when `version_now.updated_since(candidate's version)`, or the lane's current profile version id is greater than the one it was judged against.
    - `@dataclass EntryView(entry, game, candidate, facts: GameFacts)` with `costs_money: bool`, which is true when the game isn't owned and isn't free.
    - `lane_entries(db, lane, status) -> list[EntryView]`. Recommended is ordered by (`costs_money`, `rank`). The other statuses are ordered by their latest status change, newest first. It uses a fixed number of queries.
  - In `research_runs.py`:
    - Constants:
      - Statuses `RUNNING`, `PAUSED`, `FINISHED`, `STOPPED`, `CANCELLED` and `FAILED`, with `ACTIVE = (RUNNING, PAUSED)`.
      - `RUN_STATUS_LABELS`: "Running", "Paused", "Finished", "Stopped", "Cancelled", "Failed".
      - `AUTO_ROUNDS = 3`.
    - `@dataclass LaneTarget(lane: Lane, start_count: int, target: int | None, add_five: bool, note: str | None)`, with `as_json` and `from_json`. A `target` of None means the lane isn't in this run. `note` says why a lane is left out.
    - `plan_targets(counts: dict[Lane, int], add_five: set[Lane]) -> list[LaneTarget]`:
      - under 20: target 20;
      - 20 or more and ticked: count + 5;
      - otherwise: None, with the note `"{Lane} already has {n} recommended games."`.
    - `active_run(db) -> ResearchRun | None` and `run_targets(run) -> list[LaneTarget]`.
    - `create_run(db, add_five: set[Lane], now) -> ResearchRun`. It saves `running`, the targets, and the choices (`start`, plus `add_five` for each ticked lane). It commits. It raises `ValueError` while a run is active. Task 7's `start_research` is the only caller outside tests.
    - `add_event(db, run_id, step, message, now, *, round=None, lane=None) -> None` commits.
    - `lanes_short(db, run) -> list[Lane]`: the lanes with a target and no note whose recommended count is below the target.
    - `@dataclass RunSummary(run, accepted: int, rejected: int, added: int)`. `rejected` counts every verdict that isn't accepted. Also `past_runs(db, limit=20) -> list[RunSummary]`, newest first, with one query for the counts.
  - In `tests/conftest.py`:
    - `solo_version`: a published Solo review of Sample Quest ("Short runs, real choices every turn.", rating 8) and the Solo profile's version 1 built from it. It has one stated `loved` line, "Likes short, tight sessions", which cites game 10 and the review. It returns the `ProfileVersion`.
    - `research_run`: `create_run(db, set(), FIXED_NOW)`.

- [ ] **Step 1: Write the failing tests.**

```python
def judged(appid, verdict="accepted", reason="Fits short sessions.", **over):
    base = dict(appid=appid, source="web_research",
                fits=[{"section": "loved", "text": "Likes short, tight sessions", "tag": "stated", "how": "Short runs."}],
                risk="Might be too light.", version=GameVersion(False, None), facts={}, verdict=verdict,
                reason=reason, list_rejected=False, recommender_call_id=None, judge_call_id=None)
    return JudgedProposal(**(base | over))

def test_plan_targets():
    targets = plan_targets({Lane.SOLO: 3, Lane.TWO: 20, Lane.THREE_FOUR: 25, Lane.FIVE_PLUS: 0}, {Lane.THREE_FOUR})
    assert [(t.lane, t.target) for t in targets] == [(Lane.SOLO, 20), (Lane.TWO, None), (Lane.THREE_FOUR, 30), (Lane.FIVE_PLUS, 20)]
    assert targets[1].note == "Two players already has 20 recommended games."     # Review focus 4: exactly 20 is full

def test_only_one_active_run(db):
    create_run(db, set(), FIXED_NOW)
    with pytest.raises(ValueError):
        create_run(db, set(), FIXED_NOW)

def test_save_judged_adds_only_what_there_is_room_for(db, games, solo_version, research_run):  # Review focus 4
    for appid, name in ((30, "Demo Dungeon"), (40, "Cozy Crawl")):
        ensure_game(db, appid, name, FIXED_NOW)
    saved = save_judged(db, [judged(40), judged(20, "rejected", "Too long."), judged(30)], run_id=research_run.id,
                        round=1, lane=Lane.SOLO, room=1, profile_version_id=solo_version.id, now=FIXED_NOW)
    assert [(c.appid, c.verdict, c.added) for c in saved] == [(40, "accepted", True), (20, "rejected", False), (30, "accepted", False)]
    assert recommended_counts(db)[Lane.SOLO] == 1
    [change] = db.scalars(select(InventoryStatusChange)).all()
    assert (change.status, change.candidate_id) == ("recommended", saved[0].id)

def test_excluded_appids(db, games, solo_version, research_run):
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    set_currently_playing(db, 20, True, FIXED_NOW)
    save_judged(db, [judged(40)], run_id=research_run.id, round=1, lane=Lane.SOLO, room=5,
                profile_version_id=solo_version.id, now=FIXED_NOW)
    assert excluded_appids(db, Lane.SOLO) == {10, 20, 40}     # reviewed in Solo, playing, in the inventory
    assert excluded_appids(db, Lane.TWO) == {20}

@pytest.mark.parametrize("verdict,added,now_version,profile_now,back", [
    ("rejected", False, GameVersion(False, T1), 1, False),
    ("rejected", False, GameVersion(False, T2), 1, True),     # patched since
    ("rejected", False, GameVersion(False, T1), 2, True),     # the profile is newer
    ("no_verdict", False, GameVersion(False, T1), 2, True),
    ("accepted", False, GameVersion(False, T1), 1, True),     # accepted when the lane had no room
    ("accepted", True, GameVersion(False, T2), 2, False),     # it is in the inventory
])
def test_may_return(verdict, added, now_version, profile_now, back):
    c = Candidate(verdict=verdict, added=added, version=GameVersion(False, T1).as_json(), profile_version_id=1)
    assert may_return(c, now_version, profile_now) is back

def test_earlier_candidates_are_the_newest_per_game_from_other_runs(db, games, solo_version):
    first = create_run(db, set(), FIXED_NOW)
    for rnd, reason in ((1, "Old reason"), (2, "New reason")):
        save_judged(db, [judged(20, "rejected", reason)], run_id=first.id, round=rnd, lane=Lane.SOLO, room=5,
                    profile_version_id=solo_version.id, now=FIXED_NOW)
    first.status = "finished"; db.commit()
    second = create_run(db, set(), FIXED_NOW)
    assert [(c.appid, c.reason) for c in earlier_candidates(db, Lane.SOLO, second.id)] == [(20, "New reason")]
    assert earlier_candidates(db, Lane.SOLO, first.id) == []

def test_recommended_puts_owned_and_free_games_first(db, games, solo_version, research_run):
    save_facts(db, 40, "Cozy Crawl", price_cents=999)
    save_facts(db, 50, "Party Pile", is_free=True)
    for appid, name in ((40, "Cozy Crawl"), (50, "Party Pile")):
        ensure_game(db, appid, name, FIXED_NOW)
    save_judged(db, [judged(40), judged(50), judged(20)], run_id=research_run.id, round=1, lane=Lane.SOLO,
                room=5, profile_version_id=solo_version.id, now=FIXED_NOW)
    assert [v.game.appid for v in lane_entries(db, Lane.SOLO, "recommended")] == [50, 20, 40]
```

`T1` and `T2` come from `tests/fakes.py` (Task 2).

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_queue.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the models, migration `0009` (checked against the drift test), both services and the fixtures. A JSON column changed in place must be reassigned (`run.targets = [...]`) for SQLAlchemy to save it.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: inventory, candidate history and research run records`.

---

### Task 5: The Recommender's and the judge's steps

**Files:**
- Create: `app/routines/recommender.py`, `app/prompts/research_plan.md`, `app/prompts/research_extract.md`, `app/prompts/research_propose.md`, `app/prompts/research_judge.md`
- Test: `tests/test_recommender.py`

**Interfaces:**
- Consumes: `complete_json`, `load_prompt`, `Job`; `LINE_SECTIONS`, `TAG_LABELS` (plan 2); `SearchResult`; Task 2's `GameFacts`; Task 4's `SOURCE_LABELS`, `ACCEPTED`, `REJECTED`, `NO_VERDICT`.
- Produces. The replies are read leniently: the schema only checks the shape, and the code checks the rest.

```python
class ResearchPlan(BaseModel):
    backlog: list[int] = Field(default=[], max_length=10)
    queries: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default=[], max_length=4)

class FoundGame(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = ""

class FoundGames(BaseModel):
    games: list[FoundGame] = Field(default=[], max_length=12)

class Fit(BaseModel):
    line: str = Field(max_length=10)       # a profile line id, "L3"
    how: str = Field(default="", max_length=300)

class Proposal(BaseModel):
    appid: int
    fits: list[Fit] = Field(default=[], max_length=5)
    risk: str = Field(default="", max_length=300)

class Proposals(BaseModel):
    proposals: list[Proposal] = Field(default=[], max_length=25)

class Verdict(BaseModel):
    appid: int
    accept: bool
    reason: str = Field(default="", max_length=300)

class Verdicts(BaseModel):
    verdicts: list[Verdict] = Field(default=[], max_length=25)
    reject_all: str | None = Field(default=None, max_length=300)
```

  - `@dataclass(frozen=True) ProfileLineRef(id: str, section: str, text: str, tag: str)`. `number_lines(content: dict) -> list[ProfileLineRef]` numbers the lines `L1`, `L2`, ... across `LINE_SECTIONS` in order. Open questions aren't lines.
  - `@dataclass PoolGame(game: Game, facts: GameFacts, source: str, history: list[dict] = field(default_factory=list))`. `history` holds the game's earlier verdicts in this lane, `{"when": ISO date, "verdict", "reason"}`, newest first.
  - `pool_input(items: list[PoolGame]) -> list[dict]` gives what the prompts show for each game:
    - `appid`, `name`, `owned`, `hours` (owned games only)
    - `source` (its label)
    - `price`, `early_access`, `reviews` (`review_text`), `release_date`, `description`, `categories`
    - `up_to_players`, `latest_patch` (title and date), `facts_checked` (the store fact's date)
    - `history`
  - `@dataclass(frozen=True) LaneBrief(lane, label, profile_version_id: int, lines: list[ProfileLineRef], head_counts: list[int], gap: int, rejected: list[dict], today: str)`. `rejected` holds this run's rejections and refusals, as `{"game", "reason"}`.
  - `@dataclass(frozen=True) CheckedProposal(item: PoolGame, fits: list[dict], risk: str)`. `fits` are resolved to `{"section", "text", "tag", "how"}` from the profile, so a line on a pick is always a real line of the profile.
  - `check_proposals(proposals: Proposals, pool: dict[int, PoolGame], lines: list[ProfileLineRef], count: int) -> tuple[list[CheckedProposal], list[str]]`:
    - It goes in the order given and keeps up to `count`.
    - A repeated appid is dropped without a note.
    - A fit citing an unknown line id is dropped.
    - It refuses, in this exact copy:
      - `"App {appid} wasn't one of the candidates, so it was left out."`
      - `"{name} was left out: it didn't name a line of the profile it fits."` (no valid fit left)
      - `"{name} was left out: it didn't say its main risk."` (blank risk)
  - `@dataclass(frozen=True) Ruling(proposal: CheckedProposal, verdict: str, reason: str, list_rejected: bool)`, and `NO_VERDICT_REASON = "The judge gave no verdict on this game."`.
  - `apply_verdicts(verdicts: Verdicts, proposals: list[CheckedProposal]) -> list[Ruling]` gives one ruling per proposal, in order:
    - A non-blank `reject_all` rejects every proposal with that reason and `list_rejected=True`.
    - Otherwise the first verdict for each proposed appid counts. A verdict with a blank reason counts as no verdict.
    - Verdicts for appids that weren't proposed are ignored.
  - Four calls. Each renders its prompt with the brief and returns `(reply, ai_call_id)`. Each has the keyword arguments `*, services, run_ref, now`:
    - `plan_research(db, vault, brief, backlog: list[dict], known: list[str], **kw) -> tuple[ResearchPlan, int]` uses `Job.RECOMMENDER` and `research_plan`.
    - `extract_games(db, vault, brief, results: list[SearchResult], known: list[str], **kw) -> tuple[FoundGames, int]` uses `Job.RECOMMENDER` and `research_extract`. Each result's content is cut to `RESULT_CHARS = 600`.
    - `propose(db, vault, brief, pool: list[PoolGame], count: int, **kw) -> tuple[Proposals, int]` uses `Job.RECOMMENDER` and `research_propose`.
    - `judge(db, vault, brief, reviews: list[dict], proposals: list[CheckedProposal], **kw) -> tuple[Verdicts, int]` uses `Job.JUDGE` and `research_judge`. `reviews` come from `review_input`.
  - What each prompt must do. The wording is the builder's call. Every one starts with `# <name>`, treats the user's words and web text as data, and asks for JSON only in its shape.
    - **`research_plan`:**
      - Shows the lane, its head counts, the profile lines with their ids and tags, and this run's rejections.
      - Picks up to 10 owned backlog games (appid, name, hours) that might fit by how they play.
      - Writes up to 4 web searches for games that fit by how they play (taste dimensions, not genre tags), naming the head count in co-op lanes.
      - Doesn't go looking for the `known` games again.
    - **`research_extract`:**
      - Lists up to 12 PC games on Steam that the results recommend or discuss as fitting, with the exact store name and the result URL.
      - Leaves out the `known` games and never makes up a game.
    - **`research_propose`:**
      - Shows the lane, its head counts, the profile lines with their ids and tags, the candidates (`pool_input`) and this run's rejections.
      - Picks up to `count` games from the candidates only, best first.
      - Each pick names 1 to 5 profile lines by id, with how the game matches each one, and its main risk: the most likely reason it won't work, counting the cost of trying it (price, learning curve, session length, surviving a break, principle 5). A long, dense game says so.
      - Similarity means how games play, not shared tags.
      - When the fit is close, prefers owned and free games over paid ones.
      - A candidate from the history comes with its earlier reasons. Propose it again only if those reasons no longer apply, for example because a patch fixed the problem. The Recommender judges whether the patch matters.
    - **`research_judge`:**
      - Decides each proposal, accept or reject, with a reason. It tests each one against the profile lines, the reviews behind them, the facts with their dates, the head counts seen in the lane (a co-op game that can't seat them is rejected) and any earlier reasons.
      - May reject the whole list with one reason.
      - Never adds or suggests a game.
      - A vague fit is a rejection.

- [ ] **Step 1: Write the failing tests.**

```python
LINE = lambda text, tag="stated": {"text": text, "tag": tag, "games": [10], "reviews": [1]}
CONTENT = {"loved": [LINE("Likes short, tight sessions")], "dealbreakers": [LINE("Hates long grinds")],
           "patterns": [], "hunches": [LINE("Might like deckbuilders", "guess")], "open_questions": ["Co-op?"]}

def pool_game(appid, name, source="web_research", **facts):
    return PoolGame(Game(appid=appid, name=name, owned=False), facts_for(appid, name, **facts), source)

def test_number_lines_in_section_order():
    assert [(l.id, l.section, l.text) for l in number_lines(CONTENT)] == [
        ("L1", "loved", "Likes short, tight sessions"), ("L2", "dealbreakers", "Hates long grinds"),
        ("L3", "hunches", "Might like deckbuilders")]

def test_check_proposals_keeps_only_real_candidates_and_lines():  # Review focus 1
    pool = {10: pool_game(10, "Sample Quest"), 20: pool_game(20, "Test Tactics"), 30: pool_game(30, "Demo Dungeon")}
    reply = Proposals.model_validate({"proposals": [
        {"appid": 99, "fits": [{"line": "L1", "how": "x"}], "risk": "r"},
        {"appid": 10, "fits": [{"line": "L9", "how": "x"}], "risk": "r"},
        {"appid": 20, "fits": [{"line": "L9", "how": "x"}, {"line": "L2", "how": "No grind."}], "risk": "Pricey."},
        {"appid": 20, "fits": [{"line": "L1", "how": "again"}], "risk": "r"},
        {"appid": 30, "fits": [{"line": "L1", "how": "Short."}], "risk": ""},
    ]})
    kept, refused = check_proposals(reply, pool, number_lines(CONTENT), count=5)
    assert [(p.item.game.appid, p.fits) for p in kept] == [
        (20, [{"section": "dealbreakers", "text": "Hates long grinds", "tag": "stated", "how": "No grind."}])]
    assert refused == ["App 99 wasn't one of the candidates, so it was left out.",
                       "Sample Quest was left out: it didn't name a line of the profile it fits.",
                       "Demo Dungeon was left out: it didn't say its main risk."]

def test_check_proposals_stops_at_count():
    pool = {a: pool_game(a, f"Sample {a}") for a in (10, 20, 30)}
    reply = Proposals.model_validate({"proposals": [{"appid": a, "fits": [{"line": "L1", "how": "x"}], "risk": "r"} for a in (10, 20, 30)]})
    assert [p.item.game.appid for p in check_proposals(reply, pool, number_lines(CONTENT), count=2)[0]] == [10, 20]

def test_the_judge_never_adds_games():  # Review focus 1
    proposals = [CheckedProposal(pool_game(a, f"Sample {a}"), [], "r") for a in (10, 20)]
    v = Verdicts.model_validate({"verdicts": [{"appid": 10, "accept": True, "reason": "Fits L1."},
                                              {"appid": 30, "accept": True, "reason": "Also good."}]})
    assert [(r.proposal.item.game.appid, r.verdict, r.reason) for r in apply_verdicts(v, proposals)] == [
        (10, "accepted", "Fits L1."), (20, "no_verdict", "The judge gave no verdict on this game.")]

def test_reject_all():
    proposals = [CheckedProposal(pool_game(a, f"Sample {a}"), [], "r") for a in (10, 20)]
    v = Verdicts.model_validate({"verdicts": [{"appid": 10, "accept": True, "reason": "ok"}], "reject_all": "All long grinds."})
    assert {(r.verdict, r.reason, r.list_rejected) for r in apply_verdicts(v, proposals)} == {("rejected", "All long grinds.", True)}

BRIEF = LaneBrief(Lane.SOLO, "Solo", 1, number_lines(CONTENT), [], 5, [{"game": "Old Crate", "reason": "Runs are too long."}], "2026-01-15")

def test_propose_sends_lines_facts_and_rejections(db, app, services, fake_ai, saved_ai_setup):
    reply = {"proposals": [{"appid": 40, "fits": [{"line": "L1", "how": "Short runs."}], "risk": "Light."}]}
    fake_ai.replies["sample-big"] = by_prompt(research_propose=json.dumps(reply))
    proposals, call_id = propose(db, app.state.vault, BRIEF, [pool_game(40, "Cozy Crawl")], 3,
                                 services=services, run_ref="research:1", now=FIXED_NOW)
    prompt = fake_ai.calls[-1][2]
    assert prompt.startswith("# research_propose\n") and "L1" in prompt and "Likes short, tight sessions" in prompt
    assert "Very Positive (100 reviews)" in prompt and "Runs are too long." in prompt
    assert proposals.proposals[0].appid == 40 and db.get(AiCall, call_id).job == "recommender"

def test_judge_uses_the_judge_and_reads_reviews_as_written(db, app, services, fake_ai, saved_ai_setup, games):
    fake_ai.replies["sample-local"] = by_prompt(research_judge='{"verdicts": []}')
    raw = "  <b>Loved</b> it\n\nsecond  "
    reviews = review_input(db, [publish(db, 10, Lane.SOLO, ReviewForm(text=raw), FIXED_NOW)])
    _, call_id = judge(db, app.state.vault, BRIEF, reviews, [CheckedProposal(pool_game(40, "Cozy Crawl"), [], "Light.")],
                       services=services, run_ref="research:1", now=FIXED_NOW)
    assert raw in fake_ai.calls[-1][2] and db.get(AiCall, call_id).job == "judge"

def test_every_research_prompt_starts_with_its_name():
    for name in ("research_plan", "research_extract", "research_propose", "research_judge", "player_counts"):
        assert load_prompt(name).source.startswith(f"# {name}\n")
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_recommender.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the module and the four prompts.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: Recommender and judge steps with code-checked proposals and verdicts`.

---

### Task 6: One lane in one research round

**Files:**
- Create: `app/routines/research.py`
- Modify: `tests/conftest.py` (`research_world`)
- Test: `tests/test_research_round.py`

**Interfaces:**
- Consumes:
  - Tasks 2 to 5: everything above.
  - Plan 2: `JobContext`, `current_version`, `find_profile`, `lane_key`, `profile_sources`, `review_input`, `StructuredReplyError`.
  - Plan 1: `stored_ai_setup` (for the Tavily key), `services.search_factory`, `SteamClient`, `STORE_REGION_SETTING`.
- Produces:
  - Constants:
    - `PROPOSAL_EXTRA = 3`: proposals asked for beyond the gap (spec section 11).
    - `BACKLOG_MAX_MINUTES = 120` and `BACKLOG_LIST_LIMIT = 300`.
    - `HISTORY_SCAN = 40`: earlier candidates looked at, newest first. `HISTORY_LIMIT = 15`: how many of them may join the pool.
    - `KNOWN_LIMIT = 100`, `SEARCH_RESULTS = 5` and `JUDGE_REVIEWS_LIMIT = 40`.
  - `@dataclass(frozen=True) LaneOutcome(added: int, rejected: int, cancelled: bool)`.
  - `head_counts_seen(db, lane) -> list[int]`: sorted, unique, and empty for Solo. For each published version in the lane, it counts 1 plus the people tagged, raised to the lane's lowest head count, because untagged outsiders may have played. It also counts 1 plus the members of each named group whose size fits the lane.
  - `normalize_name(name: str) -> str` casefolds, drops ™ ® ©, turns each run of other characters into one space, and strips.
  - `resolve_name(steam: SteamClient, name: str, region: str) -> StoreHit | None` returns the first store-search hit whose normalized name equals the normalized `name`, or None. It never takes a near match.
  - `research_lane(ctx: JobContext, run_id: int, round: int, lane: Lane) -> LaneOutcome` runs one lane's steps. It writes one `ResearchEvent` per step, with this exact copy and step name, carrying the round and the lane:
    1. **Brief:**
       - The lane's current profile version and `number_lines`.
       - `gap` = target minus the recommended count.
       - `head_counts_seen`.
       - `rejected` = this run's `rejected_in_run` as `{"game", "reason"}`.
       - One search client for the lane: `ctx.services.search_factory(stored_ai_setup(db, ctx.vault).tavily_key)`.
    2. **Candidate history:**
       - Take the first `HISTORY_SCAN` of `earlier_candidates`, leaving out `excluded_appids` and this run's rejections.
       - Refresh their store facts. Then keep, in order, up to `HISTORY_LIMIT` of those where `may_return(candidate, facts.version, profile_version.id)`.
       - Each kept game becomes a `PoolGame` with source `candidate_history` and its history from every earlier candidate row for the lane and game.
       - Event `history`: `"{m} of {n} earlier candidates are back for another look."`, only when `n > 0`.
       - **The skip set** for the rest of the lane is `excluded_appids`, this run's rejections, and every appid in `earlier_candidates` that isn't in the history pool (see Global constraints).
    3. **Plan:** event `plan`, `"Asking the Recommender where to look."`.
       - The backlog sent is owned games under `BACKLOG_MAX_MINUTES`, not skipped and not already in the pool, fewest minutes first, then by name, up to `BACKLOG_LIST_LIMIT`.
       - `known` is the names of the lane's inventory games and the history pool, up to `KNOWN_LIMIT`.
       - Backlog appids that weren't offered are dropped. The rest get source `owned_backlog`.
    4. **Web:**
       - For each query: event `search`, `"Searching the web: {query}"`. A `SearchError` gives event `problem`, `"The web search failed ({message}). Carrying on without it."`, and the lane goes on.
       - With any results, call `extract_games`, then `resolve_name` and `ensure_game` for each name. A `SteamError` from the store search counts as no match. Games that are skipped or already in the pool are left out.
       - Event `search`: `"Found {n} games on the web."`.
       - When some names had no exact store match, event `problem`: `"Couldn't find these on Steam: {names}."`.
    5. **Facts:** event `facts`, `"Checking Steam for {n} games."`, then `refresh_store_facts` for the backlog and web games.
       - Games that aren't `is_game` leave the pool.
       - Event `problem` when needed: `"Steam's details are missing for: {names}."`.
    6. **Players:** co-op lanes only. For pool games that `needs_player_counts`: event `players`, `"Looking up player counts for {n} games."`, then `lookup_player_counts`.
       - A `SearchError`, or a `StructuredReplyError` that isn't `not_configured`, leaves that game without a count for this round.
       - Then, every lane: each game with a `lane_mismatch` leaves the pool, with event `players` `"Left out {name}: {phrase}."`.
    7. **Empty pool:** event `skipped`, `"No candidates for {lane} this round."`. It returns `LaneOutcome(0, 0, False)`.
    8. **Propose:** event `propose`, `"The Recommender is choosing games."`.
       - `count = min(gap + PROPOSAL_EXTRA, len(pool))`.
       - Run `check_proposals`. Each refusal is an event `problem` with its message.
    9. **Judge:** event `judge`, `"The judge is checking {n} games."`, with the latest version of each review behind the profile, newest first, up to `JUDGE_REVIEWS_LIMIT`, through `review_input`. Then `apply_verdicts`.
    10. **Save:**
        - If the run's `cancel_requested` is set, it saves nothing and returns `LaneOutcome(0, 0, True)`. The run is also checked before every step above.
        - Otherwise `save_judged` with `room=gap`.
        - Event `saved`: `"{lane}: {added} added, {rejected} rejected."`. When some accepted games had no room, it adds `" {k} more were accepted, but the lane is full."`.
  - **Errors:**
    - A `StructuredReplyError` with reason `not_configured` propagates, because the whole run must stop.
    - Any other `StructuredReplyError` from plan, extract, propose or judge ends the lane for this round. It gives event `problem`, `"{lane} stopped this round: {message}"`, and returns `LaneOutcome(0, 0, False)`.
  - Every AI call and search uses `run_ref=f"research:{run_id}"`.
  - `research_world` (in `tests/conftest.py`, used by Tasks 6 to 8): Solo research, ready to run, built on `games`, `saved_ai_setup` and `solo_version`.
    - Sample Quest (10) is reviewed in Solo, so it is excluded. Test Tactics has 120 minutes, so it isn't backlog.
    - "Old Crate" (60) is owned with 30 minutes. It is the backlog pick.
    - Search results come from `fake_search.responder`. Every query returns one result, `{"title": "Cozy picks", "url": "https://example.com/cozy", "content": "Try Cozy Crawl."}`.
    - `fake_steam.store_hits` resolves "Cozy Crawl" to appid 40.
    - `fake_steam.details` has single-player games for 40 and 60. Old Crate costs $4.99 and Cozy Crawl $9.99.
    - The fixture returns a dict, `world`. The fake replies read it each time they are asked, so a test can change an entry before running.
      - `fake_ai.replies["sample-big"]` is `by_prompt(...)` with `research_plan`, `research_extract` and `research_propose` each sending `json.dumps` of `world["plan"]`, `world["extract"]` and `world["propose"]`.
      - `fake_ai.replies["sample-local"]` is `by_prompt(...)`. Its `research_judge` reply is `world["judge_fn"](prompt)` when that is set, else `json.dumps(world["judge"])`. Its `player_counts` reply is `world["players"]`, a string or a function of the prompt. Its default reply is `world["profile"]`.
    - The entries:
      - `plan`: `{"backlog": [60], "queries": ["short cozy roguelites"]}`
      - `extract`: Cozy Crawl, from the result URL
      - `propose`: 40 ("Runs take twenty minutes." for L1, risk "Light on depth."), then 60 (L1, risk "Slow start.")
      - `judge`: `verdicts(accept=[40], reject={60: "Runs are too long."})`
      - `players`: `counts_reply(url="https://example.com/cozy")`, online co-op 1 to 4
      - `profile`: a `profile_reply` that keeps the Solo line and cites the Solo review

- [ ] **Step 1: Write the failing tests.**

```python
def run_lane(app, run, lane=Lane.SOLO, rnd=1):
    return research_lane(job_context(app), run.id, rnd, lane)

def messages(db, run):
    return [e.message for e in db.scalars(select(ResearchEvent).where(ResearchEvent.run_id == run.id).order_by(ResearchEvent.id))]

def test_round_adds_what_the_judge_accepts(app, db, research_world, research_run, solo_version):
    assert run_lane(app, research_run) == LaneOutcome(added=1, rejected=1, cancelled=False)
    [entry] = lane_entries(db, Lane.SOLO, "recommended")
    assert entry.game.appid == 40 and entry.candidate.source == "web_research"
    assert entry.candidate.fits == [{"section": "loved", "text": "Likes short, tight sessions", "tag": "stated",
                                     "how": "Runs take twenty minutes."}]
    old = db.scalar(select(Candidate).where(Candidate.appid == 60))
    assert (old.source, old.verdict, old.reason) == ("owned_backlog", "rejected", "Runs are too long.")
    assert old.profile_version_id == solo_version.id and old.facts["store"]["data"]["name"] == "Old Crate"
    assert "Solo: 1 added, 1 rejected." in messages(db, research_run)

def test_every_call_belongs_to_the_run(app, db, research_world, research_run):
    run_lane(app, research_run)
    ref = f"research:{research_run.id}"
    assert {c.run_ref for c in db.scalars(select(AiCall).where(AiCall.prompt_name.like("research_%")))} == {ref}
    assert {s.run_ref for s in db.scalars(select(SearchCall))} == {ref}

def test_a_lane_never_overfills(app, db, research_world, research_run, solo_version):  # Review focus 4
    fill_lane(db, Lane.SOLO, 19, solo_version)            # helper: 19 accepted, added games 100..118
    research_world["judge"] = verdicts(accept=[40, 60])
    assert run_lane(app, research_run).added == 1
    assert recommended_counts(db)[Lane.SOLO] == 20
    surplus = db.scalar(select(Candidate).where(Candidate.run_id == research_run.id, Candidate.appid == 60))
    assert surplus.verdict == "accepted" and surplus.added is False
    assert "Solo: 1 added, 0 rejected. 1 more were accepted, but the lane is full." in messages(db, research_run)

def test_invented_games_never_reach_the_judge(app, db, research_world, research_run, fake_ai):  # Review focus 1
    research_world["propose"]["proposals"].append({"appid": 99, "fits": [{"line": "L1", "how": "x"}], "risk": "r"})
    run_lane(app, research_run)
    assert db.scalar(select(Candidate).where(Candidate.appid == 99)) is None
    assert "App 99 wasn't one of the candidates, so it was left out." in messages(db, research_run)

def test_rejected_games_are_not_offered_again_in_the_run(app, db, research_world, research_run, fake_ai):
    run_lane(app, research_run)
    run_lane(app, research_run, rnd=2)
    assert db.scalar(select(func.count()).select_from(Candidate).where(Candidate.appid == 60)) == 1
    second_plan = [p for _, _, p in fake_ai.calls if p.startswith("# research_plan")][-1]
    assert "Runs are too long." in second_plan
    assert "No candidates for Solo this round." in messages(db, research_run)

def test_an_earlier_rejection_stays_out_when_nothing_changed(app, db, research_world, research_run, solo_version):
    earlier = ResearchRun(status="finished", targets=[], created_at=FIXED_NOW - timedelta(days=30)); db.add(earlier); db.commit()
    save_judged(db, [judged(40, "rejected", "Too buggy at launch.")], run_id=earlier.id, round=1, lane=Lane.SOLO,
                room=5, profile_version_id=solo_version.id, now=FIXED_NOW - timedelta(days=30))
    run_lane(app, research_run)                           # the web search finds Cozy Crawl again
    assert db.scalar(select(Candidate).where(Candidate.run_id == research_run.id, Candidate.appid == 40)) is None

def test_an_earlier_rejection_comes_back_after_a_patch(app, db, research_world, research_run, solo_version, fake_steam, fake_ai):
    earlier = ResearchRun(status="finished", targets=[], created_at=FIXED_NOW - timedelta(days=30)); db.add(earlier); db.commit()
    save_judged(db, [judged(40, "rejected", "Too buggy at launch.", version=GameVersion(False, T1))], run_id=earlier.id,
                round=1, lane=Lane.SOLO, room=5, profile_version_id=solo_version.id, now=FIXED_NOW - timedelta(days=30))
    fake_steam.news[40] = [{"title": "Patch 1.0", "date": int(T2.timestamp()), "tags": ["patchnotes"]}]
    run_lane(app, research_run)
    [entry] = lane_entries(db, Lane.SOLO, "recommended")
    assert entry.candidate.source == "candidate_history"
    assert all("Too buggy at launch." in p for _, _, p in fake_ai.calls if p.startswith(("# research_propose", "# research_judge")))

def test_a_coop_lane_leaves_out_games_that_cant_seat_it(app, db, research_world, research_run, fake_ai):  # Review focus 5
    two_version(db)                                       # helper: a Two players review and profile version 1
    research_world["players"] = lambda p: counts_reply(url="https://example.com/cozy", online=(1, 4) if "Cozy Crawl" in p else (1, 1))
    run_lane(app, research_run, lane=Lane.TWO)
    assert "Left out Old Crate: its co-op supports up to 1 player." in messages(db, research_run)
    assert {c.appid for c in db.scalars(select(Candidate))} == {40}

def test_a_search_failure_carries_on_with_the_backlog(app, db, research_world, research_run, fake_search):
    fake_search.fail(RuntimeError("down"))
    assert run_lane(app, research_run) == LaneOutcome(added=0, rejected=1, cancelled=False)
    assert any(m.startswith("The web search failed (") for m in messages(db, research_run))

def test_a_cancel_before_saving_drops_the_lane(app, db, research_world, research_run):  # Review focus 3
    def judge_then_cancel(prompt):
        with app.state.session_factory() as s:
            s.get(ResearchRun, research_run.id).cancel_requested = True; s.commit()
        return json.dumps(research_world["judge"])
    research_world["judge_fn"] = judge_then_cancel        # research_world routes the judge through this when set
    assert run_lane(app, research_run).cancelled is True
    assert db.scalar(select(func.count()).select_from(Candidate)) == 0

def test_an_unusable_judge_reply_ends_the_lane_not_the_run(app, db, research_world, research_run):
    research_world["judge_fn"] = lambda p: "nope"
    assert run_lane(app, research_run) == LaneOutcome(0, 0, False)
    assert any(m.startswith("Solo stopped this round: The model's reply wasn't in the expected format.") for m in messages(db, research_run))

def test_head_counts_seen(db, games, people):
    sam, alex, crew = people
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(text="x", people=[sam.id]), FIXED_NOW)
    assert head_counts_seen(db, Lane.THREE_FOUR) == [3]   # 1 + Sam, raised to 3; the crew of two seats 3 too
    assert head_counts_seen(db, Lane.SOLO) == []
```

Helpers, all in `tests/fakes.py` so the research test modules share them:
- `judged` (Task 4) and `counts_reply` (Task 3) move there.
- `verdicts(accept=(), reject=None) -> dict` builds a judge reply. Accepted games get the reason "Fits L1.", and `reject` maps each appid to its reason.
- `fill_lane(db, lane, n, profile_version)` adds games 100 and up, not owned, and `n` accepted, added candidates for them, from a finished run of its own.
- `messages(db, run)` lists the run's event messages, oldest first.
- `two_version(db)` publishes a Two players review of Test Tactics and saves the Two players profile's version 1, with one stated line (L1).

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_research_round.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** `research_lane` and the fixture. Open one session for the lane's reads and writes, as `_update_profile` does in the refresh. Read `cancel_requested` with a fresh query each time, never from a cached object.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: one research round per lane: history, backlog, web, facts, propose, judge`.

---

### Task 7: Research runs as jobs: rounds, pause, continue, stop, cancel

**Files:**
- Modify: `app/routines/research.py`, `app/services/research_runs.py`, `app/routines/refresh.py`, `app/jobs.py`, `app/main.py`
- Test: `tests/test_research_runs.py`

**Interfaces:**
- Consumes: Task 4's run records; Task 6's `research_lane`; `enqueue_job`, `JobContext`, `recover_interrupted_jobs` (plan 2); `job_config`, `JOB_LABELS`, `stored_ai_setup`.
- Produces:
  - `RESEARCH_KIND = "research"` in `app/jobs.py`.
  - `run_refresh(ctx, job_id) -> int` now returns its `RefreshRun` id. Nothing else about the refresh changes.
  - In `research_runs.py`:
    - `class ResearchError(Exception)`, with `.message`.
    - `research_blockers(db, vault) -> list[str]`, in this order:
      - for each job in Recommender, Review judge, Everyday tasks whose `job_config` is None: `"Set up the {label} model in Settings, then run research again."`
      - when `stored_ai_setup(db, vault).tavily_key` is empty: `"Add a Tavily key in Settings, then run research again."`
    - `start_research(db, vault, add_five: set[Lane], now) -> ResearchRun`:
      - It raises `ResearchError` with the first blocker, or `"Research is already running."` while a run is active, or `"Every lane already has 20 recommended games. Tick a lane to add 5 more."` when no lane gets a target.
      - Otherwise it calls `create_run` and `enqueue_job(db, RESEARCH_KIND, "user", now)`. The caller wakes the worker.
    - `continue_research(db, run_id, now) -> bool`: only for a paused run. It adds 1 to `rounds_allowed`, sets `running`, records the `continue` choice and enqueues a research job. It returns False for any other state, so a second click is a no-op.
    - `stop_research(db, run_id, now) -> bool`: only for a paused run. It sets `stopped` and `finished_at`, records the `stop` choice, and adds the event `"Stopped after round {n}."`.
    - `cancel_research(db, run_id, now) -> bool` records the `cancel` choice:
      - A paused run becomes `cancelled` at once, with `finished_at` and the event `"Cancelled."`.
      - A running run gets `cancel_requested`, and the handler ends it.
      - Any other state returns False.
    - `recover_interrupted_research(db, now) -> int`. `create_app` calls it right after `recover_interrupted_jobs`. Each `running` run with no queued research job:
      - becomes `cancelled` if it had `cancel_requested`;
      - otherwise becomes `paused`, with `rounds_allowed = rounds_done` and the event `"Interrupted by a restart. Run one more round to carry on, or stop here."`.
  - `run_research(ctx, job_id) -> None`, registered as `app.state.job_handlers[RESEARCH_KIND]`:
    1. Load the run with status `running`. With none, return. If `cancel_requested` is already set, end it as cancelled (step 6).
    2. **First segment** (`refresh_run_id` is None):
       - Event `refresh`, `"Refreshing profiles first."`. Then `refresh_run_id = run_refresh(ctx, job_id)`, and the event `"Profiles are up to date."`.
       - For each lane with a target but no profile version: set its note to `"No profile yet for {lane}. Publish a review played at this head count first."` and add that note as event `skipped`.
       - Add each full lane's `plan_targets` note as event `skipped` too.
    3. **Blockers:** with any, end as failed with the first one.
    4. **Rounds:** while `rounds_done < rounds_allowed` and `lanes_short`:
       - `round = rounds_done + 1`, event `round`, `"Round {round}."`.
       - Run `research_lane` for each short lane, in lane order. A cancelled outcome ends the run as cancelled.
       - Then `rounds_done += 1`, saved.
    5. **The end:**
       - No lanes short: `finished`, with the event `"Every lane is filled."`. If no lane was ever researchable, the event is `"No lane could be researched this time."` instead.
       - Otherwise: `paused`, with the event `"Paused after round {n}. Still short: {lane labels}."`.
    6. **Ending a run** is one helper, which plan 4 will extend for the score. It sets the status, `finished_at` (not for `paused`), `error` for `failed`, and the event.
       - Cancelled: `"Cancelled. Games the judge accepted before this were kept."`.
       - A `not_configured` `StructuredReplyError`: `failed`, with its message.
       - Any other exception: `failed` with `"Research stopped unexpectedly: {Type}. Try again."`, which is then raised again so the job fails too.

- [ ] **Step 1: Write the failing tests.**

```python
def research(app, db, add_five=()):
    run = start_research(db, app.state.vault, set(add_five), FIXED_NOW)
    run_pending_jobs(app)
    db.expire_all()
    return db.get(ResearchRun, run.id)

def test_start_needs_every_model_and_tavily(db, app, solo_version):
    with pytest.raises(ResearchError) as err:
        start_research(db, app.state.vault, set(), FIXED_NOW)
    assert err.value.message == "Set up the Recommender model in Settings, then run research again."

def test_two_starts_give_one_run_and_one_job(db, app, research_world):  # Review focus 4
    start_research(db, app.state.vault, set(), FIXED_NOW)
    with pytest.raises(ResearchError, match="Research is already running."):
        start_research(db, app.state.vault, set(), FIXED_NOW)
    assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "research")) == 1
    assert [c.choice for c in db.scalars(select(ResearchChoice))] == ["start"]

def test_profiles_are_refreshed_before_research(app, db, research_world, fake_ai):
    publish(db, 20, Lane.SOLO, ReviewForm(rating=6, text="Too slow."), FIXED_NOW)      # unprocessed
    run = research(app, db)
    assert run.refresh_run_id == db.scalar(select(RefreshRun.id).where(RefreshRun.job_id == db.scalar(select(Job.id).where(Job.kind == "research"))))
    prompts = [p for _, _, p in fake_ai.calls]
    assert prompts.index(next(p for p in prompts if "Too slow." in p)) < prompts.index(next(p for p in prompts if p.startswith("# research_plan")))

def test_lanes_without_a_profile_are_skipped_with_a_message(app, db, research_world):
    run = research(app, db)
    assert "No profile yet for Three to four. Publish a review played at this head count first." in messages(db, run)

def test_a_full_lane_is_left_out_unless_ticked(app, db, research_world, solo_version):
    fill_lane(db, Lane.SOLO, 20, solo_version)
    run = research(app, db)
    assert "Solo already has 20 recommended games." in messages(db, run) and run.status == "finished"
    run = research(app, db, add_five=[Lane.SOLO])
    assert run_targets(run)[0].target == 25

def test_pauses_after_three_rounds_and_each_continue_runs_one(app, db, research_world):  # Review focus 4
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    assert (run.status, run.rounds_done) == ("paused", 3)
    assert "Paused after round 3. Still short: Solo." in messages(db, run)
    assert continue_research(db, run.id, FIXED_NOW) is True
    assert continue_research(db, run.id, FIXED_NOW) is False
    run_pending_jobs(app); db.expire_all()
    assert (db.get(ResearchRun, run.id).status, db.get(ResearchRun, run.id).rounds_done) == ("paused", 4)

def test_finishes_when_the_lane_is_full(app, db, research_world, solo_version):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    run = research(app, db)
    assert (run.status, run.finished_at) == ("finished", FIXED_NOW) and recommended_counts(db)[Lane.SOLO] == 20

def test_stop_and_cancel_a_paused_run(app, db, research_world):  # Review focus 3
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    run = research(app, db)
    assert stop_research(db, run.id, FIXED_NOW) and db.get(ResearchRun, run.id).status == "stopped"
    run = research(app, db)
    assert cancel_research(db, run.id, FIXED_NOW) and db.get(ResearchRun, run.id).status == "cancelled"
    assert [c.choice for c in db.scalars(select(ResearchChoice).order_by(ResearchChoice.id))][-1] == "cancel"

def test_a_cancel_before_the_job_starts_makes_no_calls(app, db, research_world, fake_ai):  # Review focus 3
    run = start_research(db, app.state.vault, set(), FIXED_NOW)
    cancel_research(db, run.id, FIXED_NOW)
    run_pending_jobs(app); db.expire_all()
    assert db.get(ResearchRun, run.id).status == "cancelled" and fake_ai.calls == []

def test_a_restart_mid_run_pauses_it(tmp_path, services):  # Review focus 3
    app1 = create_app(tmp_path, services=services)
    with app1.state.session_factory() as s:
        run = ResearchRun(status="running", targets=[], rounds_done=1, created_at=FIXED_NOW)
        s.add_all([run, Job(kind="research", status="running", requested_by="user", created_at=FIXED_NOW)]); s.commit()
        run_id = run.id
    app2 = create_app(tmp_path, services=services)
    with app2.state.session_factory() as s:
        row = s.get(ResearchRun, run_id)
        assert (row.status, row.rounds_allowed) == ("paused", 1)
        assert "Interrupted by a restart. Run one more round to carry on, or stop here." in messages(s, row)

def test_an_unset_judge_fails_the_run_with_its_message(app, db, research_world):
    run = start_research(db, app.state.vault, set(), FIXED_NOW)
    db.execute(delete(JobAssignment).where(JobAssignment.job == "judge")); db.commit()
    run_pending_jobs(app); db.expire_all()
    row = db.get(ResearchRun, run.id)
    assert (row.status, row.error) == ("failed", "Set up the Review judge model in Settings, then run research again.")

def test_an_unexpected_error_fails_the_run_and_the_job(app, db, research_world, monkeypatch):
    monkeypatch.setattr(research_module, "research_lane", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    run = research(app, db)
    assert (run.status, run.error) == ("failed", "Research stopped unexpectedly: RuntimeError. Try again.")
    assert db.scalar(select(Job.status).where(Job.kind == "research")) == "failed"
```

`messages`, `fill_lane` and `verdicts` are the Task 6 helpers in `tests/fakes.py`. `research_module` is `app.routines.research`.

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_research_runs.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the handler, the run functions, the `run_refresh` return value, and the wiring in `create_app`.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Commit:** `feat: research runs with three rounds, pause, continue, stop and cancel`.

---

### Task 8: Research page

**Files:**
- Create: `app/routes/research.py`, `app/templates/research.html`, `app/templates/partials/research_start.html`, `app/templates/partials/research_status.html`
- Modify: `app/services/research_runs.py` (`research_status`), `app/templates/base.html`, `app/main.py`, `app/static/app.css`
- Test: `tests/test_research_pages.py`

**Interfaces:**
- Consumes: Task 7's run functions; `notify_worker`; `progress_bar` (`partials/progress.html`); `recommended_counts`; `current_version` and `find_profile`, for the lanes that have no profile yet.
- Produces:
  - **Side nav:** "Recommendations" (`/recommendations`) and "Research" (`/research`), in that order, between Profiles and Settings.
  - `research_status(db) -> ResearchStatus(run, active: bool, events: list[ResearchEvent], short: list[tuple[Lane, int, int]], summary: RunSummary | None)`:
    - `run` is the active run, else the newest run, else None.
    - `events` are its last 50 events, oldest first.
    - `short` holds (lane, recommended count, target) for each lane the run still needs to fill.
    - Its `key` is `f"{run.id}:{newest event id}:{run.status}:{run.cancel_requested}"`, or `""` when there is no run.
  - **`GET /research`:** the page. Title "Research", then `#research-status` (the status partial), then "Past runs".
    - Past runs is a table of `past_runs` with the columns Started, Result, Rounds, Accepted and Rejected.
    - The table reloads itself on the `research-finished` event (`from:body`).
  - **Status partial** (`#research-status`, swapped as `outerHTML`):
    - **Nothing active:**
      - The "Run research" button (primary). It does `hx-get="/research/start"` into `#dialog`.
      - When there was an earlier run: `"Last research {when}: {result}. {accepted} accepted, {rejected} rejected."`.
      - Its root listens for `research-started from:body` and reloads itself.
    - **Running:**
      - `progress_bar` with the newest event's message as its label and `"Round {n}"` as its value text.
      - The log as a list. Each event reads `"Round {r} · {Lane} · {message}"`, leaving out the parts that are None.
      - "Cancel research" (secondary). It shows "Cancelling" (disabled) once `cancel_requested` is set.
      - It polls `GET /research/status?was=running&shown={key}` every 2 s.
    - **Paused:**
      - `"Research paused after round {n}."`, then one line for each short lane: `"{Lane}: {count} of {target}."`.
      - "Run one more round" (primary), "Stop here" (secondary) and "Cancel research" (ghost).
  - **`GET /research/status?was=&shown=`:**
    - It answers 204 when the run is active and `key == shown`.
    - Otherwise it answers with the partial. When `was` was running or paused and nothing is active now, the answer also sends `HX-Trigger: research-finished`.
  - **`GET /research/start`:** the start dialog (Ember Dialog, as the review dialog is built). For each lane it shows the label and `"{n} recommended"`, then one of:
    - a lane with no profile version: `"No profile yet. Publish a review played at this head count first."`;
    - a lane at 20 or more: an unchecked checkbox, `name="add_five"`, `value="{lane}"`, labelled `"Add 5 more to {lane}"`;
    - any other lane: `"Fills to 20."`.
    - It lists every blocker. While there are any, "Start research" (primary) is disabled. "Cancel" closes the dialog.
  - **`POST /research`:** reads `add_five` values, ignoring unknown ones, and calls `start_research`.
    - A `ResearchError` re-renders the dialog (200) with the message as a danger alert.
    - On success it calls `notify_worker` and answers with an empty body, which closes the dialog, plus `HX-Trigger: research-started`.
  - **`POST /research/{run_id}/continue`, `/stop` and `/cancel`:** each calls its function and answers with the status partial. An unknown run gives 404.
  - Event text and game names are escaped. Nothing is marked safe.

- [ ] **Step 1: Write the failing tests.**

```python
def test_nav_has_recommendations_then_research(onboarded_client):
    text = onboarded_client.get("/").text
    assert text.index('href="/profiles"') < text.index('href="/recommendations"') < text.index('href="/research"') < text.index('href="/settings"')

def test_start_dialog_shows_counts_and_the_top_up_box(onboarded_client, db, research_world, solo_version):
    fill_lane(db, Lane.SOLO, 20, solo_version)
    text = html.unescape(onboarded_client.get("/research/start").text)
    assert "20 recommended" in text and 'name="add_five" value="solo"' in text and "Add 5 more to Solo" in text
    assert "No profile yet. Publish a review played at this head count first." in text
    assert 'checked' not in text.split('value="solo"')[1].split(">")[0]

def test_start_refused_says_what_to_set_up(onboarded_client, db, solo_version):
    r = onboarded_client.post("/research", data={})
    assert r.status_code == 200 and "Set up the Recommender model in Settings, then run research again." in html.unescape(r.text)

def test_start_twice_queues_one_run(onboarded_client, db, research_world):  # Review focus 4
    r = onboarded_client.post("/research", data={})
    assert r.text == "" and r.headers["HX-Trigger"] == "research-started"
    onboarded_client.post("/research", data={})
    assert db.scalar(select(func.count()).select_from(ResearchRun)) == 1

def test_running_status_shows_the_log_and_polls(onboarded_client, db, research_world, research_run):
    add_event(db, research_run.id, "search", "Searching the web: <b>cozy</b>", FIXED_NOW, round=1, lane="solo")
    text = onboarded_client.get("/research").text
    assert 'hx-trigger="every 2s"' in text and "Round 1 · Solo · Searching the web: &lt;b&gt;cozy&lt;/b&gt;" in text
    key = research_status(db).key
    assert onboarded_client.get(f"/research/status?was=running&shown={key}").status_code == 204

def test_paused_run_offers_one_more_round_or_stop(onboarded_client, app, db, research_world):
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    onboarded_client.post("/research", data={}); run_pending_jobs(app)
    text = html.unescape(onboarded_client.get("/research").text)
    assert "Research paused after round 3." in text and "Solo: 0 of 20." in text
    assert "Run one more round" in text and "Stop here" in text

def test_continue_twice_adds_one_round(onboarded_client, app, db, research_world):  # Review focus 4
    research_world["judge"] = verdicts(reject={40: "No.", 60: "No."})
    onboarded_client.post("/research", data={}); run_pending_jobs(app)
    run = db.scalar(select(ResearchRun))
    onboarded_client.post(f"/research/{run.id}/continue"); onboarded_client.post(f"/research/{run.id}/continue")
    db.expire_all()
    assert db.get(ResearchRun, run.id).rounds_allowed == 4

def test_finishing_tells_the_page(onboarded_client, app, db, research_world, solo_version):
    fill_lane(db, Lane.SOLO, 19, solo_version)
    onboarded_client.post("/research", data={}); run_pending_jobs(app)
    r = onboarded_client.get("/research/status?was=running")
    assert r.headers.get("HX-Trigger") == "research-finished" and "every 2s" not in r.text

def test_past_runs_are_listed(onboarded_client, app, db, research_world):
    onboarded_client.post("/research", data={}); run_pending_jobs(app)
    text = html.unescape(onboarded_client.get("/research").text)
    assert "Past runs" in text and "Finished" in text

def test_research_needs_login(app):
    assert TestClient(app).post("/research", follow_redirects=False).status_code == 303
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_research_pages.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the routes, the templates and the nav. The dialog follows `partials/review_dialog.html` for structure and focus handling.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Check it in the browser.** The controller does this step after review.
  1. Start a dev server with sample data and a Solo profile.
  2. Point the three jobs at a reachable model, or patch in fakes.
  3. Run research and watch the log.
  4. Pause, continue once, then stop.
  5. Check the light theme.

- [ ] **Step 6: Commit:** `feat: research page with live log, pause controls and past runs`.

---

### Task 9: Recommendations page

**Files:**
- Create: `app/routes/recommendations.py`, `app/templates/recommendations.html`, `app/templates/partials/pick.html`
- Modify: `app/main.py`, `app/static/app.css`
- Test: `tests/test_recommendations_page.py`

**Interfaces:**
- Consumes: `lane_entries`, `status_counts`, `STATUS_LABELS`, `SOURCE_LABELS`, `EntryView` (Task 4); `TAG_LABELS` and the badge variants used by `partials/profile_lines.html` (plan 2); `game_art`.
- Produces:
  - **`GET /recommendations?lane=solo&status=recommended`:** both parameters are optional, with those defaults. An unknown lane or status gives 404.
  - **Layout:**
    - title "Recommendations"
    - a lane control: Ember's segmented control, as links with `aria-current="page"` on the current lane
    - status tabs: Ember Tabs (`C:\Users\david\home\Ember\components\src\navigation.tsx`), as links, each `"{Label} {count}"`
    - one pick per entry
  - **A pick** (`partials/pick.html`):
    - artwork, name, and an "Owned" badge for owned games
    - an "Early Access" badge (warning) when the current facts say so
    - a facts list in which each value carries its own `"checked {when}"`:
      - "Players": `"Up to {n} players"`, co-op lanes only, from the players fact
      - "Price": `price_text`, or `"Coming soon"`, left out for owned games
      - "Reviews": `review_text`
      - "Release": `release_date`
    - the source label
    - "Why it fits": each fit as its tag badge, the profile line's text, and `how` as a caption
    - `"Main risk: {risk}"`
  - **Empty states:**
    - Recommended: `"No recommendations for {lane} yet. Run research to find some."`, with a "Go to Research" link styled as a secondary button
    - In progress: `"Nothing in progress in {lane}."`
    - Played: `"Nothing played from {lane}'s recommendations yet."`
    - Declined: `"Nothing declined in {lane}."`
  - **Text:** every piece of text is autoescaped.
  - This plan has no actions on a pick. Plan 4 adds them.

- [ ] **Step 1: Write the failing tests.**

```python
@pytest.fixture
def solo_picks(db, games, solo_version, research_run):
    save_facts(db, 40, "Cozy Crawl", price_cents=999, early_access=True)
    save_facts(db, 20, "Test Tactics")
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    save_judged(db, [judged(40, risk="Light on depth."), judged(20, source="owned_backlog")], run_id=research_run.id,
                round=1, lane=Lane.SOLO, room=5, profile_version_id=solo_version.id, now=FIXED_NOW)

def tab(label, count):
    """A status tab's label followed by its count, whatever markup sits between them."""
    return re.compile(rf">\s*{label}\s*(?:<[^>]*>\s*)*{count}\s*<")

def test_lanes_and_tabs_with_counts(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations").text
    for label in ("Solo", "Two players", "Three to four", "Five or more"):
        assert label in text
    for label, count in (("Recommended", 2), ("In progress", 0), ("Played", 0), ("Declined", 0)):
        assert tab(label, count).search(text)

def test_a_pick_explains_itself(onboarded_client, solo_picks):
    text = html.unescape(onboarded_client.get("/recommendations?lane=solo").text)
    assert "Likes short, tight sessions" in text and "Stated" in text and "Main risk: Light on depth." in text
    assert "Found by web research" in text and "$9.99" in text and "Early Access" in text and "checked" in text

def test_owned_games_come_first_and_show_no_price(onboarded_client, solo_picks):
    text = onboarded_client.get("/recommendations?lane=solo").text
    assert text.index("Test Tactics") < text.index("Cozy Crawl") and "Owned" in text and "$19.99" not in text

def test_coop_picks_show_up_to_players(onboarded_client, db, games, research_run, solo_version):
    save_facts(db, 40, "Cozy Crawl", players=((1, 4), None))
    ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    save_judged(db, [judged(40)], run_id=research_run.id, round=1, lane=Lane.TWO, room=5,
                profile_version_id=solo_version.id, now=FIXED_NOW)
    assert "Up to 4 players" in onboarded_client.get("/recommendations?lane=two").text

def test_an_empty_lane_points_to_research(onboarded_client):
    text = html.unescape(onboarded_client.get("/recommendations?lane=five_plus").text)
    assert "No recommendations for Five or more yet. Run research to find some." in text and 'href="/research"' in text

def test_unknown_lane_or_status_404(onboarded_client):
    assert onboarded_client.get("/recommendations?lane=bogus").status_code == 404
    assert onboarded_client.get("/recommendations?status=bogus").status_code == 404

def test_pick_text_is_escaped(onboarded_client, db, games, research_run, solo_version):
    save_facts(db, 40, "Cozy Crawl"); ensure_game(db, 40, "Cozy Crawl", FIXED_NOW)
    save_judged(db, [judged(40, risk="<script>x</script>")], run_id=research_run.id, round=1, lane=Lane.SOLO,
                room=5, profile_version_id=solo_version.id, now=FIXED_NOW)
    text = onboarded_client.get("/recommendations").text
    assert "<script>x" not in text and "&lt;script&gt;" in text
```

- [ ] **Step 2: Run `.venv/Scripts/python.exe -m pytest tests/test_recommendations_page.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement** the route, the templates and the styles. The pick reuses the tag badges from `partials/profile_lines.html`, as a macro moved into a shared partial if needed.

- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest -W error`.** Expected: all pass.

- [ ] **Step 5: Check it in the browser.** The controller does this step after review. Look at the picks from Task 8's run in both themes and at a narrow width.

- [ ] **Step 6: Commit:** `feat: Recommendations page by lane and status`.

---

### Task 10: README and end-to-end check

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the README.**
  - **What works today:** add research and the Recommendations page. Say that starting, declining and marking games as played come in the next update.
  - **New section, "Research and recommendations":**
    - Research runs only when you press "Run research" on the Research page. It covers every lane that has a profile.
    - Each lane fills to 20 recommended games. A lane already at 20 gets exactly 5 more, but only if you tick its box.
    - Three rounds run on their own. Then the run pauses, and each "Run one more round" runs exactly one more. You can cancel at any point. Games the judge has already accepted stay.
    - Research refreshes profiles first, as the daily refresh does.
    - **What one lane costs in one round:** up to 3 Recommender requests, 1 Review judge request and up to 4 Tavily searches. For co-op lanes, add 1 Tavily search and 1 Everyday tasks request for each game whose player count wasn't checked in the last 7 days. A reply the app can't read is asked for once more, as in the refresh. Steam's store pages are read too, and need no key.
    - **What is sent where:**
      - The Recommender gets the lane's profile, the candidates' Steam details, and the web search results.
      - The Review judge also gets the reviews behind the profile, exactly as you wrote them.
      - Tavily gets search queries and game names.
      - To keep reviews on your computer, pick a local server for the Review judge as well as for Everyday tasks.

- [ ] **Step 2: Run the full suite.** Run `.venv/Scripts/python.exe -m pytest -W error`. Expected: all pass.

- [ ] **Step 3: Check Docker.**
  1. Run `docker compose build`, then `docker compose up -d`.
  2. `/healthz` returns `ok`.
  3. `docker compose logs` shows no errors at startup, and migrations `0008` and `0009` ran.
  4. Run `docker compose down`, then delete the `./data` this check created.

- [ ] **Step 4: Commit:** `docs: README for research and recommendations`.
