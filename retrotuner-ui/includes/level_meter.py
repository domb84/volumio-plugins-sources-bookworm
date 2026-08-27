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
import select
import threading
import time

logger = logging.getLogger("Level Meter")

LCD_COLUMNS = 16
LCD_ROWS = 2

# Rows of pixels in one character cell. Two rows of cells give 16 vertical steps
# per column, which is what cava's ascii_max_range is set to.
CELL_ROWS = 8
MAX_LEVEL = CELL_ROWS * LCD_ROWS

# Display modes. The first two stack both rows into one tall bar and differ only
# in what cava sends; the "rows" pair give each channel a row of its own.
MODE_MONO = 'mono'                # 16 bands, full height
MODE_STEREO = 'stereo'            # cava mirrors L/R about the centre, 8 bands each
MODE_ROWS_EDGES = 'rows_edges'    # L hangs from the top, R rises from the bottom
MODE_ROWS_CENTRE = 'rows_centre'  # L grows up from the middle, R grows down
SUPPORTED_MODES = (MODE_MONO, MODE_STEREO, MODE_ROWS_EDGES, MODE_ROWS_CENTRE)
SPLIT_MODES = (MODE_ROWS_EDGES, MODE_ROWS_CENTRE)

# Vertical steps per channel in the split modes, and the hard reason for it.
#
# Each channel gets one character row, so its bar is drawn inside a single cell
# and needs its own glyph per height. One row grows downward from the top of the
# cell and the other upward from the bottom, and those are different bitmaps --
# a top-anchored bar is not an upside-down bottom-anchored one that the
# controller could flip, because the controller cannot flip anything.
#
# That means two glyph sets out of the eight CGRAM slots that exist, so four
# heights each. The trade against the full-height modes is deliberate and the
# whole point of this layout: 16 frequency bands per channel instead of 8, paid
# for with 4 amplitude steps instead of 16.
SPLIT_LEVELS = 4
SPLIT_CELL_STEP = CELL_ROWS // SPLIT_LEVELS   # pixel rows added per level

# CGRAM glyph codes. 0-7 are the only slots the HD44780 has; note 0x0A would be
# "\n", which lcd_render() treats as a line break, so staying inside 0-7 keeps
# frames safe to build as ordinary strings.
#
# The two split sets are disjoint, so both fit in CGRAM at the same time.
BAR_GLYPHS = tuple(range(8))                  # full-height modes: heights 1..8
SPLIT_UP_GLYPHS = tuple(range(0, 4))          # anchored to the bottom of the cell
SPLIT_DOWN_GLYPHS = tuple(range(4, 8))        # anchored to the top of the cell

# cava's raw ascii output: one line per frame, values separated by ";".
DEFAULT_BARS_PATH = "/tmp/retrotuner-bars"
BAR_SEPARATOR = ';'

# One frame is ~50 bytes, so this holds a healthy backlog without a big read.
READ_CHUNK = 4096
# Guard against a stream that never contains a newline (wrong output format, or
# something other than cava writing) growing the buffer forever.
MAX_PENDING = 64 * 1024

# Default draw rate. Must match "framerate" in cava/retrotuner-cava.conf, which
# ships with the same value -- see the note there. Both are overridden together
# from the plugin settings: index.js rewrites the cava config and index.py
# passes the rate down here, so nothing reads these two once configured.
FRAMES_PER_SECOND = 60
FRAME_INTERVAL = 1.0 / FRAMES_PER_SECOND

# Offered in the settings UI. A doubling series across the useful range: 15 is
# the escape hatch if the meter ever competes with button polling, 120 is close
# to what the display can be driven at -- a frame takes ~2.7ms to write, so 120
# already spends a third of wall time mid-render for no more detail than 60
# gives on a 16-step bar.
SUPPORTED_FRAME_RATES = (15, 30, 60, 120)
# How long every bar must sit at zero before the display says so. Measured from
# the last frame with any signal in it, so what you actually see is longer than
# this: cava's bars fall gradually rather than cutting out, and those few seconds
# of decay count as sound. At 2.0 the notice appeared after roughly five seconds
# of quiet; expect this to read as about thirteen.
#
# Erring long is deliberate. Between tracks, during a quiet passage, or while a
# stream rebuffers, the audio really has stopped -- but saying "NO AUDIO" and
# then snapping back to bars a moment later is worse than simply showing an
# empty meter for a while.
SILENCE_TIMEOUT = 10.0


