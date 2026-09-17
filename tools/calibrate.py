#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate.py — Measures the loop's real period by comparing with a recording.

WHY THIS IS NEEDED
==================
The engine's sample rate is CPU / TPS, where TPS is how many T-states one turn
of the loop takes. Counting them by hand is treacherous: the generator's
constants (212 + 178) only cover the variant template, you must add the 52 T of
`iloop` — which runs on every sample and is not in the template — and subtract
the 10 T of the `ret` that is accounted for but never actually executes. Even
then the result (432) does not match reality (423).

So it is measured instead. Two independent methods, each blind to the other:

  TEMPO  The attack envelopes of both signals are aligned, looking for the time
         scale factor that maximises the cross-correlation.
  PITCH  The average spectra are correlated on a logarithmic frequency grid,
         which does not depend on tempo at all.

If the two agree, the number can be trusted. On the reference recording used
here they gave 423.10 and 423.15 T: they agree to within 0.01%.

USAGE
    python3 vocoders.py calibrate --repo ../we_are_vocoders --ref recording.mp3

The reference can be any format ffmpeg reads; it is converted to mono 44.1 kHz
WAV automatically. Leading silence is detected and discounted.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy.io import wavfile

HOP = 0.01          # envelope resolution: 100 points per second
PPO = 200           # points per octave in the pitch analysis


# --------------------------------------------------------------------------- #
def load_audio(path: str | Path) -> tuple[int, np.ndarray]:
    """Load audio as mono float. Falls back to ffmpeg if it is not a plain WAV."""
    path = Path(path)
    try:
        sr, x = wavfile.read(path)
    except Exception:
        if not shutil.which('ffmpeg'):
            raise SystemExit('ffmpeg is required to read %s' % path.name)
        tmp = Path(tempfile.mkdtemp()) / 'ref.wav'
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(path),
                        '-ac', '1', '-ar', '44100', str(tmp)], check=True)
        sr, x = wavfile.read(tmp)
    x = x.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(1)
    if np.issubdtype(np.dtype(x.dtype), np.integer):
        x /= 32768
    return sr, x


