"""E2E-shape tests for the health endpoints over the real FastAPI app."""

import pytest
from fastapi.testclient import TestClient

from api.schemas import Envelope, HealthLive, HealthReady
from config.settings import Settings
from database import metadata
from main import create_app


def app_with_unreachable_db() -> TestClient:
    settings = Settings(metadata_db_port=1)
    return TestClient(create_app(settings=settings))


def fake_check_ok(_settings: Settings) -> tuple[bool, str]:
    return (True, "ok")


class TestHealthz:
    def test_returns_200_without_touching_any_database(self) -> None:
        client = app_with_unreachable_db()

        response = client.get("/healthz")

        assert response.status_code == 200
        body = Envelope[HealthLive].model_validate_json(response.content)
        assert body.status == "Success"
        assert body.data == HealthLive(status="ok")


class TestReadyz:
    def test_returns_503_when_metadata_db_unreachable(self) -> None:
        client = app_with_unreachable_db()

        response = client.get("/readyz")

        assert response.status_code == 503
        body = Envelope[HealthReady].model_validate_json(response.content)
        assert body.status == "Failure"
        assert body.data == HealthReady(status="unavailable", metadata_db="unreachable")

    def test_returns_200_when_metadata_db_reachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(metadata, "check_metadata_db", fake_check_ok)
        client = app_with_unreachable_db()

        response = client.get("/readyz")

        assert response.status_code == 200
        body = Envelope[HealthReady].model_validate_json(response.content)
        assert body.status == "Success"
        assert body.data == HealthReady(status="ready", metadata_db="ok")
