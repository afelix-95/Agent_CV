# Agent CV (MVP)

PostgreSQL-first backend for querying employee certifications and CV information from documents in `PDFs/REPOCV`, with Teams chatbot endpoint scaffolding.

## What is implemented

- PostgreSQL schema aligned with the architecture proposal (`sql/001_init.sql`)
- FastAPI service with endpoints:
  - `GET /health`
  - `POST /admin/init-db`
  - `POST /admin/ingest`
  - `POST /query`
  - `POST /teams/messages`
- Local ingestion pipeline:
  - Scans `PDFs/REPOCV`
  - Deduplicates by SHA-256
  - Parses filename metadata for employee/cert hints
  - Extracts PDF/TXT text snapshots
- Deterministic query MVP support:
  - expired certifications
  - Dell certifications
  - storage-related certifications (inferred via term expansion)

## Quick start

1. Create virtual environment and install dependencies.
2. Copy `.env.example` to `.env` and set `POSTGRES_DSN`.
3. Ensure PostgreSQL has `pgvector` extension available.
4. Run DB schema initialization:

```bash
python -m scripts.init_db
```

5. Start API:

```bash
uvicorn agent_cv.main:app --reload --app-dir src
```

6. Ingest files:

```bash
Invoke-RestMethod -Method POST -Uri http://localhost:8000/admin/ingest -ContentType "application/json" -Body (@{ max_files = 200 } | ConvertTo-Json -Compress)
```

7. Query:

```bash
Invoke-RestMethod -Method POST -Uri http://localhost:8000/query -ContentType "application/json; charset=utf-8" -Body (@{ query = "Quem tem certificações para armazenamento em nuvem?"; language = "pt" } | ConvertTo-Json -Compress)
```

## Notes

- The Teams endpoint currently accepts generic JSON payload and returns a simple message payload. Bot Framework signing/auth should be added before production.
- CV translation to Europass format is planned for the next step.
- Semantic embeddings/chunking pipeline is scaffolded at schema level; embedding generation will be implemented next.
