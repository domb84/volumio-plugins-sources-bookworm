# https://volumio.github.io/docs/API/API_Overview.html

import logging
import queue
import threading
from typing import Optional
logger = logging.getLogger("Volumio Functions")

_INFO_DEBOUNCE_SECONDS = 0.4

# How long after a favourite removal to wait before re-browsing the current
# list. Matches the toast display time so the "removed" message is shown before
# the rebuilt menu renders over it.
_REMOVE_REFRESH_DELAY_SECONDS = 2.0

# set socketio logging
logging.getLogger('socketio').setLevel(logging.WARNING)

import json
import socketio
import re
from datetime import datetime, timedelta
from retrying import retry

class Volumio:
    """Socket.IO client to Volumio: translates events into menu messages."""

    STREAM_URI_REGEX = re.compile(r'^(https?|spotify:track):(\/\/)?.+')
    BROWSE_URI_REGEX = re.compile(r'^(?:radio(?:\/.*)?|spotify(?::(?!track:).+|\/.*)?)$')
    SAFE_MENU_ITEM_REGEX = re.compile(r'^[A-Za-z0-9_-]+$')
    WEBRADIO_URI_REGEX = re.compile(r'^https?:\/\/.+\/.+')
    SPOTIFY_TRACK_REGEX = re.compile(r'^spotify:track:.+')

    def __init__(self, volumioQ: 'queue.Queue', menuManagerQ: 'queue.Queue', stop_event=None):
        self.volumioQ = volumioQ
        self.menuManagerQ = menuManagerQ
        self._waiting = .1
        self.stop_event = stop_event
        self.last_core_state = None  # Track core state for deduplication
        self._pending_info_timer = None
        self._pending_info_lock = threading.Lock()
        self._force_next_state = False  # next pushState was explicitly requested (info button)
        self._last_browse_uri = None    # uri of the list currently on screen (for post-removal refresh)
        self._refresh_browse = False    # next pushBrowseLibrary replaces the menu without history
        self._refresh_timer = None      # pending post-removal refresh timer
        self._sleep_end_time = None     # datetime when sleep timer fires, or None if inactive

        self.ws_api = "http://localhost:3000"
        self.sio = socketio.Client(logger=False, engineio_logger=False,reconnection=True)
        # self.sio.connect(url=self.ws_api)

        # use retry from the retrying module to reconnect until it's up
        @retry(wait_fixed=1000)
        def connect():
            self.sio.connect(url=self.ws_api)

        connect()

        # define callback functions
        self.sio.on('pushState', self._on_push_state)
        self.sio.on('pushBrowseLibrary', self._on_push_browse_library)
        self.sio.on('addToFavourites', self._on_response)
        self.sio.on('pushToastMessage', self._on_toast)
        self.sio.on('urifavourites', self._on_response)
        self.sio.on('pushBrowseSources', self._on_push_browse_sources)
        self.sio.on('pushInfoNetwork', self._on_push_info_network)
        self.sio.on('pushSleep', self._on_push_sleep)

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
            self._process_button_item(item['button'])
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

    def _process_button_item(self, button: str):
        if button == 'menu':
            self._last_browse_uri = None
            self.get_browse_sources()
            logger.debug("%s", button)
            return

        if self.STREAM_URI_REGEX.match(button):
            self.play(button)
            logger.debug("%s", button)
            return

        if self.BROWSE_URI_REGEX.match(button):
            self.get_sources(button)
            logger.debug("%s", button)
            return

        if button == 'stop':
            self.stop()
            logger.debug("%s", button)
            return

        if button == 'stop_and_clear':
            self.stop()
            logger.debug("%s", button)
            return

        if button == 'toggle':
            self._send('toggle')
            logger.debug("%s", button)
            return

        if self.SAFE_MENU_ITEM_REGEX.match(button):
            self.get_sources(button)
            logger.debug("%s", button)
            return

        if button == 'system://config':
            self._build_config_menu()
            return

        if button == 'system://wifi':
            self._send('getInfoNetwork')
            return

        if button == 'system://sleep':
            sleep_menu = [
                {'title': '15 Minutes',   'uri': 'system://sleep/15',     'service': None, 'type': None, 'position': 0},
                {'title': '30 Minutes',   'uri': 'system://sleep/30',     'service': None, 'type': None, 'position': 1},
                {'title': '45 Minutes',   'uri': 'system://sleep/45',     'service': None, 'type': None, 'position': 2},
                {'title': '60 Minutes',   'uri': 'system://sleep/60',     'service': None, 'type': None, 'position': 3},
                {'title': 'Cancel Timer', 'uri': 'system://sleep/cancel', 'service': None, 'type': None, 'position': 4},
            ]
            self.menuManagerQ.put({'menu': json.dumps(sleep_menu)})
            return

        if button == 'system://sleep/15':
            self.set_sleep(15)
            self._return_to_fresh_config()
            return

        if button == 'system://sleep/30':
            self.set_sleep(30)
            self._return_to_fresh_config()
            return

        if button == 'system://sleep/45':
            self.set_sleep(45)
            self._return_to_fresh_config()
            return

        if button == 'system://sleep/60':
            self.set_sleep(60)
            self._return_to_fresh_config()
            return

        if button == 'system://sleep/cancel':
            self.cancel_sleep()
            self._return_to_fresh_config()
            return

        if button == 'system://sleep/cancel/direct':
            self.cancel_sleep()
            msg = json.dumps([{'type': None, 'title': None, 'message': 'Sleep timer cancelled'}])
            self.menuManagerQ.put({'message': msg, 'force': True})
            return

        if button == 'system://sleep/cancel/refresh_config':
            self.cancel_sleep()
            self._build_config_menu(remember=False)
            return

        if button == 'system://shutdown':
            confirm_menu = [
                {'title': 'Confirm Shutdown', 'uri': 'system://shutdown/confirm', 'service': None, 'type': None, 'position': 0},
                {'title': 'Cancel',           'uri': 'system://cancel',           'service': None, 'type': None, 'position': 1},
            ]
            self.menuManagerQ.put({'menu': json.dumps(confirm_menu)})
            return

        if button == 'system://restart':
            confirm_menu = [
                {'title': 'Confirm Restart', 'uri': 'system://restart/confirm', 'service': None, 'type': None, 'position': 0},
                {'title': 'Cancel',          'uri': 'system://cancel',          'service': None, 'type': None, 'position': 1},
            ]
            self.menuManagerQ.put({'menu': json.dumps(confirm_menu)})
            return

        if button == 'system://cancel':
            self.menuManagerQ.put({'go_back': True})
            return

        if button == 'system://noop':
            return

        if button == 'system://shutdown/confirm':
            self._send('shutdown')
            return

        if button == 'system://restart/confirm':
            self._send('reboot')
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

        Volumio sometimes pushes an updated list immediately after removal and
        sometimes does not — the timer is a fallback for the latter case. If
        _on_push_browse_library fires first it cancels this timer so we don't
        re-browse (and potentially trigger a second go-back) unnecessarily.
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

            # Use dictionary.get('item', None) to get an item from a dictionary and return None if it's missing rather than needing to test for the item
            status = state.get('status', None)
            position = state.get('position', None)
            title = state.get('title', None)
            artist = state.get('artist', None)
            album = state.get('album', None)
            uri = state.get('uri', None)
            trackType = state.get('trackType', None)
            seek = state.get('seek', None)
            duration = state.get('duration', None)           
            bitrate = state.get('bitrate', None)
            samplerate = state.get('samplerate', None)
            bitdepth = state.get('bitdepth', None)            
            channels = state.get('channels', None)            
            random = state.get('random', None)            
            repeatSingle = state.get('repeatSingle', None)            
            consume = state.get('consume', None)            
            volume = state.get('volume', None)            
            dbVolume = state.get('dbVolume', None)            
            mute = state.get('mute', None)            
            disableVolumeControl = state.get('disableVolumeControl', None)            
            stream = state.get('stream', None)            
            updatedb = state.get('updatedb', None)            
            volatile = state.get('volatile', None)            
            service = state.get('service', None)

            clean_state = {
                'status': status,
                'artist': artist,
                'title': title,
                'album': album,
                'uri': uri,
                'service': service,
                'bitrate': bitrate,
                'samplerate': samplerate,
                'bitdepth': bitdepth,
                'channels': channels
            }

            # normalise empty strings to None so downstream only has to check for None
            clean_state = {k: (None if v == "" else v) for k, v in clean_state.items()}

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
                # Deduplicate on the track text only (audio fields excluded) so a
                # radio station re-sending the same track every few seconds — even
                # with a jittering bitrate — doesn't re-render and restart the LCD
                # scroll. The info button bypasses this via the force flag.
                core_state = (status, title, artist, album, uri, service)

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
                    # Same track. Only refresh an update that's still pending (not
                    # yet shown) so late-arriving audio details are folded into the
                    # first render; once it has displayed, skip — a radio re-send
                    # must not restart the scroll.
                    logger.debug("Duplicate state; refreshing only if still pending")
                    self._schedule_info_update(result, only_if_pending=True)


        except Exception as e:
            logger.error("Failed to processes incoming state: %s", e)
            

    def _schedule_info_update(self, result: str, immediate: bool = False,
                              only_if_pending: bool = False) -> None:
        """Debounce rapid successive pushState calls for the same track.

        Volumio often sends an initial state without audio details followed
        immediately by the same state with bitrate/samplerate filled in.
        Holding the update briefly and replacing it if a richer one arrives
        means only the final, complete message reaches the display.

        When ``immediate`` is set (an explicit info-button request) the update
        is sent straight away with a ``force`` flag so the menu manager shows
        it without its scroll-idle deferral.

        When ``only_if_pending`` is set the update is applied only if an earlier
        update is still waiting to be shown — used to fold late-arriving audio
        details into a not-yet-displayed message without re-rendering one that is
        already on screen (which would restart its scroll).
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

        result = json.dumps(sources_list)
        logger.debug("%s", result)
        # Volumio emits a spurious empty pushBrowseLibrary immediately after
        # removeFromFavourites (regardless of remaining items). If the timer is
        # still pending that means we haven't done the real re-browse yet, so
        # ignore this empty push entirely and let the timer fetch the actual
        # updated list. If we got real content, cancel the now-redundant timer.
        if not sources_list and self._refresh_timer is not None:
            logger.debug("Ignoring spurious empty pushBrowseLibrary while refresh timer is pending")
            return
        if sources_list and self._refresh_timer is not None:
            self._refresh_timer.cancel()
            self._refresh_timer = None
        refresh, self._refresh_browse = self._refresh_browse, False
        self.menuManagerQ.put({'menu': result, 'remember': not refresh})

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
        sources_list.append({'title': 'Configuration', 'uri': 'system://config', 'service': None, 'type': 'folder', 'position': None})
        result = json.dumps(sources_list)
        logger.debug(result)
        self.menuManagerQ.put({'menu': result})

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
        self._send('browseLibrary', {'uri': link})

    def add_favourite(self, title: Optional[str], link: Optional[str], service: Optional[str]) -> None:
        logger.debug(f"Add {title} from {link} to {service} favourites")
        self._send('addToFavourites', {'uri': link, 'title': title, 'service': service})

    def remove_favourite(self, title: Optional[str], link: Optional[str], service: Optional[str]) -> None:
        logger.debug(f"Remove {title} from {link} to {service} favourites")
        self._send('removeFromFavourites', {'uri': link, 'title': title, 'service': service})

    def search(self, title: str, link: str, service: str, playlist: Optional[str] = None) -> None:
        # TODO:
        # this feature does not work as search query is not documented
        # https://volumio.github.io/docs/API/WebSocket_APIs.html
        # https://community.volumio.org/t/rest-api-uri-for-browsing/10671
        logger.debug(f"Search for {title} from {link} in {service}")
        if playlist:
            self._send('search', {'uri':link, 'title':title, 'service':service, 'playlist':playlist})
        else:
            self._send('search', {'uri':link, 'title':title, 'service':service})

    
    def play(self, uri: str) -> None:
        # self._send('clearQueue')
        if self.WEBRADIO_URI_REGEX.match(uri):
            self._send('addPlay', {'status':'play', 'service':'webradio', 'uri':uri})
        elif self.SPOTIFY_TRACK_REGEX.match(uri):
            self._send('addPlay', {'status':'play', 'service':'spotify', 'uri':uri})
        else:
            logger.debug("URi does not match webradio or spotify: %s", uri)


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
