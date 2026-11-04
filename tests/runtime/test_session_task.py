from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bot.runtime.snapshots import RuntimeSnapshot
from bot.runtime.session_task import (
    PendingSession,
    RetainedSession,
    SessionAdmissionError,
    SessionTaskManager,
)
from bot.runtime.transport_auth import RuntimePlatformError
from platform_api.sessions.schemas import (
    JoinConsumptionView,
    RuntimeRegistrationRequest,
    RuntimeReleaseRequest,
    SessionStateView,
)


class NoPlatformCalls:
    credentials = SimpleNamespace(
        runner_id="runner-local",
        web_origin="https://voice.example.test",
        service_token="service-token-value-that-is-long-enough-for-tests",
    )
    timeout_seconds = 0.1

    def __init__(self) -> None:
        self.runtime_config_calls = 0
        self.consume_join_calls = 0

    async def runtime_config(self, session_id: str):
        self.runtime_config_calls += 1
        raise AssertionError("runtime configuration must not be requested without PostgreSQL storage")

    async def consume_join(self, session_id: str, request):
        self.consume_join_calls += 1
        raise AssertionError("a browser join must not be consumed without PostgreSQL storage")


class BlockingRuntimeConfigPlatform(NoPlatformCalls):
    def __init__(self) -> None:
        super().__init__()
        self.started_first = asyncio.Event()
        self.started_second = asyncio.Event()
        self.release_first = asyncio.Event()
        self.release_second = asyncio.Event()
        self.active_calls = 0
        self.max_active_calls = 0

    async def runtime_config(self, session_id: str):
        self.runtime_config_calls += 1
        call = self.runtime_config_calls
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        started, release = (
            (self.started_first, self.release_first)
            if call == 1
            else (self.started_second, self.release_second)
        )
        started.set()
        try:
            await release.wait()
        finally:
            self.active_calls -= 1
        raise RuntimePlatformError("session is unavailable", status_code=404)


class RecordingStopStore:
    storage_backend = "postgresql"

    def __init__(self, events: list[str], *, ready_error: bool = False) -> None:
        self.events = events
        self.records = {}
        self.ready_error = ready_error
        self.ready_checks = 0

    async def ensure_ready(self) -> None:
        self.ready_checks += 1
        if self.ready_error:
            raise RuntimeError("schema unavailable")

    async def load_stop_record(self, session_id: str, session_version: int):
        return self.records.get((session_id, session_version))

    async def save_stop_record(self, session_id: str, session_version: int, record) -> None:
        self.events.append("stop_record_saved")
        self.records[(session_id, session_version)] = record



class RecordingPlaybackMessageRuntime:
    def __init__(self, *, block: bool = False) -> None:
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.deny_calls = 0

    async def accept_playback_receipt(self, message, *, sender_peer_id: str):
        self.calls.append((message, sender_peer_id))
        self.started.set()
        await self.release.wait()
        return True

    def deny_speech(self) -> None:
        self.deny_calls += 1


def _bound_message_manager(*, block: bool = False):
    manager = SessionTaskManager(
        platform=NoPlatformCalls(),
        base_settings=_settings(),
    )
    session_id = uuid4()
    peer_id = uuid4()
    pc_id = "smallwebrtc-pc"
    origin = "https://voice.example.test"
    runtime = RecordingPlaybackMessageRuntime(block=block)
    pending = SimpleNamespace(
        snapshot=SimpleNamespace(session_id=session_id),
        peer_id=peer_id,
        origin=origin,
        pc_id=pc_id,
        platform_release=False,
        pipeline_done=asyncio.Event(),
        playback_receipt_lock=asyncio.Lock(),
        runtime=runtime,
    )
    manager._pending[session_id] = pending
    manager._pc_bindings[pc_id] = (session_id, peer_id, origin)
    return manager, pending, runtime

class RecordingMeter:
    receipt_source = "committed_playback_receipts"
    receipt_verifier = None

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    async def cumulative_billable_ms(self) -> int:
        return 1_250


