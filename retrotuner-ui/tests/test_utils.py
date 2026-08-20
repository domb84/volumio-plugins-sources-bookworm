"""Tests for includes/utils.py: parse_button_config and the raw-value helpers."""
from includes.utils import parse_button_config, spec_contains


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
