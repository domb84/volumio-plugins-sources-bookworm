"""Boot graphics and idle screens for the 16x2 VFD.

Same shape as the level meter: the effect owns the display, renders on a thread,
hands it back. Boot effects finish, screensavers loop. The 8 CGRAM slots shape
every one of them -- see NOTES.md ("Screen effects").
"""
import logging
import math
import socket
import threading
import time

from .level_meter import MAX_LEVEL, bar_bitmaps, render_columns

logger = logging.getLogger("Effects")

LCD_COLUMNS = 16
LCD_ROWS = 2
CELL_ROWS = 8

# Free, unlike a CGRAM glyph, and only the clock is short enough to need it.
FULL_BLOCK = chr(0xFF)

NONE = 'none'

# Long enough to read, short enough not to feel like a hang on plugin restart.
DEFAULT_BOOT_EFFECT = 'splitflap'
DEFAULT_SCREENSAVER_EFFECT = 'wave'
# Counts from the last button press, and starts only once the menu has gone idle.
DEFAULT_SCREENSAVER_TIMEOUT = 120.0


def default_text():
    """Boot text for a config that has not set any: the Volumio device name."""
    try:
        return socket.gethostname().upper()[:LCD_COLUMNS]
    except Exception:
        return "RETROTUNER"


def _pad(text):
    return str(text or '')[:LCD_COLUMNS].ljust(LCD_COLUMNS)


def _centre(text):
    text = str(text or '')[:LCD_COLUMNS]
    return text.center(LCD_COLUMNS)


def _compose(top, bottom):
    return '%s\n%s' % (_pad(top), _pad(bottom))


def _noise(n):
    """Deterministic pseudo-random in [0, 1), so a replay looks like the first run."""
    value = math.sin(n * 127.1) * 43758.5453
    return value - math.floor(value)


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _block_glyph():
    return [0b11111] * CELL_ROWS


def _left_fill_bitmaps(widths=range(1, 6)):
    """Cells filled from the left edge. Five of them give a bar 80 steps of
    travel across 16 cells instead of 16."""
    glyphs = []
    for width in widths:
        bits = 0
        for column in range(width):
            bits |= 0b10000 >> column
        glyphs.append([bits] * CELL_ROWS)
    return glyphs


def _right_fill_bitmaps(widths=range(1, 6)):
    """The mirror image, for anything that grows towards the left edge."""
    glyphs = []
    for width in widths:
        bits = 0
        for column in range(width):
            bits |= 0b00001 << column
        glyphs.append([bits] * CELL_ROWS)
    return glyphs


def _ease_in(t):
    return t ** 3


class Effect:
    """One animation. ``frame()`` is pure: same time in, same frame out."""

    id = ''
    label = ''
    fps = 15
    duration = None       # seconds, or None to loop until stopped
    uses_text = True
    uses_line2 = True

    def glyphs(self):
        """CGRAM bitmaps to load before the first frame. At most 8."""
        return []

    def frame(self, t, line1, line2):
        raise NotImplementedError


# ---- Boot ----------------------------------------------------------------

_ROLL = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


class SplitFlap(Effect):
    id = 'splitflap'
    label = 'Split-flap'
    fps = 20
    duration = 2.6

    def frame(self, t, line1, line2):
        rows = []
        for r, text in enumerate((_pad(line1), _pad(line2))):
            out = []
            for c, target in enumerate(text):
                settle = 0.38 + c * 0.09 + r * 0.045
                if t >= settle or target == ' ':
                    out.append(target)
                else:
                    tick = int(t / 0.055)
                    out.append(_ROLL[int(_noise(c * 7 + r * 31 + tick * 13) * len(_ROLL))])
            rows.append(''.join(out))
        return _compose(*rows)


