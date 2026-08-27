"""R4f metadata + filtered retrieval: deterministic wrong-manual exclusion.

Upload metadata (deviceModel / firmwareVersion / free-form string map)
lives on rag_documents.metadata; RagFilters folds into one containment
map that both stores apply — a filtered search can only ever see
documents whose metadata contains every constraint.
"""

from pathlib import Path

from config.settings import RagConfig
from rag.contracts import DocumentRecord, RagFilters
from rag.fake import HashEmbedLLM, InMemoryRagStore
from rag.ingest import ingest

DOCS_DIR = Path(__file__).parent.parent / "demo" / "docs"
CONFIG = RagConfig(chunking_v2=True)

DS200_V2 = "door-sensor-ds200-manual-v2.md"
GT800_V11 = "gps-tracker-gt800-manual-v1.1.md"


def _ingest(store: InMemoryRagStore, name: str, metadata: dict[str, str] | None) -> DocumentRecord:
    return ingest(
        store,
        HashEmbedLLM(),
        CONFIG,
        "demo",
        name,
        (DOCS_DIR / name).read_bytes(),
        "hash-embed-1536",
        metadata=metadata,
    )


def _seeded() -> InMemoryRagStore:
    store = InMemoryRagStore()
    _ingest(store, DS200_V2, {"deviceModel": "DS-200", "firmwareVersion": "2"})
    _ingest(store, GT800_V11, {"deviceModel": "GT-800", "firmwareVersion": "1.1"})
    return store


class TestRagFilters:
    def test_sugar_fields_fold_into_metadata_keys(self) -> None:
        filters = RagFilters(device_model="GT-800", firmware_version="1.1")

        assert filters.containment() == {
            "deviceModel": "GT-800",
            "firmwareVersion": "1.1",
        }

    def test_free_form_metadata_merges_with_sugar(self) -> None:
        filters = RagFilters(device_model="DS-200", metadata={"site": "north"})

        assert filters.containment() == {"deviceModel": "DS-200", "site": "north"}

    def test_empty_filters_fold_to_empty_map(self) -> None:
        assert RagFilters().containment() == {}


class TestFilteredSearch:
    def test_device_model_filter_makes_other_manuals_unreachable(self) -> None:
        store = _seeded()

        hits = store.search(
            HashEmbedLLM().embed(["error codes voltage"])[0],
            "demo",
            "shared",
            10,
            RagFilters(device_model="GT-800"),
        )

        assert hits
        assert all(h.document == GT800_V11 for h in hits)

    def test_no_match_returns_empty_deterministically(self) -> None:
        store = _seeded()

        hits = store.search(
            HashEmbedLLM().embed(["error codes"])[0],
            "demo",
            "shared",
            10,
            RagFilters(device_model="RU-500"),
        )

        assert hits == []

    def test_firmware_version_filter_selects_within_model(self) -> None:
        store = _seeded()

        hits = store.search(
            HashEmbedLLM().embed(["error codes"])[0],
            "demo",
            "shared",
            10,
            RagFilters(device_model="DS-200", firmware_version="2"),
        )

        assert hits
        assert all(h.document == DS200_V2 for h in hits)

    def test_unfiltered_search_still_sees_everything(self) -> None:
        store = _seeded()

        hits = store.search(HashEmbedLLM().embed(["error codes"])[0], "demo", "shared", 10)

        assert {h.document for h in hits} == {DS200_V2, GT800_V11}

    def test_metadata_less_documents_never_match_a_filter(self) -> None:
        store = InMemoryRagStore()
        _ingest(store, DS200_V2, None)

        hits = store.search(
            HashEmbedLLM().embed(["error codes"])[0],
            "demo",
            "shared",
            10,
            RagFilters(device_model="DS-200"),
        )

        assert hits == []


class TestMetadataRechunk:
    def test_reupload_with_new_metadata_rechunks(self) -> None:
        store = InMemoryRagStore()
        _ingest(store, DS200_V2, {"deviceModel": "DS-200"})

        upgraded = _ingest(store, DS200_V2, {"deviceModel": "DS-200", "firmwareVersion": "2"})

        assert len(store.documents) == 1
        assert store.documents[upgraded.id].metadata == {
            "deviceModel": "DS-200",
            "firmwareVersion": "2",
        }
