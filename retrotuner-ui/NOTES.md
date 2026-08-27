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
below — so the real cause of the priority 8 failure is not established.)

**Do not wrap `rt_fifo` in a `type plug`.** An earlier revision did, forcing
44100/S16_LE, which made the two `multi` branches impose contradictory
constraints. alsa-lib then asserts:

    snd1_pcm_hw_param_get_min: Assertion `!snd_interval_empty(i)' failed

and MPD dies with SIGABRT.

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

Note the tap converts **format but not rate**, so cava sees whatever the source
plays at. Its config assumes 44100; a 48kHz stream still analyses, with the
bands shifted slightly. Fixing it properly would need `type volumiohook` with
`hw_params_command` (fusion does this for CamillaDSP) plus a cava restart per
rate change — judged not worth it.

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

| Mode | bars | channels | ascii_max_range |
|---|---|---|---|
| `mono` | 16 | mono | 16 |
| `stereo` | 16 | stereo | 16 |
| `rows_edges` / `rows_centre` | 32 | stereo | 4 |

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

## Display driver (`rpi-lcd-menu` fork)

A frame is 34 controller instructions: cursor home, 16 characters, a set-address
for row 2, 16 more.

Two changes took a frame from ~14ms to ~2.7ms (ceiling roughly 370fps):

1. **`LCD_SETDDRAMADDR`, not `LCD_RETURNHOME`.** Both leave the cursor at 0, but
   return-home is a 1.52ms instruction where set-address is 37µs — and
   `write4bits` waits only 50µs before the next byte, so the gap was 30× too
   short. It only worked because the padding delays in `pulseEnable` happened to
   stretch it.
2. **No delays in `pulseEnable`.** `sleep()` has a floor of tens of
   microseconds however small a value you pass, so three nominal 1µs delays cost
   ~180µs per nibble and dominated every write. A single `RPi.GPIO` output call
   already takes 1–2µs, comfortably over the 450ns the enable pulse needs.

(2) is only safe while the GPIO calls are slow. Driving E from pigpio waves or C
would need real timing back. (1) must land before (2), since (2) removes the
padding that was hiding it.

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

**Log noise:** `ControllerMpd::pushError ... reading 'split'` and `updateQueue
error: null` appear even when everything is working. Not a signal.

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

## Testing

`pytest` from the plugin directory. Everything is pure logic — no test touches a
display, a fifo or a socket.

Several tests exist to pin things that cannot import each other and would
otherwise drift silently: the cava config against `level_meter.py`'s constants,
`UIConfig.json`'s options against `SUPPORTED_MODES` and `SUPPORTED_FRAME_RATES`,
and the cava config's section layout against what `index.js` rewrites.
