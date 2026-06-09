import RPi.GPIO as GPIO
import pigpio
import spidev
import json
import os
import threading
import time
import logging
from dataclasses import dataclass, field
from queue import Queue
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("Controls")
from .utils import parse_button_config

# Capture ("learn") mode: while the flag file exists the settings page is asking
# us to report raw button readings instead of acting on them.
CAPTURE_FLAG_PATH = "/tmp/teac-dab-controls-capture-on"
CAPTURE_READING_PATH = "/tmp/teac-dab-controls-capture.json"

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

class Controls:
    """Handle rotary encoder and MCP3008 button inputs.

    This class starts pigpio callbacks for the rotary encoder and runs a
    polling loop for MCP3008 button inputs (either bitbanged or via SPI).
    """

    def __init__(self, controlQ: Queue, config: ControlsConfig, stop_event: Optional[threading.Event] = None) -> None:
        logger.debug("Loading controls")
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


    def normalize_value(self, value, min_value, max_value, target_range):
        """Normalize sensor `value` into integer in [0, target_range)."""
        normalized_value = 1 - (value - min_value) / (max_value - min_value)
        scaled_value = normalized_value * target_range
        return int(scaled_value)

    @staticmethod
    def _lookup_button(
        channel: int,
        data: int,
        parsed_btns: List,
        parsed_skips: List,
    ) -> Tuple[bool, Optional[str]]:
        """Return (is_skipped, action_name).

        is_skipped=True  → value is in a skip range; suppress silently
        action_name=str  → matched button action to fire
        action_name=None → no match found; caller should log a warning
        """
        for _name, ch, spec in parsed_skips:
            if ch == channel:
                if (spec[0] == 'range' and spec[1] <= data <= spec[2]) or \
                   (spec[0] != 'range' and spec[1] == data):
                    return True, None

        for name, ch, spec in parsed_btns:
            if ch == channel:
                if (spec[0] == 'range' and spec[1] <= data <= spec[2]) or \
                   (spec[0] != 'range' and spec[1] == data):
                    return False, name

        return False, None

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

    def _handle_capture_reading(self, channel: int, value: int) -> None:
        """Detect press events for the settings-page learn flow.

        The first stable value seen on a channel is taken as its resting
        (no-press) baseline. A press is any stable value differing from that
        baseline; we publish once per press (on the resting->pressed edge) so the
        settings page sees one event per physical press regardless of how often
        it polls.
        """
        baseline = self._capture_baseline.get(channel)
        if baseline is None:
            self._capture_baseline[channel] = value
            self._capture_pressed[channel] = False
            return
        if value == baseline:
            self._capture_pressed[channel] = False  # released
            return
        if not self._capture_pressed.get(channel):
            self._capture_pressed[channel] = True
            self._capture_seq += 1
            self._publish_capture_reading(channel, value, self._capture_seq)

    def _publish_capture_reading(self, channel: int, value: int, seq: int) -> None:
        """Publish a detected press so the settings page can learn a button value."""
        try:
            with open(CAPTURE_READING_PATH, "w") as handle:
                json.dump({"channel": channel, "value": value, "seq": seq}, handle)
        except Exception as e:
            logger.debug("Could not publish capture reading: %s", e)

    def _process_readings(self, batch_data, channels, button_states, parsed_btns, parsed_skips,
                          button_debounce_rate, button_cooldown_rate, capture=False):
        """Apply debounce/cooldown to a batch of ADC readings and emit button actions.

        In capture mode each distinct stable reading is published for the
        settings page (so a button's value can be learned) and the normal action
        is suppressed so pressing buttons doesn't navigate the menu.
        """
        for data, channel in zip(batch_data, channels):
            data = self.normalize_value(data, 0, 1024, 32)
            state = button_states[channel]
            now = time.monotonic()

            if data != state["last_value"]:
                state["stable_since"] = now
                state["last_value"] = data
            elif now - (state["stable_since"] or 0) >= button_debounce_rate:
                if capture:
                    self._handle_capture_reading(channel, data)
                    continue
                if now - state["last_sent"] < button_cooldown_rate:
                    continue
                logger.debug(f"Channel {channel} stable value: {data}")
                skipped, action = self._lookup_button(channel, data, parsed_btns, parsed_skips)
                if skipped:
                    continue
                if action:
                    self.controlQ.put({'control': action})
                    state["last_sent"] = now
                else:
                    logger.warning(f"Uncaught press on Channel {channel}: {data}")

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

    def buttons(self, butClk, butDOUT, butDIN, butCS, but1, but2, btn_config, btn_skip_config, button_poll_rate, button_debounce_rate, button_cooldown_rate):
        CLK = butClk
        DOUT = butDOUT
        DIN = butDIN
        CS = butCS

        channels = [but1, but2]

        button_poll_rate /= 1000
        button_debounce_rate /= 1000
        button_cooldown_rate /= 1000

        MIN_POLL = 0.05
        button_poll_rate = max(button_poll_rate, MIN_POLL) if button_poll_rate > 0 else MIN_POLL

        logger.info("Bitbanged controls polling every %.3fs", button_poll_rate)

        button_states = {
            channel: {"last_value": None, "stable_since": None, "last_sent": 0.0}
            for channel in channels
        }

        parsed_btns = parse_button_config(btn_config)
        parsed_skips = parse_button_config(btn_skip_config)

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(CLK, GPIO.OUT)
        GPIO.setup(DOUT, GPIO.IN)
        GPIO.setup(DIN, GPIO.OUT)
        GPIO.setup(CS, GPIO.OUT)

        command_map = {ch: (ch | 0x18) << 3 for ch in channels}

        def read_mcp3008(channel):
            GPIO.output(CS, GPIO.LOW)
            command = command_map[channel]
            for _ in range(5):
                GPIO.output(DIN, GPIO.HIGH if (command & 0x80) else GPIO.LOW)
                command <<= 1
                GPIO.output(CLK, GPIO.HIGH)
                GPIO.output(CLK, GPIO.LOW)
            value = 0
            for _ in range(10):
                GPIO.output(CLK, GPIO.HIGH)
                GPIO.output(CLK, GPIO.LOW)
                value = (value << 1) | (1 if GPIO.input(DOUT) else 0)
            GPIO.output(CS, GPIO.HIGH)
            return value

        while not (self.stop_event and self.stop_event.is_set()):
            batch_data = []
            for channel in channels:
                if self.stop_event and self.stop_event.is_set():
                    break
                batch_data.append(read_mcp3008(channel))

            self._process_readings(batch_data, channels, button_states, parsed_btns, parsed_skips,
                                   button_debounce_rate, button_cooldown_rate,
                                   capture=self._refresh_capture_state())

            time.sleep(button_poll_rate)

        logger.info('Buttons (bitbang) stopping')

    def buttons_spi(self, spi_bus, butCS, but1, but2, btn_config, btn_skip_config, button_poll_rate=10, button_debounce_rate=50, button_cooldown_rate=500):
        spi = spidev.SpiDev()
        spi.open(0, spi_bus)
        spi.max_speed_hz = 1000000

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(butCS, GPIO.OUT)

        channels = [but1, but2]

        button_poll_rate = max(button_poll_rate / 1000, 0.05)
        button_debounce_rate /= 1000
        button_cooldown_rate /= 1000

        logger.info("SPI controls polling every %.3fs", button_poll_rate)

        button_states = {
            channel: {"last_value": None, "stable_since": None, "last_sent": 0.0}
            for channel in channels
        }

        parsed_btns = parse_button_config(btn_config)
        parsed_skips = parse_button_config(btn_skip_config)

        cmd_bytes = {ch: [1, (8 + ch) << 4, 0] for ch in channels}

        def _read_all_channels_spi(ch_list):
            GPIO.output(butCS, GPIO.LOW)
            results = []
            for ch in ch_list:
                adc_data = spi.xfer2(cmd_bytes[ch])
                adc_value = ((adc_data[1] & 3) << 8) | adc_data[2]
                results.append(adc_value)
            GPIO.output(butCS, GPIO.HIGH)
            return results

        while not (self.stop_event and self.stop_event.is_set()):
            batch_data = _read_all_channels_spi(channels)

            self._process_readings(batch_data, channels, button_states, parsed_btns, parsed_skips,
                                   button_debounce_rate, button_cooldown_rate,
                                   capture=self._refresh_capture_state())

            time.sleep(button_poll_rate)

        spi.close()
        logger.info('Buttons (SPI) stopping')
