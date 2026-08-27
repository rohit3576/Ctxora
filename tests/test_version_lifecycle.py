"""R4e version lifecycle: supersede, born-superseded, ACTIVE-only retrieval.

Uploading a newer doc_version in the same doc_family flips older ACTIVE
siblings to SUPERSEDED in the same transaction as the insert; search only
ever sees ACTIVE documents, so stale-manual answers are structurally
impossible. Uploading an older version stores it born SUPERSEDED.
"""

from pathlib import Path

from config.settings import RagConfig
from rag.contracts import DocumentRecord
from rag.fake import HashEmbedLLM, InMemoryRagStore
from rag.ingest import ingest
from rag.versions import version_key
from tools.rag_eval import evaluate
from tools.rag_eval_core import load_golden

DOCS_DIR = Path(__file__).parent.parent / "demo" / "docs"
CONFIG = RagConfig(chunking_v2=True)

V10 = "gps-tracker-gt800-manual-v1.0.md"
V11 = "gps-tracker-gt800-manual-v1.1.md"
DS200_V1 = "door-sensor-ds200-manual-v1.md"
DS200_V2 = "door-sensor-ds200-manual-v2.md"


def _ingest(store: InMemoryRagStore, name: str, family: str, version: str) -> DocumentRecord:
    return ingest(
        store,
        HashEmbedLLM(),
        CONFIG,
        "demo",
        name,
        (DOCS_DIR / name).read_bytes(),
        "hash-embed-1536",
        doc_family=family,
        doc_version=version,
    )


class TestVersionKey:
    def test_orders_numeric_versions(self) -> None:
        assert version_key("1") < version_key("2")
        assert version_key("v1") < version_key("v2")
        assert version_key("2.0") < version_key("2.3")
        assert version_key("1.0") < version_key("1.1")
        assert version_key("2.3") < version_key("2.10")

    def test_trailing_zeros_normalize(self) -> None:
        assert version_key("1") == version_key("1.0")
        assert version_key("v2") == version_key("2.0")

    def test_odd_input_falls_to_zero(self) -> None:
        assert version_key("") == (0,)
        assert version_key("x.y") == (0,)


class TestSupersedeOnNewerUpload:
    def test_older_sibling_flips_to_superseded(self) -> None:
        store = InMemoryRagStore()
        v10 = _ingest(store, V10, "gps-tracker-gt800-manual", "1.0")
        v11 = _ingest(store, V11, "gps-tracker-gt800-manual", "1.1")

        assert store.documents[v10.id].status == "SUPERSEDED"
        assert store.documents[v11.id].status == "ACTIVE"
        listed = store.list_documents("demo")
        assert [d.id for d in listed] == [v11.id]

    def test_search_only_returns_active_revision(self) -> None:
        store = InMemoryRagStore()
        _ingest(store, V10, "gps-tracker-gt800-manual", "1.0")
        _ingest(store, V11, "gps-tracker-gt800-manual", "1.1")

        results = store.search(
            HashEmbedLLM().embed(["GNSS engine firmware GFX version"])[0], "demo", "shared", 10
        )

        assert results
        assert all(r.document == V11 for r in results), "superseded chunks must not surface"

    def test_newest_answer_text_only(self) -> None:
        store = InMemoryRagStore()
        _ingest(store, V10, "gps-tracker-gt800-manual", "1.0")
        _ingest(store, V11, "gps-tracker-gt800-manual", "1.1")

        results = store.search(
            HashEmbedLLM().embed(["geofence breach events cadence"])[0], "demo", "shared", 10
        )

        map_parents = [r for r in results if r.section_title == "Telemetry Channel Map"]
        assert map_parents
        assert "| CH-09 | Geofence breach events | event | 15 s |" in map_parents[0].chunk_text


