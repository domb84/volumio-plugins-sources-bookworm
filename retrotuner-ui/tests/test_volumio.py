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


class TestPushBrowseSources:
    """The synthetic Configuration entry must sort to the end of the sources menu."""

    def _volumio(self):
        v = Volumio.__new__(Volumio)
        v.menuManagerQ = queue.Queue()
        return v

    def _configuration_item(self, v):
        result = json.loads(v.menuManagerQ.get_nowait()["menu"])
        return next(item for item in result if item["title"] == "Configuration")

    def test_configuration_sorts_past_the_highest_real_position(self):
        # menu_manager sorts by `position or 0`; a bare None here would tie
        # Configuration for position 0, landing it above sources numbered 1+.
        v = self._volumio()
        v._on_push_browse_sources([
            {"name": "Music Library", "plugin_type": "music_service", "plugin_name": "mpd", "position": 1},
            {"name": "Webradio", "plugin_type": "music_service", "plugin_name": "webradio", "position": 2},
        ])
        assert self._configuration_item(v)["position"] == 3

    def test_configuration_still_sorts_last_when_no_source_has_a_position(self):
        # This is the real-world case: Volumio's top-level source list doesn't
        # carry positions at all. Without a backfill, menu_manager falls back to
        # its alphabetical sort, where "Configuration" sorts early regardless of
        # its own position -- so real sources must get sequential positions too,
        # to force menu_manager onto the position-sort path in the first place.
        v = self._volumio()
        v._on_push_browse_sources([
            {"name": "Music Library", "plugin_type": "music_service", "plugin_name": "mpd", "position": None},
            {"name": "Webradio", "plugin_type": "music_service", "plugin_name": "webradio", "position": None},
        ])
        result = json.loads(v.menuManagerQ.get_nowait()["menu"])
        assert [item["title"] for item in result] == ["Music Library", "Webradio", "Configuration"]
        assert [item["position"] for item in result] == [0, 1, 2]


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

    def test_next_sends_next(self):
        v = self._volumio()
        v._send = Mock()
        v._process_button_item('next')
        v._send.assert_called_once_with('next')

    def test_prev_sends_prev(self):
        v = self._volumio()
        v._send = Mock()
        v._process_button_item('prev')
        v._send.assert_called_once_with('prev')

    def test_next_does_not_fall_through_to_browse_library(self):
        # 'next'/'prev' match SAFE_MENU_ITEM_REGEX, so they must be handled
        # before that catch-all or they'd be treated as a browse URI.
        v = self._volumio()
        v._send = Mock()
        v.get_sources = Mock()
        v._process_button_item('next')
        v.get_sources.assert_not_called()


class TestPlayAll:
    """The synthetic "Play All" entry: offered by item type, not by URI scheme."""

    SPOTIFY_PLAYLIST = 'spotify:user:spotify:playlist:37i9dQZF1DX4UtSsGT1Sbe'

    def _volumio(self):
        v = Volumio.__new__(Volumio)
        v._send = Mock()
        v.menuManagerQ = queue.Queue()
        v._refresh_timer = None
        v._refresh_browse = False
        v._last_browse_uri = None
        v._last_browse_kind = None
        v._browse_kinds = {}
        return v

    def _enter(self, v, uri, item_type, service='spop'):
        """Browse a list containing `uri`, then navigate into it."""
        v._remember_browse_kinds([{'uri': uri, 'type': item_type, 'service': service}])
        v.get_sources(uri)
        v._on_push_browse_library({'navigation': {'lists': [{'items': [
            {'title': 'A Track', 'uri': 'x:track:1', 'service': service,
             'type': 'song', 'position': 0},
        ]}]}})
        return v.menuManagerQ.get_nowait()

    # --- which containers get the entry ---

    def test_offered_for_every_container_type(self):
        for item_type in ('playlist', 'album', 'artist', 'folder'):
            v = self._volumio()
            assert self._enter(v, 'x://thing', item_type)['play_all'], item_type

    def test_not_offered_for_navigation_categories(self):
        # These reach the Spotify plugin's unresolved else-branch; offering
        # Play All there would hang the request rather than fail it.
        for item_type in ('streaming-category', 'spotify-category',
                          'radio-category', 'music_service', 'song', 'webradio'):
            v = self._volumio()
            assert self._enter(v, 'x://thing', item_type)['play_all'] is None, item_type

    def test_not_offered_when_the_type_is_unknown(self):
        v = self._volumio()
        v.get_sources('x://never-listed')          # entered without a listing
        v._on_push_browse_library({'navigation': {'lists': [{'items': [
            {'title': 'T', 'uri': 'x:1', 'service': 's', 'type': 'song', 'position': 0}]}]}})
        assert v.menuManagerQ.get_nowait()['play_all'] is None

    def test_not_offered_for_synthetic_system_entries(self):
        v = self._volumio()
        assert self._enter(v, 'system://config', 'folder', None)['play_all'] is None

    # --- source independence ---

    def test_works_for_spotify(self):
        v = self._volumio()
        item = self._enter(v, self.SPOTIFY_PLAYLIST, 'playlist', 'spop')
        v._process_button_item(item['play_all'])
        v._send.assert_called_with('addPlay', {'uri': self.SPOTIFY_PLAYLIST, 'service': 'spop'})

    def test_works_for_a_volumio_playlist(self):
        v = self._volumio()
        item = self._enter(v, 'playlists/Roadtrip', 'playlist', 'mpd')
        v._process_button_item(item['play_all'])
        v._send.assert_called_with('addPlay', {'uri': 'playlists/Roadtrip', 'service': 'mpd'})

    def test_works_for_a_library_folder_with_slashes_in_the_uri(self):
        v = self._volumio()
        item = self._enter(v, 'music-library/USB/Albums/Rumours', 'folder', 'mpd')
        v._process_button_item(item['play_all'])
        v._send.assert_called_with(
            'addPlay', {'uri': 'music-library/USB/Albums/Rumours', 'service': 'mpd'})

    def test_works_for_a_third_party_plugin_uri(self):
        v = self._volumio()
        item = self._enter(v, 'volusonic/album/1234', 'folder', 'volusonic')
        v._process_button_item(item['play_all'])
        v._send.assert_called_with(
            'addPlay', {'uri': 'volusonic/album/1234', 'service': 'volusonic'})

    def test_service_is_omitted_when_the_listing_had_none(self):
        v = self._volumio()
        item = self._enter(v, 'somewhere/else', 'folder', None)
        v._process_button_item(item['play_all'])
        v._send.assert_called_with('addPlay', {'uri': 'somewhere/else'})

    # --- behaviour ---

    def test_clears_the_queue_before_adding(self):
        v = self._volumio()
        v.play_all('x://thing', 'mpd')
        # c[0] is the positional-args tuple (call.args needs python 3.8).
        assert [c[0][0] for c in v._send.call_args_list] == ['clearQueue', 'addPlay']

    def test_entry_carries_the_container_not_live_browse_state(self):
        # Baked into the entry so a menu restored from back-history still plays
        # the right container, whatever the browse state has moved on to.
        v = self._volumio()
        item = self._enter(v, self.SPOTIFY_PLAYLIST, 'playlist', 'spop')
        v._last_browse_uri = 'spotify:playlist:something-else'
        v._last_browse_kind = None
        v._process_button_item(item['play_all'])
        v._send.assert_called_with('addPlay', {'uri': self.SPOTIFY_PLAYLIST, 'service': 'spop'})

    def test_empty_container_is_refused(self):
        v = self._volumio()
        v.play_all('')
        v._send.assert_not_called()
