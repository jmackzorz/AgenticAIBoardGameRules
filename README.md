# BGG Research Agent

An AI-powered board game research tool built on [BoardGameGeek](https://boardgamegeek.com) data. It has two independently deployable pieces: a serverless chat API backed by Claude, and a local pipeline that converts board game rulebook PDFs into a searchable, embedded knowledge base.

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│  Lambda API  (packages/lambda_handler)                       │
│                                                             │
│  HTTP POST /chat                                            │
│       ↓                                                     │
│  handler.py  ──  DynamoDB (session history, 24h TTL)        │
│       ↓                                                     │
│  BggAgent  ──  Claude Sonnet (agentic tool-use loop)        │
│       ↓                                                     │
│  BGG XML API v2  (search / details / hot list)              │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Ingestion Pipeline  (packages/ingestion)                    │
│                                                             │
│  BGG file listings  →  Playwright download  →  PDF          │
│       ↓                                                     │
│  marker-pdf  →  Markdown                                    │
│       ↓                                                     │
│  Heading-based chunker  →  Chunk[]                          │
│       ↓                                                     │
│  Claude Haiku (topic tags + chunk_type)                     │
│  Voyage AI (1024-dim embeddings)                            │
│       ↓                                                     │
│  GameIndex JSON  +  .npy embeddings file                    │
│       ↓                                                     │
│  Streamlit review UI                                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Lambda API

### What it does

Exposes a stateful, multi-turn chat endpoint. Each request carries a `session_id`; conversation history is persisted in DynamoDB and replayed into Claude on every turn, so the agent remembers context across separate HTTP calls.

The agent runs a standard **tool-use loop**: Claude decides which BGG tools to call, the handler executes them, results are fed back, and Claude iterates until it produces a final answer.

### Tools available to the agent

| Tool | Description |
|---|---|
| `search_games` | Full-text search of the BGG catalog by name or keyword |
| `get_game_details` | Fetch ratings, player count, complexity, mechanics, and designers for up to 20 games in one call |
| `get_hot_games` | BGG's live trending list (top 50 by page views) |

### Key technical decisions

- **Prompt caching** — the system prompt is marked `cache_control: ephemeral`, cutting token costs on repeated turns within a session.
- **Connection reuse** — `anthropic.Anthropic` and `BggClient` (an `httpx` session) are initialized once at module load and reused across warm Lambda invocations.
- **History serialization** — Anthropic SDK content blocks (`ToolUseBlock`, `TextBlock`) are converted to plain dicts before DynamoDB storage via `model_dump()`, then deserialized transparently on load.
- **Secret auth** — requests must include `x-api-secret` header; missing or wrong secret returns 403 before any BGG or Claude calls are made.

### Deployment

```powershell
.\build_lambda.ps1          # Builds package/ directory with only Lambda deps
sam deploy --parameter-overrides ...
```

The build script explicitly excludes ingestion dependencies (`marker-pdf`, `torch`, `voyageai`, `streamlit`) — enforced at test time by `test_lambda_import_isolation.py`.

---

## Ingestion Pipeline

### What it does

Downloads English-language rulebook PDFs from BGG, converts them to structured chunks with embeddings, and writes per-game `GameIndex` JSON files ready for vector search.

### Pipeline stages

1. **Download** (`downloader.py`) — Navigates BGG's Cloudflare + React SPA protection using `curl_cffi` (Chrome TLS fingerprinting) for auth and a non-headless Playwright browser to render the file page and extract a time-limited AWS presigned download URL.

2. **Extract** (`extractor.py`) — Converts PDF to Markdown using `marker-pdf`. Output is cached; re-running skips already-extracted files.

3. **Chunk** (`chunker.py`) — Splits Markdown on headings (`#`/`##`/`###`), targeting 100–500 tokens per chunk. Merges siblings under 60 tokens, never splits lists or tables, and falls back to 400-token sliding windows when no headings are detected.

4. **Enrich** (`enricher.py`) — Sends chunks in batches of 20 to **Claude Haiku** for topic tagging (3–5 tags) and chunk type classification (`rules` / `setup` / `variant` / `reference`). Then embeds all chunk text with **Voyage AI** (`voyage-3`) and saves vectors as a `.npy` float32 array.

5. **QA** (`qa.py`) — Applies automated quality gates and writes flags to `GameIndex.qa_flags`. Flagged games surface in the Streamlit review UI.

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

### Streamlit viewer

```bash
streamlit run packages/ingestion/viewer/app.py
```

Loads chunk JSON and PDF side-by-side for manual review of chunking quality and QA flags.

---

## Shared library (`bgg_shared`)

Both packages depend on a shared library that neither can modify without affecting the other:

| Module | Purpose |
|---|---|
| `bgg.py` | BGG XML API v2 client (`BggClient`) — search, details, hot list |
| `models.py` | Pydantic models for BGG API responses (`BoardGame`, `SearchResult`) |
| `schema.py` | Pipeline models (`Chunk`, `GameIndex`) |
| `embedder.py` | Voyage AI embedding wrapper with batching |
| `retry.py` | `@with_retry` decorator — exponential backoff for flaky external calls |

---

## Tech stack

| Layer | Technology |
|---|---|
| AI / LLM | Claude Sonnet 4.6 (agent), Claude Haiku 4.5 (tagging) |
| Embeddings | Voyage AI `voyage-3` |
| Serverless | AWS Lambda + SAM, DynamoDB |
| BGG client | `httpx` (BGG XML API), `curl_cffi` + Playwright (downloads) |
| PDF extraction | `marker-pdf` |
| Data models | Pydantic v2 |
| CLI | Typer |
| Review UI | Streamlit |
| Tests | pytest (36 tests) |

---

## Project layout

```
packages/
  shared/bgg_shared/        # Shared library
  lambda_handler/           # AWS Lambda API
    bgg_lambda/
      handler.py            # Entry point, DynamoDB session management
      agent.py              # BggAgent — agentic loop
      tools/                # Tool schemas + handlers
  ingestion/                # Local batch pipeline
    bgg_ingestion/
      downloader.py         # BGG PDF downloader (Cloudflare bypass)
      extractor.py          # PDF → Markdown
      chunker.py            # Markdown → Chunk[]
      enricher.py           # Claude Haiku tagging + Voyage embeddings
      qa.py                 # QA gates
      cli.py                # Typer CLI
    viewer/app.py           # Streamlit review UI
tests/
  test_chunker.py           # 22 tests
  test_qa.py                # 14 tests
  test_lambda_import_isolation.py
```

---

## Setup

**Prerequisites:** Python 3.11+, AWS credentials (Lambda deployment), `playwright install chromium` (ingestion only).

```bash
# Install all deps
pip install -e packages/shared
pip install -e packages/lambda_handler
pip install -e packages/ingestion

# Copy and fill in env vars
cp .env.example .env

# Run tests
python -m pytest tests/ -v
```

Required environment variables: `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `SESSIONS_TABLE`, `API_SECRET`, `BGG_USERNAME`, `BGG_PASSWORD`.
