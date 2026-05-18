# Architecture Decision Records — BGG Research Agent

This document captures the key architectural decisions made during the design and
evolution of this project, including the alternatives considered and the reasoning
behind each choice. It exists to make the thinking behind the code explicit and
reviewable.

---

## ADR-001: Multi-model strategy — Sonnet for the agent loop, Haiku for batch enrichment

**Decision:** Use `claude-sonnet-4` for the agentic tool-use loop and `claude-haiku-4`
for the ingestion pipeline's batch enrichment step (topic tagging and chunk type
classification).

**Alternatives considered:**
- Use Sonnet for everything — simpler, one model to manage
- Use Haiku for everything — cheapest option
- Use a local/open-source model for enrichment to eliminate API cost entirely

**Reasoning:** The agent loop requires genuine multi-step reasoning — deciding which
tools to call, synthesising results across multiple BGG API responses, handling
ambiguous user queries. Sonnet earns its cost here because quality directly affects
the user experience on every interaction.

The enrichment pipeline is fundamentally different: it's a batch classification task
applied to thousands of text chunks. The instructions are simple (assign 3–5 topic
tags, classify chunk type as `rules/setup/variant/reference`), the inputs are short
and structured, and the output is constrained. Haiku handles this reliably at a
fraction of the cost. There is no user waiting on this step — it runs offline.

Using the wrong model in either direction has real costs: Sonnet for batch tagging
is expensive and slow with no quality benefit; Haiku for the agent loop produces
noticeably worse reasoning on complex multi-tool queries.

**Result:** Cost-effective without compromising the product. The multi-model boundary
also makes the two subsystems independently swappable as models evolve.

---

## ADR-002: DynamoDB with TTL for session state, not Redis or in-memory

**Decision:** Persist conversation history in DynamoDB with a 24-hour TTL per session.

**Alternatives considered:**
- In-memory dict on the Lambda instance — zero cost, zero latency
- Redis (ElastiCache) — fast, purpose-built for session data
- S3 — cheap but high latency for small frequent reads/writes

**Reasoning:** In-memory state on a Lambda instance dies with the instance. Lambda
scales horizontally and spins up new instances on demand — there is no guarantee
the same instance handles two turns of the same conversation. This makes in-memory
state fundamentally incompatible with serverless multi-turn conversations.

Redis is the right answer in a persistent server environment but adds a standing
infrastructure cost (ElastiCache minimum ~$15–20/month) and a VPC requirement for
Lambda, which significantly increases deployment complexity for a project at this
scale.

DynamoDB is natively serverless, scales to zero when idle, and requires no VPC
configuration. The TTL feature handles expiry automatically — expired sessions are
removed without any cleanup lambda or cron job. At the conversation sizes this
agent handles, DynamoDB's latency is imperceptible.

**Result:** Zero standing infrastructure cost, automatic expiry, no VPC complexity,
genuinely serverless.

---

## ADR-003: Prompt caching on the system prompt

**Decision:** Mark the system prompt with `cache_control: {"type": "ephemeral"}` so
Anthropic's API caches it across turns within a session.

**Alternatives considered:**
- No caching — simplest implementation
- Cache the full conversation history — more aggressive savings

**Reasoning:** The system prompt is identical on every turn and every session. Without
caching, the API re-processes it from scratch on every request. The system prompt
for this agent is ~800 tokens — not enormous, but in a multi-turn conversation that
cost compounds. Anthropic's prompt caching reduces input token cost by ~90% for
cached content.

Caching the full conversation history would require careful implementation to avoid
cache invalidation on every new message (adding a message changes the content,
busting the cache). The system prompt is the safest and highest-ROI caching target
because it never changes.

**Result:** Measurable token cost reduction on multi-turn sessions with a single
two-line change.

---

## ADR-004: Connection reuse across warm Lambda invocations

**Decision:** Initialise `anthropic.Anthropic()` and `BggClient` (an `httpx` session)
once at module load time, outside the handler function.