class RecordingBudget:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.close_request = None

    async def close(self, **kwargs) -> None:
        self.events.append("budget_closed")
        self.close_request = kwargs


class RecordingRuntime:
    def __init__(self, events: list[str], *, terminal: bool, outcome: str) -> None:
        self.events = events
        self.state = SimpleNamespace(teardown_emitted=terminal, outcome=outcome)

    def deny_speech(self) -> None:
        self.events.append("speech_denied")

    async def pipeline_ended(self, reason: str) -> None:
        self.events.append("pipeline_ended")

    async def interrupt(self) -> None:
        self.events.append("flow_interrupted")


class RecordingConnection:
    def __init__(self, events: list[str], *, stops: bool = True) -> None:
        self.events = events
        self.stops = stops
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.events.append("media_stopped")
        if self.stops:
            self.connected = False


class RecordingCursor:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("cursor_closed")


class SessionLifecyclePlatform(NoPlatformCalls):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.manager = None
        self.session_id = None
        self.registration_id = None
        self.session_version = 3
        self.state = "ACTIVE"
        self.transitions = []
        self.peer_releases = []

    async def runtime_config(self, session_id: str):
        return SimpleNamespace(
            registration_id=self.registration_id,
            runner_id=self.credentials.runner_id,
            session_version=self.session_version,
            state=self.state,
            budget=SimpleNamespace(budget_version=11),
        )

    async def transition(self, session_id: str, payload):
        self.events.append(f"transition_{payload.target_state.lower()}")
        self.transitions.append(payload)
        assert payload.expected_version == self.session_version
        if payload.target_state == "ENDING":
            self.state = "ENDING"
            self.session_version += 1
            release = RuntimeReleaseRequest(
                session_id=self.session_id,
                runner_id=self.credentials.runner_id,
                registration_id=self.registration_id,
                session_version=self.session_version,
            )
            await self.manager.release_registration(release)
        else:
            assert self.state == "ENDING"
            assert self.manager._registrations[self.session_id].released
            self.state = payload.target_state
            self.session_version += 1
        return SessionStateView(
            session_id=self.session_id,
            state=self.state,
            session_version=self.session_version,
            ended_at=datetime.now(timezone.utc) if self.state in {"COMPLETED", "FAILED"} else None,
            failure_code=payload.failure_code,
        )

    async def release_peer(self, session_id: str, request):
        self.peer_releases.append((session_id, request))
        return SimpleNamespace(session_id=self.session_id, peer_id=request.peer_id, released=True)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        platform_billing_slice_ms="1000",
        platform_billing_permit_ttl_ms="5000",
    )


def _registration_request(session_id, registration_id) -> RuntimeRegistrationRequest:
    now = datetime.now(timezone.utc)
    return RuntimeRegistrationRequest(
        session_id=session_id,
        runner_id="runner-local",
        registration_id=registration_id,
        config_version_id=uuid4(),
        session_version=1,
        connect_deadline=now + timedelta(minutes=1),
        hard_end_deadline=now + timedelta(minutes=10),
    )


async def _lifecycle_manager(*, terminal: bool, outcome: str):
    events: list[str] = []
    platform = SessionLifecyclePlatform(events)
    store = RecordingStopStore(events)
    manager = SessionTaskManager(
        platform=platform,
        base_settings=_settings(),
        event_store=store,
        usage_meter_factory=None,
        installed_adapter_report_hash="qualified-adapter-hash",
    )
    platform.manager = manager
    budget = RecordingBudget(events)
    manager._budget = budget
    session_id = uuid4()
    registration_id = uuid4()
    platform.session_id = session_id
    platform.registration_id = registration_id
    request = _registration_request(session_id, registration_id)
    await manager.register(request)

    snapshot = SimpleNamespace(session_id=session_id, registration_id=registration_id)
    runtime = RecordingRuntime(events, terminal=terminal, outcome=outcome)
    meter = RecordingMeter(str(session_id))
    peer_id = uuid4()
    pending = PendingSession(
        snapshot=snapshot,
        join=JoinConsumptionView(
            session_id=session_id,
            peer_id=peer_id,
            runner_id="runner-local",
            session_version=3,
            state="CONNECTING",
            reconnect=False,
        ),
        peer_id=peer_id,
        token_digest="token-digest",
        origin="https://voice.example.test",
        runtime=runtime,
        playback_verifier=None,
        usage_meter=meter,
        speech_gate=asyncio.Event(),
        claimed=True,
        connection=RecordingConnection(events),
        cursor=RecordingCursor(events),
        session_version=3,
        budget_version=11,
    )
    manager._pending[session_id] = pending
    manager._sessions[session_id] = RetainedSession(
        snapshot=snapshot,
        runtime=runtime,
        usage_meter=meter,
        current_pending=pending,
    )
    return manager, platform, pending, store, budget, events


