"""Tests for the config-parsing helpers in index.py."""
import json
import logging

import pytest

import index
from includes import level_meter


# btn_* keys that load_button_config expects to be present (no btn_stop — removed).
_BUTTON_KEYS = (
    "btn_enter", "btn_radio", "btn_spotify",
    "btn_info", "btn_favourite", "btn_main_menu", "btn_back",
)


def test_parse_button_mapping_plain():
    assert index.parse_button_mapping("0,12") == ("0", "12")


def test_parse_button_mapping_keeps_range_segment():
    assert index.parse_button_mapping("0, 24-25") == ("0", " 24-25")


def test_parse_int_field():
    assert index.parse_int_field({"x": {"value": "17"}}, "x") == 17


def test_load_button_config():
    cfg = {key: {"value": "0,12"} for key in _BUTTON_KEYS}
    result = index.load_button_config(cfg)
    assert set(result) == set(_BUTTON_KEYS)
    assert result["btn_enter"] == ("0", "12")


def test_load_button_config_does_not_include_btn_stop():
    cfg = {key: {"value": "0,12"} for key in _BUTTON_KEYS}
    cfg["btn_stop"] = {"value": "0,31"}
    result = index.load_button_config(cfg)
    assert "btn_stop" not in result


# --- Optional buttons (absent → omitted, present → included) ---

@pytest.mark.parametrize("key,value", [
    ("btn_pause", "0,14"),
    ("btn_sleep_timer", "0,18"),
    ("btn_dimmer", "0,20"),
])
def test_optional_button_omitted_when_absent(key, value):
    cfg = {k: {"value": "0,12"} for k in _BUTTON_KEYS}
    assert key not in index.load_button_config(cfg)


@pytest.mark.parametrize("key,value", [
    ("btn_pause", "0,14"),
    ("btn_sleep_timer", "0,18"),
    ("btn_dimmer", "0,20"),
])
def test_optional_button_included_when_present(key, value):
    cfg = {k: {"value": "0,12"} for k in _BUTTON_KEYS}
    cfg[key] = {"value": value}
    result = index.load_button_config(cfg)
    assert key in result


class TestParseOptionalBoolField:
    """Used for settings (debug_mode, rotary_skip_track) an older config may not carry yet."""

    def test_returns_default_when_key_absent(self):
        assert index.parse_optional_bool_field({}, "rotary_skip_track", False) is False
        assert index.parse_optional_bool_field({}, "rotary_skip_track", True) is True

    def test_returns_stored_true(self):
        cfg = {"rotary_skip_track": {"value": True}}
        assert index.parse_optional_bool_field(cfg, "rotary_skip_track", False) is True

    def test_returns_stored_false_even_if_default_is_true(self):
        cfg = {"rotary_skip_track": {"value": False}}
        assert index.parse_optional_bool_field(cfg, "rotary_skip_track", True) is False


def test_load_button_skip_config():
    cfg = {
        "btn_no_press_channel1": {"value": "0,16"},
        "btn_no_press_channel2": {"value": "7,16"},
    }
    result = index.load_button_skip_config(cfg)
    assert result["btn_no_press_channel1"] == ("0", "16")
    assert result["btn_no_press_channel2"] == ("7", "16")


def test_load_config_reads_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert index.load_config(path) == {"a": 1}


def test_load_config_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        index.load_config(tmp_path / "does-not-exist.json")


class TestApplyLogLevel:
    """debug_mode is a plain switch: on -> DEBUG, off/absent -> INFO."""

    @pytest.fixture(autouse=True)
    def _restore_root_level(self):
        original = logging.getLogger().level
        yield
        logging.getLogger().setLevel(original)

    def test_defaults_to_info_when_absent(self):
        index.apply_log_level({})
        assert logging.getLogger().getEffectiveLevel() == logging.INFO

    def test_false_sets_info(self):
        index.apply_log_level({"debug_mode": {"value": False}})
        assert logging.getLogger().getEffectiveLevel() == logging.INFO

    def test_true_sets_debug(self):
        index.apply_log_level({"debug_mode": {"value": True}})
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


