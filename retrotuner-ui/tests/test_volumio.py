"""Tests for includes/volumio.py: URI regexes, pushState dedup, debounce guard."""
import json
import queue
import threading
from unittest.mock import Mock

from includes.volumio import Volumio


class TestRegexes:
    def test_stream_uri(self):
        assert Volumio.STREAM_URI_REGEX.match("http://example.com/stream")
        assert Volumio.STREAM_URI_REGEX.match("https://example.com")
        assert Volumio.STREAM_URI_REGEX.match("spotify:track:abc123")
        assert not Volumio.STREAM_URI_REGEX.match("radio/genres")

    def test_webradio_uri_requires_a_path(self):
        assert Volumio.WEBRADIO_URI_REGEX.match("http://host/path")
        assert Volumio.WEBRADIO_URI_REGEX.match("https://host/path.mp3")
        assert not Volumio.WEBRADIO_URI_REGEX.match("http://hostonly")

    def test_spotify_track(self):
        assert Volumio.SPOTIFY_TRACK_REGEX.match("spotify:track:xyz")
        assert not Volumio.SPOTIFY_TRACK_REGEX.match("spotify:album:xyz")

    def test_browse_uri(self):
        assert Volumio.BROWSE_URI_REGEX.match("radio")
        assert Volumio.BROWSE_URI_REGEX.match("radio/genres")
        assert Volumio.BROWSE_URI_REGEX.match("spotify")
        assert Volumio.BROWSE_URI_REGEX.match("spotify:playlists")
        # a spotify *track* is a stream, not a browse target
        assert not Volumio.BROWSE_URI_REGEX.match("spotify:track:abc")

    def test_safe_menu_item(self):
        assert Volumio.SAFE_MENU_ITEM_REGEX.match("abc_123-X")
        assert not Volumio.SAFE_MENU_ITEM_REGEX.match("abc/def")


_PLAY_STATE = {
    "status": "play", "title": "Song", "artist": "Artist", "album": "Album",
    "uri": "u", "service": "webradio", "bitrate": "320", "samplerate": "44.1",
    "bitdepth": "16", "channels": "2",
}


def _volumio_with_mocked_schedule():
    v = Volumio.__new__(Volumio)
    v.last_core_state = None
    v._force_next_state = False
    v.menuManagerQ = queue.Queue()
    v._schedule_info_update = Mock()
    return v


class TestPushStateDedup:
    def test_new_track_schedules_a_normal_update(self):
        v = _volumio_with_mocked_schedule()
        v._on_push_state(_PLAY_STATE)
        assert v._schedule_info_update.call_count == 1
        _args, kwargs = v._schedule_info_update.call_args
        assert not kwargs.get("only_if_pending")
        assert not kwargs.get("immediate")

    def test_repeated_identical_state_is_only_refreshed_if_pending(self):
        v = _volumio_with_mocked_schedule()
        v._on_push_state(_PLAY_STATE)
        v._on_push_state(dict(_PLAY_STATE))  # radio re-sends the same track
        assert v._schedule_info_update.call_count == 2
        _args, kwargs = v._schedule_info_update.call_args
        assert kwargs.get("only_if_pending") is True

    def test_info_button_forces_immediate_update(self):
        v = _volumio_with_mocked_schedule()
        v._on_push_state(_PLAY_STATE)
        v._force_next_state = True  # info button pressed
        v._on_push_state(dict(_PLAY_STATE))
        _args, kwargs = v._schedule_info_update.call_args
        assert kwargs.get("immediate") is True

    def test_not_playing_sends_message_and_no_info(self):
        v = _volumio_with_mocked_schedule()
        v._on_push_state({"status": "stop"})
        assert v._schedule_info_update.call_count == 0
        item = v.menuManagerQ.get_nowait()
        assert "message" in item


def _volumio_with_real_schedule():
    v = Volumio.__new__(Volumio)
    v._pending_info_lock = threading.Lock()
    v._pending_info_timer = None
    v.menuManagerQ = queue.Queue()
    return v


class TestScheduleOnlyIfPending:
    def test_skips_when_nothing_is_pending(self):
        v = _volumio_with_real_schedule()
        v._schedule_info_update("payload", only_if_pending=True)
        assert v._pending_info_timer is None
        assert v.menuManagerQ.empty()

    def test_reschedules_when_an_update_is_pending(self):
        v = _volumio_with_real_schedule()
        # Simulate an as-yet-undisplayed update with a long-lived dummy timer.
        v._pending_info_timer = threading.Timer(100, lambda: None)
        v._pending_info_timer.start()
        try:
            v._schedule_info_update("payload", only_if_pending=True)
            assert v._pending_info_timer is not None
        finally:
            if v._pending_info_timer is not None:
                v._pending_info_timer.cancel()


