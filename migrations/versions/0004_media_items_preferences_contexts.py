"""media items, user preferences and recommendation contexts

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18 12:00:00.000000

Additive and reversible: existing users, contacts, calendar and email rows are untouched. The only
change to an existing table is the intent CHECK constraint, extended with 'movie_series'.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _media_enum(name: str) -> sa.Enum:
    return sa.Enum(
        "movie", "tv", name=name, native_enum=False, create_constraint=True, length=32
    )


def _action_enum(name: str) -> sa.Enum:
    return sa.Enum(
        "liked", "disliked", "watched", "watchlist",
        name=name, native_enum=False, create_constraint=True, length=32,
    )  # fmt: skip


def upgrade() -> None:
    # Extend the intent of an agent run. Existing values stay valid.
    op.drop_constraint(op.f("ck_agent_runs_intent"), "agent_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_agent_runs_intent"),
        "agent_runs",
        "intent IN ('schedule', 'email', 'movie_series', 'unknown')",
    )

    op.create_table(
        "media_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("media_type", _media_enum("media_type"), nullable=False),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("title_fa", sa.String(length=500), nullable=False),
        sa.Column("original_title", sa.String(length=500), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("genres", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("vote_average", sa.Float(), nullable=True),
        sa.Column("vote_source", sa.String(length=20), nullable=True),
        sa.Column("poster_path", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_items")),
        sa.UniqueConstraint("tmdb_id", "media_type", name="uq_media_items_tmdb_id_media_type"),
    )
    op.create_index(op.f("ix_media_items_tmdb_id"), "media_items", ["tmdb_id"], unique=False)
    op.create_index(
        "ix_media_items_original_title_year", "media_items", ["original_title", "year"], unique=False
    )

    op.create_table(
        "user_media_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=False),
        sa.Column("action_type", _action_enum("media_action"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_media_preferences_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_items.id"],
            name=op.f("fk_user_media_preferences_media_id_media_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_media_preferences")),
        sa.UniqueConstraint(
            "user_id", "media_id", "action_type",
            name="uq_user_media_preferences_user_id_media_id_action_type",
        ),
    )  # fmt: skip
    op.create_index(
        op.f("ix_user_media_preferences_user_id"), "user_media_preferences", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_user_media_preferences_media_id"), "user_media_preferences", ["media_id"], unique=False
    )
    op.create_index(
        "ix_user_media_preferences_user_id_action_type",
        "user_media_preferences",
        ["user_id", "action_type"],
        unique=False,
    )

    op.create_table(
        "recommendation_contexts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("query_text", sa.String(length=500), nullable=True),
        sa.Column("target_action", _action_enum("media_action_target"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_recommendation_contexts_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_items.id"],
            name=op.f("fk_recommendation_contexts_media_id_media_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_contexts")),
    )
    op.create_index(op.f("ix_recommendation_contexts_user_id"), "recommendation_contexts", ["user_id"], unique=False)
    op.create_index(op.f("ix_recommendation_contexts_media_id"), "recommendation_contexts", ["media_id"], unique=False)
    op.create_index(op.f("ix_recommendation_contexts_status"), "recommendation_contexts", ["status"], unique=False)
    op.create_index(
        "ix_recommendation_contexts_user_id_chat_id_status",
        "recommendation_contexts",
        ["user_id", "chat_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_recommendation_contexts_expires_at", "recommendation_contexts", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_recommendation_contexts_request_hash", "recommendation_contexts", ["request_hash"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_recommendation_contexts_request_hash", table_name="recommendation_contexts")
    op.drop_index("ix_recommendation_contexts_expires_at", table_name="recommendation_contexts")
    op.drop_index("ix_recommendation_contexts_user_id_chat_id_status", table_name="recommendation_contexts")
    op.drop_index(op.f("ix_recommendation_contexts_status"), table_name="recommendation_contexts")
    op.drop_index(op.f("ix_recommendation_contexts_media_id"), table_name="recommendation_contexts")
    op.drop_index(op.f("ix_recommendation_contexts_user_id"), table_name="recommendation_contexts")
    op.drop_table("recommendation_contexts")

    op.drop_index("ix_user_media_preferences_user_id_action_type", table_name="user_media_preferences")
    op.drop_index(op.f("ix_user_media_preferences_media_id"), table_name="user_media_preferences")
    op.drop_index(op.f("ix_user_media_preferences_user_id"), table_name="user_media_preferences")
    op.drop_table("user_media_preferences")

    op.drop_index("ix_media_items_original_title_year", table_name="media_items")
    op.drop_index(op.f("ix_media_items_tmdb_id"), table_name="media_items")
    op.drop_table("media_items")

    # Keep every run, but an intent the old application does not know becomes 'unknown'.
    op.execute("UPDATE agent_runs SET intent = 'unknown' WHERE intent = 'movie_series'")
    op.drop_constraint(op.f("ck_agent_runs_intent"), "agent_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_agent_runs_intent"),
        "agent_runs",
        "intent IN ('schedule', 'email', 'unknown')",
    )
