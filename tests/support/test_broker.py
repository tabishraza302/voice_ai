from __future__ import annotations

import subprocess

import pytest

from tests.support.broker import (
    OWNERSHIP_LABEL,
    RABBITMQ_DATA_TMPFS,
    RABBITMQ_USER,
    BrokerUnavailable,
    RabbitMqBroker,
    _start_broker_with_cleanup,
)


def test_external_broker_container_hint_never_enables_control(monkeypatch) -> None:
    url = "amqp://voice_ai:voice_ai@127.0.0.1:5672/%2F"
    monkeypatch.setenv("PLATFORM_TEST_AMQP_URL", url)
    monkeypatch.setenv("PLATFORM_TEST_AMQP_CONTAINER", "user-owned-container")
    docker_calls: list[tuple[object, ...]] = []

    def forbidden_docker(*args, **kwargs):
        docker_calls.append(args)
        raise AssertionError("external broker must never be controlled through Docker")

    monkeypatch.setattr("tests.support.broker._docker", forbidden_docker)
    broker = RabbitMqBroker.from_environment()
    monkeypatch.setattr(broker, "wait_ready", lambda: None)

    broker.start()
    assert broker.url == url
    assert broker.container_name is None
    assert broker.can_restart is False
    with pytest.raises(BrokerUnavailable):
        broker.stop()
    broker.remove()
    assert docker_calls == []


def test_container_without_this_run_marker_is_never_restarted(monkeypatch) -> None:
    docker_calls: list[tuple[object, ...]] = []

    def inspect_other_run(*args, **kwargs):
        docker_calls.append(args)
        stdout = "container-id|different-run\n" if "--format" in args else "container-id\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("tests.support.broker._docker", inspect_other_run)
    broker = RabbitMqBroker(
        url="amqp://voice_ai:voice_ai@127.0.0.1:5672/%2F",
        container_name="some-container",
        owns_container=True,
        ownership_token="this-run",
    )

    with pytest.raises(BrokerUnavailable):
        broker.start()
    with pytest.raises(BrokerUnavailable):
        broker.stop()
    broker.remove()
    assert not any(call[0] in {"start", "stop", "rm", "run"} for call in docker_calls)


def test_matching_run_marker_restarts_by_verified_container_id(monkeypatch) -> None:
    docker_calls: list[tuple[object, ...]] = []

    def inspect_this_run(*args, **kwargs):
        docker_calls.append(args)
        if "--format" in args:
            template = args[args.index("--format") + 1]
            stdout = "container-id|this-run\n" if "voice-ai-events-test-run" in template else "false\n"
        else:
            stdout = "container-id\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    def record_docker(*args, **kwargs):
        docker_calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def fake_docker(*args, **kwargs):
        if args[0] == "inspect":
            return inspect_this_run(*args, **kwargs)
        return record_docker(*args, **kwargs)

    monkeypatch.setattr("tests.support.broker._docker", fake_docker)
    broker = RabbitMqBroker(
        url="amqp://voice_ai:voice_ai@127.0.0.1:5672/%2F",
        container_name="test-container",
        owns_container=True,
        ownership_token="this-run",
    )
    monkeypatch.setattr(broker, "wait_ready", lambda: None)

    broker.start()
    broker.stop()

    assert ("start", "container-id") in docker_calls
    assert ("stop", "--time", "5", "container-id") in docker_calls
    assert not any(call[-1] == "test-container" for call in docker_calls if call[0] in {"start", "stop"})


def test_new_container_gets_a_run_specific_ownership_label(monkeypatch) -> None:
    monkeypatch.delenv("PLATFORM_TEST_AMQP_URL", raising=False)
    docker_calls: list[tuple[object, ...]] = []
    broker = RabbitMqBroker.from_environment()

    def fake_docker(*args, **kwargs):
        docker_calls.append(args)
        if args[0] == "run":
            return subprocess.CompletedProcess(args, 0, stdout="container-id", stderr="")
        if args[0] == "inspect" and "--format" not in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
        if args[0] == "inspect":
            template = args[args.index("--format") + 1]
            if OWNERSHIP_LABEL in template:
                stdout = f"container-id|{broker.ownership_token}\n"
            else:
                stdout = "true\n"
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("tests.support.broker._docker", fake_docker)
    monkeypatch.setattr(broker, "wait_ready", lambda: None)

    broker.start()

    run_call = next(call for call in docker_calls if call[0] == "run")
    labels = [
        run_call[index + 1]
        for index, argument in enumerate(run_call[:-1])
        if argument == "--label"
    ]
    assert f"{OWNERSHIP_LABEL}={broker.ownership_token}" in labels
    assert run_call[run_call.index("--tmpfs") + 1] == RABBITMQ_DATA_TMPFS
    assert "size=128m" in RABBITMQ_DATA_TMPFS
    assert run_call[run_call.index("--user") + 1] == RABBITMQ_USER
    assert run_call[run_call.index("--memory") + 1] == "256m"
    assert run_call[run_call.index("--memory-swap") + 1] == "256m"
    assert broker.can_restart is True


def test_owned_broker_readiness_runs_as_rabbitmq_user(monkeypatch) -> None:
    docker_calls: list[tuple[object, ...]] = []

    def fake_docker(*args, **kwargs):
        docker_calls.append(args)
        if args[0] == "inspect":
            return subprocess.CompletedProcess(
                args, 0, stdout="container-id|this-run\n", stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("tests.support.broker._docker", fake_docker)
    broker = RabbitMqBroker(
        container_name="test-container",
        owns_container=True,
        ownership_token="this-run",
    )

    broker.wait_ready()

    assert (
        "exec",
        "--user",
        RABBITMQ_USER,
        "container-id",
        "rabbitmq-diagnostics",
        "-q",
        "check_running",
    ) in docker_calls


def test_startup_failure_removes_only_owned_container(monkeypatch) -> None:
    docker_calls: list[tuple[object, ...]] = []
    broker = RabbitMqBroker(
        container_name="test-container",
        owns_container=True,
        ownership_token="this-run",
    )

    def fake_docker(*args, **kwargs):
        docker_calls.append(args)
        if args[0] == "inspect":
            if "--format" not in args:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="not found"
                )
            template = args[args.index("--format") + 1]
            stdout = (
                "true\n"
                if ".State.Running" in template
                else f"container-id|{broker.ownership_token}\n"
            )
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def fail_readiness() -> None:
        raise BrokerUnavailable("readiness failed")

    monkeypatch.setattr("tests.support.broker._docker", fake_docker)
    monkeypatch.setattr(broker, "wait_ready", fail_readiness)

    with pytest.raises(BrokerUnavailable, match="readiness failed"):
        _start_broker_with_cleanup(broker)

    assert ("rm", "-f", "container-id") in docker_calls
