"""RAG retrieval eval harness (R1): golden set -> Recall@k + MRR report.

Usage (repo root):
    uv run python -m tools.rag_eval --fake            # offline smoke, deterministic
    uv run python -m tools.rag_eval --live --seed     # real embeddings + PG, seeds first
    uv run python -m tools.rag_eval --live --csv out.csv --report docs/tuning/rag_baseline_r1.md

--fake metrics are shape-stable smoke numbers only; real baselines need --live.
"""

import argparse
import csv
from pathlib import Path
from typing import Final

from config.settings import Settings, get_settings, load_app_config
from llm.client import LLMClient
from rag.contracts import RagStore
from rag.fake import HashEmbedLLM, InMemoryRagStore
from tools.rag_eval_core import (
    CaseResult,
    EvalSummary,
    GoldenCase,
    GoldenSetError,
    first_hit_rank,
    load_golden,
    summarize,
)

DEFAULT_GOLDEN: Final = Path("tests/golden/rag_golden.yaml")
DEFAULT_KS: Final = (1, 3, 5, 10)


def evaluate(
    store: RagStore, llm: LLMClient, cases: tuple[GoldenCase, ...], ks: tuple[int, ...]
) -> list[CaseResult]:
    """Run every golden question through the store and score first-hit rank."""
    results: list[CaseResult] = []
    for case in cases:
        embedding = llm.embed([case.question])[0]
        chunks = store.search(embedding, case.tenant, "shared", max(ks))
        rank = first_hit_rank(chunks, case.expect)
        top = chunks[0].document if chunks else "-"
        results.append(CaseResult(case=case, rank=rank, top_document=top))
    return results


def render_markdown(summary: EvalSummary, mode: str) -> str:
    """Deterministic markdown report (no timestamps: reruns diff clean)."""
    ks = [k for k, _ in summary.overall.recall_at_k]
    header = (
        f"# RAG eval report ({mode})\n\n| tag | cases | MRR | {' | '.join(f'R@{k}' for k in ks)} |"
    )
    divider = f"| --- | --- | --- | {' | '.join('---' for _ in ks)} |"
    rows = [
        f"| {tag.label} | {tag.cases} | {tag.mrr:.2f} | "
        f"{' | '.join(f'{value:.2f}' for _k, value in tag.recall_at_k)} |"
        for tag in (summary.overall, *summary.per_tag)
    ]
    return "\n".join([header, divider, *rows]) + "\n"


def write_csv(results: list[CaseResult], ks: tuple[int, ...], path: Path) -> None:
    """Per-question dump for failure inspection."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["id", "tags", "question", "rank", "rr", "top_document", *(f"hit@{k}" for k in ks)]
        )
        for result in results:
            hits = [int(result.rank is not None and result.rank <= k) for k in ks]
            writer.writerow(
                [
                    result.case.id,
                    "+".join(result.case.tags),
                    result.case.question,
                    result.rank if result.rank is not None else "",
                    f"{result.reciprocal_rank:.3f}",
                    result.top_document,
                    *hits,
                ]
            )


def _parse_ks(raw: str) -> tuple[int, ...]:
    """Argparse type: ValueError becomes a usage message (argparse contract)."""
    values = tuple(int(part) for part in raw.split(","))
    if not values or any(value < 1 for value in values):
        msg = f"ks must be comma-separated positive ints: {raw!r}"
        raise ValueError(msg)
    return values


def _build_fake(tenant: str) -> tuple[RagStore, LLMClient]:
    """In-memory store + deterministic hash embeddings, seeded from demo docs."""
    from demo.seed_docs import (  # noqa: PLC0415 (mode opt-in deps)
        HASH_EMBEDDING_MODEL,
        seed_documents,
    )

    store = InMemoryRagStore()
    embedder = HashEmbedLLM()
    seed_documents(store, embedder, load_app_config().rag, tenant, HASH_EMBEDDING_MODEL)
    return store, embedder


def _build_live(seed: bool, tenant: str) -> tuple[RagStore, LLMClient]:
    """PG-backed store + real embeddings; optionally seeds demo docs first."""
    from database.metadata import bootstrap_schema  # noqa: PLC0415 (mode opt-in deps)
    from demo.seed_docs import seed_documents  # noqa: PLC0415 (mode opt-in deps)
    from knowledge.pg import metadata_query  # noqa: PLC0415 (mode opt-in deps)
    from llm.openai_compat import OpenAICompatibleClient  # noqa: PLC0415 (mode opt-in deps)
    from rag.store import PGRagStore  # noqa: PLC0415 (mode opt-in deps)

    settings: Settings = get_settings()
    bootstrap_schema(settings)
    store: RagStore = PGRagStore(metadata_query(settings))
    embedder = OpenAICompatibleClient(settings)
    if seed:
        seed_documents(store, embedder, load_app_config().rag, tenant, settings.embedding_model)
    return store, embedder


def main() -> int:
    """Wire store + embedder per mode, evaluate, emit console + files."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="real PG store + real embeddings")
    mode.add_argument(
        "--fake", action="store_true", help="in-memory store + hash embeddings (default)"
    )
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--ks", type=_parse_ks, default=DEFAULT_KS, help="e.g. 1,3,5,10")
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--seed", action="store_true", help="live mode: seed demo docs first")
    parser.add_argument("--tenant", default="demo")
    args = parser.parse_args()

    try:
        cases = load_golden(args.golden)
    except GoldenSetError as exc:
        print(f"golden set error: {exc.detail}")
        return 2

    store, embedder = _build_live(args.seed, args.tenant) if args.live else _build_fake(args.tenant)
    results = evaluate(store, embedder, cases, args.ks)
    summary = summarize(results, args.ks)
    mode_label = "live" if args.live else "fake"
    print(render_markdown(summary, mode_label).rstrip())
    if args.csv is not None:
        write_csv(results, args.ks, args.csv)
        print(f"csv written: {args.csv}")
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render_markdown(summary, mode_label), encoding="utf-8")
        print(f"report written: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
