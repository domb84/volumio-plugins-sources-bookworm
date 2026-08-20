import RPi.GPIO as GPIO
import pigpio
import spidev
import json
import os
import subprocess
import threading
import time
import logging
from statistics import median_low
from dataclasses import dataclass, field
from queue import Queue
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("Controls")
from .utils import parse_button_config, spec_contains

# Capture ("learn") mode: while the flag file exists the settings page is asking
# us to report raw button readings instead of acting on them.
CAPTURE_FLAG_PATH = "/tmp/retrotuner-ui-capture-on"
CAPTURE_READING_PATH = "/tmp/retrotuner-ui-capture.json"
CAPTURE_BASELINE_PATH = "/tmp/retrotuner-ui-capture-baseline.json"

# Hardware SPI0 always lives on these BCM pins, and `spi.open(0, ...)` always
# selects SPI0, so they are fixed regardless of the configured pin numbers.
SPI0_PINS = "9,10,11"

# The MCP3008 charges its sample-and-hold cap through the resistor ladder during
# a window of only 1.5 clock cycles. At 1MHz that is 1.5us, which only settles a
# source impedance up to ~7k -- marginal for a button ladder, so readings drifted
# across bucket boundaries. 50kHz gives a 30us window (good for ~200k) and costs
# under 1ms per two-channel poll against a 50ms poll interval.
SPI_CLOCK_HZ = 50000

# Floor on the poll interval, whatever the configured rate says.
MIN_POLL = 0.05

# Readings are matched against the raw 10-bit ADC value. On a measured Teac
# ladder, adjacent buttons sit 69-141 counts apart (see CAPTURE_BAND_HALF_WIDTH
# in index.js, which captures against the same hardware), so the tolerances
# below are sized against that gap: wide enough to absorb the noise on one
# reading, narrow enough to leave a guard band between neighbouring buttons.
# Keep this in sync with index.js's capture tolerances if either changes.
#
# ADC_SAMPLES readings are taken per channel per poll and reduced with a median,
# which discards a read corrupted by an LCD strobe rather than averaging its
# error in. Mind the cost before raising it: one read is 3 bytes, so at
# SPI_CLOCK_HZ each costs 480us and a 5-sample two-channel poll spends ~4.8ms on
# the wire -- a tenth of the 50ms interval, in one contiguous burst.
ADC_SAMPLES = 5

# Once a button has matched, its band is widened by this many counts before the
# press is allowed to end. Without it a reading sitting on a band edge flickers
# in and out of the match and reads as a stream of press/release pairs.
BUTTON_HYSTERESIS = 6

# How far a reading must sit from a channel's resting value to count as a press
# while the settings page is learning buttons, and how far it may wander and
# still count as the same reading while it settles.
CAPTURE_PRESS_MARGIN = 10
CAPTURE_STABLE_TOLERANCE = 3


def restore_spi0_pinmux() -> None:
    """Re-assert the ALT0 (SPI0) function on the hardware SPI pins.

    Bitbang mode claims GPIO9/10/11 as plain input/output via RPi.GPIO, which
    clobbers their ALT0 function directly in the pin function-select registers.
    Nothing puts it back: GPIO.cleanup() leaves them as plain inputs, and the
    pinctrl mux is only applied when the SPI driver probes at boot. So switching
    from software to SPI mode without a reboot leaves the SPI peripheral
    disconnected from the pins and every read returns 0.

    raspi-gpio is the only way to set an alt function -- RPi.GPIO has no API for
    it. Setting alt functions needs root and the service runs as `volumio`, so
    this goes via sudo (passwordless on Volumio). Re-asserting is idempotent and
    harmless when the pins are already correct.
    """
    try:
        subprocess.run(
            ["sudo", "-n", "raspi-gpio", "set", SPI0_PINS, "a0"],
            check=True, capture_output=True,
        )
        logger.debug("Re-asserted ALT0 on SPI0 pins %s", SPI0_PINS)
    except Exception as e:
        logger.warning(
            "Could not re-assert ALT0 on SPI0 pins (%s). If button reads come back "
            "as a constant value, run `sudo raspi-gpio set %s a0` or reboot.", e, SPI0_PINS,
        )


@dataclass
class ControlsConfig:
    encA: int = 17
    encB: int = 27
    butClk: int = 11
    butDOUT: int = 9
    butDIN: int = 10
    butCS: int = 22
    but1: int = 0
    but2: int = 7
    spi_bus: int = 1
    spi: bool = True
    btn_config: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    btn_skip_config: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    button_poll_rate: int = 10
    button_debounce_rate: int = 50
    button_cooldown_rate: int = 500
    long_press_threshold: float = 1.0
    button_samples: int = ADC_SAMPLES
    button_hysteresis: int = BUTTON_HYSTERESIS

