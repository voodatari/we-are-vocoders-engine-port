#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
synth.py — The five renderers.

They all start from the same list of frames produced by engine.py; the only
difference is how that state is turned into samples.

  exact       Faithful simulation of the Z80. Nearest-neighbour table reads
              (foldover included), 8-bit accumulator that wraps, clip to ±64,
              129 levels. This is what leaves the engine, before the PWM.
  beeper      From 'exact', reconstructs the real binary signal at 3.5 MHz
              (two pulses per sample) and filters it down to 44.1 kHz.
  antialias   Same rate and same clipping, but band-limited oscillators. Useful
              to separate how much of the harshness was foldover and how much
              was the missing bandwidth.
  remaster    Native re-synthesis at 44.1 kHz in floating point, no clipping.
              Recovers the harmonics the tables contain and the engine could
              never reproduce (the tables reach the 128th harmonic).
  production  The above plus continuous morphing between tables, stereo from
              the detuned pairs, a short reverb, a gentle lift and a limiter.

FOLDOVER IS PART OF THE INSTRUMENT
==================================
Some notes are written so high (3,094.7 Hz, 2.9 samples per cycle) that their
harmonics fold back and produce an inharmonic cluster: it sounds like a ring
modulator, and the author uses it as an effect four times in the piece.
Band-limiting those notes turns them into clean sines and destroys the effect.
That is why every "improved" mode keeps the exact foldover on voices whose
fundamental is above SR/8 (fewer than 8 samples per cycle): see
ALIAS_THRESHOLD_DIV.
"""

from __future__ import annotations

from math import gcd

import numpy as np
from scipy.signal import fftconvolve, lfilter, resample_poly

from engine import ATT, CLIP_HI, CLIP_LO, FRAMELEN, FRAMEPAD, Engine

SR_OUT = 44100
TABLE_LEN = 2048            # resolution of the re-synthesised tables
ALIAS_THRESHOLD_DIV = 8     # above SR/8 the original foldover is preserved

# Families whose table changes are really a continuous envelope or sweep: in
# 'production' they are interpolated across the whole frame.
SWEEP_FAMILIES = ('wavebass', 'wavelead', 'wavebp', 'wave5bp', 'wavereso',
                  'wavebassecho')

# Per-voice panning. Voices 2-3 carry the same phoneme detuned and voices 4-5
# the same sweep: opening them in opposite directions gives real stereo image.
PAN = {1: 0.0, 2: -0.5, 3: 0.5, 4: 0.35, 5: -0.35}


class Renderer:
    """Turns an Engine's frames into audio.

    `voices` isolates voices: {2, 3} leaves only the "vocoder" and {6} only the
    drums. Indices are 1..5 for the oscillators and 6 for the PCM channel.
    """

    def __init__(self, engine: Engine, voices: set[int] | None = None):
        self.E = engine
        self.voices = voices or {1, 2, 3, 4, 5, 6}
        self._bl_cache: dict[tuple[int, int], np.ndarray] = {}
        self._raw_cache: dict[int, np.ndarray] = {}
        # exact engine -> 44.1 kHz resampling ratio, as an irreducible fraction
        n, d = SR_OUT * engine.tps, int(engine.cpu)
        g = gcd(n, d)
        self.up, self.down = n // g, d // g

    # -------------------------------------------------------------- helpers
    def _raw(self, page: int) -> np.ndarray:
        """The 256-point table as-is, for nearest-neighbour reads."""
        if page not in self._raw_cache:
            self._raw_cache[page] = self.E.wave_data(page)
        return self._raw_cache[page]

    def _bandlimited(self, page: int, max_harmonic: int) -> np.ndarray:
        """Table re-synthesised to TABLE_LEN points with harmonics <= max_h.

        Done with an FFT: harmonics that would land above Nyquist are zeroed and
        the wave is rebuilt with zero padding, which is ideal interpolation. A
        fractional-phase read of the result produces no foldover.
        """
        max_harmonic = max(1, min(128, int(max_harmonic)))
        key = (page, max_harmonic)
        if key in self._bl_cache:
            return self._bl_cache[key]
        if page == self.E.zero_page or self.E.page_name.get(page) == 'zeros':
            table = np.zeros(TABLE_LEN)
        else:
            spec = np.fft.rfft(self._raw(page))
            spec[0] = 0                       # drop DC
            spec[max_harmonic + 1:] = 0
            padded = np.zeros(TABLE_LEN // 2 + 1, complex)
            padded[:len(spec)] = spec
            table = np.fft.irfft(padded, TABLE_LEN) * (TABLE_LEN / 256)
        self._bl_cache[key] = table
        return table

    @staticmethod
    def _osc(table: np.ndarray, phase: float, inc: float, n: int) -> np.ndarray:
        """Oscillator with fractional phase and linear interpolation."""
        pos = ((phase + inc * np.arange(n)) % 1.0) * TABLE_LEN
        i0 = pos.astype(np.int32)
        frac = pos - i0
        i1 = (i0 + 1) & (TABLE_LEN - 1)
        return table[i0] * (1.0 - frac) + table[i1] * frac

    def _drum_track(self) -> np.ndarray:
        """The whole PCM channel at the engine rate (one byte per sample).

        Only bytes 40..255 of each sample page are read: register C starts at
        FRAMEPAD and counts until it wraps.
        """
        out = np.zeros(len(self.E.frames) * FRAMELEN)
        if 6 not in self.voices:
            return out
        for i, f in enumerate(self.E.frames):
            base = f.drum << 8
            raw = bytes(self.E.mem[base + FRAMEPAD:base + 256])
            out[i * FRAMELEN:(i + 1) * FRAMELEN] = np.frombuffer(raw, dtype=np.int8)
        return out

    def _global_gain(self) -> np.ndarray:
        """Global volume (one of the six jump tables), sample by sample."""
        return np.repeat([ATT[f.vol] / 256 for f in self.E.frames], FRAMELEN)

    def _frame_bounds(self, sr: float) -> list[int]:
        """Sample index where each frame starts, at the requested rate.

        At 44.1 kHz a frame lasts 1,061.4 samples, which is not an integer. The
        remainder is carried with integer arithmetic so no drift accumulates.
        """
        if sr == SR_OUT:
            return [(i * FRAMELEN * self.up) // self.down
                    for i in range(len(self.E.frames) + 1)]
        return [i * FRAMELEN for i in range(len(self.E.frames) + 1)]

    # -------------------------------------------------- 1) exact simulation
    def exact(self) -> np.ndarray:
        """Engine output at its own rate, in integer levels (-64..+64)."""
        E = self.E
        n_frames = len(E.frames)
        out = np.empty(n_frames * FRAMELEN, dtype=np.int32)
        phase = [0] * 6
        steps = np.arange(1, FRAMELEN + 1)      # phase increments BEFORE the read
        drum = self._drum_track()

        for i, f in enumerate(E.frames):
            acc = np.zeros(FRAMELEN, dtype=np.int32)
            for v in range(1, 6):
                page, freq = f.waves[v - 1], f.freqs[v - 1]
                ph = (phase[v] + freq * steps) & 0xFFFF
                phase[v] = int(ph[-1])
                if page != E.zero_page and v in self.voices:
                    acc += self._raw(page)[ph >> 8].astype(np.int32)
            acc += drum[i * FRAMELEN:(i + 1) * FRAMELEN].astype(np.int32)
            # Register A is 8-bit and WRAPS; the jump table then clips to
            # [MIN_VARIANT, MAX_VARIANT-1].
            acc = ((acc + 128) % 256) - 128
            np.clip(acc, CLIP_LO, CLIP_HI, out=acc)
            out[i * FRAMELEN:(i + 1) * FRAMELEN] = (acc * ATT[f.vol]) >> 8
        return out

    # ------------------------------------------------- 2) beeper emulation
    def beeper(self, levels: np.ndarray | None = None) -> np.ndarray:
        """Rebuild the real binary signal and bring it down to 44.1 kHz.

        Each sample lasts TPS T-states and contains two pulses whose width grows
        with the level: the first inside a 212 T window and the second inside a
        178 T one. The signal is generated at 3.5 MHz, decimated with two moving
        averages (a second-order CIC, whose nulls land exactly on the multiples
        of 175 kHz) and resampled to 44.1 kHz.
        """
        if levels is None:
            levels = self.exact()
        P1MIN, P1TOT, P2MIN, P2TOT = 18, 212, 22, 178
        TPS = self.E.tps
        lvl = np.clip(levels.astype(np.int32) + 64, 0, 128)
        w1, w2 = P1MIN + lvl, P2MIN + lvl

        DEC, CHUNK = 20, 8192                   # 3.5 MHz -> 175 kHz
        parts, carry = [], np.zeros(0)
        for start in range(0, len(lvl), CHUNK):
            a, b = w1[start:start + CHUNK], w2[start:start + CHUNK]
            n = len(a)
            bits = np.zeros(n * TPS, dtype=np.float32)
            for i in range(n):
                o = i * TPS
                bits[o:o + a[i]] = 1.0
                bits[o + P1TOT:o + P1TOT + b[i]] = 1.0
            x = np.concatenate([carry, bits])
            k = (len(x) // DEC) * DEC
            carry = x[k:]
            parts.append(x[:k].reshape(-1, DEC).mean(axis=1))
        h = np.convolve(np.concatenate(parts), np.ones(2) / 2, mode='same')
        return resample_poly(h.astype(np.float64), 63, 250)      # 175k -> 44.1k

    # ---------------------------- 3/4/5) re-synthesis (antialias/remaster/prod)
    def resynth(self, mode: str) -> np.ndarray:
        """Re-synthesise with band-limited oscillators.

        mode='antialias' keeps the engine rate, the clipping and the 129 levels.
        mode='remaster'  generates straight to 44.1 kHz in floating point.
        mode='production' adds table morphing and returns stereo (2, N).
        """
        E = self.E
        sr = E.sample_rate if mode == 'antialias' else SR_OUT
        nyq = sr / 2
        alias_th = E.sample_rate / ALIAS_THRESHOLD_DIV
        bounds = self._frame_bounds(sr)
        total = bounds[-1]
        stereo = (mode == 'production')

        buf = np.zeros((2, total) if stereo else total)
        # Engine-rate bus for the voices that must keep their foldover.
        alias = np.zeros((2, len(E.frames) * FRAMELEN) if stereo
                         else len(E.frames) * FRAMELEN)

        phase = [0.0] * 6
        prev_page = [E.zero_page] * 6
        prev_gain = ATT[E.frames[0].vol] / 256
        steps = np.arange(1, FRAMELEN + 1)

        for i, f in enumerate(E.frames):
            a, b = bounds[i], bounds[i + 1]
            n = b - a
            gain = ATT[f.vol] / 256
            ramp = (np.linspace(prev_gain, gain, n)
                    if (stereo and gain != prev_gain) else gain)
            prev_gain = gain
            mono = None if stereo else np.zeros(n)

            for v in range(1, 6):
                page, freq = f.waves[v - 1], f.freqs[v - 1]
                hz = freq * E.sample_rate / 65536
                inc = hz / sr
                silent = (page == E.zero_page or hz <= 0
                          or E.page_name.get(page) == 'zeros' or v not in self.voices)
                if silent:
                    phase[v] = (phase[v] + inc * n) % 1.0
                    prev_page[v] = page
                    continue

                if hz > alias_th:
                    # The foldover IS the timbre: read the table like the Z80.
                    p16 = int(round(phase[v] * 65536)) & 0xFFFF
                    idx = ((p16 + freq * steps) & 0xFFFF) >> 8
                    s = self._raw(page)[idx]
                    g = ramp if np.isscalar(ramp) else float(np.mean(ramp))
                    ea, eb = i * FRAMELEN, (i + 1) * FRAMELEN
                    if stereo:
                        p = PAN[v]
                        alias[0, ea:eb] += s * g * np.sqrt((1 - p) / 2)
                        alias[1, ea:eb] += s * g * np.sqrt((1 + p) / 2)
                    else:
                        alias[ea:eb] += s * g
                    phase[v] = ((p16 + freq * FRAMELEN) & 0xFFFF) / 65536
                    prev_page[v] = page
                    continue

                max_h = max(1, int(nyq / hz))
                s = self._osc(self._bandlimited(page, max_h), phase[v], inc, n)

                if stereo and prev_page[v] != page and prev_page[v] != E.zero_page:
                    # Morph: the whole frame if it is an envelope or a sweep of
                    # the same family, 3 ms if it is a phoneme change.
                    fam_old = (E.page_name.get(prev_page[v], '') or '').rstrip('0123456789')
                    fam_new = (E.page_name.get(page, '') or '').rstrip('0123456789')
                    length = n if (fam_old == fam_new and fam_new in SWEEP_FAMILIES) \
                        else min(n, 140)
                    old = self._osc(self._bandlimited(prev_page[v], max_h),
                                    phase[v], inc, length)
                    r = np.linspace(0.0, 1.0, length)
                    s[:length] = old * (1 - r) + s[:length] * r

                phase[v] = (phase[v] + inc * n) % 1.0
                prev_page[v] = page
                if stereo:
                    p = PAN[v]
                    buf[0, a:b] += s * ramp * np.sqrt((1 - p) / 2)
                    buf[1, a:b] += s * ramp * np.sqrt((1 + p) / 2)
                else:
                    mono += s
            if not stereo:
                buf[a:b] += mono * ramp

        # the folded voices live at the engine rate: resampled once, at the end
        if mode == 'antialias':
            buf += alias
        else:
            r = (resample_poly(alias, self.up, self.down) if not stereo
                 else np.array([resample_poly(c, self.up, self.down) for c in alias]))
            m = min(buf.shape[-1], r.shape[-1])
            if stereo:
                buf[:, :m] += r[:, :m]
            else:
                buf[:m] += r[:m]

        drum = self._drum_track() * self._global_gain()
        if mode == 'antialias':
            y = buf + drum
            y = ((np.rint(y) + 128) % 256) - 128      # same 8-bit accumulator
            return np.clip(y, CLIP_LO, CLIP_HI)

        d = resample_poly(drum, self.up, self.down)
        m = min(buf.shape[-1], len(d))
        if stereo:
            buf = buf[:, :m]
            buf[0] += d[:m] * 0.707
            buf[1] += d[:m] * 0.707
            return self._produce(buf)
        buf = buf[:m] + d[:m]
        return buf

    # ----------------------------------------------------------- production
    @staticmethod
    def _produce(y: np.ndarray) -> np.ndarray:
        """Short reverb, high-shelf lift and a soft limiter."""
        rng = np.random.default_rng(7)
        n = y.shape[1]
        L = int(SR_OUT * 1.1)
        env = np.exp(-np.arange(L) / (SR_OUT * 0.20))
        ir = np.array([rng.normal(size=L) * env, rng.normal(size=L) * env])
        ir = np.array([lfilter([0.12], [1, -0.88], c) for c in ir])
        ir[:, :int(SR_OUT * 0.012)] = 0                      # pre-delay
        send = (y[0] + y[1]) * 0.5
        wet = np.array([fftconvolve(send, ir[0])[:n], fftconvolve(send, ir[1])[:n]])
        wet /= np.abs(wet).max() + 1e-9
        y = y / np.abs(y).max() + wet * 0.16
        y = np.array([lfilter([1.0, -0.72], [1, -0.80], c) for c in y])   # lift
        y = np.array([dc_block(c) for c in y])
        return np.tanh(y / np.abs(y).max() * 1.25) * 0.92

    # ---------------------------------------------------------------- output
    def to_44k(self, x: np.ndarray) -> np.ndarray:
        """Resample from the engine rate to 44.1 kHz."""
        return resample_poly(x.astype(np.float64), self.up, self.down)

    def render(self, version: str) -> np.ndarray:
        """Return the requested version at 44.1 kHz, ready to be normalised."""
        if version == 'exact':
            return self.to_44k(self.exact())
        if version == 'beeper':
            return self.beeper()
        if version == 'antialias':
            return self.to_44k(self.resynth('antialias'))
        if version in ('remaster', 'production'):
            return self.resynth(version)
        raise ValueError(f'unknown version: {version}')


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def dc_block(x: np.ndarray, fs: int = SR_OUT, fc: float = 15.0) -> np.ndarray:
    """First-order high-pass: removes DC without touching the bass."""
    a = np.exp(-2 * np.pi * fc / fs)
    return lfilter([1, -1], [1, -a], x)


TARGET_RMS_DB = -18.0       # common level for every version


def to_int16(x: np.ndarray, target_rms_db: float = TARGET_RMS_DB,
             ceiling: float = 0.98) -> np.ndarray:
    """Match levels by RMS and convert to 16-bit (mono or stereo).

    IMPORTANT: this does NOT peak-normalise. Two reasons:

      · The five versions have very different crest factors (8.2 dB for the
        beeper, 17.3 dB for the remaster). Peak-normalising leaves them 8 dB
        apart, and in an A/B test that is heard as "sounds better" rather than
        "sounds louder". Matching RMS compares what actually changed.
      · The peak is fragile: a single stray sample — say the 8-bit accumulator
        wrapping, 19 samples in the whole piece — is enough for the resampling
        filter to produce a peak that rescales the entire file by over 1 dB.

    -18 dBFS RMS keeps every version's peak below -0.7 dBFS, so nothing clips.
    """
    x = np.atleast_2d(np.asarray(x, dtype=np.float64))
    x = np.array([dc_block(c) for c in x])
    rms = np.sqrt((x ** 2).mean())
    gain = 10 ** (target_rms_db / 20) / max(rms, 1e-12)
    peak = np.abs(x).max() * gain
    if peak > ceiling:                      # safety net, should never trigger
        gain *= ceiling / peak
    x = (x * gain)
    x = x.T if x.shape[0] > 1 else x[0]
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)