class TestMeterFrameRateSettings:
    """The refresh rate is defined in four places that cannot import each other:
    config.json, UIConfig.json, the cava config and includes/level_meter.py.

    Nothing at runtime would report a mismatch -- a stale UIConfig option would
    just save a rate cava never runs at, and the meter would draw at a different
    rate from the analyser. So it is pinned here instead.
    """

    def _plugin_dir(self):
        import os
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _load(self, name):
        import io
        import os
        with io.open(os.path.join(self._plugin_dir(), name), encoding="utf-8") as handle:
            return json.load(handle)

    def _ui_field(self):
        ui = self._load("UIConfig.json")
        for section in ui["sections"]:
            for item in section.get("content", []):
                if item.get("id") == "meter_framerate":
                    return section, item
        raise AssertionError("no meter_framerate field in UIConfig.json")

    def test_config_json_carries_a_default(self):
        from includes.level_meter import FRAMES_PER_SECOND
        default = self._load("config.json")["meter_framerate"]["value"]
        assert int(default) == FRAMES_PER_SECOND

    def test_index_py_reads_it(self):
        from includes.level_meter import FRAMES_PER_SECOND
        config = {"meter_framerate": {"value": "30"}}
        assert index.parse_optional_int_field(config, "meter_framerate", FRAMES_PER_SECOND) == 30

    def test_an_older_config_without_the_key_still_starts(self):
        from includes.level_meter import FRAMES_PER_SECOND
        assert index.parse_optional_int_field({}, "meter_framerate", FRAMES_PER_SECOND) \
            == FRAMES_PER_SECOND

    def test_the_dropdown_offers_exactly_the_supported_rates(self):
        from includes.level_meter import SUPPORTED_FRAME_RATES
        _section, field = self._ui_field()
        assert tuple(o["value"] for o in field["options"]) == SUPPORTED_FRAME_RATES

    def test_every_option_is_labelled_in_fps(self):
        _section, field = self._ui_field()
        for option in field["options"]:
            assert option["label"] == "%d fps" % option["value"]

    def test_the_field_is_saved_by_its_section(self):
        # A field missing from saveButton.data is rendered but never submitted.
        section, _field = self._ui_field()
        assert "meter_framerate" in section["saveButton"]["data"]
        assert section["onSave"]["method"] == "saveOptions"