@pytest.mark.asyncio
async def test_session_offer_fails_closed_before_platform_join_without_postgres_store():
    platform = NoPlatformCalls()
    manager = SessionTaskManager(platform=platform, base_settings=_settings())

    with pytest.raises(SessionAdmissionError, match="durable PostgreSQL") as error:
        await manager.prepare_offer(
            {"session_id": str(uuid4()), "join_token": "opaque-one-use-join-token"},
            origin=platform.credentials.web_origin,
        )

    assert error.value.status_code == 503
    assert platform.runtime_config_calls == 0
    assert platform.consume_join_calls == 0


@pytest.mark.asyncio
async def test_begin_pipeline_applies_published_settings_to_base_settings():
    base_settings = _settings().model_copy(
        update={
            "llm_provider": "ollama",
            "llm_base_url": "http://localhost:11434/v1",
        }
    )
    manager = SessionTaskManager(platform=NoPlatformCalls(), base_settings=base_settings)
    session_id = uuid4()
    peer_id = uuid4()
    now = datetime.now(timezone.utc)
    snapshot = RuntimeSnapshot(
        session_id=session_id,
        owner_user_id=uuid4(),
        participant_id=peer_id,
        auth_session_id=None,
        participant_kind="owner",
        origin="https://voice.example.test",
        runner_id="runner-local",
        registration_id=uuid4(),
        config_version_id=uuid4(),
        session_version=1,
        state="RECONNECTING",
        connect_deadline=now,
        hard_end_deadline=now + timedelta(minutes=10),
        budget=None,
        hashes=(),
        capabilities=None,
        compiled=None,
        model_pins={"llm_provider": "ollama", "llm_model": "published-model"},
        policy_prompt="",
        system_instruction="Published system instruction",
    )
    pending = PendingSession(
        snapshot=snapshot,
        join=JoinConsumptionView(
            session_id=session_id,
            peer_id=peer_id,
            runner_id="runner-local",
            session_version=1,
            state="RECONNECTING",
            reconnect=True,
        ),
        peer_id=peer_id,
        token_digest="join-token-digest",
        origin=snapshot.origin,
        runtime=None,
        playback_verifier=None,
        usage_meter=None,
        speech_gate=asyncio.Event(),
    )

    session_settings = await manager.begin_pipeline(pending)

    assert session_settings is not base_settings
    assert session_settings.llm_model == "published-model"
    assert session_settings.system_instruction == "Published system instruction"
    assert base_settings.llm_model != "published-model"
    assert base_settings.system_instruction != "Published system instruction"


@pytest.mark.asyncio
async def test_terminal_flow_teardown_uses_ending_and_durable_stop_before_completion():
    manager, platform, pending, store, budget, events = await _lifecycle_manager(
        terminal=True,
        outcome="completed",
    )

    await manager.finish_pipeline(pending, "flow_terminal")

    assert [transition.target_state for transition in platform.transitions] == [
        "ENDING",
        "COMPLETED",
    ]
    assert platform.peer_releases == []
    assert pending.media_stopped
    assert (
        events.index("media_stopped")
        < events.index("transition_ending")
        < events.index("stop_record_saved")
        < events.index("budget_closed")
        < events.index("transition_completed")
    )
    record = store.records[(str(pending.snapshot.session_id), 4)]
    assert record.cumulative_billable_ms == 1_250
    assert record.attestation.session_version == 4
    assert budget.close_request["cumulative_billable_ms"] == 1_250
    assert budget.close_request["stop_attestation"] is record.attestation
    assert manager._registrations[pending.snapshot.session_id].released
    assert pending.pipeline_done.is_set()


