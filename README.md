# Agent CV

PostgreSQL-first backend for querying employee certifications and CV information from documents in `PDFs/REPOCV`, integrated with Microsoft Teams via a Graph API polling bot.

## What is implemented

- PostgreSQL schema with pgvector (`sql/001_init.sql`)
- FastAPI service with endpoints:
  - `GET /health`
  - `POST /admin/init-db`
  - `POST /admin/ingest`
  - `POST /query`
  - `GET /teams/status`
- Local ingestion pipeline:
  - Scans `PDFs/REPOCV`
  - Deduplicates by SHA-256
  - Parses filename metadata for employee/cert hints
  - Extracts PDF/TXT text snapshots
  - Generates embeddings via Azure OpenAI
- **AI agent query engine** (`services/agent_service.py`):
  - Uses OpenAI tool-calling (function calling) in an agentic loop
  - LLM decides which tools to invoke based on the user query
  - Maintains per-conversation history for multi-turn dialogue
  - Responds in the same language as the user (EN/PT)
- **Agent tools available to the LLM**:
  - `search_certifications` — keyword + semantic search over the certifications table
  - `search_experience` — pgvector semantic search over CV documents
  - `get_employee_profile` — full profile lookup for a specific employee
  - `list_employees` — enumerate all employees
  - `search_web` — DuckDuckGo lookup for certification/vendor context
- **Teams integration** via Microsoft Graph polling bot (`teams/agent.py`):
  - Authenticates as service account using MSAL ROPC flow
  - Polls `GET /me/chats` and `GET /chats/{id}/messages` on a configurable interval
  - Skips messages received before bot startup
  - Posts replies via `POST /chats/{id}/messages`

## Quick start

1. Create virtual environment and install dependencies.
2. Copy `.env.example` to `.env` and set `POSTGRES_DSN`.
3. Ensure PostgreSQL has `pgvector` extension available.
4. Run DB schema initialization:

**pwsh**
```pwsh
python -m scripts.init_db
```
**bash**
```bash
python -m scripts.init_db
```

5. Start API:

**pwsh**
```pwsh
uvicorn agent_cv.main:app --reload --app-dir src
```
**bash**
```bash
uvicorn agent_cv.main:app --reload --app-dir src
```

6. Ingest files:

**pwsh**
```pwsh
Invoke-RestMethod -Method POST -Uri http://localhost:8000/admin/ingest -ContentType "application/json" -Body (@{ max_files = 200 } | ConvertTo-Json -Compress)
```
**bash**
```bash
curl -s -X POST http://localhost:8000/admin/ingest \
  -H "Content-Type: application/json" \
  -d '{"max_files": 200}'
```

7. Query:

**pwsh**
```pwsh
Invoke-RestMethod -Method POST -Uri http://localhost:8000/query -ContentType "application/json; charset=utf-8" -Body (@{ query = "Quem tem certificações para armazenamento em nuvem?"; language = "pt" } | ConvertTo-Json -Compress)
```
**bash**
```bash
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"query": "Quem tem certificações para armazenamento em nuvem?", "language": "pt"}'
```

## Architecture

```
Teams user
  └─▶ Microsoft Graph API
        └─▶ GraphPollingBot (teams/agent.py)
              └─▶ handle_user_query (services/agent_service.py)
                    └─▶ OpenAI tool-calling loop
                          ├─▶ search_certifications  ──▶ PostgreSQL certifications table + pgvector
                          ├─▶ search_experience      ──▶ pgvector CV chunks
                          ├─▶ get_employee_profile   ──▶ PostgreSQL employees + certifications
                          ├─▶ list_employees         ──▶ PostgreSQL employees
                          └─▶ search_web             ──▶ DuckDuckGo API
```

## Notes

- The bot authenticates as the service account `GRAPH_USER_EMAIL` using MSAL ROPC flow. MFA/Conditional Access must not apply to this account.
- The agent always calls tools before answering — it never guesses from training data.
- Responses are in the same language as the user query (English or Portuguese).
- The `response_service.py` module is retained for reference but is no longer used in the main query path.
- CV translation to Europass format is planned for a future step.
