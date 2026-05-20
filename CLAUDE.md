# Project: BGG Board Game Research Agent

## Overview
A board game research tool that exposes BGG (BoardGameGeek) data through an agentic Claude AI via AWS Lambda, paired with a local ingestion pipeline that converts rulebook PDFs into searchable, embedded chunks. Built as a Python portfolio project demonstrating agentic AI, cloud deployment, and data pipeline patterns.

## Stack
- **Language:** Python 3.11+
- **Runtime:** Python 3.11 (Lambda), local venv (ingestion)
- **Framework:** AWS SAM (Lambda), Typer (CLI), Streamlit (viewer UI)
- **Database:** DynamoDB (Lambda session storage, 24-hour TTL)
- **ORM/Query:** boto3 direct (no ORM)
- **Auth:** `API_SECRET` header on Lambda requests; BGG login via curl_cffi session
- **Testing:** pytest
- **Package mgr:** uv (`pyproject.toml` with optional dependency groups)
- **CI/CD:** None — Lambda deployed manually via `sam deploy`

## Project Structure
```
packages/
  shared/bgg_shared/        # Shared library — imported by both Lambda and ingestion
  lambda_handler/           # AWS Lambda artifact — strict import isolation enforced
    bgg_lambda/
      handler.py            # SAM entry point; has module-level AWS calls (not testable without creds)
      agent.py              # BggAgent — stateful agentic loop using Claude Sonnet
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
- **Strict Lambda/ingestion isolation**: `bgg_lambda` must never import `marker`, `torch`, `streamlit`, `voyageai`, `tiktoken`, or `langdetect`. Enforced by `test_lambda_import_isolation.py` and a separate `build_lambda.ps1` that installs Lambda deps explicitly.
- **curl_cffi over httpx/requests for BGG downloads**: BGG is behind Cloudflare managed challenge. Standard clients get 403. `curl_cffi` impersonates Chrome TLS fingerprinting and is the only client that works.
- **Non-headless Playwright for BGG file pages**: The download URL only exists after the React SPA renders. Headless Chromium is blocked by Cloudflare's JS challenge — the browser must be visible.
- **Voyage AI embeddings stored as `.npy` files**: Flat-file storage (one per `bgg_id`) rather than a vector database. Simple and sufficient for the current dataset size.
- **Claude Haiku for topic tagging, Sonnet for the agent**: Haiku is ~$0.01/rulebook for tagging 3-5 topics per chunk. Sonnet handles the interactive agentic loop in Lambda.
- **Prompt caching on the system prompt**: `BggAgent.chat()` sends the system prompt with `"cache_control": {"type": "ephemeral"}` to reduce per-turn cost.
- **DynamoDB for session persistence**: Lambda is stateless; conversation history is serialized to JSON and stored in DynamoDB with a 24-hour TTL. `handler.py` deserializes on cold start.
- **BGG XML API v2**: Public API (`boardgamegeek.com/xmlapi2`). No BGG API key required for basic search/detail/hot endpoints. API returns 202 when results aren't cached yet — `BggClient._get_xml()` retries with backoff.
- **Monorepo with `pyproject.toml` optional groups**: `[ingestion]`, `[lambda]`, `[cli]`, `[dev]` — keeps Lambda artifact small and ingestion deps (marker-pdf, torch, voyageai) out of Lambda.

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
- `test_lambda_import_isolation.py` — imports `bgg_lambda` and asserts none of the banned ingestion packages are transitively loaded
- `handler.py` **cannot be imported in tests** without live AWS credentials — it has module-level `boto3` and `os.environ["SESSIONS_TABLE"]` calls. Write tests against `agent.py` and `BggClient` directly, injecting mock clients.
- Chunker test paragraphs must exceed 60 tokens (`_MIN_TOKENS = 60`) to avoid unexpected merges invalidating assertions.
- No UI component tests; no need to hit the real BGG API in unit tests.

## Dependencies

**Approved (core):**
- `anthropic>=0.50.0` — Claude API (Haiku tagging + Sonnet agent)
- `httpx` — BGG XML API HTTP client
- `pydantic>=2.0` — data models and validation
- `boto3` — AWS DynamoDB + Lambda (Lambda package only)
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

**Off-limits in Lambda:** `marker`, `torch`, `streamlit`, `voyageai`, `tiktoken`, `langdetect`

**Avoid:** `requests` (use `httpx` or `curl_cffi` depending on context), `moment.js`-style date libs, adding new top-level dependencies without discussion

## Off-Limits Areas
- Do not add ingestion dependencies to `packages/lambda_handler/` or `build_lambda.ps1`
- Do not modify `requirements.txt` manually — it is generated by `uv export`
- Do not import `handler.py` in tests (module-level AWS calls will raise)
- Do not modify `data/` contents — it is gitignored runtime data
- Do not change `samconfig.toml` without discussing Lambda deployment parameters first
- Do not refactor outside the scope of the current task

## Current Focus
PDF audit pipeline — auditing ~497 downloaded rulebook PDFs. ~223 are flagged as suspicious (wrong file type, e.g. solo rules or quick-reference cards). The `refetch` command (searches `cdn.1j1ju.com`) has a false-positive name-match fix applied as of 2026-05-12. Next steps: run refetch on the suspicious list, manually review games not found on 1j1ju, delete 4 confirmed bad PDFs and re-download via `download-top --resume`, then re-run `scan` to verify.

## Known Issues / Tech Debt
- **`pick_rulebook()` is too naive** — sometimes picks solo-mode or variant rulebooks over the full game rulebook. A tier-based scoring fix is designed (see CLAUDE.md "Planned fix") but not yet implemented.
- **`handler.py` untestable without AWS** — module-level DynamoDB and `os.environ` calls make unit testing the Lambda entry point impossible without live credentials or significant mocking.
- **No CI/CD** — all deployments are manual via `sam deploy`. No automated test runner on push.
- **BGG download is slow** — the Playwright + curl_cffi flow takes ~15-30 seconds per game due to the 8-second React render wait and Cloudflare delays. `download-top` for 500 games takes 3-6 hours.
- **Scan command `--fetch-bgg` populates a 37-field BGG metadata cache** (`data/bgg_cache/<bggId>.json`). Old partial cache entries lack the `"artists"` key — used as a sentinel to detect stale entries.
