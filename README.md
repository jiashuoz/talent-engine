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

# Start a local MySQL (or set DATABASE_URL to your own)
# docker run -d -p 3306:3306 \
#   -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=talent_engine \
#   -e MYSQL_USER=talent_engine -e MYSQL_PASSWORD=talent_engine \
#   mysql:8.0

cp ../.env.example ../.env  # edit values
uvicorn main:app --reload
```

## Tests

```bash
cd api && pytest
```

Tests use SQLite in-memory and stub the BAML client — no MySQL or LLM credentials required.

## LLM provider selection

The `LLM_PROVIDER` env var picks which client BAML routes through at runtime — same image, different provider. Supported values:

| `LLM_PROVIDER` | Hosted by | Endpoint | Why pick |
|---|---|---|---|
| `Gemini` (default) | Google | Vertex AI / AI Studio | Original. Best for non-China deployments. |
| `Qwen` | Aliyun DashScope | OpenAI-compatible | Strong Chinese, mature API. |
| `Hunyuan` | Tencent | OpenAI-compatible | Best vendor consolidation if hosted on Tencent Cloud. |
| `DeepSeek` | DeepSeek | OpenAI-compatible | Cheapest per token; text-only. |

Wired through `baml_options={"client": LLM_PROVIDER}` at every BAML call site — see [llm_config.py](api/v1/resume_matching/llm_config.py) and [main.baml](api/v1/resume_matching/baml_src/main.baml). Unknown values fail loud at first request.

Only set the API key for the active provider — leaving the others unset is fine. If you're switching providers mid-flight, re-run your eval set: structured-extraction quality varies meaningfully across models, and the prompts in `baml_src/` were originally tuned against Gemini.

## China deployment

For WeChat mini-program partners, deploy on **Tencent Cloud 微信云托管 (Cloud Hosting)**. See [DEPLOY-tencent.md](DEPLOY-tencent.md) for the full runbook. The killer feature: mini-programs can call your API without configuring a request-domain whitelist, removing the biggest friction in partner onboarding.

PDF text extraction happens in Python via [pypdf](https://pypi.org/project/pypdf/) before any LLM call so the same code path works for all four providers — Qwen / Hunyuan / DeepSeek don't all support PDF input via OpenAI-compatible endpoints. Trade-off: scanned/image-only PDFs aren't supported (they surface as `PdfExtractionError`); add Tencent OCR or PaddleOCR upstream if you need them.
