from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from raggd.modules.db.migrations import MigrationLoadError, MigrationRunner
from raggd.modules.db.uuid7 import generate_uuid7, short_uuid7


def _write_migration(
    directory: Path,
    *,
    identifier,
    up_sql: str,
    down_sql: str | None = None,
) -> str:
    short = short_uuid7(identifier).value
    up_path = directory / f"{short}.up.sql"
    up_path.write_text(
        f"-- uuid7: {identifier}\n{up_sql}\n",
        encoding="utf-8",
    )
    if down_sql is not None:
        down_path = directory / f"{short}.down.sql"
        down_path.write_text(
            f"-- uuid7: {identifier}\n{down_sql}\n",
            encoding="utf-8",
        )
    return short


def test_migration_runner_loads_migrations(tmp_path: Path) -> None:
    bootstrap_uuid = generate_uuid7(
        when=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    next_uuid = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))

    first_short = _write_migration(
        tmp_path,
        identifier=bootstrap_uuid,
        up_sql="CREATE TABLE example(id INTEGER PRIMARY KEY);",
    )
    second_short = _write_migration(
        tmp_path,
        identifier=next_uuid,
        up_sql="ALTER TABLE example ADD COLUMN name TEXT;",
        down_sql="ALTER TABLE example DROP COLUMN name;",
    )

    runner = MigrationRunner.from_path(tmp_path)

    migrations = runner.list_all()
    assert migrations[0].short_value == first_short
    assert migrations[0].down_sql is None
    assert migrations[1].short_value == second_short
    assert migrations[1].down_sql is not None

    pending_all = runner.pending(())
    assert pending_all.short_values() == (first_short, second_short)

    pending_after_first = runner.pending((first_short,))
    assert pending_after_first.short_values() == (second_short,)

    downgrade_plan = runner.downgrade_plan((first_short, second_short), steps=1)
    assert downgrade_plan.short_values() == (second_short,)


def test_migration_runner_requires_metadata(tmp_path: Path) -> None:
    path = tmp_path / "foobar.up.sql"
    path.write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationLoadError):
        MigrationRunner.from_path(tmp_path)