def trim_silence(x: np.ndarray, sr: int, floor_db: float = -45) -> tuple[float, float]:
    """Return (start, end) in seconds of the part that actually has sound."""
    h = int(sr * 0.02)
    e = np.sqrt((x[:len(x) // h * h].reshape(-1, h) ** 2).mean(1))
    idx = np.where(e > e.max() * 10 ** (floor_db / 20))[0]
    return idx[0] * 0.02, (idx[-1] + 1) * 0.02


def onset_envelope(x: np.ndarray, sr: int) -> np.ndarray:
    """Positive energy flux: marks the attacks, which is what gets aligned."""
    h = int(sr * HOP)
    n = len(x) // h
    e = np.sqrt((x[:n * h].reshape(-1, h) ** 2).mean(1))
    e = np.log1p(e / (e.max() + 1e-12) * 100)
    d = np.diff(e, prepend=e[0])
    d[d < 0] = 0
    return d - d.mean()


def tempo_ratio(ref: np.ndarray, mine: np.ndarray) -> tuple[float, float]:
    """Factor my envelope must be scaled by to line up with the reference.

    Returns (ratio, correlation). A ratio > 1 means my render is shorter than
    the reference, i.e. it plays too fast.
    """
    def score(r: float, lag_range: int = 400) -> float:
        t = np.arange(len(mine)) * r
        m = np.interp(np.arange(int(t[-1]) + 1), t, mine)
        n = min(len(m), len(ref))
        a, b = m[:n], ref[:n]
        fa, fb = np.fft.rfft(a, 2 * n), np.fft.rfft(b, 2 * n)
        cc = np.fft.irfft(fa * np.conj(fb), 2 * n)
        cc = np.concatenate([cc[-lag_range:], cc[:lag_range]])
        return float(cc.max() / (np.sqrt((a ** 2).sum() * (b ** 2).sum()) + 1e-12))

    best = max(((score(r), r) for r in np.arange(0.80, 1.25, 0.002)))
    best = max([best] + [(score(r), r) for r in
                         np.arange(best[1] - 0.004, best[1] + 0.004, 0.00005)])
    return best[1], best[0]


def pitch_ratio(ref_path, mine_path, ref_t0: float, span=(20, 140)) -> tuple[float, float]:
    """Reference/mine pitch ratio, independent of tempo.

    Compares the average spectra interpolated onto a logarithmic grid: a shift
    of k steps is a factor of 2**(k/PPO).
    """
    grid = 50.0 * 2 ** (np.arange(int(np.log2(5000 / 50) * PPO)) / PPO)

    def logspec(path, t0, t1, nfft=8192):
        sr, x = load_audio(path)
        x = x[int(t0 * sr):int(t1 * sr)]
        w = np.hanning(nfft)
        S = np.zeros(nfft // 2 + 1)
        for i in range(0, len(x) - nfft, nfft // 2):
            S += np.abs(np.fft.rfft(x[i:i + nfft] * w)) ** 2
        f = np.fft.rfftfreq(nfft, 1 / sr)
        v = np.interp(grid, f[1:], np.log1p(S[1:] / S[1:].max() * 1e4))
        return v - v.mean()

    a = logspec(ref_path, ref_t0 + span[0], ref_t0 + span[1])
    b = logspec(mine_path, span[0], span[1])
    cc = np.correlate(a, b, 'full') / np.sqrt((a ** 2).sum() * (b ** 2).sum())
    i = int(np.argmax(cc))
    k = i - (len(b) - 1)
    y0, y1, y2 = cc[i - 1], cc[i], cc[i + 1]          # sub-step refinement
    d = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2)
    return float(2 ** ((k + d) / PPO)), float(cc.max())


# --------------------------------------------------------------------------- #
def calibrate(reference: str | Path, mine: str | Path, tps: int, cpu: int) -> dict:
    """Compare my render with the recording and derive the real period.

    `mine` must be the 'exact' WAV rendered with `tps`.
    """
    sr_r, xr = load_audio(reference)
    t0, t1 = trim_silence(xr, sr_r)
    sr_m, xm = load_audio(mine)

    env_r = onset_envelope(xr[int(t0 * sr_r):], sr_r)
    env_m = onset_envelope(xm, sr_m)
    r_tempo, corr_t = tempo_ratio(env_r, env_m)

    r_pitch, corr_p = pitch_ratio(reference, mine, t0)

    p_tempo, p_pitch = tps * r_tempo, tps / r_pitch
    return {
        'ref_start': t0, 'ref_end': t1, 'ref_dur': t1 - t0,
        'ratio_tempo': r_tempo, 'corr_tempo': corr_t,
        'ratio_pitch': r_pitch, 'corr_pitch': corr_p,
        'tps_tempo': p_tempo, 'tps_pitch': p_pitch,
        'tps': round((p_tempo + p_pitch) / 2),
        'sr': cpu / round((p_tempo + p_pitch) / 2),
    }


def report(res: dict, tps: int, cpu: int) -> str:
    return '\n'.join([
        f'reference      {res["ref_dur"]:.2f} s of music '
        f'(from {res["ref_start"]:.2f} to {res["ref_end"]:.2f})',
        f'tempo ratio    {res["ratio_tempo"]:.5f}  (correlation {res["corr_tempo"]:.3f})'
        f'  ->  {res["tps_tempo"]:.2f} T',
        f'pitch ratio    {res["ratio_pitch"]:.5f}  (correlation {res["corr_pitch"]:.3f})'
        f'  ->  {res["tps_pitch"]:.2f} T',
        '',
        f'current period {tps} T  ({cpu/tps:.2f} Hz)',
        f'measured       {res["tps"]} T  ({res["sr"]:.2f} Hz)',
        '',
        ('The two agree, so the number can be trusted.'
         if abs(res['tps_tempo'] - res['tps_pitch']) < 1 else
         'WARNING: tempo and pitch disagree. The reference may have been edited, '
         'or the interpreter may be playing the wrong number of repeats.'),
    ])
