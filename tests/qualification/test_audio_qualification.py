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

    evidence = _speech_evidence()
    evidence["recognized_text"] = "unrelated text"
    with pytest.raises(ValueError, match="reference|accepted transcript"):
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
    }
    with pytest.raises(ValueError, match="fake or unverified"):
        qualification.validate_audio_evidence_report(
            report,
            fixtures=fixtures,
            profile="local_offline",
            release_commit="f" * 40,
        )
def test_live_audio_qualification_requires_commit_bound_evidence_report() -> None:
    if os.environ.get("RUN_AUDIO_QUALIFICATION") != "1":
        return

    audio_root_value = os.environ.get("VERIFY_AUDIO_ROOT")
    artifact_dir_value = os.environ.get("VERIFY_ARTIFACT_DIR")
    profile = os.environ.get("VERIFY_PROFILE")
    assert audio_root_value and artifact_dir_value and profile, (
        "live qualification requires audio fixtures, artifact directory, and profile"
    )
    audio_root = Path(audio_root_value)
    artifact_dir = Path(artifact_dir_value)
    assert audio_root.is_absolute() and artifact_dir.is_absolute(), (
        "live qualification paths must be absolute"
    )
    load_fixtures = getattr(qualification, "load_audio_fixture_manifest", None)
    validate_report = getattr(qualification, "validate_audio_evidence_report", None)
    load_manifest = getattr(qualification, "load_release_manifest", None)
    assert callable(load_fixtures) and callable(validate_report) and callable(load_manifest)

    fixtures = load_fixtures(audio_root / "audio_manifest.json", audio_root=audio_root)
    release_manifest = load_manifest(artifact_dir / "manifest.json")
    report_path = qualification.require_report(
        artifact_dir / "audio-evidence.json",
        "live speech qualification evidence report",
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    deployment_models = None
    if profile == "deployment":
        config_path_value = os.environ.get("VERIFY_QUALIFICATION_CONFIG")
        assert config_path_value, "deployment speech qualification requires its reviewed model config"
        config_path = Path(config_path_value)
        assert config_path.is_absolute() and config_path.is_file(), (
            "deployment speech qualification model config is missing"
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        qualification.validate_deployment_config(config)
        deployment_models = config["models"]

    validate_report(
        report,
        fixtures=fixtures["fixtures"],
        profile=profile,
        release_commit=release_manifest["commit"],
        deployment_models=deployment_models,
    )

