"""Tests for bgg_ingestion.qa — each gate fires on synthetic input."""

from datetime import datetime, timezone

import pytest

from bgg_ingestion.qa import run_qa
from bgg_shared.schema import Chunk, GameIndex


def _make_chunk(
    idx: int,
    text: str,
    token_count: int | None = None,
    section_path: list[str] | None = None,
    topics: list[str] | None = None,
    summary: str = "A summary.",
    chunk_type: str = "rules",
) -> Chunk:
    return Chunk(
        chunk_id=f"99_{idx:03d}",
        bgg_id=99,
        game_name="Test Game",
        section_path=section_path if section_path is not None else ["Rules"],
        page=1,
        topics=topics if topics is not None else ["topic one", "topic two", "topic three"],
        summary=summary,
        chunk_type=chunk_type,
        token_count=token_count if token_count is not None else len(text.split()),
        text=text,
    )


def _make_index(chunks: list[Chunk]) -> GameIndex:
    return GameIndex(
        bgg_id=99,
        game_name="Test Game",
        source_pdf="test.pdf",
        indexed_at=datetime.now(timezone.utc),
        chunks=chunks,
        qa_flags=[],
    )


_NORMAL_TEXT = (
    "Players take turns drawing cards from the deck. "
    "Each player must play at least one card per turn. "
    "The game ends when the deck is empty and all players have played their hands."
)


# ---------------------------------------------------------------------------
# Helper: a "clean" index that should produce no flags
# ---------------------------------------------------------------------------


def _clean_index() -> GameIndex:
    chunks = [
        _make_chunk(i, _NORMAL_TEXT, token_count=50)
        for i in range(4)
    ]
    return _make_index(chunks)


def test_clean_index_has_no_flags():
    assert run_qa(_clean_index()) == []


# ---------------------------------------------------------------------------
# low_chunk_count
# ---------------------------------------------------------------------------


def test_low_chunk_count_fires_for_two_chunks():
    idx = _make_index([
        _make_chunk(0, _NORMAL_TEXT, token_count=50),
        _make_chunk(1, _NORMAL_TEXT, token_count=50),
    ])
    assert "low_chunk_count" in run_qa(idx)


def test_low_chunk_count_fires_for_one_chunk():
    idx = _make_index([_make_chunk(0, _NORMAL_TEXT, token_count=50)])
    assert "low_chunk_count" in run_qa(idx)


def test_low_chunk_count_absent_for_three_chunks():
    idx = _make_index([_make_chunk(i, _NORMAL_TEXT, token_count=50) for i in range(3)])
    assert "low_chunk_count" not in run_qa(idx)


# ---------------------------------------------------------------------------
# no_headings_detected
# ---------------------------------------------------------------------------


def test_no_headings_detected_fires_when_all_empty():
    idx = _make_index([
        _make_chunk(i, _NORMAL_TEXT, token_count=50, section_path=[])
        for i in range(4)
    ])
    assert "no_headings_detected" in run_qa(idx)


def test_no_headings_detected_absent_when_any_has_path():
    chunks = [_make_chunk(i, _NORMAL_TEXT, token_count=50, section_path=[]) for i in range(3)]
    chunks[0] = _make_chunk(0, _NORMAL_TEXT, token_count=50, section_path=["Rules"])
    idx = _make_index(chunks)
    assert "no_headings_detected" not in run_qa(idx)


# ---------------------------------------------------------------------------
# oversized_chunk
# ---------------------------------------------------------------------------


def test_oversized_chunk_fires_above_2000():
    chunks = [_make_chunk(i, _NORMAL_TEXT, token_count=50) for i in range(4)]
    chunks[2] = _make_chunk(2, _NORMAL_TEXT, token_count=2001)
    idx = _make_index(chunks)
    assert "oversized_chunk" in run_qa(idx)


def test_oversized_chunk_absent_at_2000():
    chunks = [_make_chunk(i, _NORMAL_TEXT, token_count=2000) for i in range(4)]
    idx = _make_index(chunks)
    assert "oversized_chunk" not in run_qa(idx)


# ---------------------------------------------------------------------------
# tiny_chunk
# ---------------------------------------------------------------------------


def test_tiny_chunk_fires_below_20():
    chunks = [_make_chunk(i, _NORMAL_TEXT, token_count=50) for i in range(4)]
    chunks[1] = _make_chunk(1, "Hi.", token_count=19)
    idx = _make_index(chunks)
    assert "tiny_chunk" in run_qa(idx)


def test_tiny_chunk_absent_at_20():
    chunks = [_make_chunk(i, _NORMAL_TEXT, token_count=20) for i in range(4)]
    idx = _make_index(chunks)
    assert "tiny_chunk" not in run_qa(idx)


# ---------------------------------------------------------------------------
# low_word_ratio
# ---------------------------------------------------------------------------


def test_low_word_ratio_fires_for_mostly_symbols():
    # Text that is mostly numbers, punctuation, and short tokens — not real English words
    gibberish = "123 456 789 0.1 0.2 0.3 ## ## ## $$ $$ $$ @@ @@ 1a 2b 3c 4d 5e " * 20
    chunks = [_make_chunk(i, gibberish, token_count=50) for i in range(4)]
    idx = _make_index(chunks)
    assert "low_word_ratio" in run_qa(idx)


def test_low_word_ratio_absent_for_normal_english():
    # Normal English prose has well above 50% real words
    chunks = [_make_chunk(i, _NORMAL_TEXT, token_count=50) for i in range(4)]
    idx = _make_index(chunks)
    assert "low_word_ratio" not in run_qa(idx)


# ---------------------------------------------------------------------------
# empty_enrichment
# ---------------------------------------------------------------------------


def test_empty_enrichment_fires_when_no_topics_and_no_summary():
    chunks = [_make_chunk(i, _NORMAL_TEXT, token_count=50) for i in range(4)]
    chunks[0] = _make_chunk(0, _NORMAL_TEXT, token_count=50, topics=[], summary="")
    idx = _make_index(chunks)
    assert "empty_enrichment" in run_qa(idx)


def test_empty_enrichment_absent_when_all_enriched():
    idx = _clean_index()
    assert "empty_enrichment" not in run_qa(idx)


def test_empty_enrichment_absent_when_summary_only():
    # Has no topics but has a summary — should NOT fire
    chunks = [_make_chunk(i, _NORMAL_TEXT, token_count=50) for i in range(4)]
    chunks[0] = _make_chunk(0, _NORMAL_TEXT, token_count=50, topics=[], summary="Has a summary.")
    idx = _make_index(chunks)
    assert "empty_enrichment" not in run_qa(idx)


# ---------------------------------------------------------------------------
# non_english (optional — only runs if langdetect is importable)
# ---------------------------------------------------------------------------


def test_non_english_fires_for_non_english_text():
    pytest.importorskip("langdetect")
    # Spanish text
    spanish = (
        "Los jugadores toman turnos robando cartas del mazo. "
        "Cada jugador debe jugar al menos una carta por turno. "
        "El juego termina cuando el mazo está vacío y todos los jugadores han jugado sus manos. "
    ) * 5
    chunks = [_make_chunk(i, spanish, token_count=80) for i in range(4)]
    idx = _make_index(chunks)
    flags = run_qa(idx)
    assert "non_english" in flags


def test_non_english_absent_for_english_text():
    pytest.importorskip("langdetect")
    idx = _clean_index()
    assert "non_english" not in run_qa(idx)
