"""Tests for includes/menu_manager.py: the restart-marker gate and menu building."""
import json
import os
import queue
import time
from datetime import datetime, timedelta
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


def _bare_manager():
    # __init__ runs the queue loop, so build a bare instance with only what
    # build_menu touches.
    m = mm.MenuManager.__new__(mm.MenuManager)
    m.menu = Mock()
    m.menu.items = ['existing item']
    m.display_message = Mock()
    m.remember = Mock()
    return m


class TestBuildMenuEmpty:
    """An empty menu must show a forced message and leave the current menu intact."""

    def _manager(self):
        return _bare_manager()

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


class TestBuildMenuNoneFields:
    """Items arrive with every key present but possibly None (volumio.py always
    sets them). An unnamed Spotify playlist (title None) used to crash the menu
    sort and abort the whole build, so nothing rendered."""

    def test_unnamed_item_is_skipped_and_named_item_renders(self, monkeypatch):
        fi = Mock()
        monkeypatch.setattr(mm, "FunctionItem", fi)
        m = _bare_manager()
        m.build_menu(json.dumps([
            {'title': 'My Mix', 'uri': 'spotify:user:spotify:playlist:a',
             'service': 'spop', 'type': 'playlist', 'position': None},
            {'title': None, 'uri': 'spotify:user:spotify:playlist:b',
             'service': 'spop', 'type': 'playlist', 'position': None},
        ]))
        # The None-title item is silently skipped; only the named item renders.
        assert m.menu.append_item.call_count == 1
        m.menu.render.assert_called_once()
        names = [c[0][0] for c in fi.call_args_list]
        assert '+My Mix' in names

    def test_none_type_does_not_crash_sort(self):
        m = _bare_manager()
        m.build_menu(json.dumps([
            {'title': 'B', 'uri': 'x', 'service': 'spop', 'type': None, 'position': None},
            {'title': 'A', 'uri': 'y', 'service': 'spop', 'type': 'folder', 'position': None},
        ]))
        assert m.menu.append_item.call_count == 2
        m.menu.render.assert_called_once()

    def test_mixed_positions_do_not_crash_sort(self):
        m = _bare_manager()
        m.build_menu(json.dumps([
            {'title': 'A', 'uri': 'x', 'service': 'spop', 'type': 'playlist', 'position': 1},
            {'title': 'B', 'uri': 'y', 'service': 'spop', 'type': 'playlist', 'position': None},
        ]))
        assert m.menu.append_item.call_count == 2
        m.menu.render.assert_called_once()


_TRACK_INFO_PAYLOAD = json.dumps([{
    'status': 'play', 'artist': 'Artist', 'title': 'Title', 'album': 'Album',
    'bitrate': '320', 'bitdepth': '16',
}])


class TestNavMode:
    """The rotary encoder scrolls the menu in nav mode, and skips tracks once
    the screen has fallen back to showing what's playing (see _nav_mode) --
    but only when rotary_skip_track is enabled (these tests exercise that
    enabled case; TestRotarySkipTrackDisabled covers the off-by-default one)."""

    def _manager(self):
        m = _bare_manager()
        m.volumioQ = queue.Queue()
        m._nav_mode = True
        m._rotary_skip_track = True
        return m

    def test_menu_up_scrolls_in_nav_mode(self):
        m = self._manager()
        m._menu_up()
        m.menu.processDown.assert_called_once()
        assert m.volumioQ.empty()

    def test_menu_down_scrolls_in_nav_mode(self):
        m = self._manager()
        m._menu_down()
        m.menu.processUp.assert_called_once()
        assert m.volumioQ.empty()

    def test_menu_up_skips_forward_once_out_of_nav_mode(self):
        m = self._manager()
        m._nav_mode = False
        m._menu_up()
        assert m.volumioQ.get_nowait() == {'button': 'next'}
        m.menu.processDown.assert_not_called()

    def test_menu_down_skips_back_once_out_of_nav_mode(self):
        m = self._manager()
        m._nav_mode = False
        m._menu_down()
        assert m.volumioQ.get_nowait() == {'button': 'prev'}
        m.menu.processUp.assert_not_called()


class TestRotarySkipTrackDisabled:
    """Off by default: the encoder must always just scroll, regardless of nav
    mode, unless rotary_skip_track is explicitly enabled (see MenuManager
    docstring -- this is what stops encoder noise from skipping/stopping
    playback when nobody's touched the knob)."""

    def _manager(self):
        m = _bare_manager()
        m.volumioQ = queue.Queue()
        m._rotary_skip_track = False
        return m

    def test_menu_up_scrolls_even_out_of_nav_mode_when_disabled(self):
        m = self._manager()
        m._nav_mode = False
        m._menu_up()
        m.menu.processDown.assert_called_once()
        assert m.volumioQ.empty()

    def test_menu_down_scrolls_even_out_of_nav_mode_when_disabled(self):
        m = self._manager()
        m._nav_mode = False
        m._menu_down()
        m.menu.processUp.assert_called_once()
        assert m.volumioQ.empty()

    def test_show_track_info_leaves_nav_mode(self):
        m = self._manager()
        m.show_track_info(_TRACK_INFO_PAYLOAD)
        assert m._nav_mode is False

    def test_build_menu_restores_nav_mode(self):
        m = self._manager()
        m._nav_mode = False
        m.build_menu(json.dumps([{'title': 'Radio', 'uri': 'radio', 'service': 'webradio', 'type': 'folder'}]))
        assert m._nav_mode is True

    def test_render_menu_restores_nav_mode_and_renders(self):
        m = self._manager()
        m._nav_mode = False
        m._render_menu()
        assert m._nav_mode is True
        m.menu.render.assert_called_once()


def _display_manager():
    """A manager with the real display_message/show_message, not mocked out."""
    m = mm.MenuManager.__new__(mm.MenuManager)
    m.menu = Mock()
    m.lastMessage = ""
    m.lastMessageTime = datetime.now() - timedelta(seconds=10)
    m._pending_render_timer = None
    m._schedule_deferred = Mock()
    return m


class TestMenuRevertUsesRenderMenu:
    """Both toast-revert paths must restore nav mode, not just call menu.render()
    directly -- otherwise the encoder could keep skipping tracks over a menu
    it can no longer see (e.g. 'No media is playing' reverting to a stale menu)."""

    def test_display_message_default_branch_reverts_via_render_menu(self):
        m = _display_manager()
        m.display_message("hello")
        m._schedule_deferred.assert_called_once_with(m._render_menu)

    def test_show_message_reverts_via_render_menu(self):
        m = _display_manager()
        m.show_message(json.dumps([{'message': 'No media is playing'}]))
        m._schedule_deferred.assert_called_once_with(m._render_menu)
