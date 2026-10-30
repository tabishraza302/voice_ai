"""Speech evidence must be tied to reviewed PCM fixtures and real STT/model output."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pytest

from tests.support import qualification

pytestmark = pytest.mark.qualification


FIXTURE_SHA256 = "b" * 64
MODEL_SHA256 = "c" * 64


def _speech_evidence() -> dict:
    return {
        "input_path": "speech_pcm",
        "evidence_source": "live_qualification",
        "transport_class": "browser_webrtc",
        "input_pcm_samples": [0, 2400, -1900, 4100, -3200, 800],
        "sample_rate_hz": 16_000,
        "sample_format": "s16le",
        "fixture_sha256": FIXTURE_SHA256,
        "fixture_source": "recorded_human_speech",
        "synthetic": False,
        "stt_received_samples": 6,
        "recognized_text": "I need help with my loan",
        "accepted_transcripts": ["I need help with my loan"],
        "flow_progressed": True,
        "turn_status": "completed",
        "assistant_output_pcm_samples": [0, 1500, -900, 2700, -1100],
        "models": {
            name: {
                "id": f"{name}-model",
                "revision": f"{name}-revision-1",
                "sha256": MODEL_SHA256,
            }
            for name in ("llm", "stt", "tts")
        },
        "duration_ms": 1_250,
        "backend_versions": {"stt": "stt-1", "tts": "tts-1", "transport": "webrtc-1"},
    }


def _validate(evidence: dict) -> None:
    validator = getattr(qualification, "validate_speech_evidence", None)
    assert callable(validator), "speech evidence validation is not implemented"
    validator(
        evidence,
        expected_fixture_sha256=FIXTURE_SHA256,
        expected_reference_text="I need help with my loan",
    )


def _audio_report(recognized: dict[str, str] | None = None) -> tuple[dict, dict]:
    fixtures = {
        locale: {"sha256": FIXTURE_SHA256, "reference_text": f"reference {locale}"}
        for locale in ("en", "hi")
    }
    cases = {}
    quality_scores = {}
    for locale, fixture in fixtures.items():
        transcript = (recognized or {}).get(locale, fixture["reference_text"])
        evidence = _speech_evidence()
        evidence.update(
            fixture_sha256=fixture["sha256"],
            recognized_text=transcript,
            accepted_transcripts=[fixture["reference_text"]],
        )
        cases[locale] = {"fixture_sha256": fixture["sha256"], "evidence": evidence}
        quality_scores[locale] = 1.0 if transcript == fixture["reference_text"] else 0.5
    return fixtures, {
        "schema_version": 1,
        "profile": "local_offline",
        "commit": "f" * 40,
        "cases": cases,
        "measurements": {
            "measured_concurrency": 4,
            "latency_p95_ms": 1_250,
            "quality_scores": quality_scores,
        },
    }


def test_unverified_speech_samples_cannot_qualify() -> None:
    evidence = _speech_evidence()
    evidence["evidence_source"] = "unit_test"
    with pytest.raises(ValueError, match="fake or unverified"):
        _validate(evidence)


def test_text_only_or_send_text_fallback_cannot_qualify_speech() -> None:
    evidence = _speech_evidence()
    evidence["input_path"] = "send-text"
    evidence["input_pcm_samples"] = []
    with pytest.raises(ValueError, match="speech_pcm"):
        _validate(evidence)


def test_silent_pcm_cannot_qualify_speech() -> None:
    evidence = _speech_evidence()
    evidence["input_pcm_samples"] = [0, 0, 0, 0, 0, 0]
    with pytest.raises(ValueError, match="silent"):
        _validate(evidence)


def test_synthetic_media_cannot_qualify_speech() -> None:
    evidence = _speech_evidence()
    evidence["synthetic"] = True
    evidence["fixture_source"] = "synthetic_tone"
    with pytest.raises(ValueError, match="synthetic|recorded human speech"):
        _validate(evidence)


def test_fake_or_synthetic_transport_evidence_cannot_qualify() -> None:
    evidence = _speech_evidence()
    evidence["evidence_source"] = "fake"
    with pytest.raises(ValueError, match="fake or unverified"):
        _validate(evidence)

    evidence = _speech_evidence()
    evidence["transport_class"] = "synthetic_test_peer"
    with pytest.raises(ValueError, match="real audio transport"):
        _validate(evidence)


def test_speech_evidence_requires_stt_samples_and_accepted_transcript() -> None:
    evidence = _speech_evidence()
    evidence["stt_received_samples"] = 0
    with pytest.raises(ValueError, match="STT"):
        _validate(evidence)

    evidence["accepted_transcripts"] = []
    with pytest.raises(ValueError, match="accepted transcript"):
        _validate(evidence)


def test_speech_evidence_requires_assistant_speech_and_pinned_models() -> None:
    evidence = _speech_evidence()
    evidence["assistant_output_pcm_samples"] = [0, 0, 0]
    with pytest.raises(ValueError, match="silent"):
        _validate(evidence)

    evidence = _speech_evidence()
    evidence["models"].pop("stt")
    with pytest.raises(ValueError, match="model"):
        _validate(evidence)


def test_pcm_evidence_must_match_the_reviewed_fixture_hash() -> None:
    evidence = _speech_evidence()
    evidence["fixture_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="fixture hash"):
        _validate(evidence)


def test_live_measurements_are_recomputed_and_compared_with_profile_limits() -> None:
    fixtures, report = _audio_report()
    validate = qualification.validate_audio_evidence_report
    limits = {
        "turn_timeout_ms": 2_000,
        "latency_p95_ms": 1_500,
        "quality_threshold": 0.9,
    }
    validate(
        report,
        fixtures=fixtures,
        profile="local_offline",
        release_commit="f" * 40,
        limits=limits,
        expected_concurrency=4,
    )

    timeout_limits = dict(limits, turn_timeout_ms=1_000)
    with pytest.raises(ValueError, match="turn_timeout_ms"):
        validate(
            report,
            fixtures=fixtures,
            profile="local_offline",
            release_commit="f" * 40,
            limits=timeout_limits,
            expected_concurrency=4,
        )

    latency_limits = dict(limits, latency_p95_ms=1_000)
    with pytest.raises(ValueError, match="latency_p95_ms"):
        validate(
            report,
            fixtures=fixtures,
            profile="local_offline",
            release_commit="f" * 40,
            limits=latency_limits,
            expected_concurrency=4,
        )

    with pytest.raises(ValueError, match="configured concurrency"):
        validate(
            report,
            fixtures=fixtures,
            profile="local_offline",
            release_commit="f" * 40,
            limits=limits,
            expected_concurrency=3,
        )

    with pytest.raises(ValueError, match="does not match observed"):
        validate(
            dict(report, measurements=dict(report["measurements"], latency_p95_ms=1)),
            fixtures=fixtures,
            profile="local_offline",
            release_commit="f" * 40,
            limits=limits,
            expected_concurrency=4,
        )

    poor_fixtures, poor_report = _audio_report({"en": "reference"})
    with pytest.raises(ValueError, match="quality_threshold"):
        validate(
            poor_report,
            fixtures=poor_fixtures,
            profile="local_offline",
            release_commit="f" * 40,
            limits=limits,
            expected_concurrency=4,
        )


def _fixture_bundle(root, *, samples=(900, -1200, 1600, -800), provenance="synthetic_tone"):
    import hashlib
    import json
    import struct
    import wave

    pcm = struct.pack("<" + "h" * len(samples), *samples)
    fixtures = []
    for locale in ("en", "hi"):
        filename = f"{locale}.wav"
        with wave.open(str(root / filename), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(pcm)
        fixtures.append(
            {
                "id": f"fixture-{locale}",
                "language": locale,
                "reference_text": f"reference {locale}",
                "license": "CC0-1.0",
                "provenance": provenance,
                "sha256": hashlib.sha256((root / filename).read_bytes()).hexdigest(),
                "sample_format": "wav_pcm_s16le",
                "filename": filename,
            }
        )
    manifest = {"schema_version": 1, "fixtures": fixtures}
    manifest_path = root / "audio_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path

def test_synthetic_fixture_manifest_cannot_qualify_as_real_en_hi_pcm(tmp_path) -> None:
    manifest_path = _fixture_bundle(tmp_path)
    load = getattr(qualification, "load_audio_fixture_manifest", None)
    assert callable(load), "audio fixture manifest validation is not implemented"
    with pytest.raises(ValueError, match="synthetic|recorded human speech"):
        load(manifest_path, audio_root=tmp_path)

def test_missing_synthetic_or_silent_fixture_cannot_qualify(tmp_path) -> None:
    load = getattr(qualification, "load_audio_fixture_manifest", None)
    assert callable(load), "audio fixture manifest validation is not implemented"
    with pytest.raises(FileNotFoundError, match="speech fixture manifest"):
        load(tmp_path / "missing.json", audio_root=tmp_path)

    synthetic = _fixture_bundle(tmp_path, provenance="synthetic_tone")
    with pytest.raises(ValueError, match="synthetic|recorded human speech"):
        load(synthetic, audio_root=tmp_path)

    silent = _fixture_bundle(
        tmp_path,
        samples=(0, 0, 0, 0),
        provenance="recorded_human_speech",
    )
    with pytest.raises(ValueError, match="silent"):
        load(silent, audio_root=tmp_path)


def test_forged_audio_report_cannot_qualify_with_matching_fixture_hashes() -> None:
    fixtures = {
        locale: {
            "sha256": FIXTURE_SHA256,
            "reference_text": f"reference {locale}",
        }
        for locale in ("en", "hi")
    }
    cases = {}
    for locale, fixture in fixtures.items():
        evidence = _speech_evidence()
        evidence["fixture_sha256"] = fixture["sha256"]
        evidence["recognized_text"] = fixture["reference_text"]
        evidence["accepted_transcripts"] = [fixture["reference_text"]]
        evidence["evidence_source"] = "hand-authored-json"
        cases[locale] = {"fixture_sha256": fixture["sha256"], "evidence": evidence}
    report = {
        "schema_version": 1,
        "profile": "local_offline",
        "commit": "f" * 40,
        "cases": cases,
        "measurements": {
            "measured_concurrency": 1,
            "latency_p95_ms": 1_250,
            "quality_scores": {"en": 1.0, "hi": 1.0},
        },
    }
    with pytest.raises(ValueError, match="fake or unverified"):
        qualification.validate_audio_evidence_report(
            report,
            fixtures=fixtures,
            profile="local_offline",
            release_commit="f" * 40,
        )
def test_live_audio_qualification_requires_current_run_signed_evidence() -> None:
    if os.environ.get("RUN_AUDIO_QUALIFICATION") != "1":
        pytest.skip("live audio qualification is not enabled")

    audio_root_value = os.environ.get("VERIFY_AUDIO_ROOT")
    artifact_dir_value = os.environ.get("VERIFY_ARTIFACT_DIR")
    profile = os.environ.get("VERIFY_PROFILE")
    run_id = os.environ.get("VERIFY_RUN_ID")
    profile_hash = os.environ.get("VERIFY_PROFILE_SHA256")
    signing_key = os.environ.get("VERIFY_EVIDENCE_KEY")
    if not audio_root_value:
        pytest.fail("missing prerequisite: VERIFY_AUDIO_ROOT with reviewed English and Hindi human-speech fixtures")
    if not artifact_dir_value or not profile or not run_id or not profile_hash or not signing_key:
        pytest.fail("missing prerequisite: current-run audio qualification signing context")
    audio_root = Path(audio_root_value)
    artifact_dir = Path(artifact_dir_value)
    assert audio_root.is_absolute() and artifact_dir.is_absolute(), (
        "live qualification paths must be absolute"
    )
    fixtures = qualification.load_audio_fixture_manifest(
        audio_root / "audio_manifest.json",
        audio_root=audio_root,
    )
    release_manifest = qualification.load_release_manifest(artifact_dir / "manifest.json")
    report_file = artifact_dir / f"audio-evidence-{profile}.json"
    if not report_file.is_file():
        pytest.fail(
            "missing prerequisite: no current-run PCM-to-STT-to-completed-flow audio qualification "
            f"producer created {report_file.name}"
        )
    report_path = qualification.require_report(
        report_file,
        f"current-run {profile} audio qualification evidence",
    )
    envelope = json.loads(report_path.read_text(encoding="utf-8"))
    report = qualification.validate_live_evidence(
        envelope,
        kind="audio_qualification",
        run_id=run_id,
        profile=profile,
        profile_hash=profile_hash,
        commit=release_manifest["commit"],
        key=signing_key,
    )
    config_path_value = os.environ.get("VERIFY_QUALIFICATION_CONFIG")
    assert config_path_value, f"{profile} speech qualification requires its reviewed model config"
    config_path = Path(config_path_value)
    assert config_path.is_absolute() and config_path.is_file(), (
        f"{profile} speech qualification model config is missing"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if profile == "deployment":
        qualification.validate_deployment_config(config)
        deployment_models = config["models"]
    elif profile == "local_offline":
        qualification.validate_local_qualification_config(config)
        deployment_models = None
    else:
        pytest.fail(f"unknown live speech profile: {profile}")

    qualification.validate_audio_evidence_report(
        report,
        fixtures=fixtures["fixtures"],
        profile=profile,
        release_commit=release_manifest["commit"],
        deployment_models=deployment_models,
        limits=config["limits"],
        expected_concurrency=config["concurrency"],
    )

