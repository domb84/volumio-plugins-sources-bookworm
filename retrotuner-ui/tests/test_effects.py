"""Tests for includes/effects.py: frame shape, the glyph budget, and the player.

All pure logic -- nothing here touches a display. What matters most is that no
effect can emit something the display cannot render: a frame that is not two
rows of 16, a glyph code outside the 8 CGRAM slots, or a 0x0A that lcd_render
would treat as a line break and cut the frame in half.
"""
import io
import json
import os
import time
from unittest.mock import Mock

import pytest

from includes import effects


ALL_EFFECTS = effects.BOOT_EFFECTS + effects.SCREENSAVER_EFFECTS

# Sampled across each effect's life, including well past a boot effect's duration.
SAMPLE_TIMES = (0.0, 0.05, 0.3, 0.9, 1.05, 1.4, 2.0, 2.5, 3.0, 4.5, 30.0, 3600.0)


def _frames(effect, line1="RETROTUNER", line2="T-H300DAB"):
    return [effect.frame(t, line1, line2) for t in SAMPLE_TIMES]


def centre_glyph_families_differ():
    glyphs = effects.CentreOut().glyphs()
    return glyphs[:4] != glyphs[4:]


class TestEveryEffectRendersALegalFrame:
    @pytest.mark.parametrize("cls", ALL_EFFECTS, ids=lambda c: c.id)
    def test_frame_is_two_rows_of_sixteen(self, cls):
        for frame in _frames(cls()):
            rows = frame.split('\n')
            assert len(rows) == effects.LCD_ROWS
            assert all(len(row) == effects.LCD_COLUMNS for row in rows)

    @pytest.mark.parametrize("cls", ALL_EFFECTS, ids=lambda c: c.id)
    def test_no_glyph_code_is_a_newline(self, cls):
        # 0x0A is the row separator; one inside a row would split the frame.
        for frame in _frames(cls()):
            for row in frame.split('\n'):
                assert '\n' not in row

    @pytest.mark.parametrize("cls", ALL_EFFECTS, ids=lambda c: c.id)
    def test_only_loaded_glyphs_and_printable_characters_are_used(self, cls):
        effect = cls()
        loaded = len(effect.glyphs())
        for frame in _frames(effect):
            for ch in frame.replace('\n', ''):
                code = ord(ch)
                if code < 0x20:
                    assert code < loaded, \
                        "%s used CGRAM slot %d but only loads %d" % (cls.id, code, loaded)
                else:
                    # Anything else must be a ROM character; the solid block is the
                    # only one above 0x7E any effect is allowed to reach for.
                    assert code <= 0x7E or ch == effects.FULL_BLOCK

    @pytest.mark.parametrize("cls", ALL_EFFECTS, ids=lambda c: c.id)
    def test_frames_are_deterministic(self, cls):
        # Every effect is a pure function of time, so a resync can redraw the
        # same frame and a test can rely on what it sees.
        effect = cls()
        assert _frames(effect) == _frames(effect)


class TestGlyphBudget:
    """Eight CGRAM slots exist. Anything wanting more cannot run at all."""

    @pytest.mark.parametrize("cls", ALL_EFFECTS, ids=lambda c: c.id)
    def test_no_effect_wants_more_than_eight(self, cls):
        assert len(cls().glyphs()) <= 8

    @pytest.mark.parametrize("cls", ALL_EFFECTS, ids=lambda c: c.id)
    def test_each_glyph_is_eight_rows_of_five_bits(self, cls):
        for glyph in cls().glyphs():
            assert len(glyph) == effects.CELL_ROWS
            assert all(0 <= row <= 0b11111 for row in glyph)

    def test_centre_out_spends_every_slot_and_leans_on_the_rom_block(self):
        # A curtain parting in both directions needs a left- and a right-aligned
        # family, four each. That is the whole budget, so the solid cell has to
        # be the free ROM block or the effect would want nine shapes.
        centre = effects.CentreOut()
        assert len(centre.glyphs()) == 8
        mid = centre._COVER * 0.9
        assert effects.FULL_BLOCK in centre.frame(mid, 'HELLO', 'WORLD')

    def test_centre_out_uses_both_fill_directions(self):
        # One family only would make the two curtains asymmetric.
        assert centre_glyph_families_differ()

    def test_the_wipe_reuses_one_glyph_family_for_both_passes(self):
        # Five left-aligned fills; a right-aligned set as well would be 10.
        assert len(effects.Wipe().glyphs()) == 5


