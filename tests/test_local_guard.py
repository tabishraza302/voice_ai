"""State-changing local routes reject foreign origins and rebound hosts."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot.config import Settings
from bot.control import mount_control
from bot.local_guard import mount_local_state_guard


def _settings() -> Settings:
    return Settings(_env_file=None, port=7860, web_port=3001)


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:7860")


def _state_app() -> FastAPI:
    app = FastAPI()
    mount_local_state_guard(app, _settings())

    @app.post("/state")
    async def change_state():
        return {"changed": True}

    @app.get("/state")
    async def read_state():
        return {"changed": False}

    return app


def test_same_origin_state_change_is_allowed():
    response = _client(_state_app()).post(
        "/state", headers={"Origin": "http://127.0.0.1:7860"}
    )

    assert response.status_code == 200
    assert response.json() == {"changed": True}


def test_studio_origin_state_change_is_allowed():
    response = _client(_state_app()).post(
        "/state", headers={"Origin": "http://localhost:3001"}
    )

    assert response.status_code == 200


def test_missing_origin_is_allowed_for_non_browser_clients():
    response = _client(_state_app()).post("/state")

    assert response.status_code == 200


def test_foreign_origin_is_rejected():
    response = _client(_state_app()).post(
        "/state", headers={"Origin": "https://attacker.example"}
    )

    assert response.status_code == 403
    assert "cross-origin" in response.json()["detail"].lower()


def test_rebinding_host_is_rejected_even_without_origin():
    response = _client(_state_app()).post(
        "/state", headers={"Host": "evil.example:7860"}
    )

    assert response.status_code == 403
    assert "loopback" in response.json()["detail"].lower()


def test_non_loopback_host_with_agent_port_is_rejected():
    response = _client(_state_app()).post(
        "/state", headers={"Host": "192.0.2.10:7860"}
    )

    assert response.status_code == 403


def test_read_only_requests_are_not_blocked_by_origin_or_host():
    response = _client(_state_app()).get(
        "/state",
        headers={"Origin": "https://attacker.example", "Host": "evil.example:7860"},
    )

    assert response.status_code == 200
    assert response.json() == {"changed": False}


def test_control_form_rejects_cross_site_post():
    app = FastAPI()
    mount_control(app, _settings())
    client = _client(app)

    response = client.post(
        "/settings/stt",
        data={"provider": "mlx-audio"},
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_control_form_rejects_rebound_host():
    app = FastAPI()
    mount_control(app, _settings())
    client = _client(app)

    response = client.post(
        "/settings/stt",
        data={"provider": "mlx-audio"},
        headers={"Host": "evil.example:7860"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_control_form_allows_its_own_origin():
    app = FastAPI()
    mount_control(app, _settings())
    client = _client(app)

    response = client.post(
        "/settings/stt",
        data={"provider": "mlx-audio"},
        headers={"Origin": "http://localhost:7860"},
        follow_redirects=False,
    )

    assert response.status_code == 303
