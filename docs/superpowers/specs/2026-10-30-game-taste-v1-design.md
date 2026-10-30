# Game Taste v1: design

*2026-10-30. Read this alongside [vision.md](../../vision.md) (what the project is for and why), [principles.md](../../principles.md) (what must hold however it's built) and [concepts.md](../../concepts.md) (vocabulary). This document says how the first version gets built. Where it narrows or defers something in those docs, this document wins for v1.*

Anything marked **(designer's call)** was decided while writing this, not by the owner, and can be overruled. Everything else was decided by the owner. Section 11 lists what is left to the builder.

## 1. Intent

Game Taste turns a player's own reviews into taste profiles, one for each lane and each named group, and uses them to recommend what to play next. v1 is the first usable version. The whole loop works end to end, but the AI routines behind it are simple first passes.

The project grows in crawl, walk, run stages. Two things have to be right from the start because they're expensive to redo later:

- **The user experience.** It should be easy from the first page.
- **The data collected.** It should be complete from day one, so later tuning never means collecting it again.

Everything that turns the data into recommendations will be tweaked for as long as the project lives. That covers prompts, the recommender, the judge, ranking, scoring and the refresh. So those parts should be simple, kept apart from each other, and easy to change.

## 2. Goals, non-goals, anti-goals

**Goals**
- A new user clones the repo, runs `docker compose up -d`, opens the web page and finishes all setup in the browser. There's no `.env` file and no config to edit.
- The whole loop works:
  1. Write a review.
  2. Profiles update.
  3. Research fills each lane's queue.
  4. Start or decline a pick.
  5. The next review feeds back in.
- Every piece of data in section 8 is captured from the first day.
- The AI side is easy to change. Prompts live in plain text files, each routine lives in one place, and every AI and search call is logged.
- The owner codes in Python but not web, and can read and review all of it.

**Non-goals for v1** (agreed to come later)
- A recommender score dashboard and a screen for tuning its weights.
- Combination queries ("what suits exactly these people tonight?").
- Profiles asking their open questions to the user directly.
- Richer library browsing and polish beyond the basics.
- Anything hosted, multi-user or synced between copies (out of scope in the vision).
- Games that aren't on Steam **(designer's call)**.

**Anti-goals** (count as failure even if the app works)
- **Tailored to the owner.** No preloaded data, no built-in names for people or groups, no special cases. The owner sets up their own copy exactly as any user would, so they can judge the experience. Tests, fixtures and screenshots use sample data.
- **Over-design.** No frameworks, services or abstractions before they're needed. Add complexity only when something requires it.
- **Losing or rewriting the user's words.**
- **Needing more than the browser.** After `git clone` and `docker compose up -d`, the user shouldn't need a terminal or a file editor.
- **A recommendation that can't explain itself.** Every pick needs its reasons and its main risk.

## 3. Constraints (decided)

- **Language:** Python, so the owner can review it.
- **Database:** SQLite.
- **Container:** one Docker container, started with `docker compose up -d`. All state lives in one mounted data folder.
- **Secrets:** entered in the browser and stored with standard encryption at rest.
- **Web search:** goes only through Tavily, for every model. Models never search for themselves, so model capabilities don't matter.
- **AI providers:** Local (a URL and a key), OpenRouter, OpenAI, Anthropic and Gemini.
  - Each job has its own provider and model.
  - Model names are typed as plain text. The app never fetches model lists or shows dropdowns of models.
- **Look:** the owner's Ember design system. Dark by default, with a light toggle that's remembered.
- **Platform:** Steam (PC).

## 4. Stack (designer's calls)

- **Web app:** Python 3.12 and FastAPI, with server-rendered Jinja2 templates and htmx for in-page updates. htmx handles opening the review dialog, refreshing one card and showing a research run's progress.
  - There's no JavaScript build step, no single-page app and no Node.
  - Reason: everything stays readable as Python plus HTML.
- **Database:** SQLite through SQLAlchemy 2, with Alembic migrations that run automatically at startup.
  - Reason: the schema will change as tuning goes on, and every user's copy has to upgrade without losing data.
- **Secrets:** encrypted with Fernet from the `cryptography` package.
  - The key is generated on first start and written to its own file in the data folder (`/data/secret.key`), separate from the database.
  - The login password is hashed with argon2.
  - The session-signing secret is also generated on first start and kept in the data folder.
  - Trade-off: this protects the database file if it's copied on its own, but not someone who has the whole data folder. A backup means copying the whole data folder.
- **Background work:** one in-process worker thread reads a jobs table and runs research and refreshes one at a time, so the single container is everything.
  - The daily refresh is scheduled inside the same process.
- **AI calls:** a thin adapter for each provider family.
  - The OpenAI SDK covers OpenAI, OpenRouter and Local (an OpenAI-compatible URL).
  - Anthropic uses the `anthropic` SDK, and Gemini uses the `google-genai` SDK.
  - The rest of the app calls one function along the lines of `complete(job, prompt) -> text`.
  - It uses no tool calling, no provider-side search and no structured-output features. Prompts ask for JSON, the app validates it with pydantic, and on failure it retries once with the error included. That works the same way on any model.
- **Search:** Tavily through its Python SDK.
- **Steam:**
  - *Owned games and playtime:* the Steam Web API with the user's key and profile, as in the prototype script `steam_library.py`.
  - *Game facts:* the public store endpoints, which need no key:
    - `api/appdetails` gives the description, artwork, price, release date, Early Access status and categories.
    - `appreviews/<appid>?json=1` gives the review score summary.
    - `api/storesearch` turns a game name into an app ID.
  - *Patch detection:* `ISteamNews/GetNewsForApp`.
  - Tavily is for research only. Artwork is linked from Steam's image servers rather than copied.
- **Ember:** the app ships a copy inside the repo at `app/static/ember/`. The copy is `tokens.css`, `components/bundle.css`, the fonts and the icon SVGs, taken from the owner's Ember folder (`C:\Users\david\home\Ember`).
  - Templates use Ember's `em-` CSS classes directly; the React components aren't used.
  - The rules in Ember's README apply throughout:
    - only one ember-filled primary button per view
    - sentence case
    - plain, calm copy that starts buttons with a verb
    - no emoji and no exclamation marks
  - Fonts are served from the app itself.
- **Theme:** `data-theme` is set on `<html>`, with dark as the default. The toggle at the bottom of the side nav saves the choice in a cookie, so the server renders the right theme with no flash.
- **Tests:** pytest, with AI, Tavily and Steam calls faked.

## 5. Running it

- **Compose file:** `docker-compose.yml` defines one service built from the repo's Dockerfile (`build: .`).
  - It publishes port 8420 and mounts one volume, `./data:/data`.
  - It uses `restart: unless-stopped`.
  - **(designer's call:** port and folder name)
- **First run:** opening `http://localhost:8420` starts onboarding. Until a password is set, whoever opens the page first sets it. That's acceptable for a self-run tool, and the README says so.
- **Forgotten password (designer's call):** this is the one accepted exception to "browser only", and the README documents it.
  1. Stop the container.
  2. Create an empty file named `reset-password` in the data folder.
  3. Start the container again. The next visit asks for a new password and deletes the file.
  - All data and keys are kept, because the keys are encrypted with the key file, not the password.
- **Timezone:** read from the browser during onboarding and editable in Settings. The schedule uses it. There's no `TZ` variable.
- **Upgrades:** `git pull`, then `docker compose up -d --build`. Migrations run at startup. Publishing a prebuilt image can come later.

## 6. Screens and flows

The app shell is Ember's: a side nav with Home, Library, Reviews, Profiles, Recommendations, Research and Settings, and the theme toggle at the bottom. Content sits on `surface` cards.

### 6.1 Onboarding (decided)

Onboarding has five steps, with a progress indicator and Back and Continue buttons. Everything set here can be changed later in Settings.

1. **Create your login.** One password, one user per copy, no accounts.
2. **Connect your AI.**
   - **Providers:** add the ones you'll use.
     - Local takes a server URL and a key. The key may be left empty, since many local servers don't need one **(designer's call)**.
     - OpenRouter, OpenAI, Anthropic and Gemini each take a key.
     - Each provider is entered once.
   - **Jobs:** for each of the three jobs, pick a provider and type a model name.
     - *Recommender:* proposes games.
     - *Review judge:* checks the proposals.
     - *Everyday tasks:* updates profiles from reviews, refreshes, and re-ranks the queue.
   - **Web search:** one Tavily key.
   - **Test and continue:** sends a tiny request to each chosen model and one Tavily search, then reports each result. Continue requires every one to pass, because the app can't do its job without them **(designer's call)**.
3. **Connect Steam.** Enter a Steam Web API key and a profile link (a URL, vanity name or SteamID64), then import the library.
   - If Steam returns no games, explain the "Game details: Public" privacy setting.
   - This step can be skipped.
4. **Who you play with.** Add people (a name only) and named groups (a name plus members).
   - This step can be skipped.
   - Nothing is built in.
5. **Quick tour.** The loop in a few screens. It ends on Home with a prompt to write about a game you played recently.

### 6.2 Home (decided)

- **Cards:** "Currently playing" shows one card per game. Each has the artwork, the name, the current published rating out of 10 and the tags.
  - A game can have a review in more than one lane (6.3). The card shows the most recently published one, labelled with its lane **(designer's call)**.
- **Drafts:** a card shows a "Draft saved" marker when any of its lane reviews has an unpublished draft.
- **Opening a card:** clicking it opens the review dialog on the lane review edited most recently. After Cancel, Save draft or Publish review, the dialog closes and that card refreshes.
- **Empty state (designer's call):** it says how games get here: switch one on in Library, or press Start playing in the queue. If Steam is connected, it also lists the five most recently played Steam games with a "Write a review" button each, so a new user can start right away.

### 6.3 Review dialog (decided)

The same dialog opens from Home, Library and Reviews for any game, which is how backfilling old games works **(designer's call)**.

**One review per lane (decided)**
- A game has a separate review for each lane it's played in, such as a Solo review and a Three-to-four review of the same game.
- Each lane review has its own rating, text, tags, details, draft and published history.
- Within one lane, different groups are told apart by who played on each published version.
- **Lane switcher:** a segmented control at the top of the dialog picks the lane review being written: Solo, Two players, Three to four, Five or more.
  - Lanes that already have a review or draft are marked.
  - Switching lanes with unsaved changes asks whether to save a draft first **(designer's call)**.
- **Which lane it opens on:** the lane review edited most recently. A game with no reviews opens on the lane of its In progress queue entry if it has one, otherwise on Solo **(designer's call)**.

**Main fields**
- **Rating:** 1 to 10, shown as a row of ten buttons.
- **Review:** an editable text box, saved exactly as written.
- **Tags:** comma-separated, with or without `#` in front.
  - Each tag is trimmed, lowercased, has spaces turned into `_`, and is deduplicated.
  - Tags show as chips while you type.

**More details** (collapsed by default)
- **Status:** finished, playing, demo or dropped.
- **Verdict:** loved, liked, mixed or disliked.
- **Feel:** walk-away-able (yes, partly or no), session or run length (free text), and complexity (too light, about right or too much).
The next three fields appear only in the co-op lanes:
- **Who played:** people from the user's list.
- **Named group:** choosing one adds all its members to "who played".
- **How it went for the group:** a second text box. It's saved exactly as written and kept separate from the main review.

**Head-count hint (designer's call):** if you plus the people tagged add up to more than the lane allows, a hint suggests the right lane. It never blocks saving. Fewer than the lane's count is fine, because outsiders who aren't tagged may have played.

**Buttons:** Cancel, Save draft, and Publish review (the primary).

**How saving works**
- **Drafts:** each lane review has at most one draft. It's stored separately and never touches the published review.
- **Reopening:** a lane review with a draft opens on the draft.
- **Publishing:**
  - It turns the draft (or the form) into a new published version and clears the draft.
  - Every earlier published version is kept as history.
  - The game's current Steam hours are recorded onto that version **(designer's call)**.
- **What's required:** nothing beyond the game itself. A review can be published with only a rating or only some text, but not with neither **(designer's call)**.

### 6.4 Library

- **What's listed:** owned Steam games, plus any game that is currently playing or has a review **(designer's call)**. Each row has artwork, name, hours and last played.
- **Finding games:** search by name and sort by hours or last played.
- **Per game:** a "Currently playing" switch, and a link that opens the review dialog.
- **Re-sync:** a button that pulls the Steam library again.
- **Add a game:** search the Steam store by name to add a game that isn't owned, such as a demo or one played elsewhere **(designer's call)**.

### 6.5 Reviews (designer's call: minimal)

A list of every review, published or draft, with one row per game per lane. It can be filtered by lane and verdict. Each one opens the review dialog, and its published versions can be viewed.

### 6.6 Profiles

- **Which profiles:** one for each of the four lanes, plus one for each named group.
- **What each shows:**
  - The profile is rendered from Markdown.
  - Every line carries its evidence tag (Stated, Observed or Guess) and the games behind it.
  - The sections follow concepts.md: loved, dealbreakers, play-history patterns, hunches, open questions and changelog.
  - Earlier versions can be viewed.
- **Read-only in v1 (designer's call):** profiles change only through the refresh.
- **Empty state:** "No reviews in this lane yet. Profiles fill in after you publish reviews."

### 6.7 Recommendations (decided)

**Layout**
- A segmented control picks the lane: Solo, Two players, Three to four, Five or more.
- Tabs filter by status: Recommended, In progress, Played and Declined, each with a count.

**Each pick shows**
- Artwork, name and an "Owned" badge where it applies.
- Facts with the date they were checked: players supported, price, review score and Early Access status.
- Where the pick was found: web research, candidate history or owned backlog.
- Why it fits, as the profile lines it matches with their evidence tags.
- The main risk.

**Order:** Recommended is ordered by the latest ranking, with owned games ahead of games that cost money.

**Actions**
- **Start playing:** switches on Currently playing, moves the pick to In progress, and puts it on Home.
- **Decline game:** opens a dialog asking why (a reason is required). The pick moves to Declined with the reason stored.
- **Switching Currently playing off:** a recommended game moves to Played. A game that was never recommended just leaves Home.
  - Only lane entries that are In progress move to Played. Recommended entries are left alone **(designer's call)**.
  - Switching off offers to open the review dialog ("How did it go?") on the lane that was In progress, with a "Not now" button. A review isn't required. A Played pick with no published review is left out of the outcome half of the score until it has one **(designer's call)**.
- **Switching Currently playing on from Library (designer's call):**
  - If the game is Recommended in exactly one lane, that entry moves to In progress.
  - If it's Recommended in more than one lane, a small dialog asks which lane or lanes.
  - If it isn't in any queue, nothing changes there.

**Lanes are separate:** a game can sit in more than one lane, and each lane's entry has its own status.

### 6.8 Research (mechanics as in the vision)

- **Starting a run:** Run research first shows each lane's Recommended count.
  - Any lane at 20 or more gets an "Add 5 more to <lane>" checkbox, unchecked by default.
  - Lanes under 20 are filled without asking.
- **While running:** a progress log updates live, showing the round, the lane and the current step.
- **Rounds:**
  - Up to three rounds run on their own.
  - After that the run pauses with "Run one more round" and "Stop here". Each continue runs exactly one more round.
- **Cancel:** possible at any point. Games the judge has already accepted are kept, and a half-finished round is dropped **(designer's call)**.
- **Past runs:** listed with their result, rounds, accepted and rejected counts, and the recommender score for that run.

### 6.9 Settings

- **AI and search:** providers and keys, job assignments and model names, and the Tavily key.
- **Steam:** key and profile, plus a store region for prices (default `us`) **(designer's call)**.
- **People and named groups:** add, rename and edit members.
- **Schedule:** refresh time (default 04:00) and timezone, plus a "Refresh now" button.
- **Account:** change password.

The score weights live in the settings table with their defaults and get a screen later.

## 7. Background routines (first pass, expected to change)

- **Prompts:** each one is a text file in `app/prompts/`.
- **Code:** each routine is one Python module.
- **Logging:** every AI and Tavily call is logged (section 8).

### 7.1 Steam sync

- **When it runs:** after the onboarding import, from the Re-sync button, and as part of every refresh.
- **What it does:** updates owned games, hours and last played. It adds a playtime snapshot for any game whose hours changed.

### 7.2 Game facts

- **Source:** Steam's store details and review summary.
- **Storage:** dated snapshots with their source. The latest snapshot is the current fact.
- **Refreshing (designer's call):** facts older than 7 days are re-fetched for any game that is in an inventory, currently playing, or a candidate in a run.
- **Player count:**
  - Steam's data doesn't include supported player counts.
  - Research pulls player counts from Tavily results and stores them as a dated fact with the source URL. For each mode found, it records the minimum and maximum players:
    - online co-op
    - local co-op
    - competitive
  - **Lane filter (designer's call):** a game enters a co-op lane only if its co-op play, online or local, supports at least the lane's lowest head count (2, 3 or 5).
    - The judge is told the head counts actually seen in that lane, from its reviews and from named groups of that size.
    - Each pick shows "Up to N players".
    - So a Five-or-more pick that can't seat a group of six gets caught by the judge, or is easy to spot.
  - A game with no known co-op player count can't enter a co-op lane **(designer's call)**.

### 7.3 Profile refresh

**When it runs:** daily at the set time, from Refresh now, and as the first step of every research run, for any profile with unprocessed reviews.

**Steps**
1. Run a Steam sync, if Steam is connected.
2. Gather the review versions published since each profile's last update, and the declines (with reasons) since then.
3. Update each affected profile.
   - **Which profiles:** the lane of each published review, plus the named group, if the review was tagged with one.
   - **What the Everyday model gets:**
     - the current profile
     - the new review versions, word for word
     - the declines and their reasons
     - a play-history summary, to be used only for Guess lines and choosing questions (principle 9)
   - **What it returns:** the new profile and a changelog line.
4. Check each new profile before saving it.
   - Every line has a tag.
   - Observed lines name two or more games.
   - Stated lines point to a review that says so.
   - If a check fails, the old profile stays and the failure is logged.
5. Refresh facts for games in the inventories (7.2).
6. Re-rank each lane's Recommended games against its profile with the Everyday model, owned games first.
7. Save the new profile versions with their changelog lines, and record the refresh.

### 7.4 Research run

**Before the rounds:** a lane with an empty profile is skipped, and the run says so: "No profile yet for Three to four. Publish a review played at this head count first." **(designer's call)**

**Each round, for each lane that still needs games:**
1. **Candidate history first.** Earlier candidates for the lane that have had a version update since they were last considered are proposed again, along with their earlier decline or rejection reasons.
   - A version update means the Early Access status changed, or Steam news shows a patch-notes post since then.
   - The Recommender decides whether a patch is big enough to matter **(designer's call)**.
2. **Owned backlog.** Owned games with little playtime that might fit are candidates.
3. **Web research.**
   1. The Recommender writes search queries from the lane's profile, and the app runs them through Tavily.
   2. The Recommender picks out game names from the results.
   3. The app looks up each name's Steam app ID and fetches its facts, including player count (7.2).
   4. Games that don't support the lane's head count are dropped.
4. **The Recommender proposes.** It picks enough games to fill the gap, each with:
   - why it fits, as profile lines
   - its main risk
   - its source
5. **The judge decides.** It gets the proposals, the lane profile, the reviews behind it, the facts, and any earlier decline reasons.
   - It accepts or rejects each game with a reason, or rejects the whole list with a reason.
   - It never adds games of its own.
6. **Results are saved.**
   - Accepted games go into the lane's inventory as Recommended.
   - Every proposal is added to the candidate history with:
     - the version it was considered at (its Early Access flag and the date of its latest news post)
     - the judge's verdict and reason
     - the run and round it came from
7. **Next round.** Lanes still short go into the next round, and the Recommender is told what this round rejected and why.

**Which earlier candidates can come back (designer's call):**
- **Declined by the user:** stays out of that lane until it has a version update (principle 10).
- **Rejected by the judge**, individually or as part of a rejected list: stays out of that lane until it has a version update, or until the lane's profile has a newer version than the one it was judged against. That way an owned game rejected while the profile was thin gets another chance.
- **Within one run:** a game rejected for a lane isn't proposed for that lane again.

**Stopping:**
- The run ends when every lane is filled (to 20, or by exactly 5 more where the user ticked the box).
- It pauses after three automatic rounds, and each continue runs exactly one more round.
- It can be cancelled at any point.

### 7.5 Recommender score (designer's call on the formula)

The score is recorded for each research run and shown as a number on the Research page.

- **Acceptance:** the share of this run's proposals the judge accepted.
- **Outcome:** among the lane's picks that have reached Played or Declined, the share that were played and got a rating of 7 or more, or a verdict of loved or liked, in that lane's review.
- **Score:** `w_accept × acceptance + w_outcome × outcome`. Both weights default to 0.5 and are stored in the settings table.

## 8. Data collected from day one

This is the part that has to be right. Store everything below, even where v1 doesn't use it yet.

**Ground rules**
- Never delete data the user entered.
- When something changes state, add a record of the change rather than overwriting.

**What to store**
- **Reviews:** one review per game per lane, with every published version kept.
  - The main text and the "how it went for the group" text, word for word.
  - Rating, normalized tags, and all the details fields.
  - Who played and the named group.
  - Steam hours at that moment, and a timestamp.
  - Drafts are stored separately.
  - Who played is stored on the review itself, so later changes to a group's members don't rewrite past reviews.
- **People and named groups:** names and current members.
- **Library:** owned games, plus a playtime snapshot each time hours change (game, minutes, last played, when the snapshot was taken).
- **Game facts:** dated snapshots with their source. Sources are the store, reviews, news, or a research URL for player counts.
- **Inventory:** one entry per lane and game. Each entry has:
  - why it fits, its main risk and its source
  - its facts as they were when it was recommended
  - a full status history: each change with a timestamp, and the reason for any decline
- **Candidate history:** every game ever proposed, with:
  - the lane, run and round
  - the version it was considered at
  - the version of the lane's profile it was judged against
  - the judge's verdict and reason
- **Research runs:**
  - the targets for each lane
  - the user's choices: add 5, continue, stop or cancel
  - the rounds, the results and the score
- **Profiles:** every version, with:
  - its changelog line
  - the review versions and declines that triggered it
  - the model, and the prompt file and version used
- **AI calls:**
  - the job, provider and model
  - the prompt file name and a hash of it
  - the full request and response text
  - token counts where the provider reports them
  - duration, any error, and the run or refresh it belonged to
- **Tavily calls:** the query, the raw results, and the run they belonged to.
- **Refreshes:** when each one ran, which steps ran, and what changed.

## 9. Repository layout (suggested; the builder may adjust)

```
app/
  main.py            FastAPI app and startup (migrations, worker, scheduler)
  db.py, models.py   SQLAlchemy setup and tables
  migrations/        Alembic
  routes/            onboarding, home, library, reviews, profiles, recommendations, research, settings
  services/          steam.py, game_facts.py, ai.py, search.py, secrets.py, jobs.py, scheduler.py
  routines/          refresh.py, research.py, score.py
  prompts/           one text file per prompt
  templates/         Jinja2 pages and partials
  static/ember/      the copy of Ember
tests/
docs/
Dockerfile
docker-compose.yml
pyproject.toml
README.md
```

## 10. Changes to the other docs

Lanes now follow head count, decided by the owner during design: Solo, Two players, Three to four, and Five or more. vision.md, principles.md and concepts.md were updated to match, replacing the earlier three lanes (solo, two-player, and group of six or more).

## 11. Left to the builder

- Exact table design, as long as everything in section 8 is captured.
- The wording of the prompts.
- How the scheduler and the worker are built.
- Page details this document doesn't describe, following Ember's README.
- The quick-tour content and the README (setup steps, where to get each key).
- How many extra games the Recommender proposes beyond the gap.
- Retries, timeouts and error messages for Steam, Tavily and AI calls. Errors follow Ember's voice: say what happened, then what to do.
