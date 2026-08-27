# https://volumio.github.io/docs/API/API_Overview.html

import logging
import queue
import threading
from typing import Optional
logger = logging.getLogger("Volumio Functions")

_INFO_DEBOUNCE_SECONDS = 0.4

# Matches the toast display time, so "removed" is seen before the rebuilt menu
# renders over it.
_REMOVE_REFRESH_DELAY_SECONDS = 2.0

# Synthetic "play the whole container" entry. The URI travels in the entry
# itself, so a menu restored from back-history still plays the right thing.
PLAYALL_URI_PREFIX = 'system://playall/'
PLAYALL_NO_SERVICE = '-'

# Item types whose contents can be queued as a whole. No URI pattern
# generalises across services, so the type is the only shared signal.
# Categories are excluded deliberately -- see NOTES.md ("Volumio quirks").
CONTAINER_TYPES = ('playlist', 'album', 'artist', 'folder')

# The uri -> (type, service) map accumulates rather than tracking one listing,
# so it needs a bound. An evicted entry is re-learned on the next browse.
BROWSE_KIND_CACHE_MAX = 2000

# set socketio logging
logging.getLogger('socketio').setLevel(logging.WARNING)

import json
import socketio
import re
from datetime import datetime, timedelta
from tenacity import retry, wait_fixed

