"""Tests for includes/level_meter.py: glyph shapes, scaling and frame rendering.

All pure logic -- nothing here touches a display or a fifo.
"""
import array
from unittest.mock import Mock

from includes.level_meter import (
    CELL_ROWS,
    FULL_SCALE,
    LCD_COLUMNS,
    MAX_LEVEL,
    bar_bitmaps,
    peak_level,
    render_columns,
    scale_level,
)


def _pcm(*samples):
    """Raw S16_LE bytes from signed sample values."""
    return array.array('h', samples).tobytes()


class TestBarBitmaps:
    def test_there_are_exactly_eight(self):
        # The HD44780 has 8 CGRAM slots and no more.
        assert len(bar_bitmaps()) == 8

    def test_each_glyph_is_eight_rows_of_five_bits(self):
        for glyph in bar_bitmaps():
            assert len(glyph) == CELL_ROWS
            assert all(0 <= row <= 0b11111 for row in glyph)

    def test_glyphs_fill_from_the_bottom_upwards(self):
        for height, glyph in enumerate(bar_bitmaps(), start=1):
            assert glyph[-height:] == [0b11111] * height     # filled at the bottom
            assert glyph[:-height] == [0b00000] * (CELL_ROWS - height)

    def test_glyph_n_is_n_plus_one_rows_tall(self):
        for index, glyph in enumerate(bar_bitmaps()):
            assert sum(1 for row in glyph if row) == index + 1


class TestPeakLevel:
    def test_silence_is_zero(self):
        assert peak_level(_pcm(0, 0, 0, 0)) == 0.0

    def test_full_scale_is_one(self):
        assert peak_level(_pcm(32767, 0)) == 1.0

    def test_negative_peaks_count(self):
        assert peak_level(_pcm(0, -32767)) == 1.0

    def test_takes_the_peak_not_the_mean(self):
        # A single loud sample in a quiet buffer must still register.
        assert peak_level(_pcm(0, 0, 0, 16384)) > 0.49

    def test_empty_buffer_is_zero(self):
        assert peak_level(b'') == 0.0
        assert peak_level(None) == 0.0

    def test_odd_trailing_byte_is_ignored(self):
        # A short read from the fifo can leave half a sample behind.
        assert peak_level(_pcm(32767) + b'\x01') == 1.0

    def test_never_exceeds_one(self):
        assert peak_level(_pcm(-32768)) <= 1.0


class TestScaleLevel:
    def test_silence_draws_nothing(self):
        assert scale_level(0.0) == 0

    def test_near_silence_draws_nothing(self):
        # Otherwise a permanent row of dots sits across the display.
        assert scale_level(0.0001) == 0

    def test_full_scale_fills_the_display(self):
        assert scale_level(1.0) == MAX_LEVEL

    def test_audible_signal_is_never_rounded_away_to_zero(self):
        assert scale_level(0.02) >= 1

    def test_is_monotonic(self):
        levels = [scale_level(v / 100.0) for v in range(1, 101)]
        assert levels == sorted(levels)

    def test_uses_more_than_the_bottom_few_steps_at_normal_volume(self):
        # The point of the log curve: linear scaling puts typical music in the
        # bottom two steps and the display looks dead.
        assert scale_level(0.1) >= MAX_LEVEL // 4

    def test_stays_within_the_display(self):
        for value in (0.0, 0.5, 1.0, 2.0):
            assert 0 <= scale_level(value) <= MAX_LEVEL


class TestRenderColumns:
    def _lines(self, levels):
        return render_columns(levels).split('\n')

    def test_frame_is_two_rows_of_the_display_width(self):
        top, bottom = self._lines([0] * LCD_COLUMNS)
        assert len(top) == LCD_COLUMNS
        assert len(bottom) == LCD_COLUMNS

    def test_silence_renders_blank(self):
        top, bottom = self._lines([0] * LCD_COLUMNS)
        assert top.strip() == '' and bottom.strip() == ''

    def test_low_level_fills_the_bottom_row_only(self):
        top, bottom = self._lines([1])
        assert top[0] == ' '
        assert bottom[0] == chr(0)          # 1-pixel bar

    def test_bottom_row_fills_before_the_top_starts(self):
        top, bottom = self._lines([CELL_ROWS])
        assert bottom[0] == chr(CELL_ROWS - 1)   # full cell
        assert top[0] == ' '

    def test_high_level_stacks_onto_the_top_row(self):
        top, bottom = self._lines([CELL_ROWS + 1])
        assert bottom[0] == chr(CELL_ROWS - 1)   # bottom stays full
        assert top[0] == chr(0)                  # one pixel above it

    def test_maximum_fills_both_rows(self):
        top, bottom = self._lines([MAX_LEVEL])
        assert top[0] == chr(CELL_ROWS - 1)
        assert bottom[0] == chr(CELL_ROWS - 1)

    def test_columns_are_independent(self):
        top, bottom = self._lines([0, MAX_LEVEL, CELL_ROWS])
        assert bottom[0] == ' '
        assert top[1] == chr(CELL_ROWS - 1)
        assert top[2] == ' '

    def test_never_emits_a_newline_as_a_glyph(self):
        # lcd_render() treats "\n" as a line break, so a glyph code of 0x0A
        # would split the frame and corrupt the display.
        for level in range(MAX_LEVEL + 1):
            frame = render_columns([level])
            assert frame.count('\n') == 1

    def test_out_of_range_levels_are_clamped(self):
        top, bottom = self._lines([MAX_LEVEL + 99])
        assert top[0] == chr(CELL_ROWS - 1)
        assert bottom[0] == chr(CELL_ROWS - 1)
        assert self._lines([-5])[1][0] == ' '


