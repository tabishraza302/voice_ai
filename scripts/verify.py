#!/usr/bin/env python3
"""Voice AI Verification and Qualification Runner.

Implements the contract and execution lanes specified in verification-plan.md.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.request
import uuid
# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.support.qualification import (
    GateCounts,
    parse_bun_output,
    parse_junit_xml,
    parse_playwright_json,
    prerequisite_action,
    redact_secrets,
    require_gate,
)
from tests.support.tls import generate_ephemeral_loopback_cert


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port on the given host is currently listening or bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect((host, port))
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def allocate_loopback_port(
    preferred_port: int | None = None,
    refuse_existing: bool = True,
) -> int:
    """Allocate an available loopback port.

    If preferred_port is specified, verifies it is not listening.
    If already in use and refuse_existing is True, raises RuntimeError
    instead of attaching to a foreign server.
    """
    if preferred_port is not None:
        if is_port_in_use(preferred_port):
            if refuse_existing:
                raise RuntimeError(
                    f"busy port: port {preferred_port} is already in use by a foreign server"
                )
        return preferred_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_private_log(path: Path) -> Any:
    """Open a log file with restrictive (0o600) permissions."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return open(fd, "w", encoding="utf-8")


def scrub_explicit_secrets(text: str, explicit_secrets: list[str] | set[str] | None = None) -> str:
    """Redact standard regex patterns and any explicit sensitive strings from text."""
    if not text:
        return ""
    sanitized = redact_secrets(text)
    if explicit_secrets:
        for secret in explicit_secrets:
            if secret and len(secret) >= 4:
                sanitized = sanitized.replace(secret, "***")
    return sanitized


def _redact_persisted_file(path: Path, explicit_secrets: list[str] | set[str] | None = None) -> None:
    """Read persisted log file, redact secrets, and rewrite with restrictive permissions. Fail closed."""
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        redacted = scrub_explicit_secrets(raw, explicit_secrets=explicit_secrets)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        with open(fd, "w", encoding="utf-8") as f:
            f.write(redacted)
    except Exception as e:
        # Fail closed: never leave potentially unredacted file on disk
        try:
            if path.is_file():
                path.unlink()
        except Exception:
            pass
        raise RuntimeError(f"failed to redact sensitive log file {path}: {e}") from e

class LifecycleManager:
    """Tracks owned verification resources and ensures fail-closed cleanup."""

    def __init__(self) -> None:
        self.owned_processes: list[tuple[subprocess.Popen[Any], bool]] = []
        self.owned_compose_projects: list[tuple[str, Path]] = []
        self.owned_databases: list[tuple[str, str, str | None]] = []  # (admin_url, db_name, run_token)
        self.cleanup_callbacks: list[Callable[[], None]] = []
        self.cleanup_failed = False
        self._installed_signals = False

    def install_signal_handlers(self) -> None:
        if self._installed_signals:
            return

        def handle_signal(sig: int, frame: Any) -> None:
            sys.stderr.write(f"\nReceived signal {sig}; running owned resource cleanup...\n")
            try:
                self.cleanup()
            finally:
                sys.exit(128 + sig)

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        self._installed_signals = True

    def register_process(
        self,
        proc: subprocess.Popen[Any],
        is_isolated_group: bool | None = None,
    ) -> None:
        """Register an owned subprocess with explicit group isolation tracking."""
        if is_isolated_group is None:
            try:
                pgid = os.getpgid(proc.pid)
                isolated = (pgid == proc.pid and pgid != os.getpgrp())
            except (ProcessLookupError, OSError):
                isolated = False
        else:
            isolated = is_isolated_group
        self.owned_processes.append((proc, isolated))

    def register_compose_project(self, project_name: str, compose_file: Path) -> None:
        self.owned_compose_projects.append((project_name, compose_file))

    def register_database(self, admin_url: str, db_name: str, run_token: str | None = None) -> None:
        self.owned_databases.append((admin_url, db_name, run_token))

    def register_cleanup_callback(self, cb: Callable[[], None]) -> None:
        self.cleanup_callbacks.append(cb)

    def cleanup(self) -> None:
        errors: list[Exception] = []

        # 1. Terminate and reap owned child processes first so they cannot append output
        current_pgid = os.getpgrp()
        processes = list(self.owned_processes)
        self.owned_processes.clear()
        for proc, isolated in processes:
            try:
                if proc.poll() is None:
                    if isolated:
                        try:
                            pgid = os.getpgid(proc.pid)
                            if pgid != current_pgid and pgid == proc.pid:
                                os.killpg(pgid, signal.SIGTERM)
                            else:
                                proc.terminate()
                        except (ProcessLookupError, OSError):
                            proc.terminate()
                    else:
                        proc.terminate()
            except Exception as e:
                errors.append(e)

        time.sleep(0.5)
        for proc, isolated in processes:
            try:
                if proc.poll() is None:
                    if isolated:
                        try:
                            pgid = os.getpgid(proc.pid)
                            if pgid != current_pgid and pgid == proc.pid:
                                os.killpg(pgid, signal.SIGKILL)
                            else:
                                proc.kill()
                        except (ProcessLookupError, OSError):
                            proc.kill()
                    else:
                        proc.kill()
                    proc.wait(timeout=2)
            except Exception as e:
                errors.append(e)

        # 2. Run custom callbacks in reverse order (flushing, closing handles, post-exit redaction)
        callbacks = list(reversed(self.cleanup_callbacks))
        self.cleanup_callbacks.clear()
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                errors.append(e)
        # 3. Drop owned databases
        databases = list(self.owned_databases)
        self.owned_databases.clear()
        for admin_url, db_name, run_token in databases:
            try:
                _drop_database(admin_url, db_name, run_token=run_token)
            except Exception as e:
                errors.append(e)

        # 4. Tear down owned docker compose projects
        compose_projects = list(self.owned_compose_projects)
        self.owned_compose_projects.clear()
        for project_name, compose_file in compose_projects:
            try:
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-p",
                        project_name,
                        "-f",
                        str(compose_file),
                        "down",
                        "--volumes",
                        "--remove-orphans",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except Exception as e:
                errors.append(e)
        if errors:
            self.cleanup_failed = True
            msg = "; ".join(str(e) for e in errors)
            raise RuntimeError(f"verifier cleanup failed: {msg}")


def _drop_database(admin_url: str, db_name: str, run_token: str | None = None) -> None:
    """Drop test database using asyncpg with credentials passed via stdin and verified token."""
    clean_url = re.sub(r"\+asyncpg", "", admin_url)
    if not re.match(r"^(platform|legacy)_verify_[a-f0-9]{8,16}_[a-f0-9]{8,16}$", db_name) or len(db_name) > 63:
        raise ValueError(f"Refusing to drop unowned or invalid database: {db_name}")
    input_payload = json.dumps({"url": clean_url, "db": db_name, "token": run_token})
    code = (
        "import asyncio, sys, json, asyncpg\n"
        "async def drop():\n"
        "    data = json.loads(sys.stdin.read())\n"
        "    conn = await asyncpg.connect(data['url'])\n"
        "    token = data.get('token')\n"
        "    if token:\n"
        "        has_marker = await conn.fetchval(\n"
        "            'SELECT EXISTS (SELECT 1 FROM _verification_cluster_ownership WHERE token = $1);',\n"
        "            token,\n"
        "        )\n"
        "        if not has_marker:\n"
        "            await conn.close()\n"
        "            sys.exit(3)\n"
        "    await conn.execute(\n"
        "        'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '\n"
        "        'WHERE datname = $1 AND pid <> pg_backend_pid();',\n"
        "        data['db'],\n"
        "    )\n"
        "    await conn.execute(f'DROP DATABASE IF EXISTS \"{data[\"db\"]}\";')\n"
        "    await conn.close()\n"
        "asyncio.run(drop())\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        input=input_payload,
        capture_output=True,
        text=True,
    )
    if res.returncode == 3:
        raise PermissionError(
            f"Cluster failed ownership verification on teardown: missing valid run ownership token marker; refusing to drop {db_name}"
        )
    if res.returncode != 0:
        raise RuntimeError(f"failed to drop database {db_name}: {redact_secrets(res.stderr)}")