@pytest.mark.asyncio
async def test_terminal_flow_does_not_settle_when_media_stop_is_unconfirmed():
    manager, platform, pending, store, budget, _events = await _lifecycle_manager(
        terminal=True,
        outcome="completed",
    )
    pending.connection = RecordingConnection([], stops=False)

    with pytest.raises(SessionAdmissionError, match="media did not stop"):
        await manager.finish_pipeline(pending, "flow_terminal")

    assert platform.transitions == []
    assert store.records == {}
    assert budget.close_request is None


@pytest.mark.asyncio
async def test_terminal_flow_failure_is_marked_failed_after_the_same_stop_path():
    manager, platform, pending, _store, _budget, _events = await _lifecycle_manager(
        terminal=True,
        outcome="failed",
    )

    await manager.finish_pipeline(pending, "flow_failure")

    assert [transition.target_state for transition in platform.transitions] == [
        "ENDING",
        "FAILED",
    ]
    assert [transition.failure_code for transition in platform.transitions] == [
        "FLOW_FAILED",
        "FLOW_FAILED",
    ]
    assert platform.peer_releases == []


@pytest.mark.asyncio
async def test_transport_disconnect_keeps_the_reconnect_peer_release_path():
    manager, platform, pending, _store, _budget, _events = await _lifecycle_manager(
        terminal=False,
        outcome="in_progress",
    )

    await manager.finish_pipeline(pending, "transport_disconnect")

    assert platform.transitions == []
    assert len(platform.peer_releases) == 1
    assert platform.peer_releases[0][1].reason == "transport_disconnect"


@pytest.mark.asyncio
async def test_registration_fails_closed_when_store_schema_is_unready():
    platform = NoPlatformCalls()
    store = RecordingStopStore([], ready_error=True)
    manager = SessionTaskManager(
        platform=platform,
        base_settings=_settings(),
        event_store=store,
    )

    with pytest.raises(SessionAdmissionError, match="unavailable"):
        await manager.register(_registration_request(uuid4(), uuid4()))

    assert manager._registrations == {}
    assert store.ready_checks == 1


@pytest.mark.asyncio
async def test_playback_callback_maps_bound_pc_sender_to_platform_peer():
    manager, pending, runtime = _bound_message_manager()
    message = {"type": "flow-playback-receipt", "version": 1}

    assert await manager._accept_bound_playback_message(
        pending,
        message,
        pending.pc_id,
    ) is True
    assert runtime.calls == [(message, str(pending.peer_id))]

    assert await manager._accept_bound_playback_message(
        pending,
        message,
        str(pending.peer_id),
    ) is None
    assert await manager._accept_bound_playback_message(
        pending,
        message,
        "unexpected-pc",
    ) is None

    manager._pc_bindings[pending.pc_id] = (
        pending.snapshot.session_id,
        uuid4(),
        pending.origin,
    )
    assert await manager._accept_bound_playback_message(
        pending,
        message,
        pending.pc_id,
    ) is None
    assert len(runtime.calls) == 1
    manager._pc_bindings[pending.pc_id] = (
        pending.snapshot.session_id,
        pending.peer_id,
        pending.origin,
    )
    manager._pending[pending.snapshot.session_id] = SimpleNamespace()
    assert await manager._accept_bound_playback_message(
        pending,
        message,
        pending.pc_id,
    ) is None


