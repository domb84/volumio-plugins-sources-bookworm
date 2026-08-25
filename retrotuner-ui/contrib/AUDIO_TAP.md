# Audio tap for the level meter (beta)

The level meter — hidden behind a **long press of the dimmer button** — draws a
scrolling level envelope on the VFD. It needs a raw PCM copy of whatever is
playing, which this optional ALSA config provides.

**This is not installed automatically, and that is deliberate.** The file
redefines `pcm.volumioalsa`, the device everything plays through. Get it wrong
and the unit goes silent. Enabling it is a conscious step you can undo.

Without the tap the meter simply reports `NO AUDIO TAP` and does nothing else —
the plugin is unaffected either way.

## Enable

```bash
sudo cp /data/plugins/user_interface/retrotuner-ui/contrib/volumioalsa.postalsa.7.conf \
        /data/plugins/user_interface/retrotuner-ui/asound/
```

Then have Volumio rebuild `/etc/asound.conf` — either by saving any setting in
the Playback Options page, or from the Volumio node console:

```js
commandRouter.executeOnPlugin('audio_interface', 'alsa_controller', 'updateALSAConfigFile')
```

Start playback and confirm the fifo appears:

```bash
ls -l /tmp/retrotuner-audio.fifo
```

Then long-press dimmer.

## Disable

```bash
sudo rm /data/plugins/user_interface/retrotuner-ui/asound/volumioalsa.postalsa.7.conf
```

and rebuild the ALSA config the same way. **If audio has stopped and you cannot
reach the UI**, remove the file over SSH and reboot — the rebuild happens at
startup, so a reboot alone is enough to recover.

## How it works

`type multi` duplicates the stream: branch `a` is the normal output
(`postalsa`), branch `b` is a `volumiofifo` writing signed 16-bit little-endian
stereo to `/tmp/retrotuner-audio.fifo`. The `ttable` sends both channels to both
branches, so playback is bit-identical to before — the tap is a copy, not an
insert.

Tapping `volumioalsa` rather than MPD's own fifo is what makes Spotify work:
spop plays via librespot and never goes through MPD.

## Known limitations

- **Conflicts with `mpd_oled`.** Both redefine `pcm.volumioalsa`; only one can
  win. Don't run both.
- **The meter is not a spectrum analyser.** The display has 8 user-definable
  glyphs, so all 16 columns must share the same bar shapes. Columns are
  successive moments in time, not frequency bands. A per-band spectrum would
  need CAVA and a per-column glyph budget the hardware doesn't have.
- **An oscilloscope trace is impossible on this display**, for the same reason —
  it would need a different arbitrary pattern in every column.
- **The meter competes with button polling.** Both run in one Python process
  under the GIL, and the SPI button read already takes ~4.8ms of every 50ms.
  Expect the encoder to feel slightly less responsive while the meter is up.
