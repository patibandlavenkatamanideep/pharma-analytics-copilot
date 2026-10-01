"""The serving process does not hold the database owner credential.

Review: "The current Compose serving container receives PAC_DB_OWNER_PASSWORD.
Separate migration/ingestion credentials from serving credentials." The web
process never creates, alters or loads anything; holding the owner secret
anyway turns a compromise of the web process into a compromise of the schema.

Migrations and loads now run in a one-shot `jobs` container. The serving
process refuses to start in the cloud environment if it is handed the owner
credential, and CI starts the real image both ways.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

pytestmark = pytest.mark.security

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def compose():
    return yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]


def test_the_serving_service_is_not_given_the_owner_password():
    assert "PAC_DB_OWNER_PASSWORD" not in compose()["app"]["environment"]


def test_the_jobs_service_holds_it_and_only_runs_on_demand():
    jobs = compose()["jobs"]
    assert "PAC_DB_OWNER_PASSWORD" in jobs["environment"]
    assert jobs["profiles"] == ["jobs"], "a jobs service started by default would stay up"
    assert "ports" not in jobs


def test_the_credential_check_names_the_owner_secret():
    from app.api.main import serving_credential_problems
    from app.config import Settings

    assert serving_credential_problems(Settings(db_owner_password="")) == []
    found = serving_credential_problems(Settings(db_owner_password="x"))
    assert found and "OWNER credential" in found[0]


def test_the_app_refuses_to_start_in_the_cloud_holding_the_owner_credential(
        authtest_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("PAC_ENVIRONMENT", "cloud")
    monkeypatch.setenv("PAC_DB_OWNER_PASSWORD", "any-value")
    get_settings.cache_clear()
    try:
        from app.api.main import app
        with pytest.raises(RuntimeError, match="OWNER credential"):
            with TestClient(app):
                pass
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
