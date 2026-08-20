"""Tests for includes/controls.py pure logic.

Controls() touches no hardware -- all of that lives in run() -- so instances are
built with the real constructor.
"""
import queue
import time
from unittest.mock import Mock, patch

from includes.controls import CAPTURE_PRESS_MARGIN, Controls, ControlsConfig


def _bare_controls():
    return Controls(queue.Queue(), ControlsConfig())


class TestLookupButton:
    def test_value_match(self):
        btns = [("btn_a", 0, ("value", 340))]
        assert Controls._lookup_button(0, 340, btns, []) == (False, "btn_a", ("value", 340))

    def test_range_match_inclusive(self):
        btns = [("btn_a", 0, ("range", 320, 352))]
        assert Controls._lookup_button(0, 320, btns, []) == (False, "btn_a", ("range", 320, 352))
        assert Controls._lookup_button(0, 352, btns, []) == (False, "btn_a", ("range", 320, 352))

    def test_no_match_returns_none_action(self):
        btns = [("btn_a", 0, ("value", 340))]
        assert Controls._lookup_button(0, 999, btns, []) == (False, None, None)

    def test_wrong_channel_no_match(self):
        btns = [("btn_a", 0, ("value", 340))]
        assert Controls._lookup_button(1, 340, btns, []) == (False, None, None)

    def test_skip_value(self):
        skips = [("rest", 0, ("value", 1010))]
        assert Controls._lookup_button(0, 1010, [], skips) == (True, None, None)

    def test_skip_range(self):
        skips = [("rest", 0, ("range", 1000, 1020))]
        assert Controls._lookup_button(0, 1010, [], skips) == (True, None, None)

    def test_skip_takes_precedence_over_button(self):
        btns = [("btn_a", 0, ("value", 1010))]
        skips = [("rest", 0, ("value", 1010))]
        assert Controls._lookup_button(0, 1010, btns, skips) == (True, None, None)


class TestLookupButtonHysteresis:
    """A held button keeps its match while the reading drifts around the band edge."""

    BTNS = [("btn_a", 0, ("range", 320, 352)), ("btn_b", 0, ("range", 353, 385))]
    HELD = ("btn_a", ("range", 320, 352))

    def test_reading_just_past_the_edge_stays_with_the_held_button(self):
        assert Controls._lookup_button(0, 356, self.BTNS, [], self.HELD, 6)[1] == "btn_a"

    def test_same_reading_without_hysteresis_moves_to_the_neighbour(self):
        assert Controls._lookup_button(0, 356, self.BTNS, [], None, 0)[1] == "btn_b"

    def test_reading_beyond_the_widened_band_is_released(self):
        assert Controls._lookup_button(0, 359, self.BTNS, [], self.HELD, 6)[1] == "btn_b"

    def test_held_match_returns_the_held_spec_not_the_neighbours(self):
        _, action, spec = Controls._lookup_button(0, 356, self.BTNS, [], self.HELD, 6)
        assert (action, spec) == ("btn_a", ("range", 320, 352))

    def test_genuine_release_still_reaches_the_skip_range(self):
        skips = [("rest", 0, ("range", 1000, 1020))]
        assert Controls._lookup_button(0, 1010, self.BTNS, skips, self.HELD, 6) == (True, None, None)

    def test_hysteresis_ignored_when_nothing_is_held(self):
        assert Controls._lookup_button(0, 356, self.BTNS, [], None, 6)[1] == "btn_b"


