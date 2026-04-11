"""Tests for core/task_types.py — the single source of truth for task
type metadata.

The assertions in this file guard invariants that used to live as
module-level ``assert`` statements. Module-level asserts were brittle:
if a future edit violated them the whole app would crash at import
time instead of failing CI. Moving them here means drift shows up as
a red pytest instead of a 500 on production.
"""

from __future__ import annotations

from typing import get_args

import pytest

from core.task_types import (
    AI_TASK_CREDITS,
    AI_TASK_TYPES,
    ALL_TASK_TYPES,
    HUMAN_TASK_BASE_CREDITS,
    HUMAN_TASK_TYPES,
    LLM_TASK_TYPES,
    LOCAL_TASK_TYPES,
    TASK_METADATA,
    TaskTypeMeta,
)


class TestTaskMetadataInvariants:
    def test_metadata_covers_every_type(self):
        assert set(TASK_METADATA.keys()) == ALL_TASK_TYPES

    def test_human_set_is_the_eight_human_types(self):
        assert HUMAN_TASK_TYPES == frozenset(
            {
                "label_image", "label_text", "rate_quality", "verify_fact",
                "moderate_content", "compare_rank", "answer_question",
                "transcription_review",
            }
        )

    def test_ai_set_is_the_six_ai_types(self):
        assert AI_TASK_TYPES == frozenset(
            {
                "llm_generate", "data_transform", "web_research",
                "pii_detect", "document_parse", "code_execute",
            }
        )

    def test_human_and_ai_are_disjoint(self):
        assert HUMAN_TASK_TYPES.isdisjoint(AI_TASK_TYPES)

    def test_all_task_types_is_the_union(self):
        assert ALL_TASK_TYPES == HUMAN_TASK_TYPES | AI_TASK_TYPES

    def test_local_and_llm_partition_the_ai_set(self):
        assert LOCAL_TASK_TYPES | LLM_TASK_TYPES == AI_TASK_TYPES
        assert LOCAL_TASK_TYPES.isdisjoint(LLM_TASK_TYPES)

    def test_local_task_types_are_pii_doc_code(self):
        assert LOCAL_TASK_TYPES == frozenset(
            {"pii_detect", "document_parse", "code_execute"}
        )

    def test_llm_task_types_are_generate_transform_research(self):
        assert LLM_TASK_TYPES == frozenset(
            {"llm_generate", "data_transform", "web_research"}
        )


class TestDerivedCreditMaps:
    def test_human_credits_cover_every_human_type(self):
        assert set(HUMAN_TASK_BASE_CREDITS.keys()) == HUMAN_TASK_TYPES

    def test_ai_credits_cover_every_ai_type(self):
        assert set(AI_TASK_CREDITS.keys()) == AI_TASK_TYPES

    def test_credit_maps_match_metadata_base_credits(self):
        for t, cost in HUMAN_TASK_BASE_CREDITS.items():
            assert TASK_METADATA[t].base_credits == cost
        for t, cost in AI_TASK_CREDITS.items():
            assert TASK_METADATA[t].base_credits == cost

    def test_credit_maps_are_read_only(self):
        # MappingProxyType prevents attribute-style mutation and
        # item assignment at runtime — this is why the module
        # exports wrapped views instead of the raw dicts.
        with pytest.raises(TypeError):
            HUMAN_TASK_BASE_CREDITS["label_text"] = 999  # type: ignore[index]
        with pytest.raises(TypeError):
            AI_TASK_CREDITS["llm_generate"] = 999  # type: ignore[index]


class TestMetadataShape:
    def test_every_meta_is_a_frozen_dataclass(self):
        for meta in TASK_METADATA.values():
            assert isinstance(meta, TaskTypeMeta)
            # slots=True frozen dataclass — mutation should fail
            with pytest.raises((AttributeError, Exception)):
                meta.label = "should fail"  # type: ignore[misc]

    def test_every_meta_has_nonempty_strings(self):
        for meta in TASK_METADATA.values():
            assert meta.id
            assert meta.label
            assert meta.icon
            assert meta.description

    def test_execution_mode_matches_set_membership(self):
        for meta in TASK_METADATA.values():
            if meta.execution_mode == "human":
                assert meta.id in HUMAN_TASK_TYPES
                assert meta.ai_subkind is None
            else:
                assert meta.execution_mode == "ai"
                assert meta.id in AI_TASK_TYPES
                assert meta.ai_subkind in {"local", "llm"}

    def test_base_credits_are_positive(self):
        for meta in TASK_METADATA.values():
            assert meta.base_credits > 0


class TestSchemaLiteralDrift:
    """Guard against ``models.schemas.ALL_TASK_TYPES`` drifting from
    :data:`core.task_types.HUMAN_TASK_TYPES`.

    Pydantic needs a hand-written ``Literal`` for the ``type`` field
    because it evaluates at class-definition time and can't consume a
    runtime frozenset. That means we can't derive the Literal from the
    canonical set — but we *can* fail a test if they drift.
    """

    def test_schemas_literal_matches_canonical_human_set(self):
        from models.schemas import ALL_TASK_TYPES as SCHEMAS_ALL

        literal_values = set(get_args(SCHEMAS_ALL))
        assert literal_values == HUMAN_TASK_TYPES, (
            "models.schemas.ALL_TASK_TYPES Literal drifted from "
            "core.task_types.HUMAN_TASK_TYPES — update the Literal to "
            f"match. Literal has {sorted(literal_values)}, canonical "
            f"HUMAN_TASK_TYPES has {sorted(HUMAN_TASK_TYPES)}."
        )