class PowerOnTest(Effect):
    id = 'post'
    label = 'Power-on self test'
    fps = 20
    duration = 2.4

    def glyphs(self):
        return [_block_glyph()]

    def frame(self, t, line1, line2):
        block = chr(0)
        progress = (t - 0.45) / 0.9
        rows = []
        for r, text in enumerate((_pad(line1), _pad(line2))):
            out = []
            for c, ch in enumerate(text):
                if t < 0.45:
                    out.append(block)
                elif progress >= 1:
                    out.append(ch)
                else:
                    out.append(ch if progress > _noise(c * 13 + r * 97) else block)
            rows.append(''.join(out))
        return _compose(*rows)


class Wipe(Effect):
    id = 'wipe'
    label = 'Sub-character wipe'
    fps = 30
    duration = 2.8

    # 5 columns per cell, so 5 glyphs buy an 80-step sweep across a 16-cell display.
    _COVER = 1.1
    _CLEAR = 1.1

    def glyphs(self):
        return _left_fill_bitmaps()

    @staticmethod
    def _cell(covered, ch):
        if covered <= 0:
            return ch
        return chr(covered - 1)          # glyph 4 is the full block

    def frame(self, t, line1, line2):
        span = LCD_COLUMNS * 5
        lines = (_pad(line1), _pad(line2))
        rows = []
        if t < self._COVER:
            # Cover, left to right. Ahead of the curtain the cell is blank, not text.
            edge = _ease_out(t / self._COVER) * span
            for text in lines:
                out = []
                for c in range(LCD_COLUMNS):
                    width = max(0, min(5, int(round(edge - c * 5))))
                    out.append(' ' if width == 0 else chr(width - 1))
                rows.append(''.join(out))
        elif t < self._COVER + self._CLEAR:
            # Right to left, so the covered part is always the left of a cell. See NOTES.md.
            edge = _ease_out((t - self._COVER) / self._CLEAR) * span
            for text in lines:
                out = []
                for c in range(LCD_COLUMNS):
                    cleared = max(0, min(5, int(round(edge - (LCD_COLUMNS - 1 - c) * 5))))
                    out.append(self._cell(5 - cleared, text[c]))
                rows.append(''.join(out))
        else:
            rows = list(lines)
        return _compose(*rows)


class Typewriter(Effect):
    id = 'typewriter'
    label = 'Typewriter'
    fps = 15
    duration = 3.0

    _PER_CHAR = 0.095
    _GAP = 3          # characters' worth of pause between the two rows

    def glyphs(self):
        return [_block_glyph()]

    def frame(self, t, line1, line2):
        cursor = chr(0)
        lines = [_pad(line1).rstrip(), _pad(line2).rstrip()]
        remaining = int(t / self._PER_CHAR)
        rows = ['', '']
        for r, text in enumerate(lines):
            shown = max(0, min(len(text), remaining))
            rows[r] = text[:shown]
            if shown < len(text):
                if int(t / 0.26) % 2 == 0 and shown < LCD_COLUMNS:
                    rows[r] = rows[r] + cursor
                break
            remaining -= len(text) + self._GAP
            if remaining < 0:
                break
        return _compose(*rows)


