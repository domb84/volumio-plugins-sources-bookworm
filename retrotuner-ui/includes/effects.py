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

from .level_meter import bar_bitmaps, render_columns

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


class Effect:
    """One animation. ``frame()`` is pure: same time in, same frame out."""

    id = ''
    label = ''
    fps = 15
    duration = None       # seconds, or None to loop until stopped
    uses_text = True
    uses_line2 = True
    # Display controller methods the effect needs; it refuses to start without them.
    requires = ()

    def glyphs(self):
        """CGRAM bitmaps to load before the first frame. At most 8."""
        return []

    def frame(self, t, line1, line2):
        raise NotImplementedError

    def brightness(self, t):
        """0..1 for effects that dim the panel, or None to leave it alone."""
        return None


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
        out = []
        for width in range(1, 6):
            bits = 0
            for column in range(width):
                bits |= 0b10000 >> column
            out.append([bits] * CELL_ROWS)
        return out

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
    duration = 2.4

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
        a = _ease_out(max(0.0, min(1.0, t / 0.9)))
        b = _ease_out(max(0.0, min(1.0, (t - 0.18) / 0.9)))
        s1 = str(line1 or '')[:LCD_COLUMNS]
        s2 = str(line2 or '')[:LCD_COLUMNS]
        home1 = max(0, (LCD_COLUMNS - len(s1)) // 2)
        home2 = max(0, (LCD_COLUMNS - len(s2)) // 2)
        return _compose(
            self._shift(s1, int(round(home1 + (1 - a) * LCD_COLUMNS))),
            self._shift(s2, int(round(home2 - (1 - b) * LCD_COLUMNS))),
        )


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


# ---- Screensavers --------------------------------------------------------

class Wave(Effect):
    id = 'wave'
    label = 'Travelling wave'
    fps = 15
    uses_text = False

    def glyphs(self):
        return bar_bitmaps()

    def frame(self, t, line1, line2):
        levels = []
        for c in range(LCD_COLUMNS):
            value = (8.5
                     + 7 * math.sin(c * 0.55 - t / 0.38)
                     + 1.2 * math.sin(c * 1.3 + t / 0.7))
            levels.append(int(round(max(0, min(16, value)))))
        return render_columns(levels)


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


class Marquee(Effect):
    id = 'marquee'
    label = 'Marquee'
    fps = 15

    _STEP = 0.14
    _SEPARATOR = '   -   '

    def frame(self, t, line1, line2):
        source = (str(line1 or '').strip() or default_text()) + self._SEPARATOR
        offset = int(t / self._STEP) % len(source)
        window = (source + source)[offset:offset + LCD_COLUMNS]
        return _compose(window, _centre(line2))


class Breathe(Effect):
    id = 'breathe'
    label = 'Breathing'
    fps = 4
    # No brightness command, no effect: the panel would blink rather than fade.
    requires = ('set_brightness',)

    def frame(self, t, line1, line2):
        return _compose(_centre(line1), _centre(line2))

    def brightness(self, t):
        return 0.18 + 0.82 * (0.5 - 0.5 * math.cos(t / 1.9))


class BigClock(Effect):
    id = 'clock'
    label = 'Big clock'
    fps = 1
    uses_text = False

    # Corner and bar pieces; the ROM block does the thick strokes, which is what fits 8 slots.
    _LT = [0x07, 0x0F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F]
    _UB = [0x1F, 0x1F, 0x1F, 0x00, 0x00, 0x00, 0x00, 0x00]
    _RT = [0x1C, 0x1E, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F]
    _LL = [0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x0F, 0x07]
    _LB = [0x00, 0x00, 0x00, 0x00, 0x00, 0x1F, 0x1F, 0x1F]
    _LR = [0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1E, 0x1C]
    _UM = [0x1F, 0x1F, 0x1F, 0x00, 0x00, 0x00, 0x1F, 0x1F]
    _LM = [0x1F, 0x00, 0x00, 0x00, 0x00, 0x1F, 0x1F, 0x1F]

    # Glyph slots, then FULL for the ROM block and None for a blank cell.
    LT, UB, RT, LL, LB, LR, UM, LM = range(8)
    FULL = 8

    _DIGITS = {
        '0': ((LT, UB, RT), (LL, LB, LR)),
        '1': ((UB, FULL, None), (LB, FULL, LB)),
        '2': ((UM, UM, RT), (LL, LM, LM)),
        '3': ((UM, UM, RT), (LM, LM, LR)),
        '4': ((LL, LB, FULL), (None, None, FULL)),
        '5': ((FULL, UM, UM), (LM, LM, LR)),
        '6': ((LT, UM, UM), (LL, LM, LR)),
        '7': ((UB, UB, RT), (None, LT, None)),
        '8': ((LT, UM, RT), (LL, LM, LR)),
        '9': ((LT, UM, RT), (LB, LB, LR)),
    }

    def glyphs(self):
        return [self._LT, self._UB, self._RT, self._LL,
                self._LB, self._LR, self._UM, self._LM]

    @staticmethod
    def _cell(slot):
        if slot is None:
            return ' '
        if slot == BigClock.FULL:
            return FULL_BLOCK
        return chr(slot)

    def _now(self):
        return time.localtime()

    def frame(self, t, line1, line2):
        now = self._now()
        digits = '%02d%02d' % (now.tm_hour, now.tm_min)
        rows = [[' '] * LCD_COLUMNS, [' '] * LCD_COLUMNS]
        # 13 of the 16 columns, so wander the spare ones and the phosphor wears evenly.
        col = 1 + now.tm_min % 3
        for index, ch in enumerate(digits):
            shape = self._DIGITS.get(ch)
            if shape is not None:
                for r in range(LCD_ROWS):
                    for i in range(3):
                        if col + i < LCD_COLUMNS:
                            rows[r][col + i] = self._cell(shape[r][i])
            col += 3
            if index == 1:                       # colon between hours and minutes
                if col < LCD_COLUMNS:
                    rows[0][col] = ':'
                    rows[1][col] = ':'
                col += 1
        return _compose(''.join(rows[0]), ''.join(rows[1]))


# ---- Registry ------------------------------------------------------------

BOOT_EFFECTS = (SplitFlap, PowerOnTest, Wipe, Typewriter, SlideIn, MeterTease)
SCREENSAVER_EFFECTS = (Wave, Bounce, Marquee, Breathe, BigClock)

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

    def _missing_capability(self):
        """The first capability the effect needs and the display does not have."""
        for name in self._effect.requires:
            if not callable(getattr(self._display, name, None)):
                return name
        return None

    def start(self):
        """Start drawing on a thread. Returns True if it was started."""
        if self._effect is None or self.running:
            return False
        missing = self._missing_capability()
        if missing is not None:
            logger.warning("Effect %s needs display.%s(), which this display does "
                           "not have; not starting", self._effect.id, missing)
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
        if self._missing_capability() is not None:
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

    def _set_brightness(self, level):
        setter = getattr(self._display, 'set_brightness', None)
        if setter is None:
            return
        try:
            setter(level)
        except Exception as e:
            logger.debug("Brightness set failed: %s", e)

    def _run(self):
        effect = self._effect
        interval = 1.0 / effect.fps if effect.fps > 0 else 1.0
        started = time.monotonic()
        last_brightness = None
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

                level = effect.brightness(t)
                if level is not None and level != last_brightness:
                    self._set_brightness(level)
                    last_brightness = level

                self._menu.render_frame(effect.frame(t, self._line1, self._line2))

                remaining = interval - (time.monotonic() - now)
                if remaining > 0:
                    self._stop.wait(remaining)
        except Exception as e:
            logger.error("Effect %s stopped on error: %s", effect.id, e)
        finally:
            if last_brightness is not None:
                self._set_brightness(1.0)
            # A long screensaver run is the same workload that desyncs the bus.
            self._resync()
            if self._on_stop is not None:
                try:
                    self._on_stop()
                except Exception as e:
                    logger.debug("Effect on_stop failed: %s", e)
