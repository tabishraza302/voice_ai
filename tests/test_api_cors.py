from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot.bot import mount_api
from bot.config import Settings


def test_api_allows_the_configured_local_studio_origin_only():
    app = FastAPI()
    mount_api(app, Settings(_env_file=None, web_port=3201))
    client = TestClient(app, base_url="http://127.0.0.1:7860")

    allowed = client.options(
        "/api/catalog",
        headers={
            "Origin": "http://localhost:3201",
            "Access-Control-Request-Method": "GET",
        },
    )
    denied = client.options(
        "/api/catalog",
        headers={
            "Origin": "https://outside.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3201"
    assert "access-control-allow-credentials" not in allowed.headers
    assert "access-control-allow-origin" not in denied.headers
