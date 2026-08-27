"""Chunking: section-aware splitting with size and overlap bounds.

v1 (chunk_pages): overlapping word windows per section.
v2 (chunk_pages_v2): parent/child chunks — children are structural units
(tables kept whole or split on row boundaries with the header repeated,
numbered steps and paragraphs atomic), parents are whole bounded sections.
Children get embedded; parents get returned. All v2 hashes are salted
"v2:" so v2 chunks can never collide with v1 hashes of identical text.
"""

import hashlib
import re
from dataclasses import dataclass

from rag.contracts import CHILD_KIND, PARENT_KIND, ChunkInsert

_V2_SALT = "v2:"
_PARENT_MAX_FACTOR = 3
_TABLE_HEADER_LINES = 2
_NUMBERED_ITEM: re.Pattern[str] = re.compile(r"^\s*\d{1,3}\.\s")


def _hash(text: str) -> str:
    """Stable content hash for one chunk."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def v2_hash(text: str) -> str:
    """Salted content hash: v2 chunks never collide with v1 hashes."""
    return _hash(_V2_SALT + text)


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


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _blocks(section_text: str) -> list[tuple[str, list[str]]]:
    """Split a section into (kind, lines) runs: table vs prose."""
    blocks: list[tuple[str, list[str]]] = []
    current_lines: list[str] = []
    current_kind = ""

    def close() -> None:
        nonlocal current_lines, current_kind
        if current_lines:
            blocks.append((current_kind, current_lines))
        current_lines = []
        current_kind = ""

    for line in section_text.splitlines():
        if not line.strip():
            close()
            continue
        kind = "table" if _is_table_line(line) else "prose"
        if current_kind and kind != current_kind:
            close()
        current_kind = kind
        current_lines.append(line)
    close()
    return blocks


def _prose_units(lines: list[str]) -> list[str]:
    """Atomic prose units: each numbered item, each plain paragraph."""
    units: list[str] = []
    buffer: list[str] = []

    def close() -> None:
        nonlocal buffer
        if buffer:
            units.append("\n".join(buffer).strip())
        buffer = []

    for line in lines:
        if _NUMBERED_ITEM.match(line):
            close()
            buffer.append(line)
        elif buffer and _NUMBERED_ITEM.match(buffer[0]):
            buffer.append(line)
        else:
            buffer.append(line)
    close()
    return [unit for unit in units if unit]


def _pack_units(units: list[str], max_words: int) -> list[str]:
    """Greedy pack atomic units up to max_words; oversize units word-split."""
    packed: list[str] = []
    buffer: list[str] = []
    buffer_words = 0

    def close() -> None:
        nonlocal buffer, buffer_words
        if buffer:
            packed.append("\n".join(buffer))
        buffer = []
        buffer_words = 0

    for unit in units:
        words = len(unit.split())
        if words > max_words:
            close()
            packed.extend(" ".join(piece.split()) for piece in _split(unit, max_words, 0))
            continue
        if buffer_words + words > max_words and buffer:
            close()
        buffer.append(unit)
        buffer_words += words
    close()
    return packed


def _table_children(lines: list[str], max_words: int) -> list[str]:
    """Whole table when it fits; otherwise row-boundary splits, header repeated."""
    if len(lines) <= _TABLE_HEADER_LINES or len(" ".join(lines).split()) <= max_words:
        return ["\n".join(lines)]
    header, rows = lines[:_TABLE_HEADER_LINES], lines[_TABLE_HEADER_LINES:]
    pieces: list[list[str]] = []
    current: list[str] = []
    current_words = len(" ".join(header).split())

    for row in rows:
        words = len(row.split())
        if current and current_words + words > max_words:
            pieces.append(header + current)
            current = []
            current_words = len(" ".join(header).split())
        current.append(row)
        current_words += words
    if current:
        pieces.append(header + current)
    return ["\n".join(piece) for piece in pieces]


def _section_children(section_text: str, child_max: int) -> list[str]:
    children: list[str] = []
    for kind, lines in _blocks(section_text):
        if kind == "table":
            children.extend(_table_children(lines, child_max))
        else:
            children.extend(_pack_units(_prose_units(lines), child_max))
    return [child for child in children if child.strip()]


def chunk_pages_v2(
    pages: tuple[ChunkedPage, ...], child_max: int, parent_max: int
) -> tuple[ChunkInsert, ...]:
    """Parent/child chunks: structural children, whole-section parents.

    Children are embedding units; parents are bounded joins of their
    children and are what search returns. Every chunk hash is v2-salted.
    """
    if parent_max < child_max:
        msg = f"parent_max ({parent_max}) must be >= child_max ({child_max})"
        raise ValueError(msg)
    chunks: list[ChunkInsert] = []

    def emit(
        page_number: int, section_title: str, text: str, kind: str, parent_hash: str | None
    ) -> None:
        chunks.append(
            ChunkInsert(
                page_number=page_number,
                chunk_index=len(chunks),
                section_title=section_title,
                chunk_text=text,
                chunk_hash=v2_hash(text),
                embedding=[],
                chunk_kind=kind,
                parent_hash=parent_hash,
            )
        )

    for page in pages:
        for section_title, text in page.sections:
            children = _section_children(text, child_max)
            group: list[str] = []
            group_words = 0
            for child in children:
                words = len(child.split())
                if group and group_words + words > parent_max:
                    parent_text = "\n\n".join(group)
                    emit(page.page_number, section_title, parent_text, PARENT_KIND, None)
                    for kid in group:
                        emit(page.page_number, section_title, kid, CHILD_KIND, v2_hash(parent_text))
                    group = []
                    group_words = 0
                group.append(child)
                group_words += words
            if group:
                parent_text = "\n\n".join(group)
                emit(page.page_number, section_title, parent_text, PARENT_KIND, None)
                for kid in group:
                    emit(page.page_number, section_title, kid, CHILD_KIND, v2_hash(parent_text))
    return tuple(chunks)


def parent_max_words(chunk_size: int) -> int:
    """Parent bound: chunk_size * 3 (whole-section parents, bounded)."""
    return chunk_size * _PARENT_MAX_FACTOR
