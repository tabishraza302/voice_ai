"""Load the operator-pinned adapter qualification report."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bot.flow.qualification import CapabilityReport, validate_capability_report


@dataclass(frozen=True)
class DeploymentIdentity:
    """Trusted deployment identity supplied separately from the qualification report."""

    adapter_id: str
    adapter_version: str
    model_revision: str
    profile_hash: str
    source_revision: str

    @classmethod
    def from_settings(cls, settings: Any) -> DeploymentIdentity | None:
        values = (
            getattr(settings, "deployment_adapter_id", None),
            getattr(settings, "deployment_adapter_version", None),
            getattr(settings, "deployment_model_revision", None),
            getattr(settings, "deployment_profile_hash", None),
            getattr(settings, "deployment_source_revision", None),
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            return None
        return cls(*(value.strip() for value in values))

    def matches_report(self, report: CapabilityReport) -> bool:
        return (
            self.adapter_id == report.adapter_id
            and self.adapter_version == report.adapter_version
            and self.model_revision == report.model_revision
            and self.profile_hash == report.profile_hash
            and self.source_revision == report.source_revision
        )


def load_deployment_capability_report(
    report_path: str | None,
    expected_report_hash: str | None,
) -> CapabilityReport | None:
    """Load only the report whose canonical hash is pinned by deployment config."""
    if report_path is None and expected_report_hash is None:
        return None
    if not report_path or not expected_report_hash:
        raise ValueError(
            "Adapter qualification report path and expected hash must be configured together"
        )
    if len(expected_report_hash) != 64 or any(
        char not in "0123456789abcdef" for char in expected_report_hash
    ):
        raise ValueError("Expected adapter qualification report hash is invalid")

    try:
        report_data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Configured adapter qualification report cannot be read") from None

    if not isinstance(report_data, dict):
        raise ValueError("Configured adapter qualification report must be a JSON object")
    if "protocol_version" not in report_data or "report_hash" not in report_data:
        raise ValueError("Configured adapter qualification report is incomplete")
    if (
        type(report_data["protocol_version"]) is not int
        or report_data["protocol_version"] != 2
    ):
        raise ValueError(
            "Configured adapter qualification report has an invalid protocol version"
        )

    try:
        report = CapabilityReport.model_validate(report_data)
    except ValidationError:
        raise ValueError("Configured adapter qualification report is invalid") from None

    if report.model_dump(mode="json") != report_data:
        raise ValueError("Configured adapter qualification report is not canonical")

    diagnostics = validate_capability_report(report_data)
    if diagnostics:
        raise ValueError("Configured adapter qualification report failed validation")
    if report.report_hash != expected_report_hash:
        raise ValueError(
            "Configured adapter qualification report does not match the configured report hash"
        )
    return report