class Controls:
    """Handle rotary encoder and MCP3008 button inputs.

    This class starts pigpio callbacks for the rotary encoder and runs a
    polling loop for MCP3008 button inputs (either bitbanged or via SPI).
    """

    def __init__(self, controlQ: Queue, config: ControlsConfig, stop_event: Optional[threading.Event] = None) -> None:
        self.controlQ = controlQ
        self.config = config
        self.stop_event = stop_event
        self.pi = None

        # Button capture ("learn") state, driven by the settings page
        self._capture_seq = 0            # increments on each detected press event
        self._capture_baseline = {}      # channel -> resting (no-press) value
        self._capture_pressed = {}       # channel -> currently-pressed flag
        self._capture_was_on = False
        self._capture_active = False     # cached flag read by the rotary callback

    def run(self) -> None:
        """Claim the hardware and poll it until stopped. Thread entry point.

        Deliberately separate from __init__: constructing this class touches no
        hardware, so it can be built (and tested) without a Pi, and the pigpio /
        GPIO claims happen on the thread that owns them.
        """
        logger.debug("Loading controls")
        config = self.config

        self.rotary_encoder(config.encA, config.encB)

        if config.spi:
            logger.debug('SPI mode')
            self.buttons_spi(
                config.spi_bus, config.butCS, config.but1, config.but2,
                config.btn_config, config.btn_skip_config,
                config.button_poll_rate, config.button_debounce_rate, config.button_cooldown_rate,
            )
        else:
            logger.debug('Software mode')
            self.buttons(
                config.butClk, config.butDOUT, config.butDIN, config.butCS,
                config.but1, config.but2, config.btn_config, config.btn_skip_config,
                config.button_poll_rate, config.button_debounce_rate, config.button_cooldown_rate,
            )

        if self.pi is not None:
            self.pi.stop()
        logger.info('Controls stopping')

    @staticmethod
    def _new_button_state() -> Dict:
        """Per-channel tracking for the debounce/press state machine.

        `press_match` and `last_match` are each an (action, spec) pair or None:
        the press currently latched, and whatever the previous reading resolved
        to. Both feed hysteresis -- see `_process_readings`. `press_min`/
        `press_max` track the raw reading's range over the life of the current
        press, and `triggered` the control queued as a result of it (if any),
        for the press-summary log line.
        """
        return {"last_key": None, "stable_since": None, "last_sent": 0.0, "warned": False,
                "press_match": None, "last_match": None,
                "press_start": None, "long_press_fired": False,
                "press_min": None, "press_max": None, "triggered": None,
                "capture_anchor": None, "capture_since": None}

    @staticmethod
    def _clear_press(state: Dict) -> None:
        """Drop any latched press, leaving the channel idle."""
        state["press_match"] = None
        state["press_start"] = None
        state["long_press_fired"] = False
        state["press_min"] = None
        state["press_max"] = None
        state["triggered"] = None

    @staticmethod
    def _lookup_button(
        channel: int,
        data: int,
        parsed_btns: List,
        parsed_skips: List,
        held: Optional[Tuple[Optional[str], Tuple]] = None,
        hysteresis: int = 0,
    ) -> Tuple[bool, Optional[str], Optional[Tuple]]:
        """Return (is_skipped, action_name, matched_spec) for a raw ADC reading.

        is_skipped=True  → value is in a skip range; suppress silently
        action_name=str  → matched button action to fire
        action_name=None → no match found; caller should log a warning

        `held` is the (action, spec) already latched on this channel. Its band is
        tried first, widened by `hysteresis` counts, so a reading that drifts
        just past the edge of the band it is already sitting in stays with that
        button rather than reading as a release. A real release lands on the
        resting value, far outside the widened band, so it still gets through.
        """
        if held is not None and hysteresis:
            held_action, held_spec = held
            if spec_contains(held_spec, data, hysteresis):
                return False, held_action, held_spec

        for _name, ch, spec in parsed_skips:
            if ch == channel and spec_contains(spec, data):
                return True, None, None

        for name, ch, spec in parsed_btns:
            if ch == channel and spec_contains(spec, data):
                return False, name, spec

        return False, None, None

    def _capture_enabled(self) -> bool:
        try:
            return os.path.exists(CAPTURE_FLAG_PATH)
        except Exception:
            return False

    def _refresh_capture_state(self) -> bool:
        """Return whether capture mode is active, resetting publish state on enable."""
        capture = self._capture_enabled()
        if capture and not self._capture_was_on:
            self._capture_baseline = {}
            self._capture_pressed = {}
            logger.info("Button capture mode enabled")
        elif not capture and self._capture_was_on:
            logger.info("Button capture mode disabled")
        self._capture_was_on = capture
        self._capture_active = capture
        return capture

    def _track_capture(self, state: Dict, channel: int, data: int, now: float, debounce: float) -> None:
        """Hold a reading steady before handing it to the learn flow.

        Capture wants the settled value of a press, not whatever the ladder was
        passing through as the contact closed, so a reading only counts once it
        has stayed within CAPTURE_STABLE_TOLERANCE counts for the debounce
        period. Anchoring on a tolerance rather than on an exact value is what
        makes this work on raw readings, which never repeat bit-for-bit.
        """
        anchor = state["capture_anchor"]
        if anchor is None or abs(data - anchor) > CAPTURE_STABLE_TOLERANCE:
            state["capture_anchor"] = data
            state["capture_since"] = now
            return
        if now - (state["capture_since"] or 0) < debounce:
            return
        self._handle_capture_reading(channel, data)

    def _handle_capture_reading(self, channel: int, value: int) -> None:
        """Detect press events for the settings-page learn flow.

        The first stable value seen on a channel is taken as its resting
        (no-press) baseline. A press is any stable value sitting more than
        CAPTURE_PRESS_MARGIN counts off that baseline; we publish once per press
        (on the resting->pressed edge) so the settings page sees one event per
        physical press regardless of how often it polls.
        """
        baseline = self._capture_baseline.get(channel)
        if baseline is None:
            self._capture_baseline[channel] = value
            self._capture_pressed[channel] = False
            self._publish_capture_baselines()
            return
        if abs(value - baseline) <= CAPTURE_PRESS_MARGIN:
            self._capture_pressed[channel] = False  # released
            return
        if not self._capture_pressed.get(channel):
            self._capture_pressed[channel] = True
            self._capture_seq += 1
            self._publish_capture_reading(channel, value, self._capture_seq)

    def _publish_capture_baselines(self) -> None:
        """Publish the per-channel resting values so the settings page can capture the no-press baseline."""
        try:
            with open(CAPTURE_BASELINE_PATH, "w") as handle:
                json.dump(self._capture_baseline, handle)
        except Exception as e:
            logger.debug("Could not publish capture baselines: %s", e)

    def _publish_capture_reading(self, channel: int, value: int, seq: int) -> None:
        """Publish a detected press so the settings page can learn a button value."""
        try:
            with open(CAPTURE_READING_PATH, "w") as handle:
                json.dump({"channel": channel, "value": value, "seq": seq}, handle)
        except Exception as e:
            logger.debug("Could not publish capture reading: %s", e)

    @staticmethod
    def _log_press_summary(channel: int, action: str, state: Dict, now: float) -> None:
        """Log how long a press was held, the raw range it covered, and what it triggered.

        Logged once the press ends -- whether by a clean release or by drifting
        into an unrecognised reading -- since the range only settles once the
        hold is over. `triggered` is whichever control (if any) was actually
        queued during this press: nothing for a short press still on cooldown,
        or a release after a long press has already fired. At INFO rather than
        DEBUG: it fires once per physical press (never while idle), so it's
        cheap to leave on, and the range is exactly what's needed to tune
        button bands/hysteresis against real hardware behaviour.
        """
        duration = now - (state["press_start"] or now)
        logger.info(
            "Channel %d button press: %s detected for %.1fs (raw %d-%d), triggered: %s",
            channel, action, duration, state["press_min"], state["press_max"],
            state["triggered"] or "nothing",
        )

    def _process_readings(self, batch_data, channels, button_states, parsed_btns, parsed_skips,
                          button_debounce_rate, button_cooldown_rate, long_press_threshold=None,
                          capture=False, hysteresis=BUTTON_HYSTERESIS):
        """Apply debounce/cooldown to a batch of raw ADC readings and emit button actions.

        Debounce keys on the *resolved* button, not on the reading itself. Raw
        10-bit readings wander by a count or two between polls, so waiting for
        two identical readings would never settle and the press would never
        fire; waiting for two readings that resolve to the same button settles
        immediately and absorbs that jitter.

        In capture mode each settled reading is published for the settings page
        (so a button's value can be learned) and the normal action is suppressed
        so pressing buttons doesn't navigate the menu.
        """
        for data, channel in zip(batch_data, channels):
            state = button_states[channel]
            now = time.monotonic()

            if capture:
                # Keep the press state neutral so resuming from capture (which
                # happens without a restart on the idle timeout) can't fire a
                # press that was only ever meant to be learnt.
                self._clear_press(state)
                state.update(last_key=None, stable_since=None, last_match=None)
                self._track_capture(state, channel, data, now, button_debounce_rate)
                continue

            # Hysteresis anchors on the latched press once there is one, and on
            # the previous reading's match before that. Anchoring only on the
            # latched press would be too late: a reading straddling a band edge
            # alternates between two matches, so the debounce never settles and
            # the press never latches in the first place.
            held = state["press_match"] or state["last_match"]
            skipped, action, spec = self._lookup_button(
                channel, data, parsed_btns, parsed_skips, held, hysteresis,
            )
            state["last_match"] = (action, spec) if spec else None

            key = (skipped, action)
            if key != state["last_key"]:
                state["stable_since"] = now
                state["last_key"] = key
                state["warned"] = False
                continue
            if now - (state["stable_since"] or 0) < button_debounce_rate:
                continue

            press = state["press_match"]
            ready = (press and not state["long_press_fired"]
                     and now - state["last_sent"] >= button_cooldown_rate)

            if skipped:
                # Resting value: a press that was being held ends here as a short press.
                if ready:
                    logger.debug(f"Channel {channel} short press: {press[0]}")
                    self.controlQ.put({'control': press[0]})
                    state["last_sent"] = now
                    state["triggered"] = press[0]
                if press is not None:
                    self._log_press_summary(channel, press[0], state, now)
                self._clear_press(state)
                continue

            if action:
                if press is None:
                    state["press_match"] = (action, spec)
                    state["press_start"] = now
                    state["long_press_fired"] = False
                    state["press_min"] = data
                    state["press_max"] = data
                    logger.debug(f"Channel {channel} press start: {action} (raw {data})")
                else:
                    state["press_min"] = min(state["press_min"], data)
                    state["press_max"] = max(state["press_max"], data)
                    if (long_press_threshold is not None and not state["long_press_fired"]
                          and now - (state["press_start"] or now) >= long_press_threshold):
                        logger.debug(f"Channel {channel} long press: {action}_long")
                        self.controlQ.put({'control': action + "_long"})
                        state["long_press_fired"] = True
                        state["last_sent"] = now
                        state["triggered"] = action + "_long"
                continue

            # Unrecognised value. Warn once per episode rather than once per poll:
            # a resting value that has drifted off its configured band sits here
            # indefinitely and would otherwise flood the log.
            if not state["warned"]:
                state["warned"] = True
                suffix = f" (releasing {press[0]})" if ready else ""
                logger.warning(f"Uncaught press on Channel {channel}: {data}{suffix}")
                if press is not None:
                    self._log_press_summary(channel, press[0], state, now)
            self._clear_press(state)

    def rotary_encoder(self, encA, encB):
        Enc_A = encA
        Enc_B = encB

        self.last_A = 1
        self.last_B = 1
        self.last_gpio = 0

        def rotary_interrupt(gpio, level, tim):
            if gpio == Enc_A:
                self.last_A = level
            else:
                self.last_B = level

            if self._capture_active:
                # Controls are paused while the settings page is learning buttons.
                self.last_gpio = gpio
                return

            if gpio != self.last_gpio:  # debounce
                self.last_gpio = gpio
                if gpio == Enc_A and level == 1:
                    if self.last_B == 1:
                        logger.debug('Menu down')
                        self.controlQ.put({'control': 'menu_down'})
                elif gpio == Enc_B and level == 1:
                    if self.last_A == 1:
                        logger.debug('Menu up')
                        self.controlQ.put({'control': 'menu_up'})

        self.pi = pigpio.pi()
        self.pi.set_mode(Enc_A, pigpio.INPUT)
        self.pi.set_pull_up_down(Enc_A, pigpio.PUD_UP)
        self.pi.set_mode(Enc_B, pigpio.INPUT)
        self.pi.set_pull_up_down(Enc_B, pigpio.PUD_UP)
        self.pi.callback(Enc_A, pigpio.EITHER_EDGE, rotary_interrupt)
        self.pi.callback(Enc_B, pigpio.EITHER_EDGE, rotary_interrupt)

        logger.info('Rotary thread start successfully, listening for turns')

    def _run_button_loop(self, mode, read_all, channels, btn_config, btn_skip_config,
                         button_poll_rate, button_debounce_rate, button_cooldown_rate):
        """Poll `read_all` and turn the readings into control events until stopped.

        The two hardware paths differ only in how a channel is read, so
        everything from sampling to the press state machine lives here and each
        caller supplies just its reader.
        """
        poll = max(button_poll_rate / 1000, MIN_POLL)
        debounce = button_debounce_rate / 1000
        cooldown = button_cooldown_rate / 1000

        samples = max(1, self.config.button_samples)
        hysteresis = self.config.button_hysteresis

        parsed_btns = parse_button_config(btn_config)
        parsed_skips = parse_button_config(btn_skip_config)

        logger.info("%s controls polling every %.3fs (median of %d samples)",
                    mode, poll, samples)

        button_states = {channel: self._new_button_state() for channel in channels}

        while not (self.stop_event and self.stop_event.is_set()):
            passes = []
            for _ in range(samples):
                if self.stop_event and self.stop_event.is_set():
                    break
                passes.append(read_all(channels))
            if not passes:
                break
            batch_data = [median_low([p[i] for p in passes]) for i in range(len(channels))]

            self._process_readings(batch_data, channels, button_states, parsed_btns, parsed_skips,
                                   debounce, cooldown,
                                   long_press_threshold=self.config.long_press_threshold,
                                   capture=self._refresh_capture_state(),
                                   hysteresis=hysteresis)

            time.sleep(poll)

        logger.info("Buttons (%s) stopping", mode)

    def buttons(self, butClk, butDOUT, butDIN, butCS, but1, but2, btn_config, btn_skip_config, button_poll_rate, button_debounce_rate, button_cooldown_rate):
        channels = [but1, but2]

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(butClk, GPIO.OUT)
        GPIO.setup(butDOUT, GPIO.IN)
        GPIO.setup(butDIN, GPIO.OUT)
        GPIO.setup(butCS, GPIO.OUT)

        command_map = {ch: (ch | 0x18) << 3 for ch in channels}

        def read_mcp3008(channel):
            GPIO.output(butCS, GPIO.LOW)
            command = command_map[channel]
            for _ in range(5):
                GPIO.output(butDIN, GPIO.HIGH if (command & 0x80) else GPIO.LOW)
                command <<= 1
                GPIO.output(butClk, GPIO.HIGH)
                GPIO.output(butClk, GPIO.LOW)
            value = 0
            for _ in range(10):
                GPIO.output(butClk, GPIO.HIGH)
                GPIO.output(butClk, GPIO.LOW)
                value = (value << 1) | (1 if GPIO.input(butDOUT) else 0)
            GPIO.output(butCS, GPIO.HIGH)
            return value

        def read_all(chs):
            return [read_mcp3008(ch) for ch in chs]

        self._run_button_loop("bitbang", read_all, channels, btn_config, btn_skip_config,
                              button_poll_rate, button_debounce_rate, button_cooldown_rate)

    def buttons_spi(self, spi_bus, butCS, but1, but2, btn_config, btn_skip_config, button_poll_rate=10, button_debounce_rate=50, button_cooldown_rate=500):
        restore_spi0_pinmux()

        spi = spidev.SpiDev()
        spi.open(0, spi_bus)
        spi.no_cs = True  # butCS is driven manually below; don't let the kernel also toggle CE0/CE1
        spi.max_speed_hz = SPI_CLOCK_HZ

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(butCS, GPIO.OUT)

        channels = [but1, but2]

        cmd_bytes = {ch: [1, (8 + ch) << 4, 0] for ch in channels}

        def read_all(chs):
            results = []
            for ch in chs:
                GPIO.output(butCS, GPIO.LOW)
                adc_data = spi.xfer2(list(cmd_bytes[ch]))
                GPIO.output(butCS, GPIO.HIGH)
                results.append(((adc_data[1] & 3) << 8) | adc_data[2])
            return results

        # A pinmux that never took leaves MISO dead, so every raw reading is 0 --
        # a legitimate-looking value at the bottom of the range rather than an
        # obviously broken read, so it is worth calling out explicitly.
        if not any(read_all(channels)):
            logger.warning(
                "Initial SPI read was raw 0 on every channel; the MCP3008 may not be "
                "reachable. Check `raspi-gpio get %s` reports func=ALT0.", SPI0_PINS,
            )

        try:
            self._run_button_loop("SPI", read_all, channels, btn_config, btn_skip_config,
                                  button_poll_rate, button_debounce_rate, button_cooldown_rate)
        finally:
            spi.close()
