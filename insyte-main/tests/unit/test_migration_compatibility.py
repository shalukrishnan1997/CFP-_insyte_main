"""Guardrails for cross-database migration compatibility."""

from pathlib import Path


def test_donor_uuid_migration_avoids_postgres_only_sql() -> None:
    """Keep the donor UUID migration compatible with SQLite-backed tests."""
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "core"
        / "migrations"
        / "0027_donor_uuid_pk.py"
    )
    migration_source = migration_path.read_text()

    disallowed_patterns = [
        "information_schema.columns",
        "ADD COLUMN IF NOT EXISTS",
        "DROP COLUMN IF EXISTS",
        "DO $$",
    ]

    for pattern in disallowed_patterns:
        assert pattern not in migration_source