def frame_interval(frame_rate):
    """Seconds per frame for ``frame_rate``, falling back to the default.

    Deliberately tolerant. The rate arrives from a settings file that can be
    hand-edited or left behind by an older version, and a bad value here would
    either divide by zero or spin the draw loop flat out against the display --
    neither is worth crashing the meter over, let alone taking down the thread
    that also owns the menu.
    """
    try:
        rate = int(frame_rate)
    except (TypeError, ValueError):
        rate = 0
    if rate <= 0:
        logger.warning("Ignoring invalid frame rate %r; using %d fps",
                       frame_rate, FRAMES_PER_SECOND)
        rate = FRAMES_PER_SECOND
    return 1.0 / rate


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


def split_bitmaps():
    """The 8 CGRAM glyphs for the split modes: 4 anchored down, 4 anchored up.

    Slots 0-3 are bars rising from the bottom of the cell, 4-7 the same heights
    hanging from the top. Both sets are needed at once because the two rows grow
    towards each other (or away from each other), and the controller has no way
    to mirror a glyph.
    """
    glyphs = []
    for height in range(SPLIT_CELL_STEP, CELL_ROWS + 1, SPLIT_CELL_STEP):
        glyphs.append([0b00000] * (CELL_ROWS - height) + [0b11111] * height)
    for height in range(SPLIT_CELL_STEP, CELL_ROWS + 1, SPLIT_CELL_STEP):
        glyphs.append([0b11111] * height + [0b00000] * (CELL_ROWS - height))
    return glyphs


def split_channels(levels):
    """Split one cava stereo frame into (left, right) columns.

    cava emits the two channels as one array laid out for a mirrored display:
    the first half is the left channel running high frequency to low, so that
    bass meets in the centre, and the second half is the right channel the usual
    way round. Reversing the first half puts both channels back into low-to-high
    order, which is what a row of its own wants.

    If the display comes out with the channels swapped or the spectrum running
    backwards, this is the only place that decides it.
    """
    half = len(levels) // 2
    if half == 0:
        return [], []
    return list(reversed(levels[:half])), list(levels[half:])


def render_split(left, right, from_edges):
    """Build a frame with one channel per row, each drawn inside a single cell.

    ``from_edges`` picks which way the bars grow. True anchors zero at the outer
    edges, so the left channel hangs down from the top and the right rises up
    from the bottom and a loud signal closes the gap in the middle. False anchors
    zero at the centre line, so both grow outwards towards the edges.

    Only the glyph set each row uses changes between the two.
    """
    top_glyphs = SPLIT_DOWN_GLYPHS if from_edges else SPLIT_UP_GLYPHS
    bottom_glyphs = SPLIT_UP_GLYPHS if from_edges else SPLIT_DOWN_GLYPHS

    def row(levels, glyphs):
        cells = []
        for level in levels[:LCD_COLUMNS]:
            level = max(0, min(int(level), SPLIT_LEVELS))
            cells.append(' ' if level == 0 else chr(glyphs[level - 1]))
        return ''.join(cells).ljust(LCD_COLUMNS)

    return '%s\n%s' % (row(left, top_glyphs), row(right, bottom_glyphs))


