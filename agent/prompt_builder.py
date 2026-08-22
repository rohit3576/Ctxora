"""Prompt assembly: deterministic sections, sliced by resolved keys.

S6+S7 of the pipeline. Retrieval slices the structured knowledge; this
module renders and assembles. Engine specifics come from the Dialect only.
"""

from agent.key_resolver import ResolvedKey
from config.settings import ColumnMapping
from database.contracts import Dialect
from knowledge import render
from knowledge.contracts import SQLExample, TenantKnowledge

_OUTPUT_CONTRACT = """
OUTPUT
  Exactly ONE SQL statement inside ONE ```sql fenced block.
  No prose outside the block. Read-only SELECT statements only.
"""


def _slice_examples(
    knowledge: TenantKnowledge, canonical_keys: tuple[str, ...]
) -> tuple[SQLExample, ...]:
    if not canonical_keys:
        return knowledge.examples[:2]
    matched = [
        example
        for example in knowledge.examples
        if any(key.lower() in example.question.lower() for key in canonical_keys)
    ]
    return tuple(matched[:2]) if matched else knowledge.examples[:1]


def build_prompt(
    dialect: Dialect,
    mapping: ColumnMapping,
    knowledge: TenantKnowledge,
    resolved: tuple[ResolvedKey, ...],
    question: str,
    *,
    session_context: str | None = None,
    examples_override: tuple[SQLExample, ...] | None = None,
) -> tuple[str, str]:
    """Return (system, user) prompts for SQL generation."""
    canonical = tuple(entry.canonical_key for entry in resolved)
    registry = knowledge.keys_by_canonical()
    sliced_keys = tuple(registry[key] for key in canonical if key in registry)
    examples = (
        examples_override
        if examples_override is not None
        else _slice_examples(knowledge, canonical)
    )

    system = "\n".join(
        [
            f"You are a precise SQL generator for a {dialect.name} telemetry database.",
            "",
            "TABLES",
            (
                f"  {mapping.table}({mapping.timestamp}, {mapping.entity_id}, "
                f"{mapping.key}, {mapping.value})"
            ),
            "EAV RULES",
            dialect.eav_system_rules(mapping),
            _OUTPUT_CONTRACT,
        ]
    )

    sections: list[tuple[str, str]] = [
        ("SCHEMA", render.schema_section(knowledge.schema_columns, knowledge.table_metadata)),
        ("TELEMETRY KEYS", render.telemetry_section(sliced_keys)),
        ("BUSINESS RULES", render.rules_section(knowledge.rules)),
        ("SQL EXAMPLES", render.examples_section(examples)),
        ("ALIASES", render.aliases_section(knowledge.aliases)),
        ("TENANT EAV NOTES", knowledge.eav_rules_text),
        ("SESSION DIGEST", session_context or ""),
    ]
    body = [f"=== {label} ===\n{text}" for label, text in sections if text]
    body.append(f"QUESTION: {question}")
    user = "\n\n".join(body)
    return system, user


class PromptBuilder:
    """Reusable prompt assembler bound to one dialect and mapping."""

    def __init__(self, dialect: Dialect, mapping: ColumnMapping) -> None:
        """Bind the dialect and column mapping used for every prompt."""
        self.dialect: Dialect = dialect
        self.mapping: ColumnMapping = mapping

    def build(
        self,
        knowledge: TenantKnowledge,
        resolved: tuple[ResolvedKey, ...],
        question: str,
        session_context: str | None = None,
        examples_override: tuple[SQLExample, ...] | None = None,
    ) -> tuple[str, str]:
        """Assemble (system, user) for one question."""
        return build_prompt(
            self.dialect,
            self.mapping,
            knowledge,
            resolved,
            question,
            session_context=session_context,
            examples_override=examples_override,
        )