@pytest.mark.asyncio
async def test_playback_callback_serializes_receipt_with_release():
    manager, pending, runtime = _bound_message_manager(block=True)
    message = {"type": "flow-playback-receipt", "version": 1}
    receipt_task = asyncio.create_task(
        manager._accept_bound_playback_message(pending, message, pending.pc_id)
    )
    await runtime.started.wait()

    release_task = asyncio.create_task(manager._mark_platform_release(pending))
    await asyncio.sleep(0)
    assert not release_task.done()
    assert not pending.platform_release

    runtime.release.set()
    assert await receipt_task is True
    await release_task
    assert pending.platform_release
    assert runtime.deny_calls == 1
    assert await manager._accept_bound_playback_message(
        pending,
        message,
        pending.pc_id,
    ) is None
    assert len(runtime.calls) == 1


@pytest.mark.asyncio
async def test_abort_transport_halts_output_before_closing_connection():
    events = []

    class Cursor:
        halted = False

        def halt(self):
            events.append("halt")
            self.halted = True

        def can_write(self):
            return not self.halted

    cursor = Cursor()

    class Connection:
        connected = True

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            assert cursor.halted
            events.append("disconnect")
            self.connected = False

    manager = SessionTaskManager(
        platform=NoPlatformCalls(),
        base_settings=_settings(),
    )
    pending = SimpleNamespace(media_stopped=False)

    assert await manager._abort_transport(pending, cursor, Connection()) is True
    assert events == ["halt", "disconnect"]
    assert not cursor.can_write()
    assert pending.media_stopped is True

@pytest.mark.asyncio
async def test_invalid_session_requests_reclaim_admission_locks(monkeypatch):
    platform = NoPlatformCalls()
    store = RecordingStopStore([])
    manager = SessionTaskManager(
        platform=platform,
        base_settings=_settings(),
        event_store=store,
    )
    monkeypatch.setattr(manager, "_require_session_prerequisites", lambda: None)

    for _ in range(20):
        session_id = uuid4()
        with pytest.raises(SessionAdmissionError, match="not registered"):
            await manager.prepare_offer(
                {"session_id": str(session_id), "join_token": "valid-join-token"},
                origin=platform.credentials.web_origin,
            )
        with pytest.raises(SessionAdmissionError, match="live runner registration"):
            await manager.release_registration(
                RuntimeReleaseRequest(
                    session_id=session_id,
                    runner_id=platform.credentials.runner_id,
                    registration_id=uuid4(),
                    session_version=1,
                )
            )
        assert manager._admission_locks == {}


@pytest.mark.asyncio
async def test_admission_lock_waiters_share_the_lock_until_they_finish(monkeypatch):
    platform = BlockingRuntimeConfigPlatform()
    store = RecordingStopStore([])
    manager = SessionTaskManager(
        platform=platform,
        base_settings=_settings(),
        event_store=store,
    )
    monkeypatch.setattr(manager, "_require_session_prerequisites", lambda: None)
    session_id = uuid4()
    registration_id = uuid4()
    await manager.register(_registration_request(session_id, registration_id))
    request_data = {"session_id": str(session_id), "join_token": "valid-join-token"}
    origin = platform.credentials.web_origin

    first = asyncio.create_task(manager.prepare_offer(request_data, origin=origin))
    await platform.started_first.wait()
    shared_lock = manager._admission_locks[session_id]

    second_started = asyncio.Event()

    async def prepare_second():
        second_started.set()
        return await manager.prepare_offer(request_data, origin=origin)

    second = asyncio.create_task(prepare_second())
    await second_started.wait()
    await asyncio.sleep(0)
    assert platform.runtime_config_calls == 1

    platform.release_first.set()
    with pytest.raises(SessionAdmissionError) as first_error:
        await first
    assert first_error.value.status_code == 404
    await platform.started_second.wait()
    assert manager._admission_locks[session_id] is shared_lock

    platform.release_second.set()
    with pytest.raises(SessionAdmissionError) as second_error:
        await second
    assert second_error.value.status_code == 404
    assert platform.max_active_calls == 1
    assert manager._admission_locks == {}
