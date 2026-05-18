# Migration Plan: BGG Agent → Rulebook Indexing Monorepo

## Current state audit

### Python version
`pyproject.toml`: `requires-python = ">=3.11"`. SAM `template.yaml`: `Runtime: python3.12`. The Lambda runs 3.12; local dev targets 3.11+.

### Current dependency declarations
Two sources of truth today:
- **`pyproject.toml`** (primary): `anthropic>=0.50.0`, `httpx>=0.27.0`, `rich>=13.7.0`, `python-dotenv>=1.0.0`, `boto3>=1.35.0`
- **`requirements.txt`**: pinned lockfile (`uv.lock` also present)
- **`package/`**: manually-vendored copies of the above, used directly as the Lambda CodeUri

### Lambda handler
`lambda_function.py` (repo root) — function `handler(event, _context)`.  
`template.yaml` → `CodeUri: package/`, `Handler: lambda_function.handler`.  
`package/` is a flat directory produced by hand: source files from `bgg_agent/` copied in, deps installed via `pip install -t package/`. No Makefile, no build script.

### BGG API client
`bgg_agent/bgg_client.py` — `BggClient` class. Its `_get_xml()` method contains an inline polling loop: BGG returns HTTP 202 when a query is still being cached server-side; the client waits `RETRY_DELAY * (attempt + 1)` seconds and retries up to `_MAX_RETRIES = 3` times. **This is a BGG-domain protocol detail, not a generic retry.** It stays inline.

### Models
`bgg_agent/models.py` — plain `@dataclass`, not Pydantic:
- `SearchResult`: id, name, year_published
- `BoardGame`: 17 fields + `to_summary()` method

### BggAgent
`bgg_agent/agent.py` — `BggAgent` class.  
- Stateful agentic loop (Claude tool-use)
- Accepts pre-initialized `anthropic_client` and `bgg_client` (warm-start pattern used by Lambda)
- `chat()`, `reset()`, `history`, context manager

### Tools
- `bgg_agent/tools/definitions.py` — Claude tool JSON schemas (3 tools)
- `bgg_agent/tools/handlers.py` — handler functions + `dispatch()` router
- `bgg_agent/tools/__init__.py` — re-exports `TOOL_DEFINITIONS`, `dispatch`
- `bgg_agent/__init__.py` — re-exports `BggAgent`

### Files that import from bgg_agent (every import that will need updating)
| File | Current import | New import |
|---|---|---|
| `lambda_function.py` | `from bgg_agent import BggAgent` | `from bgg_lambda import BggAgent` |
| `lambda_function.py` | `from bgg_agent.bgg_client import BggClient` | `from bgg_shared.bgg import BggClient` |
| `main.py` | `from bgg_agent import BggAgent` | `from bgg_lambda import BggAgent` |
| `bgg_agent/agent.py` | `from .bgg_client import BggClient` | `from bgg_shared.bgg import BggClient` |
| `bgg_agent/tools/handlers.py` | `from ..bgg_client import BggClient` | `from bgg_shared.bgg import BggClient` |
| `bgg_agent/bgg_client.py` | `from .models import BoardGame, SearchResult` | `from .models import BoardGame, SearchResult` (unchanged — bgg.py stays in bgg_shared alongside models.py) |

### Tests
None exist today. All tests in Step 4 are net-new.

---

## Schema / model split

**`bgg_shared/models.py`** — existing dataclasses, moved verbatim, zero changes:
```python
@dataclass class SearchResult: ...
@dataclass class BoardGame: ...
```

**`bgg_shared/schema.py`** — new Pydantic v2 models only:
```python
class Chunk(BaseModel): ...
class GameIndex(BaseModel): ...
```

These are different tools for different jobs. Dataclasses travel through BGG client → tool handlers → Claude response. Pydantic models serialize to/from JSON files on disk. They live in separate files and the code that uses one never needs to know about the other.

---

## bgg_shared/retry.py