class TestBootEffectsFinish:
    @pytest.mark.parametrize("cls", effects.BOOT_EFFECTS, ids=lambda c: c.id)
    def test_boot_effects_have_a_duration(self, cls):
        assert cls.duration is not None and cls.duration > 0

    @pytest.mark.parametrize("cls", effects.BOOT_EFFECTS, ids=lambda c: c.id)
    def test_the_text_is_shown_in_full_at_some_point(self, cls):
        # Not necessarily at the end: the slide-in leaves again, so the menu
        # arrives on a clear panel. What matters is that the name was readable.
        effect = cls()
        steps = 200
        assert any("HELLO" in frame and "WORLD" in frame
                   for frame in (effect.frame(cls.duration * i / steps,
                                              "HELLO", "WORLD")
                                 for i in range(steps + 1)))

    def test_the_slide_leaves_the_panel_clear(self):
        assert effects.SlideIn().frame(effects.SlideIn.duration,
                                       "HELLO", "WORLD").strip(" \n") == ""

    @pytest.mark.parametrize("cls", effects.SCREENSAVER_EFFECTS, ids=lambda c: c.id)
    def test_screensavers_loop_forever(self, cls):
        assert cls.duration is None


class TestTextHandling:
    @pytest.mark.parametrize("cls", ALL_EFFECTS, ids=lambda c: c.id)
    def test_over_long_text_is_cropped_not_wrapped(self, cls):
        long_text = "THIS IS FAR TOO LONG FOR THE PANEL"
        for frame in _frames(cls(), long_text, long_text):
            for row in frame.split('\n'):
                assert len(row) == effects.LCD_COLUMNS

    @pytest.mark.parametrize("cls", ALL_EFFECTS, ids=lambda c: c.id)
    def test_empty_text_still_renders(self, cls):
        for frame in _frames(cls(), "", ""):
            assert len(frame.split('\n')) == effects.LCD_ROWS

    def test_the_scanner_sweeps_rather_than_sitting_still(self):
        scanner = effects.Scanner()
        seen = {scanner.frame(t * 0.1, "", "") for t in range(28)}
        assert len(seen) > 8

    def test_the_scanner_lights_both_rows_alike(self):
        top, bottom = effects.Scanner().frame(0.4, "", "").split("\n")
        assert top == bottom

    def test_data_rain_keeps_moving(self):
        rain = effects.DataRain()
        seen = {rain.frame(t * 0.1, "", "") for t in range(30)}
        assert len(seen) > 8

    def test_the_vu_bars_move_independently(self):
        # Two channels driven off the same curve at the same phase would just
        # be one bar drawn twice.
        top, bottom = effects.VuMeters().frame(3.0, "", "").split("\n")
        assert top != bottom

    def test_bounce_moves_the_text_around(self):
        bounce = effects.Bounce()
        seen = {bounce.frame(t * bounce._STEP, "HI", "") for t in range(40)}
        assert len(seen) > 4

    def test_default_text_is_used_when_none_is_set(self):
        assert effects.default_text()          # never empty, whatever the hostname


class TestBuildEffect:
    def test_none_builds_nothing(self):
        assert effects.boot_effect(effects.NONE) is None
        assert effects.screensaver_effect(effects.NONE) is None

    def test_empty_builds_nothing(self):
        assert effects.boot_effect('') is None

    def test_unknown_id_falls_back_to_nothing_rather_than_raising(self, caplog):
        assert effects.boot_effect('nonsense') is None
        assert "Unknown effect" in caplog.text

    def test_a_screensaver_is_not_available_as_a_boot_graphic(self):
        # The lists are separate on purpose: a boot effect that never finishes
        # would hold the display before the menu had ever rendered.
        assert effects.boot_effect('wave') is None
        assert effects.screensaver_effect('splitflap') is None

    @pytest.mark.parametrize("effect_id", effects.SUPPORTED_BOOT_EFFECTS)
    def test_every_supported_boot_id_builds(self, effect_id):
        built = effects.boot_effect(effect_id)
        assert built is None if effect_id == effects.NONE else built.id == effect_id

    @pytest.mark.parametrize("effect_id", effects.SUPPORTED_SCREENSAVER_EFFECTS)
    def test_every_supported_screensaver_id_builds(self, effect_id):
        built = effects.screensaver_effect(effect_id)
        assert built is None if effect_id == effects.NONE else built.id == effect_id


