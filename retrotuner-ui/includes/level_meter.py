"""Spectrum display for a 16x2 HD44780-compatible VFD.

Hidden beta feature, reached by long-pressing the dimmer button.

All the signal processing is done by cava (see cava/retrotuner-cava.conf), which
reads the raw audio tap and writes one line of 16 numbers per frame. This module
only turns those numbers into characters, which is why there is no DSP here.

That split is deliberate rather than lazy:

  * cava holds the tap fifo open permanently. An unread fifo fills its 64kB
    buffer in ~0.37s, at which point ALSA blocks on write and playback stops --
    so whatever reads the tap must never pause. cava is a separate service, so
    restarting this plugin (which happens on every settings save) cannot
    interrupt audio.
  * the FFT, log frequency scaling and falloff happen in C, not here, next to
    the SPI button polling and under the same GIL.

A true oscilloscope trace remains impossible on this hardware: the controller
has only 8 user-definable glyphs, so at most 8 distinct pixel patterns can be on
screen at once and a scope line needs a different one per column. Bars work
because all 16 columns share the same glyph set.
"""
import logging
import os
import threading
import time

logger = logging.getLogger("Level Meter")

LCD_COLUMNS = 16
LCD_ROWS = 2

# CGRAM glyph codes. 0-7 are the only slots the HD44780 has; note 0x0A would be
# "\n", which lcd_render() treats as a line break, so staying inside 0-7 keeps
# frames safe to build as ordinary strings.
BAR_GLYPHS = tuple(range(8))

# Rows of pixels in one character cell. Two rows of cells give 16 vertical steps
# per column, which is what cava's ascii_max_range is set to.
CELL_ROWS = 8
MAX_LEVEL = CELL_ROWS * LCD_ROWS

# cava's raw ascii output: one line per frame, values separated by ";".
DEFAULT_BARS_PATH = "/tmp/retrotuner-bars"
BAR_SEPARATOR = ';'

FRAME_INTERVAL = 0.05          # matches cava's framerate; 20fps
SILENCE_TIMEOUT = 2.0          # after this long with every bar at zero, say so


def bar_bitmaps():
    """The 8 CGRAM glyphs: bars filled from the bottom up, 1 to 8 rows.

    Glyph ``n`` is a bar (n + 1) pixels tall, so a column needing ``h`` pixels
    (1..8) draws ``chr(h - 1)``.
    """
    glyphs = []
    for height in range(1, CELL_ROWS + 1):
        rows = [0b00000] * (CELL_ROWS - height) + [0b11111] * height
        glyphs.append(rows)
    return glyphs


def parse_bars(line):
    """Turn one line of cava raw ascii output into a list of column levels.

    cava emits "3;7;12;16;..." with a trailing separator, and is configured with
    ascii_max_range to match the display, so the values need no scaling. Short,
    long or malformed lines are tolerated: a partially written line is normal
    when reading a fifo that a separate process is appending to.
    """
    if not line:
        return []

    levels = []
    for field in line.strip().split(BAR_SEPARATOR):
        if not field:
            continue
        try:
            value = int(field)
        except ValueError:
            continue                      # partial write; drop the fragment
        levels.append(max(0, min(value, MAX_LEVEL)))

    return levels[:LCD_COLUMNS]


def render_columns(levels):
    """Build a "<line1>\\n<line2>" frame from per-column levels (0..MAX_LEVEL).

    Column heights are drawn bottom-up across two rows: the lower row fills
    first, then the upper one.
    """
    top = []
    bottom = []
    for level in levels:
        level = max(0, min(level, MAX_LEVEL))
        if level == 0:
            bottom.append(' ')
            top.append(' ')
        elif level <= CELL_ROWS:
            bottom.append(chr(BAR_GLYPHS[level - 1]))
            top.append(' ')
        else:
            bottom.append(chr(BAR_GLYPHS[CELL_ROWS - 1]))
            top.append(chr(BAR_GLYPHS[level - CELL_ROWS - 1]))

    return '%s\n%s' % (''.join(top).ljust(LCD_COLUMNS),
                       ''.join(bottom).ljust(LCD_COLUMNS))


class LevelMeter:
    """Draws cava's output on the display until stopped.

    Nothing here touches the audio path: cava owns the tap, and this only reads
    the small numbers it publishes. Stopping, crashing or restarting this has no
    effect on playback.
    """

    def __init__(self, menu, bars_path=DEFAULT_BARS_PATH, on_stop=None):
        self._menu = menu
        self._bars_path = bars_path
        self._on_stop = on_stop
        self._thread = None
        self._stop = threading.Event()
        self._glyphs_loaded = False

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return False
        if not self._bars_available():
            logger.warning("No cava output at %s; is retrotuner-cava running?",
                           self._bars_path)
            self._menu.message("NO ANALYSER".ljust(LCD_COLUMNS))
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="LevelMeterThread",
                                        daemon=True)
        self._thread.start()
        logger.info("Level meter started")
        return True

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        logger.info("Level meter stopped")

    def toggle(self):
        if self.running:
            self.stop()
            return False
        return self.start()

    def _bars_available(self):
        try:
            return os.path.exists(self._bars_path)
        except Exception:
            return False

    def _load_glyphs(self):
        if self._glyphs_loaded:
            return
        for location, bitmap in enumerate(bar_bitmaps()):
            self._menu.create_char(location, bitmap)
        self._glyphs_loaded = True

    def _run(self):
        handle = None
        last_sound = time.monotonic()
        try:
            self._load_glyphs()
            # Non-blocking: cava may not have opened its end yet, and a blocking
            # open would hang the button press until it did.
            fd = os.open(self._bars_path, os.O_RDONLY | os.O_NONBLOCK)
            handle = os.fdopen(fd, 'r', buffering=1, errors='replace')

            while not self._stop.is_set():
                started = time.monotonic()

                # Drain to the newest complete line: cava writes faster than the
                # display can keep up, and showing a stale frame is worse than
                # skipping to the current one.
                latest = None
                while True:
                    try:
                        line = handle.readline()
                    except (BlockingIOError, InterruptedError):
                        break
                    if not line:
                        break
                    if line.endswith('\n'):
                        latest = line

                if latest is not None:
                    levels = parse_bars(latest)
                    if levels:
                        if any(levels):
                            last_sound = started
                        self._menu.render_frame(render_columns(levels))

                if started - last_sound > SILENCE_TIMEOUT:
                    self._menu.message("NO AUDIO".ljust(LCD_COLUMNS))

                remaining = FRAME_INTERVAL - (time.monotonic() - started)
                if remaining > 0:
                    self._stop.wait(remaining)
        except Exception as e:
            logger.error("Level meter stopped on error: %s", e)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            if self._on_stop is not None:
                try:
                    self._on_stop()
                except Exception as e:
                    logger.debug("Level meter on_stop failed: %s", e)
