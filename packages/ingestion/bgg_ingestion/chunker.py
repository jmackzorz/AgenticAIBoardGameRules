"""Markdown → Chunk list.

Chunking rules (from spec):
- Use marker headings (#, ##, ###) as natural cut points.
- Build hierarchical section_path from heading ancestry.
- Target 100–500 tokens per chunk.
- Merge an adjacent sibling if either is under 60 tokens.
- Never split a list or table block across chunks.
- Preserve scoring tables, reference cards, and component lists regardless of size.
- Skip cover / credits / copyright sections.
- Fallback to ~400-token windows with 50-token overlap when no headings detected.
"""

import re
from bisect import bisect_right

import tiktoken

from bgg_shared.schema import Chunk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_TOKENS = 60
_TARGET_MAX_TOKENS = 500
_WINDOW_TOKENS = 400
_WINDOW_OVERLAP = 50

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*+([^*]+)\*+")

_SKIP_PATTERNS = [
    re.compile(r"illustration\s*:", re.IGNORECASE),
    re.compile(r"graphic\s+design\s*:", re.IGNORECASE),
    re.compile(r"©\s", re.IGNORECASE),
    re.compile(r"translation\s*:", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


# ---------------------------------------------------------------------------
# Skip detection
# ---------------------------------------------------------------------------

def _should_skip(text: str) -> bool:
    return any(p.search(text) for p in _SKIP_PATTERNS)


# ---------------------------------------------------------------------------
# Heading / section parsing
# ---------------------------------------------------------------------------

def _parse_sections(markdown: str) -> list[dict]:
    """Split markdown on headings, returning a list of section dicts.

    Each dict has:
      section_path: list[str]  — ancestor heading titles, innermost last
      text: str                — full section text including its heading line
      pos: int                 — character offset of this section in markdown
    """
    headings = [
        {
            "level": len(m.group(1)),
            "title": _MD_BOLD_RE.sub(r"\1", m.group(2)).strip(),
            "start": m.start(),
            "end": m.end(),
        }
        for m in _HEADING_RE.finditer(markdown)
    ]

    if not headings:
        return [{"section_path": [], "text": markdown.strip(), "pos": 0}]

    sections = []

    # Preamble before the first heading
    preamble = markdown[: headings[0]["start"]].strip()
    if preamble:
        sections.append({"section_path": [], "text": preamble, "pos": 0})

    # Stack stores (level, title) pairs. When a heading at level N is encountered,
    # all entries with level >= N are popped first — so same-level headings are
    # siblings, not parent-child, and a shallower heading correctly closes deeper ones.
    heading_stack: list[tuple[int, str]] = []
    for i, h in enumerate(headings):
        level = h["level"]
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, h["title"]))
        section_path = [title for _, title in heading_stack]

        # Body is everything from the end of this heading line to the start of the next
        body_start = h["end"]
        body_end = headings[i + 1]["start"] if i + 1 < len(headings) else len(markdown)
        body = markdown[body_start:body_end].strip()

        heading_line = "#" * level + " " + h["title"]
        text = (heading_line + "\n\n" + body).strip() if body else heading_line

        sections.append({
            "section_path": section_path,
            "text": text,
            "pos": h["start"],
        })

    return sections


# ---------------------------------------------------------------------------
# Block splitting (for large-section splitting without cutting lists/tables)
# ---------------------------------------------------------------------------

def _split_into_blocks(text: str) -> list[dict]:
    """Split text on double newlines, marking list and table blocks as protected."""
    blocks = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        is_table = bool(re.search(r"^\|", para, re.MULTILINE))
        is_list = bool(re.match(r"^\s*(?:[-*+]|\d+\.)\s", para))
        blocks.append({"text": para, "protected": is_table or is_list})
    return blocks


