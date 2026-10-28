"""Tests for canonical JSON encoding, hashing, manifest creation, and compilation.

Per docs/first-release/flow-contract.md section 4:
- Golden canonical assertion: {"b":2,"a":"नमस्ते"} -> {"a":"नमस्ते","b":2}.
- Key order changes preserve hash; content changes change hash.
- Missing vs explicit defaults produce distinct authoring hashes but identical flow hashes.
- CompiledFlow contains required immutable manifest structure and artifact_hash.
- Deterministic template rendering and fragment splitting.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bot.flow.canonical import (
    canonical_json_v1,
    compute_artifact_hash,
    compute_authoring_hash,
    compute_flow_hash,
    sha256_canonical,
)
from bot.flow.compiler import (
    compile_flow,
    compute_dependency_fingerprint,
    render_template,
    split_text_into_fragments,
)
from bot.flow.schemas import (
    ConfirmedFieldState,
    Evidence,
    FlowLimits,
    Origin,
    RefusedFieldState,
    UnknownFieldState,
)
from bot.flow.validation import normalize_executable

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "flow"


class TestCanonicalJsonVectors:
    """Validate canonical JSON encoding and authoring SHA-256 against golden vectors."""

    def test_golden_vectors_from_fixture(self) -> None:
        with open(FIXTURES_DIR / "hash-vectors.json", "r", encoding="utf-8") as f:
            fixture = json.load(f)

        for vector in fixture["vectors"]:
            vid = vector["id"]
            inp = vector["input"]
            expected_utf8 = vector["canonical_utf8"]
            expected_sha256 = vector["authoring_sha256"]

            encoded_bytes = canonical_json_v1(inp)
            assert encoded_bytes.decode("utf-8") == expected_utf8, f"Canonical mismatch for {vid}"

            computed_hash = compute_authoring_hash(inp)
            assert computed_hash == expected_sha256, f"Hash mismatch for {vid}"

    def test_golden_hindi_key_order(self) -> None:
        obj1 = {"b": 2, "a": "नमस्ते"}
        obj2 = {"a": "नमस्ते", "b": 2}
        assert canonical_json_v1(obj1) == '{"a":"नमस्ते","b":2}'.encode("utf-8")
        assert canonical_json_v1(obj2) == '{"a":"नमस्ते","b":2}'.encode("utf-8")
        assert compute_authoring_hash(obj1) == compute_authoring_hash(obj2)

    def test_key_insertion_order_preserves_hash(self) -> None:
        d1 = {"z": 10, "a": 1, "m": 5}
        d2 = {"a": 1, "m": 5, "z": 10}
        d3 = {"m": 5, "z": 10, "a": 1}
        h1 = compute_authoring_hash(d1)
        h2 = compute_authoring_hash(d2)
        h3 = compute_authoring_hash(d3)
        assert h1 == h2 == h3

    def test_array_order_preserved(self) -> None:
        d1 = {"items": [1, 2, 3]}
        d2 = {"items": [3, 2, 1]}
        assert canonical_json_v1(d1) != canonical_json_v1(d2)
        assert compute_authoring_hash(d1) != compute_authoring_hash(d2)

    def test_floating_tokens_rejected(self) -> None:
        with pytest.raises(ValueError, match="Floating-point"):
            canonical_json_v1({"float_val": 1.0})
        with pytest.raises(ValueError, match="Floating-point"):
            canonical_json_v1({"float_val": 3.14})

    def test_integer_boundary_checks(self) -> None:
        # Safe integer bounds
        canonical_json_v1({"max": 9007199254740991, "min": -9007199254740991})
        with pytest.raises(ValueError, match="Integer outside safe range"):
            canonical_json_v1({"too_large": 9007199254740992})
        with pytest.raises(ValueError, match="Integer outside safe range"):
            canonical_json_v1({"too_small": -9007199254740992})


class TestFlowHashingAndDefaults:
    """Assert hashing identity contracts across authoring, normalization, and compilation."""

    def test_defaults_differ_in_authoring_but_match_in_normalized_hash(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow_explicit = json.load(f)["flow"]

        # Create flow_missing by omitting optional behavior defaults
        flow_missing = copy.deepcopy(flow_explicit)
        del flow_missing["behavior"]["allow_relevant_detours"]
        del flow_missing["behavior"]["allow_opt_out"]

        # Authoring hashes differ because source JSON is different
        h_auth_explicit = compute_authoring_hash(flow_explicit)
        h_auth_missing = compute_authoring_hash(flow_missing)
        assert h_auth_explicit != h_auth_missing

        # Normalized flow hashes must be identical because normalization fills the same defaults
        norm_explicit = normalize_executable(flow_explicit)
        norm_missing = normalize_executable(flow_missing)
        h_norm_explicit = compute_flow_hash(norm_explicit)
        h_norm_missing = compute_flow_hash(norm_missing)
        assert h_norm_explicit == h_norm_missing

    def test_transition_order_change_changes_hash(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]

        h_orig = compute_flow_hash(flow)
        modified = copy.deepcopy(flow)
        # Swap transitions 0 and 1 on step 0
        t = modified["steps"][0]["transitions"]
        t[0], t[1] = t[1], t[0]
        h_mod = compute_flow_hash(modified)
        assert h_orig != h_mod

    def test_single_hindi_character_change_changes_hash(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]

        h_orig = compute_flow_hash(flow)
        modified = copy.deepcopy(flow)
        # Change Hindi text in welcome
        modified["welcome"]["hi"] = modified["welcome"]["hi"].replace("नमस्ते", "नमस्कार")
        h_mod = compute_flow_hash(modified)
        assert h_orig != h_mod


class TestFlowCompilerArtifact:
    """Validate compilation manifests and artifact hash computation."""

    def test_compile_flow_manifest_fields(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]

        compiled = compile_flow(
            flow,
            policy="Mandatory platform policy text.",
            limits=FlowLimits(),
        )

        assert compiled.flow_hash == compute_flow_hash(compiled.normalized)
        assert len(compiled.flow_hash) == 64
        assert len(compiled.artifact_hash) == 64

        manifest = compiled.manifest
        assert manifest["compiler_version"] == "flow-compiler/1"
        assert manifest["schema_version"] == 1
        assert manifest["canonicalization"] == "canonical_json_v1"
        assert manifest["flow_hash"] == compiled.flow_hash
        assert len(manifest["policy_revision_hash"]) == 64
        assert len(manifest["limits_profile_hash"]) == 64
        assert "compact_index" in manifest
        assert "locale_message_templates" in manifest
        assert "ordered_step_fragments" in manifest
        assert "evaluator_rubric" in manifest

        # Artifact hash is SHA256 of manifest
        expected_art_hash = compute_artifact_hash(manifest)
        assert compiled.artifact_hash == expected_art_hash


class TestTemplateRendering:
    """Validate deterministic message template rendering and fragment splitting."""

    def test_render_all_status_types_en(self) -> None:
        template = (
            "Emp: {{emp}}. Monthly: {{income}}. Annual: {{annual}}. Note: {{note}}."
        )
        fields = {
            "emp": ConfirmedFieldState(
                status="confirmed",
                applicability="applicable",
                value="salaried",
                origin=Origin(source_turn_id="t1", generation_id="g1", committed_state_version=1, source="user_capture"),
            ),
            "income": ConfirmedFieldState(
                status="confirmed",
                applicability="applicable",
                value=80000,
                origin=Origin(source_turn_id="t1", generation_id="g1", committed_state_version=1, source="user_capture"),
            ),
            "annual": UnknownFieldState(status="unknown", applicability="inapplicable"),
            "note": RefusedFieldState(
                status="refused",
                applicability="applicable",
                origin=Origin(source_turn_id="t1", generation_id="g1", committed_state_version=1, source="user_capture"),
            ),
        }
        rendered = render_template(template, fields, locale="en")
        assert rendered == "Emp: salaried. Monthly: 80000. Annual: not applicable. Note: not provided."

    def test_render_all_status_types_hi(self) -> None:
        template = "रोज़गार: {{emp}}। मासिक: {{income}}। वार्षिक: {{annual}}। अन्य: {{note}}।"
        fields = {
            "emp": ConfirmedFieldState(
                status="confirmed",
                applicability="applicable",
                value="salaried",
                origin=Origin(source_turn_id="t1", generation_id="g1", committed_state_version=1, source="user_capture"),
            ),
            "income": ConfirmedFieldState(
                status="confirmed",
                applicability="applicable",
                value=80000,
                origin=Origin(source_turn_id="t1", generation_id="g1", committed_state_version=1, source="user_capture"),
            ),
            "annual": UnknownFieldState(status="unknown", applicability="inapplicable"),
            "note": RefusedFieldState(
                status="refused",
                applicability="applicable",
                origin=Origin(source_turn_id="t1", generation_id="g1", committed_state_version=1, source="user_capture"),
            ),
        }
        rendered = render_template(template, fields, locale="hi")
        assert "वार्षिक: लागू नहीं।" in rendered
        assert "अन्य: प्रदान नहीं किया गया।" in rendered

    def test_split_text_into_fragments_boundary(self) -> None:
        short_text = "Short sentence."
        assert split_text_into_fragments(short_text, max_chars=1024) == [short_text]

        # Multi-sentence split
        s1 = "A" * 600 + "."
        s2 = "B" * 600 + "."
        combined = f"{s1} {s2}"
        parts = split_text_into_fragments(combined, max_chars=1024)
        assert len(parts) == 2
        assert all(len(p) <= 1024 for p in parts)
