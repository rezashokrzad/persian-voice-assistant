"""Real Alembic runs against a scratch PostgreSQL database.

These tests do not rely on the application's ``create_all`` schema: they prove that migration
0004 can be applied to a fresh database and to a database that already holds pre-media rows,
and that it can be rolled back without losing those rows.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
MEDIA_TABLES = {"media_items", "user_media_preferences", "recommendation_contexts"}


@pytest.fixture
def scratch_db():
    settings = get_settings()
    name = f"assistant_migrations_{uuid.uuid4().hex[:10]}"
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()

    url = settings.database_url.set(database=name)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    engine = create_engine(url)

    yield config, engine

    engine.dispose()
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_upgrade_head_on_a_fresh_database_and_reversible(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, "head")

    assert MEDIA_TABLES <= _tables(engine)
    assert "agent_runs" in _tables(engine)

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (telegram_user_id, name) VALUES (9001, 'Owner')")
        )
        conn.execute(
            text(
                "INSERT INTO agent_runs (intent, status, input_text, user_id) "
                "SELECT 'movie_series', 'executed', 'یک فیلم پیشنهاد بده', id FROM users "
                "WHERE telegram_user_id = 9001"
            )
        )

    # The new intent is accepted by the extended CHECK constraint.
    assert _count(engine, "agent_runs") == 1

    command.downgrade(config, "0003")

    assert not (MEDIA_TABLES & _tables(engine))
    # The run survives, but its intent is narrowed to a value the old app understands.
    with engine.connect() as conn:
        assert conn.execute(text("SELECT intent FROM agent_runs")).scalar_one() == "unknown"

    command.upgrade(config, "head")
    assert MEDIA_TABLES <= _tables(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs (intent, status, user_id) "
                "SELECT 'movie_series', 'executed', id FROM users "
                "WHERE telegram_user_id = 9001"
            )
        )
    assert _count(engine, "agent_runs") == 2


def test_upgrade_preserves_existing_0003_rows(scratch_db):
    config, engine = scratch_db
    command.upgrade(config, "0003")

    assert not (MEDIA_TABLES & _tables(engine))
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (telegram_user_id, name, email) VALUES (9002, 'Ali', 'ali@example.com')")
        )
        conn.execute(
            text(
                "INSERT INTO contacts (user_id, name, aliases) "
                "SELECT id, 'سارا', '[\"Sara\"]'::jsonb FROM users WHERE telegram_user_id = 9002"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_runs (intent, status, user_id, input_text) "
                "SELECT 'schedule', 'executed', id, 'جلسه فردا' FROM users "
                "WHERE telegram_user_id = 9002"
            )
        )

    command.upgrade(config, "head")

    assert MEDIA_TABLES <= _tables(engine)
    assert _count(engine, "users") == 1
    assert _count(engine, "contacts") == 1
    assert _count(engine, "agent_runs") == 1
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT intent, input_text FROM agent_runs")
        ).one()
    assert row.intent == "schedule" and row.input_text == "جلسه فردا"

    # New media rows work against existing users.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_items (tmdb_id, media_type, title_fa, original_title) "
                "VALUES (550, 'movie', 'باشگاه مشت‌زنی', 'Fight Club')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO user_media_preferences (user_id, media_id, action_type) "
                "SELECT u.id, m.id, 'liked' FROM users u, media_items m "
                "WHERE u.telegram_user_id = 9002 AND m.tmdb_id = 550"
            )
        )

    command.downgrade(config, "0003")
    assert not (MEDIA_TABLES & _tables(engine))
    assert _count(engine, "users") == 1
    assert _count(engine, "contacts") == 1
    assert _count(engine, "agent_runs") == 1
