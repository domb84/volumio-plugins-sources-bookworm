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

# Written by index.js before a self-triggered restart, to tell one apart from a real shutdown.
_RESTART_MARKER_PATH = "/tmp/retrotuner-ui-restarting"

from rpilcdmenu import RpiLCDMenu, DisplayController
from rpilcdmenu.items import FunctionItem

from . import effects
from .level_meter import FRAMES_PER_SECOND, MODE_MONO, LevelMeter

# One chevron, not two: rpilcdmenu prefixes ">" to the selected row, giving ">> PLAY ALL".
PLAY_ALL_LABEL = '> Play All'

class MenuManager:
    """LCD menu manager: consumes control/menu queues and updates the LCD."""

    def __init__(self, controlQ: 'queue.Queue', volumioQ: 'queue.Queue', menuManagerQ: 'queue.Queue', lcdRS: int = 7, lcdE: int = 8, lcdD4: int = 25, lcdD5: int = 24, lcdD6: int = 23, lcdD7: int = 15, stop_event=None, rotary_skip_track: bool = False,
                 meter_frame_rate: int = FRAMES_PER_SECOND,
                 meter_mode: str = MODE_MONO,
                 boot_effect: str = effects.DEFAULT_BOOT_EFFECT,
                 boot_line1: str = '', boot_line2: str = '',
                 screensaver_effect: str = effects.DEFAULT_SCREENSAVER_EFFECT,
                 screensaver_line1: str = '', screensaver_line2: str = '',
                 screensaver_timeout: float = effects.DEFAULT_SCREENSAVER_TIMEOUT):
        self.controlQ = controlQ
        self.volumioQ = volumioQ
        self.menuManagerQ = menuManagerQ
        self.stop_event = stop_event
        self._lcd_pins = (lcdRS, lcdE, lcdD4, lcdD5, lcdD6, lcdD7)
        # Off by default: encoder noise reads as a turn, harmless as a stray scroll but not as a skipped track.
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

        # True while the rotary scrolls the menu, False once it skips tracks instead. Only
        # ever set alongside an LCD write, so it always matches what is on screen.
        self._nav_mode = True

        # Meter redraw rate. The settings page rewrites cava's framerate too - halves of one setting.
        self._meter_frame_rate = meter_frame_rate
        # Meter layout. index.js pairs it with cava's bars/channels/ascii_max_range.
        self._meter_mode = meter_mode

        # Hidden beta feature; built lazily so it costs nothing until used.
        self._level_meter = None

        # Boot graphic, played once before the first menu render.
        self._boot_effect = boot_effect
        self._boot_lines = (boot_line1, boot_line2)
        # Idle screen for when nothing is playing; the meter covers the playing case.
        self._screensaver_effect = screensaver_effect
        self._screensaver_lines = (screensaver_line1, screensaver_line2)
        self._screensaver_timeout = screensaver_timeout
        self._screensaver = None
        self._screensaver_timer: Optional[threading.Timer] = None

        # Pushed by the Volumio worker; picks the idle screen. Pause and stop both count as not playing.
        self._playing = False
        # True only for an idle-triggered meter; one the user asked for outlives the music.
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
        if not self._play_boot_effect():
            self.menu.message(('Initialising...').upper(), autoscroll=True)

        # cava owns the tap and the analysis; this only draws, so it cannot affect playback.
        self._level_meter = LevelMeter(self.menu, on_stop=self._render_menu,
                                       frame_rate=self._meter_frame_rate,
                                       mode=self._meter_mode)

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
                        # Both own the display while they run, so a press has to reclaim it.
                        self._stop_screensaver()
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
                    # Info button shows now; automatic updates defer while scrolling.
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
                    if self._playing:
                        # Music started: the meter or the track is the better idle screen.
                        self._stop_screensaver()
                    else:
                        # Playback stopping is itself the trigger, so the screensaver
                        # still arrives if nobody touches a button again.
                        self._reset_screensaver_timer()
                    if not self._playing and self._meter_auto:
                        # An idle meter has nothing left to draw once paused.
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
        self._stop_screensaver()
        self._show_shutdown_message()


    def remember(self) -> None:
        # save the last menu for history
        menu = []
        index = self.menu.current_option

        for item in self.menu.items:
            menuItem = item.__getattribute__('args')
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
                                           frame_rate=self._meter_frame_rate,
                                           mode=self._meter_mode)

        if self._level_meter.running:
            self._level_meter.stop()
            self._meter_auto = False
            logger.info("Level meter off")
        elif self._level_meter.start():
            # Asked for explicitly, so stopping playback won't dismiss it the way it dismisses an idle one.
            self._meter_auto = False
            logger.info("Level meter on")

    def _meter_active(self) -> bool:
        return self._level_meter is not None and self._level_meter.running

    def _screensaver_active(self) -> bool:
        return self._screensaver is not None and self._screensaver.running

    def _play_boot_effect(self) -> bool:
        """Draw the boot graphic, blocking until it finishes.

        On the menu thread on purpose: nothing else can usefully happen before
        the first menu render, and a thread would race it onto the display.
        """
        effect = effects.boot_effect(self._boot_effect)
        if effect is None:
            return False
        line1 = self._boot_lines[0] or effects.default_text()
        player = effects.EffectPlayer(self.menu, effect, line1=line1,
                                      line2=self._boot_lines[1],
                                      display=self._display)
        logger.info("Boot graphic: %s", effect.id)
        return player.play()

    def _start_screensaver(self) -> bool:
        effect = effects.screensaver_effect(self._screensaver_effect)
        if effect is None:
            return False
        line1 = self._screensaver_lines[0] or effects.default_text()
        # on_stop redraws the menu, the same hook the meter uses to hand the display back.
        self._screensaver = effects.EffectPlayer(
            self.menu, effect, line1=line1, line2=self._screensaver_lines[1],
            display=self._display, on_stop=self._render_menu)
        return self._screensaver.start()

    def _stop_screensaver(self) -> None:
        if self._screensaver_active():
            self._screensaver.stop()
            logger.info("Screensaver off")
        self._screensaver = None

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
            if self._screensaver_timer is not None:
                self._screensaver_timer.cancel()
                self._screensaver_timer = None

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
        self._reset_screensaver_timer()

    def _reset_screensaver_timer(self) -> None:
        """Re-arm the screensaver countdown. Longer than the menu idle timeout,
        so the track or menu gets its turn on screen first."""
        if self._screensaver_timer is not None:
            self._screensaver_timer.cancel()
            self._screensaver_timer = None
        if self._screensaver_timeout <= 0 or self._screensaver_effect == effects.NONE:
            return
        self._screensaver_timer = threading.Timer(self._screensaver_timeout,
                                                  self._on_screensaver_idle)
        self._screensaver_timer.daemon = True
        self._screensaver_timer.start()

    def _on_screensaver_idle(self) -> None:
        """Take the display for the screensaver. Not re-armed: the next press
        dismisses it and restarts the countdown."""
        self._screensaver_timer = None
        # The meter is the idle screen while something plays, and it has already claimed the display.
        if self._playing or self._meter_active():
            return
        self._start_screensaver()

    def _on_menu_idle(self) -> None:
        """Fall back to a resting display 30s after the last control input.

        The meter while something plays, the track or menu otherwise. Nothing is
        re-armed: the next control press dismisses it and restarts the timer.
        """
        self._idle_timer = None

        if self._playing and self._level_meter is not None and not self._meter_active():
            # Quiet: nobody asked for the meter, so a missing cava must not overwrite the screen.
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

    def display_message(self, message, clear=False, autoscroll=False, force=False):
        """Show a message on the LCD.

        clear      blank the display afterwards (shutdown)
        autoscroll scroll it, then leave it on screen
        force      bypass duplicate/rate suppression
        default    show it, then re-render the menu after 2 seconds
        """
        # The meter or screensaver owns the display; a background toast would paint over it.
        if self._meter_active() or self._screensaver_active():
            return None

        self.messageTime = datetime.now()
        since_last = (self.messageTime - self.lastMessageTime).total_seconds()
        # A repeat is allowed once the previous message has been up a while.
        if not (force or (self.lastMessage != message and since_last > 2) or since_last > 5):
            logger.debug("Skipping duplicate message")
            return None
        if self.menu is None:
            return self

        self.lastMessageTime = datetime.now()
        if clear:
            self.menu.message(message.upper())
            self._schedule_deferred(self.menu.clearDisplay)
            return None

        self.lastMessage = message
        if autoscroll:
            self._cancel_pending_render()
            return self.menu.message(message.upper(), autoscroll=True)

        self.menu.message(message.upper())
        self._schedule_deferred(self._render_menu)
        return None


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

        # An empty refresh goes up to the parent, not a stale list. Before remember(), to keep history clean.
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

        # Keys are present but may be None, so `or 0`, not a .get() default - None crashes the sort.
        if menu and menu[0].get('position') is not None:
            menu = sorted(menu, key=lambda x: (x.get('position') or 0))
        else:
            menu = sorted(menu, key=lambda x: (
                is_folder(x),                            # folders after tracks
                (x.get('title') or '').strip().lower()   # then by title
            ))

        # parse menu
        counter = 0

        # Only if the list holds something playable. Before the loop, so it takes position 0.
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

        # Must return the menu, or the original one is rendered again.
        return self.menu.render()

    def resolve_item(self, item_index: int, button_name: str, button_link: str, button_service: str) -> None:
        logger.debug("item %d pressed", item_index)
        logger.debug("item name: %s", button_name)
        logger.debug("item link: %s", button_link)
        logger.debug("item service: %s", button_service)
        self.display_message(button_name.lstrip('+'), autoscroll=True)
        # Service and title both travel with the item; an untitled queue item reads
        # as nothing playing. See NOTES.md ("Volumio quirks").
        self.volumioQ.put({'button': button_link, 'service': button_service,
                           'title': button_name.lstrip('+')})


    def dimmer(self):
        # Through the menu, not menu.lcd: reaching past the library's lock puts a second
        # writer on the data pins and desyncs the bus. See NOTES.md ("Display driver").
        self.menu.toggle_display()