class TestCaptureReading:
    """The press-detection state machine used by the settings-page learn flow."""

    def _controls(self):
        c = _bare_controls()
        c._publish_capture_reading = Mock()
        c._publish_capture_baselines = Mock()
        return c

    REST = 1010    # resting value with nothing pressed
    PRESS = 340    # a button pulling the ladder down

    def test_first_reading_is_baseline_not_a_press(self):
        c = self._controls()
        c._handle_capture_reading(0, self.REST)
        c._publish_capture_reading.assert_not_called()
        assert c._capture_baseline[0] == self.REST

    def test_baseline_is_published_when_established(self):
        c = self._controls()
        c._handle_capture_reading(0, self.REST)   # ch0 baseline -> publish
        c._handle_capture_reading(0, self.REST)   # still resting, no re-publish
        c._handle_capture_reading(1, 1006)        # ch1 baseline -> publish again
        assert c._publish_capture_baselines.call_count == 2
        assert c._capture_baseline == {0: self.REST, 1: 1006}

    def test_press_publishes_once(self):
        c = self._controls()
        c._handle_capture_reading(0, self.REST)
        c._handle_capture_reading(0, self.PRESS)
        c._publish_capture_reading.assert_called_once_with(0, self.PRESS, 1)

    def test_held_press_does_not_republish(self):
        c = self._controls()
        c._handle_capture_reading(0, self.REST)
        c._handle_capture_reading(0, self.PRESS)
        c._handle_capture_reading(0, self.PRESS + 2)   # still held, drifted a little
        c._publish_capture_reading.assert_called_once()

    def test_release_then_press_publishes_again_with_next_seq(self):
        c = self._controls()
        c._handle_capture_reading(0, self.REST)    # baseline
        c._handle_capture_reading(0, self.PRESS)   # press -> seq 1
        c._handle_capture_reading(0, self.REST)    # release
        c._handle_capture_reading(0, self.PRESS)   # press -> seq 2
        assert c._publish_capture_reading.call_count == 2
        c._publish_capture_reading.assert_called_with(0, self.PRESS, 2)

    def test_two_channels_tracked_independently(self):
        c = self._controls()
        c._handle_capture_reading(0, self.REST)    # ch0 baseline
        c._handle_capture_reading(1, self.REST)    # ch1 baseline
        c._handle_capture_reading(1, self.PRESS)   # ch1 press -> seq 1
        c._publish_capture_reading.assert_called_once_with(1, self.PRESS, 1)

    # --- tolerance around the resting value ---

    def test_noise_on_the_resting_value_is_not_a_press(self):
        c = self._controls()
        c._handle_capture_reading(0, self.REST)
        c._handle_capture_reading(0, self.REST - CAPTURE_PRESS_MARGIN)
        c._publish_capture_reading.assert_not_called()

    def test_release_tolerates_a_resting_value_that_drifted(self):
        c = self._controls()
        c._handle_capture_reading(0, self.REST)
        c._handle_capture_reading(0, self.PRESS)              # press
        c._handle_capture_reading(0, self.REST - 4)           # release, slightly off
        c._handle_capture_reading(0, self.PRESS)              # press again
        assert c._publish_capture_reading.call_count == 2