**Alternatives considered:**
- Initialise inside the handler on every invocation — simpler, no state concerns
- Dependency injection via constructor — cleaner for testing

**Reasoning:** Lambda reuses the execution environment (the "warm start") across
invocations when load allows. Code at module level executes once during the cold
start and is then available on subsequent warm invocations. Initialising an HTTP
client inside the handler creates a new TCP connection on every invocation — cold
and warm — which adds measurable latency and discards connection pooling benefits.

`httpx` in particular benefits significantly from session reuse: keep-alive
connections to the BGG API avoid the TCP + TLS handshake cost on every tool call.

The `BggAgent` class accepts pre-initialised clients in its constructor specifically
to support this pattern — the Lambda handler creates them once, the agent uses them.
This also makes the agent independently testable without involving Lambda
infrastructure.

**Result:** Lower cold-start time, faster tool call execution on warm invocations,
and a testable agent that doesn't depend on module-level state.

---

## ADR-005: BGG polling loop kept inline, not abstracted into the generic retry decorator

**Decision:** The BGG HTTP 202 polling loop in `BggClient._get_xml()` stays as inline
code. The generic `@with_retry` decorator in `bgg_shared/retry.py` is used for
Voyage AI calls and Anthropic ingestion calls, but not for the BGG client.

**Alternatives considered:**
- Use `@with_retry` everywhere for consistency
- Extract the BGG polling into its own decorator

**Reasoning:** The BGG 202 loop is not a retry in the normal sense — it is a
protocol feature. BGG returns HTTP 202 when a query result is being cached server-
side, and the client is expected to poll until it gets 200. The delay schedule
(`RETRY_DELAY * (attempt + 1)`) encodes BGG-specific knowledge about how long
their cache warming typically takes.

The generic `@with_retry` decorator is designed for transient failures: timeouts,
5xx errors, rate limits. Abstracting the BGG polling loop through it would force
it to treat a 202 (a normal in-progress response) as a failure condition, which
is semantically wrong and would require ugly special-casing.

Keeping domain-specific protocol handling inline makes it readable and explicit.
The comment in the code explains the 202 semantics so future maintainers
understand it is intentional, not a missing abstraction.

**Result:** The generic retry decorator stays clean and purpose-appropriate. The BGG
protocol detail is self-contained and documented where it lives.

---

## ADR-006: Monorepo with optional dependency groups, not separate packages

**Decision:** Single root `pyproject.toml` with optional dependency groups (`lambda`,
`ingestion`, `cli`, `dev`) rather than three separate packages with a workspace
coordinator.

**Alternatives considered:**
- Three separate `pyproject.toml` files with `uv workspaces` or PEP 517 path deps
- Full monorepo with independent versioning per package

**Reasoning:** The workspace approach achieves cleaner isolation but adds ~30 lines
of workspace coordinator config, requires understanding `uv workspace` resolution,
and introduces the risk of version conflicts across workspace members. For a solo
portfolio project with two deployable pieces and one shared library, this overhead
does not pay for itself.

The optional-deps approach achieves the critical requirement — Lambda deployment
isolation — with a simpler contract: the import isolation test in
`test_lambda_import_isolation.py` verifies at test time that `bgg_lambda` imports
do not pull in `marker`, `torch`, `streamlit`, or `voyageai`. If the isolation
breaks, the test fails. This is a behavioural contract, which is more reliable
than a structural constraint enforced by package boundaries.

The decision was also influenced by this being a living project with a documented
migration plan. Workspace config adds friction to refactoring; optional deps do not.

**Result:** Simpler config, equivalent Lambda isolation, tested by the import
isolation test rather than enforced by packaging structure.

---

## ADR-007: Heading-based chunking with a token floor, not fixed-size sliding windows

**Decision:** The ingestion chunker splits rulebook Markdown on headings
(`#`/`##`/`###`), targeting 100–500 tokens per chunk. Siblings under 60 tokens are
merged with the next sibling. A 400-token sliding window fallback handles documents
with no headings.

