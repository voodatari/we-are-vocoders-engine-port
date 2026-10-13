# We Are Vocoders — beeper engine reconstruction

Tools that **run the sound engine** of the ZX Spectrum intro *We Are Vocoders*
(Dmitry Milk, DiHalt 2026) without a Spectrum and without an emulator, plus a
web app that runs the same engine live in the browser.

The original repository contains no music file. The song lives spread across
several `.asm` sources, so this project assembles them, interprets the note
stream and synthesises the audio sample by sample — five wavetable oscillators
and one PCM channel summed into an 8-bit accumulator at 8,274.23 Hz, exactly as
the Z80 does it.

Out of that come five renders of the music, a tracker view of the score, and a
browser app where you can mute channels and switch synthesis mode while it plays.

```
tools/    Python: assembler, note-stream interpreter, five renderers, calibration
web/      Static site: the engine in an AudioWorklet, tracker view, live controls
docs/     What the engine does and how it was worked out
```

---

## Quick start

Python 3.10 or newer.

```bash
git clone <this repository> we-are-vocoders-engine
cd we-are-vocoders-engine
pip install -r requirements.txt
./scripts/fetch-original.sh            # clones the author's repo next door

cd tools
python3 vocoders.py info                    # engine and song summary
python3 vocoders.py render --out ../out     # the five WAV files (~40 s)
python3 vocoders.py score --text 832 24     # the score as text
```

The commands expect the original repository as a sibling directory. Anywhere
else, pass `--repo /wherever/it/is`.

`ffmpeg` is only needed to calibrate against a recording that is not a WAV.

### The web app

`web/` is a static site with no build step and no dependencies. The song data it
needs is committed as `web/data/song.json` (239 KB), so it works out of the box:

```bash
cd web
python3 -m http.server 8080        # then open http://localhost:8080
```

Opening `index.html` as a local file will not work: the data and the worklet are
loaded with `fetch()` and need an HTTP origin. To deploy, drag `web/` onto
https://app.netlify.com/drop, or `netlify deploy --prod --dir web`.

To regenerate the data after changing the engine:

```bash
cd tools && python3 vocoders.py webdata --out ../web/data/song.json
```

There is a second way to get the data, kept because it is interesting: setting
`USE_LOCAL_DATA = false` at the top of `web/app.js` makes `builder.js` fetch the
original `.asm` and `.dat` sources from the author's GitHub and assemble them in
the visitor's browser — a full port of the Python assembler and interpreter,
verified byte-identical. It costs about 3.5 seconds on a first visit, then caches
in local storage. The bundled file is the default because it is faster and does
not depend on the original repository staying where it is.

---

## The five versions

| Version | What it is |
|---|---|
| `exact` | Faithful simulation of the Z80: nearest-neighbour table reads with their foldover, 8-bit accumulator that wraps, clip to ±64, 129 levels. This is the signal that leaves the engine, before the PWM. |
| `beeper` | Rebuilds the real 1-bit signal at 3.5 MHz (two pulses per sample) and filters it down to 44.1 kHz. Sounds like a Spectrum, hiss included. |
| `antialias` | Same rate and same clipping, but band-limited oscillators. Separates how much of the harshness was foldover from how much was missing bandwidth. |
| `remaster` | Native re-synthesis at 44.1 kHz in floating point, no clipping. Recovers harmonics that **are in the tables** and the engine could not reproduce: energy between 10 and 20 kHz goes from −83 dB to −38 dB. |
| `production` | The above plus continuous table morphing, stereo, short reverb and a gentle lift. |

All five are level-matched to **−18 dBFS RMS**, not peak-normalised. Peak
normalisation left them 8.1 dB apart (the beeper has a crest factor of 8 dB, the
remaster 17), and in an A/B test that is heard as quality rather than as level.

### Foldover is part of the instrument

Some notes are written so high — 3,094.7 Hz, **2.9 samples per cycle** — that
their harmonics fold back and produce an inharmonic cluster that sounds like a
ring modulator. The author uses it as an effect four times in the piece
(1:43.8 · 1:52.1 · 2:38.1 · 2:46.4).

