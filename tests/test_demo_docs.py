"""Demo document corpus locks: the RAG golden set references these files.

R1 seeds four short manuals — two device families, two versions each.
R4a adds the GT-800 family: ~2800-word manuals whose Telemetry Channel Map
(1600+ words) and Installation Procedure (850+ words) exceed the chunk
window, so the current chunker splits them mid-content. Version-
discriminating facts (torque values, error codes, refrigerant types) are
pinned here so a doc edit cannot silently invalidate the golden
expectations in tests/golden/rag_golden.yaml.
"""

from pathlib import Path

import pytest

from config.settings import load_app_config
from rag.chunker import chunk_pages
from rag.parsers import parse

DOCS_DIR = Path(__file__).parent.parent / "demo" / "docs"

DS200_V1 = "door-sensor-ds200-manual-v1.md"
DS200_V2 = "door-sensor-ds200-manual-v2.md"
RU500_V20 = "refrigeration-ru500-manual-v2.0.md"
RU500_V23 = "refrigeration-ru500-manual-v2.3.md"
GT800_V10 = "gps-tracker-gt800-manual-v1.0.md"
GT800_V11 = "gps-tracker-gt800-manual-v1.1.md"

EXPECTED_FILES = (
    DS200_V1,
    DS200_V2,
    RU500_V20,
    RU500_V23,
    GT800_V10,
    GT800_V11,
)

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
    GT800_V10: (
        "Overview",
        "Technical Specifications",
        "Error Codes",
        "Telemetry Channel Map",
        "Installation Procedure",
        "Antenna Alignment",
        "Maintenance",
    ),
    GT800_V11: (
        "Overview",
        "Technical Specifications",
        "Error Codes",
        "Telemetry Channel Map",
        "Installation Procedure",
        "Antenna Alignment",
        "Maintenance",
    ),
}

# Short manuals: every section fits one chunk (the R1 corpus shape).
SINGLE_CHUNK_MANUALS = (DS200_V1, DS200_V2, RU500_V20, RU500_V23)

# R4a long manuals: sections deliberately larger than the chunk window.
OVERSIZED_SECTIONS = {
    GT800_V10: ("Telemetry Channel Map", "Installation Procedure"),
    GT800_V11: ("Telemetry Channel Map", "Installation Procedure"),
}

# Facts present in BOTH versions of a family (codes/parts that persist).
SHARED_FACTS = {
    DS200_V1: ("E-301", "E-302", "TS-001", "TS-002", "12-24V"),
    DS200_V2: ("E-301", "E-302", "TS-001", "TS-002", "12-24V"),
    RU500_V20: ("A-101", "A-102", "A-103", "240 V", "8 A", "1.2 bar", "18 bar"),
    RU500_V23: ("A-101", "A-102", "A-103", "240 V", "8 A", "1.2 bar", "18 bar"),
    GT800_V10: ("E-501", "E-502", "E-503", "CH-48", "CH-92", "900 s", "11.8 V", "PG-9", "2.5 Nm"),
    GT800_V11: ("E-501", "E-502", "E-503", "CH-48", "CH-92", "900 s", "11.8 V", "PG-9", "2.5 Nm"),
}

# Version-discriminating facts: present in this file, absent from its sibling.
EXCLUSIVE_FACTS = {
    DS200_V1: ("battery low", "45 Nm", "90 days"),
    DS200_V2: ("reed-switch drift", "E-304", "50 Nm", "TS-004", "180 days"),
    RU500_V20: ("R-134a", "1.8 kg", "12 A", "22 bar", "2000 running hours"),
    RU500_V23: ("R-450A", "1.6 kg", "11.5 A", "21 bar", "A-104", "2500 running hours"),
    GT800_V10: ("GFX-2.1", "E-504", "30 days", "firmware 1.0"),
    GT800_V11: ("GFX-2.4", "E-512", "15 days", "firmware 1.1"),
}

_FAMILY = {
    DS200_V1: DS200_V2,
    DS200_V2: DS200_V1,
    RU500_V20: RU500_V23,
    RU500_V23: RU500_V20,
    GT800_V10: GT800_V11,
    GT800_V11: GT800_V10,
}

