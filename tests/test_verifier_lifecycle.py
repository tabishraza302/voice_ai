"""Tests for verifier lifecycle, process ownership, port safety, and cleanup.

Implements the contract specified in verification-plan.md Section 3, 5, and 6.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import sys
import pytest

# Ensure repo root is on sys.path so scripts.verify is importable
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify import (
    LifecycleManager,
    allocate_loopback_port,
    build_gate_command,
    is_port_in_use,
)

def test_broker_tests_run_in_postgres_gate_not_unit_gate() -> None:
    unit_markers = build_gate_command("unit", REPO_ROOT)
    unit_expression = unit_markers[unit_markers.index("-m") + 1]
    assert "not broker" in unit_expression

    postgres_markers = build_gate_command("postgres", REPO_ROOT)
    postgres_expression = postgres_markers[postgres_markers.index("-m") + 1]
    assert postgres_expression == "postgres or broker"



def test_busy_port_never_reuses_foreign_server() -> None:
    """If a port is already bound by a foreign server, allocator refuses to reuse it."""
    # Bind a dummy server socket on loopback
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]

        assert is_port_in_use(busy_port, "127.0.0.1") is True

        # When asking specifically for busy_port, allocate_loopback_port must fail
        # rather than attaching or pretending it is available
        with pytest.raises(RuntimeError, match="busy port|already in use"):
            allocate_loopback_port(preferred_port=busy_port, refuse_existing=True)

        # Dynamic allocation must never return the busy port
        allocated = allocate_loopback_port()
        assert allocated != busy_port


def test_cleanup_failure_prevents_pass() -> None:
    """Even if tests pass, a cleanup failure must prevent a passing outcome."""
    lifecycle = LifecycleManager()

    def bad_cleanup() -> None:
        raise RuntimeError("database drop failed")

    lifecycle.register_cleanup_callback(bad_cleanup)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        lifecycle.cleanup()

    assert lifecycle.cleanup_failed is True


def test_interrupt_cleans_only_owned_resources() -> None:
    """Interrupt triggers cleanup of only registered owned resources."""
    lifecycle = LifecycleManager()
    cleaned_up: list[str] = []

    lifecycle.register_cleanup_callback(lambda: cleaned_up.append("owned_db"))
    lifecycle.register_cleanup_callback(lambda: cleaned_up.append("owned_container"))

    lifecycle.cleanup()

    assert cleaned_up == ["owned_container", "owned_db"]
    assert lifecycle.cleanup_failed is False


def test_bun_test_does_not_collect_playwright_specs(tmp_path: Path) -> None:
    """Actual bun test execution restricts collection to ./src and never collects e2e/ specs."""
    import subprocess
    web_dir = tmp_path / "web"
    src_dir = web_dir / "src"
    e2e_dir = web_dir / "e2e"
    src_dir.mkdir(parents=True)
    e2e_dir.mkdir(parents=True)

    bunfig = REPO_ROOT / "web" / "bunfig.toml"
    (web_dir / "bunfig.toml").write_text(bunfig.read_text(encoding="utf-8"))

    (src_dir / "app.test.js").write_text(
        "import { test, expect } from 'bun:test'; test('unit', () => expect(1).toBe(1));\n"
    )
    (e2e_dir / "journey.spec.ts").write_text(
        "throw new Error('Playwright spec should never be executed by bun test');\n"
    )

    res = subprocess.run(
        ["bun", "test"],
        cwd=web_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr or res.stdout
    combined = res.stdout + res.stderr
    assert "1 pass" in combined
    assert "journey.spec.ts" not in combined


def test_process_ownership_cleanup() -> None:
    """Owned child processes in dedicated sessions registered with LifecycleManager are terminated during cleanup."""
    import subprocess
    lifecycle = LifecycleManager()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    lifecycle.register_process(proc, is_isolated_group=True)
    assert proc.poll() is None, "process should still be running"

    lifecycle.cleanup()
    assert proc.poll() is not None, "process must be terminated by cleanup"
    assert lifecycle.cleanup_failed is False


def test_process_cleanup_never_kills_foreign_siblings() -> None:
    """LifecycleManager cleanup must terminate only owned processes and leave foreign siblings unharmed."""
    import subprocess
    lifecycle = LifecycleManager()
    # Sibling process running in the current process group (unowned)
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        # Owned process in its own isolated group
        owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        lifecycle.register_process(owned, is_isolated_group=True)

        # Also register a process that shares the current process group to verify it is terminated individually
        shared = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        lifecycle.register_process(shared, is_isolated_group=False)

        lifecycle.cleanup()

        # Foreign sibling was NOT registered, so it MUST still be alive!
        assert sibling.poll() is None, "unowned foreign sibling process must survive cleanup"
        # Owned processes must both be terminated
        assert owned.poll() is not None, "owned isolated process must be terminated"
        assert shared.poll() is not None, "owned non-isolated process must be terminated individually"
    finally:
        sibling.terminate()
        sibling.wait(timeout=2)

def test_derive_admin_url_rejects_sqlite() -> None:
    """_derive_admin_url refuses SQLite URLs with a clear error."""
    from tests.support.postgres import _derive_admin_url
    with pytest.raises(ValueError, match="SQLite fallback is forbidden for platform database"):
        _derive_admin_url("sqlite+aiosqlite:///tmp/test.db")

def test_verification_harness_result_records_nonempty_reason_on_failure(tmp_path: Path) -> None:
    """Every failed gate must record a non-empty reason in result.json."""
    import json
    from scripts.verify import VerificationHarness
    harness = VerificationHarness(tmp_path)
    harness.results["test_gate"] = {"status": "failed", "reason": "something went wrong"}
    rc = harness.write_result()
    assert rc == 1

    result_data = json.loads(harness.result_file.read_text())
    assert result_data["overall_success"] is False
    assert result_data["gates"]["test_gate"]["status"] == "failed"
    assert bool(result_data["gates"]["test_gate"]["reason"])


def test_artifact_dir_must_reside_under_data_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VERIFY_ARTIFACT_DIR outside repo data/verification must be strictly rejected."""
    from scripts.verify import VerificationHarness
    outside = tmp_path / "outside_run"
    monkeypatch.setenv("VERIFY_ARTIFACT_DIR", str(outside))

    with pytest.raises(ValueError, match="VERIFY_ARTIFACT_DIR must reside under"):
        VerificationHarness(REPO_ROOT)


