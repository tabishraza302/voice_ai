"""Session-bound admission and peer lifecycle for the production voice runner."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import inspect
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID, uuid4

from bot.config import Settings
from bot.flow.schemas import SessionState
from bot.flow_runtime import FlowEventStore, FlowRuntime
from bot.runtime.budget_client import (
    BudgetPrerequisiteError,
    RuntimeBudgetClient,
    validate_initial_permit,
)
from bot.runtime.snapshots import (
    RuntimeSnapshot,
    RuntimeSnapshotError,
    validate_runtime_snapshot,
)
from bot.runtime.transport_auth import (
    PlatformRuntimeClient,
    RuntimePlatformError,
)
from platform_api.billing.schemas import RuntimeStopAttestation
from platform_api.sessions.schemas import (
    JoinConsumptionRequest,
    JoinConsumptionView,
    PeerReleaseRequest,
    RuntimeRegistrationRequest,
    RuntimeReleaseAcknowledgement,
    RuntimeReleaseRequest,
    RuntimeReservation,
    SessionStateView,
    SessionTransitionRequest,
)


class SessionAdmissionError(RuntimeError):
    """A session cannot safely enter the media or provider pipeline."""

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class DurablePostgresFlowStore(FlowEventStore, Protocol):
    """PostgreSQL-only event store required for session-managed flow state."""

    storage_backend: str

    async def save_stop_record(
        self,
        session_id: str,
        session_version: int,
        record: "RuntimeStopRecord",
    ) -> None:
        """Persist the exact stop proof and billing request before sending it."""
        ...

    async def load_stop_record(
        self,
        session_id: str,
        session_version: int,
    ) -> "RuntimeStopRecord | None":
        """Load an unchanged close request after a runner restart."""
        ...
class VerifiedPlaybackUsageMeter(Protocol):
    """Meter cumulative billable time only from committed render receipts."""
    session_id: str
    receipt_source: str
    receipt_verifier: Any | None

    async def cumulative_billable_ms(self) -> int:
        """Return cumulative verified rendered milliseconds for this session."""
        ...


@dataclass(frozen=True)
class RuntimeStopRecord:
    attestation: RuntimeStopAttestation
    expected_budget_version: int
    cumulative_billable_ms: int


@dataclass(frozen=True)
class SessionRegistration:
    request: RuntimeRegistrationRequest
    expires_at: datetime
    released: bool = False
    released_version: int | None = None
    stop_record: RuntimeStopRecord | None = None


@dataclass
class PendingSession:
    snapshot: RuntimeSnapshot
    join: JoinConsumptionView
    peer_id: UUID
    token_digest: str
    origin: str
    runtime: FlowRuntime
    playback_verifier: Any
    usage_meter: VerifiedPlaybackUsageMeter
    speech_gate: asyncio.Event
    claimed: bool = False
    pc_id: str | None = None
    connection: Any | None = None
    pipeline_done: asyncio.Event = field(default_factory=asyncio.Event)
    pc_bound: asyncio.Event = field(default_factory=asyncio.Event)
    platform_release: bool = False
    media_stopped: bool = False
    session_version: int = 0
    budget_version: int = 0
    permit_expires_at: datetime | None = None
    checkpoint_sequence: int = 0
    budget_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finish_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    billing_task: asyncio.Task[None] | None = None
    cursor: Any | None = None


@dataclass
class RetainedSession:
    snapshot: RuntimeSnapshot
    runtime: FlowRuntime
    playback_verifier: Any | None = None
    usage_meter: VerifiedPlaybackUsageMeter | None = None
    current_pending: PendingSession | None = None


UsageMeterFactory = Callable[[str, Any | None], VerifiedPlaybackUsageMeter]


class SessionTaskManager:
    """Coordinate one browser peer with a verified immutable platform snapshot."""

    def __init__(
        self,
        *,
        platform: PlatformRuntimeClient,
        base_settings: Settings,
        event_store: DurablePostgresFlowStore | None = None,
        usage_meter_factory: UsageMeterFactory | None = None,
        installed_adapter_report_hash: str | None = None,
    ) -> None:
        self._platform = platform
        self._budget = RuntimeBudgetClient(platform)
        self._base_settings = base_settings.model_copy(deep=True)
        self._event_store = event_store
        self._usage_meter_factory = usage_meter_factory
        self._installed_adapter_report_hash = installed_adapter_report_hash
        self._registrations: dict[UUID, SessionRegistration] = {}
        self._sessions: dict[UUID, RetainedSession] = {}
        self._pending: dict[UUID, PendingSession] = {}
        self._pc_bindings: dict[str, tuple[UUID, UUID, str]] = {}
        self._admission_locks: dict[UUID, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()
        self._billing_slice_ms_raw = base_settings.platform_billing_slice_ms
        self._permit_ttl_ms_raw = base_settings.platform_billing_permit_ttl_ms

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        event_store: DurablePostgresFlowStore | None = None,
        usage_meter_factory: UsageMeterFactory | None = None,
        installed_adapter_report_hash: str | None = None,
    ) -> "SessionTaskManager":
        return cls(
            platform=PlatformRuntimeClient.from_settings(settings),
            base_settings=settings,
            event_store=event_store,
            usage_meter_factory=usage_meter_factory,
            installed_adapter_report_hash=installed_adapter_report_hash,
        )

    def authenticate_internal(self, authorization: str | None, runner_id: str | None) -> bool:
        expected_runner = self._platform.credentials.runner_id
        expected_authorization = f"Bearer {self._platform.credentials.service_token}"
        return bool(
            isinstance(authorization, str)
            and isinstance(runner_id, str)
            and hmac.compare_digest(authorization, expected_authorization)
            and hmac.compare_digest(runner_id, expected_runner)
        )

    async def register(self, request: RuntimeRegistrationRequest) -> RuntimeReservation:
        if request.runner_id != self._platform.credentials.runner_id:
            raise SessionAdmissionError("Registration targets a different runner", status_code=403)
        if request.connect_deadline.tzinfo is None or request.hard_end_deadline.tzinfo is None:
            raise SessionAdmissionError("Registration deadlines must be timezone-aware", status_code=400)
        if request.connect_deadline >= request.hard_end_deadline:
            raise SessionAdmissionError("Registration deadlines are inconsistent", status_code=400)
        async with self._registry_lock:
            existing = self._registrations.get(request.session_id)
            if existing is not None:
                if existing.request != request or existing.released:
                    raise SessionAdmissionError("Session registration conflicts with an existing reservation", status_code=409)
                return RuntimeReservation(
                    session_id=request.session_id,
                    runner_id=request.runner_id,
                    registration_id=request.registration_id,
                    expires_at=existing.expires_at,
                )
            if any(
                item.request.registration_id == request.registration_id
                for item in self._registrations.values()
            ):
                raise SessionAdmissionError("Registration ID is already bound to another session", status_code=409)
            now = datetime.now(timezone.utc)
            if request.connect_deadline <= now or request.hard_end_deadline <= now:
                raise SessionAdmissionError("Registration deadline has passed", status_code=409)
            expires_at = request.connect_deadline
            self._registrations[request.session_id] = SessionRegistration(
                request=request.model_copy(deep=True),
                expires_at=expires_at,
            )
        return RuntimeReservation(
            session_id=request.session_id,
            runner_id=request.runner_id,
            registration_id=request.registration_id,
            expires_at=expires_at,
        )

    async def prepare_offer(self, request_data: object, *, origin: str | None) -> PendingSession:
        session_id, join_token = _parse_join_request(request_data)
        credentials = self._platform.credentials
        if origin is None or origin != credentials.web_origin:
            raise SessionAdmissionError("Browser Origin does not match the registered session origin", status_code=403)
        lock = await self._lock_for(session_id)
        async with lock:
            if session_id in self._pending:
                raise SessionAdmissionError("A browser peer is already being admitted", status_code=409)
            self._require_session_prerequisites()
            registration = self._registrations.get(session_id)
            if registration is None or registration.released:
                raise SessionAdmissionError("Session is not registered on this runner", status_code=404)
            if registration.request.runner_id != credentials.runner_id:
                raise SessionAdmissionError("Session registration belongs to a different runner", status_code=403)

            try:
                view = await self._platform.runtime_config(str(session_id))
                snapshot = validate_runtime_snapshot(
                    view,
                    expected_session_id=session_id,
                    expected_runner_id=credentials.runner_id,
                )
            except RuntimeSnapshotError as exc:
                raise SessionAdmissionError(str(exc), status_code=409) from exc
            except RuntimePlatformError as exc:
                status = exc.status_code if exc.status_code in {404, 409} else 503
                raise SessionAdmissionError(str(exc), status_code=status) from exc

            self._validate_registration_snapshot(registration, snapshot)
            if snapshot.origin != credentials.web_origin:
                raise SessionAdmissionError("Session snapshot origin does not match the configured browser origin", status_code=403)
            if snapshot.capabilities.report_hash != self._installed_adapter_report_hash:
                raise SessionAdmissionError("Installed adapter does not match the pinned qualification report", status_code=503)

            reconnect = snapshot.state == "RECONNECTING"
            retained = self._sessions.get(session_id)
            if reconnect and retained is None:
                state = await self._load_durable_state(snapshot)
                if state is None:
                    raise SessionAdmissionError("Reconnect has no durable prior flow state", status_code=409)
            elif not reconnect and retained is not None:
                raise SessionAdmissionError("A prior runtime already owns this registered session", status_code=409)
            else:
                state = None

            try:
                playback_module = importlib.import_module("bot.flow.playback_receipts")
                if not callable(getattr(playback_module, "PlaybackReceiptVerifier", None)):
                    raise RuntimeError("Playback receipt verifier is unavailable")
                if not callable(getattr(playback_module, "SmallWebRTCPlaybackCursor", None)):
                    raise RuntimeError("SmallWebRTC playback cursor is unavailable")
                permit_window = validate_initial_permit(
                    snapshot.budget,
                    session_id=session_id,
                    hard_end_deadline=snapshot.hard_end_deadline,
                )
            except BudgetPrerequisiteError as exc:
                raise SessionAdmissionError(str(exc), status_code=409) from exc
            except Exception as exc:
                raise SessionAdmissionError(
                    "Qualified playback or verified usage prerequisites are unavailable",
                    status_code=503,
                ) from exc

            peer_id = uuid4()
            gate = asyncio.Event()
            join_attempted = False
            try:
                verifier = playback_module.PlaybackReceiptVerifier(
                    session_id=str(session_id),
                    transport_epoch=str(peer_id),
                    active_peer_id=str(peer_id),
                    capability_report_hash=snapshot.capabilities.report_hash,
                )
                usage_meter = self._usage_meter_factory(str(session_id), verifier)  # type: ignore[misc]
                if inspect.isawaitable(usage_meter):
                    usage_meter = await usage_meter
                if (
                    getattr(usage_meter, "receipt_source", None) != "committed_playback_receipts"
                    or getattr(usage_meter, "session_id", None) != str(session_id)
                    or getattr(usage_meter, "receipt_verifier", None) is not verifier
                    or not callable(getattr(usage_meter, "cumulative_billable_ms", None))
                ):
                    raise SessionAdmissionError(
                        "Usage meter is not bound to committed playback receipts",
                        status_code=503,
                    )
                if retained is not None:
                    if (
                        retained.snapshot.config_version_id != snapshot.config_version_id
                        or retained.snapshot.hash_value("flow_hash") != snapshot.hash_value("flow_hash")
                    ):
                        raise SessionAdmissionError(
                            "Reconnect snapshot does not match the retained flow version",
                            status_code=409,
                        )
                    runtime = retained.runtime
                else:
                    transport_epoch = str(peer_id)
                    runtime_state = state
                    if state is not None:
                        if not state.transport_epoch:
                            raise SessionAdmissionError(
                                "Durable reconnect state has no prior transport epoch",
                                status_code=409,
                            )
                        runtime_state = state.model_copy(
                            update={"transport_epoch": transport_epoch},
                            deep=True,
                        )
                    runtime = FlowRuntime(
                        snapshot.compiled,
                        str(session_id),
                        transport_epoch,
                        state=runtime_state,
                        event_store=self._event_store,
                        transport_event_verifier=verifier.verify_event,
                        session_managed=True,
                        speech_authorization_gate=gate,
                        playback_receipt_verifier=verifier,
                    )
                join_attempted = True
                join = await self._platform.consume_join(
                    str(session_id),
                    JoinConsumptionRequest(
                        join_token=join_token,
                        peer_id=peer_id,
                        origin=origin,
                    ),
                )
                if (
                    join.session_id != session_id
                    or join.peer_id != peer_id
                    or join.runner_id != credentials.runner_id
                    or join.reconnect != reconnect
                    or (reconnect and join.state != "RECONNECTING")
                    or (not reconnect and join.state != "CONNECTING")
                ):
                    raise SessionAdmissionError(
                        "Platform join consumption returned a mismatched peer binding",
                        status_code=502,
                    )
                if retained is not None:
                    await runtime.rebind_transport_epoch(
                        str(peer_id),
                        gate,
                        playback_receipt_verifier=verifier,
                    )
            except SessionAdmissionError:
                if join_attempted:
                    await self._release_consumed_peer(session_id, peer_id)
                raise
            except RuntimePlatformError as exc:
                if join_attempted:
                    await self._release_consumed_peer(session_id, peer_id)
                status = exc.status_code if exc.status_code in {403, 404, 409} else 503
                raise SessionAdmissionError(
                    "Platform rejected or could not confirm this browser join",
                    status_code=status,
                ) from exc
            except Exception as exc:
                if join_attempted:
                    await self._release_consumed_peer(session_id, peer_id)
                raise SessionAdmissionError(
                    "Session admission failed before speech",
                    status_code=503,
                ) from exc

            pending = PendingSession(
                snapshot=snapshot,
                join=join,
                peer_id=peer_id,
                token_digest=hashlib.sha256(join_token.encode("utf-8")).hexdigest(),
                origin=origin,
                runtime=runtime,
                playback_verifier=verifier,
                usage_meter=usage_meter,
                speech_gate=gate,
                session_version=join.session_version,
                budget_version=permit_window.budget_version,
                permit_expires_at=permit_window.expires_at,
            )

            self._pending[session_id] = pending
            if retained is None:
                self._sessions[session_id] = RetainedSession(
                    snapshot=snapshot,
                    runtime=runtime,
                    playback_verifier=verifier,
                    usage_meter=usage_meter,
                    current_pending=pending,
                )
            else:
                retained.snapshot = snapshot
                retained.playback_verifier = verifier
                retained.usage_meter = usage_meter
                retained.current_pending = pending
            return pending

    async def claim_pipeline(self, body: object) -> PendingSession:
        session_id, join_token = _parse_join_request(body)
        pending = self._pending.get(session_id)
        if pending is None or pending.claimed or pending.platform_release or pending.pipeline_done.is_set():
            raise SessionAdmissionError("No unclaimed platform admission exists for this pipeline", status_code=403)
        if not hmac.compare_digest(
            hashlib.sha256(join_token.encode("utf-8")).hexdigest(),
            pending.token_digest,
        ):
            raise SessionAdmissionError("Pipeline request does not match the admitted browser join", status_code=403)
        pending.claimed = True
        return pending

    async def begin_pipeline(self, pending: PendingSession) -> Settings:
        if pending.platform_release or pending.pipeline_done.is_set():
            raise SessionAdmissionError("Platform session release has already started", status_code=409)
        if pending.join.state == "CONNECTING":
            state = await self._transition(pending, "INITIALIZING")
            if state.state != "INITIALIZING":
                raise SessionAdmissionError("Platform did not enter INITIALIZING", status_code=409)
        return pending.snapshot.settings_for(self._base_settings)

    async def attach_transport(self, pending: PendingSession, transport: Any, connection: Any) -> None:
        if pending.platform_release or pending.pipeline_done.is_set():
            raise SessionAdmissionError("Platform session release has already started", status_code=409)
        try:
            playback_module = importlib.import_module("bot.flow.playback_receipts")
            cursor = playback_module.SmallWebRTCPlaybackCursor.install(transport)
            output = transport.output()
            send_message = getattr(output, "send_message", None)
            if not callable(send_message):
                raise RuntimeError("SmallWebRTC transport has no app-message output")
            from pipecat.frames.frames import OutputTransportMessageUrgentFrame

            async def send_marker(message: dict[str, Any]) -> None:
                await send_message(OutputTransportMessageUrgentFrame(message=message))

            pending.runtime.configure_playback(
                cursor=cursor,
                verifier=pending.playback_verifier,
                marker_sender=send_marker,
            )

            @transport.event_handler("on_app_message")
            async def on_session_app_message(_transport, message, sender):
                await pending.runtime.accept_playback_receipt(
                    message,
                    sender_peer_id=str(sender),
                )

            pending.cursor = cursor
            pending.connection = connection
        except Exception as exc:
            pending.runtime.deny_speech()
            raise SessionAdmissionError(
                "Qualified SmallWebRTC playback could not be installed",
                status_code=503,
            ) from exc
    async def wait_until_active(self, pending: PendingSession) -> None:
        try:
            await pending.runtime.wait_for_pipeline_start()
            connect_deadline = min(
                pending.snapshot.hard_end_deadline,
                pending.permit_expires_at or pending.snapshot.hard_end_deadline,
            )
            connect_timeout = max(
                0.0,
                (connect_deadline - datetime.now(timezone.utc)).total_seconds(),
            )
            await asyncio.wait_for(pending.pc_bound.wait(), timeout=connect_timeout)
            await self._wait_for_real_connection(pending)
            if pending.join.state == "CONNECTING":
                state = await self._transition(pending, "READY")
                if state.state != "READY":
                    raise SessionAdmissionError("Platform did not enter READY", status_code=409)
            state = await self._transition(pending, "ACTIVE")
            if state.state != "ACTIVE":
                raise SessionAdmissionError("Platform did not enter ACTIVE", status_code=409)
            if pending.platform_release:
                raise SessionAdmissionError("Platform session release interrupted activation", status_code=409)
            connected = pending.connection.is_connected()
            if inspect.isawaitable(connected):
                connected = await connected
            if connected is not True:
                raise SessionAdmissionError("SmallWebRTC connection closed before activation", status_code=409)
            pending.session_version = state.session_version
            pending.runtime.authorize_speech()
            pending.billing_task = asyncio.create_task(self._renew_budget(pending))
        except asyncio.CancelledError:
            pending.runtime.deny_speech()
            raise
        except Exception:
            pending.runtime.deny_speech()
            if pending.connection is not None:
                await _close_connection(pending.connection)
            raise


    async def bind_peer_pc(self, session_id: UUID, pc_id: str) -> None:
        pc_id = pc_id.strip()
        if not pc_id or len(pc_id) > 256:
            raise SessionAdmissionError("SmallWebRTC returned an invalid pc_id", status_code=502)
        pending = self._pending.get(session_id)
        if pending is None:
            raise SessionAdmissionError("Offer response has no pending session admission", status_code=409)
        existing = self._pc_bindings.get(pc_id)
        if existing is not None and existing != (session_id, pending.peer_id, pending.origin):
            raise SessionAdmissionError("SmallWebRTC pc_id is already bound to another session", status_code=409)
        pending.pc_id = pc_id
        self._pc_bindings[pc_id] = (session_id, pending.peer_id, pending.origin)
        pending.pc_bound.set()

    def authorize_patch(self, pc_id: str | None, origin: str | None) -> bool:
        if not pc_id or origin != self._platform.credentials.web_origin:
            return False
        binding = self._pc_bindings.get(pc_id)
        if binding is None:
            return False
        session_id, peer_id, bound_origin = binding
        pending = self._pending.get(session_id)
        return bool(
            pending
            and pending.peer_id == peer_id
            and pending.origin == bound_origin == origin
        )

    async def finish_pipeline(self, pending: PendingSession, reason: str) -> None:
        async with pending.finish_lock:
            if pending.pipeline_done.is_set():
                return
            pending.runtime.deny_speech()
            try:
                await pending.runtime.pipeline_ended(reason[:64] or "pipeline_ended")
                if pending.cursor is not None:
                    close = getattr(pending.cursor, "close", None)
                    if not callable(close):
                        raise SessionAdmissionError("Playback cursor cannot be closed safely", status_code=503)
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                if pending.connection is not None:
                    pending.media_stopped = await _close_connection(pending.connection)
                    if not pending.media_stopped:
                        raise SessionAdmissionError("SmallWebRTC media did not stop", status_code=503)
                else:
                    pending.media_stopped = not pending.claimed
                if pending.billing_task is not None:
                    pending.billing_task.cancel()
                    try:
                        await pending.billing_task
                    except asyncio.CancelledError:
                        pass
                if not pending.platform_release:
                    try:
                        await self._platform.release_peer(
                            str(pending.snapshot.session_id),
                            PeerReleaseRequest(
                                peer_id=pending.peer_id,
                                reason=reason[:64] or "peer_closed",
                            ),
                        )
                    except RuntimePlatformError:
                        # The platform keeps the peer for its reconciliation path.
                        pass
            finally:
                if pending.pc_id is not None:
                    self._pc_bindings.pop(pending.pc_id, None)
                self._pending.pop(pending.snapshot.session_id, None)
                retained = self._sessions.get(pending.snapshot.session_id)
                if retained is not None:
                    retained.current_pending = None
                pending.pipeline_done.set()

    async def release_registration(self, request: RuntimeReleaseRequest) -> RuntimeReleaseAcknowledgement:
        lock = await self._lock_for(request.session_id)
        async with lock:
            registration = self._registrations.get(request.session_id)
            if (
                registration is not None
                and registration.released
                and registration.released_version == request.session_version
                and registration.request.runner_id == request.runner_id
                and registration.request.registration_id == request.registration_id
            ):
                return _release_ack(request)
            if (
                registration is None
                or registration.released
                or registration.request.runner_id != request.runner_id
                or registration.request.registration_id != request.registration_id
                or request.runner_id != self._platform.credentials.runner_id
            ):
                raise SessionAdmissionError("Release does not match a live runner registration", status_code=409)
            view = await self._get_ending_view(request)
            pending = self._pending.get(request.session_id)
            retained = self._sessions.get(request.session_id)
            if pending is not None:
                pending.platform_release = True
                pending.runtime.deny_speech()
                await pending.runtime.interrupt()
                if pending.connection is not None:
                    pending.media_stopped = await _close_connection(pending.connection)
                    if not pending.media_stopped:
                        raise SessionAdmissionError("SmallWebRTC media did not stop", status_code=503)
                if pending.claimed:
                    try:
                        await asyncio.wait_for(
                            pending.pipeline_done.wait(),
                            timeout=self._platform.timeout_seconds,
                        )
                    except TimeoutError as exc:
                        raise SessionAdmissionError(
                            "Voice pipeline did not stop after the platform release",
                            status_code=503,
                        ) from exc
                else:
                    await self.finish_pipeline(pending, "platform_release")
                if not pending.media_stopped:
                    raise SessionAdmissionError("Media stop could not be verified", status_code=503)

            final_view = await self._get_ending_view(request)
            meter = retained.usage_meter if retained is not None else None
            if meter is None and callable(self._usage_meter_factory):
                meter = self._usage_meter_factory(str(request.session_id), None)
                if inspect.isawaitable(meter):
                    meter = await meter
            if (
                meter is None
                or getattr(meter, "session_id", None) != str(request.session_id)
                or getattr(meter, "receipt_source", None) != "committed_playback_receipts"
            ):
                raise SessionAdmissionError(
                    "Verified playback usage meter is unavailable at release",
                    status_code=503,
                )
            store = self._event_store
            if store is None:
                raise SessionAdmissionError("Durable stop-record storage is unavailable", status_code=503)
            try:
                record = await store.load_stop_record(str(request.session_id), request.session_version)
                if record is None:
                    usage_ms = await _verified_usage(meter, request.session_id)
                    attestation = RuntimeStopAttestation(
                        source="voice-runtime",
                        stop_id=uuid4(),
                        session_id=request.session_id,
                        runner_id=request.runner_id,
                        registration_id=request.registration_id,
                        session_version=request.session_version,
                        stopped_at=datetime.now(timezone.utc),
                        reason="platform_release",
                    )
                    record = RuntimeStopRecord(
                        attestation=attestation,
                        expected_budget_version=final_view.budget.budget_version,
                        cumulative_billable_ms=usage_ms,
                    )
                    await store.save_stop_record(
                        str(request.session_id),
                        request.session_version,
                        record,
                    )
                _validate_stop_record(record, request)
                await self._budget.close(
                    session_id=request.session_id,
                    operation_id=f"voice:{request.session_id}:close:{request.session_version}",
                    expected_budget_version=record.expected_budget_version,
                    cumulative_billable_ms=record.cumulative_billable_ms,
                    stop_attestation=record.attestation,
                )
            except Exception as exc:
                raise SessionAdmissionError(
                    "Budget could not be verified and closed after media stop",
                    status_code=503,
                ) from exc

            async with self._registry_lock:
                self._registrations[request.session_id] = SessionRegistration(
                    request=registration.request,
                    expires_at=registration.expires_at,
                    released=True,
                    released_version=request.session_version,
                    stop_record=record,
                )
            self._sessions.pop(request.session_id, None)
            return _release_ack(request)

    async def _get_ending_view(self, request: RuntimeReleaseRequest):
        try:
            view = await self._platform.runtime_config(str(request.session_id))
        except RuntimePlatformError as exc:
            raise SessionAdmissionError(
                "Platform session state could not be verified before release",
                status_code=503,
            ) from exc
        if (
            view.registration_id != request.registration_id
            or view.runner_id != request.runner_id
            or view.session_version != request.session_version
            or view.state != "ENDING"
        ):
            raise SessionAdmissionError(
                "Release does not match the exact ENDING session version",
                status_code=409,
            )
        return view

    async def abort_offer(self, session_id: UUID, *, reason: str) -> None:
        pending = self._pending.get(session_id)
        if pending is None:
            return
        pending.runtime.deny_speech()
        if pending.connection is not None:
            pending.media_stopped = await _close_connection(pending.connection)
        if pending.claimed:
            try:
                await asyncio.wait_for(
                    pending.pipeline_done.wait(),
                    timeout=self._platform.timeout_seconds,
                )
            except TimeoutError as exc:
                raise SessionAdmissionError("Failed offer pipeline did not stop", status_code=503) from exc
        else:
            await self.finish_pipeline(pending, reason)
    async def _renew_budget(self, pending: PendingSession) -> None:
        slice_ms = _positive_env(self._billing_slice_ms_raw, "PLATFORM_BILLING_SLICE_MS")
        ttl_ms = _positive_env(self._permit_ttl_ms_raw, "PLATFORM_BILLING_PERMIT_TTL_MS")
        permit_expiry = pending.permit_expires_at
        if permit_expiry is None:
            raise BudgetPrerequisiteError("Initial permit expiry is missing")
        expected_version = pending.budget_version
        safety_margin = min(0.25, ttl_ms / 4000)
        while not pending.pipeline_done.is_set():
            renew_at = permit_expiry.timestamp() - (ttl_ms / 2000)
            delay = max(0.0, renew_at - time.time())
            try:
                await asyncio.wait_for(pending.pipeline_done.wait(), timeout=delay)
                return
            except TimeoutError:
                pass

            operation_id = f"voice:{pending.snapshot.session_id}:checkpoint:{pending.checkpoint_sequence}"
            async with pending.budget_lock:
                if pending.pipeline_done.is_set():
                    return
                try:
                    usage_ms = await _verified_usage(
                        pending.usage_meter,
                        pending.snapshot.session_id,
                    )
                except Exception:
                    await self._halt_for_budget(pending)
                    return

                while not pending.pipeline_done.is_set():
                    remaining = permit_expiry.timestamp() - safety_margin - time.time()
                    if remaining <= 0:
                        break
                    try:
                        permit = await self._budget.checkpoint(
                            session_id=pending.snapshot.session_id,
                            operation_id=operation_id,
                            expected_budget_version=expected_version,
                            cumulative_billable_ms=usage_ms,
                            requested_future_ms=slice_ms,
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        retry_delay = min(0.25, max(0.01, remaining / 4))
                        try:
                            await asyncio.wait_for(
                                pending.pipeline_done.wait(),
                                timeout=retry_delay,
                            )
                            return
                        except TimeoutError:
                            continue
                else:
                    return

                if remaining <= 0:
                    await self._halt_for_budget(pending)
                    return
                expected_version = permit.budget_version
                pending.budget_version = permit.budget_version
                pending.checkpoint_sequence += 1
                permit_expiry = permit.expires_at
                pending.permit_expires_at = permit.expires_at
                if permit.funds_exhausted or permit.authorized_through_ms <= permit.proven_ms:
                    await self._halt_for_budget(pending)
                    return

    async def _halt_for_budget(self, pending: PendingSession) -> None:
        pending.runtime.deny_speech()
        try:
            await pending.runtime.interrupt()
        finally:
            if pending.connection is not None:
                pending.media_stopped = await _close_connection(pending.connection)

    async def _transition(self, pending: PendingSession, target: str) -> SessionStateView:
        result = await self._platform.transition(
            str(pending.snapshot.session_id),
            SessionTransitionRequest(
                registration_id=pending.snapshot.registration_id,
                expected_version=pending.session_version,
                target_state=target,
            ),
        )
        if result.session_id != pending.snapshot.session_id or result.session_version <= pending.session_version:
            raise SessionAdmissionError("Platform returned a stale session transition", status_code=409)
        pending.session_version = result.session_version
        return result

    async def _wait_for_real_connection(self, pending: PendingSession) -> None:
        if pending.connection is None:
            raise SessionAdmissionError("SmallWebRTC connection was not attached", status_code=409)
        now = datetime.now(timezone.utc)
        deadline = min(
            pending.snapshot.hard_end_deadline,
            pending.permit_expires_at or pending.snapshot.hard_end_deadline,
        )
        timeout = (deadline - now).total_seconds()
        if timeout <= 0:
            raise SessionAdmissionError("Session permit expired before the media connection became ready", status_code=409)
        loop_deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < loop_deadline:
            connected = pending.connection.is_connected()
            if inspect.isawaitable(connected):
                connected = await connected
            if connected is True:
                return
            await asyncio.sleep(min(0.05, max(0.0, loop_deadline - asyncio.get_running_loop().time())))
        raise SessionAdmissionError("SmallWebRTC peer did not reach connected state before the permit deadline", status_code=409)

    async def _load_durable_state(self, snapshot: RuntimeSnapshot) -> SessionState | None:
        store = self._event_store
        if store is None:
            raise SessionAdmissionError("Durable PostgreSQL flow store is unavailable", status_code=503)
        state = await store.load_state(str(snapshot.session_id), snapshot.hash_value("flow_hash"))
        if state is not None and (
            state.session_id != str(snapshot.session_id)
            or state.flow_hash != snapshot.hash_value("flow_hash")
            or state.artifact_hash != snapshot.hash_value("artifact_hash")
        ):
            raise SessionAdmissionError("Durable flow state does not match the immutable session version", status_code=409)
        return state

    def _require_session_prerequisites(self) -> None:
        store = self._event_store
        if (
            store is None
            or getattr(store, "storage_backend", None) != "postgresql"
            or not callable(getattr(store, "commit", None))
            or not callable(getattr(store, "load_state", None))
            or not callable(getattr(store, "save_stop_record", None))
            or not callable(getattr(store, "load_stop_record", None))
        ):
            raise SessionAdmissionError("A durable PostgreSQL flow event store is required before admission", status_code=503)
        if not callable(self._usage_meter_factory):
            raise SessionAdmissionError("A verified playback receipt usage meter is required before admission", status_code=503)
        if not self._installed_adapter_report_hash:
            raise SessionAdmissionError("The installed adapter qualification report is unavailable", status_code=503)
        _positive_env(self._billing_slice_ms_raw, "PLATFORM_BILLING_SLICE_MS")
        _positive_env(self._permit_ttl_ms_raw, "PLATFORM_BILLING_PERMIT_TTL_MS")

    def _validate_registration_snapshot(
        self,
        registration: SessionRegistration,
        snapshot: RuntimeSnapshot,
    ) -> None:
        request = registration.request
        if (
            snapshot.registration_id != request.registration_id
            or snapshot.config_version_id != request.config_version_id
            or snapshot.runner_id != request.runner_id
            or snapshot.connect_deadline != request.connect_deadline
            or snapshot.hard_end_deadline != request.hard_end_deadline
            or snapshot.session_version < request.session_version
        ):
            raise SessionAdmissionError("Runtime snapshot does not match its runner registration", status_code=409)

    async def _lock_for(self, session_id: UUID) -> asyncio.Lock:
        async with self._registry_lock:
            return self._admission_locks.setdefault(session_id, asyncio.Lock())

    async def _release_consumed_peer(
        self,
        session_id: UUID,
        peer_id: UUID,
        *,
        reason: str = "admission_failed",
    ) -> None:
        try:
            await self._platform.release_peer(
                str(session_id),
                PeerReleaseRequest(peer_id=peer_id, reason=reason[:64]),
            )
        except RuntimePlatformError:
            pass


def _parse_join_request(request_data: object) -> tuple[UUID, str]:
    if not isinstance(request_data, dict) or set(request_data) != {"session_id", "join_token"}:
        raise SessionAdmissionError("Session offer requires exactly session_id and join_token", status_code=400)
    session_id_raw = request_data.get("session_id")
    join_token = request_data.get("join_token")
    if not isinstance(session_id_raw, str) or not isinstance(join_token, str) or not join_token or len(join_token) > 8192:
        raise SessionAdmissionError("Session offer credentials are malformed", status_code=400)
    try:
        session_id = UUID(session_id_raw)
    except ValueError as exc:
        raise SessionAdmissionError("Session ID is invalid", status_code=400) from exc
    return session_id, join_token


def _positive_env(value: str | None, name: str) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        raise SessionAdmissionError(f"{name} must be explicitly configured as a positive integer", status_code=503)
    parsed = int(value)
    if parsed <= 0:
        raise SessionAdmissionError(f"{name} must be explicitly configured as a positive integer", status_code=503)
    return parsed


async def _verified_usage(meter: VerifiedPlaybackUsageMeter, session_id: UUID) -> int:
    if (
        getattr(meter, "session_id", None) != str(session_id)
        or getattr(meter, "receipt_source", None) != "committed_playback_receipts"
    ):
        raise BudgetPrerequisiteError("Billable usage is not sourced from this session's committed receipts")
    value = meter.cumulative_billable_ms()
    if inspect.isawaitable(value):
        value = await value
    if type(value) is not int or value < 0:
        raise BudgetPrerequisiteError("Verified playback meter returned an invalid cumulative duration")
    return value


def _validate_stop_record(record: RuntimeStopRecord, request: RuntimeReleaseRequest) -> None:
    attestation = record.attestation
    if (
        type(record.expected_budget_version) is not int
        or record.expected_budget_version < 1
        or type(record.cumulative_billable_ms) is not int
        or record.cumulative_billable_ms < 0
        or attestation.source != "voice-runtime"
        or attestation.session_id != request.session_id
        or attestation.runner_id != request.runner_id
        or attestation.registration_id != request.registration_id
        or attestation.session_version != request.session_version
        or attestation.reason != "platform_release"
        or attestation.stopped_at.tzinfo is None
    ):
        raise BudgetPrerequisiteError("Durable stop record does not match this platform release")


def _release_ack(request: RuntimeReleaseRequest) -> RuntimeReleaseAcknowledgement:
    return RuntimeReleaseAcknowledgement(
        session_id=request.session_id,
        runner_id=request.runner_id,
        registration_id=request.registration_id,
        session_version=request.session_version,
        released=True,
    )


async def _close_connection(connection: Any) -> bool:
    is_connected = getattr(connection, "is_connected", None)
    if callable(is_connected):
        connected = is_connected()
        if inspect.isawaitable(connected):
            connected = await connected
        if connected is False:
            return True
    for target, method_name in (
        (connection, "disconnect"),
        (connection, "close"),
        (getattr(connection, "pc", None), "close"),
    ):
        close = getattr(target, method_name, None)
        if not callable(close):
            continue
        result = close()
        if inspect.isawaitable(result):
            await result
        if callable(is_connected):
            connected = is_connected()
            if inspect.isawaitable(connected):
                connected = await connected
            return connected is False
        return True
    return False

__all__ = [
    "DurablePostgresFlowStore",
    "PendingSession",
    "SessionAdmissionError",
    "RuntimeStopRecord",
    "SessionTaskManager",
    "VerifiedPlaybackUsageMeter",
]