class TestProcessReadings:
    """Short-press fires on release; long-press fires after threshold."""

    CHANNEL = 0
    SKIP_VALUE = 1010   # resting / no-press
    BTN_VALUE = 340     # the press value

    def _controls(self):
        return _bare_controls()

    def _make_states(self):
        state = Controls._new_button_state()
        state["stable_since"] = 0.0
        return {self.CHANNEL: state}

    def _parsed_btns(self):
        return [("btn_pause", self.CHANNEL, ("range", self.BTN_VALUE - 12, self.BTN_VALUE + 12))]

    def _parsed_skips(self):
        return [("rest", self.CHANNEL, ("range", self.SKIP_VALUE - 12, self.SKIP_VALUE + 12))]

    def _call(self, c, states, data, debounce=0.0, cooldown=0.0, threshold=1.0, hysteresis=6):
        # Debounce keys on the resolved button, so a reading has to be seen
        # twice before it acts: the first call settles it, the second acts on it.
        # With debounce=0 the second call is always past the gate.
        for _ in range(2):
            c._process_readings(
                [data], [self.CHANNEL], states,
                self._parsed_btns(), self._parsed_skips(),
                button_debounce_rate=debounce,
                button_cooldown_rate=cooldown,
                long_press_threshold=threshold,
                hysteresis=hysteresis,
            )

    # --- press state transitions ---

    def test_press_does_not_fire_immediately(self):
        c = self._controls()
        states = self._make_states()
        self._call(c, states, self.BTN_VALUE)
        assert c.controlQ.empty()
        assert states[self.CHANNEL]["press_match"][0] == "btn_pause"

    def test_short_press_fires_on_release(self):
        c = self._controls()
        states = self._make_states()
        self._call(c, states, self.BTN_VALUE)          # press
        self._call(c, states, self.SKIP_VALUE)         # release
        assert c.controlQ.get_nowait() == {"control": "btn_pause"}
        assert states[self.CHANNEL]["press_match"] is None

    def test_held_press_does_not_fire_short_before_threshold(self):
        c = self._controls()
        states = self._make_states()
        self._call(c, states, self.BTN_VALUE)          # press
        self._call(c, states, self.BTN_VALUE)          # still held, under threshold
        assert c.controlQ.empty()

    # --- long press ---

    def test_long_press_fires_action_long_after_threshold(self):
        c = self._controls()
        states = self._make_states()
        self._call(c, states, self.BTN_VALUE)          # press -> state = pressed
        states[self.CHANNEL]["press_start"] = time.monotonic() - 2.0  # exceed threshold
        self._call(c, states, self.BTN_VALUE)          # still held
        assert c.controlQ.get_nowait() == {"control": "btn_pause_long"}
        assert states[self.CHANNEL]["long_press_fired"] is True

    def test_long_press_fires_only_once_while_held(self):
        c = self._controls()
        states = self._make_states()
        self._call(c, states, self.BTN_VALUE)
        states[self.CHANNEL]["press_start"] = time.monotonic() - 2.0
        self._call(c, states, self.BTN_VALUE)          # long press fires
        self._call(c, states, self.BTN_VALUE)          # still held — no second fire
        assert c.controlQ.qsize() == 1

    def test_short_press_not_fired_after_long_press(self):
        c = self._controls()
        states = self._make_states()
        self._call(c, states, self.BTN_VALUE)
        states[self.CHANNEL]["press_start"] = time.monotonic() - 2.0
        self._call(c, states, self.BTN_VALUE)          # long press fires
        c.controlQ.get_nowait()                        # consume long press event
        self._call(c, states, self.SKIP_VALUE)         # release
        assert c.controlQ.empty()

    def test_no_long_press_when_threshold_is_none(self):
        c = self._controls()
        states = self._make_states()
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
        states = self._make_states()
        states[self.CHANNEL]["last_sent"] = time.monotonic()   # sent very recently
        self._call(c, states, self.BTN_VALUE)
        self._call(c, states, self.SKIP_VALUE, cooldown=60.0)  # very long cooldown
        assert c.controlQ.empty()

    # --- capture mode suppresses normal actions ---

    def _call_capture(self, c, states, data, times=2):
        for _ in range(times):
            c._process_readings(
                [data], [self.CHANNEL], states,
                self._parsed_btns(), self._parsed_skips(),
                button_debounce_rate=0.0, button_cooldown_rate=0.0,
                long_press_threshold=1.0, capture=True,
            )

    def test_capture_mode_suppresses_short_press(self):
        c = self._controls()
        c._handle_capture_reading = Mock()
        states = self._make_states()
        self._call_capture(c, states, self.BTN_VALUE)
        assert c.controlQ.empty()
        c._handle_capture_reading.assert_called_once_with(self.CHANNEL, self.BTN_VALUE)

    def test_capture_mode_waits_for_the_reading_to_settle(self):
        c = self._controls()
        c._handle_capture_reading = Mock()
        states = self._make_states()
        # The first reading only anchors; nothing is published from it alone.
        self._call_capture(c, states, self.BTN_VALUE, times=1)
        c._handle_capture_reading.assert_not_called()

    def test_capture_mode_reanchors_when_the_reading_jumps(self):
        c = self._controls()
        c._handle_capture_reading = Mock()
        states = self._make_states()
        self._call_capture(c, states, self.BTN_VALUE, times=1)   # anchor
        self._call_capture(c, states, self.SKIP_VALUE, times=1)  # jumped -> re-anchor
        c._handle_capture_reading.assert_not_called()

    def test_capture_mode_leaves_the_press_state_neutral(self):
        # Capture can end on the idle timeout rather than a restart, so the
        # action state must not be carrying a press when normal polling resumes.
        c = self._controls()
        c._handle_capture_reading = Mock()
        states = self._make_states()
        states[self.CHANNEL]["press_match"] = ("btn_pause", ("value", self.BTN_VALUE))
        self._call_capture(c, states, self.BTN_VALUE)
        assert states[self.CHANNEL]["press_match"] is None
        assert c.controlQ.empty()


class TestProcessReadingsJitter:
    """The whole point of the change: a reading that wanders must still work."""

    CHANNEL = 0
    BAND = ("range", 328, 352)
    REST = ("range", 998, 1022)

    def _controls(self):
        return _bare_controls()

    def _run(self, c, states, readings, debounce=0.0):
        for value in readings:
            c._process_readings(
                [value], [self.CHANNEL], states,
                [("btn_pause", self.CHANNEL, self.BAND)],
                [("rest", self.CHANNEL, self.REST)],
                button_debounce_rate=debounce,
                button_cooldown_rate=0.0,
                long_press_threshold=1.0,
                hysteresis=6,
            )

    def test_a_wandering_press_still_fires_on_release(self):
        # Under value-equality debounce these readings never settled, so the
        # press was silently dropped rather than merely mis-read.
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        self._run(c, states, [339, 341, 340, 342, 338])   # held, jittering
        self._run(c, states, [1008, 1011])                # released
        assert c.controlQ.get_nowait() == {"control": "btn_pause"}

    def test_a_reading_straddling_the_band_edge_does_not_retrigger(self):
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        # 353/355 sit outside the band but inside the hysteresis margin.
        self._run(c, states, [352, 353, 351, 355, 352])
        assert c.controlQ.empty()                          # no press/release churn
        assert states[self.CHANNEL]["press_match"] is not None
        self._run(c, states, [1010, 1010])
        assert c.controlQ.qsize() == 1                     # exactly one short press


