"""Chunker v2: parent/child chunks, structure preservation, store parity.

R4d acceptance lives here: children are structural units (whole tables or
row-boundary splits with the header repeated, atomic numbered steps),
parents are bounded whole sections returned by search, hashes are
v2-salted, and re-uploads under a different chunker version re-chunk.
"""

import hashlib
from pathlib import Path

import pytest

from config.settings import RagConfig, load_app_config
from rag.chunker import (
    ChunkedPage,
    chunk_pages,
    chunk_pages_v2,
    parent_max_words,
    v2_hash,
)
from rag.contracts import CHILD_KIND, PARENT_KIND
from rag.fake import HashEmbedLLM, HashScriptedLLM, InMemoryRagStore
from rag.ingest import ingest
from rag.parsers import parse

DOCS_DIR = Path(__file__).parent.parent / "demo" / "docs"
GT800_V11 = "gps-tracker-gt800-manual-v1.1.md"


def _big_table(rows: int) -> str:
    lines = ["| Channel | Parameter | Unit | Cadence | Notes |", "| --- | --- | --- | --- | --- |"]
    lines += [
        f"| CH-{i:03d} | Parameter {i} | unit | {i} s cadence here | note {i} |"
        for i in range(rows)
    ]
    return "\n".join(lines)


class TestStructurePreservation:
    def test_small_table_is_one_whole_child(self) -> None:
        table = "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
        pages = (ChunkedPage(1, (("S", table),)),)

        chunks = chunk_pages_v2(pages, child_max=800, parent_max=2400)

        children = [c for c in chunks if c.chunk_kind == CHILD_KIND]
        assert len(children) == 1
        assert children[0].chunk_text == table

    def test_big_table_splits_on_row_boundaries_with_header_repeated(self) -> None:
        table = _big_table(rows=120)
        pages = (ChunkedPage(1, (("S", table),)),)

        chunks = chunk_pages_v2(pages, child_max=50, parent_max=2400)

        children = [c for c in chunks if c.chunk_kind == CHILD_KIND]
        assert len(children) > 1
        header = "| Channel | Parameter | Unit | Cadence | Notes |"
        for child in children:
            lines = child.chunk_text.splitlines()
            assert lines[0] == header, "every table child repeats the header"
            assert lines[1].startswith("| ---"), "separator row repeats too"
            assert len(lines) > 2, "no empty table child"
            for line in lines[2:]:
                assert line.startswith("| CH-"), "rows never split mid-row"
        body_rows = {line for child in children for line in child.chunk_text.splitlines()[2:]}
        original_rows = set(table.splitlines()[2:])
        assert body_rows == original_rows, "row split loses no rows"

    def test_numbered_steps_stay_atomic(self) -> None:
        steps = "\n".join(f"{i}. Step {i} " + "word " * 8 for i in range(1, 30))
        pages = (ChunkedPage(1, (("S", steps),)),)

        chunks = chunk_pages_v2(pages, child_max=60, parent_max=2400)

        children = [c for c in chunks if c.chunk_kind == CHILD_KIND]
        step_lines = [
            line
            for child in children
            for line in child.chunk_text.splitlines()
            if line.strip() and line.strip()[0].isdigit()
        ]
        assert len(step_lines) == 29, "every step appears exactly once, unsplit"
        assert all("Step " in line for line in step_lines)

    def test_prose_paragraphs_packed_to_child_bound(self) -> None:
        prose = "\n\n".join(f"Paragraph {i} " + "filler " * 15 for i in range(20))
        pages = (ChunkedPage(1, (("S", prose),)),)

        chunks = chunk_pages_v2(pages, child_max=100, parent_max=2400)

        children = [c for c in chunks if c.chunk_kind == CHILD_KIND]
        assert len(children) > 1
        assert all(len(c.chunk_text.split()) <= 100 for c in children)
        assert "Paragraph 0" in children[0].chunk_text
        assert "Paragraph 19" in children[-1].chunk_text


