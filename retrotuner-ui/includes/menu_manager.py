import logging
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime
from time import sleep
from typing import Optional

import json
import re

logger = logging.getLogger("Menu Manager")

_SCROLL_IDLE_SECONDS = 3.0
_MENU_IDLE_SECONDS = 30.0

# Written by index.js just before a self-triggered restart so we can tell a
# restart (capture/settings save) apart from a genuine stop/shutdown.
_RESTART_MARKER_PATH = "/tmp/retrotuner-ui-restarting"

from rpilcdmenu import RpiLCDMenu, DisplayController
from rpilcdmenu.items import FunctionItem

from .level_meter import FRAMES_PER_SECOND, LevelMeter

# Plain ASCII: the HD44780-compatible display has no arrow glyph, and this
# mirrors the "+" prefix build_menu puts on folders.
#
# One chevron, not two: rpilcdmenu renders the selected row as ">" + text, so
# the cursor supplies the second one and this reads ">> PLAY ALL" in place.
PLAY_ALL_LABEL = '> Play All'

class MenuManager:
    """LCD menu manager: consumes control/menu queues and updates the LCD."""

    def __init__(self, controlQ: 'queue.Queue', volumioQ: 'queue.Queue', menuManagerQ: 'queue.Queue', lcdRS: int = 7, lcdE: int = 8, lcdD4: int = 25, lcdD5: int = 24, lcdD6: int = 23, lcdD7: int = 15, stop_event=None, rotary_skip_track: bool = False,
                 meter_frame_rate: int = FRAMES_PER_SECOND):
        self.controlQ = controlQ
        self.volumioQ = volumioQ
        self.menuManagerQ = menuManagerQ
        self.stop_event = stop_event
        self._lcd_pins = (lcdRS, lcdE, lcdD4, lcdD5, lcdD6, lcdD7)
        # Off by default: on some encoders, electrical noise registers as a
        # turn even untouched, which is harmless as a stray menu-scroll but can
        # unexpectedly skip/stop playback once enabled. See _menu_up/_menu_down.
        self._rotary_skip_track = rotary_skip_track

        # menu access times
        self.menuAccessTime = datetime.now()
        self.lastMessageTime = datetime.now()
        self.messageTime = datetime.now()
        self.last_10_items = deque([],maxlen=10)

        # log last message for deduplication
        self.lastMessage = ""
        self._pending_render_timer: Optional[threading.Timer] = None
        self._suppressed_info: Optional[str] = None
        self._info_release_timer: Optional[threading.Timer] = None
        self._idle_timer: Optional[threading.Timer] = None
        self._current_context: Optional[str] = None

        # True while the rotary encoder scrolls the menu; False once the screen
        # has fallen back to showing what's playing, where it skips tracks
        # instead. Only ever set alongside an actual LCD write (menu.render() or
        # menu.message() in show_track_info) so it always matches what's really
        # on screen -- see build_menu, _render_menu and show_track_info.
        self._nav_mode = True

        # How fast the level meter redraws. Set from the plugin settings, which
        # also rewrite cava's own framerate -- the two are halves of one setting.
        self._meter_frame_rate = meter_frame_rate

        # Hidden beta feature; built lazily so it costs nothing until used.
        self._level_meter = None

        # Whether audio is playing, pushed by the Volumio worker on change. The
        # idle timeout uses it to decide between the meter and the now-playing
        # screen; pause and stop both count as not playing.
        self._playing = False
        # True only while the meter is up because the display went idle. A meter
        # the user asked for by long-pressing the dimmer stays until they dismiss
        # it, even if the music stops.
        self._meter_auto = False

    def run(self):
        """Claim the display and service the queues until stopped. Thread entry point.

        Separate from __init__ so the class can be constructed without a display
        attached, and so the LCD is claimed on the thread that drives it.
        """
        lcdRS, lcdE, lcdD4, lcdD5, lcdD6, lcdD7 = self._lcd_pins

        # init menu
        self.menu = RpiLCDMenu(lcdRS, lcdE, [lcdD4, lcdD5, lcdD6, lcdD7], scrolling_menu=False)
        self._display = DisplayController()
        self._display.on()
        self.menu.message(('Initialising...').upper(), autoscroll=True)

        # cava owns the audio tap and does the analysis; this only draws what it
        # publishes, so nothing here can affect playback.
        self._level_meter = LevelMeter(self.menu, on_stop=self._render_menu,
                                       frame_rate=self._meter_frame_rate)

        # render main menu
        self.volumioQ.put({'button': 'menu'})

        # define control actions
        self.control_actions = {
            'menu_up': self._menu_up,
            'menu_down': self._menu_down,
            'btn_main_menu': lambda: self.volumioQ.put({'button': 'menu'}),
            'btn_enter': self.menu.processEnter,
            'btn_radio': lambda: self.volumioQ.put({'button': 'radio'}),
            'btn_pause': lambda: self.volumioQ.put({'button': 'toggle'}),
            'btn_pause_long': lambda: self.volumioQ.put({'button': 'stop_and_clear'}),
            'btn_info': lambda: self.volumioQ.put({'show': 'info'}),
            'btn_spotify': lambda: self.volumioQ.put({'button': 'spotify'}),
            'btn_favourite': self.add_favorite,
            'btn_favourite_long': self.remove_favorite,
            'btn_sleep_timer': lambda: self.volumioQ.put({'button': 'system://sleep'}),
            'btn_sleep_timer_long': self._cancel_sleep_timer,
            'btn_dimmer': lambda: self._display.toggle(),
            # Hidden beta: long-press the dimmer for the audio level meter.
            'btn_dimmer_long': self._toggle_level_meter,
            'btn_back': lambda: self.menuManagerQ.put({'menu': self.go_back(), 'remember':False})
        }


        # Use blocking gets with timeout to reduce CPU usage
        while not (self.stop_event and self.stop_event.is_set()):
            queueItem = None
            try:
                queueItem = self.controlQ.get(timeout=0.5)
                source = 'control'
            except queue.Empty:
                try:
                    queueItem = self.menuManagerQ.get(timeout=0.5)
                    source = 'menuManager'
                except queue.Empty:
                    continue

            logger.debug(f"Processing item {queueItem} from {source}")
            try:
                if 'control' in queueItem:
                    action = queueItem['control']
                    if action in self.control_actions:
                        # The meter owns the whole display, so any control other
                        # than its own toggle means the user wants the menu back.
                        if action != 'btn_dimmer_long' and self._meter_active():
                            self._level_meter.stop()
                            self._meter_auto = False
                        self.menuAccessTime = datetime.now()
                        if self._suppressed_info is not None:
                            self._defer_info(self._suppressed_info)
                        self._reset_idle_timer()
                        self.control_actions[action]()
                    else:
                        logger.warning(f"Unknown control action: {action}")
                elif 'menu' in queueItem:
                    if queueItem['menu']:
                        self._current_context = queueItem.get('context')
                        self.build_menu(queueItem['menu'], queueItem.get('remember', True),
                                        queueItem.get('play_all'))
                elif 'info' in queueItem:
                    # An explicitly requested info update (info button) must show
                    # immediately; only automatic pushState updates are deferred
                    # while the user is scrolling the menu.
                    if queueItem.get('force'):
                        if self._info_release_timer is not None:
                            self._info_release_timer.cancel()
                            self._info_release_timer = None
                        self._suppressed_info = None
                        self.show_track_info(queueItem['info'])
                    else:
                        idle = (datetime.now() - self.menuAccessTime).total_seconds()
                        if idle < _SCROLL_IDLE_SECONDS:
                            logger.debug("Deferring track info during menu activity")
                            self._defer_info(queueItem['info'])
                        else:
                            self.show_track_info(queueItem['info'])
                elif 'go_back' in queueItem:
                    previous = self.go_back()
                    if previous:
                        self.build_menu(previous, remember=False)
                elif 'pop_history' in queueItem:
                    self.go_back()  # discard stale history entry without rendering it
                elif 'message' in queueItem:
                    self.show_message(queueItem['message'],
                                      force=queueItem.get('force', False),
                                      persist=queueItem.get('persist', False))
                elif 'playing' in queueItem:
                    self._playing = queueItem['playing']
                    if not self._playing and self._meter_auto:
                        # Paused or stopped. An idle meter has nothing left to
                        # draw, so get it off the screen and ask what state we
                        # are in: paused shows the track, stopped falls back to
                        # the menu via the "no media" toast.
                        self._stop_auto_meter()
                elif 'clear' in queueItem:
                    self.display_message("", clear=True)
                else:
                    logger.warning("Queue item did not match any filters: %s", queueItem)
            except Exception as e:
                logger.error("Failed to process queue item: %s", e)
                try:
                    logger.error("Failed item %s from %s", queueItem, source)
                    logger.error("processEnter needs to be resolved in the upstream module")
                except Exception:
                    logger.exception(e)
            finally:
                # Prevent tight-looping in case of repeated errors; yield CPU briefly
                sleep(0.01)

        # cleanup on exit
        logger.info('Menu manager stopping')
        self._show_shutdown_message()


    def remember(self) -> None:
        # save the last menu for history
        menu = []
        index = self.menu.current_option

        for item in self.menu.items:
            menuItem = item.__getattribute__('args')
            # logger.debug(item.__getattribute__('args'))
            # Create a dictionary for the current item
            saveData = {
                'position': menuItem[0],
                'title': menuItem[1],
                'uri': menuItem[2],
                # Return None if theres no service
                'service': next(iter(menuItem[3:]), None)
            }
            menu.append(saveData)

        menu = {'menu': menu, 'index':index}
        
        self.last_10_items.appendleft(json.dumps(menu))

    def go_back(self) -> Optional[str]:
        if len(self.last_10_items) > 1:
            return self.last_10_items.popleft()
        return None

    def _menu_up(self) -> None:
        """Scroll the menu in nav mode; skip to the next track once we've
        fallen back to the now-playing screen (only if rotary_skip_track is
        enabled -- otherwise this always scrolls, regardless of nav mode)."""
        if self._nav_mode or not self._rotary_skip_track:
            self.menu.processDown()
        else:
            self.volumioQ.put({'button': 'next'})

    def _menu_down(self) -> None:
        """Scroll the menu in nav mode; skip to the previous track once we've
        fallen back to the now-playing screen (only if rotary_skip_track is
        enabled -- otherwise this always scrolls, regardless of nav mode)."""
        if self._nav_mode or not self._rotary_skip_track:
            self.menu.processUp()
        else:
            self.volumioQ.put({'button': 'prev'})

    def _toggle_level_meter(self) -> None:
        """Start or stop the hidden audio level meter (long-press dimmer).

        While it runs it owns the display, so the normal menu/track-info
        rendering is paused -- see _meter_active.
        """
        if self._level_meter is None:      # not reached once run() has started
            self._level_meter = LevelMeter(self.menu, on_stop=self._render_menu,
                                           frame_rate=self._meter_frame_rate)

        if self._level_meter.running:
            self._level_meter.stop()
            self._meter_auto = False
            logger.info("Level meter off")
        elif self._level_meter.start():
            # Asked for explicitly, so it outlives the music: stopping playback
            # won't dismiss it the way it dismisses an idle one.
            self._meter_auto = False
            logger.info("Level meter on")

    def _meter_active(self) -> bool:
        return self._level_meter is not None and self._level_meter.running

    def _render_menu(self) -> None:
        """Re-render the current menu and restore the rotary encoder to nav mode.

        Used wherever the menu can reappear without going through build_menu --
        e.g. a toast's deferred revert -- so nav mode always matches what a
        render actually just put on screen.
        """
        self._nav_mode = True
        self.menu.render()

    def _selected_favourite(self) -> Optional[str]:
        """Return JSON {title, uri, service} for the highlighted menu item.

        The args are [position, name, uri, service] as built in build_menu.
        Returns None if there's no selectable item.
        """
        try:
            args = self.menu.items[self.menu.current_option].__getattribute__('args')
        except (AttributeError, IndexError) as e:
            logger.error("No selectable item for favourite action: %s", e)
            return None

        favourite = {'title': args[1], 'uri': args[2], 'service': args[3]}
        logger.debug("Selected favourite: %s", favourite)
        return json.dumps(favourite)

    def add_favorite(self) -> None:
        favourite = self._selected_favourite()
        if favourite is not None:
            self.volumioQ.put({'memory': favourite})

    def remove_favorite(self) -> None:
        favourite = self._selected_favourite()
        if favourite is not None:
            self.volumioQ.put({'remove_favourite': favourite})

    def _cancel_sleep_timer(self) -> None:
        if self._current_context == 'config':
            # Already in the config menu — cancel and rebuild it in place so the
            # label updates immediately without needing to navigate away and back.
            self.volumioQ.put({'button': 'system://sleep/cancel/refresh_config'})
        else:
            # Elsewhere — cancel and show a confirmation message.
            self.volumioQ.put({'button': 'system://sleep/cancel/direct'})

    def _defer_info(self, info: str) -> None:
        """Hold a track-info update until scroll activity has been idle for _SCROLL_IDLE_SECONDS."""
        self._suppressed_info = info
        if self._info_release_timer is not None:
            self._info_release_timer.cancel()
        self._info_release_timer = threading.Timer(_SCROLL_IDLE_SECONDS, self._flush_deferred_info)
        self._info_release_timer.daemon = True
        self._info_release_timer.start()

    def _flush_deferred_info(self) -> None:
        self._info_release_timer = None
        if self._suppressed_info is not None:
            info, self._suppressed_info = self._suppressed_info, None
            self.show_track_info(info)

    @staticmethod
    def _consume_restart_marker() -> bool:
        """True if this stop is one of our own restarts (capture/settings save).

        index.js drops a marker file just before it restarts the service, so a
        genuine stop/shutdown is the default. The freshness window guards against
        a stale marker left by a restart that never actually stopped us.
        """
        try:
            if not os.path.exists(_RESTART_MARKER_PATH):
                return False
            age = time.time() - os.path.getmtime(_RESTART_MARKER_PATH)
            os.remove(_RESTART_MARKER_PATH)
            return age < 30
        except Exception:
            return False

    def _show_shutdown_message(self) -> None:
        """Show a message then blank the LCD when the service is stopping.

        Skipped for our own restarts. Done synchronously (no timers, which get
        killed as the process exits).
        """
        if self.menu is None or self._consume_restart_marker():
            return
        try:
            # Stop any pending timers from drawing over the shutdown screen.
            self._cancel_pending_render()
            if self._info_release_timer is not None:
                self._info_release_timer.cancel()
                self._info_release_timer = None
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

            self.menu.message("Shutting down...".upper())
            sleep(1.5)
            self.menu.clearDisplay()
        except Exception as e:
            logger.error("Failed to show shutdown message: %s", e)

    def _schedule_deferred(self, callback, delay: float = 2.0) -> None:
        """Schedule a deferred LCD action (render or clear) without blocking the queue thread."""
        if self._pending_render_timer is not None:
            self._pending_render_timer.cancel()
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()
        self._pending_render_timer = timer

    def _cancel_pending_render(self) -> None:
        if self._pending_render_timer is not None:
            self._pending_render_timer.cancel()
            self._pending_render_timer = None

    def _reset_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(_MENU_IDLE_SECONDS, self._on_menu_idle)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _on_menu_idle(self) -> None:
        """Fall back to a resting display 30s after the last control input.

        While something is playing that is the level meter -- on a tuner front
        panel the spectrum is what should be sitting there when nobody is
        looking, and the track is still one press of INFO away. Paused or
        stopped there is nothing to draw, so we ask for the state instead and
        get the track or the menu.

        Nothing is re-armed here: the meter is the resting state, and the next
        control press both dismisses it and starts the timer again.
        """
        self._idle_timer = None

        if self._playing and self._level_meter is not None and not self._meter_active():
            # Quiet: nobody asked for the meter, so a missing cava must not put
            # an error over whatever is currently on screen.
            if self._level_meter.start(announce=False):
                self._meter_auto = True
                logger.info("Level meter on (idle)")
                return

        self.volumioQ.put({'show': 'info'})

    def _stop_auto_meter(self) -> None:
        """Dismiss an idle-started meter and show what's playing instead.

        stop() re-renders the menu through its on_stop hook, so the info request
        is what actually decides the screen: a paused track renders with "||",
        while a genuine stop has no title and comes back as "No media is
        playing", which reverts to the menu on its own.
        """
        self._meter_auto = False
        if self._meter_active():
            self._level_meter.stop()
        self.volumioQ.put({'show': 'info'})

    def display_message(self, message, clear=False, static=False, autoscroll=False, force=False):
        # The level meter has the display; a background track-info or toast
        # update would otherwise paint straight over it.
        if self._meter_active():
            return
        # clear: clears the display and renders nothing after (for shutdown)
        # static: leaves the message on screen with nothing rendering over it
        # autoscroll: scrolls the message then leaves it on screen
        # force: bypasses duplicate/rate suppression so the message always shows
        # default: shows the message, then renders the menu after 2 seconds

        self.messageTime = datetime.now()
        lastMessageTime = (self.messageTime - self.lastMessageTime).total_seconds()

        # check if message is a duplicate, or allow duplicates if last message was longer than 5 seconds ago
        if force or (self.lastMessage != message and lastMessageTime > 2) or lastMessageTime > 5:
            if self.menu is not None:
                if clear == True:
                    self.menu.message(message.upper())
                    self.lastMessageTime = datetime.now()
                    self._schedule_deferred(self.menu.clearDisplay)
                    return
                elif static == True:
                    self._cancel_pending_render()
                    self.lastMessageTime = datetime.now()
                    self.lastMessage = message
                    return self.menu.message(message.upper(), autoscroll=False)
                elif autoscroll == True:
                    self._cancel_pending_render()
                    self.lastMessageTime = datetime.now()
                    self.lastMessage = message
                    return self.menu.message(message.upper(), autoscroll=True)
                else:
                    self.menu.message(message.upper())
                    self.lastMessageTime = datetime.now()
                    self.lastMessage = message
                    self._schedule_deferred(self._render_menu)
                    return

            return self
        else:
            logger.debug("Skipping duplicate message")


    def show_track_info(self, payload: str) -> None:
        try:

            statusSymbols = {'play': '>', 'stop': '[]', 'pause': '||'}

            logger.debug("Track info args: %s", payload)
            input_data = json.loads(payload)

            for i in input_data:
                logger.debug("Track info input: %s", i)

                symbol   = statusSymbols.get(i['status'], i['status'])
                artist   = i['artist']
                title    = i['title']
                album    = i['album']
                bitrate  = i['bitrate']
                bitdepth = i['bitdepth']

                track       = '/'.join(str(x) for x in [title, artist] if x is not None)
                first_line  = f"{symbol} {track}" if track else symbol
                quality     = '/'.join(str(x) for x in [bitrate, bitdepth] if x is not None)
                second_line = '/'.join(x for x in [album, quality] if x)

                message = f"{first_line}\n{second_line}"
                self._nav_mode = False  # rotary now skips tracks instead of scrolling
                self.display_message(message, autoscroll=True)

        except Exception as e:
            logger.error("Failed to process track info: %s", e)


    def show_message(self, payload: str, force: bool = False, persist: bool = False) -> None:
        """Show one or more {type, title, message} toasts from a JSON list payload.

        force=True bypasses duplicate/rate suppression; persist=True skips the
        deferred menu re-render, so the message stays until the next interaction.
        """
        logger.debug("Message input: %s", payload)
        input_data = json.loads(payload)

        for i in input_data:
            logger.debug("Message input: %s", i)
            try:
                type = i.get('type', None)
                title = i.get('title', None)
                message = i.get('message', None)

                if title:
                    message = f"{title}\n{message}"

                self.display_message(message, autoscroll=True, force=force)
                if not persist:
                    self._schedule_deferred(self._render_menu)
            except Exception as e:
                logger.error("Failed to process message: %s", e)


    def build_menu(self, payload: str, remember: bool = True, play_all_uri: Optional[str] = None):

        # possible types that are folders
        folderTypes = ['folder', '-category', 'favourites', 'playlist', 'music_service']

        def is_folder(item) -> bool:
            item_type = item.get('type') or ''
            return any(item_type.endswith(folder_type) for folder_type in folderTypes)

        logger.debug("Message menu: %s", payload)
        input_data = json.loads(payload)
        
        # check if the instance is a list (i.e. the input from volumio)
        if isinstance(input_data, list):
            input_data = {'menu': input_data, 'index': 0}

        index = input_data.get('index', 0)
        menu = input_data.get('menu', None)

        # An empty menu after a background refresh (remember=False, e.g. deleting
        # the last favourite) navigates up to the parent instead of showing a
        # stale list. Otherwise just tell the user and stay put. Checked before
        # remember() so the back-button history isn't polluted, and forced past
        # duplicate suppression since resolve_item usually just showed this title.
        if not menu:
            if not remember:
                previous = self.go_back()
                if previous:
                    self.menuManagerQ.put({'menu': previous, 'remember': False})
                    return
            return self.display_message("Menu is empty", force=True)

        # save last rendered menu for back button
        if remember:
            logger.debug("Saving last menu")
            self.remember()

        # clear the current menu items before building the new menu
        if self.menu is not None:
            self.menu.items = []

        # Items arrive with every key present but possibly None, so `or ''`/`or 0`
        # is needed rather than .get() defaults -- an unnamed Spotify playlist
        # would otherwise crash the sort and abort the whole menu build.
        if menu and menu[0].get('position') is not None:
            menu = sorted(menu, key=lambda x: (x.get('position') or 0))
        else:
            menu = sorted(menu, key=lambda x: (
                is_folder(x),                            # folders after tracks
                (x.get('title') or '').strip().lower()   # then by title
            ))

        # parse menu
        counter = 0

        # Offer to play the whole container, but only when the list actually
        # holds playable items -- a list of sub-folders has nothing to play.
        # Added before the loop so it takes position 0, which also keeps it
        # first when this menu is restored from back-history (remember() saves
        # each item's position and build_menu sorts by it).
        if play_all_uri and any(not is_folder(i) for i in menu):
            logger.debug("Adding %s -> %s", PLAY_ALL_LABEL, play_all_uri)
            self.menu.append_item(
                FunctionItem(PLAY_ALL_LABEL, self.resolve_item,
                             [counter, PLAY_ALL_LABEL, play_all_uri, None])
            )
            counter += 1
        elif play_all_uri:
            logger.debug("Play All offered (%s) but no playable items in this list",
                         play_all_uri)

        for i in menu:
            logger.debug("Menu input: %s", i)
            try:
                buttonName = i.get('title', None)
                buttonLink = i.get('uri', None)
                buttonService = i.get('service', None)

                # covers both "" and None (e.g. an unnamed Spotify playlist)
                if not buttonName:
                    logger.debug("Skipping unnamed menu item at position %d", counter)
                    continue

                if is_folder(i):
                    buttonName = f"+{buttonName}"

                if buttonService:
                            menuItem = FunctionItem(buttonName, self.resolve_item, [counter, buttonName, buttonLink, buttonService])
                # genres in webradio do not seem to return it's service type, so capture this and resolve
                elif not buttonService and buttonLink and re.match(r'radio(/.+)?', buttonLink):
                    menuItem = FunctionItem(buttonName, self.resolve_item, [counter, buttonName, buttonLink, 'webradio'])
                else:
                    menuItem = FunctionItem(buttonName, self.resolve_item, [counter, buttonName, buttonLink, None])
                # add to main menu
                self.menu.append_item(menuItem)
                counter += 1

            except Exception as e:
                logger.error("Failed to process menu input: %s", e)
        
        self.menu.current_option = index

        # A real menu is now on screen, so the rotary encoder scrolls it again.
        self._nav_mode = True

        # return rendered menu
        # if you do not return the menu it will render the original one again
        return self.menu.render()

    def resolve_item(self, item_index: int, button_name: str, button_link: str, button_service: str) -> None:
        logger.debug("item %d pressed", item_index)
        logger.debug("item name: %s", button_name)
        logger.debug("item link: %s", button_link)
        logger.debug("item service: %s", button_service)
        self.display_message(button_name.lstrip('+'), autoscroll=True)
        # The owning service travels with the item. Volumio routes addPlay by
        # service name, and guessing it from the URI later gets it wrong (the
        # Spotify plugin registers as "spop", not "spotify").
        # The title travels with the item for the same reason the service does:
        # Volumio's queue keeps whatever we send verbatim, and an item without a
        # name shows as nothing playing everywhere -- VFD and web UI alike.
        self.volumioQ.put({'button': button_link, 'service': button_service,
                           'title': button_name.lstrip('+')})


    def dimmer(self):
        self.menu.lcd.displayToggle()


