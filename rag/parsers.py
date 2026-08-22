"""Document parsers: bytes + filename -> per-page sections.

Markdown and HTML are parsed in-process; PDF, DOCX, and XLSX are delegated
to rag.office so the stubs-poor format libraries stay behind one boundary.
"""

import re
from typing import Final

from bs4 import BeautifulSoup

from rag.chunker import ChunkedPage

_SUPPORTED: Final = (".md", ".txt", ".html", ".htm", ".pdf", ".docx", ".xlsx")


class UnsupportedFormatError(Exception):
    """The uploaded file has no parser."""

    def __init__(self, suffix: str) -> None:
        """Name the unsupported suffix."""
        self.suffix: str = suffix
        super().__init__(f"unsupported document format: {suffix}")


_MD_HEADING: Final = re.compile(r"^(#{1,6})\s+(.*)$")


def supported(suffix: str) -> bool:
    """Whether the suffix has a parser."""
    return suffix.lower() in _SUPPORTED


def parse(filename: str, content: bytes) -> tuple[ChunkedPage, ...]:
    """Dispatch parsing by file suffix.

    Raises:
        UnsupportedFormatError: suffix has no parser.
    """
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in _SUPPORTED:
        raise UnsupportedFormatError(suffix)
    text = content.decode("utf-8", errors="replace")
    if suffix in (".md", ".txt"):
        return _markdown(text)
    if suffix in (".html", ".htm"):
        return _html(text)
    if suffix == ".pdf":
        from rag.office import pdf_pages

        return pdf_pages(content)
    if suffix == ".docx":
        from rag.office import docx_sections

        return docx_sections(content)
    from rag.office import xlsx_pages

    return xlsx_pages(content)


def _markdown(text: str) -> tuple[ChunkedPage, ...]:
    """Split markdown into heading sections on one page."""
    sections: list[tuple[str, str]] = []
    current_title = "Document"
    buffer: list[str] = []
    for line in text.splitlines():
        heading = _MD_HEADING.match(line)
        if heading:
            if buffer:
                sections.append((current_title, "\n".join(buffer)))
                buffer = []
            current_title = heading.group(2).strip()
        else:
            buffer.append(line)
    if buffer:
        sections.append((current_title, "\n".join(buffer)))
    return (ChunkedPage(page_number=1, sections=tuple(sections)),)


def _html(text: str) -> tuple[ChunkedPage, ...]:
    """Extract heading/paragraph structure from HTML on one page."""
    soup = BeautifulSoup(text, "html.parser")
    sections: list[tuple[str, str]] = []
    current_title = "Document"
    buffer: list[str] = []
    for element in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        piece = element.get_text(" ", strip=True)
        if not piece:
            continue
        if element.name in ("h1", "h2", "h3"):
            if buffer:
                sections.append((current_title, " ".join(buffer)))
                buffer = []
            current_title = piece
        else:
            buffer.append(piece)
    if buffer:
        sections.append((current_title, " ".join(buffer)))
    return (ChunkedPage(page_number=1, sections=tuple(sections)),)
