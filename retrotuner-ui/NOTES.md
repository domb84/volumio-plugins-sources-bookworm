# RetroTuner UI — design notes

Things that took a while to work out, kept here so the source can stay short.
Source comments referencing "NOTES.md" point at these sections.

---

## Audio tap (`asound/rt_in.rt_out.2.conf`)

Splits the output into the normal chain plus a copy for cava.

**The fifo must always have a reader.** An unread fifo fills its 64kB pipe in
~0.37s, ALSA then blocks on write and playback stops. cava is that reader, which
is why it runs as its own service and why `onRestart` never bounces it.

**Priority 2 is deliberate.** Filenames are `<inPCM>.<outPCM>.<priority>.conf`
and priority sorts *descending*, so a higher number sits nearer the player. At
priority 8 this sat ahead of softvolume and cava analysed silence; after
softvolume it works, and the meter follows the volume control, which is what you
want. `mpd_oled` (postalsa) and fusion (postDsp) tap in the same place.

(The original diagnosis blamed a missing `format_N` key. That was wrong — see
below — so the real cause of the priority-8 failure is not established. Since
`format_N` only constrains negotiation, the likely cause is that the format
available at priority 8 was not in the declared list, so the branch never
opened. Unverified.)

**Do not wrap `rt_fifo` in a `type plug`.** Tried twice. The first attempt
forced 44100/S16_LE and alsa-lib asserted:

    snd1_pcm_hw_param_get_min: Assertion `!snd_interval_empty(i)' failed

killing MPD with SIGABRT. The second put a converting plug on the fifo branch
only, with `format`/`rate` under `slave` rather than at the top level, on the
theory that the first attempt had merely constrained the wrong side. The device
then failed to open. Do not try a third time — and note the second attempt was
solving a problem that does not exist, see below.

**Measured behaviour (verified on hardware, 192k/24-bit source):**

| | Format | Rate |
|---|---|---|
| DAC, branch a | `S24_LE` — native | 192000 — native |
| cava, branch b | `S16_LE` — converted | 192000 — native |

So the two branches carry **different formats at the same time**: a `multi` does
not force a uniform format across its slaves, and the tap costs the output path
nothing. There is no bit-depth clamp.

That needed proving rather than assuming, because `hw_params` reports the
*container*, not the content — `S24_LE` shows the DAC is fed 24-bit words, not
that those words carry 24 bits. The check that settles it is a tone below the
16-bit floor, which an undithered S24→S16 truncation destroys outright:

    sox -n -b 24 -r 48000 -c 2 /tmp/quiet24.wav synth 30 sine 1000 vol -100dB

Played with the amp well up it is faintly but clearly audible, so the low bits
survive the tap. (16-bit carries ~96 dB of range, so −100 dBFS quantises to
nothing.)

What converts for branch b alone is still unidentified — `volumiofifo` does not,
and neither does `multi`. Only the outer `type plug` on `rt_in` can, so it
appears to convert per-slave. Behaviour established, mechanism not.

Method, since none of this is visible from the config: `cat
/proc/asound/card*/pcm*p/sub*/hw_params` while playing gives the DAC side.
For the fifo, read cava's own consumption rather than the fifo itself — a pipe
delivers each byte to one reader, so `cat`-ing it steals data from cava:

    pid=$(pgrep -f retrotuner-cava)
    a=$(awk '/^rchar/{print $2}' /proc/$pid/io); sleep 5
    b=$(awk '/^rchar/{print $2}' /proc/$pid/io)
    echo $(( (b - a) / 5 ))          # bytes/sec = rate x channels x bytes/sample

Cross-check the rate against `hw_params` at the same moment: 16-bit at 192k and
24-in-32-bit at 96k are both 768,000 B/s and otherwise indistinguishable.

**cava's `sample_rate` is wrong except on 44.1k material.** `volumiofifo` calls
`snd_pcm_ioplug_set_param_list` for `SND_PCM_IOPLUG_HW_FORMAT` only, so the
format is pinned to what we declare but the rate passes straight through. cava's
config hardcodes 44100, so its frequency mapping is off by the ratio: 1.09x at
48k, 2.2x at 96k, 4.35x at 192k. The bars still move, which is why it looks
fine. Fixing it needs `volumiohook` + `hw_params_command` (as fusion does)
rewriting `sample_rate` and restarting cava -- outside the multi, where it
cannot cause either failure above.

**`format_N` is a list, not a mapping.** volumiofifo matches on the `"format_"`
prefix with `strncmp` and never parses the suffix — it exists only because ALSA
config has no array syntax. So the number carries no meaning, and declaring the
same format under several numbers just repeats it. We declare one entry,
`S16_LE`, because cava is configured for 16-bit. fusion declares three different
formats because CamillaDSP preserves bit depth.
[Source](https://github.com/volumio/volumio-alsa-fifo/blob/master/src/pcm_volumiofifo.c)

**Do not define `pcm.rt_out`** — `alsa_controller` emits it. This file defines
`pcm.rt_in` and hands on. `package.json` must set
`volumio_info.has_alsa_contribution` or the scan skips the plugin entirely.

`mpd_oled` targets `postalsa` and peppy `peppy_out`; neither exists in this
Volumio version. Check names in `/etc/asound.conf` before changing the target.

## cava (`cava/retrotuner-cava.conf`)

Runs as `retrotuner-cava.service` so a plugin restart — which happens on every
settings save — cannot interrupt audio. It does the FFT, log frequency scaling
and falloff in C rather than in Python next to the SPI button polling.

**Config keys vary by build.** `/usr/share/cava/example_files/config` is the
authoritative reference. An earlier version set `[smoothing] noise_reduction`,
which does not exist in this build: every bar stayed at zero with nothing
logged. Check a key is there, and what range it takes, before adding it.

`framerate`, `bars`, `channels` and `ascii_max_range` are **rewritten by
index.js** from the settings page. Editing them by hand is undone on the next
save.

**Nothing drains `/tmp/retrotuner-bars` while the meter is off.** cava holds its
own read fd on it and never reads, so the pipe fills — about two minutes at
60fps — and cava blocks in its write. Playback is unaffected: the input thread
carries on draining the tap, which is the only part audio depends on. But the
64kB left sitting there is stale, so `LevelMeter._drain()` empties it on open
rather than drawing minutes-old frames. This is why restarting cava looked like
a fix: it destroys the pipe and its contents along with it.

cava scales its `gravity` falloff relative to framerate, so changing the rate
changes how the bars decay — worth a look if a rate change makes the levels
read differently.

## Level meter (`includes/level_meter.py`)

Draws cava's numbers. No DSP here.

**Eight CGRAM glyphs exist, and that is the binding constraint.**

* Full-height modes (`mono`, `stereo`) use all 8 for bar heights 1–8, stacking
  both character rows into one 16-step bar.
* Split modes (`rows_edges`, `rows_centre`) give each channel one character
  row. One row grows down from the top and the other up from the bottom, and
  those are different bitmaps — the controller cannot flip a glyph — so two sets
  are needed at once. Four slots each, hence 4 height steps per channel. The
  trade is the point: 16 frequency bands per channel instead of 8.

A true oscilloscope trace is impossible for the same reason: a scope line needs
a different glyph per column, and there are only 8.

**Glyph codes must stay in 0–7.** 0x0A is `"\n"`, which `lcd_render()` treats as
a line break and would cut a frame in half.

What each mode asks of cava — `index.js` writes these, `level_meter.py` expects
them, and neither end scales anything:

| Mode | bars | channels | ascii_max_range | autosens |
|---|---|---|---|---|
| `mono` | 16 | mono | 16 | 1 |
| `stereo` | 16 | stereo | 16 | 1 |
| `rows_edges` / `rows_centre` | 32 | stereo | 4 | 1 |
| `vu` | 32 | stereo | 80 | **0** |

**`vu` is not a spectrum, and autosens is the whole reason it needed a mode of
its own.** cava's autosens continuously renormalises so the bars fill the
display whatever the input — correct for a visualiser, and the one thing a meter
must not do, since a quiet passage gets pulled up to look like a loud one. With
`autosens = 0` and a fixed `sensitivity` the output is proportional to signal
again. It is still not a calibrated VU: no RMS of the waveform, no 300ms
standard ballistic. It moves with the music and it is honest about *relative*
level; it does not measure anything.

The reading per channel is the **mean** of that channel's 16 bands, not the
loudest — the loudest follows whichever frequency happens to dominate and jumps
about. `ascii_max_range` is 80 because a horizontal bar is 16 cells of 5 pixel
columns, so cava's range maps one-to-one onto the bar and it glides instead of
stepping five pixels at a time. Six glyphs: five bar-end fills and a peak
marker.

Ballistics are ours, not cava's: fast attack, slow release, and a peak that
holds `VU_PEAK_HOLD` before sliding back. Raw per-frame means are twitchy and
read as a spectrum.

**Every key in `applyCavaSettings`' list must already exist in the shipped cava
config.** `replaceInIniSection` replaces, it never adds, and a miss aborts the
whole write and leaves cava on its old settings. That is why `autosens` and
`sensitivity` are in the file even though the spectrum modes leave them alone.

**Channel order is an assumption.** cava sends both channels in one array laid
out for a mirrored display: first half is the left channel high-to-low so bass
meets in the centre, second half the right channel the usual way round.
`split_channels()` reverses the first half. If the channels come out swapped or
the spectrum runs backwards, that function is the only thing that decides it.

The draw interval and cava's `framerate` are two halves of one setting. Raising
one alone just buys duplicate or dropped frames.

`SILENCE_TIMEOUT` counts from the last frame with any signal in it. cava's bars
decay gradually and that decay counts as sound, so what you see is several
seconds longer than the constant.

**The idle meter only appears if a countdown is running.** `_on_menu_idle` fires
once and does not re-arm, so every trigger that reaches it without a button
press has to arm its own: `run()` at startup, a control action, and the `playing`
push. Miss one and the symptom is silent — the menu simply sits there, and a
single button press hides it by arming the timer the normal way.

## Screen effects (`includes/effects.py`)

A boot graphic, played once before the first menu render, and a screensaver for
when nothing is playing. Same shape as the level meter: the effect owns the
display, renders frames on a thread, and hands it back through an `on_stop`
hook that redraws the menu.

`frame(t, line1, line2)` is a pure function of time, so a resync can redraw the
same frame and the tests can assert on what an effect draws at any moment.
Boot effects declare a `duration` and finish; screensavers return `None` and
loop. The two registries are separate on purpose -- a looping effect chosen as
a boot graphic would hold the display before the menu had ever rendered.

**The same 8 CGRAM slots, and they shape every effect.**

| Effect | Glyphs | Why |
|---|---|---|
| Split-flap, slide-in, data rain | 0 | ROM characters only |
| Self test, typewriter | 1 | a solid block |
| Wipe, scanner | 5 | a cell filled 1–5 columns |
| VU meters | 6 | the same five, plus a peak marker |
| Meter tease, travelling wave | 8 | `bar_bitmaps()`, unchanged |
| Centre-out reveal | 8 + ROM block | four fills anchored to each edge |

* **The wipe covers left-to-right and uncovers right-to-left.** The obvious
  version — a bar sweeping right with text appearing behind it — cannot work:
  the boundary cell would need text pixels *and* block pixels in one glyph,
  which is per-cell art. Reversing the second pass keeps the covered part always
  on the *left* of a cell, so one family of left-aligned fills does both
  directions, and 5 glyphs buy an 80-step sweep across 16 cells.
* **Centre-out needs nine shapes for eight slots.** A curtain parting in both
  directions wants a left- *and* a right-aligned family, four each, which is the
  whole budget — so its solid cell is the ROM block (`chr(0xFF)`). It is the one
  effect that breaks on a module whose ROM lacks that character; `FULL_BLOCK` is
  the single place to change. Everything else spends a CGRAM slot on its block.
  It also centres its text: the curtains part from the middle, so text starting
  hard left would appear from *under* one rather than behind it.
* **The slide-in leaves again.** It arrives, holds, then carries on the way each
  row came, so the menu renders onto a clear panel rather than cutting over the
  text. It is the one boot effect whose last frame is not the name.

The VU screensaver's movement is **synthesised**, not audio. Nothing is playing
when a screensaver is up, so there is no signal to follow; the cava-driven meter
is a *mode of the level meter* (`meter_mode = vu`), not a screensaver.

**Screensavers run at 10-15 fps deliberately.** Sustained 60fps is the workload
that desynced the bus (see "Display driver") and nothing idle needs more. VFDs
also burn in, so the idle set moves and the clock wanders a column each minute.

Only bouncing text uses `screensaver_line1`; the rest are graphics. There is no
`screensaver_line2` — the marquee was its only user, and a settings field that
nothing reads is worse than no field.

Both hooks are in `menu_manager.py`: boot runs inline in `run()` in place of the
"Initialising..." message, and the screensaver hangs off
`_reset_screensaver_timer()`, armed at startup, by any control input, and by
playback stopping. It only takes the display when nothing is playing — the meter
is already the idle screen while something is. Both countdowns are armed in
`run()` rather than only on a button press, or a restart nobody touches never
reaches either. Empty text falls back to the hostname, i.e. the device name.

## Display driver (`rpi-lcd-menu` fork)

A frame is 34 controller instructions: cursor home, 16 characters, a set-address
for row 2, 16 more.

Two changes took a frame from ~14ms to ~2.7ms (ceiling roughly 370fps):

1. **`LCD_SETDDRAMADDR`, not `LCD_RETURNHOME`.** Both leave the cursor at 0, but
   return-home is a 1.52ms instruction where set-address is 37µs — and
   `write4bits` waits only 50µs before the next byte, so the gap was 30× too
   short. It only worked because the padding delays in `pulseEnable` happened to
   stretch it.
2. **No `sleep()` in `pulseEnable`.** `sleep()` has a floor of tens of
   microseconds however small a value you pass, so three nominal 1µs delays cost
   ~180µs per nibble and dominated every write.

(1) must land before (2), since (2) removes the padding that was hiding it.

### The bus desync (fixed in 2.4.0)

There is no RW pin on this wiring, so the busy flag can never be read and every
delay in the driver is a fixed guess. Undershoot one and the controller misses a
nibble; from then on every byte is assembled from the low nibble of one write and
the high nibble of the next. `0xC0` — "move to line 2" — is never seen intact, so
all 32 characters land on line 1: **line 2 dead and frozen, line 1 flickering**
because it is written twice a frame. Not gradual — clean until the first bad
nibble, broken from then on, and only a plugin restart cleared it, because
`initDisplay()` ran once in `__init__` and never again.

What undershot was (2). Its first cut removed the delays and put nothing back, on
the reasoning that a single `RPi.GPIO` call takes 1–2µs anyway and so covers the
450ns the enable pulse needs. That is Python overhead, not a guarantee: 0.4–2µs
depending on clock, cache and contention, and *shortest* under sustained
rendering, when the governor is pinned at 1.4GHz — narrowest exactly when the
display is busiest. Removing `LCD_RETURNHOME` in (1) is what made it permanent
rather than self-correcting: home also resets the display shift register every
frame, and that was the only recovery behaviour there was.

Two changes in 2.4.0:

* **`pulseEnable` holds E for a real 1µs**, busy-waiting on `perf_counter()`
  rather than sleeping — ~150µs a frame, against the ~2ms the `command_delay_us`
  sleeps already cost.
* **`resync_display()`** re-runs the handshake, reloads CGRAM and redraws.
  `lcd_render` calls it every `RESYNC_FRAME_INTERVAL` frames (600, ~10s at
  60fps), turning "broken until restart" into "one bad frame". The meter and the
  screen effects also call it on the way in and out.

**Confirmed fixed by an overnight run at 60fps with no corruption** (28 Aug 2026)
— hours of sustained rendering is the only thing that ever reproduced it.

Neither is the *correct* fix, which is to wire RW and poll the busy flag,
removing every fixed delay; that is a hardware change. Worth checking first, and
not determinable from the code: whether the module is a 5V part fed 3.3V logic,
where V_IH is ~3.5V and every edge is marginal — that would explain the thermal
sensitivity better than clock scaling alone.

### Brightness

The panel dims, which is not obvious from the wiring. Function Set is
`0 0 1 DL N F * *` and the bottom two bits are don't-cares on a genuine
HD44780; this VFD reuses them as an attenuator. Four levels, no finer control:
100, 75, 50 and 25 percent. 25 is the dimmest step, **not** off — display on/off
is a different register and the two do not disturb each other.

The bits live in the driver's `displayfunction`, so `resync_display()` restores
them along with everything else. Held anywhere else, brightness would reset to
full every ~10s at 60fps.

Brightness is hardware state, so the panel comes up at full on every process
start. The plugin therefore records the level in
`/data/configuration/user_interface/retrotuner-ui/state.json` and reapplies it
in `run()` **before the boot graphic**, or a settings save would silently undim
the display. That file is written only by the python side; it sits beside
`config.json` rather than in it so v-conf never fights it.

`BRIGHTNESS_LEVELS` in `menu_manager.py` is the cycle the dimmer steps through,
brightest first, wrapping at the bottom. **The dimmer is the one control that
does not reclaim the display**: every other press dismisses the meter or the
screensaver, but changing brightness has no reason to. Its *long* press does,
being the meter toggle.

That exemption is why `_on_screensaver_idle()` checks whether a screensaver is
already running. The dimmer still re-arms the idle countdown, so without the
check a press during a screensaver would start a second `EffectPlayer` beside
the first -- two threads rendering to one display.

### One writer at a time

Every display write goes through `RpiLCDMenu`, which serialises on a lock its
own worker thread holds while rendering. **Never call `menu.lcd.*` directly** —
that reaches past the lock and puts a second writer on the data pins, which
desyncs the bus just as reliably as a short pulse does. `menu.toggle_display()`,
not `menu.lcd.displayToggle()`; `menu.clearDisplay()`, which locks internally,
not `menu.lcd.write4bits(...)`. This matters most for anything on a
`threading.Timer` (`_schedule_deferred`) or on the meter thread, since those run
alongside the worker by definition.

`command_delay_us = 50` in `write4bits` is what gives the controller its 37µs to
settle. Removing that one *will* drop characters.

**Scroll speed is independent of render speed.** `_lcd_queue_processor`
subtracts render time from `SCROLL_INTERVAL`, so a step is 75ms of total
on-screen time either way.

Roughly 20% CPU at 60fps on a Pi 3B+, scaling linearly with frame rate.

## Volumio quirks

**Anything sent to `addPlay` must carry a title.** `CorePlayQueue.addQueueItems`
passes the whole payload to the owning service's `explodeUri`, and for a plain
stream URL webradio's implementation is a pass-through — `if (data.title) {
data.name = data.title }` and resolve. Omit it and the queue item is nameless;
`CoreStateMachine` builds pushed state from the queue item rather than from MPD,
so both the web UI and the VFD show "nothing playing" while the audio plays
fine. The web UI never hits this because it posts the entire browse item.

It only fails for *some* stations: Icecast/SHOUTcast (`.aac`, `.mp3`) supply ICY
metadata that masks the missing name, HLS (`.m3u8`) supplies none.

**`service` must come from the menu item**, not guessed from the URI — the
Spotify plugin registers as `spop`, and `spotify` names a service that does not
exist.

**Categories are not containers.** `CONTAINER_TYPES` excludes them: asking a
service to explode a category ranges from a no-op to a hang — the Spotify
plugin's `explodeUri` falls through to an else-branch that never resolves its
promise. `spotify/category/<id>` is typed `streaming-category`.

`compareTrackListByUri` dedups against the **tail** of the queue, so replaying
the current station reuses its index. Otherwise `addPlay` appends forever;
`replaceAndPlay` is what the web UI sends for a single item.

`pushState` fires every few seconds for radio, so state is deduplicated on track
text only — a jittering bitrate must not restart the LCD scroll.

**An item's type is only in the listing that contains it.** By the time you are
inside it that listing is gone, so `_remember_browse_kinds` captures it on the
way past. The map is **accumulated, never replaced**: the back button restores
menus from history without re-browsing, so the listing that told us about an
item may never arrive again. Replacing the map meant leaving a playlist and
re-entering it lost the fact that it was a playlist, and Play All vanished until
you went back to the main menu.

**Volumio emits a spurious empty `pushBrowseLibrary`** right after
`removeFromFavourites`. If the post-removal refresh timer is still pending, that
empty push is not the real re-browse — ignore it and let the timer fetch the
real list. Real content means the timer is redundant and gets cancelled.

**Browse sources carry no `position`.** `menu_manager.build_menu` only sorts by
position when the *first* item has one, otherwise it sorts alphabetically, where
"Configuration" lands early regardless. So positions are backfilled in arrival
order purely to force the position-sort path.

**`search()` does not work.** The query format is undocumented; neither
[the WebSocket API docs](https://volumio.github.io/docs/API/WebSocket_APIs.html)
nor [this thread](https://community.volumio.org/t/rest-api-uri-for-browsing/10671)
give the shape it expects.

**Log noise:** `ControllerMpd::pushError ... reading 'split'` and `updateQueue
error: null` appear even when everything is working. Not a signal.

## Button reading

Resistor ladder into an MCP3008, matched against **raw ADC counts**, not
voltages. On a measured Teac ladder adjacent buttons sit **69–141 counts
apart**, and every tolerance is sized against that gap.

The dominant error is contact resistance, not electrical noise: an aged switch
read **21 counts higher on a light press than a firm one**. So the bands and the
capture confirm tolerance both have to absorb more press-to-press variation than
noise alone would suggest.

Four values must stay in step across the two languages, because `index.js` hard
codes fallbacks for configs written before these settings existed:

| `controls.py` | `index.js` |
|---|---|
| `ADC_SAMPLES` (5) | `setValue(..., 'button_samples', 5)` |
| `BUTTON_HYSTERESIS` (6) | `setValue(..., 'button_hysteresis', 6)` |
| tolerances sized to the 69–141 gap | `CAPTURE_CONFIRM_TOLERANCE`, `CAPTURE_BAND_HALF_WIDTH` |

Samples are reduced with a **median, not a mean** — that discards a read
corrupted by an LCD strobe instead of averaging its error in. Each raw read
costs ~480µs at `SPI_CLOCK_HZ`, so raising the count eats into the poll
interval.

**Every channel reading 0 usually means the pin mux never took.** That is a
legitimate-looking value rather than an obviously broken read, so it is warned
about explicitly. Bitbang mode claims the SPI pins as plain GPIO and nothing
restores their ALT0 function afterwards — only a reboot, when the SPI driver
probes, does that.

## Config plumbing

`config.json` holds defaults; v-conf copies them to
`/data/configuration/user_interface/retrotuner-ui/config.json` and that is what
survives updates. `UIConfig.json` is the form. `index.js` validates and saves;
`index.py` reads the saved file directly.

* `saveOptions`' `isValid()` only accepts numbers and booleans. A select posts a
  **string**, so each string setting declares its permitted set in
  `STRING_SETTINGS` — accepting arbitrary strings would let a typo reach cava.
* A select also posts `{value, label}` and needs a label handed back in
  `getUIConfig`, or the control renders blank.
* A field missing from `saveButton.data` renders fine and never submits.
* `meter_stereo` (switch) became `meter_mode` (select). `onStart` migrates the
  old key once and deletes it; `index.py` has the same fallback in case it wins
  the race on first start.

**Restart semantics.** A settings save restarts the plugin service, never cava —
see the fifo note above. Only a meter setting change bounces cava, via
`try-restart` so it never starts on a box with no audio tap. `index.js` drops
`/tmp/retrotuner-ui-restarting` first so the display shows no shutdown screen
for our own restarts.

**SPI** needs `dtparam=spi=on` in `/boot/userconfig.txt` and a reboot — only a
boot re-probes the pin mux. Until then every MCP3008 read returns 0.

## Install

**Nothing that needs compiling goes in the venv.** `RPi.GPIO` and `spidev` are
the only requirements that would, so they come prebuilt from apt
(`python3-rpi.gpio`, `python3-spidev`) and the venv is created with
`--system-site-packages` so it can see them. That keeps `python3-dev` out
entirely, which matters: `python3-dev` and `python3-venv` are pinned with `=` to
the exact python3.11 build on this image, so asking for either drags apt into
upgrading python3.11/libc6/locales to match — which previously triggered a mass
service restart (needrestart, or the libc6 postinst prompt) that broke playback
and crash-looped upmpdcli. The apt packages depend on python3 by an ordinary
range instead, so they pull none of that in.

For the same reason the venv is built by `virtualenv` fetched as a standalone
zipapp, not by the stdlib `venv` module — that would need `python3-venv`, same
pin. virtualenv bundles its own pip, so it does not need `ensurepip` either.

**The venv lives outside the plugin directory** (`/data/retrotuner-ui/venv`). It
is created as root, and a root-owned subfolder inside the plugin dir stops
Volumio (running as `volumio`) removing the old folder on update, so the
update's `mv` fails with "Directory not empty".

**cava is an install dependency, not an optional extra** — it holds the tap fifo
open permanently, and an unread fifo stops playback within a second. See the
audio tap section above.

**Updating the `rpi-lcd-menu` fork needs `--force-reinstall`.** pip sees an
installed version and skips otherwise, and pushing to a branch does not move the
version number, so it cannot tell a new commit from the installed one:

```
pip install --force-reinstall --no-deps git+https://github.com/domb84/rpi-lcd-menu.git
```

Three requirements the fork has to meet, none of which pip can check, and only
the first of which fails loudly:

* `create_char()` and `render_frame()`, or the level meter dies at runtime with
  `AttributeError`.
* The render-performance work (#6) — a frame takes ~2.7ms instead of ~14ms. The
  meter defaults to 60fps and offers 120, which the older timing cannot serve:
  it would sit at 84% of wall time mid-render and starve the SPI polling.
* 2.4.0 or newer, for the enable-pulse hold and `resync_display()` — see the
  display driver section. Both degrade quietly on an older copy.

## Testing

`pytest` from the plugin directory. Everything is pure logic — no test touches a
display, a fifo or a socket.

Several tests exist to pin things that cannot import each other and would
otherwise drift silently: the cava config against `level_meter.py`'s constants,
`UIConfig.json`'s options against `SUPPORTED_MODES`, `SUPPORTED_FRAME_RATES` and
the effect registries, the effect ids across `effects.py` / `config.json` /
`UIConfig.json` / `index.js`, and the cava config's section layout against what
`index.js` rewrites.
