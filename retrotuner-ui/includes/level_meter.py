"""Spectrum display for a 16x2 HD44780-compatible VFD.

Draws the numbers cava publishes; all the signal processing is cava's. See
NOTES.md ("Level meter") for the glyph budget that shapes everything here, and
("cava") for why the analysis lives in a separate service.
"""
import logging
import os
import select
import threading
import time

logger = logging.getLogger("Level Meter")

LCD_COLUMNS = 16
LCD_ROWS = 2

# Pixel rows per character cell; two cells give the 16 steps cava's ascii_max_range sends.
CELL_ROWS = 8
MAX_LEVEL = CELL_ROWS * LCD_ROWS

# The first two stack both rows into one tall bar; the "rows" pair give each channel a row.
MODE_MONO = 'mono'                # 16 bands, full height
MODE_STEREO = 'stereo'            # cava mirrors L/R about the centre, 8 bands each
MODE_ROWS_EDGES = 'rows_edges'    # L hangs from the top, R rises from the bottom
MODE_ROWS_CENTRE = 'rows_centre'  # L grows up from the middle, R grows down
SUPPORTED_MODES = (MODE_MONO, MODE_STEREO, MODE_ROWS_EDGES, MODE_ROWS_CENTRE)
SPLIT_MODES = (MODE_ROWS_EDGES, MODE_ROWS_CENTRE)

# Split modes need two glyph sets (one up, one down) in 8 CGRAM slots, so four each. See NOTES.md.
SPLIT_LEVELS = 4
SPLIT_CELL_STEP = CELL_ROWS // SPLIT_LEVELS   # pixel rows added per level

# The only CGRAM slots there are - and staying inside them keeps 0x0A ("\n") out of frames.
BAR_GLYPHS = tuple(range(8))                  # full-height modes: heights 1..8
SPLIT_UP_GLYPHS = tuple(range(0, 4))          # anchored to the bottom of the cell
SPLIT_DOWN_GLYPHS = tuple(range(4, 8))        # anchored to the top of the cell

# cava's raw ascii output: one line per frame, values separated by ";".
DEFAULT_BARS_PATH = "/tmp/retrotuner-bars"
BAR_SEPARATOR = ';'

# One frame is ~50 bytes, so this holds a healthy backlog without a big read.
READ_CHUNK = 4096
# Bound on a stream that never sends a newline - wrong format, or not cava at all.
MAX_PENDING = 64 * 1024

# Must match "framerate" in cava/retrotuner-cava.conf; the settings page sets both together.
FRAMES_PER_SECOND = 60
FRAME_INTERVAL = 1.0 / FRAMES_PER_SECOND

# 15 is the escape hatch if the meter competes with button polling; 120 is near the display's ceiling.
SUPPORTED_FRAME_RATES = (15, 30, 60, 120)
# Quiet before "NO AUDIO", and long on purpose - a false alarm between tracks is worse.
SILENCE_TIMEOUT = 10.0


def frame_interval(frame_rate):
    """Seconds per frame, falling back to the default on a nonsense setting.

    Tolerant because the value comes from a hand-editable file, and zero or
    negative would divide by zero or spin the draw loop against the display.
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
    """The 8 split-mode glyphs: slots 0-3 rise from the bottom, 4-7 hang from
    the top. Both sets are resident because the controller cannot mirror one."""
    glyphs = []
    for height in range(SPLIT_CELL_STEP, CELL_ROWS + 1, SPLIT_CELL_STEP):
        glyphs.append([0b00000] * (CELL_ROWS - height) + [0b11111] * height)
    for height in range(SPLIT_CELL_STEP, CELL_ROWS + 1, SPLIT_CELL_STEP):
        glyphs.append([0b11111] * height + [0b00000] * (CELL_ROWS - height))
    return glyphs


def split_channels(levels):
    """Split one cava stereo frame into (left, right) columns.

    cava sends the left channel first and reversed, for a mirrored display; this
    puts both back in low-to-high order. If the channels come out swapped or the
    spectrum runs backwards, this is the only place that decides it.
    """
    half = len(levels) // 2
    if half == 0:
        return [], []
    return list(reversed(levels[:half])), list(levels[half:])


def render_split(left, right, from_edges):
    """Build a frame with one channel per row, left on top.

    ``from_edges`` anchors zero at the outer edges, so the bars grow inwards and
    a loud signal closes the gap in the middle; otherwise zero is the centre
    line and they grow outwards. Only the glyph set each row uses differs.
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

    cava emits "3;7;12;16;..." with a trailing separator, already scaled to the
    display by ascii_max_range. A partially written line is normal when reading
    a file another process is appending to, so malformed fields are dropped
    rather than losing the frame.

    ``max_level`` and ``columns`` differ by mode -- see the table in NOTES.md.
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

        ``announce=False`` keeps a failed start silent, for the idle timeout:
        nobody asked for the meter, so a missing cava must not paint an error
        over whatever was on screen.
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

    def _resync_display(self):
        """Re-run the display's 4-bit handshake at a meter boundary.

        Not the safety net -- the library resyncs periodically on its own. This
        covers the two moments most exposed to a desync, and where a lost frame
        costs nothing: 60fps about to start, and the menu about to come back
        after it. See NOTES.md ("Display driver").
        """
        resync = getattr(self._menu, 'resync_display', None)
        if resync is None:
            # Older rpi-lcd-menu without the recovery path; see requirements.txt.
            return
        try:
            resync()
        except Exception as e:
            logger.debug("Display resync failed: %s", e)

    def _load_glyphs(self):
        """Fill CGRAM with this mode's glyph set. Once per run: the mode cannot
        change without a settings save, which restarts the service."""
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
            self._resync_display()
            self._load_glyphs()
            # Non-blocking, and a raw fd: readline() can't say "nothing yet" at end of file.
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

                # Newest complete line only - a stale frame is worse than a skipped one.
                latest = None
                if b'\n' in pending:
                    parts = pending.split(b'\n')
                    pending = parts[-1]          # keep the partial tail
                    for line in reversed(parts[:-1]):
                        if line.strip():
                            latest = line.decode('ascii', 'replace')
                            break

                # Not cava, or badly broken. Don't grow the buffer waiting for a newline.
                if len(pending) > MAX_PENDING:
                    pending = b''

                frame, levels = (self._build_frame(latest)
                                 if latest is not None else (None, None))
                if levels and any(levels):
                    last_sound = started

                # One write per frame, notice latched: bars plus "NO AUDIO" over them flickers badly.
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
            # Before the menu is redrawn: an hour of 60fps is what desyncs the bus. See NOTES.md.
            self._resync_display()
            if self._on_stop is not None:
                try:
                    self._on_stop()
                except Exception as e:
                    logger.debug("Level meter on_stop failed: %s", e)
