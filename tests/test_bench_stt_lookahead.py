"""The lookahead benchmark's scoring and reporting rules.

The model run itself is a measurement, not a unit test. What can be pinned
is the arithmetic around it: which frame counts are legal, how the native
chunk is derived, how per-clip scores are aggregated, and how the paired
bootstrap is reported. Those are the parts a README number depends on.
"""

from __future__ import annotations

import pytest

from scripts.bench_stt_lookahead import (
    native_chunk_ms,
    paired_bootstrap,
    parse_lookaheads,
    score_text,
    summarize,
)


class TestParseLookaheads:
    def test_preserves_the_requested_order(self):
        assert parse_lookaheads("13,6,3,0") == [13, 6, 3, 0]

    def test_ignores_surrounding_whitespace(self):
        assert parse_lookaheads(" 13 , 6 ") == [13, 6]

    def test_rejects_an_empty_list(self):
        with pytest.raises(ValueError, match="at least one"):
            parse_lookaheads("")
        with pytest.raises(ValueError, match="at least one"):
            parse_lookaheads(" , ")

    def test_rejects_a_value_that_is_not_an_integer(self):
        with pytest.raises(ValueError, match="integer"):
            parse_lookaheads("13,six")

    def test_rejects_a_frame_count_the_checkpoint_does_not_support(self):
        with pytest.raises(ValueError, match="unsupported lookahead 5"):
            parse_lookaheads("13,5")

    def test_rejects_a_duplicate_arm(self):
        # A duplicate would run the same measurement twice and double-count it
        # in any later aggregation.
        with pytest.raises(ValueError, match="duplicate lookahead 6"):
            parse_lookaheads("13,6,6")


class TestNativeChunk:
    def test_chunk_is_one_more_frame_than_the_right_context(self):
        assert native_chunk_ms(13) == 14 * 80
        assert native_chunk_ms(6) == 7 * 80
        assert native_chunk_ms(3) == 4 * 80
        assert native_chunk_ms(0) == 80


class TestScoreText:
    def test_identical_text_scores_zero(self):
        assert score_text("What is the capital of France?", "What is the capital of France?", "en") == (
            0.0,
            0.0,
        )

    def test_case_and_punctuation_are_folded(self):
        assert score_text("What is the capital of France?", "what is the capital of france", "en") == (
            0.0,
            0.0,
        )

    def test_an_empty_hypothesis_scores_one_not_zero(self):
        # jiwer's ratio for an empty hypothesis is infinite; a report that
        # averaged that in would be meaningless.
        assert score_text("hello world", "   ", "en") == (1.0, 1.0)

    def test_hindi_uses_the_indic_normalizer(self):
        assert score_text("हमने उस उम्मीदवार को चुना।", "हमने उस उम्मीदवार को चुना।", "hi") == (
            0.0,
            0.0,
        )


class TestSummarize:
    def test_reports_mean_per_clip_scores_in_percent(self):
        report = summarize(
            "en",
            6,
            wers=[0.0, 0.5],
            cers=[0.0, 0.25],
            clips=["0000", "0001"],
            seconds=12.34,
        )

        assert report["wer"] == 25.0
        assert report["cer"] == 12.5
        assert report["n"] == 2
        assert report["native_chunk_ms"] == 560
        assert report["clips"] == ["0000", "0001"]
        assert report["wers"] == [0.0, 50.0]
        assert report["cers"] == [0.0, 25.0]
        assert report["seconds"] == 12.3

    def test_an_empty_arm_is_an_error_rather_than_a_nan(self):
        with pytest.raises(ValueError, match="no clips"):
            summarize("en", 13, wers=[], cers=[], clips=[], seconds=0.0)


class TestPairedBootstrap:
    def test_identical_arms_have_a_zero_width_interval(self):
        result = paired_bootstrap([10.0] * 40, [10.0] * 40, samples=2000)

        assert result["mean"] == 0.0
        assert result["lo"] == 0.0
        assert result["hi"] == 0.0
        assert result["worse"] == 0
        assert result["better"] == 0
        assert result["tied"] == 40

    def test_a_uniform_cost_has_a_one_point_interval(self):
        result = paired_bootstrap([10.0] * 40, [11.0] * 40, samples=2000)

        assert result["mean"] == 1.0
        assert result["lo"] == 1.0
        assert result["hi"] == 1.0
        assert result["worse"] == 40

    def test_the_same_seed_reproduces_the_interval(self):
        first = paired_bootstrap([10.0, 12.0, 9.0], [11.0, 11.0, 10.0], samples=500)
        second = paired_bootstrap([10.0, 12.0, 9.0], [11.0, 11.0, 10.0], samples=500)

        assert first == second

    def test_brackets_the_mean_and_labels_direction(self):
        result = paired_bootstrap(
            [10.0, 12.0, 9.0, 11.0],
            [11.0, 11.0, 10.0, 13.0],
            samples=2000,
        )

        assert result["lo"] <= result["mean"] <= result["hi"]
        assert result["worse"] == 3
        assert result["better"] == 1
        assert result["tied"] == 0

    def test_rejects_different_lengths(self):
        with pytest.raises(ValueError, match="same number of clips"):
            paired_bootstrap([10.0], [10.0, 11.0], samples=100)
