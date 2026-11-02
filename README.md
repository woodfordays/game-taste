# Game Taste

Game Taste turns your reviews of the games you play into a picture of what works for you and the people you play with, and uses that picture to recommend what to play next. You run your own copy in a container on your own computer. Your Steam library, reviews and keys are stored there, and your reviews are sent to the AI provider you pick. See [the vision](docs/vision.md) for what it is for and why.

## What works today

Setup, the Library, reviews written by lane (with drafts and a history of every version), taste profiles, the daily refresh that keeps them current, research, the Recommendations page, and Settings. Research finds games for each lane and the Recommendations page lists what it found, by lane and status, with the reasons. Starting a game, declining it and marking it as played come in the next update, so for now the page is for reading.

Home shows your numbers: how many games you've reviewed, your play time, your average rating and your most played game. Only published reviews count, not drafts. Play time is Steam's own count for the games you own, so a game left running counts too.

## Quick start

You need [Docker](https://docs.docker.com/get-started/get-docker/) with Compose. Docker Desktop on Windows and macOS includes both.

```bash
git clone <repo-url> && cd game-taste
docker compose up -d
```

The first run builds the image, which takes a few minutes. Then open `http://localhost:8420` and follow setup. Everything you enter is saved in the `data` folder, which appears next to `docker-compose.yml`.

## What you'll need during setup

- A password for the app. You choose it on your first visit.
- An AI provider key, or a local server URL. Setup offers Local, OpenRouter, OpenAI, Anthropic and Gemini. Add the ones you'll use, then pick a provider and type a model name for each of three jobs: the Recommender, the Review judge and Everyday tasks. Setup sends a small test request to each model and moves on only when every one works.
- A Tavily key, from `https://app.tavily.com`. Game research searches the web through it.
- Optionally, a Steam Web API key from `https://steamcommunity.com/dev/apikey`, and your Steam profile link, vanity name or SteamID64. Set Game details to Public in your Steam privacy settings, or Steam returns no games. You can skip this and connect Steam later in Settings.
- Optionally, the people you play with and any named groups. You can skip this and add them later in Settings.

Everything you set up can be changed later in Settings.

### Using an AI server on the same computer

Inside the container, `localhost` means the container itself, not your computer. To reach a local AI server such as LM Studio or Ollama, use `host.docker.internal` as the host name, for example `http://host.docker.internal:1234/v1`. Replace `1234` with your server's port. Most local servers end the URL in `/v1`. On Linux, the server must listen on all addresses, not only on `127.0.0.1`, or the container can't reach it.

## Profiles and the daily refresh

A taste profile is a short read on what works for one set of players: what they loved, what rules a game out, the patterns across their reviews, and hunches still to test. There is one for each lane (Solo, Two players, Three to four, Five or more) and one for each named group. Profiles are built from your published reviews, not drafts. Profiles lists every lane and every named group. A profile fills in at the first refresh after its lane or group has a published review. Until then its page says there are no reviews yet, or that they're waiting for the next refresh.

The refresh keeps the profiles current. It runs every day at the Refresh time under Schedule in Settings, in the timezone set there, and whenever you select "Refresh now" on Settings or Profiles. If Steam is connected, the refresh syncs your library first. If the app was off at the set time, the refresh runs when the app starts again. While it runs, Settings and Profiles show what it is doing and which profile it is on.

For each profile with new reviews, the refresh asks the Everyday tasks model to update the profile, with one request for every 20 new reviews, rounded up. A reply the app can't read is asked for once more, so a batch of reviews can take two requests. Editing a review and publishing it again counts as a new review. A review tagged with a named group counts for both its lane's profile and the group's, so it is sent for each. The cost depends on the provider you chose. Each request carries the instructions, the current profile and up to 20 reviews, so give a local model a context of at least 16,000 tokens, because many local servers default to less. Each update is saved as a new version. If an update fails its checks, the profile keeps its last version, the refresh status says why, and those reviews are sent again at the next refresh.

Your reviews go to the provider you picked for Everyday tasks, exactly as you wrote them, with the names of the game, the people you played with, and the group and its members. Lane profiles also send a short list of your most-played games and their hours. To keep reviews on your computer, pick a local server for Everyday tasks.

## Research and recommendations

Research finds games for each lane and puts the ones that pass a check on the Recommendations page. It runs only when you select "Run research" on the Research page, never by itself. It covers every lane that has a profile. A lane without one is skipped, and the log says so. Before the first round, research refreshes the profiles, Steam sync included, exactly as "Refresh now" does, so it works from your latest reviews.

Two models share the work. The Recommender proposes games from a pool the app gathers: games you own and haven't played much, games the web searches turn up, and earlier picks the app lets back in. Each proposal names the lines of the profile it fits and its main risk. The Review judge then rules on each proposal, accept or reject, with a reason. Only a game the judge accepts reaches Recommendations.

Each lane fills to 20 recommended games. A lane that already has 20 gets exactly 5 more, but only if you tick its box when you start the run. Three rounds run on their own. If a lane is still short after that, the run pauses, and each "Run one more round" runs exactly one more. "Stop here" ends the run. Cancel works at any point and keeps the games the judge has already accepted. A restart pauses a run in progress, and "Run one more round" carries on from there. Past runs stay listed on the Research page.

### What one lane costs in one round

- The Recommender: up to 3 requests. One plans the round by picking games you own and writing up to 4 web searches, one reads the search results when there are any, and one proposes games when the pool isn't empty.
- The Review judge: 1 request, when there are proposals to rule on.
- Tavily: up to 4 searches.
- Co-op lanes also look up player counts. Each game whose player count wasn't checked in the last 7 days costs 1 Tavily search and 1 Everyday tasks request.
- A reply the app can't read is asked for once more, as in the refresh.
- Steam's store pages are read for each game's details, review summary and latest patch notes. They need no key. Facts older than 7 days are fetched again before they are used. If Steam doesn't answer the review summary, picks show no review score and everything else works.

### What is sent where

- The Recommender gets the lane's profile lines, what this run has rejected so far, your owned games with little play time (names and hours played), the candidates' Steam details, and the web search results.
- The Review judge gets the profile lines and the proposals with their facts. It also gets the reviews behind the profile, the newest 40, exactly as you wrote them, and the reasons for any earlier verdicts on a game.
- Everyday tasks gets web search results for a game's player count.
- Tavily gets the searches the Recommender writes, and for player counts the game's name with the words "number of players co-op multiplayer".

To keep your reviews on your computer, pick a local server for the Review judge as well as for Everyday tasks.

## Security note

Until a password is set, whoever opens the page first sets it. Port 8420 is open to other devices on your network, so open the page and set your password right after you start the container.

## Forgot your password

1. Stop the container.
2. Create an empty file named `reset-password` in the data folder.
3. Start the container again. The next visit asks for a new password and deletes the file.

All data and keys are kept, because the keys are encrypted with the key file, not the password.

From the folder that holds `docker-compose.yml`, the commands are:

```bash
docker compose stop
touch data/reset-password        # in PowerShell: New-Item data/reset-password
docker compose start
```

On Linux the data folder may belong to root. If `touch` says permission denied, run it with `sudo`.

## Backups

Copy the whole `data` folder. It holds the database (`game-taste.db`) and the key file (`secret.key`), and the key file is needed to read the stored keys. Without the key file, the keys stored in a copied database can't be read. Run `docker compose stop` first so the copy is taken while nothing is writing, then copy the folder, then run `docker compose start`. While the app runs, `game-taste.db-wal` and `game-taste.db-shm` may sit beside the database. They are part of the copy. On Linux the files in `data` may belong to root, so copying them may need `sudo`.

To restore, put the folder back as `data` next to `docker-compose.yml` and run `docker compose up -d`.

## Upgrading

```bash
git pull
docker compose up -d --build
```

Your data is kept. Updates to the database are applied when the app starts.

## Development

Python 3.12 or newer is required.

```bash
pip install -c constraints.txt -e ".[dev]"
pytest
```

`constraints.txt` pins the versions of the runtime dependencies. The Docker image installs exactly those, and the `-c` flag makes your install match.

The tests fake every Steam, AI and Tavily call, so they need no network and no keys.
