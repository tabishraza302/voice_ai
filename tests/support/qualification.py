"""Pure verification and qualification policy helpers.

Implements the contract specified in verification-plan.md Section 6.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET


RELEASE_REQUIRED_GATES = (
    "python_unit",
    "bun_test",
    "bun_typecheck",
    "bun_build",
    "postgres_gate",
    "browser_gate",
    "voice_local_offline",
    "voice_deployment",
    "release_evidence",
)
_COUNT_FIELDS = frozenset(
    {"collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TEST_GATES = frozenset(
    {
        "python_unit",
        "bun_test",
        "postgres_gate",
        "browser_gate",
        "voice_local_offline",
        "voice_deployment",
        "release_evidence",
    }
)

_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "run_dir",
        "created_at",
        "commit",
        "python_version",
        "uv_lock_sha256",
        "bun_lock_sha256",
        "pyproject_toml_sha256",
        "required_gates",
        "gates",
        "overall_success",
        "validation_error",
    }
)
_GATE_FIELDS = frozenset({"status", "reason", "counts"})


def require_prerequisite(name: str, *, available: bool) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("required qualification prerequisite name is empty")
    if available is not True:
        raise ValueError(f"required qualification prerequisite unavailable: {name}")


def require_report(path: Path | str, description: str) -> Path:
    report = Path(path)
    if not report.is_file():
        raise FileNotFoundError(f"required {description} missing: {report}")
    if report.stat().st_size == 0:
        raise ValueError(f"required {description} is empty: {report}")
    return report


def validate_release_manifest(
    manifest: Any,
    required_gates: Sequence[str] = RELEASE_REQUIRED_GATES,
) -> bool:
    """Validate the versioned manifest shape and every declared gate outcome."""
    if not isinstance(manifest, Mapping):
        raise ValueError("release manifest must be a JSON object")
    required_fields = _MANIFEST_FIELDS - {"validation_error"}
    if not required_fields.issubset(manifest) or set(manifest) - _MANIFEST_FIELDS:
        raise ValueError("release manifest has missing or unknown fields")
    if type(manifest.get("manifest_version")) is not int or manifest["manifest_version"] != 1:
        raise ValueError("release manifest_version must be integer 1")
    run_dir = manifest.get("run_dir")
    if not isinstance(run_dir, str) or not Path(run_dir).is_absolute():
        raise ValueError("release manifest run_dir must be an absolute path")
    if not isinstance(manifest.get("python_version"), str) or not manifest["python_version"].strip():
        raise ValueError("release manifest python_version is missing")
    for field in ("uv_lock_sha256", "bun_lock_sha256", "pyproject_toml_sha256"):
        value = manifest.get(field)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.lower()):
            raise ValueError(f"release manifest {field} must be a SHA-256 digest")

    commit = manifest.get("commit")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ValueError("release manifest commit must be a full 40- or 64-character commit hash")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise ValueError("release manifest created_at must be an ISO-8601 timestamp")
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as err:
        raise ValueError("release manifest created_at must be an ISO-8601 timestamp") from err
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("release manifest created_at must include a timezone")

    expected = tuple(required_gates)
    if not expected or len(set(expected)) != len(expected):
        raise ValueError("release manifest must name unique required gates")
    if any(name not in RELEASE_REQUIRED_GATES for name in expected):
        raise ValueError("release manifest contains an unknown required gate")
    declared = manifest.get("required_gates")
    if not isinstance(declared, list) or tuple(declared) != expected:
        raise ValueError("release manifest required_gates do not match the verifier gate contract")

    gates = manifest.get("gates")
    if not isinstance(gates, Mapping):
        raise ValueError("release manifest gates must be an object")
    allowed_names = set(expected) | {"cleanup"}
    unknown = set(gates) - allowed_names
    if unknown:
        raise ValueError(f"release manifest contains an unknown gate: {', '.join(sorted(unknown))}")
    missing = set(expected) - set(gates)
    if missing:
        raise ValueError(f"release manifest missing required gate: {', '.join(sorted(missing))}")
    outcomes: dict[str, str] = {}
    for gate_name, gate in gates.items():
        if not isinstance(gate, Mapping) or set(gate) - _GATE_FIELDS:
            raise ValueError(f"release manifest gate {gate_name} has unknown or malformed fields")
        if not {"status", "reason"}.issubset(gate):
            raise ValueError(f"release manifest gate {gate_name} must include status and reason")
        status = gate.get("status")
        if not isinstance(status, str) or status not in {"passed", "failed", "not_run"}:
            raise ValueError(f"release manifest gate {gate_name} has invalid status")
        reason = gate.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"release manifest gate {gate_name} must include a nonempty reason")
        outcomes[gate_name] = status

        counts = gate.get("counts")
        if status == "passed" and gate_name in _TEST_GATES and not isinstance(counts, Mapping):
            raise ValueError(f"release manifest test gate {gate_name} passed without test counts")
        if counts is not None:
            if not isinstance(counts, Mapping) or set(counts) != _COUNT_FIELDS:
                raise ValueError(f"release manifest gate {gate_name} has malformed counts")
            if any(type(value) is not int or value < 0 for value in counts.values()):
                raise ValueError(f"release manifest gate {gate_name} has invalid counts")
            if counts["collected"] != sum(counts[field] for field in _COUNT_FIELDS if field != "collected"):
                raise ValueError(f"release manifest gate {gate_name} counts do not add up")
            if status == "passed":
                try:
                    require_gate(GateCounts(**counts))
                except ValueError as err:
                    raise ValueError(
                        f"release manifest gate {gate_name} passed with non-pass counts: {err}"
                    ) from err

    actual_success = all(outcomes.get(name) == "passed" for name in expected) and (
        outcomes.get("cleanup", "passed") == "passed"
    )
    if type(manifest.get("overall_success")) is not bool:
        raise ValueError("release manifest overall_success must be a boolean")
    if manifest["overall_success"] is not actual_success:
        raise ValueError("release manifest overall_success does not match gate statuses")
    validation_error = manifest.get("validation_error")
    if validation_error is not None and (
        not isinstance(validation_error, str)
        or not validation_error.strip()
        or manifest["overall_success"] is not False
    ):
        raise ValueError("release manifest validation_error is invalid")
    return actual_success


def load_release_manifest(path: Path | str) -> dict[str, Any]:
    manifest_path = require_report(path, "release manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"malformed release manifest: {err}") from err
    validate_release_manifest(manifest)
    return manifest


def validate_speech_evidence(
    evidence: Any,
    *,
    expected_fixture_sha256: str,
    expected_reference_text: str,
) -> None:
    """Reject text-only, synthetic, silent, unpinned, or incomplete speech proof."""
    if not isinstance(evidence, Mapping):
        raise ValueError("speech evidence must be a JSON object")
    expected_hash = expected_fixture_sha256.lower()
    if not _SHA256_RE.fullmatch(expected_hash):
        raise ValueError("reviewed speech fixture hash is invalid")
    if evidence.get("input_path") != "speech_pcm":
        raise ValueError("speech qualification requires the speech_pcm input path; send-text is not speech")
    if evidence.get("synthetic") is not False or evidence.get("fixture_source") != "recorded_human_speech":
        raise ValueError("synthetic media is not eligible; fixture must be recorded human speech")
    if evidence.get("fixture_sha256") != expected_hash:
        raise ValueError("speech evidence fixture hash does not match the reviewed fixture hash")
    sample_rate = evidence.get("sample_rate_hz")
    if type(sample_rate) is not int or not 8_000 <= sample_rate <= 192_000:
        raise ValueError("speech PCM sample rate is missing or invalid")

    if evidence.get("evidence_source") != "live_qualification":
        raise ValueError("fake or unverified speech evidence cannot qualify")
    if evidence.get("transport_class") not in {"browser_webrtc", "local_audio_device"}:
        raise ValueError("speech evidence requires a real audio transport, not a fake or synthetic transport")
    if evidence.get("sample_format") != "s16le":
        raise ValueError("speech PCM sample format must be s16le")

    def require_speech_samples(field: str, label: str) -> list[int]:
        samples = evidence.get(field)
        if not isinstance(samples, list) or not samples:
            raise ValueError(f"{label} PCM samples are missing")
        if any(type(value) is not int or value < -32_768 or value > 32_767 for value in samples):
            raise ValueError(f"{label} PCM samples must be signed 16-bit integers")
        if max(abs(value) for value in samples) < 256:
            raise ValueError(f"{label} PCM is silent")
        return samples

    input_samples = require_speech_samples("input_pcm_samples", "input")
    received = evidence.get("stt_received_samples")
    if type(received) is not int or received != len(input_samples):
        raise ValueError("STT must report receipt of every actual input sample")
    recognized = evidence.get("recognized_text")
    accepted = evidence.get("accepted_transcripts")
    if not isinstance(expected_reference_text, str) or not expected_reference_text.strip():
        raise ValueError("reviewed corpus transcript reference is missing")
    if not isinstance(recognized, str) or not recognized.strip():
        raise ValueError("STT recognized text is missing")
    if not isinstance(accepted, list) or not accepted:
        raise ValueError("corpus-specific accepted transcript reference is missing")
    normalize = lambda text: " ".join(text.casefold().split())
    if normalize(recognized) != normalize(expected_reference_text):
        raise ValueError("recognized text does not match the reviewed fixture reference")
    if not any(isinstance(item, str) and normalize(item) == normalize(expected_reference_text) for item in accepted):
        raise ValueError("accepted transcript policy does not match the reviewed fixture reference")
    if evidence.get("flow_progressed") is not True:
        raise ValueError("valid pinned-flow progress evidence is missing")
    if evidence.get("turn_status") != "completed":
        raise ValueError("speech turn did not complete")
    require_speech_samples("assistant_output_pcm_samples", "assistant output")

    models = evidence.get("models")
    if not isinstance(models, Mapping) or set(models) != {"llm", "stt", "tts"}:
        raise ValueError("pinned LLM, STT, and TTS model identities are required")
    for name, model in models.items():
        if not isinstance(model, Mapping):
            raise ValueError(f"{name} model identity must be an object")
        for field in ("id", "revision", "sha256"):
            if not isinstance(model.get(field), str) or not model[field].strip():
                raise ValueError(f"{name} model {field} is missing")
        if not _SHA256_RE.fullmatch(model["sha256"].lower()):
            raise ValueError(f"{name} model hash is invalid")
    duration = evidence.get("duration_ms")
    if type(duration) is not int or duration <= 0:
        raise ValueError("speech qualification duration is missing")
    versions = evidence.get("backend_versions")
    if not isinstance(versions, Mapping) or any(
        not isinstance(versions.get(name), str) or not versions[name].strip()
        for name in ("stt", "tts", "transport")
    ):
        raise ValueError("speech qualification backend versions are incomplete")


def validate_audio_evidence_report(
    report: Any,
    *,
    fixtures: Mapping[str, Any],
    profile: str,
    release_commit: str,
    deployment_models: Mapping[str, Any] | None = None,
) -> None:
    """Bind each live EN/HI speech result to reviewed fixtures and release models."""
    if not isinstance(report, Mapping) or set(report) != {"schema_version", "profile", "commit", "cases"}:
        raise ValueError("audio evidence report has missing or unknown fields")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("audio evidence report schema_version must be integer 1")
    if report.get("profile") != profile:
        raise ValueError("audio evidence report profile does not match the requested gate")
    if report.get("commit") != release_commit or not _COMMIT_RE.fullmatch(release_commit):
        raise ValueError("audio evidence report must identify the exact release commit")
    cases = report.get("cases")
    if not isinstance(cases, Mapping) or set(cases) != {"en", "hi"}:
        raise ValueError("audio evidence report must include exactly English and Hindi speech cases")
    for locale in ("en", "hi"):
        fixture = fixtures.get(locale)
        case = cases.get(locale)
        if not isinstance(fixture, Mapping) or not isinstance(case, Mapping):
            raise ValueError(f"required {locale} speech evidence is missing")
        if set(case) != {"fixture_sha256", "evidence"}:
            raise ValueError(f"{locale} speech evidence has missing or unknown fields")
        if case.get("fixture_sha256") != fixture.get("sha256"):
            raise ValueError(f"{locale} speech evidence does not match its reviewed fixture hash")
        evidence = case.get("evidence")
        validate_speech_evidence(
            evidence,
            expected_fixture_sha256=fixture["sha256"],
            expected_reference_text=fixture["reference_text"],
        )
        if deployment_models is not None:
            expected = {
                "llm": deployment_models["llm"],
                "stt": deployment_models["stt"],
                "tts": deployment_models["tts_en" if locale == "en" else "tts_hi"],
            }
            for name, model in expected.items():
                actual = evidence["models"][name]
                if any(actual[field] != model[field] for field in ("id", "revision", "sha256")):
                    raise ValueError(f"{locale} speech evidence used an unpinned {name} model")


def validate_backup_restore_evidence(
    evidence: Any,
    *,
    release_commit: str,
) -> None:
    """Verify a backup archive and fresh-target restore result against this release."""
    if not isinstance(evidence, Mapping):
        raise ValueError("backup restore evidence must be a JSON object")
    source_commit = evidence.get("source_commit")
    if (
        not isinstance(release_commit, str)
        or not _COMMIT_RE.fullmatch(release_commit)
        or source_commit != release_commit
    ):
        raise ValueError("backup restore evidence must refer to the same release commit")
    if type(evidence.get("schema_version")) is not int or evidence["schema_version"] < 1:
        raise ValueError("backup restore schema version is missing")

    archive_path = evidence.get("archive_path")
    if not isinstance(archive_path, str) or not archive_path:
        raise ValueError("backup artifact path is missing")
    archive = Path(archive_path)
    if not archive.is_file():
        raise FileNotFoundError(f"required backup artifact missing: {archive}")
    archive_hash = evidence.get("archive_sha256")
    if not isinstance(archive_hash, str) or not _SHA256_RE.fullmatch(archive_hash.lower()):
        raise ValueError("backup archive hash is invalid")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != archive_hash.lower():
        raise ValueError("backup artifact hash does not match its evidence")
    if evidence.get("fresh_target") is not True:
        raise ValueError("restore must use a fresh target database")
    if evidence.get("target_is_production") is not False:
        raise ValueError("restore target must not be production")
    target_id = evidence.get("target_database_id")
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("restore target database identity is missing")

    source_counts = evidence.get("source_counts")
    restored_counts = evidence.get("restored_counts")
    required_tables = {"users", "wallets", "ledger_entries", "audit_logs"}
    if (
        not isinstance(source_counts, Mapping)
        or not isinstance(restored_counts, Mapping)
        or not required_tables.issubset(source_counts)
        or set(source_counts) != set(restored_counts)
    ):
        raise ValueError("source and restored table counts are incomplete or mismatched")
    for counts in (source_counts, restored_counts):
        if any(type(count) is not int or count < 0 for count in counts.values()):
            raise ValueError("backup restore table counts are invalid")
    if dict(source_counts) != dict(restored_counts):
        raise ValueError("backup restore source and target counts do not match")
    if evidence.get("ledger_reconciled") is not True:
        raise ValueError("restored wallet ledger is not reconciled")
    if evidence.get("prior_audit_preserved") is not True:
        raise ValueError("prior audit evidence was not preserved")
    restore_event = evidence.get("restore_audit_entry")
    if (
        not isinstance(restore_event, Mapping)
        or restore_event.get("action") != "ops.backup.restored"
        or restore_event.get("present") is not True
    ):
        raise ValueError("restore audit entry is missing")
    if evidence.get("restore_status") != "passed":
        raise ValueError("backup restore did not pass")


def validate_public_transport_evidence(
    evidence: Any,
    *,
    config: Mapping[str, Any],
    release_commit: str,
) -> None:
    """Require fresh, same-release proof of real TLS/WebRTC/TURN voice media."""
    fields = {
        "schema_version",
        "profile",
        "commit",
        "origin",
        "browser",
        "tls_verified",
        "relay",
        "media",
        "turn_completed",
        "barge_in_cancelled_audio",
        "reconnect_restored_audio",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != fields:
        raise ValueError("public transport evidence has missing or unknown fields")
    if type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1:
        raise ValueError("public transport evidence schema version must be integer 1")
    if evidence.get("profile") != "deployment":
        raise ValueError("public transport evidence must use the deployment profile")
    commit = evidence.get("commit")
    if (
        not isinstance(release_commit, str)
        or not _COMMIT_RE.fullmatch(release_commit)
        or not isinstance(commit, str)
        or not _COMMIT_RE.fullmatch(commit)
        or commit != release_commit
    ):
        raise ValueError("public transport evidence must match the release commit")

    origins = config.get("allowed_origins")
    if (
        not isinstance(origins, list)
        or not origins
        or not isinstance(evidence.get("origin"), str)
        or evidence["origin"] not in origins
    ):
        raise ValueError("public transport evidence origin is not an approved staging origin")

    expected_browser = config.get("browser")
    browser = evidence.get("browser")
    if not isinstance(expected_browser, Mapping) or not isinstance(browser, Mapping) or set(browser) != {
        "engine",
        "version",
        "real_browser",
    }:
        raise ValueError("public transport browser identity is missing or malformed")
    if (
        browser.get("engine") != expected_browser.get("engine")
        or browser.get("version") != expected_browser.get("version")
        or browser.get("engine") != "chromium"
        or browser.get("real_browser") is not True
    ):
        raise ValueError("public transport evidence requires the configured real Chromium browser")
    if evidence.get("tls_verified") is not True:
        raise ValueError("public transport TLS verification failed")

    expected_turn = config.get("turn")
    relay = evidence.get("relay")
    if not isinstance(expected_turn, Mapping) or not isinstance(relay, Mapping) or set(relay) != {
        "endpoint",
        "relay_used",
        "candidate_type",
    }:
        raise ValueError("public transport TURN relay evidence is missing or malformed")
    if (
        expected_turn.get("required") is not True
        or relay.get("endpoint") != expected_turn.get("endpoint")
        or relay.get("relay_used") is not True
    ):
        raise ValueError("public transport must use the configured TURN relay")
    if relay.get("candidate_type") != "relay":
        raise ValueError("public transport candidate must be a relay candidate")

    media = evidence.get("media")
    if not isinstance(media, Mapping) or set(media) != {
        "remote_track_received",
        "input_pcm_samples",
        "output_pcm_samples",
    }:
        raise ValueError("public transport media evidence is missing or malformed")
    if (
        media.get("remote_track_received") is not True
        or type(media.get("input_pcm_samples")) is not int
        or media["input_pcm_samples"] <= 0
        or type(media.get("output_pcm_samples")) is not int
        or media["output_pcm_samples"] <= 0
    ):
        raise ValueError("public transport requires non-silent real media in both directions")
    for field, description in (
        ("turn_completed", "completed turn"),
        ("barge_in_cancelled_audio", "barge-in cancellation"),
        ("reconnect_restored_audio", "reconnect recovery"),
    ):
        if evidence.get(field) is not True:
            raise ValueError(f"public transport {description} evidence is missing")


def validate_deployment_config(config: Any) -> None:
    """Validate the exact reviewed deployment profile and all pinned dependencies."""
    if not isinstance(config, Mapping):
        raise ValueError("deployment qualification config must be an object")
    expected_fields = {
        "schema_version",
        "profile",
        "allowed_origins",
        "models",
        "runner_api_endpoints",
        "turn",
        "browser",
        "database",
        "concurrency",
        "limits",
        "baseline_artifact_ids",
    }
    if set(config) != expected_fields:
        raise ValueError("deployment qualification config has missing or unknown fields")
    if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise ValueError("deployment qualification config schema_version must be 1")
    if config.get("profile") != "deployment":
        raise ValueError("deployment qualification config profile must be deployment")

    origins = config.get("allowed_origins")
    if (
        not isinstance(origins, list)
        or not origins
        or any(not isinstance(origin, str) for origin in origins)
        or len(set(origins)) != len(origins)
    ):
        raise ValueError("deployment qualification config has no unique allowed origins")
    for origin in origins:
        parsed = urlparse(origin) if isinstance(origin, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("deployment allowed origins must be exact HTTPS origins")

    models = config.get("models")
    model_names = {"llm", "stt", "tts_en", "tts_hi"}
    if not isinstance(models, Mapping) or set(models) != model_names:
        raise ValueError("deployment model identities must include llm, stt, tts_en, and tts_hi")
    for name, model in models.items():
        if not isinstance(model, Mapping) or set(model) != {"id", "revision", "sha256"}:
            raise ValueError(f"deployment {name} model identity has missing or unknown fields")
        for field in ("id", "revision", "sha256"):
            if not isinstance(model.get(field), str) or not model[field].strip():
                raise ValueError(f"deployment {name} model {field} is missing")
        if not _SHA256_RE.fullmatch(model["sha256"].lower()):
            raise ValueError(f"deployment {name} model hash is invalid")

    endpoints = config.get("runner_api_endpoints")
    if not isinstance(endpoints, Mapping) or set(endpoints) != {"runner", "api"}:
        raise ValueError("deployment runner/API endpoints are missing or unknown")
    for name, endpoint in endpoints.items():
        parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"deployment {name} endpoint must be an HTTPS URL")

    turn = config.get("turn")
    if not isinstance(turn, Mapping) or set(turn) != {"required", "endpoint", "credential_reference"}:
        raise ValueError("deployment TURN relay configuration is missing or malformed")
    if turn.get("required") is not True:
        raise ValueError("deployment TURN relay is required")
    if not isinstance(turn.get("endpoint"), str) or not turn["endpoint"].startswith(("turn:", "turns:")):
        raise ValueError("deployment TURN endpoint is missing or invalid")
    if not isinstance(turn.get("credential_reference"), str) or not turn["credential_reference"].strip():
        raise ValueError("deployment TURN credential reference is missing")

    browser = config.get("browser")
    if (
        not isinstance(browser, Mapping)
        or set(browser) != {"engine", "version"}
        or browser.get("engine") != "chromium"
        or not isinstance(browser.get("version"), str)
        or not browser["version"].strip()
    ):
        raise ValueError("deployment Chromium browser identity is missing or malformed")
    database = config.get("database")
    if (
        not isinstance(database, Mapping)
        or set(database) != {"server_version", "image_digest"}
        or not isinstance(database.get("server_version"), str)
        or not database["server_version"].strip()
        or not isinstance(database.get("image_digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", database["image_digest"])
    ):
        raise ValueError("deployment PostgreSQL database identity is missing or malformed")
    concurrency = config.get("concurrency")
    if type(concurrency) is not int or concurrency <= 0:
        raise ValueError("deployment measured concurrency must be a positive integer")
    limits = config.get("limits")
    if not isinstance(limits, Mapping) or set(limits) != {"turn_timeout_ms", "latency_p95_ms", "quality_threshold"}:
        raise ValueError("deployment timeout, latency, and quality limits are missing or malformed")
    for field in ("turn_timeout_ms", "latency_p95_ms"):
        if type(limits.get(field)) is not int or limits[field] <= 0:
            raise ValueError(f"deployment {field} must be a positive integer")
    quality = limits.get("quality_threshold")
    if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not 0 < quality <= 1:
        raise ValueError("deployment quality_threshold must be greater than zero and at most one")
    baselines = config.get("baseline_artifact_ids")
    if not isinstance(baselines, list) or not baselines or any(
        not isinstance(item, str) or not item.strip() for item in baselines
    ) or len(set(baselines)) != len(baselines):
        raise ValueError("deployment measured baseline artifact IDs are missing or duplicated")


def require_junit_testcase(path: Path | str, test_name: str) -> None:
    report = require_report(path, "JUnit report")
    try:
        root = ET.parse(report).getroot()
    except ET.ParseError as err:
        raise ValueError(f"malformed JUnit report {report}: {err}") from err
    matches = [
        case for case in root.findall(".//testcase")
        if case.attrib.get("name") == test_name
    ]
    if len(matches) != 1:
        raise ValueError(f"backup restore acceptance testcase missing or duplicated: {test_name}")
    case = matches[0]
    if case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None:
        raise ValueError(f"required testcase did not pass: {test_name}")


def validate_staging_runbook(text: str) -> None:
    required_phrases = (
        "## staging release qualification",
        "scripts/voice-ai.sh verify release",
        "--profile deployment --config",
        "verify_audio_root",
        "turn",
        "manifest.json",
        "result.json",
        "same release commit",
        "audio-evidence.json",
        "transport-evidence.json",
        "real chromium tls/webrtc session",
        "public and real-paid flags remain off",
    )
    normalized = " ".join(text.casefold().replace("\\\n", " ").split())
    missing = [phrase for phrase in required_phrases if phrase not in normalized]
    if missing:
        raise ValueError(f"staging release runbook is incomplete: missing {', '.join(missing)}")


def validate_release_artifacts(run_dir: Path | str, *, require_final_report: bool) -> None:
    """Require complete reports and passed prerequisite gates from an owned release run."""
    root = Path(run_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"required release report directory missing: {root}")
    manifest_path = require_report(root / "manifest.json", "release manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"malformed release manifest: {err}") from err
    validate_release_manifest(manifest)
    if manifest.get("commit") is None:
        raise ValueError("release manifest commit is missing")
    gates = manifest["gates"]
    for name in RELEASE_REQUIRED_GATES[:-1]:
        if gates[name]["status"] != "passed":
            raise ValueError(f"required release gate {name} did not pass: {gates[name]['reason']}")
    if gates["release_evidence"]["status"] not in {"not_run", "passed"}:
        raise ValueError("release evidence gate already failed")

    commands_path = require_report(root / "commands.json", "command report")
    try:
        commands = json.loads(commands_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"malformed command report: {err}") from err
    if not isinstance(commands, list):
        raise ValueError("command report must be an array")
    command_names = {
        "python_unit",
        "bun_test",
        "bun_typecheck",
        "bun_build",
        "postgres_tests",
        "browser_dev_tests",
        "browser_tls_tests",
        "voice_qualification",
    }
    for name in command_names:
        matching = [entry for entry in commands if isinstance(entry, Mapping) and entry.get("name") == name]
        if not matching:
            raise ValueError(f"required release command report missing: {name}")
        if any(type(entry.get("returncode")) is not int or entry["returncode"] != 0 for entry in matching):
            raise ValueError(f"required release command failed: {name}")

    for filename in ("python-unit.xml", "postgres.xml", "audio.xml"):
        counts = parse_junit_xml(require_report(root / filename, f"{filename} test report"))
        require_gate(counts)
    require_junit_testcase(
        root / "postgres.xml",
        "test_backup_restores_platform_data_into_fresh_owned_database",
    )
    browser_counts = parse_playwright_json(require_report(root / "playwright.json", "browser report"))
    require_gate(browser_counts)
    if require_final_report:
        require_gate(parse_junit_xml(require_report(root / "release-evidence.xml", "release evidence report")))


@dataclass(frozen=True)
class GateCounts:
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    xpassed: int


def require_gate(counts: GateCounts) -> None:
    if counts.collected == 0:
        raise ValueError("required gate collected zero tests")
    if counts.passed == 0:
        raise ValueError("required gate passed zero tests")
    if any(
        (
            counts.failed,
            counts.errors,
            counts.skipped,
            counts.xfailed,
            counts.xpassed,
        )
    ):
        raise ValueError("required gate contains non-pass outcomes")


def prerequisite_action(*, available: bool, required: bool) -> str:
    if available:
        return "run"
    if required:
        raise ValueError("required qualification prerequisite unavailable")
    return "skip"


def redact_secrets(text: str) -> str:
    """Redact passwords and tokens from URLs and log text."""
    if not text:
        return ""
    # Redact postgres and http/https credentials
    sanitized = re.sub(
        r"([a-zA-Z0-9+._-]+://[^:/?#@\s\"';]+:)([^/?#@\s\"';]+)(@)",
        r"\1***\3",
        text,
    )
    # Redact common token patterns in query params or headers
    sanitized = re.sub(
        r"(token|secret|password|key)=([a-zA-Z0-9_\-\.]+)",
        r"\1=***",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def parse_junit_xml(path: Path | str) -> GateCounts:
    """Parse pytest JUnit XML into strict GateCounts."""
    report_path = Path(path)
    if not report_path.is_file():
        raise FileNotFoundError(f"required report missing: {report_path}")

    try:
        tree = ET.parse(report_path)
    except ET.ParseError as err:
        raise ValueError(f"malformed JUnit XML report {report_path}: {err}") from err

    root = tree.getroot()

    collected = 0
    passed = 0
    failed = 0
    errors = 0
    skipped = 0
    xfailed = 0
    xpassed = 0

    testcases = root.findall(".//testcase")
    if not testcases:
        # Check if root or testsuite has attributes indicating empty or errored run
        total_tests = int(root.attrib.get("tests", 0))
        total_errors = int(root.attrib.get("errors", 0))
        total_failures = int(root.attrib.get("failures", 0))
        total_skipped = int(root.attrib.get("skipped", 0))
        collected = total_tests
        errors = total_errors
        failed = total_failures
        skipped = total_skipped
        passed = max(0, collected - (errors + failed + skipped))
        return GateCounts(
            collected=collected,
            passed=passed,
            failed=failed,
            errors=errors,
            skipped=skipped,
            xfailed=0,
            xpassed=0,
        )

    for tc in testcases:
        collected += 1
        fail_el = tc.find("failure")
        err_el = tc.find("error")
        skip_el = tc.find("skipped")

        if fail_el is not None:
            failed += 1
        elif err_el is not None:
            errors += 1
        elif skip_el is not None:
            msg = (skip_el.attrib.get("message") or "").lower()
            skip_type = (skip_el.attrib.get("type") or "").lower()
            if "xfail" in msg or "xfail" in skip_type:
                xfailed += 1
            elif "xpass" in msg or "xpass" in skip_type:
                xpassed += 1
            else:
                skipped += 1
        else:
            passed += 1

    return GateCounts(
        collected=collected,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        xfailed=xfailed,
        xpassed=xpassed,
    )


def parse_bun_output(output: str) -> GateCounts:
    """Parse captured Bun test runner output into strict GateCounts."""
    if not output or not output.strip():
        raise ValueError("missing Bun test runner output")

    # Strip ANSI color escapes and match anchored summary lines
    clean_output = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)

    pass_match = re.search(r"^\s*(\d+)\s+pass\b", clean_output, re.MULTILINE)
    fail_match = re.search(r"^\s*(\d+)\s+fail\b", clean_output, re.MULTILINE)
    skip_match = re.search(r"^\s*(\d+)\s+skip\b", clean_output, re.MULTILINE)
    todo_match = re.search(r"^\s*(\d+)\s+todo\b", clean_output, re.MULTILINE)
    error_match = re.search(r"^\s*(\d+)\s+error\b", clean_output, re.MULTILINE)

    if todo_match and int(todo_match.group(1)) > 0:
        raise ValueError(f"required gate contains todo tests: {todo_match.group(1)} todo")

    if not pass_match and not fail_match:
        ran_match = re.search(r"^\s*Ran\s+(\d+)\s+tests\b", clean_output, re.MULTILINE)
        if not ran_match:
            raise ValueError(f"unparseable Bun test report: {clean_output[-200:]}")
        raise ValueError(f"unparseable Bun test summary lines: {clean_output[-200:]}")
    passed = int(pass_match.group(1)) if pass_match else 0
    failed = int(fail_match.group(1)) if fail_match else 0
    skipped = int(skip_match.group(1)) if skip_match else 0
    errors = int(error_match.group(1)) if error_match else 0

    collected = passed + failed + skipped + errors

    return GateCounts(
        collected=collected,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        xfailed=0,
        xpassed=0,
    )


def parse_playwright_json(data_or_path: dict[str, Any] | str | Path) -> GateCounts:
    """Parse Playwright JSON reporter output into strict GateCounts."""
    if isinstance(data_or_path, (str, Path)):
        p = Path(data_or_path)
        if not p.is_file():
            raise FileNotFoundError(f"Playwright report file missing: {p}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise ValueError(f"malformed Playwright JSON report: {err}") from err
    else:
        raw = data_or_path

    stats = raw.get("stats")
    if not stats:
        # If no stats block, try walking suites
        suites = raw.get("suites")
        if not suites:
            raise ValueError("Playwright report missing stats and suites")
        collected = 0
        passed = 0
        failed = 0
        skipped = 0

        def walk(suite: dict[str, Any]) -> None:
            nonlocal collected, passed, failed, skipped
            for spec in suite.get("specs", []):
                for test in spec.get("tests", []):
                    collected += 1
                    status = test.get("status")
                    if status == "expected":
                        passed += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        failed += 1
            for child in suite.get("suites", []):
                walk(child)

        for s in suites:
            walk(s)

        return GateCounts(
            collected=collected,
            passed=passed,
            failed=failed,
            errors=0,
            skipped=skipped,
            xfailed=0,
            xpassed=0,
        )

    expected = stats.get("expected", 0)
    unexpected = stats.get("unexpected", 0)
    flaky = stats.get("flaky", 0)
    skipped = stats.get("skipped", 0)

    # In strict verification: flaky is treated as failed (no retries tolerated)
    failed = unexpected + flaky
    collected = expected + failed + skipped

    return GateCounts(
        collected=collected,
        passed=expected,
        failed=failed,
        errors=0,
        skipped=skipped,
        xfailed=0,
        xpassed=0,
    )


def load_audio_fixture_manifest(
    path: Path | str,
    *,
    audio_root: Path | str | None = None,
) -> dict[str, Any]:
    """Load the committed EN/HI corpus manifest and verify provisioned PCM bytes."""
    import wave

    manifest_path = require_report(path, "speech fixture manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"malformed speech fixture manifest: {err}") from err
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "fixtures"}
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise ValueError("speech fixture manifest must use the strict version-1 schema")
    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("speech fixture manifest must include one English and one Hindi fixture")

    root = Path(audio_root) if audio_root is not None else manifest_path.parent
    if not root.is_absolute():
        raise ValueError(f"required absolute speech fixture directory missing: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"required speech fixture directory missing: {root}")
    required_fields = {
        "id",
        "language",
        "reference_text",
        "license",
        "provenance",
        "sha256",
        "sample_format",
        "filename",
    }
    fixtures: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for fixture in entries:
        if not isinstance(fixture, Mapping) or set(fixture) != required_fields:
            raise ValueError("speech fixture entries must use the strict corpus metadata schema")
        locale = fixture.get("language")
        if not isinstance(locale, str) or locale not in {"en", "hi"} or locale in fixtures:
            raise ValueError("speech fixture corpus must contain exactly one en and one hi fixture")
        fixture_id = fixture.get("id")
        if not isinstance(fixture_id, str) or not fixture_id.strip() or fixture_id in seen_ids:
            raise ValueError(f"{locale} speech fixture identifier is missing or duplicated")
        seen_ids.add(fixture_id)
        if fixture.get("provenance") != "recorded_human_speech":
            raise ValueError(f"{locale} synthetic media cannot qualify; recorded human speech is required")
        if not isinstance(fixture.get("license"), str) or not fixture["license"].strip():
            raise ValueError(f"{locale} speech fixture license is missing")
        reference = fixture.get("reference_text")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"{locale} speech fixture reference text is missing")
        filename = fixture.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ValueError(f"{locale} speech fixture filename is missing")
        relative = Path(filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"{locale} speech fixture filename must stay inside VERIFY_AUDIO_ROOT")
        fixture_path = root / relative
        resolved_root = root.resolve()
        resolved_path = fixture_path.resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError as err:
            raise ValueError(f"{locale} speech fixture resolves outside VERIFY_AUDIO_ROOT") from err
        fixture_path = resolved_path
        if not fixture_path.is_file():
            raise FileNotFoundError(f"required {locale} speech fixture missing: {fixture_path}")
        expected_hash = fixture.get("sha256")
        if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash.lower()):
            raise ValueError(f"{locale} speech fixture SHA-256 is missing or invalid")
        fixture_bytes = fixture_path.read_bytes()
        if hashlib.sha256(fixture_bytes).hexdigest() != expected_hash.lower():
            raise ValueError(f"{locale} speech fixture hash does not match its manifest")
        if fixture.get("sample_format") != "wav_pcm_s16le":
            raise ValueError(f"{locale} speech fixture sample format must be wav_pcm_s16le")
        try:
            with wave.open(str(fixture_path), "rb") as audio:
                channels = audio.getnchannels()
                sample_width = audio.getsampwidth()
                frame_rate = audio.getframerate()
                frame_count = audio.getnframes()
                pcm = audio.readframes(frame_count)
                if (
                    audio.getcomptype() != "NONE"
                    or channels not in (1, 2)
                    or sample_width != 2
                    or frame_rate < 8_000
                    or frame_count <= 0
                    or len(pcm) != frame_count * channels * sample_width
                ):
                    raise ValueError(f"{locale} fixture must be nonempty uncompressed 16-bit PCM")
        except (wave.Error, EOFError, OSError) as err:
            raise ValueError(f"{locale} speech fixture is not a valid PCM WAV: {err}") from err
        peak = max(
            abs(int.from_bytes(pcm[offset : offset + 2], "little", signed=True))
            for offset in range(0, len(pcm), 2)
        )
        if peak < 256:
            raise ValueError(f"{locale} speech fixture is silent")
        fixtures[locale] = dict(fixture)

    if set(fixtures) != {"en", "hi"}:
        raise ValueError("speech fixture corpus must include English and Hindi inputs")
    return {"schema_version": 1, "fixtures": fixtures}
