# Audio tap for the level meter (beta)

The level meter — hidden behind a **long press of the dimmer button** — draws a
scrolling level envelope on the VFD. It needs a raw PCM copy of whatever is
playing, which this optional ALSA config provides.

**This is not installed automatically, and that is deliberate.** The file
inserts itself into the ALSA output chain that everything plays through. Get it
wrong and the unit goes silent. Enabling it is a conscious step you can undo.

Without the tap the meter simply reports `NO AUDIO TAP` and does nothing else —
the plugin is unaffected either way.

## Enable

```bash
sudo cp /data/plugins/user_interface/retrotuner-ui/contrib/rt_in.rt_out.8.conf \
        /data/plugins/user_interface/retrotuner-ui/asound/
```

Then restart the plugin (Plugins page, or `sudo systemctl restart volumio`).

On start the plugin notices the file and does the two things Volumio will not do
for you: creates the fifo with `mkfifo`, then calls `updateALSAConfigFile` to
fold the config into `/etc/asound.conf`. Neither happens unless the file is
there.

Confirm both:

```bash
ls -l /tmp/retrotuner-audio.fifo      # should be a fifo (prefix "p")
grep -A6 rt_fifo /etc/asound.conf     # should show the tap
```

Then long-press dimmer.

## Disable

```bash
sudo rm /data/plugins/user_interface/retrotuner-ui/asound/rt_in.rt_out.8.conf
```

then **reboot**. Removing the file is not enough on its own: the plugin only
asks for an ALSA rebuild when the file is present, so `/etc/asound.conf` keeps
the tap until something regenerates it, which happens at startup.

This is also the recovery path if audio has stopped and you cannot reach the
UI — delete the file over SSH and reboot.

## How it works

`type multi` duplicates the stream: branch `b` is the normal output (`rt_out`),
branch `a` is a `volumiofifo` writing to `/tmp/retrotuner-audio.fifo`. The
`ttable` sends both channels to both branches, so playback is bit-identical to
before — the tap is a copy, not an insert.

The fifo branch is wrapped in a `type plug` forcing 44100Hz S16_LE stereo. The
meter decodes raw signed 16-bit samples, so without that it would read noise
whenever the hardware ran at 24-bit or 96kHz.

Modelled on `stylish_player`'s `sp_in.sp_out.7.conf`, which does the same job
from the same kind of plugin.

Tapping the output chain rather than MPD's own fifo is what makes Spotify work:
spop plays via librespot and never goes through MPD.

## Known limitations

- **May conflict with other visualiser plugins.** peppymeter, stylish_player,
  mpd_oled and fusion all insert into the same ALSA chain. Running several is
  untested.
- **The meter is not a spectrum analyser.** The display has 8 user-definable
  glyphs, so all 16 columns must share the same bar shapes. Columns are
  successive moments in time, not frequency bands. A per-band spectrum would
  need CAVA and a per-column glyph budget the hardware doesn't have.
- **An oscilloscope trace is impossible on this display**, for the same reason —
  it would need a different arbitrary pattern in every column.
- **The meter competes with button polling.** Both run in one Python process
  under the GIL, and the SPI button read already takes ~4.8ms of every 50ms.
  Expect the encoder to feel slightly less responsive while the meter is up.
