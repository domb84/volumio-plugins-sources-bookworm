"""Tests for includes/utils.py: parse_button_config and the raw-value helpers."""
import pytest

from includes.utils import (
    LEGACY_VALUE_CEILING,
    looks_like_legacy_values,
    median,
    parse_button_config,
    spec_contains,
)


def test_single_value():
    assert parse_button_config({"btn_a": ("0", "12")}) == [("btn_a", 0, ("value", 12))]


def test_value_with_whitespace_is_stripped():
    assert parse_button_config({"btn_a": ("0", " 12 ")}) == [("btn_a", 0, ("value", 12))]


def test_range():
    assert parse_button_config({"btn_a": ("0", "24-25")}) == [("btn_a", 0, ("range", 24, 25))]


def test_range_is_sorted_low_to_high():
    assert parse_button_config({"btn_a": ("1", "30-28")}) == [("btn_a", 1, ("range", 28, 30))]


def test_empty_config():
    assert parse_button_config({}) == []


def test_none_config():
    assert parse_button_config(None) == []


def test_malformed_pair_too_short_is_skipped():
    assert parse_button_config({"btn_a": ("0",)}) == []


def test_malformed_pair_too_long_is_skipped():
    assert parse_button_config({"btn_a": ("0", "1", "2")}) == []


def test_multiple_buttons():
    result = parse_button_config({"a": ("0", "12"), "b": ("1", "20-22")})
    assert ("a", 0, ("value", 12)) in result
    assert ("b", 1, ("range", 20, 22)) in result
    assert len(result) == 2


class TestLooksLikeLegacyValues:
    """Values left over from the old 32-bucket scale can never match a raw reading."""

    def test_old_scale_values_are_flagged(self):
        parsed = parse_button_config({"a": ("0", "21"), "b": ("0", "24-25")})
        assert looks_like_legacy_values(parsed)

    def test_raw_values_are_not_flagged(self):
        parsed = parse_button_config({"a": ("0", "321-352"), "b": ("0", "193-256")})
        assert not looks_like_legacy_values(parsed)

    def test_a_single_raw_value_is_enough_to_clear_the_whole_config(self):
        parsed = parse_button_config({"a": ("0", "21"), "b": ("0", "340")})
        assert not looks_like_legacy_values(parsed)

    def test_boundary_value_counts_as_legacy(self):
        parsed = parse_button_config({"a": ("0", str(LEGACY_VALUE_CEILING))})
        assert looks_like_legacy_values(parsed)

    def test_empty_config_is_not_flagged(self):
        # Nothing mapped yet is a fresh install, not a stale one.
        assert not looks_like_legacy_values([])


class TestSpecContains:
    def test_value_spec_is_exact_without_margin(self):
        assert spec_contains(("value", 340), 340)
        assert not spec_contains(("value", 340), 341)

    def test_value_spec_widens_by_margin(self):
        assert spec_contains(("value", 340), 346, margin=6)
        assert not spec_contains(("value", 340), 347, margin=6)

    def test_range_spec_is_inclusive(self):
        assert spec_contains(("range", 320, 352), 320)
        assert spec_contains(("range", 320, 352), 352)
        assert not spec_contains(("range", 320, 352), 353)

    def test_range_spec_widens_by_margin_on_both_sides(self):
        assert spec_contains(("range", 320, 352), 314, margin=6)
        assert spec_contains(("range", 320, 352), 358, margin=6)
        assert not spec_contains(("range", 320, 352), 359, margin=6)


class TestMedian:
    def test_odd_count_takes_the_middle(self):
        assert median([340, 338, 342]) == 340

    def test_even_count_is_biased_low(self):
        assert median([340, 342]) == 340

    def test_single_outlier_is_discarded(self):
        # One read corrupted by a strobe must not move the result at all.
        assert median([339, 340, 341, 340, 12]) == 340

    def test_result_is_always_one_of_the_samples(self):
        assert median([1, 2, 4, 8, 16]) in (1, 2, 4, 8, 16)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            median([])
