#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score.py — Turns the song into a tracker "module" and prints it as text.

The engine maps onto a tracker format naturally:

    1 frame  = 1 tick   (216 samples, ~26 ms)
    5 ticks  = 1 row    (~130 ms, a sixteenth note at 114.9 BPM)

which is exactly what "speed 5" means in a module. Each voice becomes a
channel; the active wave page acts as the instrument number. On the vocal
voices the instrument IS the phoneme, so the lyrics are written into the score
itself.
"""

from __future__ import annotations

import numpy as np

from engine import Engine

CHANNEL_NAMES = ['BASS', 'VOX L', 'VOX R', 'SWEEP A', 'SWEEP B', 'DRUMS']
NOTE_NAMES = ['C-', 'C#', 'D-', 'D#', 'E-', 'F-', 'F#', 'G-', 'G#', 'A-', 'A#', 'B-']


def tuning_offset(engine: Engine) -> float:
    """Offset in semitones between the song's own grid and A440.

    The piece is in tune with itself but not with 440 Hz. This looks for the
    offset that minimises the distance from every frequency used to the
    equal-tempered grid. Careful: the function has a minimum every semitone, so
    the search is limited to ±0.5 to land on the representative nearest A440.
    """
    freqs = np.array(sorted({f.freqs[v] for f in engine.frames
                             for v in range(5) if f.freqs[v] > 0}))
    hz = freqs * engine.sample_rate / 65536
    hz = hz[hz > 20]
    semis = 12 * np.log2(hz / 440) + 69
    return float(min(np.arange(-0.5, 0.5, 0.001),
                     key=lambda o: np.abs((semis - o) - np.round(semis - o)).mean()))


def note_of(engine: Engine, freq16: int, offset: float) -> int:
    """MIDI-style note number for a 16-bit phase increment (-1 = silent)."""
    if freq16 <= 0:
        return -1
    hz = freq16 * engine.sample_rate / 65536
    if hz < 15:
        return -1
    return int(round(12 * np.log2(hz / 440) + 69 - offset))


def note_name(n: int) -> str:
    return '...' if n < 0 else f'{NOTE_NAMES[n % 12]}{max(0, n // 12 - 1)}'


def as_text(engine: Engine, first: int = 0, count: int = 64) -> str:
    """Dump the score as text, tracker style.

    Handy for looking at the music without opening the web app:
        python3 vocoders.py score --text 832 32
    """
    if not engine.frames:
        engine.run()
    off = tuning_offset(engine)
    header = 'ROW | ' + ' | '.join(f'{c:<12}' for c in CHANNEL_NAMES)
    lines = [header, '-' * len(header)]

    prev_note, prev_on = [-1] * 5, [False] * 5
    for i, row in enumerate(engine.rows):
        a, b = row.tick, row.tick + row.n
        f = engine.frames[a]
        cells = []
        for v in range(5):
            on = any(engine.frames[t].waves[v] != engine.zero_page for t in range(a, b))
            n = note_of(engine, f.freqs[v], off)
            trig = on and (n != prev_note[v] or not prev_on[v])
            page = next((engine.frames[t].waves[v] for t in range(a, b)
                         if engine.frames[t].waves[v] != engine.zero_page), None)
            name = engine.page_name.get(page, '').replace('wave', '').upper()[:8] if page else '....'
            cells.append(f'{note_name(n) if trig else "...":<3} {name:<8}'[:12])
            if on:
                prev_note[v], prev_on[v] = n, True
            else:
                prev_on[v] = False
        drum = engine.page_name.get(f.drum, 'zeros')
        cells.append(f'{"C-4" if drum != "zeros" else "...":<3} '
                     f'{drum.replace("sample_", "").upper()[:8] if drum != "zeros" else "....":<8}'[:12])
        if first <= i < first + count:
            lines.append(f'{i:03X} | ' + ' | '.join(f'{c:<12}' for c in cells))
        if i >= first + count:
            break
    return '\n'.join(lines)
