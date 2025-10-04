"""Migration discovery and execution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Iterator, Sequence
import uuid
import hashlib

from .uuid7 import ShortUUID7, ensure_short_uuid7_order, short_uuid7

__all__ = [
    "MigrationLoadError",
    "Migration",
    "MigrationPlan",
    "MigrationRunner",
]


_METADATA_PATTERN = re.compile(r"^--\s*uuid7:\s*(?P<uuid>[0-9a-fA-F-]{36})\s*$")


class MigrationLoadError(RuntimeError):
    """Raised when migration resources are malformed."""


@dataclass(slots=True)
class Migration:
    """Represents a paired up/down migration script."""

    uuid: uuid.UUID
    short: ShortUUID7
    up_sql: str
    down_sql: str | None
    checksum_up: str
    checksum_down: str | None

    @property
    def short_value(self) -> str:
        return self.short.value


@dataclass(slots=True)
class MigrationPlan:
    """Sequence of migrations to apply or rollback."""

    migrations: tuple[Migration, ...]

    def short_values(self) -> tuple[str, ...]:
        return tuple(m.short_value for m in self.migrations)


class MigrationRunner:
    """Load migrations and orchestrate upgrade/downgrade plans."""

    def __init__(self, migrations: Sequence[Migration]) -> None:
        if not migrations:
            raise MigrationLoadError("No migrations discovered")

        ordered = tuple(sorted(migrations, key=lambda item: item.short_value))
        canonical_order = ensure_short_uuid7_order(item.uuid for item in ordered)
        if not canonical_order:
            raise MigrationLoadError(
                "shortuuid7 ordering does not match canonical UUID7 ordering"
            )

        self._migrations = ordered
        self._index = {m.short_value: m for m in ordered}
        if len(self._index) != len(ordered):
            raise MigrationLoadError("Duplicate migration identifiers detected")

        bootstrap = ordered[0]
        if bootstrap.down_sql:
            raise MigrationLoadError(
                "Bootstrap migration must not provide a .down script"
            )
        for migration in ordered[1:]:
            if migration.down_sql is None:
                raise MigrationLoadError(
                    f"Missing .down script for migration {migration.short_value}"
                )

    @classmethod
    def from_path(cls, path: Path) -> "MigrationRunner":
        migrations = list(_load_migrations_from_path(path))
        return cls(migrations)

    def list_all(self) -> tuple[Migration, ...]:
        return self._migrations

    def bootstrap(self) -> Migration:
        return self._migrations[0]

    def pending(self, applied: Iterable[str]) -> MigrationPlan:
        applied_set = set(applied)
        migrations = tuple(
            migration
            for migration in self._migrations
            if migration.short_value not in applied_set
        )
        return MigrationPlan(migrations)

    def downgrade_plan(self, applied: Sequence[str], steps: int) -> MigrationPlan:
        if steps < 1:
            raise ValueError("steps must be >= 1")

        applied_order = [value for value in applied if value in self._index]
        if not applied_order:
            return MigrationPlan(())

        to_remove: list[Migration] = []
        remaining_steps = steps
        for short in reversed(applied_order):
            if remaining_steps == 0:
                break
            migration = self._index[short]
            if migration == self.bootstrap():
                break
            if migration.down_sql is None:
                raise MigrationLoadError(
                    f"Cannot downgrade migration {short}; missing .down script"
                )
            to_remove.append(migration)
            remaining_steps -= 1

        return MigrationPlan(tuple(to_remove))


def _load_migrations_from_path(path: Path) -> Iterator[Migration]:
    if not path.exists() or not path.is_dir():
        raise MigrationLoadError(f"Migration path not found: {path}")

    up_scripts: dict[str, Path] = {}
    down_scripts: dict[str, Path] = {}

    for entry in sorted(path.iterdir()):
        if entry.is_dir():
            continue
        name = entry.name
        if name.endswith(".up.sql"):
            short = name[:-7]
            up_scripts[short] = entry
        elif name.endswith(".down.sql"):
            short = name[:-9]
            down_scripts[short] = entry

    if not up_scripts:
        raise MigrationLoadError("No .up.sql migrations discovered")

    migrations: list[Migration] = []
    for short, up_path in up_scripts.items():
        up_sql_raw = up_path.read_text(encoding="utf-8")
        uuid_value = _extract_uuid7(up_sql_raw, up_path)
        short_obj = ShortUUID7(short)
        canonical_short = short_uuid7(uuid_value)
        if canonical_short.value != short_obj.value:
            raise MigrationLoadError(
                (
                    f"Short UUID mismatch for {up_path}: filename {short_obj.value} "
                    f"does not match canonical {canonical_short.value}"
                )
            )

        down_sql_raw: str | None = None
        if short in down_scripts:
            down_sql_raw = down_scripts[short].read_text(encoding="utf-8")
            _extract_uuid7(down_sql_raw, down_scripts[short], expected=uuid_value)

        up_sql = _normalize_sql(up_sql_raw)
        down_sql = _normalize_sql(down_sql_raw) if down_sql_raw else None
        checksum_up = _checksum(up_sql)
        checksum_down = _checksum(down_sql) if down_sql else None

        migrations.append(
            Migration(
                uuid=uuid_value,
                short=short_obj,
                up_sql=up_sql,
                down_sql=down_sql,
                checksum_up=checksum_up,
                checksum_down=checksum_down,
            )
        )

    return iter(migrations)


def _extract_uuid7(
    sql_text: str,
    path: Path,
    *,
    expected: uuid.UUID | None = None,
) -> uuid.UUID:
    first_line = sql_text.splitlines()[0] if sql_text else ""
    match = _METADATA_PATTERN.match(first_line.strip())
    if not match:
        raise MigrationLoadError(
            f"Migration {path} must begin with `-- uuid7: <uuid>` metadata"
        )
    value = uuid.UUID(match.group("uuid"))
    if expected and value != expected:
        raise MigrationLoadError(
            f"Migration {path} uuid7 {value} did not match paired script"
        )
    return value


def _normalize_sql(sql: str | None) -> str:
    if sql is None:
        return ""
    text = sql.replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = [line.rstrip() for line in text.splitlines()]
    normalized = "\n".join(lines).strip()
    if not normalized:
        return ""
    return normalized + "\n"


def _checksum(sql: str | None) -> str | None:
    if not sql:
        return None
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
