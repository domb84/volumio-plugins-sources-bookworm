"""Tests for includes/level_meter.py: glyph shapes and cava output rendering.

All pure logic -- nothing here touches a display or a fifo. The signal
processing lives in cava, so there is none to test here.
"""
from unittest.mock import Mock

from includes.level_meter import (
    CELL_ROWS,
    FRAME_INTERVAL,
    FRAMES_PER_SECOND,
    LCD_COLUMNS,
    MAX_LEVEL,
    MODE_MONO,
    MODE_ROWS_CENTRE,
    MODE_ROWS_EDGES,
    MODE_STEREO,
    SPLIT_DOWN_GLYPHS,
    SPLIT_LEVELS,
    SPLIT_UP_GLYPHS,
    SUPPORTED_FRAME_RATES,
    SUPPORTED_MODES,
    frame_interval,
    render_split,
    split_bitmaps,
    split_channels,
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


class TestFrameRateMatchesCava:
    """Our draw rate and cava's framerate are two halves of one setting.

    cava caps how many distinct frames exist; FRAME_INTERVAL caps how often we
    draw one. If they drift apart the display either redraws frames it has
    already shown or throws away frames cava computed -- neither is visible as a
    failure, it just quietly wastes one side or the other.

    The settings page rewrites both together, so what is pinned here is that the
    two shipped defaults agree: a fresh install runs on these until someone
    opens the settings page, and nothing else would catch them diverging.
    """

    def _cava_framerate(self):
        import os
        import re
        conf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'cava', 'retrotuner-cava.conf')
        with open(conf) as handle:
            match = re.search(r'^framerate\s*=\s*(\d+)', handle.read(), re.M)
        assert match, "no framerate in the cava config"
        return int(match.group(1))

    def test_the_two_rates_agree(self):
        assert self._cava_framerate() == FRAMES_PER_SECOND

    def test_the_interval_is_the_reciprocal_of_the_rate(self):
        assert FRAME_INTERVAL == 1.0 / FRAMES_PER_SECOND


class TestConfigurableFrameRate:
    """The draw rate comes from the plugin settings, so it must survive whatever
    a hand-edited or outdated config file contains."""

    def test_a_supported_rate_sets_the_interval(self):
        for rate in SUPPORTED_FRAME_RATES:
            assert frame_interval(rate) == 1.0 / rate

    def test_the_default_is_one_of_the_offered_rates(self):
        assert FRAMES_PER_SECOND in SUPPORTED_FRAME_RATES

    def test_a_string_from_the_config_file_still_works(self):
        # config.json stores numbers as strings ("value": "60").
        assert frame_interval("30") == 1.0 / 30

    def test_zero_falls_back_rather_than_dividing_by_zero(self):
        assert frame_interval(0) == FRAME_INTERVAL

    def test_a_negative_rate_falls_back(self):
        # Otherwise the draw loop never waits and spins flat out on the display.
        assert frame_interval(-10) == FRAME_INTERVAL

    def test_nonsense_falls_back(self):
        assert frame_interval("fast") == FRAME_INTERVAL
        assert frame_interval(None) == FRAME_INTERVAL

    def test_the_meter_uses_the_rate_it_was_given(self):
        meter = LevelMeter(Mock(), bars_path='/nonexistent', frame_rate=15)
        assert meter._frame_interval == 1.0 / 15

    def test_the_meter_defaults_to_the_shipped_rate(self):
        meter = LevelMeter(Mock(), bars_path='/nonexistent')
        assert meter._frame_interval == FRAME_INTERVAL


def _rows(frame):
    """Expand a frame into the 16 pixel rows the display would actually light."""
    bitmaps = split_bitmaps()
    lines = []
    for row in frame.split('\n'):
        cells = [bitmaps[ord(c)] if ord(c) < 8 else [0] * CELL_ROWS for c in row]
        for r in range(CELL_ROWS):
            lines.append(''.join('#' if cell[r] else '.' for cell in cells))
    return lines


