"""Platform database models registry.

Imports all platform model declarations onto Base.metadata.
Completely isolated from bot.db.
"""

from platform_api.db.base import Base
from platform_api.db.schema import SchemaInfo
from platform_api.models.admission_pause import AdmissionPause
from platform_api.models.tariff import ActiveSessionTariff, SessionTariffRecord
from platform_api.models.transcript_policy import TranscriptCorrection, TranscriptPolicy
from platform_api.models.wallet import WalletAccount
from platform_api.models.auth import (
    AuditLog,
    AuthMail,
    AuthMfa,
    AuthRateLimit,
    AuthSession,
    AuthToken,
    User,
)
from platform_api.models.session import Session
from platform_api.sessions.models import (
    RunnerCapacity,
    RuntimeRegistration,
    SessionJoinJti,
    SessionParticipant,
    SessionPeer,
    SessionSlot,
)
from platform_api.billing.models import (
    BillingOperation,
    CallBudget,
    MeterCheckpoint,
    WalletEntry,
)
from platform_api.agents.models import (
    Agent,
    AgentDraft,
    AgentTemplateVersion,
    AgentVersion,
    IdempotencyRecord,
    PlatformPromptVersion,
)
from platform_api.events.models import (
    EventQuarantine,
    ProcessedEvent,
    ProviderUsage,
    TranscriptGap,
    TranscriptSegment,
    TranscriptState,
)
from platform_api.generation.models import GenerationJob
from platform_api.testing.models import ChatMessage, ChatTestSession
from platform_api.public.models import PublicDeployment, PublicGuestCapability
from platform_api.qa.models import FlowQAJob

__all__ = [
    "Base",
    "SchemaInfo",
    "User",
    "AuthSession",
    "AuthToken",
    "AuthMfa",
    "AuthRateLimit",
    "AuthMail",
    "AuditLog",
    "AdmissionPause",
    "ActiveSessionTariff",
    "SessionTariffRecord",
    "TranscriptPolicy",
    "TranscriptCorrection",
    "WalletAccount",
    "WalletEntry",
    "CallBudget",
    "BillingOperation",
    "MeterCheckpoint",
    "RunnerCapacity",
    "Session",
    "RuntimeRegistration",
    "SessionParticipant",
    "SessionJoinJti",
    "SessionPeer",
    "SessionSlot",
    "Agent",
    "AgentDraft",
    "AgentVersion",
    "AgentTemplateVersion",
    "PlatformPromptVersion",
    "IdempotencyRecord",
    "ProcessedEvent",
    "TranscriptSegment",
    "ProviderUsage",
    "TranscriptState",
    "TranscriptGap",
    "EventQuarantine",
    "GenerationJob",
    "ChatTestSession",
    "ChatMessage",
    "PublicDeployment",
    "PublicGuestCapability",
    "FlowQAJob",
]
