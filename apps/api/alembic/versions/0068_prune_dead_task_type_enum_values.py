"""Prune dead values from task_type_enum.

The task_type_enum Postgres type was created with 18 values back when
the product wrapped RebaseKit's 20-API suite. Four of those values
(entity_lookup, screenshot, audio_transcribe, web_intel) were pruned
from the Python codebase months ago when we tore out RebaseKit, but
the Postgres enum kept them because Postgres doesn't support
``DROP VALUE`` on an existing type.

This migration rebuilds the enum with only the 14 valid values
(8 human task types + 6 pipeline-AI primitives) using the standard
Postgres pattern:

  1. CREATE TYPE task_type_enum_new AS ENUM (..14 values..);
  2. ALTER TABLE tasks ALTER COLUMN type TYPE task_type_enum_new
     USING type::text::task_type_enum_new;
  3. DROP TYPE task_type_enum;
  4. ALTER TYPE task_type_enum_new RENAME TO task_type_enum;

Step 2 fails if any row has a type that isn't in the new set, which
is exactly the safety behaviour we want. The upgrade path verifies
zero dead-type rows exist first and bails out loudly with a clearer
error message if they do.

Revision ID: 0068
Revises: 0067
"""
from alembic import op
from sqlalchemy import text

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


_VALID_VALUES = (
    # Human task types (submittable via POST /v1/tasks)
    "label_image",
    "label_text",
    "rate_quality",
    "verify_fact",
    "moderate_content",
    "compare_rank",
    "answer_question",
    "transcription_review",
    # Pipeline-AI primitives (pipeline-only, see routers/pipelines.py)
    "web_research",
    "document_parse",
    "data_transform",
    "llm_generate",
    "pii_detect",
    "code_execute",
)

_DEAD_VALUES = (
    "entity_lookup",
    "screenshot",
    "audio_transcribe",
    "web_intel",
)


def upgrade() -> None:
    # Safety check: abort if any row still uses a dead value. The
    # USING cast below would fail anyway, but this gives a clearer
    # error that tells the operator what to clean up first.
    dead_list = ", ".join(f"'{v}'" for v in _DEAD_VALUES)
    result = op.get_bind().execute(
        text(
            f"SELECT type::text AS t, COUNT(*) AS n FROM tasks "  # noqa: S608
            f"WHERE type::text IN ({dead_list}) GROUP BY type"
        )
    )
    dead_rows = [(row.t, row.n) for row in result]
    if dead_rows:
        summary = ", ".join(f"{t}={n}" for t, n in dead_rows)
        raise RuntimeError(
            "Cannot drop dead task_type_enum values — rows still reference "
            f"them: {summary}. Reassign or delete these rows before re-running."
        )

    new_values_sql = ", ".join(f"'{v}'" for v in _VALID_VALUES)
    op.execute(f"CREATE TYPE task_type_enum_new AS ENUM ({new_values_sql})")
    op.execute(
        "ALTER TABLE tasks "
        "ALTER COLUMN type TYPE task_type_enum_new "
        "USING type::text::task_type_enum_new"
    )
    op.execute("DROP TYPE task_type_enum")
    op.execute("ALTER TYPE task_type_enum_new RENAME TO task_type_enum")


def downgrade() -> None:
    # Rebuild the old 18-value enum. Rolling back only makes sense if
    # nothing was inserted with a "new-only" type between upgrade and
    # downgrade — every row's type is still valid in the old set
    # because the old set is a superset of the new one. Data loss isn't
    # possible; we're only adding values back.
    all_values = _VALID_VALUES + _DEAD_VALUES
    all_values_sql = ", ".join(f"'{v}'" for v in all_values)
    op.execute(f"CREATE TYPE task_type_enum_old AS ENUM ({all_values_sql})")
    op.execute(
        "ALTER TABLE tasks "
        "ALTER COLUMN type TYPE task_type_enum_old "
        "USING type::text::task_type_enum_old"
    )
    op.execute("DROP TYPE task_type_enum")
    op.execute("ALTER TYPE task_type_enum_old RENAME TO task_type_enum")
