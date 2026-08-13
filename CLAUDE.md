# Project: BGG Board Game Research Agent

## Overview
A board game research tool that exposes BGG (BoardGameGeek) data through an agentic Claude AI hosted on Amazon Bedrock AgentCore Runtime, paired with a local ingestion pipeline that converts rulebook PDFs into searchable, embedded chunks. Built as a Python portfolio project demonstrating agentic AI, cloud deployment, and data pipeline patterns.

## Stack
- **Language:** Python 3.11+
- **Runtime:** Bedrock AgentCore Runtime (ARM64, container or CodeZip), local venv (ingestion)
- **Framework:** `bedrock-agentcore` SDK (agent host), Typer (CLI), Streamlit (viewer UI)
- **Model provider:** Amazon Bedrock (`AnthropicBedrockMantle`) — no Anthropic API key
- **Database:** AgentCore Memory short-term events (conversation history)
- **ORM/Query:** boto3 direct (no ORM)
- **Auth:** SigV4 on the Runtime endpoint; BGG login via curl_cffi session
- **Testing:** pytest
- **Package mgr:** uv (`pyproject.toml` with optional dependency groups)
- **CI/CD:** None — deployed manually via the `agentcore` CLI

## Project Structure
```
packages/
  shared/bgg_shared/        # Shared library — imported by both the agent and ingestion
  agentcore/                # AgentCore Runtime artifact — strict import isolation enforced
    bgg_agentcore/
      app.py                # Runtime entry point (@app.entrypoint, POST /invocations)
      agent.py              # BggAgent — stateful agentic loop using Claude Sonnet via Bedrock
      sessions.py           # MemorySessionStore — AgentCore Memory short-term events
      tools/                # Claude tool definitions + dispatch handlers
  ingestion/                # Local-only batch pipeline — never referenced by Lambda
    bgg_ingestion/
      cli.py                # Typer CLI (download / download-top / index / qa / reembed / inspect / scan / refetch)
      downloader.py         # BGG rulebook downloader (curl_cffi + Playwright)
      extractor.py          # PDF → markdown via marker-pdf (cached)
      chunker.py            # Markdown → Chunk list (heading-based with fallback windowing)
      enricher.py           # Claude Haiku topic tagging + Voyage AI embeddings
      qa.py                 # QA gates → GameIndex.qa_flags
    viewer/app.py           # Streamlit review UI
data/                       # Gitignored runtime data (pdfs/, markdown/, chunks/, embeddings/, bgg_cache/)
tests/                      # pytest test suite (flat, not mirroring src)
```

## Architecture Decisions
- **Strict agent/ingestion isolation**: `bgg_agentcore` must never import `marker`, `torch`, `streamlit`, `voyageai`, `tiktoken`, or `langdetect`. Enforced by `test_agentcore_import_isolation.py`. AgentCore images have no 250 MB Lambda ceiling, but `torch` would add gigabytes to every image pull and cold start — the constraint survives the platform change.
- **Bedrock over the direct Anthropic API**: `AnthropicBedrockMantle` (the Messages-API Bedrock path, not legacy `InvokeModel`). Credentials come from the ambient AWS chain, so there is no `ANTHROPIC_API_KEY` to store, rotate, or pass through deploy parameters. Model IDs carry the `anthropic.` prefix.
- **curl_cffi over httpx/requests for BGG downloads**: BGG is behind Cloudflare managed challenge. Standard clients get 403. `curl_cffi` impersonates Chrome TLS fingerprinting and is the only client that works.
- **Non-headless Playwright for BGG file pages**: The download URL only exists after the React SPA renders. Headless Chromium is blocked by Cloudflare's JS challenge — the browser must be visible.
- **Voyage AI embeddings stored as `.npy` files**: Flat-file storage (one per `bgg_id`) rather than a vector database. Simple and sufficient for the current dataset size.
- **Claude Haiku for topic tagging, Sonnet for the agent**: Haiku is ~$0.01/rulebook for tagging 3-5 topics per chunk. Sonnet handles the interactive agentic loop in Lambda.
- **Prompt caching on the system prompt**: `BggAgent.chat()` sends the system prompt with `"cache_control": {"type": "ephemeral"}` to reduce per-turn cost.
- **AgentCore Memory for session persistence**: conversation history is stored as short-term events keyed by `(memoryId, actorId, sessionId)`, where `sessionId` is the Runtime session ID. Turns are written as `blob` payloads, not `conversational` ones — a turn may contain `tool_use`/`tool_result` blocks that do not survive being flattened into the `{role, content-string}` shape `conversational` expects. One event per turn (payload caps at 100 items, so `MemorySessionStore.append` chunks beyond that).
- **BGG XML API v2**: Public API (`boardgamegeek.com/xmlapi2`). No BGG API key required for basic search/detail/hot endpoints. API returns 202 when results aren't cached yet — `BggClient._get_xml()` retries with backoff.
- **Monorepo with `pyproject.toml` optional groups**: `[ingestion]`, `[agentcore]`, `[cli]`, `[dev]` — keeps the deployed agent artifact small and ingestion deps (marker-pdf, torch, voyageai) out of it.

