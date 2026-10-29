"""Disposable RabbitMQ fixture for the Phase 8 broker gates.

The image is pinned to the same tag/digest used by the optional
``tests/compose.rabbitmq.yml`` profile.  A test-owned container is bound to a
dynamic loopback port, given an explicit memory limit and removed on teardown;
it never touches the project's 5672/5673 ports or the running stack.

Set ``PLATFORM_TEST_AMQP_URL`` to use an externally managed broker. External
brokers are never controlled; the outage fault test skips for those brokers.
Without a URL, the fixture creates a test-owned container with a
run-specific ownership label.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from urllib.parse import urlsplit

import pytest


PINNED_RABBITMQ_IMAGE = "rabbitmq:4.1.3-alpine"
# docker.io/library/rabbitmq:4.1.3-alpine, resolved 2026-10-29.
PINNED_RABBITMQ_DIGEST = (
    "sha256:a6dbb0d4e409a371b0c4baa0cc844903be8702ad29e7917fd7f3d19207cb468e"
)
MEMORY_LIMIT = "256m"
RABBITMQ_USER = "100:101"
# RabbitMQ's 50 MB free-disk alarm needs headroom beyond the writable data.
# The tmpfs remains private to RabbitMQ and uses memory only as data is written.
RABBITMQ_DATA_TMPFS = (
    "/var/lib/rabbitmq:rw,nodev,nosuid,noexec,"
    "size=128m,mode=0700,uid=100,gid=101"
)
CONTAINER_PREFIX = "voice-ai-events-test-"

OWNERSHIP_LABEL = "voice-ai-events-test-run"


class BrokerUnavailable(RuntimeError):
    pass


def _docker(*args: str, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise BrokerUnavailable(
            f"docker {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RabbitMqBroker:
    def __init__(
        self,
        *,
        url: str | None = None,
        container_name: str | None = None,
        owns_container: bool = False,
        ownership_token: str | None = None,
        port: int | None = None,
    ) -> None:
        self.url = url
        self.container_name = container_name
        self.owns_container = owns_container
        self.ownership_token = ownership_token or (
            uuid.uuid4().hex if owns_container else None
        )
        self.port = port or _free_loopback_port()

    @classmethod
    def from_environment(cls) -> "RabbitMqBroker":
        url = os.environ.get("PLATFORM_TEST_AMQP_URL")
        if url:
            return cls(url=url)
        run_id = uuid.uuid4().hex
        return cls(
            container_name=f"{CONTAINER_PREFIX}{run_id[:12]}",
            owns_container=True,
            ownership_token=run_id,
        )

    def _owned_container_id(self) -> str | None:
        if (
            not self.owns_container
            or self.container_name is None
            or self.ownership_token is None
        ):
            return None
        template = (
            f'{{{{.Id}}}}|{{{{index .Config.Labels "{OWNERSHIP_LABEL}"}}}}'
        )
        result = _docker(
            "inspect",
            "--format",
            template,
            self.container_name,
            check=False,
        )
        if result.returncode != 0:
            return None
        container_id, separator, label = result.stdout.strip().partition("|")
        if not separator or label != self.ownership_token:
            return None
        return container_id or None

    @property
    def can_restart(self) -> bool:
        return self._owned_container_id() is not None

    @property
    def digest(self) -> str:
        result = _docker(
            "image",
            "inspect",
            PINNED_RABBITMQ_IMAGE,
            "--format",
            "{{index .RepoDigests 0}}",
            check=False,
        )
        return result.stdout.strip() or PINNED_RABBITMQ_DIGEST

    def start(self) -> None:
        if self.container_name is None:
            if self.url is None:
                raise BrokerUnavailable(
                    "no broker URL or test-owned container is configured"
                )
            self.wait_ready()
            return
        if not self.owns_container or self.ownership_token is None:
            raise BrokerUnavailable(
                "only a test-owned broker container may be started"
            )

        exists = _docker(
            "inspect", self.container_name, check=False
        ).returncode == 0
        if not exists:
            _docker(
                "run",
                "--detach",
                "--name",
                self.container_name,
                "--memory",
                MEMORY_LIMIT,
                "--memory-swap",
                MEMORY_LIMIT,
                "--user",
                RABBITMQ_USER,
                "--label",
                "voice-ai-events-test=1",
                "--label",
                f"{OWNERSHIP_LABEL}={self.ownership_token}",
                "-p",
                f"127.0.0.1:{self.port}:5672",
                "--tmpfs",
                RABBITMQ_DATA_TMPFS,
                "-e",
                "RABBITMQ_DEFAULT_USER=voice_ai",
                "-e",
                "RABBITMQ_DEFAULT_PASS=voice_ai",
                PINNED_RABBITMQ_IMAGE,
            )
            self.url = (
                f"amqp://voice_ai:voice_ai@127.0.0.1:{self.port}/%2F"
            )
        container_id = self._owned_container_id()
        if container_id is None:
            raise BrokerUnavailable(
                "refusing to start a container without this run's ownership label"
            )
        state = _docker(
            "inspect",
            "--format",
            "{{.State.Running}}",
            container_id,
        ).stdout.strip()
        if state != "true":
            _docker("start", container_id)
        if self.url is None:
            mapping = _docker(
                "port", container_id, "5672/tcp"
            ).stdout.strip()
            if mapping:
                self.port = int(mapping.rsplit(":", 1)[1])
            self.url = (
                f"amqp://voice_ai:voice_ai@127.0.0.1:{self.port}/%2F"
            )
        self.wait_ready()

    def wait_ready(self, timeout: float = 60.0) -> None:
        if self.container_name is None:
            if self.url is None:
                raise BrokerUnavailable("no external AMQP URL is configured")
            try:
                parsed = urlsplit(self.url)
                host = parsed.hostname
                port = parsed.port or (
                    5671 if parsed.scheme == "amqps" else 5672
                )
            except ValueError as exc:
                raise BrokerUnavailable(
                    "external AMQP URL has an invalid host or port"
                ) from exc
            if parsed.scheme not in {"amqp", "amqps"} or host is None:
                raise BrokerUnavailable(
                    "external AMQP URL must include a valid host"
                )
            self.port = port
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection((host, port), timeout=1.0):
                        return
                except OSError:
                    time.sleep(0.25)
            raise BrokerUnavailable("external AMQP broker did not open its port")

        container_id = self._owned_container_id()
        if container_id is None:
            raise BrokerUnavailable(
                "refusing to inspect a container without this run's ownership label"
            )
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            result = _docker(
                "exec",
                "--user",
                RABBITMQ_USER,
                container_id,
                "rabbitmq-diagnostics",
                "-q",
                "check_running",
                check=False,
                timeout=10.0,
            )
            if result.returncode == 0:
                return
            last = result.stderr.strip() or result.stdout.strip()
            time.sleep(0.5)
        logs = _docker(
            "logs", "--tail", "40", container_id, check=False
        ).stdout
        raise BrokerUnavailable(
            f"RabbitMQ did not become ready: {last}\n{logs}"
        )

    def stop(self) -> None:
        container_id = self._owned_container_id()
        if container_id is None:
            raise BrokerUnavailable(
                "refusing to stop a container without this run's ownership label"
            )
        _docker("stop", "--time", "5", container_id)

    def remove(self) -> None:
        container_id = self._owned_container_id()
        if container_id is None:
            return
        _docker("rm", "-f", container_id, check=False)


def _start_broker_with_cleanup(broker: RabbitMqBroker) -> None:
    try:
        broker.start()
    except Exception:
        broker.remove()
        raise


@pytest.fixture(scope="session")
def rabbitmq_broker() -> RabbitMqBroker:
    broker = RabbitMqBroker.from_environment()
    try:
        _start_broker_with_cleanup(broker)
    except Exception as exc:
        if os.environ.get("VERIFY_REQUIRED") == "1":
            pytest.fail(f"required broker prerequisite unavailable: {exc}")
        pytest.skip(f"RabbitMQ broker unavailable: {exc}")
    try:
        yield broker
    finally:
        broker.remove()


__all__ = [
    "BrokerUnavailable",
    "PINNED_RABBITMQ_DIGEST",
    "PINNED_RABBITMQ_IMAGE",
    "RabbitMqBroker",
    "rabbitmq_broker",
]