class TestUncaughtReadingWarnings:
    """A reading matching nothing warns once, not once per poll."""

    CHANNEL = 0
    BAND = ("range", 328, 352)
    REST = ("range", 998, 1022)
    UNMAPPED = 600

    def _controls(self):
        return _bare_controls()

    def _run(self, c, states, readings):
        for value in readings:
            c._process_readings(
                [value], [self.CHANNEL], states,
                [("btn_pause", self.CHANNEL, self.BAND)],
                [("rest", self.CHANNEL, self.REST)],
                button_debounce_rate=0.0, button_cooldown_rate=0.0,
                long_press_threshold=1.0, hysteresis=6,
            )

    def test_a_resting_value_off_its_band_warns_once_not_every_poll(self):
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        with patch("includes.controls.logger") as log:
            self._run(c, states, [self.UNMAPPED] * 10)
        assert log.warning.call_count == 1

    def test_warning_returns_after_the_reading_changes_and_comes_back(self):
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        with patch("includes.controls.logger") as log:
            self._run(c, states, [self.UNMAPPED] * 3)   # warns once
            self._run(c, states, [1010] * 2)            # back to rest
            self._run(c, states, [self.UNMAPPED] * 3)   # warns again
        assert log.warning.call_count == 2


class TestPressSummaryLogging:
    """A completed press logs its held duration and observed raw range once, at INFO."""

    CHANNEL = 0
    BAND = ("range", 328, 352)
    REST = ("range", 998, 1022)

    def _controls(self):
        return _bare_controls()

    def _run(self, c, states, readings, debounce=0.0):
        for value in readings:
            c._process_readings(
                [value], [self.CHANNEL], states,
                [("btn_pause", self.CHANNEL, self.BAND)],
                [("rest", self.CHANNEL, self.REST)],
                button_debounce_rate=debounce,
                button_cooldown_rate=0.0,
                long_press_threshold=1.0,
                hysteresis=6,
            )

    def test_clean_release_logs_the_observed_range_and_what_it_triggered(self):
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        with patch("includes.controls.logger") as log:
            self._run(c, states, [340, 340])    # press settles
            self._run(c, states, [345, 345])    # drifts while held
            self._run(c, states, [1010, 1010])  # release -> short press fires
        assert log.info.call_count == 1
        _, channel, action, duration, low, high, triggered = log.info.call_args[0]
        assert (channel, action, low, high, triggered) == (self.CHANNEL, "btn_pause", 340, 345, "btn_pause")
        assert duration >= 0

    def test_nothing_is_logged_while_idle(self):
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        with patch("includes.controls.logger") as log:
            self._run(c, states, [1010] * 5)
        assert log.info.call_count == 0

    def test_drifting_into_an_uncaught_reading_also_logs_the_range_and_nothing_triggered(self):
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        with patch("includes.controls.logger") as log:
            self._run(c, states, [340, 340])  # press
            self._run(c, states, [600, 600])  # drifts into no-man's land, nothing queued
        assert log.warning.call_count == 1
        assert log.info.call_count == 1
        _, channel, action, duration, low, high, triggered = log.info.call_args[0]
        assert (channel, action, low, high, triggered) == (self.CHANNEL, "btn_pause", 340, 340, "nothing")

    def test_release_after_cooldown_suppresses_the_short_press_reports_nothing_triggered(self):
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        states[self.CHANNEL]["last_sent"] = time.monotonic()  # sent very recently
        with patch("includes.controls.logger") as log:
            self._run(c, states, [340, 340])                            # press
            for value in [1010, 1010]:
                c._process_readings(
                    [value], [self.CHANNEL], states,
                    [("btn_pause", self.CHANNEL, self.BAND)],
                    [("rest", self.CHANNEL, self.REST)],
                    button_debounce_rate=0.0, button_cooldown_rate=60.0,  # long cooldown
                    long_press_threshold=1.0, hysteresis=6,
                )
        assert log.info.call_count == 1
        _, channel, action, duration, low, high, triggered = log.info.call_args[0]
        assert triggered == "nothing"

    def test_long_press_release_reports_the_long_press_as_triggered(self):
        c = self._controls()
        states = {self.CHANNEL: Controls._new_button_state()}
        with patch("includes.controls.logger") as log:
            self._run(c, states, [340, 340])                          # press
            states[self.CHANNEL]["press_start"] = time.monotonic() - 2.0  # exceed threshold
            self._run(c, states, [340, 340])                          # long press fires
            self._run(c, states, [1010, 1010])                        # release afterwards
        assert log.info.call_count == 1  # only logged once, on release
        _, channel, action, duration, low, high, triggered = log.info.call_args[0]
        assert triggered == "btn_pause_long"
