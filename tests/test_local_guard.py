"""State-changing local routes reject foreign origins and rebound hosts."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot.config import Settings
import bot.control as control
from bot.control import mount_control
from bot.local_guard import mount_local_state_guard

@pytest.fixture(autouse=True)
def reset_provider_override(monkeypatch):
    monkeypatch.setattr(control, "_override", None)


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


@pytest.mark.parametrize(
    "loopback_host",
    [
        "127.0.0.2:7860",
        "127.0.0.100:7860",
        "127.1.2.3:7860",
        "[::1]:7860",
        "[::ffff:127.0.0.1]:7860",
        "LOCALHOST:7860",
    ],
)
def test_alternate_loopback_literals_allowed_in_host(loopback_host: str):
    response = _client(_state_app()).post(
        "/state",
        headers={"Host": loopback_host},
    )
    assert response.status_code == 200
    assert response.json() == {"changed": True}


@pytest.mark.parametrize(
    "loopback_origin",
    [
        "http://127.0.0.2:7860",
        "http://127.0.0.100:7860",
        "http://127.1.2.3:7860",
        "http://[::1]:7860",
        "http://127.0.0.2:3001",
    ],
)
def test_alternate_loopback_literals_allowed_in_origin(loopback_origin: str):
    response = _client(_state_app()).post(
        "/state",
        headers={"Origin": loopback_origin},
    )
    assert response.status_code == 200
    assert response.json() == {"changed": True}


def test_port_override_honors_bound_port_and_rejects_default_port():
    app = _state_app()
    client = TestClient(app, base_url="http://127.0.0.1:9000")

    # Requests matching actual binding port 9000 succeed
    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:9000", "Origin": "http://127.0.0.1:9000"},
    )
    assert response.status_code == 200
    assert response.json() == {"changed": True}

    response = client.post(
        "/state",
        headers={"Host": "localhost:9000", "Origin": "http://localhost:9000"},
    )
    assert response.status_code == 200

    response = client.post(
        "/state",
        headers={"Host": "127.0.0.2:9000", "Origin": "http://127.0.0.2:9000"},
    )
    assert response.status_code == 200

    # Studio origin on web_port remains allowed
    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:9000", "Origin": "http://localhost:3001"},
    )
    assert response.status_code == 200

    # Requests with Host using default configured port 7860 are rejected
    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:7860"},
    )
    assert response.status_code == 403
    assert "loopback" in response.json()["detail"].lower()

    # Requests with Origin using default configured port 7860 are rejected
    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:9000", "Origin": "http://127.0.0.1:7860"},
    )
    assert response.status_code == 403
    assert "cross-origin" in response.json()["detail"].lower()


def test_port_override_via_mount_port_kwarg():
    app = FastAPI()
    mount_local_state_guard(app, _settings(), port=8080)

    @app.post("/state")
    async def change_state():
        return {"changed": True}

    client = TestClient(app, base_url="http://127.0.0.1:8080")

    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"},
    )
    assert response.status_code == 200

    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:7860"},
    )
    assert response.status_code == 403

    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:7860"},
    )
    assert response.status_code == 403


def test_port_override_via_runner_global(monkeypatch):
    import pipecat.runner.run as runner

    monkeypatch.setattr(runner, "RUNNER_PORT", 9999)

    app = FastAPI()
    mount_local_state_guard(app, _settings())

    @app.post("/state")
    async def change_state():
        return {"changed": True}

    client = TestClient(app, base_url="http://127.0.0.1:9999")

    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:9999", "Origin": "http://127.0.0.1:9999"},
    )
    assert response.status_code == 200

    response = client.post(
        "/state",
        headers={"Host": "127.0.0.1:7860"},
    )
    assert response.status_code == 403

@pytest.mark.parametrize(
    "rejected_host",
    [
        "0.0.0.0:7860",
        "[::]:7860",
        "192.168.1.1:7860",
        "10.0.0.5:7860",
        "172.16.0.1:7860",
        "0.0.0.0.nip.io:7860",
        "127.0.0.1.nip.io:7860",
        "local.test:7860",
        "localhost:7860@evil.com",
        "localhost:7860/path",
        "localhost:abc",
        "[invalid",
        "[::1:7860",
        "localhost:999999",
        "localhost:-1",
    ],
)
def test_remote_and_rebinding_hosts_rejected(rejected_host: str):
    response = _client(_state_app()).post(
        "/state",
        headers={"Host": rejected_host},
    )
    assert response.status_code == 403
    assert "loopback" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "rejected_origin",
    [
        "http://0.0.0.0:7860",
        "http://[::]:7860",
        "http://192.168.1.5:7860",
        "http://127.0.0.1.nip.io:7860",
        "http://localhost:7860@evil.com",
        "http://localhost:abc",
        "http://[invalid",
        "http://[::1:7860",
        "http://localhost:999999",
        "http://localhost:-1",
        "http://[::1]]:7860",
        "http://127.0.0.1:NaN",
    ],
)
def test_remote_and_rebinding_origins_rejected(rejected_origin: str):
    response = _client(_state_app()).post(
        "/state",
        headers={"Origin": rejected_origin},
    )
    assert response.status_code == 403
    assert "cross-origin" in response.json()["detail"].lower()


def test_control_form_allows_alternate_loopback_and_honors_port_override():
    app = FastAPI()
    mount_control(app, _settings())

    # Alternate loopback host on standard port 7860
    client_7860 = TestClient(app, base_url="http://127.0.0.2:7860")
    response = client_7860.post(
        "/settings/stt",
        data={"provider": "mlx-audio"},
        headers={"Host": "127.0.0.2:7860", "Origin": "http://127.0.0.2:7860"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    # Port override to 9000
    app_override = FastAPI()
    mount_control(app_override, _settings())
    client_9000 = TestClient(app_override, base_url="http://127.0.0.1:9000")

    response = client_9000.post(
        "/settings/stt",
        data={"provider": "mlx-audio"},
        headers={"Host": "127.0.0.1:9000", "Origin": "http://127.0.0.1:9000"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    response = client_9000.post(
        "/settings/stt",
        data={"provider": "mlx-audio"},
        headers={"Host": "127.0.0.1:9000", "Origin": "http://127.0.0.1:7860"},
        follow_redirects=False,
    )
    assert response.status_code == 403

