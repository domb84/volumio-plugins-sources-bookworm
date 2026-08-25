"""Scrolling audio level display for a 16x2 HD44780-compatible VFD.

Hidden beta feature, reached by long-pressing the dimmer button.

A true oscilloscope trace is impossible here: the controller has only 8
user-definable glyphs (CGRAM), so at most 8 distinct pixel patterns can be on
screen at once, and a scope line needs a different arbitrary pattern in every
column. A *bar* display works because all 16 columns draw from the same 8
glyphs -- bar heights 1/8 to 8/8 -- so this shows the level envelope over time
instead: each column is one recent time slice, scrolling right to left, which
reads much like a waveform.

Audio comes from a raw PCM tap (see contrib/AUDIO_TAP.md). Nothing here touches
the audio path itself -- it only reads a fifo, and does nothing at all when
that fifo is absent.
"""
import array
import logging
import math
import os
import threading
import time
from collections import deque

logger = logging.getLogger("Level Meter")

try:                                    # removed from the stdlib in python 3.13
    import audioop as _audioop
except ImportError:                     # pragma: no cover - depends on runtime
    _audioop = None

LCD_COLUMNS = 16
LCD_ROWS = 2

# CGRAM glyph codes. 0-7 are the only slots the HD44780 has; note 0x0A would be
# "\n", which lcd_render() treats as a line break, so staying inside 0-7 keeps
# frames safe to build as ordinary strings.
BAR_GLYPHS = tuple(range(8))

# Rows of pixels in one character cell. Two rows of cells therefore give 16
# vertical steps for a column.
CELL_ROWS = 8
MAX_LEVEL = CELL_ROWS * LCD_ROWS

# Raw PCM tap. Signed 16-bit little-endian stereo, which is what the ALSA
# volumiofifo hook produces.
DEFAULT_FIFO_PATH = "/tmp/retrotuner-audio.fifo"
SAMPLE_BYTES = 2
CHANNELS = 2

# Bytes pulled per frame. At 44.1kHz stereo S16, 1024 frames is ~23ms of audio,
# which is about the right window for a 20fps display and keeps the read small
# enough that a stalled fifo cannot block for long.
READ_FRAMES = 1024
READ_BYTES = READ_FRAMES * SAMPLE_BYTES * CHANNELS

FRAME_INTERVAL = 0.05          # 20fps ceiling; the display cannot keep up with more
SILENCE_TIMEOUT = 2.0          # after this long with no audio, show the idle message

# Peak sample value for S16. Levels are scaled against this.
FULL_SCALE = 32767.0

# Below this the input is treated as silence rather than drawn as a 1-pixel bar,
# which otherwise leaves a permanent line of dots across the display.
SILENCE_FLOOR = 0.005


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


def _peak_raw(pcm_bytes):
    """Largest absolute sample in an S16_LE buffer.

    audioop does this in C. It matters more than it looks: scanning a frame's
    worth of samples in interpreted Python costs ~80x more, on a Pi, in the same
    process and under the same GIL as the SPI button polling. audioop was
    removed in python 3.13, hence the fallback.
    """
    if _audioop is not None:
        return _audioop.max(pcm_bytes, SAMPLE_BYTES)

    samples = array.array('h')
    samples.frombytes(pcm_bytes)
    peak = 0
    for sample in samples:
        if sample < 0:
            sample = -sample
        if sample > peak:
            peak = sample
    return peak


def peak_level(pcm_bytes):
    """Peak amplitude of a raw S16_LE buffer, as 0.0-1.0.

    Peak rather than RMS: on a 16-step display RMS reads as an almost static
    mid-level bar, while peak actually moves with the music.
    """
    if not pcm_bytes:
        return 0.0

    # Both paths need a whole number of samples; a short read from the fifo can
    # leave a trailing odd byte.
    usable = len(pcm_bytes) - (len(pcm_bytes) % SAMPLE_BYTES)
    if usable <= 0:
        return 0.0

    return min(_peak_raw(pcm_bytes[:usable]) / FULL_SCALE, 1.0)


def scale_level(level):
    """Map a 0.0-1.0 amplitude onto 0..MAX_LEVEL pixel rows, logarithmically.

    Linear scaling wastes most of the display: normal listening levels sit in
    the bottom couple of steps and everything looks flat. This is roughly a dB
    scale over a 40dB window, which spreads real music across the full height.
    """
    if level <= SILENCE_FLOOR:
        return 0

    db = 20.0 * math.log10(level)
    normalised = (db + 40.0) / 40.0        # -40dB -> 0.0, 0dB -> 1.0
    normalised = max(0.0, min(normalised, 1.0))
    return max(1, int(round(normalised * MAX_LEVEL)))


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
    """Reads the PCM tap and drives the display until stopped.

    Constructing this touches nothing; ``start()`` opens the fifo and spawns the
    render thread, ``stop()`` puts the menu back.
    """

    def __init__(self, menu, fifo_path=DEFAULT_FIFO_PATH, on_stop=None):
        self._menu = menu
        self._fifo_path = fifo_path
        self._on_stop = on_stop
        self._thread = None
        self._stop = threading.Event()
        self._glyphs_loaded = False
        self._history = deque([0] * LCD_COLUMNS, maxlen=LCD_COLUMNS)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return False
        if not self._fifo_available():
            logger.warning("No audio tap at %s; not starting the level meter",
                           self._fifo_path)
            self._menu.message("NO AUDIO TAP".ljust(LCD_COLUMNS))
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

    def _fifo_available(self):
        try:
            return os.path.exists(self._fifo_path)
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
            # Non-blocking: a fifo with no writer would otherwise block open()
            # until something starts playing, leaving the button press dead.
            fd = os.open(self._fifo_path, os.O_RDONLY | os.O_NONBLOCK)
            handle = os.fdopen(fd, 'rb', 0)

            while not self._stop.is_set():
                started = time.monotonic()

                try:
                    chunk = handle.read(READ_BYTES)
                except (BlockingIOError, InterruptedError):
                    chunk = None
                except OSError as e:
                    logger.warning("Audio tap read failed: %s", e)
                    break

                level = peak_level(chunk) if chunk else 0.0
                if level > SILENCE_FLOOR:
                    last_sound = started

                self._history.append(scale_level(level))

                if started - last_sound > SILENCE_TIMEOUT:
                    self._menu.message("NO AUDIO".ljust(LCD_COLUMNS))
                else:
                    self._menu.render_frame(render_columns(self._history))

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
