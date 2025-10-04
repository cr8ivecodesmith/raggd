from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import raggd.modules.db.migrations as migrations
from raggd.modules.db.migrations import (
    Migration,
    MigrationLoadError,
    MigrationRunner,
)
from raggd.modules.db.uuid7 import ShortUUID7
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


def test_migration_runner_rejects_uuid_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_uuid = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
    second_uuid = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))

    first = Migration(
        uuid=first_uuid,
        short=ShortUUID7("000000000000"),
        up_sql="CREATE TABLE example(id INTEGER PRIMARY KEY);",
        down_sql=None,
        checksum_up="sha256:boot",
        checksum_down=None,
    )
    second = Migration(
        uuid=second_uuid,
        short=ShortUUID7("111111111111"),
        up_sql="ALTER TABLE example ADD COLUMN name TEXT;",
        down_sql="ALTER TABLE example DROP COLUMN name;",
        checksum_up="sha256:next",
        checksum_down="sha256:next-down",
    )

    monkeypatch.setattr(
        migrations,
        "ensure_short_uuid7_order",
        lambda values: False,
    )

    with pytest.raises(MigrationLoadError):
        migrations.MigrationRunner((first, second))


def test_migration_runner_requires_metadata(tmp_path: Path) -> None:
    path = tmp_path / "foobar.up.sql"
    path.write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationLoadError):
        MigrationRunner.from_path(tmp_path)


def test_migration_runner_requires_down_script_for_non_bootstrap(
    tmp_path: Path,
) -> None:
    bootstrap_uuid = generate_uuid7(
        when=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    _write_migration(
        tmp_path,
        identifier=bootstrap_uuid,
        up_sql="CREATE TABLE example(id INTEGER PRIMARY KEY);",
    )

    second_uuid = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))
    short_second = short_uuid7(second_uuid).value
    path = tmp_path / f"{short_second}.up.sql"
    path.write_text(
        f"-- uuid7: {second_uuid}\nALTER TABLE example ADD COLUMN name TEXT;\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationLoadError):
        MigrationRunner.from_path(tmp_path)


def test_migration_runner_detects_filename_mismatch(tmp_path: Path) -> None:
    identifier = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
    short_value = short_uuid7(identifier).value
    path = tmp_path / f"{short_value}.up.sql"
    path.write_text(
        "-- uuid7: 00000000-0000-7000-8000-000000000000\nSELECT 1;\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationLoadError):
        MigrationRunner.from_path(tmp_path)


def test_migration_runner_normalizes_sql(tmp_path: Path) -> None:
    identifier = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
    short_value = _write_migration(
        tmp_path,
        identifier=identifier,
        up_sql="\nSELECT 1;  \n\n",
    )

    runner = MigrationRunner.from_path(tmp_path)
    migration = runner.list_all()[0]
    assert migration.short_value == short_value
    assert migration.up_sql.startswith("-- uuid7:")
    assert migration.up_sql.endswith("SELECT 1;\n")
    assert migration.down_sql is None


def test_migration_runner_downgrade_steps_validation(tmp_path: Path) -> None:
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

    with pytest.raises(ValueError):
        runner.downgrade_plan((first_short, second_short), steps=0)

    plan = runner.downgrade_plan((first_short,), steps=1)
    assert plan.short_values() == ()


def test_migration_runner_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(MigrationLoadError):
        MigrationRunner.from_path(missing)


def test_migration_downgrade_plan_empty_when_unknown(tmp_path: Path) -> None:
    bootstrap_uuid = generate_uuid7(
        when=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    _write_migration(
        tmp_path,
        identifier=bootstrap_uuid,
        up_sql="SELECT 1;",
    )
    runner = MigrationRunner.from_path(tmp_path)
    plan = runner.downgrade_plan(("missing",), steps=1)
    assert plan.short_values() == ()


def test_migration_downgrade_plan_missing_down_script(tmp_path: Path) -> None:
    bootstrap_uuid = generate_uuid7(
        when=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    next_uuid = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))

    _write_migration(
        tmp_path,
        identifier=bootstrap_uuid,
        up_sql="CREATE TABLE example(id INTEGER PRIMARY KEY);",
    )
    short_next = _write_migration(
        tmp_path,
        identifier=next_uuid,
        up_sql="ALTER TABLE example ADD COLUMN name TEXT;",
        down_sql="ALTER TABLE example DROP COLUMN name;",
    )

    runner = MigrationRunner.from_path(tmp_path)
    runner._index[short_next].down_sql = None  # type: ignore[attr-defined]

    with pytest.raises(MigrationLoadError):
        runner.downgrade_plan((short_next,), steps=1)


def test_migration_helpers_normalize_and_checksum() -> None:
    assert migrations._normalize_sql(None) == ""
    assert migrations._checksum(None) is None


def test_migration_extract_uuid7_mismatch(tmp_path: Path) -> None:
    identifier = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
    short = short_uuid7(identifier).value
    up_path = tmp_path / f"{short}.up.sql"
    up_path.write_text(
        f"-- uuid7: {identifier}\nSELECT 1;\n",
        encoding="utf-8",
    )
    down_path = tmp_path / f"{short}.down.sql"
    down_path.write_text(
        "-- uuid7: 00000000-0000-7000-8000-000000000000\nSELECT 1;\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationLoadError):
        migrations._extract_uuid7(
            down_path.read_text(),
            down_path,
            expected=identifier,
        )


def _build_migration(
    *,
    identifier,
    short_value: str,
    down_sql: str | None,
) -> Migration:
    return Migration(
        uuid=identifier,
        short=ShortUUID7(short_value),
        up_sql="SELECT 1;\n",
        down_sql=down_sql,
        checksum_up="sha256:1",
        checksum_down="sha256:1" if down_sql else None,
    )


def test_migration_runner_requires_migrations_list() -> None:
    with pytest.raises(MigrationLoadError):
        MigrationRunner(())


def test_migration_runner_detects_ordering_mismatch() -> None:
    first = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
    second = generate_uuid7(when=datetime(2024, 1, 2, tzinfo=timezone.utc))
    short_first = short_uuid7(first).value
    short_second = short_uuid7(second).value

    migrations_list = (
        _build_migration(
            identifier=first,
            short_value=short_second,
            down_sql=None,
        ),
        _build_migration(
            identifier=second,
            short_value=short_first,
            down_sql="SELECT 1;\n",
        ),
    )

    with pytest.raises(MigrationLoadError):
        MigrationRunner(migrations_list)


def test_migration_runner_detects_duplicate_identifiers() -> None:
    identifier = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
    short_value = short_uuid7(identifier).value
    migration = _build_migration(
        identifier=identifier,
        short_value=short_value,
        down_sql="SELECT 1;\n",
    )

    with pytest.raises(MigrationLoadError):
        MigrationRunner((migration, migration))


def test_migration_runner_rejects_bootstrap_down_script() -> None:
    identifier = generate_uuid7(when=datetime(2024, 1, 1, tzinfo=timezone.utc))
    short_value = short_uuid7(identifier).value
    migration = _build_migration(
        identifier=identifier,
        short_value=short_value,
        down_sql="SELECT 1;\n",
    )

    with pytest.raises(MigrationLoadError):
        MigrationRunner((migration,))


def test_migration_loader_skips_directories_and_requires_up(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    with pytest.raises(MigrationLoadError):
        MigrationRunner.from_path(tmp_path)


def test_migration_helpers_normalize_and_checksum_blank() -> None:
    assert migrations._normalize_sql(" ") == ""
    assert migrations._checksum("") is None
