# talent-engine

AI infrastructure powering a recruiting platform. Today: resume ↔ job-description matching via Gemini. Designed to grow to additional talent operations (parsing, ranking, screening).

Extracted from the `mnexa-ai` monorepo's `api/v1/resume_matching/` module. The folder layout intentionally mirrors mnexa-ai (`api/v1/...`) so code can be diffed and ported back if needed.

## Layout

```
api/
  main.py                      # FastAPI entrypoint, mounts routers
  v1/
    db/database.py             # async + sync engines, init_db, close_db
    routers/deps.py            # get_engine FastAPI dependency
    resume_matching/           # the matching service
      pipeline.py              # parse + score orchestration
      router.py                # streaming UI endpoint
      public_router.py         # API-key gated JSON API
      auth.py, rate_limit.py, notify.py, public_schema.py
      baml_src/                # BAML prompts (Gemini)
      baml_client/             # GENERATED — run `baml-cli generate`
      storage/                 # api_keys + usage tables (own MetaData)
      tests/                   # pytest, sqlite in-memory
      scripts/                 # operator CLI (create_api_key, demo, smoke test)
  requirements.txt
  requirements-dev.txt
  pyproject.toml
  Dockerfile
```

## Local setup

```bash
cd api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Generate the BAML client (gitignored — must regen after editing .baml)
cd v1/resume_matching && baml-cli generate && cd ../..

# Start a local Postgres (or set DATABASE_URL to your own)
# docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=talent_engine postgres:15

cp ../.env.example ../.env  # edit values
uvicorn main:app --reload
```

## Tests

```bash
cd api && pytest
```

Tests use SQLite in-memory and stub the BAML client — no Postgres or LLM credentials required.

## China deployment notes

The default BAML client targets Gemini via Vertex AI (`us-central1`), with Google AI Studio as fallback. Both are unreachable from mainland China. To deploy domestically:

1. Edit `v1/resume_matching/baml_src/main.baml` to add a domestic model client (Qwen via DashScope, Doubao via Volcengine, DeepSeek, GLM, or Kimi). BAML's `openai-generic` provider works for most.
2. Re-run `baml-cli generate`.
3. Re-run the eval set in `scripts/test_public_api.py` to confirm match-quality regression is acceptable.

Recommended cloud target for mainland: Aliyun Function Compute (FC) or Serverless App Engine (SAE) — Cloud Run-equivalents that accept this Dockerfile directly. Aliyun also hosts the Qwen LLM, keeping vendor count low. ICP filing is required to serve from a mainland IP under a domain name.