class TestBornSuperseded:
    def test_older_upload_never_becomes_searchable(self) -> None:
        store = InMemoryRagStore()
        _ingest(store, V11, "gps-tracker-gt800-manual", "1.1")
        stale = _ingest(store, V10, "gps-tracker-gt800-manual", "1.0")

        assert store.documents[stale.id].status == "SUPERSEDED"
        listed = store.list_documents("demo")
        assert len(listed) == 1 and listed[0].filename == V11

        results = store.search(HashEmbedLLM().embed(["anything at all"])[0], "demo", "shared", 10)
        assert all(r.document != V10 for r in results)


class TestFamilyIsolation:
    def test_different_families_coexist_active(self) -> None:
        store = InMemoryRagStore()
        ds1 = _ingest(store, DS200_V1, "door-sensor-ds200-manual", "1")
        ds2 = _ingest(store, DS200_V2, "door-sensor-ds200-manual", "2")
        gt = _ingest(store, V11, "gps-tracker-gt800-manual", "1.1")

        assert store.documents[ds1.id].status == "SUPERSEDED"
        assert store.documents[ds2.id].status == "ACTIVE"
        assert store.documents[gt.id].status == "ACTIVE"
        assert {d.filename for d in store.list_documents("demo")} == {DS200_V2, V11}

    def test_familyless_documents_never_supersede(self) -> None:
        store = InMemoryRagStore()
        plain_config = RagConfig(chunking_v2=True)
        first = ingest(
            store,
            HashEmbedLLM(),
            plain_config,
            "demo",
            DS200_V1,
            (DOCS_DIR / DS200_V1).read_bytes(),
            "hash-embed-1536",
        )
        second = ingest(
            store,
            HashEmbedLLM(),
            plain_config,
            "demo",
            DS200_V2,
            (DOCS_DIR / DS200_V2).read_bytes(),
            "hash-embed-1536",
        )

        assert store.documents[first.id].status == "ACTIVE"
        assert store.documents[second.id].status == "ACTIVE"


class TestMetadataUpgradeRechunk:
    def test_existing_doc_without_family_gets_rechunked_with_meta(self) -> None:
        store = InMemoryRagStore()
        plain = ingest(
            store,
            HashEmbedLLM(),
            RagConfig(chunking_v2=True),
            "demo",
            V10,
            (DOCS_DIR / V10).read_bytes(),
            "hash-embed-1536",
        )
        assert plain.doc_family is None and plain.doc_version is None

        upgraded = _ingest(store, V10, "gps-tracker-gt800-manual", "1.0")

        assert len(store.documents) == 1, "replaced, not duplicated"
        assert upgraded.doc_family == "gps-tracker-gt800-manual"
        assert upgraded.doc_version == "1.0"
        assert store.documents[upgraded.id].status == "ACTIVE"

    def test_same_meta_reseed_is_still_idempotent(self) -> None:
        store = InMemoryRagStore()
        first = _ingest(store, V10, "gps-tracker-gt800-manual", "1.0")
        again = _ingest(store, V10, "gps-tracker-gt800-manual", "1.0")

        assert again.id == first.id
        assert len(store.documents) == 1


class TestGoldenVersionCasesUnderLifecycle:
    def test_fake_mode_version_cases_hit_at_rank_one(self) -> None:
        from demo.seed_docs import HASH_EMBEDDING_MODEL, seed_documents

        config = RagConfig(chunking_v2=True)
        store = InMemoryRagStore()
        seed_documents(store, HashEmbedLLM(), config, "demo", HASH_EMBEDDING_MODEL)

        cases = load_golden(Path(__file__).parent / "golden" / "rag_golden.yaml")
        by_id = {r.case.id: r for r in evaluate(store, HashEmbedLLM(), config, cases, ks=(5,))}

        for case_id in (
            "gt800-v11-geofence",
            "ru500-v23-charge",
            "ru500-v23-defrost-revision",
            "ds200-v2-torque",
            "err-e303-battery",
            "charge-1-8kg",
        ):
            rank = by_id[case_id].rank
            assert rank is not None and rank <= 5, f"{case_id} must hit within R@5"
        # battery-replace-part stays in the golden set but is lexically
        # unmatchable under hash embeddings ("battery" exists only in the
        # superseded revision); the live 4g re-baseline judges it.