**Alternatives considered:**
- Fixed-size sliding windows with overlap — common default in RAG tutorials
- Sentence-level splitting — finer granularity
- Page-level splitting — coarser, simpler

**Reasoning:** Rulebooks have natural semantic boundaries: setup, rules, variants,
reference tables. Heading-based chunking preserves these boundaries, which means
each chunk is about one coherent topic. This makes retrieval more accurate because
a query about setup doesn't retrieve a chunk that mixes setup with scoring rules.

Fixed-size sliding windows ignore document structure entirely. A 400-token window
will routinely split a rule mid-sentence or merge unrelated sections if they happen
to fall within the same window. The retrieved context is harder for the model to
reason about.

The 60-token merge floor exists because headings with very little content (e.g. a
section title followed by one sentence) produce low-signal embeddings. The embedding
model has less to anchor on and tends to produce generic vectors that don't
distinguish the chunk well.

The 400-token sliding window fallback handles the real-world case where some
rulebook PDFs convert to unstructured Markdown with no heading hierarchy — common
for older PDFs that were scanned rather than exported.

**Result:** Semantically coherent chunks that align with how a human would section
a rulebook, with pragmatic fallback for low-structure documents. 22 unit tests
cover edge cases including heading-only sections, tables that should not be split,
and the sliding window fallback.

---

## ADR-008: curl_cffi with Chrome TLS fingerprinting for BGG PDF downloads

**Decision:** Use `curl_cffi` (with `impersonate="chrome"`) for the authentication
step of the BGG file downloader, with Playwright rendering the file listing page
to extract the presigned download URL.

**Alternatives considered:**
- Standard `httpx` or `requests` for the full download flow
- Selenium instead of Playwright
- Scraping BGG's undocumented internal API directly

**Reasoning:** BGG's file download pages are protected by Cloudflare. Standard Python
HTTP clients (`httpx`, `requests`) present TLS fingerprints that Cloudflare
identifies as non-browser traffic and blocks with a challenge page. `curl_cffi`
mimics Chrome's TLS handshake at the libcurl level, which passes Cloudflare's
fingerprint check.

The file listing page itself is a React SPA — the file entries are rendered
client-side after JavaScript execution. A plain HTTP request to the page URL
returns the shell HTML with no file entries. Playwright drives a real Chromium
instance, waits for the SPA to hydrate, and then reads the DOM to extract the
time-limited AWS presigned URL that BGG generates for each download.

Selenium was considered but Playwright's async-native API, better DevTools
integration, and faster startup made it the better fit for this pipeline.

**Result:** Reliable automated download of BGG rulebook PDFs despite Cloudflare
protection and React SPA rendering, without requiring manual browser interaction.

---

## ADR-009: Pydantic v2 for pipeline models, dataclasses for BGG API models

**Decision:** `Chunk` and `GameIndex` (pipeline data structures) are Pydantic v2
`BaseModel` subclasses. `SearchResult` and `BoardGame` (BGG API response structures)
remain plain Python `@dataclass`.

**Alternatives considered:**
- Pydantic for everything — consistent
- Dataclasses for everything — no external dependency

**Reasoning:** Pipeline models (`Chunk`, `GameIndex`) are serialised to and
deserialised from JSON files on disk between pipeline stages. Pydantic v2 provides
field validation, automatic JSON serialisation via `model_dump()`, and typed
deserialisation via `model_validate()`. These are the exact capabilities needed
for a data pipeline that reads and writes structured files.

BGG API models (`SearchResult`, `BoardGame`) are constructed once from API
responses, passed through the tool handler, and embedded in Claude's context as
formatted strings. They are never serialised to disk. Plain dataclasses are
sufficient — they are lighter, have no external dependency, and the `BoardGame.
to_summary()` method provides the formatting needed for Claude's context. Adding
Pydantic validation here would be adding complexity for no benefit.

The two model families live in separate files (`models.py` and `schema.py`) in
`bgg_shared` precisely to make this distinction explicit.

**Result:** Right tool for each job. Pipeline models are validated and serialisable;
API models are lightweight and dependency-free.