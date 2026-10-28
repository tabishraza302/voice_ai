"""Targeted tests for scripts/verify.py browser server lifecycle, log permissions, and post-exit redaction."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import pytest
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify import (
    LifecycleManager,
    VerificationHarness,
    _open_private_log,
    _redact_persisted_file,
    scrub_explicit_secrets,
)


def test_private_log_creation_and_redaction(tmp_path: Path) -> None:
    """Log files are created with restrictive 0o600 permissions and redacted after process output."""
    log_file = tmp_path / "test_server.stdout.log"
    f = _open_private_log(log_file)
    try:
        f.write("Starting server with postgresql+asyncpg://user:secretpass123@127.0.0.1:5432/db?token=tok_abc123\n")
        f.flush()
    finally:
        f.close()

    # Permissions must be restricted (0o600: read/write by owner only)
    mode = stat.S_IMODE(log_file.stat().st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    # Before redaction, secretpass123 is present
    raw_content = log_file.read_text(encoding="utf-8")
    assert "secretpass123" in raw_content

    # Run redaction
    _redact_persisted_file(log_file)

    # After redaction, secrets are sanitized and permissions remain restricted
    redacted_content = log_file.read_text(encoding="utf-8")
    assert "secretpass123" not in redacted_content
    assert "tok_abc123" not in redacted_content
    assert "user:***@" in redacted_content
    assert "token=***" in redacted_content

    mode_after = stat.S_IMODE(log_file.stat().st_mode)
    assert mode_after == 0o600, f"Expected 0o600, got {oct(mode_after)}"


def test_scrub_explicit_secrets_handles_raw_tokens_and_urls() -> None:
    """Explicit registered keys/passwords not caught by standard regex are scrubbed."""
    raw_key = "k_39fjA92kLx001_secret_value"
    text = f"Server initialized with raw secret: {raw_key} and connection http://admin:pw@localhost:5432/db"
    sanitized = scrub_explicit_secrets(text, explicit_secrets=[raw_key])
    assert raw_key not in sanitized
    assert "pw" not in sanitized
    assert "admin:***@" in sanitized


def test_redact_persisted_file_fails_closed_and_removes_unredacted_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If redaction encounters an unexpected error, it raises and unlinks the sensitive file."""
    log_file = tmp_path / "sensitive.log"
    log_file.write_text("raw secret data")

    def broken_redact(text: str, explicit_secrets: Any = None) -> str:
        raise OSError("disk failure during redaction")

    monkeypatch.setattr("scripts.verify.scrub_explicit_secrets", broken_redact)

    with pytest.raises(RuntimeError, match="failed to redact sensitive log file"):
        _redact_persisted_file(log_file)

    # File must be deleted to avoid leaving unredacted sensitive artifact on disk
    assert not log_file.exists()


def test_cleanup_ordering_terminates_children_before_callbacks() -> None:
    """LifecycleManager terminates child processes first, so no child can append after callback."""
    lifecycle = LifecycleManager()
    events: list[str] = []

    # Start a dummy subprocess
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        start_new_session=True,
    )
    lifecycle.register_process(proc, is_isolated_group=True)

    def on_cleanup() -> None:
        # Child must already be terminated when this callback executes
        status = proc.poll()
        events.append(f"callback_ran_proc_terminated_{status is not None}")

    lifecycle.register_cleanup_callback(on_cleanup)
    try:
        lifecycle.cleanup()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)

    assert not lifecycle.cleanup_failed
    assert events == ["callback_ran_proc_terminated_True"]

def test_run_dir_has_restrictive_permissions(tmp_path: Path) -> None:
    """Verifier run artifact directory has 0o700 permissions to protect evidence."""
    artifact_dir = tmp_path / "data" / "verification" / "custom_run"
    old_env = os.environ.get("VERIFY_ARTIFACT_DIR")
    os.environ["VERIFY_ARTIFACT_DIR"] = str(artifact_dir)
    try:
        harness = VerificationHarness(repo_root=tmp_path)
        mode = stat.S_IMODE(harness.run_dir.stat().st_mode)
        assert mode == 0o700, f"Expected 0o700, got {oct(mode)}"
    finally:
        if old_env is None:
            os.environ.pop("VERIFY_ARTIFACT_DIR", None)
        else:
            os.environ["VERIFY_ARTIFACT_DIR"] = old_env


def test_browser_server_lifecycle_safe_capture_and_cleanup(tmp_path: Path) -> None:
    """Simulates browser lane server logs: restrictive permissions, process exit, cleanup callback redaction."""
    harness = VerificationHarness(repo_root=tmp_path)
    api_out = harness.run_dir / "api_server.stdout.log"
    api_err = harness.run_dir / "api_server.stderr.log"

    out_f = _open_private_log(api_out)
    err_f = _open_private_log(api_err)

    explicit_token = "arbitrary_custom_secret_key_12345"
    harness.register_secret(explicit_token)

    harness.lifecycle.register_cleanup_callback(out_f.close)
    harness.lifecycle.register_cleanup_callback(err_f.close)
    harness.lifecycle.register_cleanup_callback(out_f.flush)
    harness.lifecycle.register_cleanup_callback(err_f.flush)
    harness.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(api_out, explicit_secrets=harness.explicit_secrets))
    harness.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(api_err, explicit_secrets=harness.explicit_secrets))
    ready_marker = tmp_path / "proc_ready.marker"

    # Spawn dummy process that logs secrets, flushes streams, writes IPC marker, then waits for termination
    child_code = (
        "import sys, time, pathlib\n"
        f"print('API token=leaked_api_token_456 custom={explicit_token}', flush=True)\n"
        "print('ERR password=secret_password_789', file=sys.stderr, flush=True)\n"
        f"pathlib.Path('{ready_marker}').write_text('ready')\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=out_f,
        stderr=err_f,
        start_new_session=True,
    )
    harness.lifecycle.register_process(proc, is_isolated_group=True)

    try:
        # Wait explicitly for child process to flush logs and signal readiness via IPC marker
        for _ in range(50):
            if ready_marker.is_file():
                break
            time.sleep(0.05)
        assert ready_marker.is_file(), "Child process failed to signal log emission readiness before cleanup"
    finally:
        # Execute cleanup (terminates proc, then closes handles and redacts persisted files)
        harness.lifecycle.cleanup()

    assert not harness.lifecycle.cleanup_failed
    out_text = api_out.read_text(encoding="utf-8")
    err_text = api_err.read_text(encoding="utf-8")

    assert "leaked_api_token_456" not in out_text
    assert explicit_token not in out_text
    assert "token=***" in out_text
    assert "secret_password_789" not in err_text
    assert "password=***" in err_text

    assert stat.S_IMODE(api_out.stat().st_mode) == 0o600
    assert stat.S_IMODE(api_err.stat().st_mode) == 0o600
