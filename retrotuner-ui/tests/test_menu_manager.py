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
    m._level_meter = None      # hidden beta feature; off unless started
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


class TestIdleFallsBackToTheMeter:
    """30s after the last control input the display rests on the level meter --
    but only while something is actually playing. Paused or stopped there is
    nothing to draw, so it asks for the state and shows the track or the menu."""

    def _manager(self, playing, meter_running=False, starts=True):
        m = _bare_manager()
        m.volumioQ = queue.Queue()
        m._playing = playing
        m._meter_auto = False
        m._idle_timer = Mock()
        m._level_meter = Mock()
        m._level_meter.running = meter_running
        m._level_meter.start.return_value = starts
        return m

    def test_playing_starts_the_meter_instead_of_asking_for_info(self):
        m = self._manager(playing=True)
        m._on_menu_idle()
        m._level_meter.start.assert_called_once_with(announce=False)
        assert m._meter_auto is True
        assert m.volumioQ.empty()

    def test_paused_or_stopped_asks_for_info_and_leaves_the_meter_alone(self):
        m = self._manager(playing=False)
        m._on_menu_idle()
        m._level_meter.start.assert_not_called()
        assert m._meter_auto is False
        assert m.volumioQ.get_nowait() == {'show': 'info'}

    def test_a_meter_that_will_not_start_falls_back_to_info(self):
        # cava down: the meter refuses, so the idle screen is the track, not a
        # blank display.
        m = self._manager(playing=True, starts=False)
        m._on_menu_idle()
        assert m._meter_auto is False
        assert m.volumioQ.get_nowait() == {'show': 'info'}

    def test_an_already_running_meter_is_not_restarted(self):
        m = self._manager(playing=True, meter_running=True)
        m._on_menu_idle()
        m._level_meter.start.assert_not_called()

    def test_nothing_is_rearmed_so_the_meter_is_the_resting_state(self):
        m = self._manager(playing=True)
        m._on_menu_idle()
        assert m._idle_timer is None


class TestPlayingUpdatesDismissTheIdleMeter:
    """Pausing from the web UI fires no control, so the only thing that can get
    an idle meter off the screen is the play-state push."""

    def _manager(self, meter_auto):
        m = _bare_manager()
        m.volumioQ = queue.Queue()
        m._playing = True
        m._meter_auto = meter_auto
        m._level_meter = Mock()
        m._level_meter.running = True
        return m

    def test_stopping_dismisses_an_idle_meter_and_asks_what_is_playing(self):
        m = self._manager(meter_auto=True)
        m._stop_auto_meter()
        m._level_meter.stop.assert_called_once()
        assert m._meter_auto is False
        # Paused renders the track with "||"; a genuine stop has no title and
        # comes back as the "no media" toast, which reverts to the menu.
        assert m.volumioQ.get_nowait() == {'show': 'info'}

    def test_a_meter_the_user_asked_for_survives_the_music_stopping(self):
        m = self._manager(meter_auto=False)
        m._playing = False
        # _meter_auto is False, so the queue loop never calls _stop_auto_meter.
        m._level_meter.stop.assert_not_called()
        assert m._level_meter.running is True