class TestEndToEndShape:
    def test_a_loud_buffer_renders_a_tall_bar(self):
        level = scale_level(peak_level(_pcm(30000, -30000)))
        top, _bottom = render_columns([level]).split('\n')
        assert top[0] != ' '        # tall enough to reach the upper row

    def test_a_silent_buffer_renders_nothing(self):
        level = scale_level(peak_level(_pcm(0, 0)))
        assert render_columns([level]).strip() == ''


class TestPeakBackends:
    """The C and pure-python peak paths must agree.

    audioop does the scan ~80x faster and is what runs on the device; the
    fallback only exists for python 3.13+, where audioop was removed. A silent
    disagreement between them would make the meter behave differently depending
    on the runtime.
    """

    def _both(self, data):
        from includes import level_meter as lm
        real, lm._audioop = lm._audioop, None
        try:
            fallback = lm.peak_level(data)
        finally:
            lm._audioop = real
        return lm.peak_level(data), fallback

    def test_backends_agree_across_the_range(self):
        for samples in ([0, 0], [32767, 0], [0, -32767], [1, -2, 3],
                        [15000, -15000, 100], [-32768, 5]):
            c, py = self._both(_pcm(*samples))
            assert c == py, samples

    def test_backends_agree_on_a_trailing_odd_byte(self):
        c, py = self._both(_pcm(32767) + b'\x01')
        assert c == py == 1.0

    def test_backends_agree_on_empty_input(self):
        assert self._both(b'') == (0.0, 0.0)


class TestDrainKeepsUpWithTheStream:
    """The fifo must be emptied at the rate audio is produced.

    A linux pipe holds 64kB and 44.1kHz stereo S16 is 176kB/s, so a reader that
    falls behind lets the buffer fill, ALSA blocks on write, and playback stops
    after about a third of a second. This is not a performance nicety -- it is
    the difference between working audio and silence.
    """

    STREAM_BYTES_PER_SEC = 44100 * 2 * 2
    PIPE_BUFFER = 65536

    def test_a_full_pipe_stalls_audio_in_well_under_a_second(self):
        # Documents the observed symptom, so the number is not mysterious later.
        assert self.PIPE_BUFFER / self.STREAM_BYTES_PER_SEC < 0.5

    def test_a_single_wakeup_can_move_a_useful_share_of_the_buffer(self):
        from includes.level_meter import DRAIN_BYTES
        # Throughput is not DRAIN_BYTES/DRAIN_POLL: select() returns as soon as
        # data is there (the timeout only bounds idle wake-ups), and the loop
        # then reads until empty. What matters is that each read is large
        # enough that emptying the pipe takes a handful of syscalls, not
        # hundreds.
        assert DRAIN_BYTES >= self.PIPE_BUFFER // 8

    def test_the_old_per_frame_read_would_not_have_kept_up(self):
        from includes.level_meter import FRAME_INTERVAL
        # The original design read one 4kB chunk per rendered frame.
        assert (4096 / FRAME_INTERVAL) < self.STREAM_BYTES_PER_SEC


class TestDrainOutlivesTheDisplay:
    """Hiding the meter must not stop draining, or audio stops with it."""

    def _meter(self):
        from includes.level_meter import LevelMeter
        m = LevelMeter(Mock(), fifo_path='/nonexistent')
        return m

    def test_stop_does_not_touch_the_drain(self):
        m = self._meter()
        m._drain_thread = Mock(is_alive=Mock(return_value=True))
        m.stop()                       # hide the display
        assert m.draining, "stop() must leave the fifo being drained"

    def test_stop_drain_is_a_separate_call(self):
        m = self._meter()
        assert hasattr(m, 'stop_drain')
        assert m.stop is not m.stop_drain

    def test_start_refuses_when_there_is_no_tap(self):
        m = self._meter()
        assert m.start() is False
        assert not m.draining

    def test_take_peak_resets_for_the_next_frame(self):
        m = self._meter()
        m._peak = 16384
        first = m._take_peak()
        assert first > 0.49
        assert m._take_peak() == 0.0    # consumed

    def test_peak_is_the_loudest_since_the_last_frame(self):
        m = self._meter()
        m._record_peak(_pcm(1000, -2000))
        m._record_peak(_pcm(30000, 5))
        m._record_peak(_pcm(10, 10))
        assert m._take_peak() > 0.9     # the transient survives, not the average
