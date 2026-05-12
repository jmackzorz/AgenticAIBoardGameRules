"""Tests for bgg_ingestion.chunker."""

import pytest

from bgg_ingestion.chunker import (
    _merge_small_sections,
    _parse_sections,
    _split_large_section,
    _window_chunks,
    chunk,
    count_tokens,
)

BGG_ID = 99999
GAME = "Test Game"

# A paragraph long enough to exceed _MIN_TOKENS (60) on its own (~75 tokens).
_LONG_PARA = (
    "This section of the rulebook explains the detailed mechanics of how players take turns "
    "during the main game phase. Each player on their turn must draw a card from the top of the "
    "deck, play at least one card from their hand to the table, and then discard remaining cards "
    "down to the maximum hand limit. Players who cannot play a valid card must pass their entire "
    "turn to the next player in clockwise order around the table."
)


# ---------------------------------------------------------------------------
# count_tokens sanity
# ---------------------------------------------------------------------------


def test_count_tokens_nonempty():
    assert count_tokens("hello world") > 0


def test_count_tokens_empty():
    assert count_tokens("") == 0


# ---------------------------------------------------------------------------
# _parse_sections
# ---------------------------------------------------------------------------


def test_parse_sections_no_headings():
    md = "Just some text with no headings at all."
    sections = _parse_sections(md)
    assert len(sections) == 1
    assert sections[0]["section_path"] == []
    assert "Just some text" in sections[0]["text"]


def test_parse_sections_heading_hierarchy():
    md = (
        "# Top\n\n"
        "## Middle\n\n"
        "### Deep\n\nDeep content.\n\n"
        "## Sibling\n\nSibling content.\n"
    )
    sections = _parse_sections(md)
    paths = [s["section_path"] for s in sections]

    assert ["Top"] in paths
    assert ["Top", "Middle"] in paths
    assert ["Top", "Middle", "Deep"] in paths
    assert ["Top", "Sibling"] in paths


def test_parse_sections_same_level_are_siblings():
    md = "## A\n\nContent A.\n\n## B\n\nContent B.\n"
    sections = _parse_sections(md)
    paths = [s["section_path"] for s in sections]
    assert ["A"] in paths
    assert ["B"] in paths
    # B must NOT be nested under A
    assert ["A", "B"] not in paths


def test_parse_sections_preamble_captured():
    md = "Intro text before any heading.\n\n# Section\n\nBody."
    sections = _parse_sections(md)
    assert sections[0]["section_path"] == []
    assert "Intro text" in sections[0]["text"]


def test_parse_sections_bold_stripped_from_title():
    md = "## **Bold Title**\n\nSome content."
    sections = _parse_sections(md)
    assert sections[0]["section_path"] == ["Bold Title"]


# ---------------------------------------------------------------------------
# _merge_small_sections
# ---------------------------------------------------------------------------


def test_merge_small_sections_combines_tiny_neighbours():
    tiny = {"section_path": ["A"], "text": "Short.", "pos": 0}
    also_tiny = {"section_path": ["B"], "text": "Also short.", "pos": 10}
    merged = _merge_small_sections([tiny, also_tiny])
    assert len(merged) == 1
    assert "Short." in merged[0]["text"]
    assert "Also short." in merged[0]["text"]


def test_merge_small_sections_leaves_large_sections_separate():
    big1 = {"section_path": ["A"], "text": _LONG_PARA, "pos": 0}
    big2 = {"section_path": ["B"], "text": _LONG_PARA, "pos": 100}
    merged = _merge_small_sections([big1, big2])
    assert len(merged) == 2


def test_merge_small_sections_empty_input():
    assert _merge_small_sections([]) == []


def test_merge_small_sections_single_section():
    sec = {"section_path": ["A"], "text": "Just one.", "pos": 0}
    result = _merge_small_sections([sec])
    assert result == [sec]


# ---------------------------------------------------------------------------
# _split_large_section
# ---------------------------------------------------------------------------


def test_split_large_section_leaves_small_section_intact():
    sec = {"section_path": ["A"], "text": "Small.", "pos": 0}
    result = _split_large_section(sec)
    assert result == [sec]


def test_split_large_section_splits_prose():
    # Build a section that clearly exceeds 500 tokens by joining many paragraphs
    # with double newlines so _split_into_blocks can break them apart.
    big_text = "\n\n".join([_LONG_PARA] * 15)
    sec = {"section_path": ["A"], "text": big_text, "pos": 0}
    result = _split_large_section(sec)
    assert len(result) > 1


