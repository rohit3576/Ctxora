"""Golden-set models, loading, and retrieval metrics for the RAG eval harness.

The YAML golden set crosses a trust boundary exactly once: yaml.safe_load ->
Pydantic models. Everything downstream receives typed values only.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from rag.contracts import RetrievedChunk

ALLOWED_TAGS: Final = frozenset(
    ("identifier", "paraphrase", "table", "procedure", "followup", "multi-hop")
)


class GoldenSetError(Exception):
    """The golden set file is malformed."""

    def __init__(self, detail: str) -> None:
        """Describe the malformation."""
        self.detail: str = detail
        super().__init__(detail)


class GoldenExpectation(BaseModel):
    """Where the right answer lives: one document (or any of a list) + locator."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    document: str | None = None
    documents: list[str] = Field(default_factory=list)
    section: str | None = None
    page: int | None = None

    @model_validator(mode="after")
    def _exactly_one_document_selector(self) -> "GoldenExpectation":
        if bool(self.document) == bool(self.documents):
            msg = "expect exactly one of 'document' or 'documents'"
            raise ValueError(msg)
        return self

    @property
    def target_documents(self) -> tuple[str, ...]:
        """All filenames a hit may come from."""
        return (self.document,) if self.document else tuple(self.documents)


class GoldenCase(BaseModel):
    """One golden question with its expectation and tags."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    id: str
    tenant: str
    question: str
    context: tuple[str, ...] = ()
    expect: GoldenExpectation
    tags: tuple[str, ...]

    @model_validator(mode="after")
    def _tags_are_known_and_present(self) -> "GoldenCase":
        if not self.tags:
            msg = "tags must not be empty"
            raise ValueError(msg)
        unknown = set(self.tags) - ALLOWED_TAGS
        if unknown:
            msg = f"unknown tags: {sorted(unknown)}"
            raise ValueError(msg)
        return self


_CASE_ADAPTER: Final = TypeAdapter(tuple[GoldenCase, ...])


def load_golden(path: Path) -> tuple[GoldenCase, ...]:
    """Load and validate the golden set.

    Raises:
        GoldenSetError: unparseable YAML, schema violations, or duplicate ids.
    """
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        cases = _CASE_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        msg = f"{path}: {exc.error_count()} schema errors"
        raise GoldenSetError(msg) from exc
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        duplicates = sorted({id_ for id_ in ids if ids.count(id_) > 1})
        msg = f"{path}: duplicate case ids: {duplicates}"
        raise GoldenSetError(msg) from None
    if not cases:
        msg = f"{path}: golden set is empty"
        raise GoldenSetError(msg)
    return cases


def is_hit(chunk: RetrievedChunk, expectation: GoldenExpectation) -> bool:
    """Whether one retrieved chunk satisfies the expectation."""
    if chunk.document not in expectation.target_documents:
        return False
    if (
        expectation.section is not None
        and expectation.section.lower() not in chunk.section_title.lower()
    ):
        return False
    return expectation.page is None or chunk.page_number == expectation.page


def first_hit_rank(chunks: list[RetrievedChunk], expectation: GoldenExpectation) -> int | None:
    """1-based rank of the first satisfying chunk; None when absent."""
    for rank, chunk in enumerate(chunks, start=1):
        if is_hit(chunk, expectation):
            return rank
    return None


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One evaluated golden case."""

    case: GoldenCase
    rank: int | None
    top_document: str

    @property
    def reciprocal_rank(self) -> float:
        """1/rank for hits, 0 for misses."""
        return 0.0 if self.rank is None else 1.0 / self.rank


@dataclass(frozen=True, slots=True)
class TagSummary:
    """Metrics for one tag (or 'overall')."""

    label: str
    cases: int
    recall_at_k: tuple[tuple[int, float], ...]
    mrr: float


@dataclass(frozen=True, slots=True)
class EvalSummary:
    """Aggregated metrics: overall plus one summary per tag."""

    overall: TagSummary
    per_tag: tuple[TagSummary, ...]


def recall_at(results: list[CaseResult], k: int) -> float:
    """Share of cases whose hit rank is within k."""
    if not results:
        return 0.0
    hits = sum(1 for result in results if result.rank is not None and result.rank <= k)
    return hits / len(results)


def mean_reciprocal_rank(results: list[CaseResult]) -> float:
    """MRR over all cases (misses contribute zero)."""
    if not results:
        return 0.0
    return sum(result.reciprocal_rank for result in results) / len(results)


def summarize(results: list[CaseResult], ks: tuple[int, ...]) -> EvalSummary:
    """Aggregate overall and per-tag metrics at each k."""
    tag_labels = sorted({tag for result in results for tag in result.case.tags})

    def summary_for(label: str, subset: list[CaseResult]) -> TagSummary:
        return TagSummary(
            label=label,
            cases=len(subset),
            recall_at_k=tuple((k, recall_at(subset, k)) for k in ks),
            mrr=mean_reciprocal_rank(subset),
        )

    per_tag = tuple(
        summary_for(tag, [r for r in results if tag in r.case.tags]) for tag in tag_labels
    )
    return EvalSummary(overall=summary_for("overall", results), per_tag=per_tag)
