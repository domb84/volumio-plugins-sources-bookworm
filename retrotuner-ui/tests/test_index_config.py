"""Tests for the config-parsing helpers in index.py."""
import json
import logging

import pytest

import index


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