## Coding Conventions
- **Naming**: `snake_case` for variables/functions/modules, `PascalCase` for classes, `UPPER_SNAKE_CASE` for module-level constants
- **Private scope**: Module-level private symbols and instance helpers prefixed with `_` (e.g., `_MODEL`, `_get_xml`, `_run_tool_calls`)
- **Type hints**: Required on all function signatures; use `X | Y` union syntax (Python 3.10+ style), not `Optional[X]`
- **Context managers**: Stateful clients (`BggClient`, `BggAgent`) implement `__enter__`/`__exit__` and a `close()` method
- **Docstrings**: Public methods get a one-liner or short Google-style docstring with `Args:` if non-obvious. Private helpers get none.
- **Comments**: Explain WHY (hidden constraints, non-obvious invariants, known BGG behavior). No inline comments describing what the code does.
- **Models**: Pydantic v2 for all data models (`bgg_shared/models.py`, `bgg_shared/schema.py`)
- **Line length**: No enforced limit, but keep lines readable

## Testing Expectations
- Run with: `.venv\Scripts\python.exe -m pytest tests/ -v`
- `test_chunker.py` — 22 tests covering chunker internals and the public `chunk()` function
- `test_qa.py` — 14 tests, one synthetic trigger per QA gate
- `test_agentcore_import_isolation.py` — imports `bgg_agentcore`, `.agent`, and `.sessions` and asserts none of the banned ingestion packages are transitively loaded
- `test_sessions.py` — 11 tests covering `MemorySessionStore` against a fake boto3 client; this is what pins the AgentCore Memory call shape
- `app.py` **is** importable without AWS config — `MemorySessionStore` is built lazily behind `_get_store()`. Keep it that way; module-level `os.environ[...]` is what made the old Lambda handler untestable.
- Inject mock clients rather than reaching for network: `BggAgent(anthropic_client=..., bgg_client=...)` and `MemorySessionStore(memory_id, client=...)`.
- Chunker test paragraphs must exceed 60 tokens (`_MIN_TOKENS = 60`) to avoid unexpected merges invalidating assertions.
- No UI component tests; no need to hit the real BGG API in unit tests.

## Dependencies

**Approved (core):**
- `anthropic>=0.50.0` — Claude SDK (Haiku tagging via API key; Sonnet agent via Bedrock)
- `httpx` — BGG XML API HTTP client
- `pydantic>=2.0` — data models and validation
- `boto3` — AgentCore Memory data plane (agent package only)
- `bedrock-agentcore` — Runtime host / `@app.entrypoint` (agent package only)
- `python-dotenv` — `.env` loading
- `rich` — CLI output formatting

**Approved (ingestion only — never add to Lambda):**
- `marker-pdf` — PDF → markdown conversion
- `voyageai` — Voyage AI embeddings
- `numpy` — embedding storage (`.npy` files)
- `tiktoken` — token counting for chunking
- `langdetect` — language filtering
- `typer` — CLI framework
- `streamlit`, `streamlit-pdf-viewer` — viewer UI
- `curl-cffi>=0.15.0` — Cloudflare-bypassing HTTP for BGG downloads
- `playwright>=1.59.0` — Chromium automation for BGG file page rendering
- `pypdf>=6.11.0` — PDF auditing in the `scan` command
- `duckduckgo-search`, `ddgs` — rulebook search in the `refetch` command

**Off-limits in the agent package:** `marker`, `torch`, `streamlit`, `voyageai`, `tiktoken`, `langdetect`

**Avoid:** `requests` (use `httpx` or `curl_cffi` depending on context), `moment.js`-style date libs, adding new top-level dependencies without discussion

## Off-Limits Areas
- Do not add ingestion dependencies to `packages/agentcore/`
- Do not modify `requirements.txt` manually — it is generated by `uv export`
- Do not reintroduce module-level `os.environ[...]` or AWS client construction in `app.py`
- Do not modify `data/` contents — it is gitignored runtime data
- Do not change AgentCore deployment configuration without discussing it first
- Do not refactor outside the scope of the current task

## Current Focus
PDF audit pipeline — auditing ~497 downloaded rulebook PDFs. ~223 are flagged as suspicious (wrong file type, e.g. solo rules or quick-reference cards). The `refetch` command (searches `cdn.1j1ju.com`) has a false-positive name-match fix applied as of 2026-05-12. Next steps: run refetch on the suspicious list, manually review games not found on 1j1ju, delete 4 confirmed bad PDFs and re-download via `download-top --resume`, then re-run `scan` to verify.

## Known Issues / Tech Debt
- **`pick_rulebook()` is too naive** — sometimes picks solo-mode or variant rulebooks over the full game rulebook. A tier-based scoring fix is designed (see CLAUDE.md "Planned fix") but not yet implemented.
- **AgentCore Memory call shapes are unverified against live AWS** — `MemorySessionStore` is written to the documented `CreateEvent`/`ListEvents` contract and covered by unit tests with a fake client, but has not yet run against a real memory resource. `ListEvents` ordering is not contractual, so `load()` sorts by `eventTimestamp` defensively.
- **No CI/CD** — all deployments are manual via the `agentcore` CLI. No automated test runner on push.
- **`.venv` is orphaned** — its base interpreter (Python 3.11) is gone from this machine; system Python is 3.14. Recreate before running the documented pytest command.
- **BGG download is slow** — the Playwright + curl_cffi flow takes ~15-30 seconds per game due to the 8-second React render wait and Cloudflare delays. `download-top` for 500 games takes 3-6 hours.
- **Scan command `--fetch-bgg` populates a 37-field BGG metadata cache** (`data/bgg_cache/<bggId>.json`). Old partial cache entries lack the `"artists"` key — used as a sentinel to detect stale entries.
