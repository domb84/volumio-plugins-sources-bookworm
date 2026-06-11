"""Tests for includes/controls.py pure logic.

The class does I/O and runs blocking loops in __init__, so instances are built
with __new__ and only the attributes a given method needs are set.
"""
import queue
import time
from unittest.mock import Mock, patch

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
        c._publish_capture_baselines = Mock()
        return c

    def test_first_reading_is_baseline_not_a_press(self):
        c = self._controls()
        c._handle_capture_reading(0, 16)
        c._publish_capture_reading.assert_not_called()
        assert c._capture_baseline[0] == 16

    def test_baseline_is_published_when_established(self):
        c = self._controls()
        c._handle_capture_reading(0, 16)   # ch0 baseline -> publish
        c._handle_capture_reading(0, 16)   # still resting, no re-publish
        c._handle_capture_reading(1, 20)   # ch1 baseline -> publish again
        assert c._publish_capture_baselines.call_count == 2
        assert c._capture_baseline == {0: 16, 1: 20}

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


class TestProcessReadings:
    """Short-press fires on release; long-press fires after threshold."""

    CHANNEL = 0
    SKIP_VALUE = 16   # resting / no-press
    BTN_VALUE = 12    # the press value

    def _controls(self):
        c = Controls.__new__(Controls)
        c.controlQ = queue.Queue()
        return c

    def _make_states(self, initial_value=None):
        """States pre-primed so the debounce gate is always open."""
        return {
            self.CHANNEL: {
                "last_value": initial_value,
                "stable_since": 0.0,
                "last_sent": 0.0,
                "btn_state": "idle",
                "press_action": None,
                "press_start": None,
                "long_press_fired": False,
            }
        }

    def _parsed_btns(self):
        return [("btn_pause", self.CHANNEL, ("value", self.BTN_VALUE))]

    def _parsed_skips(self):
        return [("rest", self.CHANNEL, ("value", self.SKIP_VALUE))]

    def _call(self, c, states, data, debounce=0.0, cooldown=0.0, threshold=1.0):
        # Pre-prime last_value so the debounce gate (`data == last_value`) passes
        # on every call regardless of the previous value. Also patch normalize_value
        # to identity so our small test values aren't remapped to different numbers.
        states[self.CHANNEL]["last_value"] = data
        with patch.object(c, 'normalize_value', side_effect=lambda v, *_: v):
            c._process_readings(
                [data], [self.CHANNEL], states,
                self._parsed_btns(), self._parsed_skips(),
                button_debounce_rate=debounce,
                button_cooldown_rate=cooldown,
                long_press_threshold=threshold,
            )

    # --- press state transitions ---

    def test_press_does_not_fire_immediately(self):
        c = self._controls()
        states = self._make_states(initial_value=self.BTN_VALUE)
        self._call(c, states, self.BTN_VALUE)
        assert c.controlQ.empty()
        assert states[self.CHANNEL]["btn_state"] == "pressed"
        assert states[self.CHANNEL]["press_action"] == "btn_pause"

    def test_short_press_fires_on_release(self):
        c = self._controls()
        states = self._make_states(initial_value=self.BTN_VALUE)
        self._call(c, states, self.BTN_VALUE)          # press
        self._call(c, states, self.SKIP_VALUE)         # release
        assert c.controlQ.get_nowait() == {"control": "btn_pause"}
        assert states[self.CHANNEL]["btn_state"] == "idle"

    def test_held_press_does_not_fire_short_before_threshold(self):
        c = self._controls()
        states = self._make_states(initial_value=self.BTN_VALUE)
        self._call(c, states, self.BTN_VALUE)          # press
        self._call(c, states, self.BTN_VALUE)          # still held, under threshold
        assert c.controlQ.empty()

    # --- long press ---

    def test_long_press_fires_action_long_after_threshold(self):
        c = self._controls()
        states = self._make_states(initial_value=self.BTN_VALUE)
        self._call(c, states, self.BTN_VALUE)          # press -> state = pressed
        states[self.CHANNEL]["press_start"] = time.monotonic() - 2.0  # exceed threshold
        self._call(c, states, self.BTN_VALUE)          # still held
        assert c.controlQ.get_nowait() == {"control": "btn_pause_long"}
        assert states[self.CHANNEL]["long_press_fired"] is True

    def test_long_press_fires_only_once_while_held(self):
        c = self._controls()
        states = self._make_states(initial_value=self.BTN_VALUE)
        self._call(c, states, self.BTN_VALUE)
        states[self.CHANNEL]["press_start"] = time.monotonic() - 2.0
        self._call(c, states, self.BTN_VALUE)          # long press fires
        self._call(c, states, self.BTN_VALUE)          # still held — no second fire
        assert c.controlQ.qsize() == 1

    def test_short_press_not_fired_after_long_press(self):
        c = self._controls()
        states = self._make_states(initial_value=self.BTN_VALUE)
        self._call(c, states, self.BTN_VALUE)
        states[self.CHANNEL]["press_start"] = time.monotonic() - 2.0
        self._call(c, states, self.BTN_VALUE)          # long press fires
        c.controlQ.get_nowait()                        # consume long press event
        self._call(c, states, self.SKIP_VALUE)         # release
        assert c.controlQ.empty()

    def test_no_long_press_when_threshold_is_none(self):
        c = self._controls()
        states = self._make_states(initial_value=self.BTN_VALUE)
        self._call(c, states, self.BTN_VALUE, threshold=None)
        states[self.CHANNEL]["press_start"] = time.monotonic() - 2.0
        self._call(c, states, self.BTN_VALUE, threshold=None)  # held past threshold
        assert c.controlQ.empty()                      # no long press
        # but short press still fires on release
        self._call(c, states, self.SKIP_VALUE, threshold=None)
        assert c.controlQ.get_nowait() == {"control": "btn_pause"}

    # --- cooldown ---

    def test_cooldown_suppresses_short_press_on_release(self):
        c = self._controls()
        states = self._make_states(initial_value=self.BTN_VALUE)
        states[self.CHANNEL]["last_sent"] = time.monotonic()   # sent very recently
        self._call(c, states, self.BTN_VALUE)
        self._call(c, states, self.SKIP_VALUE, cooldown=60.0)  # very long cooldown
        assert c.controlQ.empty()

    # --- capture mode suppresses normal actions ---

    def test_capture_mode_suppresses_short_press(self):
        c = self._controls()
        c._handle_capture_reading = Mock()
        states = self._make_states(initial_value=self.BTN_VALUE)
        with patch.object(c, 'normalize_value', side_effect=lambda v, *_: v):
            c._process_readings(
                [self.BTN_VALUE], [self.CHANNEL], states,
                self._parsed_btns(), self._parsed_skips(),
                button_debounce_rate=0.0, button_cooldown_rate=0.0,
                long_press_threshold=1.0, capture=True,
            )
        assert c.controlQ.empty()
        c._handle_capture_reading.assert_called_once_with(self.CHANNEL, self.BTN_VALUE)
