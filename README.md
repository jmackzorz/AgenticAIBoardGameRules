# BGG Research Agent

An AI-powered board game research tool built on [BoardGameGeek](https://boardgamegeek.com) data. Two independently deployable pieces: a serverless chat API backed by Claude, and a local pipeline that converts board game rulebook PDFs into a searchable, embedded knowledge base.

**Lambda API:** HTTP POST /chat → DynamoDB (session history) → BggAgent → Claude Sonnet → BGG XML API v2

**Ingestion pipeline:** BGG file listings → Playwright download → marker-pdf → chunker → Claude Haiku (tagging) + Voyage AI (embeddings) → GameIndex JSON → Streamlit viewer

## Lambda API

Stateful multi-turn chat endpoint. Each request carries a `session_id`; conversation history is persisted in DynamoDB and replayed into Claude on every turn.

The agent runs a tool-use loop — Claude decides which BGG tools to call, the handler executes them, results feed back in, and Claude iterates until it has an answer. The system prompt is cached (`cache_control: ephemeral`) to cut token costs on repeated turns. The Anthropic client and BGG HTTP session are initialized once at module load so warm Lambda invocations reuse connections.

The agent has three tools: `search_games` (full-text BGG catalog search), `get_game_details` (ratings, player count, complexity, mechanics, designers — up to 20 games per call), and `get_hot_games` (BGG's live top-50 trending list). Requests require an `x-api-secret` header; missing or wrong value returns 403 before any BGG or Claude calls are made.

### Deployment

```powershell
.\build_lambda.ps1          # Builds package/ directory with only Lambda deps
sam deploy --parameter-overrides ...
```

The build script explicitly excludes ingestion dependencies (`marker-pdf`, `torch`, `voyageai`, `streamlit`) — enforced at test time by `test_lambda_import_isolation.py`.

## Ingestion Pipeline

Downloads English-language rulebook PDFs from BGG, converts them to structured chunks with embeddings, and writes per-game `GameIndex` JSON files.

1. **Download** (`downloader.py`) — BGG file pages sit behind Cloudflare and a React SPA, so this uses `curl_cffi` (Chrome TLS fingerprinting) for auth and a non-headless Playwright browser to render the file listing and extract the time-limited AWS presigned download URL.

2. **Extract** (`extractor.py`) — PDF → Markdown via `marker-pdf`. Output is cached; re-runs skip already-extracted files.

3. **Chunk** (`chunker.py`) — Splits on headings (`#`/`##`/`###`), targeting 100–500 tokens per chunk. Siblings under 60 tokens get merged. Fallback to 400-token sliding windows for PDFs with no heading structure.

4. **Enrich** (`enricher.py`) — Batches of 20 chunks go to Claude Haiku for topic tags (3–5) and chunk type classification (`rules`/`setup`/`variant`/`reference`). Then Voyage AI embeds everything and saves vectors as a `.npy` float32 array.

5. **QA** (`qa.py`) — Quality gates write flags to `GameIndex.qa_flags`. Flagged games surface in the Streamlit viewer.

### CLI

```bash
# Download rulebooks for specific games
python -m bgg_ingestion.cli download 178900 266192

# Download the top 500 BGG-ranked games (~3-6 hours, resumable)
python -m bgg_ingestion.cli download-top

# Index all PDFs
python -m bgg_ingestion.cli index data/pdfs/ --output data/chunks/ --resume

# Review QA flags
python -m bgg_ingestion.cli qa data/chunks/

# Inspect chunks for a game
python -m bgg_ingestion.cli inspect 178900 --limit 5
```

```bash
streamlit run packages/ingestion/viewer/app.py
```

Loads chunk JSON and PDF side-by-side for manual review of chunking quality and QA flags.

## Shared library (`bgg_shared`)

Both packages depend on this — `bgg.py` (BGG XML API client), `models.py` (BGG API dataclasses), `schema.py` (`Chunk`/`GameIndex` Pydantic models), `embedder.py` (Voyage AI wrapper), and `retry.py` (exponential backoff decorator).

## Stack

Claude Sonnet 4.6 (agent loop), Claude Haiku 4.5 (batch tagging), Voyage AI `voyage-3` (embeddings), AWS Lambda + SAM + DynamoDB, `marker-pdf`, Pydantic v2, Typer, Streamlit, pytest.

## Setup

Python 3.11+, AWS credentials for Lambda deployment, `playwright install chromium` for ingestion.

```bash
pip install -e packages/shared
pip install -e packages/lambda_handler
pip install -e packages/ingestion

cp .env.example .env

python -m pytest tests/ -v
```

Required env vars: `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `SESSIONS_TABLE`, `API_SECRET`, `BGG_USERNAME`, `BGG_PASSWORD`.
