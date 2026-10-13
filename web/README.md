# web/ — the engine in the browser

A static site with no build step. There are **no audio files**: an AudioWorklet
synthesises the music from the same data the Python renderer uses, which is what
makes muting channels, switching synthesis mode and playing with the effects work
while the music plays.

About 60 KB of code plus 239 KB of song data.

## Where the music comes from

`data/song.json` holds everything the engine needs: 57 wavetables, two drum
samples, and the state of all six voices on each of the 8,880 ticks. It is
committed, so a deployment works as-is. To rebuild it after changing the engine:

```bash
cd ../tools && python3 vocoders.py webdata --out ../web/data/song.json
```

There is a second path, kept because it is a nice piece of work in its own
right. Set `USE_LOCAL_DATA = false` at the top of `app.js` and `builder.js`
fetches the original `.asm` and `.dat` sources from `raw.githubusercontent.com`,
assembles them into a 64 KB image, interprets the note stream and collects the
same 8,880 ticks — a port of `tools/engine.py`, verified **byte-identical**. It
takes about 3.5 seconds on a first visit and then caches in local storage, and
it means the site holds none of the author's data itself. The bundled file is the
default because it is faster and does not depend on the original repository
staying where it is. See `../NOTICE.md`.

Either way it must be served over HTTP — opening `index.html` as a local file
will not work, because the data and the worklet are loaded with `fetch()`:

```bash
python3 -m http.server 8080      # http://localhost:8080
```

To deploy, drag this folder onto https://app.netlify.com/drop, or run
`netlify deploy --prod --dir .`. `netlify.toml` already sets the cache headers.

Requires a browser with AudioWorklet support: Chrome/Edge 66+, Firefox 76+,
Safari 14.1+.

## Controls

| | |
|---|---|
| **Synthesis mode** | `1`–`5` or click. Exact, 1-bit beeper, anti-alias, remaster, production |
| **Channels** | click to mute, shift-click to solo. Also `6`–`9` |
| **Sample clock** | moves the engine clock from 330 to 520 T per sample: pitch and tempo change together, as on real hardware |
| **8-BIT CLIP** | turns off the accumulator's clipping |
| **RING-MOD** | turns off the exception that preserves foldover on high notes — you can hear the ring-modulator effect vanish at 1:43.8 · 1:52.1 · 2:38.1 · 2:46.4 |
| **Production FX** | reverb, stereo width, drive and tone, in production mode only |

Space plays and pauses; clicking the grid jumps to that row.

## Files

| | |
|---|---|
| `index.html`, `style.css`, `app.js` | interface, tracker view and graphics |
| `data/song.json` | the song data: wavetables, samples and 8,880 ticks of state |
| `builder.js` | alternative: fetches the original sources and builds that data in the browser |
| `engine-worklet.js` | the engine: oscillators, 8-bit accumulator, PWM and resampling |

## How the levels are handled

All five modes are matched to **−18 dBFS RMS**, not peak-normalised. Peak
normalisation left them 8 dB apart, and in an A/B test that is heard as quality
rather than as level. The per-mode constants live in `MODE_GAIN` in
`engine-worklet.js`.

## Verification

The browser engine was checked against the Python renderer by rendering the same
8 seconds in an `OfflineAudioContext`: in *exact* mode the correlation is
**1.0000 with zero lag** — the same signal, sample for sample. The band-limited
modes correlate 0.999 (anti-alias) and 0.94 (remaster); the difference is that
the browser uses mipmapped tables instead of recomputing the exact band limit for
every note.