class TestMeterModeSetting:
    """Stereo mode is purely a cava setting -- it still emits 16 bars, so nothing
    on the python side changes. What has to hold is that index.js can find the
    key to rewrite, and that it rewrites the right one."""

    def _plugin_dir(self):
        import os
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _cava_sections(self):
        """{section: {key: value}} for the shipped cava config."""
        import io
        import os
        sections = {}
        current = None
        path = os.path.join(self._plugin_dir(), "cava", "retrotuner-cava.conf")
        with io.open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith((";", "#")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    current = line[1:-1].strip()
                    sections[current] = {}
                elif "=" in line and current is not None:
                    key, _, value = line.partition("=")
                    sections[current][key.strip()] = value.strip()
        return sections

    def _load_json(self, name):
        import io
        import os
        with io.open(os.path.join(self._plugin_dir(), name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_channels_appears_in_both_cava_sections(self):
        # The whole reason index.js edits this file by section: a plain line
        # match on "channels" would hit [input] first and rewrite the tap's
        # channel count with "mono", breaking the analyser's view of the audio.
        sections = self._cava_sections()
        assert "channels" in sections["input"]
        assert "channels" in sections["output"]

    def test_input_channels_is_a_count_and_output_is_a_mode(self):
        sections = self._cava_sections()
        assert sections["input"]["channels"].isdigit()
        assert sections["output"]["channels"] in ("mono", "stereo")

    def test_framerate_is_under_general(self):
        # index.js looks for it there specifically.
        sections = self._cava_sections()
        assert "framerate" in sections["general"]

    def _mode_field(self):
        ui = self._load_json("UIConfig.json")
        section = next(s for s in ui["sections"] if s["id"] == "level_meter")
        return section, next(c for c in section["content"] if c["id"] == "meter_mode")

    def test_config_default_matches_the_shipped_cava_config(self):
        # A fresh install must be self-consistent before anyone opens settings:
        # mono means 16 bars over one channel at full height.
        from includes.level_meter import MODE_MONO, MAX_LEVEL
        assert self._load_json("config.json")["meter_mode"]["value"] == MODE_MONO
        general, output = self._cava_sections()["general"], self._cava_sections()["output"]
        assert output["channels"] == "mono"
        assert int(general["bars"]) == 16
        assert int(output["ascii_max_range"]) == MAX_LEVEL

    def test_the_dropdown_offers_exactly_the_supported_modes(self):
        from includes.level_meter import SUPPORTED_MODES
        _section, field = self._mode_field()
        assert tuple(o["value"] for o in field["options"]) == SUPPORTED_MODES

    def test_every_mode_is_labelled(self):
        _section, field = self._mode_field()
        for option in field["options"]:
            assert option["label"] and option["label"] != option["value"]

    def test_the_field_is_saved_by_its_section(self):
        section, _field = self._mode_field()
        assert "meter_mode" in section["saveButton"]["data"]

    def test_the_old_switch_is_gone(self):
        # It became a select. Leaving the switch behind would give two controls
        # writing the same thing, with the switch silently winning on save.
        section, _field = self._mode_field()
        assert "meter_stereo" not in [c["id"] for c in section["content"]]
        assert "meter_stereo" not in section["saveButton"]["data"]
        assert "meter_stereo" not in self._load_json("config.json")

    def test_the_old_switch_still_migrates(self):
        # index.js rewrites the key on plugin start, but a config that has not
        # been through that yet must still pick the right layout.
        from includes.level_meter import MODE_STEREO, MODE_MONO
        assert index.parse_meter_mode({"meter_stereo": {"value": True}}) == MODE_STEREO
        assert index.parse_meter_mode({"meter_stereo": {"value": False}}) == MODE_MONO

    def test_a_known_mode_wins_over_the_old_switch(self):
        from includes.level_meter import MODE_ROWS_EDGES
        config = {"meter_mode": {"value": "rows_edges"}, "meter_stereo": {"value": True}}
        assert index.parse_meter_mode(config) == MODE_ROWS_EDGES

    def test_an_unknown_mode_falls_back_rather_than_crashing(self):
        from includes.level_meter import MODE_MONO
        assert index.parse_meter_mode({"meter_mode": {"value": "spiral"}}) == MODE_MONO
        assert index.parse_meter_mode({}) == MODE_MONO

class TestVuModeSettings:
    """The VU mode is the only one that needs cava configured differently rather
    than merely differently sized, so three numbers have to agree across files
    that cannot import each other.
    """

    def _plugin_dir(self):
        import os
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _index_js(self):
        import io
        import os
        with io.open(os.path.join(self._plugin_dir(), "index.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def _meter_modes_block(self):
        js = self._index_js()
        start = js.index("var METER_MODES = {")
        return js[start:js.index("};", start)]

    def _cava_general(self):
        import io
        import os
        section, sections = None, {}
        path = os.path.join(self._plugin_dir(), "cava", "retrotuner-cava.conf")
        with io.open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip()
                    sections[section] = {}
                elif "=" in line and section is not None:
                    key, _, value = line.partition("=")
                    sections[section][key.strip()] = value.strip()
        return sections["general"]

    def test_the_shipped_config_carries_every_key_index_js_rewrites(self):
        # applyCavaSettings aborts the whole write if a key is missing rather
        # than adding it, so a key absent here silently freezes cava's settings.
        general = self._cava_general()
        for key in ("bars", "framerate", "autosens", "sensitivity"):
            assert key in general, key

    def test_every_mode_declares_autosens_and_sensitivity(self):
        # A mode missing either would write "undefined" into cava's config.
        import re
        block = self._meter_modes_block()
        modes = re.findall(r"(\w+):\s*\{([^}]*)\}", block)
        assert len(modes) == len(level_meter.SUPPORTED_MODES)
        for name, body in modes:
            assert "autosens" in body, name
            assert "sensitivity" in body, name

    def test_vu_is_the_only_mode_with_autosens_off(self):
        # It is the whole point of the mode: autosens normalises a quiet passage
        # up to look like a loud one, which is the one thing a meter must not do.
        import re
        block = self._meter_modes_block()
        off = [name for name, body in re.findall(r"(\w+):\s*\{([^}]*)\}", block)
               if re.search(r"autosens:\s*0\b", body)]
        assert off == [level_meter.MODE_VU]

    def test_vu_asks_cava_for_one_value_per_sub_column(self):
        # ascii_max_range has to match the bar's resolution or the meter either
        # moves in five-pixel jumps (too low) or clips (too high).
        import re
        block = self._meter_modes_block()
        body = re.search(r"vu:\s*\{([^}]*)\}", block).group(1)
        declared = int(re.search(r"range:\s*(\d+)", body).group(1))
        assert declared == level_meter.VU_COLUMNS

    def test_vu_reads_the_same_stereo_stream_as_the_row_modes(self):
        import re
        body = re.search(r"vu:\s*\{([^}]*)\}", self._meter_modes_block()).group(1)
        assert int(re.search(r"bars:\s*(\d+)", body).group(1)) == 32
        assert "channels: 'stereo'" in body

    def test_the_dropdown_offers_it(self):
        import io
        import json
        import os
        with io.open(os.path.join(self._plugin_dir(), "UIConfig.json"),
                     encoding="utf-8") as handle:
            ui = json.load(handle)
        for section in ui["sections"]:
            for item in section.get("content", []):
                if item.get("id") == "meter_mode":
                    values = [o["value"] for o in item["options"]]
                    assert level_meter.MODE_VU in values
                    return
        raise AssertionError("no meter_mode field in UIConfig.json")