A generic decorator for external HTTP/API calls that may fail with timeouts, 5xx responses, or rate limits. Used by: Voyage embedding client (`bgg_shared/embedder.py`) and any Anthropic calls in the ingestion pipeline. **Not used by `BggClient._get_xml()`** — that polling loop is BGG-protocol-specific and stays inline.

```python
def with_retry(max_attempts=3, base_delay=1.0):
    """Exponential backoff for transient failures (timeouts, 5xx, rate limits)."""
    ...
```

---

## pyproject.toml strategy: single root with optional-deps groups

**Decision: single root `pyproject.toml`**, not three separate files.

Three separate pyproject files with a workspace coordinator adds ~30 lines of config and requires understanding `uv` workspace resolution. The optional-deps approach achieves the same Lambda isolation with the contract enforced by the import-isolation test in Step 4 instead of package structure.

```toml
[project]
name = "bgg-monorepo"
requires-python = ">=3.11"
# No top-level dependencies — install a group instead.

[project.optional-dependencies]
lambda = ["anthropic>=0.50.0", "httpx>=0.27.0", "boto3>=1.35.0", "python-dotenv>=1.0.0"]
ingestion = ["anthropic>=0.50.0", "httpx>=0.27.0", "python-dotenv>=1.0.0",
             "pydantic>=2.0", "marker-pdf", "voyageai", "tiktoken",
             "langdetect", "tqdm", "typer", "streamlit", "streamlit-pdf-viewer"]
cli = ["rich>=13.7.0"]
dev = ["pytest"]

[tool.setuptools.packages.find]
where = ["packages/shared", "packages/lambda_handler", "packages/ingestion", "."]
# discovers: bgg_shared, bgg_lambda, bgg_ingestion, and lets main.py stay at root
```

Local developer install: `pip install -e .[ingestion,cli,dev]`  
Lambda vendor step: installs only `[lambda]` group (see below).  
`rich` stays available locally via the `cli` group. It is **never installed into `package/`**.

---

## Lambda vendor build flow

Replaces the current undocumented manual copy. A `build_lambda.sh` (or PowerShell equivalent) script documented in the README:

```bash
# 1. Wipe and recreate the vendor directory
rm -rf package && mkdir package

# 2. Install Lambda pip deps (no ingestion, no rich, no dev)
pip install -t package/ \
    "anthropic>=0.50.0" "httpx>=0.27.0" "boto3>=1.35.0" "python-dotenv>=1.0.0"

# 3. Copy bgg_shared and bgg_lambda source packages
cp -r packages/shared/bgg_shared   package/bgg_shared
cp -r packages/lambda_handler/bgg_lambda  package/bgg_lambda

# 4. Copy the SAM entry-point shim
cp packages/lambda_handler/lambda_function.py  package/lambda_function.py
```

`package/lambda_function.py` (the shim) is a two-liner:
```python
from bgg_lambda.handler import handler  # noqa: F401 — re-export for SAM
```

SAM sees `Handler: lambda_function.handler` — unchanged. The rest of the Lambda code lives in `bgg_lambda/handler.py`.

**The import-isolation test** (Step 4) verifies this contract by importing `bgg_lambda` in a fresh interpreter and asserting that `marker`, `torch`, `streamlit`, and `voyageai` are not in `sys.modules`.

---

## Proposed directory layout (final)