def parse_bars(line, max_level=MAX_LEVEL, columns=LCD_COLUMNS):
    """Turn one line of cava raw ascii output into a list of column levels.

    cava emits "3;7;12;16;..." with a trailing separator, and is configured with
    ascii_max_range to match the display, so the values need no scaling. Short,
    long or malformed lines are tolerated: a partially written line is normal
    when reading a fifo that a separate process is appending to.

    ``max_level`` and ``columns`` differ by mode: the full-height modes take 16
    columns of 0..16, the split modes take 32 columns -- both channels -- of
    0..4. Both mirror what the mode writes into cava's config, so neither end
    scales anything.
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
        levels.append(max(0, min(value, max_level)))

    return levels[:columns]


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

    def __init__(self, menu, bars_path=DEFAULT_BARS_PATH, on_stop=None,
                 frame_rate=FRAMES_PER_SECOND, mode=MODE_MONO):
        self._menu = menu
        self._bars_path = bars_path
        self._on_stop = on_stop
        self._frame_interval = frame_interval(frame_rate)
        self._mode = mode if mode in SUPPORTED_MODES else MODE_MONO
        if mode not in SUPPORTED_MODES:
            logger.warning("Unknown meter mode %r; using %s", mode, MODE_MONO)
        self._thread = None
        self._stop = threading.Event()
        self._glyphs_loaded = False

    @property
    def _split(self):
        return self._mode in SPLIT_MODES

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, announce=True):
        """Start drawing. Returns True if the thread was started.

        ``announce=False`` keeps a failed start silent. The idle timeout starts
        the meter without being asked, so if cava happens to be down it must not
        paint an error over whatever the user was last looking at -- a button
        press, which did ask, still says why nothing happened.
        """
        if self.running:
            return False
        if not self._bars_available():
            logger.warning("No cava output at %s; is retrotuner-cava running?",
                           self._bars_path)
            if announce:
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
        """Fill CGRAM with the glyph set this mode draws from.

        Loaded once per run rather than per frame: all 8 slots are rewritten,
        and the mode cannot change without a settings save, which restarts the
        service anyway.
        """
        if self._glyphs_loaded:
            return
        bitmaps = split_bitmaps() if self._split else bar_bitmaps()
        for location, bitmap in enumerate(bitmaps):
            self._menu.create_char(location, bitmap)
        self._glyphs_loaded = True

    def _build_frame(self, line):
        """Turn one cava line into a frame, or None if it carried no levels."""
        if self._split:
            levels = parse_bars(line, max_level=SPLIT_LEVELS,
                                columns=LCD_COLUMNS * 2)
            if not levels:
                return None, levels
            left, right = split_channels(levels)
            return render_split(left, right,
                                from_edges=self._mode == MODE_ROWS_EDGES), levels

        levels = parse_bars(line)
        if not levels:
            return None, levels
        return render_columns(levels), levels

    def _run(self):
        fd = None
        pending = b''
        last_sound = time.monotonic()
        showing_silence = False
        try:
            self._load_glyphs()
            # Non-blocking: cava may not have opened its end yet, and a blocking
            # open would hang the button press until it did.
            #
            # Read the raw descriptor rather than wrapping it in a file object:
            # a non-blocking stream returns None when no data is ready, which
            # TextIOWrapper.readline() cannot represent -- it raises or returns
            # "" for "nothing yet", indistinguishable from end of stream.
            fd = os.open(self._bars_path, os.O_RDONLY | os.O_NONBLOCK)

            while not self._stop.is_set():
                started = time.monotonic()

                ready, _, _ = select.select([fd], [], [], 0)
                if ready:
                    try:
                        chunk = os.read(fd, READ_CHUNK)
                    except (BlockingIOError, InterruptedError):
                        chunk = b''
                    if chunk:
                        pending += chunk

                # cava writes faster than the display can be redrawn, so take
                # the newest complete line and discard the backlog; showing a
                # stale frame is worse than skipping to the current one.
                latest = None
                if b'\n' in pending:
                    parts = pending.split(b'\n')
                    pending = parts[-1]          # keep the partial tail
                    for line in reversed(parts[:-1]):
                        if line.strip():
                            latest = line.decode('ascii', 'replace')
                            break

                # A stream with no newline is either not cava or badly broken;
                # don't let the buffer grow without bound waiting for one.
                if len(pending) > MAX_PENDING:
                    pending = b''

                frame, levels = (self._build_frame(latest)
                                 if latest is not None else (None, None))
                if levels and any(levels):
                    last_sound = started

                # Exactly one write per frame, and the silence notice is latched
                # rather than resent. Previously both branches could fire in the
                # same frame -- bars drawn directly, then "NO AUDIO" queued over
                # the top 20 times a second, which reads as a violent flicker.
                if started - last_sound > SILENCE_TIMEOUT:
                    if not showing_silence:
                        self._menu.message("NO AUDIO".ljust(LCD_COLUMNS))
                        showing_silence = True
                elif frame is not None:
                    showing_silence = False
                    self._menu.render_frame(frame)

                remaining = self._frame_interval - (time.monotonic() - started)
                if remaining > 0:
                    self._stop.wait(remaining)
        except Exception as e:
            logger.error("Level meter stopped on error: %s", e)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            if self._on_stop is not None:
                try:
                    self._on_stop()
                except Exception as e:
                    logger.debug("Level meter on_stop failed: %s", e)
