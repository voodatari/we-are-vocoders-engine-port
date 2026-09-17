# How the engine works, and how it was worked out

Notes taken while reconstructing the sound engine of *We Are Vocoders*. Everything
here was verified against the original sources or measured; where something is
still unexplained, it says so.

---

## 1. The synthesis

Six sources are summed sample by sample:

- **five wavetable oscillators.** Each has a 16-bit phase accumulator whose high
  byte indexes a 256-byte table of signed values. No interpolation: the table is
  read nearest-neighbour, which is where the foldover comes from.
- **one PCM channel** for the drums, one byte per sample.

The sum lands in the Z80's `A` register. `A` is **8 bits and wraps**, and is then
used as the index into a jump table that selects one of 129 code fragments
generated at startup, each emitting a different pulse width. That is the origin
of the ±64 clip: levels outside `[MIN_VARIANT, MAX_VARIANT]` are pinned.

The order matters and is easy to get wrong: **wrap first, clip second**. A sum of
+150 wraps to −106 and then clips to −64, not to +64. It only affects 19 samples
in the whole piece, but they are full-scale polarity flips.

### Envelopes are wavetable changes

There is no multiply. `bass2 → bass1 → bass0` is the bass decaying. Measuring the
correlation between those three tables gives **0.91–0.96**, not ~0.999, so they
are not the same wave scaled: they are also **filtered** differently. The bass
gets darker as it decays. Anything that "optimises" them into one table with a
gain flattens the instrument.

The same trick drives the filter sweeps: `bp0…bp8`, `5bp0…5bp8` and `reso0…reso7`
are sequences of increasing brightness, stepped one table per tick.

### The vocoder

There is no vocoder. There are 23 phoneme wavetables whose single cycle already
carries the formants:

| Table | Where the energy sits |
|---|---|
| `ph_a` | harmonics 7–14 (F1 ≈ 700 Hz) |
| `ph_i` | harmonics 4–6 and 20–30 (low F1, high F2) |
| `ph_s` | harmonics 50–58 (pure hiss) |

Voices 2 and 3 play the same phoneme a fraction apart, which gives the chorus.
The phoneme changes every tick (~26 ms) while the oscillator follows the melody:
carrier from the note, spectrum from the voice. Perceptually a vocoder;
mechanically a wavetable lookup.

---

## 2. The time base

```
1 frame (tick) = 216 samples  ~26 ms
5 ticks        = 1 row        ~130 ms   -> 114.9 BPM
```

which is exactly "speed 5" in tracker terms, and why the score maps onto a
tracker grid so cleanly.

The note data is a **byte stream**:

- a byte `< 0x80` is a qframe index — 5 rows of 3 bytes holding the wave pages of
  voices 1–3;
- a byte `>= 0x80` indexes a command routine that reads its own parameters from
  the stream, including call and return so sections can repeat.

Command names describe their parameters in order, which makes the format
self-documenting: `nts_seq_f3_f2_f1_wp45_end` loads the drum sequence, freq3,
freq2, freq1 and a packed word with the wave pages of voices 4 and 5. The
routines fall through into one another, so the interpreter here is a flat list of
micro-operations plus a label index — which is what the Z80 does.

A detail that causes real divergence if missed: `frmdst1/2/3` are self-modified
pointers deciding whether the frame data still drives voices 1–3. When one points
at `fakevar`, that voice ignores the qframe and keeps whatever a note command
gave it.

---

## 3. The sample rate

The loop takes `TPS` T-states per sample, so the rate is `CPU / TPS`. This one
number sets pitch and tempo together.

| | T-states | Result |
|---|---|---|
| The generator's constants, `PULSE1TOTAL + PULSE2TOTAL` (212 + 178) | 390 | 13.3% too fast |
| \+ `iloop`, 52 T, which runs every sample and is not in the variant template | 442 | 4.3% too slow |
| − 10 T for the `ret` counted in `part2` that never executes when the loop continues | 432 | still wrong |
| **Measured against a hardware recording** | **423** | correct |

About 9 T are still unexplained by reading the source. The generator's parameters
were ruled out by running the author's own `codegen.py`, which reproduces
`VARIANTS_SIZE`, `TEMPLATE_SIZE`, `VARIANTS_COUNT` and the rest identically.

### How it was measured

Two independent methods, each blind to the other:

- **tempo** — align the attack envelopes of both signals (100 points per second)
  and find the time scale factor maximising the cross-correlation → **423.10 T**;
- **pitch** — correlate the average spectra on a log-frequency grid, which does
  not depend on tempo at all → **423.15 T**, correlation 0.97.

They agree to 0.01%. The resulting rate is **8,274.23 Hz** and the piece lasts
**3:51.8**.

### A red herring worth recording

The song's 74 distinct frequencies fit an equal-tempered grid anchored *exactly*
at A440 if you assume 3,546,900 / 390 = 9,094.6 Hz — the 128K clock with the
`iloop` overhead ignored. That is almost certainly the constant the composing
tool used, and it is a very convincing wrong answer. On real hardware the piece
plays about 2.4 semitones below concert pitch, which for a demo tune nobody
minds.

---

## 4. What the data is worth

| | |
|---|---|
| Note data | 4 KB |
| Frames, sequences, address table | ~2 KB |
| 57 wavetables | 14.6 KB |
| 2 drum samples | 2.5 KB |
| **Total** | **~23 KB for 3:51 of music** |

The 8 KB of PWM variant code is not stored at all: it is generated at startup.

And the tables hold more bandwidth than the engine can play. `ph_s` has its
energy at harmonics 50–58: on a 200 Hz note that is 10–11.6 kHz, which at
8.27 kHz sampling folds down into a buzz instead of a hiss. Re-synthesising at
44.1 kHz recovers it — that is real information in the `.dat` files, not invented
detail. Energy between 10 and 20 kHz goes from −83 dB to −38 dB.

---

## 5. Things that were got wrong along the way

Kept because they are the useful part of the record.

- **390 T** for the sample period (13% fast), then **442 T** (4.3% slow), before
  measuring 423.
- **Band-limiting everything**, which silently deleted the ring-modulator effect
  the author had composed into four passages. The fix is the `SR/8` exception.
- **Peak-normalising** the five renders, which left them 8.1 dB apart and made
  the A/B comparison meaningless. RMS matching fixed it.
- **Clipping before wrapping** the 8-bit accumulator. 19 samples, but one of them
  set the peak used for normalisation and shifted a whole file by 1.2 dB.
