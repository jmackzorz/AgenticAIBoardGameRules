"""QA gates for indexed game data.

Flags are informational — the pipeline never raises on them.
"""

import re

from bgg_shared.schema import GameIndex

_LOW_WORD_RATIO_THRESHOLD = 0.5
_REAL_WORD_RE = re.compile(r"^[a-zA-Z]{3,}$")


def run_qa(index: GameIndex) -> list[str]:
    """Run all QA gates and return a list of flag strings."""
    flags: list[str] = []
    chunks = index.chunks

    if len(chunks) < 3:
        flags.append("low_chunk_count")

    if all(not c.section_path for c in chunks):
        flags.append("no_headings_detected")

    if any(c.token_count > 2000 for c in chunks):
        flags.append("oversized_chunk")

    if any(c.token_count < 20 for c in chunks):
        flags.append("tiny_chunk")

    all_words = " ".join(c.text for c in chunks).split()
    if all_words:
        real_count = sum(1 for w in all_words if _REAL_WORD_RE.match(w))
        if real_count / len(all_words) < _LOW_WORD_RATIO_THRESHOLD:
            flags.append("low_word_ratio")

    if any(not c.topics and not c.summary for c in chunks):
        flags.append("empty_enrichment")

    try:
        from langdetect import detect, LangDetectException  # type: ignore
        sample = " ".join(c.text for c in chunks[:5])[:3000]
        if detect(sample) != "en":
            flags.append("non_english")
    except Exception:
        pass

    return flags