class TestParents:
    def test_every_child_links_to_a_parent_and_parent_contains_children(self) -> None:
        content = (DOCS_DIR / GT800_V11).read_bytes()
        chunks = chunk_pages_v2(
            parse(GT800_V11, content), child_max=800, parent_max=parent_max_words(800)
        )

        parents = {c.chunk_hash: c for c in chunks if c.chunk_kind == PARENT_KIND}
        children = [c for c in chunks if c.chunk_kind == CHILD_KIND]
        assert parents and children
        for child in children:
            assert child.parent_hash in parents, "dangling child"
            parent = parents[child.parent_hash]
            assert parent.section_title == child.section_title
            assert child.chunk_text in parent.chunk_text

    def test_parents_are_bounded(self) -> None:
        content = (DOCS_DIR / GT800_V11).read_bytes()
        parent_max = parent_max_words(800)

        chunks = chunk_pages_v2(parse(GT800_V11, content), child_max=800, parent_max=parent_max)

        for chunk in chunks:
            if chunk.chunk_kind == PARENT_KIND:
                assert len(chunk.chunk_text.split()) <= parent_max

    def test_split_probe_pairs_share_one_parent(self) -> None:
        content = (DOCS_DIR / GT800_V11).read_bytes()
        chunks = chunk_pages_v2(
            parse(GT800_V11, content), child_max=800, parent_max=parent_max_words(800)
        )
        parents = [c for c in chunks if c.chunk_kind == PARENT_KIND]

        for key, answer in (("CH-48", "900 s"), ("CH-92", "11.8 V"), ("PG-9", "2.5 Nm")):
            holders = [p for p in parents if key in p.chunk_text and answer in p.chunk_text]
            assert holders, f"no parent holds both {key} and {answer}"

    def test_parent_max_below_child_max_rejected(self) -> None:
        pages = (ChunkedPage(1, (("S", "text"),)),)

        with pytest.raises(ValueError, match="parent_max"):
            chunk_pages_v2(pages, child_max=800, parent_max=400)


class TestHashSalt:
    def test_v2_hash_differs_from_v1_and_is_deterministic(self) -> None:
        text = "identical text chunk"

        assert v2_hash(text) != hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert v2_hash(text) == v2_hash(text)

    def test_v2_and_v1_chunks_of_same_text_never_collide(self) -> None:
        text = "the same section body"
        v1 = chunk_pages((ChunkedPage(1, (("S", text),)),), 800, 120)
        v2 = chunk_pages_v2((ChunkedPage(1, (("S", text),)),), 800, 2400)

        v1_hashes = {c.chunk_hash for c in v1}
        v2_hashes = {c.chunk_hash for c in v2}
        assert v1_hashes and v2_hashes
        assert v1_hashes.isdisjoint(v2_hashes)


class TestStoreSearchReturnsParents:
    def _seed_v2(self, store: InMemoryRagStore, filename: str) -> None:
        content = (DOCS_DIR / filename).read_bytes()
        config = RagConfig(chunking_v2=True)
        ingest(store, HashEmbedLLM(), config, "demo", filename, content, "hash-embed-1536")

    def test_search_returns_parent_text_not_children(self) -> None:
        store = InMemoryRagStore()
        self._seed_v2(store, GT800_V11)

        results = store.search(
            HashEmbedLLM().embed(["auxiliary tank level reporting interval CH-48"])[0],
            "demo",
            "shared",
            5,
        )

        assert results
        map_hits = [r for r in results if r.section_title == "Telemetry Channel Map"]
        assert map_hits
        winner = map_hits[0]
        assert "CH-48" in winner.chunk_text
        assert "900 s" in winner.chunk_text
        for result in results:
            words = len(result.chunk_text.split())
            assert words > 800 or result.section_title != "Telemetry Channel Map"

    def test_children_of_same_parent_dedupe_into_one_result(self) -> None:
        store = InMemoryRagStore()
        self._seed_v2(store, GT800_V11)

        results = store.search(
            HashEmbedLLM().embed(["telemetry channel cadence unit parameter"])[0],
            "demo",
            "shared",
            10,
        )

        keys = [(r.document, r.page_number, r.section_title, r.chunk_text) for r in results]
        assert len(keys) == len(set(keys)), "duplicate parent results"

    def test_legacy_v1_chunks_still_retrievable_in_mixed_store(self) -> None:
        store = InMemoryRagStore()
        config_v1 = RagConfig(chunking_v2=False)
        ingest(
            store,
            HashEmbedLLM(),
            config_v1,
            "demo",
            "door-sensor-ds200-manual-v1.md",
            (DOCS_DIR / "door-sensor-ds200-manual-v1.md").read_bytes(),
            "hash-embed-1536",
        )
        self._seed_v2(store, GT800_V11)

        assert store.has_parented_chunks("doc-1") is False
        assert store.has_parented_chunks("doc-2") is True

        door_hits = store.search(
            HashEmbedLLM().embed(["error code E-302 sensor signal loss"])[0],
            "demo",
            "shared",
            10,
        )
        assert any(
            r.document == "door-sensor-ds200-manual-v1.md" and "E-302" in r.chunk_text
            for r in door_hits
        ), "v1 chunks must keep answering in a mixed store"

        gt_hits = store.search(
            HashEmbedLLM().embed(["auxiliary tank level channel CH-48"])[0],
            "demo",
            "shared",
            10,
        )
        assert any(r.document == GT800_V11 for r in gt_hits)


