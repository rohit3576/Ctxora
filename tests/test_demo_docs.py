"""Demo document corpus locks: the RAG golden set references these files.

R1 (rag upgrade plan) seeds four deterministic manuals — two device families,
two versions each. Version-discriminating facts (torque values, error codes,
refrigerant types) are pinned here so a doc edit cannot silently invalidate
the golden expectations in tests/golden/rag_golden.yaml.
"""

from pathlib import Path

import pytest

from rag.parsers import parse

DOCS_DIR = Path(__file__).parent.parent / "demo" / "docs"

DS200_V1 = "door-sensor-ds200-manual-v1.md"
DS200_V2 = "door-sensor-ds200-manual-v2.md"
RU500_V20 = "refrigeration-ru500-manual-v2.0.md"
RU500_V23 = "refrigeration-ru500-manual-v2.3.md"

EXPECTED_FILES = (DS200_V1, DS200_V2, RU500_V20, RU500_V23)

# Section titles the golden set's `section` expectations match against.
REQUIRED_SECTIONS = {
    DS200_V1: ("Specifications", "Error Codes", "Maintenance Procedure", "Troubleshooting"),
    DS200_V2: (
        "Specifications",
        "Error Codes",
        "Maintenance Procedure",
        "Troubleshooting",
        "Gasket Replacement",
    ),
    RU500_V20: (
        "Electrical Specifications",
        "Refrigerant Specifications",
        "Pressure Settings",
        "Alarm Codes",
        "Service Intervals",
        "Defrost Procedure",
    ),
    RU500_V23: (
        "Electrical Specifications",
        "Refrigerant Specifications",
        "Pressure Settings",
        "Alarm Codes",
        "Service Intervals",
        "Defrost Procedure",
    ),
}

# Facts present in BOTH versions of a family (codes/parts that persist).
SHARED_FACTS = {
    DS200_V1: ("E-301", "E-302", "TS-001", "TS-002", "12-24V"),
    DS200_V2: ("E-301", "E-302", "TS-001", "TS-002", "12-24V"),
    RU500_V20: ("A-101", "A-102", "A-103", "240 V", "8 A", "1.2 bar", "18 bar"),
    RU500_V23: ("A-101", "A-102", "A-103", "240 V", "8 A", "1.2 bar", "18 bar"),
}

# Version-discriminating facts: present in this file, absent from its sibling.
EXCLUSIVE_FACTS = {
    DS200_V1: ("battery low", "45 Nm", "90 days"),
    DS200_V2: ("reed-switch drift", "E-304", "50 Nm", "TS-004", "180 days"),
    RU500_V20: ("R-134a", "1.8 kg", "12 A", "22 bar", "2000 running hours"),
    RU500_V23: ("R-450A", "1.6 kg", "11.5 A", "21 bar", "A-104", "2500 running hours"),
}

_FAMILY = {DS200_V1: DS200_V2, DS200_V2: DS200_V1, RU500_V20: RU500_V23, RU500_V23: RU500_V20}


def _read(filename: str) -> str:
    return (DOCS_DIR / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize(("filename", "sections"), sorted(REQUIRED_SECTIONS.items()))
class TestSectionStructure:
    def test_file_exists_and_parses_to_sections(
        self, filename: str, sections: tuple[str, ...]
    ) -> None:
        pages = parse(filename, _read(filename).encode("utf-8"))

        assert len(pages) == 1  # markdown parser: single page
        titles = [title for title, _text in pages[0].sections]
        for required in sections:
            assert required in titles

    def test_sections_are_single_chunk_sized(
        self, filename: str, sections: tuple[str, ...]
    ) -> None:
        pages = parse(filename, _read(filename).encode("utf-8"))

        for _title, text in pages[0].sections:
            assert len(text.split()) < 200  # stay well under chunk_size=800 words


@pytest.mark.parametrize("filename", sorted(SHARED_FACTS))
class TestSharedFacts:
    def test_family_shared_facts_present(self, filename: str) -> None:
        content = _read(filename).lower()

        for fact in SHARED_FACTS[filename]:
            assert fact.lower() in content, f"shared fact {fact!r} missing from {filename}"


@pytest.mark.parametrize("filename", sorted(EXCLUSIVE_FACTS))
class TestVersionExclusiveFacts:
    def test_own_facts_present(self, filename: str) -> None:
        content = _read(filename).lower()

        for fact in EXCLUSIVE_FACTS[filename]:
            assert fact.lower() in content, f"exclusive fact {fact!r} missing from {filename}"

    def test_sibling_facts_absent(self, filename: str) -> None:
        content = _read(filename).lower()
        sibling = _FAMILY[filename]

        for fact in EXCLUSIVE_FACTS[sibling]:
            assert fact.lower() not in content, f"sibling fact {fact!r} leaked into {filename}"


def test_golden_referenced_files_are_the_complete_set() -> None:
    files = {path.name for path in DOCS_DIR.glob("*.md")}

    assert files == set(EXPECTED_FILES)
