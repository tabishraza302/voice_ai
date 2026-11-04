from __future__ import annotations

import json
from types import SimpleNamespace

from bot.config import Settings
from bot.saas_runtime import mount_saas_runtime
from tests.platform_api.conftest import _qualified_test_report
from tests.platform_api.test_capability_source import _test_settings


class _App:
    def __init__(self) -> None:
        self.state = SimpleNamespace()
        self.routers = []

    def include_router(self, router) -> None:
        self.routers.append(router)

    def add_event_handler(self, _event: str, _handler) -> None:
        raise AssertionError("no platform database URL was configured")


def _runner_settings() -> Settings:
    return Settings(
        _env_file=None,
        platform_api_base_url="http://127.0.0.1:8000",
        platform_session_runner_id="runner-test",
        platform_runtime_service_token="test-service-token-that-is-long-enough",
        platform_web_origin="https://voice.example.test",
        platform_billing_slice_ms="1000",
        platform_billing_permit_ttl_ms="5000",
    )


def _platform_settings(report_path: str | None, report_hash: str | None, report=None):
    updates = {
        "adapter_capability_report_path": report_path,
        "adapter_capability_report_hash": report_hash,
    }
    if report is not None:
        updates.update(
            {
                "deployment_adapter_id": report.adapter_id,
                "deployment_adapter_version": report.adapter_version,
                "deployment_model_revision": report.model_revision,
                "deployment_profile_hash": report.profile_hash,
                "deployment_source_revision": report.source_revision,
            }
        )
    return _test_settings().model_copy(update=updates)


def test_managed_startup_passes_only_loaded_and_independently_verified_report(
    tmp_path, monkeypatch
):
    report = _qualified_test_report()
    report_path = tmp_path / "adapter-report.json"
    report_path.write_text(
        json.dumps(report.model_dump(mode="json")),
        encoding="utf-8",
    )
    platform_settings = _platform_settings(str(report_path), report.report_hash, report)
    monkeypatch.setattr(
        "platform_api.config.get_settings",
        lambda: platform_settings,
    )

    app = _App()
    mount_saas_runtime(app, _runner_settings())

    manager = app.state.voice_session_manager
    assert manager is not None
    assert manager.installed_adapter_report is not None
    assert manager.installed_adapter_report.model_dump(mode="json") == report.model_dump(
        mode="json"
    )
    assert manager._installed_adapter_report_hash == report.report_hash


def test_managed_startup_rejects_missing_or_forged_report(tmp_path, monkeypatch):
    report = _qualified_test_report()
    report_path = tmp_path / "adapter-report.json"
    report_path.write_text(
        json.dumps(report.model_dump(mode="json")),
        encoding="utf-8",
    )

    for settings in (
        _platform_settings(None, None),
        _platform_settings(str(report_path), "0" * 64, report),
    ):
        monkeypatch.setattr("platform_api.config.get_settings", lambda: settings)
        app = _App()
        app.state.installed_adapter_report_hash = "a" * 64
        mount_saas_runtime(app, _runner_settings())
        assert app.state.voice_session_manager is None


def test_local_startup_does_not_load_platform_settings(monkeypatch):
    def unexpected_settings_load():
        raise AssertionError("local-only startup must not require platform configuration")

    monkeypatch.setattr("platform_api.config.get_settings", unexpected_settings_load)
    app = _App()
    mount_saas_runtime(app, Settings(_env_file=None))

    assert app.state.voice_session_manager is None
    assert len(app.routers) == 1