class TestIngestRechunk:
    def test_reupload_with_flipped_flag_rechunks(self) -> None:
        store = InMemoryRagStore()
        content = (DOCS_DIR / GT800_V11).read_bytes()
        llm = HashEmbedLLM()

        v1 = ingest(store, llm, RagConfig(chunking_v2=False), "demo", GT800_V11, content, "m")
        assert store.has_parented_chunks(v1.id) is False
        v1_chunks = [c for c, _s, _o in store.chunks[v1.id]]
        assert all(chunk.chunk_kind is None for chunk in v1_chunks)

        v2 = ingest(store, llm, RagConfig(chunking_v2=True), "demo", GT800_V11, content, "m")

        assert len(store.documents) == 1, "old document deleted, exactly one remains"
        assert store.has_parented_chunks(v2.id) is True
        v2_chunks = [c for c, _s, _o in store.chunks[v2.id]]
        assert any(chunk.chunk_kind == PARENT_KIND for chunk in v2_chunks)
        assert any(chunk.chunk_kind == CHILD_KIND for chunk in v2_chunks)
        assert v2.chunk_count != v1.chunk_count, "re-chunked shape must differ from v1"

    def test_same_version_reupload_still_dedupes(self) -> None:
        store = InMemoryRagStore()
        content = (DOCS_DIR / GT800_V11).read_bytes()
        config = RagConfig(chunking_v2=True)

        first = ingest(store, HashEmbedLLM(), config, "demo", GT800_V11, content, "m")
        second = ingest(store, HashEmbedLLM(), config, "demo", GT800_V11, content, "m")

        assert second.id == first.id
        assert len(store.documents) == 1

    def test_parents_never_embedded_children_all_embedded(self) -> None:
        store = InMemoryRagStore()
        recorder = HashScriptedLLM(generated=[])
        content = (DOCS_DIR / GT800_V11).read_bytes()

        ingest(store, recorder, RagConfig(chunking_v2=True), "demo", GT800_V11, content, "m")

        stored = [c for c, _s, _o in store.chunks[next(iter(store.documents))]]
        parents = [c for c in stored if c.chunk_kind == PARENT_KIND]
        children = [c for c in stored if c.chunk_kind == CHILD_KIND]
        assert parents and children
        assert all(c.embedding == [] for c in parents)
        assert all(c.embedding for c in children)
        assert len(recorder.embedded) == len(children), "one embed call per child, none per parent"


class TestGoldenSetUnderV2:
    def test_fake_mode_split_cases_now_hit(self) -> None:
        from tools.rag_eval import evaluate
        from tools.rag_eval_core import load_golden

        config = load_app_config().rag
        assert config.chunking_v2, "defaults.yaml must ship chunking_v2 for this acceptance"
        cases = load_golden(Path(__file__).parent / "golden" / "rag_golden.yaml")
        store = InMemoryRagStore()
        for name in (
            "door-sensor-ds200-manual-v1.md",
            "door-sensor-ds200-manual-v2.md",
            "refrigeration-ru500-manual-v2.0.md",
            "refrigeration-ru500-manual-v2.3.md",
            "gps-tracker-gt800-manual-v1.0.md",
            "gps-tracker-gt800-manual-v1.1.md",
        ):
            ingest(
                store,
                HashEmbedLLM(),
                config,
                "demo",
                name,
                (DOCS_DIR / name).read_bytes(),
                "hash-embed-1536",
            )

        by_id = {r.case.id: r for r in evaluate(store, HashEmbedLLM(), config, cases, ks=(5,))}

        for case_id in ("gt800-tank-note-interval", "gt800-lvd-threshold", "gt800-gland-torque"):
            rank = by_id[case_id].rank
            assert rank is not None and rank <= 5, f"{case_id} must hit within R@5 under v2"