Band-limiting those notes turns them into clean sines and **destroys the
effect**. So every "improved" mode keeps the exact foldover on voices whose
fundamental is above SR/8 — fewer than 8 samples per cycle. That is 0.37% of all
voice-frames, so nothing else is affected. The threshold lives in
`ALIAS_THRESHOLD_DIV` in `tools/synth.py`; the web app has a RING-MOD switch
that turns it off so you can hear the effect disappear.

### Hearing the voices on their own

Indices are 1–5 for the oscillators and 6 for the drums:

```bash
python3 vocoders.py render --voices 2,3 --versions remaster --out ../out
python3 vocoders.py render --without 6  --versions exact    --out ../out
```

Voices 2 and 3 are the "vocoder": the same phoneme wavetable played detuned.

---

## The sample rate is measured, not deduced

The engine emits one sample every `TPS` T-states, so the rate is `CPU / TPS`.
That single number sets both pitch and tempo, and getting it out of the source
is more treacherous than it looks:

| | T-states | Result |
|---|---|---|
| The generator's constants (212 + 178) | 390 | 13.3% too fast |
| \+ the 52 T of `iloop`, which runs on every sample and is not in the template | 442 | 4.3% too slow |
| − the 10 T of the `ret` that is counted but never executes | 432 | still wrong |
| **Measured against real hardware** | **423** | correct |

About 9 T remain that static analysis does not explain; most likely another
instruction is accounted for that the real path skips. The generator's
parameters were ruled out by running the author's own `codegen.py`: it
reproduces `VARIANTS_SIZE`, `TEMPLATE_SIZE` and the rest identically.

`vocoders.py calibrate --ref recording.mp3` measures it two ways, each blind to
the other — aligning attack envelopes for tempo, correlating log-frequency
spectra for pitch. On the reference used here they gave **423.10** and
**423.15** T.

---

## Verification

- **Browser against Python.** The same 8 seconds rendered in an
  `OfflineAudioContext` and compared with the Python renderer: in `exact` mode
  the correlation is **1.0000 with zero lag** — the same signal, sample for
  sample. Band-limited modes correlate 0.999 (antialias) and 0.94 (remaster);
  the difference is that the browser uses mipmapped tables instead of
  recomputing the exact band limit per note.
- **Browser builder against Python.** `web/builder.js` is a port of the
  assembler and the note-stream interpreter. Building the song in the browser
  and comparing every field with the Python exporter gives a **byte-identical**
  result: wavetables, samples, all 8,880 ticks of state, row starts, sections
  and metadata.
- **Note grid.** 99% of the 74 distinct frequencies in the song fall within
  0.12 semitones of one consistent equal-tempered grid (mean residual 0.046
  semitones). A one-byte misread in the note stream would have scattered them.
- **Levels.** Spread across the five renders: 0.00 dB, no clipping.

---

## Licensing

Two copyrights, both MIT:

| | |
|---|---|
| The code in `tools/` and `web/` | © 2026 VooD — `LICENSE` |
| The intro, its engine, wavetables, drum samples and note data | © 2026 Dmitry_Milk — `LICENSE-we_are_vocoders.txt` |

The author licensed his repository under MIT and gave his blessing for this
reconstruction to be published, which is why `web/data/song.json` — a derivative
of his wavetables, samples and note data — can be committed here at all. His
copyright notice and permission notice travel with it, as MIT requires.

If you fork this, ship both licence files and keep `NOTICE.md`, which says what
came from where.

---

## Credits

Music, wavetables and the original beeper engine: **Dmitry Milk** —
[github.com/DmitryMilk/we_are_vocoders](https://github.com/DmitryMilk/we_are_vocoders).
Released at DiHalt 2026 Summer, where it placed second.

This project only reads that repository and reproduces what the Z80 would do.
Thanks to him for publishing the sources, for licensing them under MIT, and for
being happy to see this exist — none of it would have been possible otherwise.
