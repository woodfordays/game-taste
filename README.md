# Game Taste

Game Taste turns your reviews of the games you play into a picture of what works for you and the people you play with, and uses that picture to recommend what to play next. You run your own copy in a container on your own computer, so your Steam library, reviews and keys stay with you. See [the vision](docs/vision.md) for what it is for and why.

## What works today

Setup, the Library, reviews written by lane (with drafts and a history of every version), and Settings. Taste profiles and recommendations arrive in later updates. The tour in setup and the Settings page already describe them.

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

Copy the whole `data` folder. It holds the database (`game-taste.db`) and the key file (`secret.key`), and the key file is needed to read the stored keys. Without the key file, the keys stored in a copied database can't be read. Run `docker compose stop` first so the copy is taken while nothing is writing.

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
