"""Chunk enrichment: topic tagging via Claude Haiku and embeddings via Voyage."""

import json
import logging
import re
from pathlib import Path

from tqdm import tqdm

from bgg_shared.retry import with_retry
from bgg_shared.schema import Chunk

logger = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"

_TAG_PROMPT = """\
Tag these board game rulebook sections. For each chunk provide:
- topics: exactly 3-5 short tags (2-4 words each)
- summary: one sentence describing what rules or content this section covers
- chunk_type: one of "rules", "setup", "variant", "reference"

Return ONLY a JSON array — one object per chunk, same order as input, no other text:
[
  {{"topics": [...], "summary": "...", "chunk_type": "..."}},
  ...
]

Chunks:
{chunks_json}"""


@with_retry(max_attempts=3, base_delay=2.0)
def _call_haiku(chunks_data: list[dict], client) -> list[dict]:
    prompt = _TAG_PROMPT.format(chunks_json=json.dumps(chunks_data, indent=2))
    response = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def tag_chunks(
    chunks: list[Chunk],
    client,  # anthropic.Anthropic
    batch_size: int = 20,
    log=None,  # optional callable(str) for verbose output
) -> None:
    """Add topics, summary, and chunk_type to chunks in place via Claude Haiku."""
    for i in tqdm(range(0, len(chunks), batch_size), desc="Tagging", unit="batch"):
        batch = chunks[i : i + batch_size]
        payload = [
            {"id": c.chunk_id, "text": c.text[:600]}  # truncate to control cost
            for c in batch
        ]
        if log:
            ids = [p["id"] for p in payload]
            size = sum(len(p["text"]) for p in payload)
            log(f"  [enricher] batch {i // batch_size}: chunks={ids} payload={size}chars")
        try:
            results = _call_haiku(payload, client)
            for chunk, result in zip(batch, results):
                chunk.topics = result.get("topics", [])[:5]
                chunk.summary = result.get("summary", "")
                raw_type = result.get("chunk_type", "rules")
                chunk.chunk_type = raw_type if raw_type in ("rules", "setup", "variant", "reference") else "rules"
                if log:
                    log(f"  [enricher] {chunk.chunk_id}: topics={chunk.topics} summary={chunk.summary[:60]!r}")
        except Exception as exc:
            logger.warning("Haiku tagging failed for batch %d: %s", i // batch_size, exc)
            if log:
                log(f"  [enricher] batch {i // batch_size} FAILED: {exc}")


def embed_chunks(
    chunks: list[Chunk],
    embedder,
    embeddings_path: Path | None = None,
) -> None:
    """Add Voyage embeddings to chunks in place; optionally save to a .npy file.

    Calls embedder.embed_batch in batches of 100, showing a tqdm progress bar.
    When embeddings_path is provided, saves all embeddings as a float32 numpy array
    of shape (num_chunks, embedding_dim).
    """
    batch_size = 100
    with tqdm(total=len(chunks), desc="Embedding", unit="chunk") as pbar:
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c.text for c in batch]
            embeddings = embedder.embed_batch(texts)
            for c, emb in zip(batch, embeddings):
                c.embedding = emb
            pbar.update(len(batch))

    if embeddings_path is not None:
        import numpy as np
        vecs = [c.embedding or [] for c in chunks]
        np.save(str(embeddings_path), np.array(vecs, dtype=np.float32))
        logger.info("Saved embeddings → %s", embeddings_path)
