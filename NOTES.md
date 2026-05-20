# CLAUDE.md — BGG Monorepo

## What this repo is

A board game research tool with two main pieces:
1. **AWS Lambda** — exposes BGG (BoardGameGeek) API functionality to an agentic Claude AI via HTTP
2. **Local ingestion pipeline** — converts board game rulebook PDFs into searchable, embedded chunks

## Monorepo layout

```
packages/
  shared/bgg_shared/        # Shared library (Lambda + ingestion both depend on this)
    bgg.py                  # BGG XML API v2 client (BggClient)
    models.py               # BGG API Pydantic models (BoardGame, SearchResult)
    schema.py               # Pipeline models (Chunk, GameIndex)
    embedder.py             # Voyage AI embeddings wrapper
    retry.py                # Exponential backoff decorator (@with_retry)

  lambda_handler/           # AWS Lambda — do NOT add ingestion deps here
    lambda_function.py      # SAM entry point (re-exports handler)
    bgg_lambda/
      handler.py            # Lambda handler (boto3 DynamoDB, anthropic SDK)
      agent.py              # BggAgent — agentic loop using Claude
      tools/
        definitions.py      # Tool schemas for Claude
        handlers.py         # Tool implementations

  ingestion/                # Local-only batch pipeline
    bgg_ingestion/
      downloader.py         # BGG rulebook downloader (see "BGG download architecture" below)
                            #   also contains get_top_rankings() for scraping BGG ranked list
      extractor.py          # PDF → markdown via marker-pdf (cached to data/markdown/)
      chunker.py            # Markdown → Chunk list (heading-based, with fallback windowing)
      enricher.py           # Claude Haiku topic tagging + Voyage embeddings
      qa.py                 # QA gates → GameIndex.qa_flags
      cli.py                # Typer CLI (download / download-top / index / qa / reembed / inspect commands)
    viewer/
      app.py                # Streamlit review UI

data/                       # gitignored runtime data
  pdfs/                     # Input: <bggId>.pdf
  markdown/                 # Cache: marker output per PDF
  chunks/                   # Output: <bggId>.json (GameIndex)
  embeddings/               # Output: <bggId>.npy (float32 vectors)
  bgg_cache/                # Cache: BGG API responses

tests/
  test_chunker.py           # 22 tests — chunker internals + public chunk()
  test_qa.py                # 14 tests — one synthetic trigger per QA gate
  test_lambda_import_isolation.py  # Ensures bgg_lambda never imports ingestion deps
```

## Key data models (bgg_shared/schema.py)

```python
class Chunk(BaseModel):
    chunk_id: str           # "{bgg_id}_{idx:03d}"
    bgg_id: int
    game_name: str
    section_path: list[str] # heading ancestry, e.g. ["Setup", "The Key"]
    page: int | None
    topics: list[str]       # 3-5 tags from Haiku
    summary: str
    chunk_type: str         # "rules" | "setup" | "variant" | "reference"
    token_count: int
    text: str
    embedding: list[float] | None  # excluded from JSON serialization

class GameIndex(BaseModel):
    bgg_id: int
    game_name: str
    source_pdf: str
    indexed_at: datetime
    chunks: list[Chunk]
    qa_flags: list[str]
```

## Environment variables (.env)

```
ANTHROPIC_API_KEY=...       # Used by: Lambda handler + ingestion enricher (Haiku tagging)
VOYAGE_API_KEY=...          # Used by: ingestion embedder (Voyage AI)
SESSIONS_TABLE=...          # Used by: Lambda handler (DynamoDB)
API_SECRET=...              # Used by: Lambda handler (request auth)
BGG_API_KEY=...             # Optional — BGG client (public API works without it)
BGG_USERNAME=...            # Used by: ingestion downloader (BGG account login)
BGG_PASSWORD=...            # Used by: ingestion downloader (BGG account login)
```

## How to run things

### Ingestion pipeline
```
# Download English rulebook PDFs from BGG (requires BGG_USERNAME + BGG_PASSWORD in .env)
# Opens a brief non-headless browser window per game (needed to bypass Cloudflare)
python -m bgg_ingestion.cli download 178900 266192 --resume

# Download rulebooks for the top 500 BGG-ranked games (skips already-downloaded by default)
# ~3-6 hours for a full run; safe to interrupt and re-run (resume is on by default)
python -m bgg_ingestion.cli download-top
python -m bgg_ingestion.cli download-top 100          # top 100 instead
python -m bgg_ingestion.cli download-top --delay 10.0 # longer delay between games

# Index all PDFs in data/pdfs/, skip already-indexed
python -m bgg_ingestion.cli index data/pdfs/ --output data/chunks/ --resume

# Check QA flags across all indexed games
python -m bgg_ingestion.cli qa data/chunks/

# Inspect chunks for a specific game
python -m bgg_ingestion.cli inspect 178900 --limit 5

# Re-run embeddings only (no re-tagging)
python -m bgg_ingestion.cli reembed 178900
```

### Streamlit viewer
```
streamlit run packages/ingestion/viewer/app.py
```
Run from repo root. Loads from data/chunks/ and data/pdfs/.

### Tests
```
.venv\Scripts\python.exe -m pytest tests/ -v
```