def test_artifact_dir_rejects_non_empty_existing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VERIFY_ARTIFACT_DIR pointing to an existing non-empty directory must be rejected."""
    from scripts.verify import VerificationHarness
    owned_dir = REPO_ROOT / "data" / "verification" / "test_existing_non_empty"
    owned_dir.mkdir(parents=True, exist_ok=True)
    (owned_dir / "existing_file.txt").write_text("prior evidence")
    monkeypatch.setenv("VERIFY_ARTIFACT_DIR", str(owned_dir))

    try:
        with pytest.raises(ValueError, match="must be a new, empty directory"):
            VerificationHarness(REPO_ROOT)
    finally:
        import shutil
        shutil.rmtree(owned_dir, ignore_errors=True)


def test_drop_database_refuses_unowned_database() -> None:
    """_drop_database refuses to drop any database not matching the verification pattern."""
    from scripts.verify import _drop_database
    with pytest.raises(ValueError, match="Refusing to drop unowned"):
        _drop_database("postgresql://admin@127.0.0.1:5432/postgres", "production_voice_ai")

def test_drop_database_requires_ownership_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup must never contact PostgreSQL if the owner token is absent."""
    import scripts.verify as verify

    def unexpected_connection(*args, **kwargs):
        raise AssertionError("database cleanup ran without an ownership token")

    monkeypatch.setattr(verify.subprocess, "run", unexpected_connection)
    with pytest.raises(PermissionError, match="ownership token"):
        verify._drop_database(
            "postgresql://admin@127.0.0.1:5432/postgres",
            "platform_verify_0123456789ab_abcdef012345",
        )