class SlideIn(Effect):
    id = 'slide'
    label = 'Slide-in'
    fps = 30
    # In, hold, then out the way it came -- so the menu arrives on a clear panel.
    _IN = 0.9
    _HOLD = 0.9
    _OUT = 0.8
    _STAGGER = 0.18                    # row 2 trails row 1, or it reads as one block
    duration = _IN + _HOLD + _OUT

    @staticmethod
    def _shift(text, offset):
        """Place ``text`` at ``offset``, cropped to the display."""
        row = [' '] * LCD_COLUMNS
        for i, ch in enumerate(text):
            c = i + offset
            if 0 <= c < LCD_COLUMNS:
                row[c] = ch
        return ''.join(row)

    def frame(self, t, line1, line2):
        s1 = str(line1 or '')[:LCD_COLUMNS]
        s2 = str(line2 or '')[:LCD_COLUMNS]
        home1 = max(0, (LCD_COLUMNS - len(s1)) // 2)
        home2 = max(0, (LCD_COLUMNS - len(s2)) // 2)

        leaving = t - (self._IN + self._HOLD)
        if leaving <= 0:
            a = _ease_out(max(0.0, min(1.0, t / self._IN)))
            b = _ease_out(max(0.0, min(1.0, (t - self._STAGGER) / self._IN)))
            drift1 = (1 - a) * LCD_COLUMNS
            drift2 = -(1 - b) * LCD_COLUMNS
        else:
            # Out the way each row came in, rather than reversing into itself.
            k = _ease_in(max(0.0, min(1.0, leaving / self._OUT)))
            drift1 = -k * LCD_COLUMNS
            drift2 = k * LCD_COLUMNS

        return _compose(self._shift(s1, int(round(home1 + drift1))),
                        self._shift(s2, int(round(home2 + drift2))))


class MeterTease(Effect):
    id = 'tease'
    label = 'Meter tease'
    fps = 30
    duration = 3.4

    def glyphs(self):
        return bar_bitmaps()

    def frame(self, t, line1, line2):
        if t < 2.1:
            levels = []
            for c in range(LCD_COLUMNS):
                if t < 1.1:
                    seed = _noise(c * 5.7)
                    level = (t / 1.1) * (7 + seed * 9) * (0.65 + 0.35 * math.sin(t / 0.09 + c))
                elif t < 1.45:
                    level = 16
                else:
                    level = 16 * (1 - (t - 1.45) / 0.65)
                levels.append(int(round(max(0, min(16, level)))))
            return render_columns(levels)
        return _compose(_centre(line1), _centre(line2))


class CentreOut(Effect):
    id = 'centre'
    label = 'Centre-out reveal'
    fps = 30
    _COVER = 1.2
    _CLEAR = 1.2
    duration = _COVER + _CLEAR + 0.6

    # Four fills anchored to each edge of the cell; the solid one is the ROM
    # block, or a curtain that parts in both directions would want nine glyphs.
    RIGHT_FILL_GLYPHS = tuple(range(0, 4))
    LEFT_FILL_GLYPHS = tuple(range(4, 8))

    def glyphs(self):
        return _right_fill_bitmaps(range(1, 5)) + _left_fill_bitmaps(range(1, 5))

    def _cell(self, width, left_half, ch):
        if width <= 0:
            return ch
        if width >= 5:
            return FULL_BLOCK
        glyphs = self.RIGHT_FILL_GLYPHS if left_half else self.LEFT_FILL_GLYPHS
        return chr(glyphs[width - 1])

    def frame(self, t, line1, line2):
        # Centred, because the curtains part from the middle: text starting hard
        # left would appear from under the curtain rather than behind it.
        lines = (_centre(line1), _centre(line2))
        half = LCD_COLUMNS * 5 / 2.0

        if t < self._COVER:
            edge = _ease_out(t / self._COVER) * half
            revealed = False
        elif t < self._COVER + self._CLEAR:
            edge = half - _ease_out((t - self._COVER) / self._CLEAR) * half
            revealed = True
        else:
            return _compose(*lines)

        middle = LCD_COLUMNS // 2
        rows = []
        for text in lines:
            out = []
            for c in range(LCD_COLUMNS):
                left_half = c < middle
                distance = (middle - 1 - c) if left_half else (c - middle)
                width = int(round(edge - distance * 5))
                out.append(self._cell(width, left_half,
                                      text[c] if revealed else ' '))
            rows.append(''.join(out))
        return _compose(*rows)


# ---- Screensavers --------------------------------------------------------

def _wave_shape(column, t):
    """The full-height wave: about 1.4 cycles across the panel, moving quickly.
    Sixteen steps of height absorb that much detail."""
    value = (0.53125
             + 0.4375 * math.sin(column * 0.55 - t / 0.38)
             + 0.075 * math.sin(column * 1.3 + t / 0.7))
    return max(0.0, min(1.0, value))


def _levels(shape, t, steps):
    return [int(round(shape(c, t) * steps)) for c in range(LCD_COLUMNS)]


class Wave(Effect):
    id = 'wave'
    label = 'Travelling wave'
    fps = 15
    uses_text = False

    def glyphs(self):
        return bar_bitmaps()

    def frame(self, t, line1, line2):
        # One wave stacked over both rows: 16 steps of one pixel each.
        return render_columns(_levels(_wave_shape, t, MAX_LEVEL))


class Bounce(Effect):
    id = 'bounce'
    label = 'Bouncing text'
    fps = 10
    uses_line2 = False

    _STEP = 0.19

    def frame(self, t, line1, line2):
        # Two columns spare: with no room to move it flips rows every frame instead.
        text = (str(line1 or '').strip() or default_text())[:LCD_COLUMNS - 2]
        span = max(1, LCD_COLUMNS - len(text))
        step = int(t / self._STEP)
        period = span * 2
        x = abs(step % period - span)
        y = (step // span) % LCD_ROWS
        row = (' ' * x + text)[:LCD_COLUMNS]
        return _compose(row, '') if y == 0 else _compose('', row)


class VuMeters(Effect):
    id = 'vu'
    label = 'VU meters'
    fps = 15
    uses_text = False

    _PEAK_GLYPH = 5
    _PEAK_WINDOW = 1.4          # seconds the marker looks back over
    _PEAK_SAMPLES = 14

    def glyphs(self):
        return _left_fill_bitmaps() + [[0b00100] * CELL_ROWS]

    @staticmethod
    def _level(channel, t):
        """Synthesised movement. Nothing is playing when this is on screen, so
        there is no signal to follow -- the cava-driven meter is a mode of the
        level meter, not a screensaver."""
        value = (0.52
                 + 0.30 * math.sin(t / 0.43 + channel * 2.3)
                 + 0.14 * math.sin(t / 0.17 + channel * 5.1)
                 + 0.08 * math.sin(t / 0.07 + channel * 1.7))
        return max(0.0, min(1.0, value))

    def frame(self, t, line1, line2):
        span = LCD_COLUMNS * 5
        step = self._PEAK_WINDOW / self._PEAK_SAMPLES
        rows = []
        for channel in range(LCD_ROWS):
            end = self._level(channel, t) * span
            # Held over a window rather than carried between frames, so the
            # animation stays a pure function of time.
            peak = max(self._level(channel, t - i * step)
                       for i in range(self._PEAK_SAMPLES)) * span
            peak_cell = min(LCD_COLUMNS - 1, int(peak) // 5)
            out = []
            for c in range(LCD_COLUMNS):
                width = max(0, min(5, int(round(end - c * 5))))
                if width > 0:
                    out.append(chr(width - 1))
                elif c == peak_cell:
                    out.append(chr(self._PEAK_GLYPH))
                else:
                    out.append(' ')
            rows.append(''.join(out))
        return _compose(*rows)


class Scanner(Effect):
    id = 'scanner'
    label = 'Scanner'
    fps = 15
    uses_text = False

    _SWEEP = 1.4                # seconds per pass

    def glyphs(self):
        return _left_fill_bitmaps()

    def frame(self, t, line1, line2):
        u = (t / self._SWEEP) % 2
        swing = _ease_out(u) if u < 1 else 1 - _ease_out(u - 1)
        head = swing * (LCD_COLUMNS * 5 - 1)
        out = []
        for c in range(LCD_COLUMNS):
            # Narrower the further behind the head, which is the whole trail.
            width = int(round(5 - abs(c * 5 + 2 - head) / 3))
            out.append(chr(min(5, width) - 1) if width > 0 else ' ')
        row = ''.join(out)
        return _compose(row, row)


class DataRain(Effect):
    id = 'rain'
    label = 'Data rain'
    fps = 10
    uses_text = False

    _BAND = 7                   # cells still lit behind the head
    _SPEED = 0.085              # seconds per column
    _CHURN = 0.11               # seconds between character changes

    def frame(self, t, line1, line2):
        head = (t / self._SPEED) % (LCD_COLUMNS + 10)
        tick = int(t / self._CHURN)
        rows = []
        for r in range(LCD_ROWS):
            out = []
            for c in range(LCD_COLUMNS):
                age = head - c
                # Thinning towards the tail is what makes it read as motion.
                if (age < 0 or age > self._BAND
                        or _noise(c * 3.7 + r * 11.3 + tick * 2.1) < 0.25 + age * 0.09):
                    out.append(' ')
                    continue
                out.append(_ROLL[int(_noise(c * 5.1 + r * 17.9 + tick) * len(_ROLL))])
            rows.append(''.join(out))
        return _compose(*rows)


# ---- Registry ------------------------------------------------------------

BOOT_EFFECTS = (SplitFlap, PowerOnTest, Wipe, Typewriter, SlideIn, MeterTease,
                CentreOut)
SCREENSAVER_EFFECTS = (Wave, Bounce, VuMeters, Scanner, DataRain)

SUPPORTED_BOOT_EFFECTS = (NONE,) + tuple(e.id for e in BOOT_EFFECTS)
SUPPORTED_SCREENSAVER_EFFECTS = (NONE,) + tuple(e.id for e in SCREENSAVER_EFFECTS)


def build_effect(effect_id, available):
    """Instantiate one effect by id, or None for 'none' and anything unknown."""
    if not effect_id or effect_id == NONE:
        return None
    for cls in available:
        if cls.id == effect_id:
            return cls()
    logger.warning("Unknown effect %r; showing nothing", effect_id)
    return None


def boot_effect(effect_id):
    return build_effect(effect_id, BOOT_EFFECTS)


def screensaver_effect(effect_id):
    return build_effect(effect_id, SCREENSAVER_EFFECTS)


class EffectPlayer:
    """Renders one effect on the display until it finishes or is stopped.

    ``play()`` runs a boot effect inline; ``start()`` gives a screensaver its own
    thread. Both resync the bus going in and out, as the level meter does.
    """

    def __init__(self, menu, effect, line1='', line2='', display=None, on_stop=None):
        self._menu = menu
        self._effect = effect
        self._line1 = line1
        self._line2 = line2
        self._display = display
        self._on_stop = on_stop
        self._thread = None
        self._stop = threading.Event()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Start drawing on a thread. Returns True if it was started."""
        if self._effect is None or self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="EffectThread",
                                        daemon=True)
        self._thread.start()
        logger.info("Screensaver on (%s)", self._effect.id)
        return True

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def play(self):
        """Run a boot effect to completion on this thread."""
        if self._effect is None:
            return False
        self._stop.clear()
        self._run()
        return True

    def _resync(self):
        resync = getattr(self._menu, 'resync_display', None)
        if resync is None:
            return
        try:
            resync()
        except Exception as e:
            logger.debug("Display resync failed: %s", e)

    def _load_glyphs(self):
        bitmaps = self._effect.glyphs()
        if len(bitmaps) > 8:
            raise ValueError("%s wants %d CGRAM slots; there are 8"
                             % (self._effect.id, len(bitmaps)))
        for location, bitmap in enumerate(bitmaps):
            self._menu.create_char(location, bitmap)

    def _run(self):
        effect = self._effect
        interval = 1.0 / effect.fps if effect.fps > 0 else 1.0
        started = time.monotonic()
        try:
            self._resync()
            self._load_glyphs()
            while not self._stop.is_set():
                now = time.monotonic()
                t = now - started
                if effect.duration is not None and t >= effect.duration:
                    # Hold the last frame rather than cutting mid-animation.
                    self._menu.render_frame(effect.frame(effect.duration,
                                                         self._line1, self._line2))
                    break

                self._menu.render_frame(effect.frame(t, self._line1, self._line2))

                remaining = interval - (time.monotonic() - now)
                if remaining > 0:
                    self._stop.wait(remaining)
        except Exception as e:
            logger.error("Effect %s stopped on error: %s", effect.id, e)
        finally:
            # A long screensaver run is the same workload that desyncs the bus.
            self._resync()
            if self._on_stop is not None:
                try:
                    self._on_stop()
                except Exception as e:
                    logger.debug("Effect on_stop failed: %s", e)