```
<repo-root>/
  pyproject.toml             ← replaces existing; single root with optional-dep groups
  README.md
  MIGRATION_PLAN.md
  main.py                    ← stays at root; 1 import updated (bgg_agent → bgg_lambda)
  template.yaml              ← CodeUri: package/ unchanged; Handler unchanged
  samconfig.toml             ← unchanged
  .env / .env.example        ← unchanged
  requirements.txt           ← updated to reflect new dep groups
  uv.lock                    ← regenerated after pyproject.toml update

  packages/
    shared/
      bgg_shared/
        __init__.py
        models.py            ← SearchResult, BoardGame @dataclass (MOVED verbatim)
        schema.py            ← Chunk, GameIndex Pydantic v2 (NEW)
        bgg.py               ← BggClient (MOVED verbatim; 1 import: .models → .models)
        embedder.py          ← Voyage wrapper (NEW, Step 2)
        retry.py             ← generic backoff decorator (NEW)

    lambda_handler/
      bgg_lambda/
        __init__.py          ← re-exports BggAgent (content unchanged)
        handler.py           ← lambda_function.py MOVED; 2 imports updated
        agent.py             ← bgg_agent/agent.py MOVED; 1 import updated
        tools/
          __init__.py        ← unchanged
          definitions.py     ← unchanged
          handlers.py        ← 1 import updated (bgg_shared.bgg)
      lambda_function.py     ← SAM shim (2 lines), sits alongside bgg_lambda/
                                so SAM CodeUri: package/ structure is preserved

    ingestion/               ← NEW (Step 2)
      bgg_ingestion/
        __init__.py
        extractor.py
        chunker.py
        enricher.py
        qa.py
        cli.py
      viewer/
        app.py               ← Streamlit UI (Step 3)

  data/                      ← gitignored
    pdfs/
    markdown/
    chunks/
    bgg_cache/

  tests/
    test_chunker.py          ← NEW (Step 4)
    test_qa.py               ← NEW (Step 4)
    test_lambda_import_isolation.py  ← NEW (Step 4)

  package/                   ← gitignored; SAM artifact; rebuilt by build_lambda script
```

---

## Files: current path → destination (complete list)

| Current path | Destination | Action | Imports changed |
|---|---|---|---|
| `bgg_agent/models.py` | `packages/shared/bgg_shared/models.py` | MOVE verbatim | none |
| `bgg_agent/bgg_client.py` | `packages/shared/bgg_shared/bgg.py` | MOVE verbatim | `from .models` stays `.models` (same package) |
| `bgg_agent/agent.py` | `packages/lambda_handler/bgg_lambda/agent.py` | MOVE | `from .bgg_client import BggClient` → `from bgg_shared.bgg import BggClient` |
| `bgg_agent/tools/definitions.py` | `packages/lambda_handler/bgg_lambda/tools/definitions.py` | MOVE verbatim | none |
| `bgg_agent/tools/handlers.py` | `packages/lambda_handler/bgg_lambda/tools/handlers.py` | MOVE | `from ..bgg_client import BggClient` → `from bgg_shared.bgg import BggClient` |
| `bgg_agent/tools/__init__.py` | `packages/lambda_handler/bgg_lambda/tools/__init__.py` | MOVE verbatim | none |
| `bgg_agent/__init__.py` | `packages/lambda_handler/bgg_lambda/__init__.py` | MOVE verbatim | none |
| `lambda_function.py` (root) | `packages/lambda_handler/bgg_lambda/handler.py` | MOVE | `from bgg_agent import BggAgent` → `from bgg_lambda import BggAgent`; `from bgg_agent.bgg_client import BggClient` → `from bgg_shared.bgg import BggClient` |
| `main.py` (root) | `main.py` (root, stays) | UPDATE | `from bgg_agent import BggAgent` → `from bgg_lambda import BggAgent` |
| `bgg_agent/` directory | — | DELETE after all moves complete | — |
| *(new)* | `packages/lambda_handler/lambda_function.py` | CREATE (shim) | — |
| *(new)* | `packages/shared/bgg_shared/retry.py` | CREATE | — |
| *(new)* | `packages/shared/bgg_shared/__init__.py` | CREATE | — |

---

## Lambda artifact size

Currently: `bgg_agent/` source + `anthropic`, `httpx`, `boto3`, `python-dotenv`, `rich` + transitive deps.  
After: `bgg_lambda/` + `bgg_shared/` source (~2 KB net addition) + same pip deps **minus `rich`** (~500 KB removed).  
Result: **same or smaller**.

---

## Before starting

Don't proceed to Step 1 until this plan is approved.
