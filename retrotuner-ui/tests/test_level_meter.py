"""Tests for includes/level_meter.py: glyph shapes and cava output rendering.

All pure logic -- nothing here touches a display or a fifo. The signal
processing lives in cava, so there is none to test here.
"""
from unittest.mock import Mock

from includes.level_meter import (
    CELL_ROWS,
    LCD_COLUMNS,
    MAX_LEVEL,
    LevelMeter,
    bar_bitmaps,
    parse_bars,
    render_columns,
)


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


class TestParseBars:
    """cava raw ascii: one line per frame, values separated by ';'.

    Configured with ascii_max_range to match the display, so no scaling is
    needed on our side -- see cava/retrotuner-cava.conf.
    """

    def test_reads_a_normal_frame(self):
        assert parse_bars("0;3;7;16") == [0, 3, 7, 16]

    def test_tolerates_the_trailing_separator_cava_emits(self):
        assert parse_bars("1;2;3;") == [1, 2, 3]

    def test_strips_the_newline(self):
        assert parse_bars("4;5\n") == [4, 5]

    def test_never_returns_more_columns_than_the_display_has(self):
        line = ';'.join(['8'] * 40)
        assert len(parse_bars(line)) == LCD_COLUMNS

    def test_clamps_values_to_the_display_height(self):
        assert parse_bars("99;-5") == [MAX_LEVEL, 0]

    def test_a_partial_line_drops_only_the_broken_field(self):
        # Reading a fifo another process is appending to, a half-written value
        # is normal and must not lose the whole frame.
        assert parse_bars("5;7;1x") == [5, 7]

    def test_empty_input_is_empty(self):
        assert parse_bars("") == []
        assert parse_bars(None) == []
        assert parse_bars("\n") == []

    def test_silence_is_all_zeros_not_an_empty_frame(self):
        assert parse_bars("0;0;0;0") == [0, 0, 0, 0]


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
            assert render_columns([level]).count('\n') == 1

    def test_out_of_range_levels_are_clamped(self):
        top, bottom = self._lines([MAX_LEVEL + 99])
        assert top[0] == chr(CELL_ROWS - 1)
        assert bottom[0] == chr(CELL_ROWS - 1)
        assert self._lines([-5])[1][0] == ' '


class TestCavaOutputEndToEnd:
    """A line from cava must survive straight through to a drawable frame."""

    def test_cava_max_range_matches_the_display_height(self):
        # cava is configured with ascii_max_range = MAX_LEVEL, so its loudest
        # bar must fill the display exactly -- no scaling on our side.
        frame = render_columns(parse_bars(';'.join([str(MAX_LEVEL)] * LCD_COLUMNS)))
        top, bottom = frame.split('\n')
        assert top == chr(CELL_ROWS - 1) * LCD_COLUMNS
        assert bottom == chr(CELL_ROWS - 1) * LCD_COLUMNS

    def test_a_realistic_frame_renders(self):
        frame = render_columns(parse_bars("0;2;5;9;14;16;12;7;4;2;1;0;0;1;3;6;"))
        top, bottom = frame.split('\n')
        assert len(top) == len(bottom) == LCD_COLUMNS
        assert top[5] != ' '        # the loud band reaches the upper row
        assert top[0] == ' '        # the silent one does not


class TestMeterCannotAffectAudio:
    """The meter reads cava's output, never the audio tap.

    cava owns the tap and keeps it drained; this class only consumes small
    numbers. That separation is what stops a bug here from stalling playback,
    so it is worth pinning down.
    """

    def test_it_reads_cavas_output_not_the_audio_fifo(self):
        from includes.level_meter import DEFAULT_BARS_PATH
        assert 'bars' in DEFAULT_BARS_PATH
        assert 'audio' not in DEFAULT_BARS_PATH

    def test_start_refuses_when_cava_is_not_publishing(self):
        menu = Mock()
        meter = LevelMeter(menu, bars_path='/nonexistent')
        assert meter.start() is False
        assert not meter.running
        menu.message.assert_called_once()      # tells the user, does not crash

    def test_there_is_no_drain_machinery_left(self):
        # Draining is cava's job now. A drain here would mean the audio path
        # depended on this process again.
        meter = LevelMeter(Mock(), bars_path='/nonexistent')
        assert not hasattr(meter, 'start_drain')
        assert not hasattr(meter, '_record_peak')


class TestModuleIsSelfConsistent:
    """Catch names used in _run() that are not actually imported.

    _run() only executes on the device, so a missing import there survives the
    rest of the suite untouched and surfaces as a silent "Level meter stopped on
    error" -- which is exactly how `select` went missing once already.
    """

    def test_modules_used_as_x_dot_y_are_imported(self):
        import ast
        from includes import level_meter

        tree = ast.parse(open(level_meter.__file__, encoding='utf8').read())

        # Bases of attribute access -- the "select" in select.select(...).
        bases = {n.value.id for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        # Anything bound locally (assignment, parameter, except-as) is not a
        # module reference, so exclude it.
        bound = {n.id for n in ast.walk(tree)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        bound |= {a.arg for a in ast.walk(tree) if isinstance(a, ast.arg)}
        bound |= {h.name for h in ast.walk(tree)
                  if isinstance(h, ast.ExceptHandler) and h.name}

        missing = sorted(b for b in bases - bound - {'self'}
                         if b not in vars(level_meter))
        assert not missing, "used but never imported: %s" % missing


class TestQuietStart:
    """The idle timeout starts the meter without being asked, so a failure to
    start must not paint over whatever the user was last looking at."""

    def test_an_unrequested_start_stays_silent(self):
        menu = Mock()
        meter = LevelMeter(menu, bars_path='/nonexistent')
        assert meter.start(announce=False) is False
        menu.message.assert_not_called()

    def test_a_requested_start_still_explains_itself(self):
        menu = Mock()
        meter = LevelMeter(menu, bars_path='/nonexistent')
        assert meter.start() is False
        menu.message.assert_called_once()