class TestFavourites:
    def _volumio(self):
        v = Volumio.__new__(Volumio)
        v.add_favourite = Mock()
        v.remove_favourite = Mock()
        v._schedule_browse_refresh = Mock()
        return v

    def test_memory_item_adds_favourite(self):
        v = self._volumio()
        v._process_memory_item({"memory": json.dumps({"title": "T", "uri": "U", "service": "S"})})
        v.add_favourite.assert_called_once_with("T", "U", "S")
        v.remove_favourite.assert_not_called()

    def test_remove_favourite_item_removes(self):
        v = self._volumio()
        v._process_remove_favourite_item({"remove_favourite": json.dumps(
            {"title": "T", "uri": "U", "service": "S"})})
        v.remove_favourite.assert_called_once_with("T", "U", "S")
        v.add_favourite.assert_not_called()

    def test_invalid_payload_is_ignored(self):
        v = self._volumio()
        v._process_remove_favourite_item({"remove_favourite": "{not valid json"})
        v.remove_favourite.assert_not_called()

    def test_queue_routes_remove_favourite(self):
        v = self._volumio()
        v._process_remove_favourite_item = Mock()
        v._process_queue_item({"remove_favourite": "x"})
        v._process_remove_favourite_item.assert_called_once()

    def test_remove_favourite_schedules_a_browse_refresh(self):
        v = self._volumio()
        v._process_remove_favourite_item({"remove_favourite": json.dumps(
            {"title": "T", "uri": "U", "service": "S"})})
        v._schedule_browse_refresh.assert_called_once()

    def test_invalid_payload_does_not_schedule_a_refresh(self):
        v = self._volumio()
        v._process_remove_favourite_item({"remove_favourite": "{not valid json"})
        v._schedule_browse_refresh.assert_not_called()


class TestBrowseRefreshAfterRemoval:
    """Removing a favourite re-browses the on-screen list so the menu rebuilds."""

    _BROWSE_PUSH = {"navigation": {"lists": [{"items": [
        {"title": "Song", "uri": "u1", "service": "mpd", "type": "song", "position": 0},
    ]}]}}

    def _volumio(self):
        v = Volumio.__new__(Volumio)
        v.menuManagerQ = queue.Queue()
        v._last_browse_uri = None
        v._refresh_browse = False
        v._refresh_timer = None
        v.get_sources = Mock()
        return v

    def test_no_refresh_when_no_list_has_been_browsed(self):
        v = self._volumio()
        v._schedule_browse_refresh()  # e.g. still on the sources menu
        v.get_sources.assert_not_called()

    def test_refresh_rebrowses_the_current_uri(self):
        v = self._volumio()
        v._refresh_current_browse("favourites")
        assert v._refresh_browse is True
        v.get_sources.assert_called_once_with("favourites")

    def test_refreshed_push_replaces_menu_without_history(self):
        v = self._volumio()
        v._refresh_browse = True
        v._on_push_browse_library(self._BROWSE_PUSH)
        item = v.menuManagerQ.get_nowait()
        assert item["remember"] is False
        assert v._refresh_browse is False  # one-shot flag

    def test_normal_push_is_remembered(self):
        v = self._volumio()
        v._on_push_browse_library(self._BROWSE_PUSH)
        item = v.menuManagerQ.get_nowait()
        assert item["remember"] is True


class TestButtonRouting:
    """_process_button_item routes named actions to the correct handler."""

    def _volumio(self):
        v = Volumio.__new__(Volumio)
        v.stop = Mock()
        return v

    def test_stop_calls_stop(self):
        v = self._volumio()
        v._process_button_item('stop')
        v.stop.assert_called_once()

    def test_stop_and_clear_calls_stop(self):
        """Long-press pause: stop + clear queue via the same stop() method."""
        v = self._volumio()
        v._process_button_item('stop_and_clear')
        v.stop.assert_called_once()

    def test_stop_and_stop_and_clear_are_independent_calls(self):
        v = self._volumio()
        v._process_button_item('stop')
        v._process_button_item('stop_and_clear')
        assert v.stop.call_count == 2
