from __future__ import annotations

from raggd.modules.db.settings import DbModuleSettings, db_settings_from_mapping


def test_db_settings_from_mapping_honors_overrides() -> None:
    payload = {
        "db": {
            "migrations_path": "./migrations",
            "ensure_auto_upgrade": False,
            "vacuum_max_stale_days": 30,
            "vacuum_concurrency": 8,
            "run_allow_outside": False,
            "run_autocommit_default": True,
            "drift_warning_seconds": 120,
        }
    }

    overrides = {"vacuum_concurrency": 4}
    settings = db_settings_from_mapping(payload, overrides=overrides)

    assert isinstance(settings, DbModuleSettings)
    assert settings.migrations_path == "./migrations"
    assert settings.ensure_auto_upgrade is False
    assert settings.vacuum_max_stale_days == 30
    assert settings.vacuum_concurrency == 4
    assert settings.run_allow_outside is False
    assert settings.run_autocommit_default is True
    assert settings.drift_warning_seconds == 120


def test_db_settings_from_mapping_defaults_when_missing() -> None:
    settings = db_settings_from_mapping(None)

    assert settings.migrations_path == "resources/db/migrations"
    assert settings.ensure_auto_upgrade is True
    assert settings.vacuum_max_stale_days == 7
    assert settings.vacuum_concurrency == "auto"
    assert settings.run_allow_outside is True
    assert settings.run_autocommit_default is False
    assert settings.drift_warning_seconds == 0