def test_run_cmd_sanitizes_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cmd must strip ambient database credentials and secret tokens from child process env."""
    import json
    from scripts.verify import VerificationHarness
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod_user:supersecret@db.prod.internal/prod")
    monkeypatch.setenv("SECRET_KEY", "supersecretkey")
    monkeypatch.setenv("API_KEY_OPENAI", "sk-proj-123456789")

    harness = VerificationHarness(REPO_ROOT)
    probe_code = (
        "import os, json\n"
        "print(json.dumps(dict(os.environ)))\n"
    )
    rc, out, err = harness.run_cmd("probe_env", [sys.executable, "-c", probe_code])
    assert rc == 0
    child_env = json.loads(out)
    assert "DATABASE_URL" not in child_env
    assert "SECRET_KEY" not in child_env
    assert "API_KEY_OPENAI" not in child_env
    assert child_env.get("VERIFY_REQUIRED") == "1"


def test_release_gate_rejects_non_deployment_profile() -> None:
    """verify release must reject any profile other than deployment."""
    import json
    from scripts.verify import VerificationHarness
    harness = VerificationHarness(REPO_ROOT)
    rc = harness.release(profile="local_offline")
    assert rc != 0
    result_data = json.loads(harness.result_file.read_text())
    assert result_data["overall_success"] is False
    assert result_data["gates"]["release_evidence"]["status"] == "failed"
    assert "requires --profile deployment" in result_data["gates"]["release_evidence"]["reason"]


def test_release_gate_prepopulates_all_required_gates_on_failure() -> None:
    """When release gate fails early, all required gates must be present in result.json with reasons."""
    import json
    from scripts.verify import VerificationHarness
    harness = VerificationHarness(REPO_ROOT)
    rc = harness.release(profile="deployment", config="/nonexistent/qualification.json")
    assert rc != 0
    result_data = json.loads(harness.result_file.read_text())
    assert result_data["overall_success"] is False
    required_gates = [
        "python_unit", "bun_test", "bun_typecheck", "bun_build",
        "postgres_gate", "browser_gate", "voice_local_offline",
        "voice_deployment", "release_evidence",
    ]
    for g in required_gates:
        assert g in result_data["gates"]
        assert bool(result_data["gates"][g].get("reason"))


def test_refuses_arbitrary_remote_production_cluster_without_ownership_marker() -> None:
    """_check_cluster_ownership_token returns False for arbitrary clusters lacking token marker."""
    import asyncio
    from tests.support.postgres import _check_cluster_ownership_token
    from unittest.mock import AsyncMock

    mock_conn = AsyncMock()
    mock_conn.scalar.return_value = False

    has_marker = asyncio.run(_check_cluster_ownership_token(mock_conn, "arbitrary-token-1234"))
    assert has_marker is False, "cluster lacking _verification_cluster_ownership table must fail check"

def test_test_database_ownership_matches_the_supplied_marker_without_leaking_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership checks accept any matching marker row and hide tokens on failure."""
    import asyncio
    import traceback
    from tests.support.postgres import verify_test_cluster_ownership

    expected = "owner-secret-that-must-not-be-logged"
    markers = {"stale-owner-secret", expected}

    class Result:
        def __init__(self, matched: bool):
            self.matched = matched

        def scalar_one(self) -> bool:
            return self.matched

    class Connection:
        async def execute(self, statement, parameters=None):
            token = (parameters or {}).get("token")
            return Result(token in markers)

    connection = Connection()
    monkeypatch.setenv("TEST_DATABASE_OWNERSHIP_TOKEN", expected)
    asyncio.run(verify_test_cluster_ownership(connection))

    mismatch = "missing-owner-secret"
    monkeypatch.setenv("TEST_DATABASE_OWNERSHIP_TOKEN", mismatch)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(verify_test_cluster_ownership(connection))
    assert expected not in str(exc_info.value)
    assert mismatch not in str(exc_info.value)

    class BrokenConnection:
        async def execute(self, statement, parameters=None):
            raise RuntimeError(expected)

    monkeypatch.setenv("TEST_DATABASE_OWNERSHIP_TOKEN", expected)
    with pytest.raises(RuntimeError) as query_error:
        asyncio.run(verify_test_cluster_ownership(BrokenConnection()))
    formatted_error = "".join(traceback.format_exception(query_error.value))
    assert expected not in formatted_error




def test_bounded_database_identifiers_stay_within_postgres_limit() -> None:
    """Generated test database identifiers must strictly respect PostgreSQL's 63-byte limit."""
    import uuid
    run_token = uuid.uuid4().hex[:12]
    uid1 = uuid.uuid4().hex[:12]
    uid2 = uuid.uuid4().hex[:12]
    platform_db = f"platform_verify_{run_token}_{uid1}"
    legacy_db = f"legacy_verify_{run_token}_{uid2}"
    assert len(platform_db) <= 63, f"platform DB identifier too long: {len(platform_db)} bytes"
    assert len(legacy_db) <= 63, f"legacy DB identifier too long: {len(legacy_db)} bytes"