class TestSplitGlyphs:
    """Two glyph sets have to coexist in the 8 CGRAM slots, so neither may spill
    into the other's range and both must span the full cell height."""

    def test_there_are_exactly_eight(self):
        assert len(split_bitmaps()) == 8

    def test_the_two_sets_do_not_overlap(self):
        assert set(SPLIT_UP_GLYPHS).isdisjoint(SPLIT_DOWN_GLYPHS)
        assert len(SPLIT_UP_GLYPHS) == len(SPLIT_DOWN_GLYPHS) == SPLIT_LEVELS

    def test_slots_stay_inside_cgram(self):
        assert max(SPLIT_UP_GLYPHS + SPLIT_DOWN_GLYPHS) < 8

    def test_up_glyphs_fill_from_the_bottom(self):
        for slot in SPLIT_UP_GLYPHS:
            rows = split_bitmaps()[slot]
            assert rows[-1] != 0            # bottom pixel row always lit

    def test_down_glyphs_fill_from_the_top(self):
        for slot in SPLIT_DOWN_GLYPHS:
            rows = split_bitmaps()[slot]
            assert rows[0] != 0             # top pixel row always lit

    def test_the_tallest_of_each_set_fills_the_cell(self):
        assert all(split_bitmaps()[SPLIT_UP_GLYPHS[-1]])
        assert all(split_bitmaps()[SPLIT_DOWN_GLYPHS[-1]])

    def test_the_two_sets_are_vertical_mirrors(self):
        for up, down in zip(SPLIT_UP_GLYPHS, SPLIT_DOWN_GLYPHS):
            assert split_bitmaps()[down] == list(reversed(split_bitmaps()[up]))


class TestSplitChannels:
    """cava sends both channels in one array, laid out for a mirrored display."""

    def test_the_frame_halves_into_two_channels(self):
        left, right = split_channels(list(range(32)))
        assert len(left) == len(right) == 16

    def test_the_left_half_is_reversed_back_to_low_high(self):
        # cava sends the left channel high-to-low so bass meets in the centre.
        left, right = split_channels([1, 2, 3, 4])
        assert left == [2, 1]
        assert right == [3, 4]

    def test_an_empty_frame_is_survivable(self):
        assert split_channels([]) == ([], [])


class TestRenderSplit:
    """Left on the top row, right on the bottom, growing either in from the outer
    edges or out from the centre line."""

    def test_the_frame_is_two_rows_of_the_display_width(self):
        top, bottom = render_split([2] * 16, [2] * 16, from_edges=True).split('\n')
        assert len(top) == len(bottom) == LCD_COLUMNS

    def test_silence_renders_blank(self):
        frame = render_split([0] * 16, [0] * 16, from_edges=True)
        assert frame == ' ' * LCD_COLUMNS + '\n' + ' ' * LCD_COLUMNS

    def test_from_edges_leaves_the_gap_in_the_middle(self):
        rows = _rows(render_split([1] * 16, [1] * 16, from_edges=True))
        assert rows[0] == '#' * LCD_COLUMNS      # top edge lit
        assert rows[-1] == '#' * LCD_COLUMNS     # bottom edge lit
        assert rows[7] == '.' * LCD_COLUMNS      # either side of the centre dark
        assert rows[8] == '.' * LCD_COLUMNS

    def test_from_centre_leaves_the_gap_at_top_and_bottom(self):
        rows = _rows(render_split([1] * 16, [1] * 16, from_edges=False))
        assert rows[0] == '.' * LCD_COLUMNS      # outer edges dark
        assert rows[-1] == '.' * LCD_COLUMNS
        assert rows[7] == '#' * LCD_COLUMNS      # lit either side of the centre
        assert rows[8] == '#' * LCD_COLUMNS

    def test_each_row_is_flipped_between_the_two_modes(self):
        # Not a mirror of the whole frame: each channel keeps its own row, and
        # only the direction it grows in changes. Flipping the frame instead
        # would swap left and right over, which is a different bug entirely.
        left, right = [0, 1, 2, 3] * 4, [3, 2, 1, 0] * 4
        edges = _rows(render_split(left, right, from_edges=True))
        centre = _rows(render_split(left, right, from_edges=False))

        assert centre[:CELL_ROWS] == list(reversed(edges[:CELL_ROWS]))
        assert centre[CELL_ROWS:] == list(reversed(edges[CELL_ROWS:]))

    def test_the_channels_stay_on_their_own_rows_in_both_modes(self):
        loud_left = [SPLIT_LEVELS] * 16
        silent_right = [0] * 16
        for from_edges in (True, False):
            rows = _rows(render_split(loud_left, silent_right, from_edges=from_edges))
            assert all(row == '#' * LCD_COLUMNS for row in rows[:CELL_ROWS])
            assert all(row == '.' * LCD_COLUMNS for row in rows[CELL_ROWS:])

    def test_full_scale_fills_both_rows(self):
        rows = _rows(render_split([SPLIT_LEVELS] * 16, [SPLIT_LEVELS] * 16,
                                  from_edges=True))
        assert all(row == '#' * LCD_COLUMNS for row in rows)

    def test_columns_are_independent(self):
        left = [0, SPLIT_LEVELS] + [0] * 14
        rows = _rows(render_split(left, [0] * 16, from_edges=True))
        assert rows[0][0] == '.'
        assert rows[0][1] == '#'

    def test_levels_are_clamped_to_what_the_glyphs_can_draw(self):
        # cava is configured to the same range, but a stale config could send 16.
        frame = render_split([99] * 16, [-5] * 16, from_edges=True)
        top, bottom = frame.split('\n')
        assert top == chr(SPLIT_DOWN_GLYPHS[-1]) * LCD_COLUMNS
        assert bottom == ' ' * LCD_COLUMNS

    def test_a_short_frame_is_padded_not_crashed(self):
        top, bottom = render_split([1, 2], [3], from_edges=True).split('\n')
        assert len(top) == len(bottom) == LCD_COLUMNS

    def test_no_glyph_collides_with_the_line_break(self):
        # render_frame splits on the newline, so a glyph code of 0x0A would cut a
        # frame in half. Staying inside 0-7 is what keeps that impossible.
        frame = render_split([1, 2, 3, 4] * 4, [4, 3, 2, 1] * 4, from_edges=True)
        assert frame.count('\n') == 1


