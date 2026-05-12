"""Pydantic v2 models for the indexing pipeline."""

from datetime import datetime

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    chunk_id: str            # f"{bgg_id}_{idx:03d}"
    bgg_id: int
    game_name: str
    section_path: list[str]  # e.g. ["Valid Clues", "Firm Rules"]
    page: int | None
    topics: list[str]        # 3-5 short tags from Haiku
    summary: str             # 1-sentence Haiku summary
    chunk_type: str          # "rules" | "setup" | "variant" | "reference"
    token_count: int
    text: str
    embedding: list[float] | None = Field(default=None, exclude=True)


class GameIndex(BaseModel):
    bgg_id: int
    game_name: str
    source_pdf: str
    indexed_at: datetime
    chunks: list[Chunk]
    qa_flags: list[str]