def test_split_large_section_keeps_table_intact():
    # Table rows that would exceed the target on their own
    rows = "\n".join(f"| Col1 | Col2 | Col3 | Col4 | Col5 |\n| Val{i} | Val{i} | Val{i} | Val{i} | Val{i} |" for i in range(50))
    table_text = "| H1 | H2 | H3 | H4 | H5 |\n| -- | -- | -- | -- | -- |\n" + rows
    pre = "Some prose before the table.\n\n"
    sec = {"section_path": ["A"], "text": pre + table_text, "pos": 0}
    result = _split_large_section(sec)
    # The table block must appear as a single un-split chunk
    table_chunks = [r for r in result if "|" in r["text"]]
    assert len(table_chunks) == 1
    assert rows[:20] in table_chunks[0]["text"]


# ---------------------------------------------------------------------------
# _window_chunks
# ---------------------------------------------------------------------------


def test_window_chunks_produces_output():
    text = (_LONG_PARA + " ") * 30
    windows = _window_chunks(text)
    assert len(windows) > 1


def test_window_chunks_overlaps():
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    text = (_LONG_PARA + " ") * 30
    windows = _window_chunks(text)
    assert len(windows) > 1
    for i in range(len(windows) - 1):
        tokens_i = enc.encode(windows[i])
        tokens_next = enc.encode(windows[i + 1])
        # Last 50 tokens of window i must equal first 50 tokens of window i+1 (exact overlap).
        assert tokens_i[-50:] == tokens_next[:50], f"Window {i} and {i+1} don't share 50-token overlap"


# ---------------------------------------------------------------------------
# chunk() — public entry point
# ---------------------------------------------------------------------------


def test_chunk_heading_based():
    md = f"# Rules\n\n{_LONG_PARA}\n\n# Setup\n\n{_LONG_PARA}\n"
    chunks = chunk(md, BGG_ID, GAME)
    assert len(chunks) >= 2
    paths = [c.section_path for c in chunks]
    assert ["Rules"] in paths
    assert ["Setup"] in paths


def test_chunk_fallback_no_headings():
    # Flat text → windowed fallback, section_path empty
    text = (_LONG_PARA + " ") * 30
    chunks = chunk(text, BGG_ID, GAME)
    assert all(c.section_path == [] for c in chunks)
    assert len(chunks) > 1


def test_chunk_section_path_in_output():
    # Parent must have enough body text (> 60 tokens) so it won't be merged into Child.
    md = f"# Parent\n\n{_LONG_PARA}\n\n## Child\n\n{_LONG_PARA}"
    chunks = chunk(md, BGG_ID, GAME)
    child_chunks = [c for c in chunks if len(c.section_path) == 2]
    assert any(c.section_path == ["Parent", "Child"] for c in child_chunks)


def test_chunk_skips_copyright_section():
    md = (
        "# Rules\n\n" + _LONG_PARA + "\n\n"
        "# Credits\n\nIllustration: Artist Name\nGraphic Design: Designer\n© 2020 Publisher\n"
    )
    chunks = chunk(md, BGG_ID, GAME)
    texts = " ".join(c.text for c in chunks)
    assert "Illustration:" not in texts


def test_chunk_token_counts_match_text():
    md = f"# Rules\n\n{_LONG_PARA}\n"
    chunks = chunk(md, BGG_ID, GAME)
    for c in chunks:
        assert c.token_count == count_tokens(c.text)


def test_chunk_ids_unique_and_sequential():
    md = f"# A\n\n{_LONG_PARA}\n\n# B\n\n{_LONG_PARA}\n"
    chunks = chunk(md, BGG_ID, GAME)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids)), "chunk_ids are not unique"
    assert ids == sorted(ids), "chunk_ids are not sequential"


def test_chunk_page_offsets_assigned():
    md = f"# A\n\n{_LONG_PARA}\n\n# B\n\n{_LONG_PARA}\n"
    # page_offsets: page 1 at char 0, page 2 at char 50
    offsets = [0, 50]
    chunks = chunk(md, BGG_ID, GAME, page_offsets=offsets)
    assert any(c.page is not None for c in chunks)


def test_chunk_no_page_offsets_returns_none_pages():
    md = f"# A\n\n{_LONG_PARA}\n"
    chunks = chunk(md, BGG_ID, GAME, page_offsets=[])
    assert all(c.page is None for c in chunks)
