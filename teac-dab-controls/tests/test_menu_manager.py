"""Tests for includes/menu_manager.py: the restart-marker gate and menu building."""
import json
import os
import time
from unittest.mock import Mock

from includes import menu_manager as mm


class TestConsumeRestartMarker:
    def test_absent_marker_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm, "_RESTART_MARKER_PATH", str(tmp_path / "marker"))
        assert mm.MenuManager._consume_restart_marker() is False

    def test_fresh_marker_returns_true_and_is_consumed(self, tmp_path, monkeypatch):
        marker = tmp_path / "marker"
        marker.write_text("x")
        monkeypatch.setattr(mm, "_RESTART_MARKER_PATH", str(marker))
        assert mm.MenuManager._consume_restart_marker() is True
        assert not marker.exists()

    def test_stale_marker_returns_false_but_is_still_removed(self, tmp_path, monkeypatch):
        marker = tmp_path / "marker"
        marker.write_text("x")
        old = time.time() - 60  # older than the 30s freshness window
        os.utime(marker, (old, old))
        monkeypatch.setattr(mm, "_RESTART_MARKER_PATH", str(marker))
        assert mm.MenuManager._consume_restart_marker() is False
        assert not marker.exists()


class TestBuildMenuEmpty:
    """An empty menu must show a forced message and leave the current menu intact."""

    def _manager(self):
        # __init__ runs the queue loop, so build a bare instance with only what
        # build_menu touches.
        m = mm.MenuManager.__new__(mm.MenuManager)
        m.menu = Mock()
        m.menu.items = ['existing item']
        m.display_message = Mock()
        m.remember = Mock()
        return m

    def test_empty_menu_shows_forced_message(self):
        m = self._manager()
        m.build_menu(json.dumps([]))
        m.display_message.assert_called_once_with("Menu is empty", force=True)

    def test_empty_menu_keeps_current_menu_and_history(self):
        m = self._manager()
        m.build_menu(json.dumps([]))
        m.remember.assert_not_called()              # back history untouched
        assert m.menu.items == ['existing item']    # current menu not cleared
        m.menu.render.assert_not_called()           # no render of an empty menu

    def test_non_empty_menu_still_builds_and_remembers(self):
        m = self._manager()
        m.build_menu(json.dumps([{'title': 'Radio', 'uri': 'radio', 'service': 'webradio', 'type': 'folder'}]))
        m.remember.assert_called_once()
        m.menu.append_item.assert_called_once()
        m.menu.render.assert_called_once()