def build_gate_command(gate: str, repo_root: Path) -> list[str]:
    """Construct command tokens for each verification lane using frozen local tools."""
    if gate == "unit":
        return [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "tests",
            "-m",
            "not postgres and not browser and not live_voice and not qualification",
            "--strict-markers",
        ]
    elif gate == "postgres":
        targets = ["tests/test_agent_postgres_integration.py"]
        if (repo_root / "tests/platform_api").exists():
            targets.append("tests/platform_api")
        return [
            "uv",
            "run",
            "--frozen",
            "pytest",
            *targets,
            "-m",
            "postgres",
            "--strict-markers",
        ]
    elif gate == "voice_local_offline":
        return [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "tests/qualification/test_voice_audio_input.py",
            "tests/qualification/test_flow_adapter.py",
            "--strict-markers",
        ]
    elif gate == "voice_deployment":
        return [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "tests/qualification/test_voice_audio_input.py",
            "tests/qualification/test_flow_adapter.py",
            "tests/qualification/test_public_transport.py",
            "--strict-markers",
        ]
    elif gate == "release":
        return [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "tests/qualification/test_backup_restore.py",
            "tests/qualification/test_release_evidence.py",
            "--strict-markers",
        ]
    raise ValueError(f"unknown gate: {gate}")


class VerificationHarness:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.lifecycle = LifecycleManager()
        self.lifecycle.install_signal_handlers()

        # Initialize run directory strictly under data/verification
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        custom_artifact_dir = os.environ.get("VERIFY_ARTIFACT_DIR")
        if custom_artifact_dir:
            candidate = Path(custom_artifact_dir).resolve()
            allowed_base = (self.repo_root / "data" / "verification").resolve()
            try:
                candidate.relative_to(allowed_base)
            except ValueError:
                raise ValueError(
                    f"VERIFY_ARTIFACT_DIR must reside under {allowed_base}, got: {candidate}"
                )
            if candidate.exists() and any(candidate.iterdir()):
                raise ValueError(
                    f"VERIFY_ARTIFACT_DIR must be a new, empty directory: {candidate} already exists and is non-empty"
                )
            self.run_dir = candidate
        else:
            self.run_dir = self.repo_root / "data" / "verification" / run_id

        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.run_dir.chmod(0o700)
        except OSError:
            pass
        self.commands_log = self.run_dir / "commands.json"
        self.manifest_file = self.run_dir / "manifest.json"
        self.result_file = self.run_dir / "result.json"

        self.commands_recorded: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}
        self.explicit_secrets: set[str] = set()

    def register_secret(self, secret: str | None) -> None:
        """Register an explicit sensitive value to be scrubbed from all retained evidence."""
        if secret and len(secret) >= 4:
            self.explicit_secrets.add(secret)
    def record_command(
        self,
        name: str,
        cmd: list[str],
        cwd: Path,
        returncode: int,
        duration_s: float,
        stdout_text: str,
        stderr_text: str,
    ) -> None:
        redacted_out = scrub_explicit_secrets(stdout_text, explicit_secrets=self.explicit_secrets)
        redacted_err = scrub_explicit_secrets(stderr_text, explicit_secrets=self.explicit_secrets)
        stdout_file = self.run_dir / f"{name}.stdout.log"
        stderr_file = self.run_dir / f"{name}.stderr.log"
        stdout_file.write_text(redacted_out)
        stderr_file.write_text(redacted_err)
        try:
            stdout_file.chmod(0o600)
            stderr_file.chmod(0o600)
        except OSError:
            pass

        entry = {
            "name": name,
            "command": cmd,
            "cwd": str(cwd),
            "returncode": returncode,
            "duration_seconds": duration_s,
            "stdout_log": str(stdout_file),
            "stderr_log": str(stderr_file),
            "stdout": redacted_out,
            "stderr": redacted_err,
            "stdout_snippet": redacted_out[:1000],
            "stderr_snippet": redacted_err[:1000],
        }
        self.commands_recorded.append(entry)
        self.commands_log.write_text(json.dumps(self.commands_recorded, indent=2))

    def write_manifest(self) -> None:
        # Collect lockfile hashes
        uv_lock = self.repo_root / "uv.lock"
        uv_hash = (
            hashlib.sha256(uv_lock.read_bytes()).hexdigest()
            if uv_lock.is_file()
            else None
        )
        bun_lock = self.repo_root / "web" / "bun.lock"
        bun_hash = (
            hashlib.sha256(bun_lock.read_bytes()).hexdigest()
            if bun_lock.is_file()
            else None
        )
        pyproject = self.repo_root / "pyproject.toml"
        pyproject_hash = (
            hashlib.sha256(pyproject.read_bytes()).hexdigest()
            if pyproject.is_file()
            else None
        )

        commit = None
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                commit = res.stdout.strip()
        except Exception:
            pass

        manifest = {
            "run_dir": str(self.run_dir),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "commit": commit,
            "python_version": sys.version,
            "uv_lock_sha256": uv_hash,
            "bun_lock_sha256": bun_hash,
            "pyproject_toml_sha256": pyproject_hash,
        }
        self.manifest_file.write_text(json.dumps(manifest, indent=2))

    def write_result(self, target_gates: list[str] | None = None) -> int:
        if target_gates is not None:
            overall_passed = bool(target_gates) and all(
                self.results.get(g, {}).get("status") == "passed" for g in target_gates
            )
        else:
            overall_passed = bool(self.results) and all(
                data.get("status") == "passed" for data in self.results.values()
            )

        for gate_name, data in self.results.items():
            if data.get("status") != "passed":
                if not data.get("reason"):
                    data["reason"] = f"gate {gate_name} failed without explicit reason"

        if self.lifecycle.cleanup_failed:
            overall_passed = False
            self.results["cleanup"] = {
                "status": "failed",
                "reason": "verifier cleanup encountered errors",
            }

        payload = {
            "overall_success": overall_passed,
            "gates": self.results,
        }
        self.result_file.write_text(json.dumps(payload, indent=2))
        return 0 if overall_passed else 1

    def _build_sanitized_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        allowed_sys_keys = {
            "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TEMP", "TMP",
            "SHELL", "LANG", "LC_ALL", "TERM", "SYSTEMROOT", "COMSPEC",
            "VIRTUAL_ENV", "CI", "GITHUB_ACTIONS", "GITHUB_WORKSPACE",
        }
        sanitized_env = {
            k: v for k, v in os.environ.items()
            if k in allowed_sys_keys or k.startswith(("UV_", "BUN_", "NODE_", "PLAYWRIGHT_"))
        }
        for k in list(sanitized_env.keys()):
            if any(p in k for p in ["DATABASE", "POSTGRES", "SECRET", "API_KEY", "TOKEN", "JWT", "TOTP"]):
                del sanitized_env[k]

        sanitized_env["VERIFY_REQUIRED"] = "1"
        if env:
            sanitized_env.update(env)
        return sanitized_env

    def run_cmd(
        self,
        name: str,
        cmd: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        working_dir = cwd or self.repo_root
        sanitized_env = self._build_sanitized_env(env)

        start = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            env=sanitized_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.lifecycle.register_process(proc, is_isolated_group=True)
        stdout, stderr = proc.communicate()
        duration = time.monotonic() - start

        self.record_command(
            name=name,
            cmd=cmd,
            cwd=working_dir,
            returncode=proc.returncode,
            duration_s=duration,
            stdout_text=stdout,
            stderr_text=stderr,
        )
        return proc.returncode, stdout, stderr
    def prepare(self) -> int:
        print("==> Running verify prepare...")
        # 1. uv sync
        rc, out, err = self.run_cmd("uv_sync", ["uv", "sync", "--frozen", "--extra", "dev"])
        if rc != 0:
            print(f"Error during uv sync: {err}", file=sys.stderr)
            return rc

        # 2. bun install
        web_dir = self.repo_root / "web"
        rc, out, err = self.run_cmd(
            "bun_install",
            ["bun", "install", "--frozen-lockfile"],
            cwd=web_dir,
        )
        if rc != 0:
            print(f"Error during bun install: {err}", file=sys.stderr)
            return rc

        # 3. playwright install chromium
        rc, out, err = self.run_cmd(
            "playwright_install",
            ["bun", "./node_modules/@playwright/test/cli.js", "install", "chromium"],
            cwd=web_dir,
        )
        if rc != 0:
            print(f"Error during playwright install: {err}", file=sys.stderr)
            return rc

        # 4. docker pull postgres:16-alpine (must fail preparation if pull fails)
        rc, out, err = self.run_cmd(
            "docker_pull_postgres",
            ["docker", "pull", "postgres:16-alpine"],
        )
        if rc != 0:
            print(f"Error pulling docker image postgres:16-alpine: {err}", file=sys.stderr)
            return rc

        self.write_manifest()
        print("==> verify prepare completed successfully.")
        return 0

    def unit(self, standalone: bool = True) -> int:
        print("==> Running verify unit lane...")
        self.write_manifest()
        junit_xml = self.run_dir / "python-unit.xml"
        cmd = build_gate_command("unit", self.repo_root) + [f"--junitxml={junit_xml}"]

        py_rc, py_out, py_err = self.run_cmd("python_unit", cmd)

        # Parse python junit xml
        try:
            py_counts = parse_junit_xml(junit_xml)
            require_gate(py_counts)
            if py_counts.collected == 0 or py_counts.passed == 0:
                raise ValueError("zero python unit tests ran")
            if py_rc != 0:
                raise ValueError(f"pytest exited with code {py_rc}")
        except Exception as e:
            self.results["python_unit"] = {"status": "failed", "reason": str(e)}
        else:
            self.results["python_unit"] = {
                "status": "passed",
                "counts": py_counts.__dict__,
            }

        # Bun tests (using combined stdout and stderr as Bun writes reports to stderr)
        web_dir = self.repo_root / "web"
        bun_rc, bun_out, bun_err = self.run_cmd("bun_test", ["bun", "test"], cwd=web_dir)
        bun_combined = f"{bun_out}\n{bun_err}"
        try:
            bun_counts = parse_bun_output(bun_combined)
            require_gate(bun_counts)
            if bun_counts.collected == 0 or bun_counts.passed == 0:
                raise ValueError("zero bun tests ran")
            if bun_rc != 0:
                raise ValueError(f"bun test exited with code {bun_rc}")
        except Exception as e:
            self.results["bun_test"] = {"status": "failed", "reason": str(e)}
        else:
            self.results["bun_test"] = {
                "status": "passed",
                "counts": bun_counts.__dict__,
            }

        # Bun typecheck
        tc_rc, tc_out, tc_err = self.run_cmd(
            "bun_typecheck",
            ["bun", "run", "typecheck"],
            cwd=web_dir,
        )
        if tc_rc == 0:
            self.results["bun_typecheck"] = {"status": "passed"}
        else:
            self.results["bun_typecheck"] = {
                "status": "failed",
                "reason": f"typecheck failed: {tc_err or tc_out}",
            }

        # Bun build
        bld_rc, bld_out, bld_err = self.run_cmd(
            "bun_build",
            ["bun", "run", "build"],
            cwd=web_dir,
        )
        if bld_rc == 0:
            self.results["bun_build"] = {"status": "passed"}
        else:
            self.results["bun_build"] = {
                "status": "failed",
                "reason": f"build failed: {bld_err or bld_out}",
            }

        try:
            self.lifecycle.cleanup()
        except Exception as e:
            self.results["cleanup"] = {"status": "failed", "reason": str(e)}

        unit_gates = ["python_unit", "bun_test", "bun_typecheck", "bun_build"]
        unit_passed = all(
            self.results.get(g, {}).get("status") == "passed" for g in unit_gates
        ) and not self.lifecycle.cleanup_failed

        if standalone:
            return self.write_result(target_gates=unit_gates)
        return 0 if unit_passed else 1

    def provision_disposable_postgres(self) -> tuple[str, str, str]:
        """Provisions disposable PostgreSQL container with dynamic port and returns URLs.

        Returns (admin_url, platform_db_url, legacy_db_url).
        """
        admin_url_env = os.environ.get("PLATFORM_TEST_DATABASE_ADMIN_URL")
        if admin_url_env:
            if "sqlite" in admin_url_env.lower():
                raise ValueError("SQLite fallback is forbidden for PostgreSQL admin URL")
            pre_established_token = (
                os.environ.get("PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN")
                or os.environ.get("_VERIFY_RUN_OWNERSHIP_TOKEN")
            )
            if not pre_established_token:
                raise ValueError(
                    "PLATFORM_TEST_DATABASE_ADMIN_URL requires a pre-established ownership token in "
                    "PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN to verify run ownership"
                )
            run_token = pre_established_token[:12]
            self.run_token = run_token
            os.environ["_VERIFY_RUN_OWNERSHIP_TOKEN"] = run_token
            os.environ["PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN"] = run_token
            base_admin_url = admin_url_env
            allocated_port = 5432
            port_match = re.search(r":(\d+)/", admin_url_env)
            if port_match:
                allocated_port = int(port_match.group(1))
        else:
            run_token = uuid.uuid4().hex[:12]
            self.run_token = run_token
            os.environ["_VERIFY_RUN_OWNERSHIP_TOKEN"] = run_token
            os.environ["PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN"] = run_token
            project_id = f"voice-ai-verify-{run_token}"
            compose_file = self.repo_root / "tests" / "compose.postgres.yml"
            self.lifecycle.register_compose_project(project_id, compose_file)

            # Use --pull never during test execution to ensure local cached image is used
            rc, out, err = self.run_cmd(
                "docker_compose_up",
                ["docker", "compose", "-p", project_id, "-f", str(compose_file), "up", "-d", "--pull", "never"],
            )
            if rc != 0:
                raise RuntimeError(f"failed to start docker compose postgres: {err}")

            # Inspect allocated port
            rc, out, err = self.run_cmd(
                "docker_compose_port",
                ["docker", "compose", "-p", project_id, "-f", str(compose_file), "port", "postgres", "5432"],
            )
            if rc != 0 or not out.strip():
                raise RuntimeError(f"failed to obtain dynamic postgres port: {err}")

            match = re.search(r":(\d+)$", out.strip())
            if not match:
                raise RuntimeError(f"unparseable port output from docker compose: {out}")
            allocated_port = int(match.group(1))
            base_admin_url = f"postgresql+asyncpg://postgres:postgres_password@127.0.0.1:{allocated_port}/postgres"

        raw_admin_url = re.sub(r"\+asyncpg", "", base_admin_url)

        # Health check actual PostgreSQL database connection (not just port open)
        health_check_code = (
            "import asyncio, sys, json, asyncpg\n"
            "async def check():\n"
            "    data = json.loads(sys.stdin.read())\n"
            "    conn = await asyncpg.connect(data['url'])\n"
            "    val = await conn.fetchval('SELECT 1;')\n"
            "    await conn.close()\n"
            "    assert val == 1\n"
            "asyncio.run(check())\n"
        )
        ready = False
        for _ in range(30):
            res = subprocess.run(
                [sys.executable, "-c", health_check_code],
                input=json.dumps({"url": raw_admin_url}),
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                ready = True
                break
            time.sleep(1)

        if not ready:
            raise RuntimeError(f"PostgreSQL container port {allocated_port} database failed health check")

        # Set up ownership marker on the cluster if provisioned by compose, or verify existing marker
        if not admin_url_env:
            setup_marker_code = (
                "import asyncio, sys, json, asyncpg\n"
                "async def setup():\n"
                "    data = json.loads(sys.stdin.read())\n"
                "    conn = await asyncpg.connect(data['url'])\n"
                "    await conn.execute('''\n"
                "        CREATE TABLE IF NOT EXISTS _verification_cluster_ownership (\n"
                "            token TEXT PRIMARY KEY,\n"
                "            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
                "        );\n"
                "    ''')\n"
                "    await conn.execute(\n"
                "        'INSERT INTO _verification_cluster_ownership (token) VALUES ($1) ON CONFLICT (token) DO NOTHING;',\n"
                "        data['token']\n"
                "    )\n"
                "    await conn.close()\n"
                "asyncio.run(setup())\n"
            )
            res = subprocess.run(
                [sys.executable, "-c", setup_marker_code],
                input=json.dumps({"url": raw_admin_url, "token": run_token}),
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"failed to set up cluster ownership marker: {redact_secrets(res.stderr)}")
        else:
            # External admin URL: must verify that it already contains the unpredictable ownership token marker
            verify_marker_code = (
                "import asyncio, sys, json, asyncpg\n"
                "async def verify():\n"
                "    data = json.loads(sys.stdin.read())\n"
                "    conn = await asyncpg.connect(data['url'])\n"
                "    table_exists = await conn.fetchval('''\n"
                "        SELECT EXISTS (\n"
                "            SELECT 1 FROM information_schema.tables\n"
                "            WHERE table_schema = 'public' AND table_name = '_verification_cluster_ownership'\n"
                "        );\n"
                "    ''')\n"
                "    if not table_exists:\n"
                "        await conn.close()\n"
                "        sys.exit(2)\n"
                "    has_token = await conn.fetchval(\n"
                "        'SELECT EXISTS (SELECT 1 FROM _verification_cluster_ownership WHERE token = $1);',\n"
                "        data['token']\n"
                "    )\n"
                "    await conn.close()\n"
                "    sys.exit(0 if has_token else 3)\n"
                "asyncio.run(verify())\n"
            )
            res = subprocess.run(
                [sys.executable, "-c", verify_marker_code],
                input=json.dumps({"url": raw_admin_url, "token": run_token}),
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise PermissionError(
                    "Cluster failed ownership verification: missing valid run ownership token marker; "
                    "refusing database operations on unowned cluster"
                )

        # Create two distinct disposable databases with bounded identifiers (<=63 bytes)
        uid1 = uuid.uuid4().hex[:12]
        uid2 = uuid.uuid4().hex[:12]
        platform_db_name = f"platform_verify_{run_token}_{uid1}"
        legacy_db_name = f"legacy_verify_{run_token}_{uid2}"
        assert len(platform_db_name) <= 63, f"database name {platform_db_name} exceeds PostgreSQL 63-byte limit"
        assert len(legacy_db_name) <= 63, f"database name {legacy_db_name} exceeds PostgreSQL 63-byte limit"
        # Register for automatic cleanup
        self.lifecycle.register_database(raw_admin_url, platform_db_name, run_token=run_token)
        self.lifecycle.register_database(raw_admin_url, legacy_db_name, run_token=run_token)

        # Create databases with credentials passed via stdin (never in argv)
        create_code = (
            "import asyncio, sys, json, asyncpg\n"
            "async def mk():\n"
            "    data = json.loads(sys.stdin.read())\n"
            "    conn = await asyncpg.connect(data['url'])\n"
            "    ver = await conn.fetchval('SELECT version();')\n"
            "    print(ver)\n"
            "    await conn.execute(f'CREATE DATABASE \"{data[\"platform_db\"]}\";')\n"
            "    await conn.execute(f'CREATE DATABASE \"{data[\"legacy_db\"]}\";')\n"
            "    await conn.close()\n"
            "asyncio.run(mk())\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", create_code],
            input=json.dumps({
                "url": raw_admin_url,
                "platform_db": platform_db_name,
                "legacy_db": legacy_db_name,
            }),
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(f"failed to create test databases: {redact_secrets(res.stderr)}")
        pg_version = res.stdout.strip()
        if self.manifest_file.is_file():
            manifest_data = json.loads(self.manifest_file.read_text())
            manifest_data["postgres"] = {
                "server_version": pg_version,
                "host_port": allocated_port,
                "image": "postgres:16-alpine",
            }
            self.manifest_file.write_text(json.dumps(manifest_data, indent=2))
        platform_url = re.sub(r"/postgres(\?.*)?$", f"/{platform_db_name}\\1", base_admin_url)
        legacy_url = re.sub(r"/postgres(\?.*)?$", f"/{legacy_db_name}\\1", base_admin_url)

        return base_admin_url, platform_url, legacy_url

    def postgres(self, standalone: bool = True) -> int:
        print("==> Running verify postgres lane...")
        self.write_manifest()
        try:
            try:
                admin_url, platform_url, legacy_url = self.provision_disposable_postgres()
            except Exception as e:
                self.results["postgres_gate"] = {"status": "failed", "reason": redact_secrets(str(e))}
                if standalone:
                    return self.write_result(target_gates=["postgres_gate"])
                return 1

            env = {
                "PLATFORM_TEST_DATABASE_ADMIN_URL": admin_url,
                "PLATFORM_DATABASE_URL": platform_url,
                "TEST_DATABASE_URL": legacy_url,
                "PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN": self.run_token,
                "_VERIFY_RUN_OWNERSHIP_TOKEN": self.run_token,
            }
            junit_xml = self.run_dir / "postgres.xml"
            cmd = build_gate_command("postgres", self.repo_root) + [f"--junitxml={junit_xml}"]

            rc, out, err = self.run_cmd("postgres_tests", cmd, env=env)

            try:
                pg_counts = parse_junit_xml(junit_xml)
                require_gate(pg_counts)
                if pg_counts.collected == 0 or pg_counts.passed == 0:
                    raise ValueError("zero postgres tests executed")
                if rc != 0:
                    raise ValueError(f"pytest exited with code {rc}")
            except Exception as e:
                self.results["postgres_gate"] = {"status": "failed", "reason": redact_secrets(str(e))}
            else:
                self.results["postgres_gate"] = {
                    "status": "passed",
                    "counts": pg_counts.__dict__,
                }
        finally:
            try:
                self.lifecycle.cleanup()
            except Exception as e:
                self.results["cleanup"] = {"status": "failed", "reason": redact_secrets(str(e))}

        postgres_passed = (
            self.results.get("postgres_gate", {}).get("status") == "passed"
            and not self.lifecycle.cleanup_failed
        )
        if standalone:
            return self.write_result(target_gates=["postgres_gate"])
        return 0 if postgres_passed else 1

    def _run_browser_dev(self) -> GateCounts:
        admin_url, platform_url, legacy_url = self.provision_disposable_postgres()
        api_port = allocate_loopback_port(preferred_port=None)
        web_port = allocate_loopback_port(preferred_port=None)

        csrf_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        rate_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        mfa_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        self.register_secret(csrf_key)
        self.register_secret(rate_key)
        self.register_secret(mfa_key)
        self.register_secret(admin_url)
        self.register_secret(platform_url)
        self.register_secret(legacy_url)
        self.register_secret(self.run_token)

        mail_dir = self.run_dir / "auth_mail_dev"
        mail_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        def _cleanup_mail_dir() -> None:
            if mail_dir.exists():
                import shutil
                try:
                    shutil.rmtree(mail_dir)
                except Exception as e:
                    raise RuntimeError(f"failed to remove ephemeral mail directory {mail_dir}: {e}") from e

        self.lifecycle.register_cleanup_callback(_cleanup_mail_dir)

        schema_init_cmd = [
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "platform_api.cli",
            "init-db",
        ]
        schema_init_env = {
            "PLATFORM_DATABASE_URL": platform_url,
            "PLATFORM_TEST_DATABASE_ADMIN_URL": admin_url,
            "PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN": self.run_token,
            "_VERIFY_RUN_OWNERSHIP_TOKEN": self.run_token,
            "PLATFORM_AUTH_CSRF_KEY": csrf_key,
            "PLATFORM_AUTH_RATE_KEY": rate_key,
            "PLATFORM_AUTH_MFA_KEY": mfa_key,
            "PLATFORM_AUTH_MAIL_DIR": str(mail_dir),
            "PLATFORM_AUTH_MAIL_MODE": "file",
        }
        init_rc, init_out, init_err = self.run_cmd("browser_schema_init_dev", schema_init_cmd, env=schema_init_env)
        if init_rc != 0:
            raise RuntimeError(f"failed to initialize platform schema before dev browser tests: {redact_secrets(init_err or init_out)}")

        api_cmd = [
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "uvicorn",
            "platform_api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
        ]
        api_env = {
            "PLATFORM_DATABASE_URL": platform_url,
            "PLATFORM_TEST_DATABASE_ADMIN_URL": admin_url,
            "PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN": self.run_token,
            "_VERIFY_RUN_OWNERSHIP_TOKEN": self.run_token,
            "PLATFORM_HOST": "127.0.0.1",
            "PLATFORM_PORT": str(api_port),
            "PLATFORM_WEB_ORIGIN": f"http://127.0.0.1:{web_port}",
            "PLATFORM_ALLOWED_HOSTS": '["127.0.0.1", "localhost"]',
            "PLATFORM_AUTH_CSRF_KEY": csrf_key,
            "PLATFORM_AUTH_RATE_KEY": rate_key,
            "PLATFORM_AUTH_MFA_KEY": mfa_key,
            "PLATFORM_AUTH_MAIL_DIR": str(mail_dir),
            "PLATFORM_AUTH_MAIL_MODE": "file",
            "PLATFORM_ENV": "development",
            "PLATFORM_COOKIE_SECURE": "false",
            "PLATFORM_SESSION_COOKIE_NAME": "platform_dev_session",
        }

        api_stdout_path = self.run_dir / "api_server_dev.stdout.log"
        api_stderr_path = self.run_dir / "api_server_dev.stderr.log"
        api_log_out = _open_private_log(api_stdout_path)
        api_log_err = _open_private_log(api_stderr_path)
        self.lifecycle.register_cleanup_callback(api_log_out.close)
        self.lifecycle.register_cleanup_callback(api_log_err.close)
        self.lifecycle.register_cleanup_callback(api_log_out.flush)
        self.lifecycle.register_cleanup_callback(api_log_err.flush)
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(api_stdout_path, explicit_secrets=self.explicit_secrets))
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(api_stderr_path, explicit_secrets=self.explicit_secrets))

        api_proc = subprocess.Popen(
            api_cmd,
            cwd=self.repo_root,
            env=self._build_sanitized_env(api_env),
            stdout=api_log_out,
            stderr=api_log_err,
            start_new_session=True,
        )
        self.lifecycle.register_process(api_proc, is_isolated_group=True)

        api_ready = False
        for _ in range(30):
            if api_proc.poll() is not None:
                raise RuntimeError(f"dev control-plane API exited early with code {api_proc.returncode}")
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{api_port}/health/live")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        api_ready = True
                        break
            except Exception:
                pass
            time.sleep(0.5)

        if not api_ready:
            raise RuntimeError(f"dev control-plane API server failed to respond on http://127.0.0.1:{api_port}/health/live within 15s")

        web_dir = self.repo_root / "web"
        next_bin = web_dir / "node_modules" / "next" / "dist" / "bin" / "next"
        if not next_bin.is_file():
            raise RuntimeError(f"next binary missing at {next_bin}")

        web_bld_cmd = ["bun", "run", "build"]
        web_bld_env = {
            "NEXT_PUBLIC_APP_MODE": "saas",
            "NEXT_PUBLIC_PLATFORM_API_URL": f"http://127.0.0.1:{api_port}/api/v1",
            "NODE_ENV": "production",
        }
        bld_rc, bld_out, bld_err = self.run_cmd("browser_web_build_dev", web_bld_cmd, cwd=web_dir, env=web_bld_env)
        if bld_rc != 0:
            raise RuntimeError(f"failed to build web studio for dev browser gate: {redact_secrets(bld_err or bld_out)}")

        if not (web_dir / ".next").is_dir():
            raise RuntimeError(
                "Built production web studio missing at web/.next; verify browser requires a built production server, refusing next dev fallback"
            )

        web_cmd = ["node", str(next_bin), "start", "-H", "127.0.0.1", "-p", str(web_port)]
        web_env = {
            "PORT": str(web_port),
            "HOST": "127.0.0.1",
            "NODE_ENV": "production",
            "NEXT_PUBLIC_APP_MODE": "saas",
            "NEXT_PUBLIC_PLATFORM_API_URL": f"http://127.0.0.1:{api_port}/api/v1",
        }
        web_stdout_path = self.run_dir / "web_server_dev.stdout.log"
        web_stderr_path = self.run_dir / "web_server_dev.stderr.log"
        web_log_out = _open_private_log(web_stdout_path)
        web_log_err = _open_private_log(web_stderr_path)
        self.lifecycle.register_cleanup_callback(web_log_out.close)
        self.lifecycle.register_cleanup_callback(web_log_err.close)
        self.lifecycle.register_cleanup_callback(web_log_out.flush)
        self.lifecycle.register_cleanup_callback(web_log_err.flush)
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(web_stdout_path, explicit_secrets=self.explicit_secrets))
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(web_stderr_path, explicit_secrets=self.explicit_secrets))

        web_proc = subprocess.Popen(
            web_cmd,
            cwd=web_dir,
            env=self._build_sanitized_env(web_env),
            stdout=web_log_out,
            stderr=web_log_err,
            start_new_session=True,
        )
        self.lifecycle.register_process(web_proc, is_isolated_group=True)

        web_ready = False
        for _ in range(40):
            if web_proc.poll() is not None:
                raise RuntimeError(f"web studio server exited early with code {web_proc.returncode}")
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{web_port}/")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status in (200, 301, 302, 307, 308):
                        web_ready = True
                        break
            except Exception:
                pass
            time.sleep(0.5)

        if not web_ready:
            raise RuntimeError(f"web studio server failed to respond on http://127.0.0.1:{web_port}/ within 20s")

        playwright_json = self.run_dir / "playwright_dev.json"
        cmd = [
            "bun",
            "run",
            "test:e2e",
            "e2e/auth-journeys.spec.ts",
            "--project=chromium",
            "--reporter=json",
        ]
        env = {
            "PLATFORM_DATABASE_URL": platform_url,
            "PLATFORM_TEST_DATABASE_ADMIN_URL": admin_url,
            "PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN": self.run_token,
            "_VERIFY_RUN_OWNERSHIP_TOKEN": self.run_token,
            "PLATFORM_AUTH_CSRF_KEY": csrf_key,
            "PLATFORM_AUTH_RATE_KEY": rate_key,
            "PLATFORM_AUTH_MFA_KEY": mfa_key,
            "PLATFORM_AUTH_MAIL_DIR": str(mail_dir),
            "PLATFORM_AUTH_MAIL_MODE": "file",
            "NEXT_PUBLIC_APP_MODE": "saas",
            "NEXT_PUBLIC_PLATFORM_API_URL": f"http://127.0.0.1:{api_port}/api/v1",
            "PLAYWRIGHT_BASE_URL": f"http://127.0.0.1:{web_port}",
            "PLAYWRIGHT_JSON_OUTPUT_NAME": str(playwright_json),
        }
        rc, out, err = self.run_cmd("browser_dev_tests", cmd, cwd=web_dir, env=env)
        self.lifecycle.register_cleanup_callback(
            lambda: _redact_persisted_file(playwright_json, explicit_secrets=self.explicit_secrets)
            if playwright_json.is_file()
            else None
        )

        try:
            if rc != 0:
                raise ValueError(f"dev playwright exited with code {rc}: {redact_secrets(err or out)}")
            counts = parse_playwright_json(playwright_json)
            return counts
        finally:
            self.lifecycle.cleanup()

    def _run_browser_tls_ingress(self) -> GateCounts:
        admin_url, platform_url, legacy_url = self.provision_disposable_postgres()
        api_port = allocate_loopback_port(preferred_port=None)
        web_port = allocate_loopback_port(preferred_port=None)
        ingress_port = allocate_loopback_port(preferred_port=None)

        csrf_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        rate_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        mfa_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        self.register_secret(csrf_key)
        self.register_secret(rate_key)
        self.register_secret(mfa_key)
        self.register_secret(admin_url)
        self.register_secret(platform_url)
        self.register_secret(legacy_url)
        self.register_secret(self.run_token)

        mail_dir = self.run_dir / "auth_mail_tls"
        mail_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        def _cleanup_mail_dir() -> None:
            if mail_dir.exists():
                import shutil
                try:
                    shutil.rmtree(mail_dir)
                except Exception as e:
                    raise RuntimeError(f"failed to remove ephemeral mail directory {mail_dir}: {e}") from e

        self.lifecycle.register_cleanup_callback(_cleanup_mail_dir)

        # Generate ephemeral loopback TLS certificate without modifying system trust
        cert_path = self.run_dir / "tls_cert.pem"
        key_path = self.run_dir / "tls_key.pem"
        generate_ephemeral_loopback_cert(cert_path, key_path)
        try:
            key_text = key_path.read_text(encoding="utf-8")
            self.register_secret(key_text)
        except Exception:
            pass

        def _cleanup_tls_files() -> None:
            for p in (cert_path, key_path):
                try:
                    if p.is_file():
                        p.unlink()
                except Exception:
                    pass

        self.lifecycle.register_cleanup_callback(_cleanup_tls_files)

        schema_init_cmd = [
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "platform_api.cli",
            "init-db",
        ]
        schema_init_env = {
            "PLATFORM_DATABASE_URL": platform_url,
            "PLATFORM_TEST_DATABASE_ADMIN_URL": admin_url,
            "PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN": self.run_token,
            "_VERIFY_RUN_OWNERSHIP_TOKEN": self.run_token,
            "PLATFORM_AUTH_CSRF_KEY": csrf_key,
            "PLATFORM_AUTH_RATE_KEY": rate_key,
            "PLATFORM_AUTH_MFA_KEY": mfa_key,
            "PLATFORM_AUTH_MAIL_DIR": str(mail_dir),
            "PLATFORM_AUTH_MAIL_MODE": "file",
        }
        init_rc, init_out, init_err = self.run_cmd("browser_schema_init_tls", schema_init_cmd, env=schema_init_env)
        if init_rc != 0:
            raise RuntimeError(f"failed to initialize platform schema before TLS browser tests: {redact_secrets(init_err or init_out)}")

        # Start control-plane API server with production cookie semantics
        api_cmd = [
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "uvicorn",
            "platform_api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
        ]
        api_env = {
            "PLATFORM_DATABASE_URL": platform_url,
            "PLATFORM_TEST_DATABASE_ADMIN_URL": admin_url,
            "PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN": self.run_token,
            "_VERIFY_RUN_OWNERSHIP_TOKEN": self.run_token,
            "PLATFORM_HOST": "127.0.0.1",
            "PLATFORM_PORT": str(api_port),
            "PLATFORM_WEB_ORIGIN": f"https://127.0.0.1:{ingress_port}",
            "PLATFORM_ALLOWED_HOSTS": '["127.0.0.1", "localhost"]',
            "PLATFORM_AUTH_CSRF_KEY": csrf_key,
            "PLATFORM_AUTH_RATE_KEY": rate_key,
            "PLATFORM_AUTH_MFA_KEY": mfa_key,
            "PLATFORM_AUTH_MAIL_DIR": str(mail_dir),
            "PLATFORM_AUTH_MAIL_MODE": "file",
            "PLATFORM_ENV": "development",
            "PLATFORM_COOKIE_SECURE": "true",
            "PLATFORM_SESSION_COOKIE_NAME": "__Host-platform_session",
            "PLATFORM_TRUSTED_PROXY_CIDRS": '["127.0.0.1/32"]',
        }

        api_stdout_path = self.run_dir / "api_server_tls.stdout.log"
        api_stderr_path = self.run_dir / "api_server_tls.stderr.log"
        api_log_out = _open_private_log(api_stdout_path)
        api_log_err = _open_private_log(api_stderr_path)
        self.lifecycle.register_cleanup_callback(api_log_out.close)
        self.lifecycle.register_cleanup_callback(api_log_err.close)
        self.lifecycle.register_cleanup_callback(api_log_out.flush)
        self.lifecycle.register_cleanup_callback(api_log_err.flush)
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(api_stdout_path, explicit_secrets=self.explicit_secrets))
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(api_stderr_path, explicit_secrets=self.explicit_secrets))

        api_proc = subprocess.Popen(
            api_cmd,
            cwd=self.repo_root,
            env=self._build_sanitized_env(api_env),
            stdout=api_log_out,
            stderr=api_log_err,
            start_new_session=True,
        )
        self.lifecycle.register_process(api_proc, is_isolated_group=True)

        api_ready = False
        for _ in range(30):
            if api_proc.poll() is not None:
                raise RuntimeError(f"TLS control-plane API exited early with code {api_proc.returncode}")
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{api_port}/health/live")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        api_ready = True
                        break
            except Exception:
                pass
            time.sleep(0.5)

        if not api_ready:
            raise RuntimeError(f"TLS control-plane API server failed to respond on http://127.0.0.1:{api_port}/health/live within 15s")

        web_dir = self.repo_root / "web"
        next_bin = web_dir / "node_modules" / "next" / "dist" / "bin" / "next"
        if not next_bin.is_file():
            raise RuntimeError(f"next binary missing at {next_bin}")

        # Build Next.js web studio configured for same-origin /api/v1 routing
        web_bld_cmd = ["bun", "run", "build"]
        web_bld_env = {
            "NEXT_PUBLIC_APP_MODE": "saas",
            "NEXT_PUBLIC_PLATFORM_API_URL": "/api/v1",
            "NODE_ENV": "production",
        }
        bld_rc, bld_out, bld_err = self.run_cmd("browser_web_build_tls", web_bld_cmd, cwd=web_dir, env=web_bld_env)
        if bld_rc != 0:
            raise RuntimeError(f"failed to build web studio for TLS browser gate: {redact_secrets(bld_err or bld_out)}")

        if not (web_dir / ".next").is_dir():
            raise RuntimeError(
                "Built production web studio missing at web/.next; verify browser requires a built production server, refusing next dev fallback"
            )

        web_cmd = ["node", str(next_bin), "start", "-H", "127.0.0.1", "-p", str(web_port)]
        web_env = {
            "PORT": str(web_port),
            "HOST": "127.0.0.1",
            "NODE_ENV": "production",
            "NEXT_PUBLIC_APP_MODE": "saas",
            "NEXT_PUBLIC_PLATFORM_API_URL": "/api/v1",
        }
        web_stdout_path = self.run_dir / "web_server_tls.stdout.log"
        web_stderr_path = self.run_dir / "web_server_tls.stderr.log"
        web_log_out = _open_private_log(web_stdout_path)
        web_log_err = _open_private_log(web_stderr_path)
        self.lifecycle.register_cleanup_callback(web_log_out.close)
        self.lifecycle.register_cleanup_callback(web_log_err.close)
        self.lifecycle.register_cleanup_callback(web_log_out.flush)
        self.lifecycle.register_cleanup_callback(web_log_err.flush)
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(web_stdout_path, explicit_secrets=self.explicit_secrets))
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(web_stderr_path, explicit_secrets=self.explicit_secrets))

        web_proc = subprocess.Popen(
            web_cmd,
            cwd=web_dir,
            env=self._build_sanitized_env(web_env),
            stdout=web_log_out,
            stderr=web_log_err,
            start_new_session=True,
        )
        self.lifecycle.register_process(web_proc, is_isolated_group=True)

        web_ready = False
        for _ in range(40):
            if web_proc.poll() is not None:
                raise RuntimeError(f"web studio server exited early with code {web_proc.returncode}")
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{web_port}/")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status in (200, 301, 302, 307, 308):
                        web_ready = True
                        break
            except Exception:
                pass
            time.sleep(0.5)

        if not web_ready:
            raise RuntimeError(f"web studio server failed to respond on http://127.0.0.1:{web_port}/ within 20s")

        # Start owned loopback HTTPS ingress proxy
        ingress_cmd = [
            "bun",
            "run",
            str(self.repo_root / "tests" / "support" / "https_ingress.ts"),
            "--port",
            str(ingress_port),
            "--api",
            f"http://127.0.0.1:{api_port}",
            "--web",
            f"http://127.0.0.1:{web_port}",
            "--cert",
            str(cert_path),
            "--key",
            str(key_path),
        ]
        ingress_stdout_path = self.run_dir / "ingress_server.stdout.log"
        ingress_stderr_path = self.run_dir / "ingress_server.stderr.log"
        ingress_log_out = _open_private_log(ingress_stdout_path)
        ingress_log_err = _open_private_log(ingress_stderr_path)
        self.lifecycle.register_cleanup_callback(ingress_log_out.close)
        self.lifecycle.register_cleanup_callback(ingress_log_err.close)
        self.lifecycle.register_cleanup_callback(ingress_log_out.flush)
        self.lifecycle.register_cleanup_callback(ingress_log_err.flush)
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(ingress_stdout_path, explicit_secrets=self.explicit_secrets))
        self.lifecycle.register_cleanup_callback(lambda: _redact_persisted_file(ingress_stderr_path, explicit_secrets=self.explicit_secrets))

        ingress_proc = subprocess.Popen(
            ingress_cmd,
            cwd=self.repo_root,
            env=self._build_sanitized_env({}),
            stdout=ingress_log_out,
            stderr=ingress_log_err,
            start_new_session=True,
        )
        self.lifecycle.register_process(ingress_proc, is_isolated_group=True)

        # Verify HTTPS Ingress endpoint
        import ssl
        ingress_ready = False
        ssl_ctx = ssl._create_unverified_context()
        for _ in range(40):
            if ingress_proc.poll() is not None:
                raise RuntimeError(f"HTTPS ingress proxy exited early with code {ingress_proc.returncode}")
            try:
                req = urllib.request.Request(f"https://127.0.0.1:{ingress_port}/health/ingress")
                with urllib.request.urlopen(req, timeout=1.0, context=ssl_ctx) as resp:
                    if resp.status == 200:
                        ingress_ready = True
                        break
            except Exception:
                pass
            time.sleep(0.5)

        if not ingress_ready:
            raise RuntimeError(
                f"HTTPS ingress proxy failed to respond on https://127.0.0.1:{ingress_port}/health/ingress within 20s"
            )

        # Run TLS Playwright journeys
        playwright_json = self.run_dir / "playwright_tls.json"
        cmd = [
            "bun",
            "run",
            "test:e2e",
            "e2e/tls-ingress.spec.ts",
            "--project=chromium",
            "--reporter=json",
        ]
        env = {
            "PLATFORM_DATABASE_URL": platform_url,
            "PLATFORM_TEST_DATABASE_ADMIN_URL": admin_url,
            "PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN": self.run_token,
            "_VERIFY_RUN_OWNERSHIP_TOKEN": self.run_token,
            "PLATFORM_AUTH_CSRF_KEY": csrf_key,
            "PLATFORM_AUTH_RATE_KEY": rate_key,
            "PLATFORM_AUTH_MFA_KEY": mfa_key,
            "PLATFORM_AUTH_MAIL_DIR": str(mail_dir),
            "PLATFORM_AUTH_MAIL_MODE": "file",
            "NEXT_PUBLIC_APP_MODE": "saas",
            "NEXT_PUBLIC_PLATFORM_API_URL": "/api/v1",
            "PLAYWRIGHT_BASE_URL": f"https://127.0.0.1:{ingress_port}",
            "PLAYWRIGHT_JSON_OUTPUT_NAME": str(playwright_json),
        }
        rc, out, err = self.run_cmd("browser_tls_tests", cmd, cwd=web_dir, env=env)
        self.lifecycle.register_cleanup_callback(
            lambda: _redact_persisted_file(playwright_json, explicit_secrets=self.explicit_secrets)
            if playwright_json.is_file()
            else None
        )

        try:
            if rc != 0:
                raise ValueError(f"TLS playwright exited with code {rc}: {redact_secrets(err or out)}")
            counts = parse_playwright_json(playwright_json)
            return counts
        finally:
            self.lifecycle.cleanup()

    def browser(self, mode: str = "all", standalone: bool = True) -> int:
        print(f"==> Running verify browser lane (mode={mode})...")
        self.write_manifest()
        if mode not in ("all", "tls", "dev"):
            raise ValueError(f"unknown browser verification mode: {mode}")

        dev_counts: GateCounts | None = None
        tls_counts: GateCounts | None = None

        try:
            if mode in ("all", "dev"):
                print("--> Executing development split-port browser journey...")
                dev_counts = self._run_browser_dev()
                require_gate(dev_counts)

            if mode in ("all", "tls"):
                print("--> Executing TLS production-like ingress browser journey...")
                tls_counts = self._run_browser_tls_ingress()
                require_gate(tls_counts)

            # Combine counts for gate reporting
            if dev_counts and tls_counts:
                combined_counts = GateCounts(
                    collected=dev_counts.collected + tls_counts.collected,
                    passed=dev_counts.passed + tls_counts.passed,
                    failed=dev_counts.failed + tls_counts.failed,
                    errors=dev_counts.errors + tls_counts.errors,
                    skipped=dev_counts.skipped + tls_counts.skipped,
                    xfailed=dev_counts.xfailed + tls_counts.xfailed,
                    xpassed=dev_counts.xpassed + tls_counts.xpassed,
                )
            elif tls_counts:
                combined_counts = tls_counts
            elif dev_counts:
                combined_counts = dev_counts
            else:
                raise ValueError("no browser tests executed")

            require_gate(combined_counts)
            if combined_counts.collected == 0 or combined_counts.passed == 0:
                raise ValueError("zero browser tests collected")

            playwright_json = self.run_dir / "playwright.json"
            merged_stats = {
                "stats": {
                    "expected": combined_counts.passed,
                    "unexpected": combined_counts.failed,
                    "flaky": 0,
                    "skipped": combined_counts.skipped,
                }
            }
            playwright_json.write_text(json.dumps(merged_stats, indent=2), encoding="utf-8")
            _redact_persisted_file(playwright_json, explicit_secrets=self.explicit_secrets)

            self.results["browser_gate"] = {
                "status": "passed",
                "counts": combined_counts.__dict__,
            }
        except Exception as e:
            self.results["browser_gate"] = {"status": "failed", "reason": redact_secrets(str(e))}
        finally:
            try:
                self.lifecycle.cleanup()
            except Exception as e:
                self.results["cleanup"] = {"status": "failed", "reason": redact_secrets(str(e))}
            # Ensure persisted server logs are closed and redacted after process termination
            for log_p in (
                self.run_dir / "api_server.stdout.log",
                self.run_dir / "api_server.stderr.log",
                self.run_dir / "web_server.stdout.log",
                self.run_dir / "web_server.stderr.log",
                self.run_dir / "api_server_dev.stdout.log",
                self.run_dir / "api_server_dev.stderr.log",
                self.run_dir / "web_server_dev.stdout.log",
                self.run_dir / "web_server_dev.stderr.log",
                self.run_dir / "api_server_tls.stdout.log",
                self.run_dir / "api_server_tls.stderr.log",
                self.run_dir / "web_server_tls.stdout.log",
                self.run_dir / "web_server_tls.stderr.log",
                self.run_dir / "ingress_server.stdout.log",
                self.run_dir / "ingress_server.stderr.log",
            ):
                try:
                    _redact_persisted_file(log_p, explicit_secrets=self.explicit_secrets)
                except Exception as e:
                    self.results["cleanup"] = {"status": "failed", "reason": redact_secrets(str(e))}

        browser_passed = (
            self.results.get("browser_gate", {}).get("status") == "passed"
            and not self.lifecycle.cleanup_failed
        )
        if standalone:
            return self.write_result(target_gates=["browser_gate"])
        return 0 if browser_passed else 1
    def voice(self, profile: str, config: str | None = None, standalone: bool = True) -> int:
        print(f"==> Running verify voice lane (profile={profile})...")
        self.write_manifest()
        gate_name = "voice_local_offline" if profile == "local_offline" else "voice_deployment"
        try:
            audio_root = os.environ.get("VERIFY_AUDIO_ROOT")
            action = prerequisite_action(available=bool(audio_root), required=True)
            if action != "run":
                self.results[gate_name] = {
                    "status": "failed",
                    "reason": "VERIFY_AUDIO_ROOT missing for voice qualification",
                }
                if standalone:
                    return self.write_result(target_gates=[gate_name])
                return 1

            junit_xml = self.run_dir / "audio.xml"
            cmd = build_gate_command(gate_name, self.repo_root) + [f"--junitxml={junit_xml}"]
            env = {
                "RUN_AUDIO_QUALIFICATION": "1",
                "VERIFY_QUALIFICATION_CONFIG": config or "",
            }

            rc, out, err = self.run_cmd("voice_qualification", cmd, env=env)
            try:
                if rc != 0:
                    raise ValueError(f"voice qualification exited with code {rc}: {redact_secrets(err or out)}")
                counts = parse_junit_xml(junit_xml)
                require_gate(counts)
            except Exception as e:
                self.results[gate_name] = {"status": "failed", "reason": redact_secrets(str(e))}
            else:
                self.results[gate_name] = {"status": "passed", "counts": counts.__dict__}
        finally:
            try:
                self.lifecycle.cleanup()
            except Exception as e:
                self.results["cleanup"] = {"status": "failed", "reason": redact_secrets(str(e))}

        voice_passed = (
            self.results.get(gate_name, {}).get("status") == "passed"
            and not self.lifecycle.cleanup_failed
        )
        if standalone:
            return self.write_result(target_gates=[gate_name])
        return 0 if voice_passed else 1

    def release(self, profile: str, config: str | None = None) -> int:
        print("==> Running verify release gate...")
        self.write_manifest()

        required_gates = [
            "python_unit",
            "bun_test",
            "bun_typecheck",
            "bun_build",
            "postgres_gate",
            "browser_gate",
            "voice_local_offline",
            "voice_deployment",
            "release_evidence",
        ]
        for g in required_gates:
            self.results[g] = {"status": "not_run", "reason": "pending execution"}

        # Profile validation: release gate requires deployment profile and reviewed config
        if profile != "deployment":
            for g in required_gates:
                self.results[g] = {"status": "not_run", "reason": "release gate requires --profile deployment"}
            self.results["release_evidence"] = {
                "status": "failed",
                "reason": "release gate requires --profile deployment",
            }
            return self.write_result(target_gates=required_gates)

        if not config or not Path(config).is_file():
            for g in required_gates:
                self.results[g] = {"status": "not_run", "reason": f"reviewed qualification config JSON missing or invalid: {config}"}
            self.results["release_evidence"] = {
                "status": "failed",
                "reason": f"reviewed qualification config JSON missing or invalid: {config}",
            }
            return self.write_result(target_gates=required_gates)

        # 1. unit gate (python_unit, bun_test, bun_typecheck, bun_build)
        unit_rc = self.unit(standalone=False)
        if unit_rc != 0:
            for g in ["postgres_gate", "browser_gate", "voice_local_offline", "voice_deployment", "release_evidence"]:
                if self.results[g]["status"] == "not_run":
                    self.results[g]["reason"] = "prerequisite unit gate failed"
            return self.write_result(target_gates=required_gates)

        # 2. postgres gate
        pg_rc = self.postgres(standalone=False)
        if pg_rc != 0:
            for g in ["browser_gate", "voice_local_offline", "voice_deployment", "release_evidence"]:
                if self.results[g]["status"] == "not_run":
                    self.results[g]["reason"] = "prerequisite postgres gate failed"
            return self.write_result(target_gates=required_gates)

        # 3. browser gate
        br_rc = self.browser(standalone=False)
        if br_rc != 0:
            for g in ["voice_local_offline", "voice_deployment", "release_evidence"]:
                if self.results[g]["status"] == "not_run":
                    self.results[g]["reason"] = "prerequisite browser gate failed"
            return self.write_result(target_gates=required_gates)

        # 4. voice local_offline profile
        vo_local_rc = self.voice(profile="local_offline", standalone=False)
        if vo_local_rc != 0:
            for g in ["voice_deployment", "release_evidence"]:
                if self.results[g]["status"] == "not_run":
                    self.results[g]["reason"] = "prerequisite voice local_offline gate failed"
            return self.write_result(target_gates=required_gates)

        # 5. voice deployment profile
        vo_dep_rc = self.voice(profile="deployment", config=config, standalone=False)
        if vo_dep_rc != 0:
            if self.results["release_evidence"]["status"] == "not_run":
                self.results["release_evidence"]["reason"] = "prerequisite voice deployment gate failed"
            return self.write_result(target_gates=required_gates)

        # 6. release evidence tests
        try:
            junit_xml = self.run_dir / "release-evidence.xml"
            cmd = build_gate_command("release", self.repo_root) + [f"--junitxml={junit_xml}"]
            rc, out, err = self.run_cmd("release_evidence", cmd)
            try:
                if rc != 0:
                    raise ValueError(f"release evidence tests exited with code {rc}: {redact_secrets(err or out)}")
                counts = parse_junit_xml(junit_xml)
                require_gate(counts)
            except Exception as e:
                self.results["release_evidence"] = {"status": "failed", "reason": redact_secrets(str(e))}
            else:
                self.results["release_evidence"] = {
                    "status": "passed",
                    "counts": counts.__dict__,
                }
        finally:
            try:
                self.lifecycle.cleanup()
            except Exception as e:
                self.results["cleanup"] = {"status": "failed", "reason": redact_secrets(str(e))}

        return self.write_result(target_gates=required_gates)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Voice AI Verification and Qualification Dispatcher"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # prepare
    subparsers.add_parser("prepare", help="Prepare dependencies and cached container images")

    # unit
    subparsers.add_parser("unit", help="Run Python and Bun unit verification lanes")

    # postgres
    subparsers.add_parser("postgres", help="Run PostgreSQL integration tests")

    # browser
    browser_parser = subparsers.add_parser("browser", help="Run Playwright browser journeys")
    browser_parser.add_argument(
        "--mode",
        choices=["all", "tls", "dev"],
        default="all",
        help="Browser qualification mode: all (dev split-port + TLS production ingress), tls, dev",
    )

    # voice
    voice_parser = subparsers.add_parser("voice", help="Run voice qualification")
    voice_parser.add_argument(
        "--profile",
        choices=["local_offline", "deployment"],
        required=True,
        help="Voice qualification profile",
    )
    voice_parser.add_argument(
        "--config",
        help="Path to reviewed qualification config JSON",
    )

    # release
    release_parser = subparsers.add_parser("release", help="Run full release gate")
    release_parser.add_argument(
        "--profile",
        choices=["local_offline", "deployment"],
        required=True,
        help="Voice qualification profile",
    )
    release_parser.add_argument(
        "--config",
        help="Path to reviewed qualification config JSON",
    )

    args = parser.parse_args()
    harness = VerificationHarness(REPO_ROOT)

    if args.subcommand == "prepare":
        sys.exit(harness.prepare())
    elif args.subcommand == "unit":
        sys.exit(harness.unit())
    elif args.subcommand == "postgres":
        sys.exit(harness.postgres())
    elif args.subcommand == "browser":
        sys.exit(harness.browser(mode=args.mode))
    elif args.subcommand == "voice":
        sys.exit(harness.voice(profile=args.profile, config=args.config))
    elif args.subcommand == "release":
        sys.exit(harness.release(profile=args.profile, config=args.config))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
