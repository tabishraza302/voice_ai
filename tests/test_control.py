from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot.config import Settings
from bot.control import (
    BATCH_PROVIDER,
    STREAMING_PROVIDER,
    apply_mode,
    effective_provider,
    mount_control,
    set_provider,
)
from bot.services import NEMOTRON_MODEL, WHISPER_MODEL


@pytest.fixture(autouse=True)
def reset_provider_override():
    set_provider(None)
    yield
    set_provider(None)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    settings = Settings(_env_file=None)
    mount_control(app, settings)
    return TestClient(app, base_url="http://127.0.0.1:7860")


class TestControlEndpoints:
    def test_get_root_and_settings_are_200_and_name_both_modes(self, client: TestClient):
        for path in ("/", "/settings"):
            response = client.get(path)
            assert response.status_code == 200
            assert "Batch" in response.text
            assert "Streaming" in response.text
            assert BATCH_PROVIDER in response.text
            assert STREAMING_PROVIDER in response.text

    def test_post_settings_stt_form_switches_mode_and_shows_override_hint(
        self, client: TestClient
    ):
        response = client.post(
            "/settings/stt",
            data={"provider": BATCH_PROVIDER},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings"

        get_res = client.get("/settings")
        assert get_res.status_code == 200
        assert f'value="{BATCH_PROVIDER}" checked' in get_res.text
        assert "overrides .env" in get_res.text

    def test_post_settings_stt_json_switches_mode(self, client: TestClient):
        # Batch differs from the configured default, so the assertion
        # fails if the endpoint ignores the JSON body.
        response = client.post(
            "/settings/stt",
            json={"provider": BATCH_PROVIDER},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings"

        get_res = client.get("/settings")
        assert get_res.status_code == 200
        assert f'value="{BATCH_PROVIDER}" checked' in get_res.text

    def test_post_from_the_client_bar_returns_to_the_client(self, client: TestClient):
        response = client.post(
            "/settings/stt",
            data={"provider": STREAMING_PROVIDER, "next": "/client/"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/client/"

    def test_post_return_path_outside_the_known_pages_falls_back(
        self, client: TestClient
    ):
        response = client.post(
            "/settings/stt",
            data={"provider": STREAMING_PROVIDER, "next": "https://example.com/"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings"

    def test_post_invalid_provider_returns_400_and_leaves_effective_unchanged(
        self, client: TestClient
    ):
        settings = Settings(_env_file=None)
        initial_effective = effective_provider(settings)

        response = client.post(
            "/settings/stt",
            data={"provider": "nope"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert BATCH_PROVIDER in response.text
        assert STREAMING_PROVIDER in response.text
        assert effective_provider(settings) == initial_effective

    def test_post_empty_provider_restores_configured_provider(self, client: TestClient):
        client.post(
            "/settings/stt",
            data={"provider": BATCH_PROVIDER},
            follow_redirects=False,
        )
        settings = Settings(_env_file=None)
        assert effective_provider(settings) == BATCH_PROVIDER

        response = client.post(
            "/settings/stt",
            data={"provider": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings"
        assert effective_provider(settings) == settings.stt_provider


class TestClientPage:
    """The switch is on the client page, which is the URL the docs give."""

    INDEX = "<!doctype html><html><body><div id='app'></div></body></html>"

    @pytest.fixture(autouse=True)
    def fake_index(self, monkeypatch):
        import bot.control as control

        monkeypatch.setattr(control, "_client_index_html", lambda: self.INDEX)

    def test_client_serves_the_prebuilt_page_with_the_switch(self, client: TestClient):
        response = client.get("/client/")
        assert response.status_code == 200
        assert "<div id='app'>" in response.text
        assert 'id="voice-ai-stt-bar"' in response.text
        assert 'action="/settings/stt"' in response.text
        assert 'name="next" value="/client/"' in response.text

    def test_bare_client_url_redirects_to_the_slash_url(self, client: TestClient):
        # The page's assets resolve against its own URL; at "/client"
        # without the slash they land on the site root and the app comes
        # up empty.
        response = client.get("/client", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/client/"

    def test_client_bar_marks_the_active_mode(self, client: TestClient):
        set_provider(STREAMING_PROVIDER)
        response = client.get("/client/")

        assert "Streaming · active" in response.text
        # The other mode stays clickable.
        assert f'name="provider" value="{BATCH_PROVIDER}"' in response.text

    def test_client_redirects_to_the_switch_when_the_ui_is_absent(
        self, client: TestClient, monkeypatch
    ):
        import bot.control as control

        monkeypatch.setattr(control, "_client_index_html", lambda: None)

        response = client.get("/client/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/settings"

    def test_inject_appends_the_bar_when_the_page_has_no_body_tag(self):
        import bot.control as control

        page = control.inject_client_bar("<div></div>", BATCH_PROVIDER)
        assert page.startswith("<div></div>")
        assert 'id="voice-ai-stt-bar"' in page


class TestApplyMode:
    def test_apply_mode_with_override_writes_provider_and_nemotron_model(self):
        # Start on the provider the default does not use, so the
        # assertion fails if apply_mode ignores the override.
        set_provider(STREAMING_PROVIDER)
        settings = Settings(
            _env_file=None,
            stt_provider=BATCH_PROVIDER,
            stt_model=WHISPER_MODEL,
        )
        updated = apply_mode(settings)

        assert updated.stt_provider == STREAMING_PROVIDER
        assert updated.stt_model == NEMOTRON_MODEL

    def test_apply_mode_without_override_follows_env_value(self):
        set_provider(None)
        settings = Settings(
            _env_file=None,
            stt_provider="mlx-whisper",
            stt_model=WHISPER_MODEL,
        )
        updated = apply_mode(settings)

        assert updated.stt_provider == "mlx-whisper"
        assert updated.stt_model == WHISPER_MODEL