### Lambda deployment
```powershell
.\build_lambda.ps1          # Rebuilds package/ directory
sam deploy --parameter-overrides ...
```
Lambda dependencies are installed explicitly in `build_lambda.ps1` — never add ingestion deps (marker-pdf, torch, streamlit, voyageai) to the Lambda artifact.

## Important constraints

- **Lambda isolation**: `bgg_lambda` must never import `marker`, `torch`, `streamlit`, `voyageai`, `tiktoken`, or `langdetect`. Enforced by `test_lambda_import_isolation.py`.
- **handler.py has module-level AWS calls** (`boto3`, `os.environ["SESSIONS_TABLE"]`) — it cannot be imported in tests without AWS credentials.
- **Ingestion API costs**: `tag_chunks` calls Claude Haiku via API key (not Claude Pro). Haiku is cheap (~$0.01/rulebook) but it's not free.
- **Chunker merge threshold**: `_MIN_TOKENS = 60`. Sections under 60 tokens get merged with their neighbour. Test paragraphs must exceed 60 tokens to avoid unexpected merges.

## BGG download architecture

BGG rulebook downloads require navigating three layers of protection. This is already solved — do not rearchitect without reading this first.

### Why it's complex
- `boardgamegeek.com` is behind **Cloudflare managed challenge** — headless browsers get 403
- The filepage is a **React SPA** — the download URL only exists after JavaScript renders it
- The `api.geekdo.com/api/file/downloadurls` endpoint requires browser-held session state that cannot be replicated with a plain HTTP client (returns 403)

### What the BGG JSON API provides (no auth needed)
- `GET api.geekdo.com/api/files?objecttype=thing&objectid={bggId}&languageid=2184&sort=hot&pageid=1`
  Returns paginated file listings. `config.endpage` tells you total page count.
- Each file entry has: `fileid`, `filepageid`, `filename`, `title`, `numpositive`, `href` (`/filepage/{id}/slug`)
- **Does NOT include a download URL** — that requires authentication + the browser session

### The actual download flow
1. **`curl_cffi` session** (Chrome TLS impersonation) logs in via `POST boardgamegeek.com/login/api/v1` → gets `SessionID` cookie
2. **Non-headless Playwright** (Chromium) navigates to `boardgamegeek.com`, logs in via `fetch()`, navigates to the filepage, waits 8s for React to render
3. Extracts `/file/download_redirect/{signed-token}/{filename}` from the rendered DOM — this is a time-limited AWS presigned redirect
4. **`curl_cffi`** follows the redirect chain: `boardgamegeek.com/file/download_redirect/...` → `s3.amazonaws.com/geekdo-files.com/bgg{fileid}?X-Amz-...` → PDF bytes

### Key implementation notes
- `downloader.py` uses `curl_cffi` (not `requests`) throughout — it's the only client that passes Cloudflare's TLS fingerprinting for `boardgamegeek.com` downloads
- The browser must be **non-headless** — Cloudflare's JS challenge detects and blocks headless Chromium
- The signed S3 URL expires in ~120 seconds, so extract-then-download must happen in one flow
- `playwright install chromium` must be run once after installing deps
- Files are stored at `s3.amazonaws.com/geekdo-files.com/bgg{fileid}` (discovered via network interception)

### Picking the best rulebook
`pick_rulebook(files)` in `downloader.py` filters to `.pdf` files, prefers those with "rule" in title/filename, then sorts by `numpositive` (community votes).

**Known issue**: the current selector is too naive. In practice it sometimes picks:
- Solo mode rulebooks (e.g. "Solo Rules for Concordia") over the full game rulebook
- Variant rule documents
- Reference cards / player aids

**Planned fix** (not yet implemented): replace with tier-based scoring:
- Tier -1 (last resort): title/filename contains "solo", "variant", "player aid", "reference card", "quick reference", "faq", "errata"
- Tier 0 (fallback PDF): no rule-related keywords
- Tier 1: contains "rule" but no avoid terms
- Tier 2: contains "rules" but no avoid terms
- Tier 3: contains "rulebook", "complete rules", "core rules"
- Within each tier, sort by `numpositive` (community votes) descending

Also planned: a `scan` CLI command (requires `pip install pypdf`) that audits already-downloaded PDFs by page count, file size, and first-page keyword analysis to flag ones that are probably not full rulebooks.

### Fetching the BGG ranked list
`get_top_rankings(session, limit)` in `downloader.py` scrapes `boardgamegeek.com/browse/boardgame`.

Key implementation notes:
- **Must use the `curl_cffi` session** (Chrome TLS fingerprint) — plain `httpx` gets a 403 from Cloudflare
- **Pagination is path-based**: `/browse/boardgame/page/2`, NOT `?page=2` (query param is ignored)
- **Game name links have `class='primary'`** — use this to avoid matching thumbnail/sidebar/footer links that would pollute results with non-ranked game IDs
- 100 games per page; for 500 games, fetches 5 pages with a 2-second delay between pages
- No login required — the rankings page is public

## Chunking rules summary

- Markdown headings (`#`, `##`, `###`) are cut points; section_path tracks ancestry
- Target: 100–500 tokens per chunk
- Merge siblings if either is under 60 tokens
- Never split lists or tables (protected blocks)
- Skip sections matching: "illustration:", "graphic design:", "© ", "translation:"
- Fallback: 400-token windows with 50-token overlap when no headings detected