class TestMeterModes:
    def test_every_supported_mode_is_accepted(self):
        for mode in SUPPORTED_MODES:
            meter = LevelMeter(Mock(), bars_path='/nonexistent', mode=mode)
            assert meter._mode == mode

    def test_an_unknown_mode_falls_back_to_mono(self):
        meter = LevelMeter(Mock(), bars_path='/nonexistent', mode='spiral')
        assert meter._mode == MODE_MONO

    def test_only_the_row_layouts_split(self):
        splits = set()
        for mode in SUPPORTED_MODES:
            if LevelMeter(Mock(), bars_path='/x', mode=mode)._split:
                splits.add(mode)
        assert splits == {MODE_ROWS_EDGES, MODE_ROWS_CENTRE}

    def test_full_height_modes_read_sixteen_columns(self):
        meter = LevelMeter(Mock(), bars_path='/x', mode=MODE_STEREO)
        frame, levels = meter._build_frame(';'.join(['8'] * 32))
        assert len(levels) == LCD_COLUMNS
        assert len(frame.split('\n')[0]) == LCD_COLUMNS

    def test_split_modes_read_both_channels(self):
        meter = LevelMeter(Mock(), bars_path='/x', mode=MODE_ROWS_EDGES)
        frame, levels = meter._build_frame(';'.join(['4'] * 32))
        assert len(levels) == LCD_COLUMNS * 2
        top, bottom = frame.split('\n')
        assert len(top) == len(bottom) == LCD_COLUMNS

    def test_an_empty_line_yields_no_frame(self):
        for mode in SUPPORTED_MODES:
            meter = LevelMeter(Mock(), bars_path='/x', mode=mode)
            assert meter._build_frame('') == (None, [])

    def test_the_split_modes_load_the_split_glyphs(self):
        menu = Mock()
        meter = LevelMeter(menu, bars_path='/x', mode=MODE_ROWS_CENTRE)
        meter._load_glyphs()
        assert menu.create_char.call_count == 8
        loaded = [call[0][1] for call in menu.create_char.call_args_list]
        assert loaded == split_bitmaps()

    def test_the_full_height_modes_load_the_tall_glyphs(self):
        menu = Mock()
        meter = LevelMeter(menu, bars_path='/x', mode=MODE_MONO)
        meter._load_glyphs()
        loaded = [call[0][1] for call in menu.create_char.call_args_list]
        assert loaded == bar_bitmaps()