# Split probes (golden ids gt800-tank-note-interval, gt800-lvd-threshold,
# gt800-gland-torque): (section, key string, answer string). Each key and its
# answer sit >120 words apart across a chunk boundary, so no current chunk
# holds both — the failure chunker v2 (R4d) exists to fix.
SPLIT_PROBES = {
    "Telemetry Channel Map": (("CH-48", "900 s"), ("CH-92", "11.8 V")),
    "Installation Procedure": (("PG-9", "2.5 Nm"),),
}


def _read(filename: str) -> str:
    return (DOCS_DIR / filename).read_text(encoding="utf-8")


def _sections(filename: str) -> dict[str, str]:
    pages = parse(filename, _read(filename).encode("utf-8"))
    assert len(pages) == 1
    return dict(pages[0].sections)


@pytest.mark.parametrize(("filename", "sections"), sorted(REQUIRED_SECTIONS.items()))
class TestSectionStructure:
    def test_file_exists_and_parses_to_sections(
        self, filename: str, sections: tuple[str, ...]
    ) -> None:
        parsed = _sections(filename)

        for required in sections:
            assert required in parsed

    def test_short_manual_sections_are_single_chunk_sized(
        self, filename: str, sections: tuple[str, ...]
    ) -> None:
        if filename not in SINGLE_CHUNK_MANUALS:
            pytest.skip("long manuals deliberately exceed the chunk window")
        for text in _sections(filename).values():
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


def _without_geofence_row(section_text: str) -> str:
    return "\n".join(
        "CH-09-ROW" if line.startswith("| CH-09 |") else line for line in section_text.splitlines()
    )


class TestLongManualFailureModes:
    @pytest.mark.parametrize(("filename", "sections"), sorted(OVERSIZED_SECTIONS.items()))
    def test_oversized_sections_exceed_chunk_window(
        self, filename: str, sections: tuple[str, ...]
    ) -> None:
        config = load_app_config().rag
        parsed = _sections(filename)

        for title in sections:
            assert len(parsed[title].split()) > config.chunk_size, title

    @pytest.mark.parametrize("filename", [GT800_V10, GT800_V11])
    def test_split_probes_straddle_chunk_boundaries(self, filename: str) -> None:
        config = load_app_config().rag
        parsed = _sections(filename)
        pages = parse(filename, _read(filename).encode("utf-8"))
        chunks = chunk_pages(pages, config.chunk_size, config.chunk_overlap)

        for title, probes in SPLIT_PROBES.items():
            section_text = parsed[title].lower()
            for key, answer in probes:
                assert key.lower() in section_text and answer.lower() in section_text
                colocated = [
                    chunk
                    for chunk in chunks
                    if chunk.section_title == title
                    and key.lower() in chunk.chunk_text.lower()
                    and answer.lower() in chunk.chunk_text.lower()
                ]
                assert not colocated, (
                    f"{filename}: {key}+{answer} share a chunk under the current "
                    "chunker; the split-probe corpus lost its failure mode"
                )

    @pytest.mark.parametrize(
        ("filename", "cadence"),
        [(GT800_V10, "60 s"), (GT800_V11, "15 s")],
    )
    def test_geofence_channel_cadence_pins_the_version(self, filename: str, cadence: str) -> None:
        rows = [line for line in _read(filename).splitlines() if line.startswith("| CH-09 |")]

        assert len(rows) == 1
        assert f"| {cadence} |" in rows[0]

    @pytest.mark.parametrize("filename", [GT800_V10, GT800_V11])
    def test_long_sections_identical_across_versions_except_geofence_row(
        self, filename: str
    ) -> None:
        sibling = _FAMILY[filename]
        mine = _sections(filename)
        theirs = _sections(sibling)

        assert mine["Installation Procedure"] == theirs["Installation Procedure"]
        assert mine["Antenna Alignment"] == theirs["Antenna Alignment"]
        assert _without_geofence_row(mine["Telemetry Channel Map"]) == _without_geofence_row(
            theirs["Telemetry Channel Map"]
        )


def test_golden_referenced_files_are_the_complete_set() -> None:
    files = {path.name for path in DOCS_DIR.glob("*.md")}

    assert files == set(EXPECTED_FILES)