def _split_large_section(section: dict) -> list[dict]:
    """Split a section that exceeds _TARGET_MAX_TOKENS into smaller pieces.

    Protected blocks (lists, tables) are never broken. A protected block that
    exceeds the target on its own is emitted as a single oversized chunk
    (the QA gate will flag it if it's over 2 000 tokens).
    """
    if count_tokens(section["text"]) <= _TARGET_MAX_TOKENS:
        return [section]

    blocks = _split_into_blocks(section["text"])
    result: list[dict] = []
    acc_texts: list[str] = []
    acc_tokens = 0

    def _flush():
        if acc_texts:
            result.append({**section, "text": "\n\n".join(acc_texts)})

    for block in blocks:
        bt = count_tokens(block["text"])

        if block["protected"]:
            # Protected block: flush accumulator first, then emit block alone
            _flush()
            acc_texts, acc_tokens = [], 0
            result.append({**section, "text": block["text"]})
        elif acc_tokens + bt > _TARGET_MAX_TOKENS and acc_texts:
            _flush()
            acc_texts, acc_tokens = [block["text"]], bt
        else:
            acc_texts.append(block["text"])
            acc_tokens += bt

    _flush()
    return result if result else [section]


# ---------------------------------------------------------------------------
# Sibling merging
# ---------------------------------------------------------------------------

def _merge_small_sections(sections: list[dict]) -> list[dict]:
    """Merge adjacent sections when either neighbour is under _MIN_TOKENS."""
    if not sections:
        return sections

    result = [sections[0]]
    for sec in sections[1:]:
        prev = result[-1]
        if count_tokens(prev["text"]) < _MIN_TOKENS or count_tokens(sec["text"]) < _MIN_TOKENS:
            result[-1] = {
                "section_path": prev["section_path"],
                "text": prev["text"] + "\n\n" + sec["text"],
                "pos": prev["pos"],
            }
        else:
            result.append(sec)
    return result


# ---------------------------------------------------------------------------
# Windowed fallback
# ---------------------------------------------------------------------------

def _window_chunks(text: str) -> list[str]:
    """Split text into overlapping token windows (used when no headings found)."""
    enc = _get_encoder()
    tokens = enc.encode(text)
    result = []
    start = 0
    while start < len(tokens):
        end = min(start + _WINDOW_TOKENS, len(tokens))
        result.append(enc.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start += _WINDOW_TOKENS - _WINDOW_OVERLAP
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def chunk(
    markdown: str,
    bgg_id: int,
    game_name: str,
    page_offsets: list[int] | None = None,
) -> list[Chunk]:
    """Convert a markdown document into a list of Chunk objects.

    topics, summary, embedding are left empty — the enricher fills those in.
    chunk_type defaults to "rules"; the enricher overwrites it per chunk.
    """
    enc = _get_encoder()
    offsets = page_offsets or []

    def _page(pos: int) -> int | None:
        if not offsets:
            return None
        # offsets[0] == 0 (page 1 start), offsets[1] == first break, etc.
        # bisect_right gives 1-based page number.
        return bisect_right(offsets, pos)

    sections = _parse_sections(markdown)
    has_headings = any(s["section_path"] for s in sections)

    if not has_headings:
        # Fallback: windowed chunks, page unknown
        windows = _window_chunks(markdown)
        return [
            Chunk(
                chunk_id=f"{bgg_id}_{idx:03d}",
                bgg_id=bgg_id,
                game_name=game_name,
                section_path=[],
                page=None,
                topics=[],
                summary="",
                chunk_type="rules",
                token_count=len(enc.encode(text)),
                text=text,
            )
            for idx, text in enumerate(windows)
        ]

    # Skip cover / credits / copyright sections
    sections = [s for s in sections if not _should_skip(s["text"])]

    # Merge thin siblings
    sections = _merge_small_sections(sections)

    # Split oversized sections (respecting protected blocks)
    raw: list[dict] = []
    for sec in sections:
        raw.extend(_split_large_section(sec))

    return [
        Chunk(
            chunk_id=f"{bgg_id}_{idx:03d}",
            bgg_id=bgg_id,
            game_name=game_name,
            section_path=rc["section_path"],
            page=_page(rc["pos"]),
            topics=[],
            summary="",
            chunk_type="rules",
            token_count=len(enc.encode(rc["text"])),
            text=rc["text"],
        )
        for idx, rc in enumerate(raw)
    ]
