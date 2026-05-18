# Decisions — BGG Research Agent

**Sonnet for the agent loop, Haiku for batch enrichment.** The agent loop needs real multi-step reasoning and quality affects every interaction, so Sonnet is worth it there. Batch enrichment is simple offline classification — Haiku handles it reliably at a fraction of the cost.

**DynamoDB for session state.** Lambda has no guarantee the same instance handles two turns of the same conversation, so in-memory state doesn't work. Redis was the obvious alternative but ElastiCache has a standing monthly cost and requires a VPC. DynamoDB scales to zero and TTL handles expiry automatically.

**Prompt caching on the system prompt.** The system prompt is identical on every turn so it's the safest caching target — adding a message busts the cache if you try to cache the full history. ~90% cost reduction on cached tokens for a two-line change.

**Module-level client initialization.** Lambda reuses the execution environment across warm invocations, so initializing `anthropic.Anthropic()` and the `httpx` session once at module load means warm calls reuse connections instead of doing a new TCP/TLS handshake every time.

**BGG's HTTP 202 polling stays inline, not routed through `@with_retry`.** BGG returns 202 while a query result is being cached server-side — it's a protocol feature, not a failure. Treating it as an error would require the generic retry decorator to special-case it, which would be wrong.

**Single root `pyproject.toml` with optional-dep groups.** A full workspace setup would achieve cleaner isolation but adds config overhead that isn't worth it for a solo project. Lambda isolation is enforced by `test_lambda_import_isolation.py` instead of package structure.

**Heading-based chunking with a token floor.** Rulebooks have natural section boundaries; splitting on headings keeps each chunk semantically coherent. Fixed-size sliding windows routinely split rules mid-sentence. The 60-token floor merges short sections that would produce weak embeddings, and there's a sliding window fallback for PDFs with no heading structure.

**`curl_cffi` + Playwright for BGG downloads.** Standard HTTP clients get blocked by Cloudflare's TLS fingerprinting. `curl_cffi` impersonates Chrome at the TLS level. The file listing page is also a React SPA so Playwright drives a real Chromium instance to render it and extract the presigned download URL from the DOM.

**Pydantic for pipeline models, dataclasses for BGG API models.** Pipeline models serialize to/from JSON files on disk so Pydantic's `model_dump()` / `model_validate()` are actually useful there. BGG API models just get constructed from responses and formatted as strings — dataclasses are sufficient and lighter.