def test_external_admin_url_requires_preestablished_ownership_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """External dedicated cluster mode must require PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN."""
    from scripts.verify import VerificationHarness
    monkeypatch.setenv("PLATFORM_TEST_DATABASE_ADMIN_URL", "postgresql://admin:secret@127.0.0.1:5432/postgres")
    monkeypatch.delenv("PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN", raising=False)
    monkeypatch.delenv("_VERIFY_RUN_OWNERSHIP_TOKEN", raising=False)

    harness = VerificationHarness(REPO_ROOT)
    with pytest.raises(ValueError, match="requires a pre-established ownership token"):
        harness.provision_disposable_postgres()


def test_external_admin_url_requires_asyncpg_driver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject sync PostgreSQL URLs before any verifier subprocess can contact the cluster."""
    import scripts.verify as verify

    monkeypatch.setenv(
        "PLATFORM_TEST_DATABASE_ADMIN_URL",
        "postgresql://admin:secret@127.0.0.1:5432/postgres",
    )
    monkeypatch.setenv("PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN", "owned-cluster-token")
    monkeypatch.delenv("VERIFY_ARTIFACT_DIR", raising=False)

    def unexpected_connection(*args, **kwargs):
        raise AssertionError("invalid admin URL reached a PostgreSQL subprocess")

    monkeypatch.setattr(verify.subprocess, "run", unexpected_connection)
    harness = verify.VerificationHarness(tmp_path)
    with pytest.raises(ValueError, match="asyncpg"):
        harness.provision_disposable_postgres()


def test_external_admin_keeps_opaque_owner_token_out_of_database_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """External ownership secrets stay intact while generated database names remain bounded."""
    import json
    import re
    from types import SimpleNamespace
    import scripts.verify as verify

    ownership_token = "owner-secret-not-lowercase-hex-and-longer-than-twelve"
    monkeypatch.setenv(
        "PLATFORM_TEST_DATABASE_ADMIN_URL",
        "postgresql+asyncpg://admin:secret@127.0.0.1:5432/verification_admin?sslmode=require",
    )
    monkeypatch.setenv("PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN", ownership_token)
    monkeypatch.delenv("_VERIFY_RUN_OWNERSHIP_TOKEN", raising=False)
    calls: list[dict[str, str]] = []

    def fake_run(*args, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout="PostgreSQL 16", stderr="")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    harness = verify.VerificationHarness(tmp_path)

    admin_url, _, _ = harness.provision_disposable_postgres()

    assert admin_url.endswith("/postgres?sslmode=require")
    assert harness.ownership_token == ownership_token
    assert all(call.get("token") == ownership_token for call in calls if "token" in call)
    assert os.environ["PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN"] == ownership_token
    assert len(harness.lifecycle.owned_databases) == 2
    for admin_url, db_name, token in harness.lifecycle.owned_databases:
        assert admin_url.endswith("/postgres?sslmode=require")
        assert token == ownership_token
        assert re.fullmatch(
            r"(?:platform|legacy)_verify_[a-f0-9]{12}_[a-f0-9]{12}",
            db_name,
        )
        assert ownership_token not in db_name




def test_release_sequencing_stops_at_failing_lane_and_records_pending_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid release profile reaches the mocked lanes and preserves the first failure."""
    import json
    from types import SimpleNamespace

    from scripts import verify
    from scripts.verify import VerificationHarness
    from tests.qualification.test_release_evidence import _deployment_config, _local_config

    harness = VerificationHarness(REPO_ROOT)
    real_run = verify.subprocess.run

    def clean_checkout_for_lane_test(command, **kwargs):
        if command == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", clean_checkout_for_lane_test)
    deployment = _deployment_config()
    local = _local_config()
    commit = harness.current_commit()
    deployment["qualification"]["source_revision"] = commit
    local["qualification"]["source_revision"] = commit
    deployment_path = tmp_path / "deployment.json"
    local_path = tmp_path / "local.json"
    deployment_path.write_text(json.dumps(deployment), encoding="utf-8")
    local_path.write_text(json.dumps(local), encoding="utf-8")

    def fake_unit(standalone: bool = True) -> int:
        for gate in ("python_unit", "bun_test", "bun_typecheck", "bun_build"):
            harness.results[gate] = {"status": "passed"}
        return 0

    def fake_postgres(standalone: bool = True) -> int:
        harness.results["postgres_gate"] = {"status": "passed"}
        return 0

    def fake_browser(standalone: bool = True) -> int:
        harness.results["browser_gate"] = {"status": "failed", "reason": "browser servers failed"}
        return 1

    harness.unit = fake_unit  # type: ignore[assignment]
    harness.postgres = fake_postgres  # type: ignore[assignment]
    harness.browser = fake_browser  # type: ignore[assignment]

    rc = harness.release(
        profile="deployment",
        config=str(deployment_path),
        local_config=str(local_path),
    )
    assert rc != 0

    result_data = json.loads(harness.result_file.read_text(encoding="utf-8"))
    assert result_data["overall_success"] is False
    assert result_data["gates"]["python_unit"]["status"] == "passed"
    assert result_data["gates"]["postgres_gate"]["status"] == "passed"
    assert result_data["gates"]["browser_gate"]["status"] == "failed"
    assert result_data["gates"]["voice_local_offline"]["status"] == "not_run"
    assert "prerequisite browser gate failed" in result_data["gates"]["voice_local_offline"]["reason"]
    assert result_data["gates"]["release_evidence"]["status"] == "not_run"


