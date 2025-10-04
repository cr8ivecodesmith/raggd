from __future__ import annotations

from raggd.modules.db.resources import resource_path


def test_resource_path_resolves_packaged_resource() -> None:
    path = resource_path("db/migrations")
    assert path.exists()
    assert path.is_dir()