class Volumio:
    """Socket.IO client to Volumio: translates events into menu messages."""

    STREAM_URI_REGEX = re.compile(r'^(https?|spotify:track):(\/\/)?.+')
    BROWSE_URI_REGEX = re.compile(r'^(?:radio(?:\/.*)?|spotify(?::(?!track:).+|\/.*)?)$')
    SAFE_MENU_ITEM_REGEX = re.compile(r'^[A-Za-z0-9_-]+$')
    WEBRADIO_URI_REGEX = re.compile(r'^https?:\/\/.+\/.+')
    SPOTIFY_TRACK_REGEX = re.compile(r'^spotify:track:.+')

    SLEEP_MINUTES = (15, 30, 45, 60)

    # The only fields of Volumio's state the display uses.
    STATE_KEYS = ('status', 'artist', 'title', 'album', 'uri', 'service',
                  'bitrate', 'samplerate', 'bitdepth', 'channels')
    DEDUP_KEYS = ('status', 'title', 'artist', 'album', 'uri', 'service')

    def __init__(self, volumioQ: 'queue.Queue', menuManagerQ: 'queue.Queue', stop_event=None):
        self.volumioQ = volumioQ
        self.menuManagerQ = menuManagerQ
        self._waiting = .1
        self.stop_event = stop_event
        self.last_core_state = None  # Track core state for deduplication
        self._pending_info_timer = None
        self._pending_info_lock = threading.Lock()
        self._force_next_state = False  # next pushState was explicitly requested (info button)
        self._last_playing = None       # last play/not-play we told the menu manager about
        self._last_browse_uri = None    # uri of the list currently on screen (for post-removal refresh)
        self._refresh_browse = False    # next pushBrowseLibrary replaces the menu without history
        self._refresh_timer = None      # pending post-removal refresh timer
        self._sleep_end_time = None     # datetime when sleep timer fires, or None if inactive
        self._browse_kinds = {}         # uri -> (type, service) for the list on screen
        self._last_browse_kind = None   # (type, service) of the container we are inside

        self.ws_api = "http://localhost:3000"
        self.sio = socketio.Client(logger=False, engineio_logger=False,reconnection=True)
        # self.sio.connect(url=self.ws_api)

        # define callback functions
        self.sio.on('pushState', self._on_push_state)
        self.sio.on('pushBrowseLibrary', self._on_push_browse_library)
        self.sio.on('addToFavourites', self._on_response)
        self.sio.on('pushToastMessage', self._on_toast)
        self.sio.on('urifavourites', self._on_response)
        self.sio.on('pushBrowseSources', self._on_push_browse_sources)
        self.sio.on('pushInfoNetwork', self._on_push_info_network)
        self.sio.on('pushSleep', self._on_push_sleep)

    def run(self):
        """Connect to Volumio and service the queue until stopped. Thread entry point.

        Separate from __init__ so constructing this class performs no I/O -- the
        connect retries forever, which would block startup if it ran anywhere but
        this thread.
        """
        # Retry until Volumio's socket.io server is up. Note tenacity takes
        # seconds where the old `retrying` took milliseconds.
        @retry(wait=wait_fixed(1))
        def connect():
            self.sio.connect(url=self.ws_api)

        connect()

        # Sync sleep timer state with Volumio on startup
        self._send('getSleep')

        # Process incoming requests from the volumioQ using blocking get
        while not (self.stop_event and self.stop_event.is_set()):
            try:
                item = self.volumioQ.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._process_queue_item(item)
            except Exception as e:
                logger.error("Failed to process queue item: %s", e)
            finally:
                try:
                    self.volumioQ.task_done()
                except Exception:
                    pass

        with self._pending_info_lock:
            if self._pending_info_timer is not None:
                self._pending_info_timer.cancel()
                self._pending_info_timer = None

        try:
            self.sio.disconnect()
        except Exception as e:
            logger.warning("Failed to disconnect socket.io cleanly: %s", e)

        logger.info('Volumio worker stopping')

    def _process_queue_item(self, item):
        if 'show' in item:
            self._process_show_item(item)
        elif 'button' in item:
            self._process_button_item(item['button'], item.get('service'),
                                      item.get('title'))
        elif 'memory' in item:
            self._process_memory_item(item)
        elif 'remove_favourite' in item:
            self._process_remove_favourite_item(item)
        else:
            logger.warning("Queue item did not match filter: %s", item)

    def _process_show_item(self, item):
        if item.get('show') == 'info':
            # User pressed the info button — force the next state through the
            # dedup/debounce so it is displayed immediately.
            self._force_next_state = True
            self.get_state()
            logger.debug("%s", item)

    @staticmethod
    def _menu_payload(*titles_and_uris):
        """A menu payload from (title, uri) pairs, positions assigned in order."""
        return json.dumps([
            {'title': title, 'uri': uri, 'service': None, 'type': None, 'position': i}
            for i, (title, uri) in enumerate(titles_and_uris)
        ])

    def _show_sleep_menu(self):
        items = [('%d Minutes' % m, 'system://sleep/%d' % m) for m in self.SLEEP_MINUTES]
        items.append(('Cancel Timer', 'system://sleep/cancel'))
        self.menuManagerQ.put({'menu': self._menu_payload(*items)})

    def _show_confirm_menu(self, label, uri):
        self.menuManagerQ.put({'menu': self._menu_payload(
            (label, uri), ('Cancel', 'system://cancel'))})

    def _set_sleep_and_refresh(self, minutes):
        self.set_sleep(minutes)
        self._return_to_fresh_config()

    def _cancel_sleep_and_refresh(self):
        self.cancel_sleep()
        self._return_to_fresh_config()

    def _cancel_sleep_and_rebuild_config(self):
        self.cancel_sleep()
        self._build_config_menu(remember=False)

    def _cancel_sleep_with_message(self):
        self.cancel_sleep()
        msg = json.dumps([{'type': None, 'title': None, 'message': 'Sleep timer cancelled'}])
        self.menuManagerQ.put({'message': msg, 'force': True})

    def _button_actions(self):
        """Exact-match button handlers, rebuilt per call so tests can patch them."""
        actions = {
            'stop': self.stop,
            'stop_and_clear': self.stop,
            'toggle': lambda: self._send('toggle'),
            'next': lambda: self._send('next'),
            'prev': lambda: self._send('prev'),
            'system://config': self._build_config_menu,
            'system://wifi': lambda: self._send('getInfoNetwork'),
            'system://sleep': self._show_sleep_menu,
            'system://sleep/cancel': self._cancel_sleep_and_refresh,
            'system://sleep/cancel/direct': self._cancel_sleep_with_message,
            'system://sleep/cancel/refresh_config': self._cancel_sleep_and_rebuild_config,
            'system://shutdown':
                lambda: self._show_confirm_menu('Confirm Shutdown', 'system://shutdown/confirm'),
            'system://restart':
                lambda: self._show_confirm_menu('Confirm Restart', 'system://restart/confirm'),
            'system://shutdown/confirm': lambda: self._send('shutdown'),
            'system://restart/confirm': lambda: self._send('reboot'),
            'system://cancel': lambda: self.menuManagerQ.put({'go_back': True}),
            'system://noop': lambda: None,
        }
        for minutes in self.SLEEP_MINUTES:
            actions['system://sleep/%d' % minutes] = (
                lambda m=minutes: self._set_sleep_and_refresh(m))
        return actions

    def _process_button_item(self, button: str, service: Optional[str] = None,
                             title: Optional[str] = None):
        logger.debug("%s", button)

        if button == 'menu':
            self._last_browse_uri = None
            self.get_browse_sources()
            return

        if self.STREAM_URI_REGEX.match(button):
            self.play(button, service, title)
            return

        if self.BROWSE_URI_REGEX.match(button):
            self.get_sources(button)
            return

        # Before SAFE_MENU_ITEM_REGEX below: "stop", "next" and "prev" all match
        # it, and would otherwise be browsed for instead of acted on.
        action = self._button_actions().get(button)
        if action is not None:
            action()
            return

        if button.startswith(PLAYALL_URI_PREFIX):
            # The container may contain slashes, service names never do.
            service, _, container = button[len(PLAYALL_URI_PREFIX):].partition('/')
            self.play_all(container, None if service == PLAYALL_NO_SERVICE else service)
            return

        if self.SAFE_MENU_ITEM_REGEX.match(button):
            self.get_sources(button)
            return

        logger.warning("Unhandled button item: %s", button)

    def _parse_favourite(self, raw):
        """Parse a {title, uri, service} JSON payload into a tuple, or None."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("Invalid favourite payload: %s", e)
            return None
        logger.debug("%s", payload)
        return payload.get('title'), payload.get('uri'), payload.get('service')

    def _process_memory_item(self, item):
        parsed = self._parse_favourite(item['memory'])
        if parsed is not None:
            self.add_favourite(*parsed)

    def _process_remove_favourite_item(self, item):
        parsed = self._parse_favourite(item['remove_favourite'])
        if parsed is not None:
            # Set the flag before sending so that any pushBrowseLibrary Volumio
            # emits immediately in response to the removal is treated as a
            # refresh (remember=False) rather than a user-navigated menu.
            self._refresh_browse = True
            self.remove_favourite(*parsed)
            self._schedule_browse_refresh()

    def _schedule_browse_refresh(self):
        """Re-browse the list currently on screen after a favourite removal.

        Volumio doesn't always push an updated list after removal, so this
        timer is the fallback -- if _on_push_browse_library fires first, it
        cancels this timer to avoid a redundant re-browse.
        """
        uri = self._last_browse_uri
        if not uri:
            return
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        self._refresh_timer = threading.Timer(_REMOVE_REFRESH_DELAY_SECONDS,
                                self._refresh_current_browse, args=(uri,))
        self._refresh_timer.daemon = True
        self._refresh_timer.start()

    def _refresh_current_browse(self, uri):
        self._refresh_timer = None  # timer has fired; clear so the response isn't ignored
        self._refresh_browse = True
        self.get_sources(uri)

    def _send(self, command, args=None, callback=None, namespace=None):
        self.sio.emit(command, args, callback=callback, namespace=namespace)


    def get_state(self):
        logger.debug("Getting state")
        self._send('getState', args=None, callback=self._on_push_state)


    def _on_toast(self, *args):
        try:
            logger.debug("Toast args: %s", args)
            logger.debug("Toast args length: %d", len(args))
            toast = args[0]
            logger.debug("Toast: %s", toast)

            type = toast.get('type', None)
            title = toast.get('title', None)
            message = toast.get('message', None)

            toast_list = [{
                'type': type,
                'title': title,
                'message': message
            }]
            logger.debug("Toast: %s", toast_list)
            result = json.dumps(toast_list)
            logger.debug("Toast as json: %s", result)
            self.menuManagerQ.put({'message':result})

        except Exception as e:
            logger.error("Failed to processes incoming toast: %s", e)

    def _on_response(self, *args):
        logger.debug("%s", args)


    def _on_push_state(self, *args):
        try:
            # Consume any pending force request (set by the info button). When
            # forced we bypass dedup/debounce so the update shows immediately.
            force = self._force_next_state
            self._force_next_state = False

            # logger.debug("State: " + str(args))
            state = args[0]

            # Empty strings normalise to None so downstream only checks for None.
            # Not `or None`: that would swallow a legitimate 0 bitrate too.
            clean_state = {key: state.get(key) for key in self.STATE_KEYS}
            clean_state = {k: (None if v == "" else v) for k, v in clean_state.items()}
            status = clean_state['status']

            # Drives the menu manager's idle screen. Decided here because a
            # stop never reaches show_track_info. Pause counts as not playing;
            # sent on change only, as radio re-pushes state constantly.
            playing = status == 'play'
            if playing != self._last_playing:
                self._last_playing = playing
                self.menuManagerQ.put({'playing': playing})

            # nothing is playing if neither artist nor title is set
            all_none = clean_state['artist'] is None and clean_state['title'] is None

            # if theres too many missing items log it and skip the rest
            if status == 'play' and all_none:
                logger.warning("Now playing item missing state")
            # check if we're not actually playing anything.
            # This happens between every track change so don't show anything in this instance else we spam the display with 'stop' events.
            elif status != 'play' and all_none:
                self.last_core_state = None  # allow same track to redisplay when playback resumes
                message = json.dumps([{'message': 'No media is playing'}])
                self.menuManagerQ.put({'message': message})

            else:
                # Track text only, audio fields excluded: a radio station
                # re-sending the same track with a jittering bitrate must not
                # re-render and restart the LCD scroll.
                core_state = tuple(clean_state[k] for k in self.DEDUP_KEYS)

                # wire format is a list of one
                result = json.dumps([clean_state])
                if force:
                    # Explicit info request — always show, even if unchanged.
                    self.last_core_state = core_state
                    logger.debug("Forced state update (info button): %s", core_state)
                    self._schedule_info_update(result, immediate=True)
                elif self.last_core_state != core_state:
                    self.last_core_state = core_state
                    logger.debug("State changed: %s", core_state)
                    self._schedule_info_update(result)
                else:
                    # Same track: refresh only if an update is still pending (not
                    # yet shown), folding late audio details into the first render --
                    # once shown, skip, since a radio re-send must not restart the scroll.
                    logger.debug("Duplicate state; refreshing only if still pending")
                    self._schedule_info_update(result, only_if_pending=True)


        except Exception as e:
            logger.error("Failed to processes incoming state: %s", e)
            

    def _schedule_info_update(self, result: str, immediate: bool = False,
                              only_if_pending: bool = False) -> None:
        """Debounce rapid pushState calls for the same track.

        Volumio sends state without audio details, then again with them; holding
        the update briefly means only the final one reaches the display.
        ``immediate`` skips the wait (info button); ``only_if_pending`` folds
        late details into an update not yet shown, without restarting a scroll.
        """
        with self._pending_info_lock:
            if only_if_pending and self._pending_info_timer is None:
                return
            if self._pending_info_timer is not None:
                self._pending_info_timer.cancel()
                self._pending_info_timer = None
            if immediate:
                self.menuManagerQ.put({'info': result, 'force': True})
                return
            self._pending_info_timer = threading.Timer(
                _INFO_DEBOUNCE_SECONDS,
                self._flush_info_update,
                args=(result,),
            )
            self._pending_info_timer.daemon = True
            self._pending_info_timer.start()

    def _flush_info_update(self, result: str) -> None:
        with self._pending_info_lock:
            self._pending_info_timer = None
        self.menuManagerQ.put({'info': result})

    def _on_push_info_network(self, *args):
        try:
            networks = args[0] if args else []
            items = []
            pos = 0

            def _add(label):
                nonlocal pos
                items.append({'title': label, 'uri': 'system://noop', 'service': None, 'type': None, 'position': pos})
                pos += 1

            if not networks:
                _add('Not connected')
            else:
                for net in networks:
                    net_type = net.get('type', 'Unknown')
                    ip    = (net.get('ip')    or '').strip()
                    speed = (net.get('speed') or '').strip()
                    if net_type == 'Wireless':
                        ssid   = (net.get('ssid') or 'Unknown').strip()
                        signal = net.get('signal')
                        _add(f"SSID: {ssid}")
                        if ip:
                            _add(f"IP: {ip}")
                        if signal is not None:
                            _add(f"Signal: {signal}/5")
                        if speed:
                            _add(f"Speed: {speed}")
                    else:
                        _add('Wired')
                        if ip:
                            _add(f"IP: {ip}")
                        if speed:
                            _add(f"Speed: {speed}")

            logger.debug("Network info menu: %s", items)
            self.menuManagerQ.put({'menu': json.dumps(items)})
        except Exception as e:
            logger.error("Failed to process network info: %s", e)

    def _on_push_browse_library(self, *args):
        logger.debug("Received: %s", args)

        if not args or not args[0]:
            logger.warning("Received empty data: %s", args)
            return

        main_source = args[0].get('navigation', {}).get('lists', [])
        sources_list = []

        for lists in main_source:
            sources_list.extend(self._format_browse_items(lists.get('items', [])))

        self._remember_browse_kinds(sources_list)
        result = json.dumps(sources_list)
        logger.debug("%s", result)
        # Volumio emits a spurious empty push after removeFromFavourites; while
        # the refresh timer is pending that is not the real re-browse.
        if not sources_list and self._refresh_timer is not None:
            logger.debug("Ignoring spurious empty pushBrowseLibrary while refresh timer is pending")
            return
        if sources_list and self._refresh_timer is not None:
            self._refresh_timer.cancel()
            self._refresh_timer = None
        refresh, self._refresh_browse = self._refresh_browse, False
        play_all = self._play_all_uri()
        self.menuManagerQ.put({'menu': result, 'remember': not refresh, 'play_all': play_all})

    def _on_push_browse_sources(self, *args):
        if not args or not args[0]:
            logger.warning("Received empty data: %s", args)
            return

        items = args[0]
        for item in items:
            item['title'] = item.pop('name', None)
            item['type'] = item.pop('plugin_type', None)
            item['service'] = item.pop('plugin_name', None)

        sources_list = self._format_browse_items(items)
        # Backfilled in arrival order purely to force build_menu's position-sort
        # path; without it the list sorts alphabetically. See NOTES.md.
        if not any(item.get('position') is not None for item in sources_list):
            for index, item in enumerate(sources_list):
                item['position'] = index
        positions = [item['position'] for item in sources_list if item.get('position') is not None]
        config_position = max(positions) + 1 if positions else 0
        sources_list.append({'title': 'Configuration', 'uri': 'system://config', 'service': None, 'type': 'folder', 'position': config_position})
        self._remember_browse_kinds(sources_list)
        result = json.dumps(sources_list)
        logger.debug(result)
        self.menuManagerQ.put({'menu': result})

    def _remember_browse_kinds(self, sources_list) -> None:
        """Record what each listed item is, so entering one tells us its type.

        Accumulated, never replaced -- the back button restores menus without
        re-browsing, so a listing may never arrive again. See NOTES.md.
        """
        for item in sources_list:
            uri = item.get('uri')
            if uri:
                self._browse_kinds[uri] = (item.get('type'), item.get('service'))

        # Dicts keep insertion order, so this drops the oldest entries first.
        excess = len(self._browse_kinds) - BROWSE_KIND_CACHE_MAX
        if excess > 0:
            for stale in list(self._browse_kinds)[:excess]:
                del self._browse_kinds[stale]

    def _play_all_uri(self) -> Optional[str]:
        """The "Play All" entry for the list on screen, or None if it makes no sense.

        The container's URI and owning service are baked into the entry rather
        than read back from browse state when it is pressed, so a menu restored
        from back-history still plays the right thing.
        """
        uri = self._last_browse_uri
        if not uri or uri.startswith('system://'):
            logger.debug("No Play All: not inside a browsable container (uri=%r)", uri)
            return None

        kind = self._last_browse_kind
        if not kind:
            logger.debug(
                "No Play All: never saw %r in a listing, so its type is unknown "
                "(known: %s)", uri, sorted(self._browse_kinds)[:8],
            )
            return None
        if kind[0] not in CONTAINER_TYPES:
            logger.debug("No Play All: %r is type %r, not one of %s",
                         uri, kind[0], CONTAINER_TYPES)
            return None

        play_all = '%s%s/%s' % (PLAYALL_URI_PREFIX, kind[1] or PLAYALL_NO_SERVICE, uri)
        logger.debug("Offering Play All for %r (type %r): %s", uri, kind[0], play_all)
        return play_all

    def _format_browse_items(self, items):
        sources_list = []

        for source in items:
            menu_type = source.get('type')
            if isinstance(menu_type, str) and menu_type.strip() == '':
                menu_type = source.get('uri')

            sources_list.append({
                'title': source.get('title'),
                'uri': source.get('uri'),
                'service': source.get('service'),
                'type': menu_type,
                'position': source.get('position')
            })

        return sources_list

    def get_browse_sources(self) -> None:
        self._send('getBrowseSources')

    def get_sources(self, link: str) -> None:
        logger.debug("Get sources from %s", link)
        self._last_browse_uri = link
        # Looked up before the response replaces the listing it came from --
        # this is how we learn whether what we just entered is a playable
        # container, and which service owns it.
        self._last_browse_kind = self._browse_kinds.get(link)
        self._send('browseLibrary', {'uri': link})

    def add_favourite(self, title: Optional[str], link: Optional[str], service: Optional[str]) -> None:
        logger.debug(f"Add {title} from {link} to {service} favourites")
        self._send('addToFavourites', {'uri': link, 'title': title, 'service': service})

    def remove_favourite(self, title: Optional[str], link: Optional[str], service: Optional[str]) -> None:
        logger.debug(f"Remove {title} from {link} to {service} favourites")
        self._send('removeFromFavourites', {'uri': link, 'title': title, 'service': service})

    def search(self, title: str, link: str, service: str, playlist: Optional[str] = None) -> None:
        # TODO: does not work -- the query format is undocumented. See NOTES.md.
        logger.debug(f"Search for {title} from {link} in {service}")
        if playlist:
            self._send('search', {'uri':link, 'title':title, 'service':service, 'playlist':playlist})
        else:
            self._send('search', {'uri':link, 'title':title, 'service':service})

    
    def play(self, uri: str, service: Optional[str] = None,
             title: Optional[str] = None) -> None:
        """Queue and play a single track.

        Both `service` and `title` come from the menu item and both matter:
        service names cannot be guessed from a URI, and an untitled queue item
        reads as "nothing playing" everywhere while the audio plays fine. See
        NOTES.md ("Volumio quirks").
        """
        if not service:
            # Only reached for an item that arrived without one. Inferring from
            # the URI is the old behaviour, kept as a fallback -- note "spop",
            # which is what the Spotify plugin actually registers as.
            if self.WEBRADIO_URI_REGEX.match(uri):
                service = 'webradio'
            elif self.SPOTIFY_TRACK_REGEX.match(uri):
                service = 'spop'
            logger.debug("No service on item; inferred %r for %s", service, uri)

        if not service:
            logger.warning("Refusing to play %s: no service to route it to", uri)
            return

        logger.debug("Play %s via %s (title %r)", uri, service, title)
        payload = {'status': 'play', 'service': service, 'uri': uri}
        if title:
            payload['title'] = title
        self._send('addPlay', payload)


    def play_all(self, container_uri: str, service: Optional[str] = None) -> None:
        """Replace the queue with everything in `container_uri` and play.

        Volumio expands it server-side via the owning service's explodeUri.
        Whether a URI is explodable was decided from its type when the entry was
        offered -- no URI pattern generalises, so it cannot be re-checked here.
        """
        if not container_uri:
            logger.warning("Play all called with no container uri")
            return

        logger.debug("Play all: %s (service %s)", container_uri, service)
        payload = {'uri': container_uri}
        if service:
            payload['service'] = service
        self._send('clearQueue')
        self._send('addPlay', payload)


    def set_sleep(self, minutes: int) -> None:
        hours = minutes // 60
        mins = minutes % 60
        logger.debug("Setting sleep timer for %d minutes", minutes)
        self._sleep_end_time = datetime.now() + timedelta(minutes=minutes)
        self._send('setSleep', {'time': f'{hours}:{mins:02d}', 'enabled': True})

    def cancel_sleep(self) -> None:
        logger.debug("Cancelling sleep timer")
        self._sleep_end_time = None
        self._send('setSleep', {'time': '0:00', 'enabled': False})

    def _return_to_fresh_config(self) -> None:
        """Pop the stale config history entry then push a freshly-built config menu."""
        self.menuManagerQ.put({'pop_history': True})
        self._build_config_menu(remember=False)

    def _build_config_menu(self, remember: bool = True) -> None:
        sleep_label = 'Sleep Timer'
        if self._sleep_end_time:
            remaining_secs = int((self._sleep_end_time - datetime.now()).total_seconds())
            if remaining_secs > 0:
                remaining_mins = (remaining_secs + 59) // 60  # round up to nearest minute
                if remaining_mins >= 60:
                    h, m = divmod(remaining_mins, 60)
                    sleep_label = f"Sleep: {h}h {m}m" if m else f"Sleep: {h}h"
                else:
                    sleep_label = f"Sleep: {remaining_mins}m"
            else:
                self._sleep_end_time = None  # timer has already fired

        config_menu = [
            {'title': 'WiFi Status', 'uri': 'system://wifi',     'service': None, 'type': 'folder', 'position': 0},
            {'title': sleep_label,   'uri': 'system://sleep',    'service': None, 'type': 'folder', 'position': 1},
            {'title': 'Shutdown',    'uri': 'system://shutdown', 'service': None, 'type': 'folder', 'position': 2},
            {'title': 'Restart',     'uri': 'system://restart',  'service': None, 'type': 'folder', 'position': 3},
        ]
        self.menuManagerQ.put({'menu': json.dumps(config_menu), 'remember': remember, 'context': 'config'})

    def _on_push_sleep(self, *args) -> None:
        """Sync sleep state from Volumio (only getSleep responses carry useful data)."""
        try:
            data = args[0] if args else {}
            # setSleep resolves with {} — ignore those empty responses
            if not data or 'enabled' not in data:
                return
            if not data.get('enabled'):
                self._sleep_end_time = None
                return
            time_str = data.get('time', '0:0')
            h, m = time_str.split(':')
            remaining_mins = int(h) * 60 + int(m)
            self._sleep_end_time = datetime.now() + timedelta(minutes=remaining_mins) if remaining_mins > 0 else None
            logger.debug("Sleep state synced: %s mins remaining", remaining_mins)
        except Exception as e:
            logger.error("Failed to process sleep state: %s", e)

    def stop(self) -> None:
        self._send('stop')
        self._send('clearQueue')