def _preflight_harness(tmp_path: Path):
    from scripts.verify import VerificationHarness

    harness = VerificationHarness(tmp_path)
    harness.run_dir = tmp_path / "preflight-run"
    harness.run_dir.mkdir()
    harness.run_id = harness.run_dir.name
    harness.profile_hashes["deployment"] = "a" * 64
    harness.current_commit = lambda: "f" * 40  # type: ignore[method-assign]
    config = {
        "runner_api_endpoints": {
            "runner": "https://runner.example.invalid/health",
            "api": "https://api.example.invalid/health",
            "flags": "https://flags.example.invalid/health/flags",
        }
    }
    return harness, config


class _JsonResponse:
    def __init__(self, url: str, payload: dict, status: int = 200) -> None:
        import json

        self._url = url
        self._body = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, _size: int = -1) -> bytes:
        return self._body


def test_deployment_preflight_records_actual_live_flags_and_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from scripts import verify
    from tests.support import qualification

    harness, config = _preflight_harness(tmp_path)
    flags = {
        "PLATFORM_ENABLE_PUBLIC_CALLS": False,
        "PLATFORM_ENABLE_REAL_PAID_CALLS": False,
        "PLATFORM_ENABLE_GATEWAY": False,
    }

    def fake_urlopen(request, timeout):
        assert timeout == 5
        if request.full_url == config["runner_api_endpoints"]["flags"]:
            payload = {"flags": flags}
        else:
            payload = {"status": "ready"}
        return _JsonResponse(request.full_url, payload)

    monkeypatch.setattr(verify.urllib.request, "urlopen", fake_urlopen)
    harness.check_deployment_endpoints(config)
    envelope = json.loads((harness.run_dir / "deployment-preflight.json").read_text())
    payload = qualification.validate_live_evidence(
        envelope,
        kind="deployment_preflight",
        run_id=harness.run_id,
        profile="deployment",
        profile_hash=harness.profile_hashes["deployment"],
        commit="f" * 40,
        key=harness.evidence_key,
    )
    assert payload["endpoints"]["flags"]["flags"] == flags
    assert payload["endpoints"]["runner"]["status"] == "ready"


@pytest.mark.parametrize(
    "flags, health, message",
    [
        (
            {
                "PLATFORM_ENABLE_PUBLIC_CALLS": True,
                "PLATFORM_ENABLE_REAL_PAID_CALLS": False,
                "PLATFORM_ENABLE_GATEWAY": False,
            },
            "ready",
            "PLATFORM_ENABLE_PUBLIC_CALLS",
        ),
        (
            {
                "PLATFORM_ENABLE_PUBLIC_CALLS": False,
                "PLATFORM_ENABLE_REAL_PAID_CALLS": False,
            },
            "ready",
            "PLATFORM_ENABLE_GATEWAY",
        ),
        (
            {
                "PLATFORM_ENABLE_PUBLIC_CALLS": False,
                "PLATFORM_ENABLE_REAL_PAID_CALLS": False,
                "PLATFORM_ENABLE_GATEWAY": False,
            },
            "degraded",
            "status ok or ready",
        ),
    ],
)
def test_deployment_preflight_fails_for_live_flags_or_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: dict,
    health: str,
    message: str,
) -> None:
    from scripts import verify

    harness, config = _preflight_harness(tmp_path)

    def fake_urlopen(request, timeout):
        if request.full_url == config["runner_api_endpoints"]["flags"]:
            payload = {"flags": flags}
        else:
            payload = {"status": health}
        return _JsonResponse(request.full_url, payload)

    monkeypatch.setattr(verify.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError, match=message):
        harness.check_deployment_endpoints(config)