class _TwoFrames(effects.Effect):
    """A boot effect short enough to run inside a test."""
    id = 'twoframes'
    fps = 50
    duration = 0.04

    def glyphs(self):
        return [[0b11111] * 8]

    def frame(self, t, line1, line2):
        return effects._compose(line1, line2)


class TestEffectPlayer:
    def _menu(self):
        return Mock(spec=['render_frame', 'create_char', 'resync_display'])

    def test_play_runs_to_completion_on_this_thread(self):
        menu = self._menu()
        player = effects.EffectPlayer(menu, _TwoFrames(), line1="HI")
        assert player.play() is True
        assert menu.render_frame.called
        assert not player.running

    def test_glyphs_are_loaded_before_the_first_frame(self):
        menu = self._menu()
        effects.EffectPlayer(menu, _TwoFrames()).play()
        assert menu.create_char.call_count == 1

    def test_the_bus_is_resynced_around_the_run(self):
        # Once on the way in and once on the way out, as the level meter does.
        menu = self._menu()
        effects.EffectPlayer(menu, _TwoFrames()).play()
        assert menu.resync_display.call_count == 2

    def test_an_older_library_without_resync_still_plays(self):
        menu = Mock(spec=['render_frame', 'create_char'])
        assert effects.EffectPlayer(menu, _TwoFrames()).play() is True

    def test_no_effect_means_nothing_is_drawn(self):
        menu = self._menu()
        player = effects.EffectPlayer(menu, None)
        assert player.play() is False
        assert player.start() is False
        assert not menu.render_frame.called

    def test_on_stop_fires_once_the_run_ends(self):
        on_stop = Mock()
        effects.EffectPlayer(self._menu(), _TwoFrames(), on_stop=on_stop).play()
        on_stop.assert_called_once_with()

    def test_a_render_failure_does_not_escape(self):
        menu = self._menu()
        menu.render_frame.side_effect = RuntimeError("bus fell over")
        on_stop = Mock()
        effects.EffectPlayer(menu, _TwoFrames(), on_stop=on_stop).play()
        on_stop.assert_called_once_with()      # still handed the display back

    def test_start_and_stop_a_looping_effect(self):
        menu = self._menu()
        player = effects.EffectPlayer(menu, effects.Wave())
        assert player.start() is True
        try:
            deadline = time.monotonic() + 2.0
            while not menu.render_frame.called and time.monotonic() < deadline:
                time.sleep(0.01)
            assert menu.render_frame.called
        finally:
            player.stop()
        assert not player.running

    def test_starting_twice_is_refused(self):
        player = effects.EffectPlayer(self._menu(), effects.Wave())
        assert player.start() is True
        try:
            assert player.start() is False
        finally:
            player.stop()


