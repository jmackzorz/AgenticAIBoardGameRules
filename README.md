# BGG Research Agent

An AI-powered board game research tool built on [BoardGameGeek](https://boardgamegeek.com) data. Two independently deployable pieces: a serverless chat API backed by Claude, and a local pipeline that converts board game rulebook PDFs into a searchable, embedded knowledge base.

**Agent API:** POST /invocations → AgentCore Memory (session history) → BggAgent → Claude Sonnet on Bedrock → BGG XML API v2

**Ingestion pipeline:** BGG file listings → Playwright download → marker-pdf → chunker → Claude Haiku (tagging) + Voyage AI (embeddings) → GameIndex JSON → Streamlit viewer

## Agent API

Stateful multi-turn chat hosted on Amazon Bedrock AgentCore Runtime. Each request carries a Runtime session ID; conversation history is persisted as AgentCore Memory short-term events and replayed into Claude on every turn. Each session runs in its own microVM, and turns may run up to 8 hours.

The agent runs a tool-use loop — Claude decides which BGG tools to call, the entrypoint executes them, results feed back in, and Claude iterates until it has an answer. The system prompt is marked `cache_control: ephemeral` to cut token costs on repeated turns.

The agent has three tools: `search_games` (full-text BGG catalog search), `get_game_details` (ratings, player count, complexity, mechanics, designers — up to 20 games per call), and `get_hot_games` (BGG's live top-50 trending list). Callers authenticate with SigV4; there is no shared secret and no Anthropic API key anywhere in the deployment.

### Deployment

```powershell
npm install -g @aws/agentcore
agentcore add memory --name bgg_agent_memory
agentcore deploy
agentcore invoke --session-id my-session "What is Brass: Birmingham?"
```

Runtime requires an ARM64 container serving `POST /invocations` and `GET /ping` on port 8080; `BedrockAgentCoreApp` implements both. The deployed artifact excludes ingestion dependencies (`marker-pdf`, `torch`, `voyageai`, `streamlit`) — enforced at test time by `test_agentcore_import_isolation.py`.

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

Claude Sonnet 4.6 via Amazon Bedrock (agent loop), Claude Haiku 4.5 (batch tagging), Voyage AI `voyage-3` (embeddings), Bedrock AgentCore Runtime + Memory, `marker-pdf`, Pydantic v2, Typer, Streamlit, pytest.

## Setup

Python 3.11+, AWS credentials with Bedrock model access, `playwright install chromium` for ingestion.

```bash
pip install -e packages/shared
pip install -e packages/agentcore
pip install -e packages/ingestion

cp .env.example .env

python -m pytest tests/ -v
```

Required env vars: `AWS_REGION` and `MEMORY_ID` (agent); `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY` (ingestion); `BGG_USERNAME` and `BGG_PASSWORD` (rulebook downloads).
