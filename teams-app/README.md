# Agent CV — Teams App

Sideloadable Microsoft Teams bot that acts as a conversational front-end for the **Agent CV** service. It receives messages from Teams via the Bot Framework activity protocol, forwards them to the main backend's `/query` REST endpoint, and returns the response.

## Architecture

```
Teams client
    │  Bot Framework activity (HTTP POST)
    ▼
teams-app  (this project)
    │  POST /query  {query, conversation_id}
    ▼
Agent CV backend  (src/agent_cv/)
    │
    ▼
Azure OpenAI + PostgreSQL/pgvector
```

The bot does **no AI work itself** — it is a thin proxy. All business logic, tool calls, and LLM interactions live in the main backend.

## Pre-requisites

1. **Azure Bot registration** — create an Entra app registration and a Bot Service resource (or a standalone Azure Bot), noting the `BOT_APP_ID` and generating a `BOT_APP_PASSWORD` client secret.
2. **Agent CV backend running** — either `uvicorn agent_cv.main:app` locally or via `docker-compose up`.
3. Python 3.12+.

## Local development

The `DevToolsPlugin` automatically creates a localhost tunnel so Teams can reach your local process — no separate tunnel tool needed.

```powershell
cd teams-app
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .

# Copy and fill in credentials
Copy-Item .env.example .env
# Edit .env: set BOT_APP_ID, BOT_APP_PASSWORD, TEAMS_APP_ID

python src/main.py
```

On startup the plugin prints the tunnel URL and updates `appPackage/manifest.json` with `BOT_DOMAIN` automatically. Open **`http://localhost:3979/devtools`** in your browser to access the DevTools UI. Follow the printed link to sideload the app in Teams.

## Sideloading into Teams

1. Populate all variables in `.env` (see `.env.example`).
2. Run the bot (`python src/main.py`) — DevToolsPlugin handles tunneling and sideloading in local dev.
3. For a permanent install: zip the `appPackage/` folder (after substituting `${{...}}` variables) and upload via **Teams Admin Center → Manage apps → Upload** or via the Teams client **Apps → Manage your apps → Upload an app**.

## Environment variables

See `.env.example` for the full list. Key variables:

| Variable | Description |
|---|---|
| `BOT_APP_ID` | Entra app registration client ID |
| `BOT_APP_PASSWORD` | Entra app client secret |
| `BOT_DOMAIN` | Public hostname for the bot (no `https://`) |
| `TEAMS_APP_ID` | Stable UUID identifying this Teams app |
| `DEVELOPER_NAME` | Shown in the Teams app store card |
| `BACKEND_QUERY_URL` | URL of the Agent CV backend `/query` endpoint |
| `BACKEND_TIMEOUT_SECONDS` | HTTP timeout when calling the backend (default: 20) |