class TestConfigWiring:
    """The effect ids live in three files that cannot import each other:
    includes/effects.py, config.json and UIConfig.json. index.js carries the
    labels. Nothing at runtime reports a mismatch -- a stale UIConfig option
    just saves an id that effects.py then refuses and falls back from.
    """

    def _plugin_dir(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _load(self, name):
        with io.open(os.path.join(self._plugin_dir(), name), encoding="utf-8") as handle:
            return json.load(handle)

    def _field(self, field_id):
        for section in self._load("UIConfig.json")["sections"]:
            for item in section.get("content", []):
                if item.get("id") == field_id:
                    return section, item
        raise AssertionError("no %s field in UIConfig.json" % field_id)

    def _index_js(self):
        with io.open(os.path.join(self._plugin_dir(), "index.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    @pytest.mark.parametrize("field_id,supported", [
        ("boot_effect", effects.SUPPORTED_BOOT_EFFECTS),
        ("screensaver_effect", effects.SUPPORTED_SCREENSAVER_EFFECTS),
    ])
    def test_the_dropdown_offers_exactly_the_supported_effects(self, field_id, supported):
        _section, field = self._field(field_id)
        assert tuple(o["value"] for o in field["options"]) == supported

    @pytest.mark.parametrize("field_id", ["boot_effect", "screensaver_effect"])
    def test_every_option_is_labelled(self, field_id):
        _section, field = self._field(field_id)
        for option in field["options"]:
            assert option["label"] and option["label"] != option["value"]

    @pytest.mark.parametrize("field_id", [
        "boot_effect", "boot_line1", "boot_line2",
        "screensaver_effect", "screensaver_line1", "screensaver_timeout",
    ])
    def test_the_field_is_saved_by_its_section(self, field_id):
        # A field missing from saveButton.data is rendered but never submitted.
        section, _field = self._field(field_id)
        assert field_id in section["saveButton"]["data"]
        assert section["onSave"]["method"] == "saveOptions"

    @pytest.mark.parametrize("key,expected", [
        ("boot_effect", effects.DEFAULT_BOOT_EFFECT),
        ("screensaver_effect", effects.DEFAULT_SCREENSAVER_EFFECT),
    ])
    def test_config_json_default_matches_the_module(self, key, expected):
        assert self._load("config.json")[key]["value"] == expected

    def test_config_json_timeout_default_matches_the_module(self):
        default = self._load("config.json")["screensaver_timeout"]["value"]
        assert int(default) == int(effects.DEFAULT_SCREENSAVER_TIMEOUT)

    @pytest.mark.parametrize("key", [
        "boot_effect", "boot_line1", "boot_line2", "screensaver_effect",
        "screensaver_line1", "screensaver_timeout",
    ])
    def test_config_json_carries_every_key(self, key):
        assert key in self._load("config.json")

    @pytest.mark.parametrize("field_id,default", [
        ("boot_effect", effects.DEFAULT_BOOT_EFFECT),
        ("screensaver_effect", effects.DEFAULT_SCREENSAVER_EFFECT),
    ])
    def test_the_dropdown_opens_on_the_default(self, field_id, default):
        _section, field = self._field(field_id)
        assert field["value"]["value"] == default

    def _label_block(self, name):
        js = self._index_js()
        start = js.index("var %s = {" % name)
        return js[start:js.index("};", start)]

    @pytest.mark.parametrize("cls", effects.BOOT_EFFECTS, ids=lambda c: c.id)
    def test_index_js_labels_every_boot_effect(self, cls):
        # Scoped to the right map: "vu:" also appears in METER_MODE_LABELS, so a
        # search over the whole file passes without the screensaver label existing.
        assert "%s:" % cls.id in self._label_block("BOOT_EFFECT_LABELS")

    @pytest.mark.parametrize("cls", effects.SCREENSAVER_EFFECTS, ids=lambda c: c.id)
    def test_index_js_labels_every_screensaver(self, cls):
        assert "%s:" % cls.id in self._label_block("SCREENSAVER_EFFECT_LABELS")

    def test_index_js_flattens_the_new_selects_before_validating(self):
        # A select posts {value, label}; unflattened it fails the numeric check
        # and the setting is silently dropped.
        js = self._index_js()
        for key in ("boot_effect", "screensaver_effect", "screensaver_timeout"):
            assert key in js.split("for (const key of ['meter_framerate'")[1][:400]

    def test_index_js_accepts_the_free_text_fields(self):
        js = self._index_js()
        block = js.split("var TEXT_SETTINGS")[1][:300]
        for key in ("boot_line1", "boot_line2", "screensaver_line1"):
            assert key in block
        # Its only user was the marquee; a field nothing reads is worse than none.
        assert "screensaver_line2" not in js


class TestIndexPyParsing:
    def test_an_older_config_without_the_keys_still_starts(self):
        import index
        assert index.parse_effect({}, "boot_effect",
                                  effects.SUPPORTED_BOOT_EFFECTS,
                                  effects.DEFAULT_BOOT_EFFECT) == effects.DEFAULT_BOOT_EFFECT

    def test_a_known_effect_is_read_through(self):
        import index
        config = {"boot_effect": {"value": "wipe"}}
        assert index.parse_effect(config, "boot_effect",
                                  effects.SUPPORTED_BOOT_EFFECTS, "splitflap") == "wipe"

    def test_an_unknown_effect_falls_back(self, caplog):
        import index
        config = {"boot_effect": {"value": "nonsense"}}
        assert index.parse_effect(config, "boot_effect",
                                  effects.SUPPORTED_BOOT_EFFECTS, "splitflap") == "splitflap"
        assert "Unknown boot_effect" in caplog.text

    def test_text_is_cropped_to_the_display_width(self):
        import index
        config = {"boot_line1": {"value": "THIS IS FAR TOO LONG"}}
        assert len(index.parse_optional_text_field(config, "boot_line1")) == effects.LCD_COLUMNS

    def test_missing_text_reads_as_empty(self):
        import index
        assert index.parse_optional_text_field({}, "boot_line1") == ""
