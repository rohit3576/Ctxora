"""Chunking: section-aware splitting with size and overlap bounds."""

import hashlib
from dataclasses import dataclass

from rag.contracts import ChunkInsert


def _hash(text: str) -> str:
    """Stable content hash for one chunk."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkedPage:
    """One parsed page: ordinal plus its section-split text."""

    page_number: int
    sections: tuple[tuple[str, str], ...]


def chunk_pages(
    pages: tuple[ChunkedPage, ...], chunk_size: int, chunk_overlap: int
) -> tuple[ChunkInsert, ...]:
    """Split section text into overlapping chunks within size bounds."""
    chunks: list[ChunkInsert] = []
    for page in pages:
        for section_title, text in page.sections:
            for piece in _split(text, chunk_size, chunk_overlap):
                if not piece.strip():
                    continue
                chunks.append(
                    ChunkInsert(
                        page_number=page.page_number,
                        chunk_index=len(chunks),
                        section_title=section_title,
                        chunk_text=piece.strip(),
                        chunk_hash=_hash(piece),
                        embedding=[],
                    )
                )
    return tuple(chunks)


def _split(text: str, size: int, overlap: int) -> list[str]:
    """Word-boundary splitting with overlap between consecutive pieces."""
    words = text.split()
    if not words:
        return []
    pieces: list[str] = []
    step = max(size - overlap, 1)
    start = 0
    while start < len(words):
        pieces.append(" ".join(words[start : start + size]))
        start += step
    return pieces
