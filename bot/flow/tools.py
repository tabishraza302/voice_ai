"""Tool contract types and allowlisted registry interface per flow contract section 10.

- Registry is empty by default; no arbitrary customer code.
- Tools require code-registered pinned contracts, input schemas, and confirmation policies.
- Consequential operations require explicit user confirmation.
- Immutable explicit registry with zero process-global state.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.flow.canonical import canonical_json_v1


class ToolContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_id: str
    version: str
    input_schema: dict[str, Any]
    result_schema: dict[str, Any]
    required_roles: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    field_bindings: dict[str, str] = Field(default_factory=dict)
    confirmation_policy: Literal["explicit_user", "none"] = "explicit_user"
    idempotency_scope: str = "session"
    max_retries: int = 2
    timeout_config_key: str
    resumable_status_lookup: bool = False
    expected_failure_types: list[str] = Field(default_factory=list)
    compensation_procedure: str | None = None

    @property
    def contract_hash(self) -> str:
        data = {
            "compensation_procedure": self.compensation_procedure or "",
            "confirmation_policy": self.confirmation_policy,
            "expected_failure_types": sorted(self.expected_failure_types),
            "field_bindings": dict(sorted(self.field_bindings.items())),
            "idempotency_scope": self.idempotency_scope,
            "input_schema": self.input_schema,
            "max_retries": self.max_retries,
            "required_roles": sorted(self.required_roles),
            "required_scopes": sorted(self.required_scopes),
            "resumable_status_lookup": self.resumable_status_lookup,
            "result_schema": self.result_schema,
            "timeout_config_key": self.timeout_config_key,
            "tool_id": self.tool_id,
            "version": self.version,
        }
        return hashlib.sha256(b"voice-ai/tool-contract/v1\0" + canonical_json_v1(data)).hexdigest()


class ToolRegistry(BaseModel):
    """Immutable allowlisted tool registry per flow contract section 10."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: dict[str, ToolContract] = Field(default_factory=dict)

    def get(self, tool_id: str) -> ToolContract | None:
        return self.tools.get(tool_id)

    def has(self, tool_id: str) -> bool:
        return tool_id in self.tools

    def is_registered(self, tool_id: str) -> bool:
        return tool_id in self.tools

    @classmethod
    def empty(cls) -> ToolRegistry:
        return cls(tools={})

    @classmethod
    def from_contracts(cls, contracts: list[ToolContract]) -> ToolRegistry:
        return cls(tools={c.tool_id: c for c in contracts})

    def compute_tools_hash(self) -> str:
        if not self.tools:
            return "0" * 64
        sorted_keys = sorted(self.tools.keys())
        hashes = [self.tools[k].contract_hash for k in sorted_keys]
        payload = ":".join(hashes).encode("utf-8")
        return hashlib.sha256(b"voice-ai/tools-manifest/v1\0" + payload).hexdigest()
