"""Tests for includes/controls.py pure logic.

The class does I/O and runs blocking loops in __init__, so instances are built
with __new__ and only the attributes a given method needs are set.
"""
from unittest.mock import Mock

import pytest

from includes.controls import Controls


def _bare_controls():
    return Controls.__new__(Controls)


class TestNormalizeValue:
    @pytest.mark.parametrize("value,expected", [
        (0, 32),      # min -> top of range
        (1024, 0),    # max -> bottom
        (512, 16),    # midpoint
        (1023, 0),    # just above max -> 0 after truncation
    ])
    def test_scaling(self, value, expected):
        assert _bare_controls().normalize_value(value, 0, 1024, 32) == expected


class TestLookupButton:
    def test_value_match(self):
        btns = [("btn_a", 0, ("value", 12))]
        assert Controls._lookup_button(0, 12, btns, []) == (False, "btn_a")

    def test_range_match_inclusive(self):
        btns = [("btn_a", 0, ("range", 24, 25))]
        assert Controls._lookup_button(0, 24, btns, []) == (False, "btn_a")
        assert Controls._lookup_button(0, 25, btns, []) == (False, "btn_a")

    def test_no_match_returns_none_action(self):
        btns = [("btn_a", 0, ("value", 12))]
        assert Controls._lookup_button(0, 99, btns, []) == (False, None)

    def test_wrong_channel_no_match(self):
        btns = [("btn_a", 0, ("value", 12))]
        assert Controls._lookup_button(1, 12, btns, []) == (False, None)

    def test_skip_value(self):
        skips = [("rest", 0, ("value", 16))]
        assert Controls._lookup_button(0, 16, [], skips) == (True, None)

    def test_skip_range(self):
        skips = [("rest", 0, ("range", 14, 18))]
        assert Controls._lookup_button(0, 16, [], skips) == (True, None)

    def test_skip_takes_precedence_over_button(self):
        btns = [("btn_a", 0, ("value", 16))]
        skips = [("rest", 0, ("value", 16))]
        assert Controls._lookup_button(0, 16, btns, skips) == (True, None)


class TestCaptureReading:
    """The press-detection state machine used by the settings-page learn flow."""

    def _controls(self):
        c = _bare_controls()
        c._capture_baseline = {}
        c._capture_pressed = {}
        c._capture_seq = 0
        c._publish_capture_reading = Mock()
        return c

    def test_first_reading_is_baseline_not_a_press(self):
        c = self._controls()
        c._handle_capture_reading(0, 16)
        c._publish_capture_reading.assert_not_called()
        assert c._capture_baseline[0] == 16

    def test_press_publishes_once(self):
        c = self._controls()
        c._handle_capture_reading(0, 16)   # baseline
        c._handle_capture_reading(0, 12)   # press
        c._publish_capture_reading.assert_called_once_with(0, 12, 1)

    def test_held_press_does_not_republish(self):
        c = self._controls()
        c._handle_capture_reading(0, 16)
        c._handle_capture_reading(0, 12)
        c._handle_capture_reading(0, 12)   # still held
        c._publish_capture_reading.assert_called_once()

    def test_release_then_press_publishes_again_with_next_seq(self):
        c = self._controls()
        c._handle_capture_reading(0, 16)   # baseline
        c._handle_capture_reading(0, 12)   # press -> seq 1
        c._handle_capture_reading(0, 16)   # release
        c._handle_capture_reading(0, 12)   # press -> seq 2
        assert c._publish_capture_reading.call_count == 2
        c._publish_capture_reading.assert_called_with(0, 12, 2)

    def test_two_channels_tracked_independently(self):
        c = self._controls()
        c._handle_capture_reading(0, 16)   # ch0 baseline
        c._handle_capture_reading(1, 16)   # ch1 baseline
        c._handle_capture_reading(1, 20)   # ch1 press -> seq 1
        c._publish_capture_reading.assert_called_once_with(1, 20, 1)
