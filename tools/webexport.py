#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webexport.py — Exports everything the browser needs to SYNTHESISE the song:
not just the score, but the wavetables, the drum samples and the engine's full
state on every tick.

With this the web app plays no audio files: it runs the same engine inside an
AudioWorklet, which is what makes muting channels and switching synthesis mode
work while the music plays.

Format (JSON with binary arrays in base64, to keep it small):

    meta      rate, tempo, tick and row counts, tuning…
    inst      [{n: name, f: family, pk: peak}] — instrument 0..N-1
    waves     base64, N x 256 int8: the wavetables, one per instrument
    drums     base64, 2 x 1280 int8: kick and snare
    wpage     base64, 5 x T int8:   instrument of each voice on each tick (-1 mute)
    freq      base64, 5 x T uint16: phase increment of each voice
    dpage     base64, T int8:       drum instrument (-1 if silent)
    doff      base64, T uint8:      which page (0..4) of the sample is playing
    vol       base64, T uint8:      global volume index
    rowStart  base64, R uint16:     tick each row starts on
    sections  [[row, name]] only where it changes

The browser rebuilds the score's notes from `freq`, so they are not sent.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np

from engine import Engine
from score import CHANNEL_NAMES, tuning_offset


def _b64(arr: np.ndarray) -> str:
    return base64.b64encode(arr.tobytes()).decode()


def export_web(engine: Engine, out_path: str | Path) -> dict:
    E = engine
    if not E.frames:
        E.run()
    zero = E.zero_page
    n_ticks, n_rows = len(E.frames), len(E.rows)

    # ------------------------------------------------------------ instruments
    # Only BASE pages (a sample's continuation pages are resolved with the
    # `doff` offset, so the worklet reads the sample as one block).
    pages = sorted({w for f in E.frames for w in f.waves})
    inst, iid, tables = [], {}, []
    for p in pages:
        name = E.page_name.get(p)
        if name is None or name == 'zeros':
            continue
        short = name.replace('wave', '')
        iid[p] = len(inst)
        inst.append({'n': short.upper()[:8],
                     'f': short.rstrip('0123456789') or short,
                     'pk': int(np.abs(E.wave_data(p)).max())})
        tables.append(np.frombuffer(bytes(E.mem[p << 8:(p << 8) + 256]), dtype=np.int8))

    # drums: two 1280-byte samples (5 pages each)
    drum_base = {}
    drums = []
    for label in ('sample_kick', 'sample_snare'):
        base = E.sym[label] >> 8
        for k in range(5):
            drum_base[base + k] = (len(drums), k)
        block = np.frombuffer(bytes(E.mem[base << 8:(base << 8) + 1280]), dtype=np.int8)
        drums.append(block)
        inst.append({'n': label.replace('sample_', '').upper(), 'f': label[7:],
                     'pk': int(np.abs(block).max())})
    drum_inst0 = len(inst) - 2          # index of the first drum instrument

    # --------------------------------------------------------------- per tick
    wpage = np.full((n_ticks, 5), -1, dtype=np.int8)
    freq = np.zeros((n_ticks, 5), dtype=np.uint16)
    dpage = np.full(n_ticks, -1, dtype=np.int8)
    doff = np.zeros(n_ticks, dtype=np.uint8)
    vol = np.zeros(n_ticks, dtype=np.uint8)
    for i, f in enumerate(E.frames):
        for v in range(5):
            if f.waves[v] != zero and E.page_name.get(f.waves[v]) != 'zeros':
                wpage[i, v] = iid.get(f.waves[v], -1)
            freq[i, v] = f.freqs[v]
        if f.drum in drum_base:
            which, page = drum_base[f.drum]
            dpage[i] = drum_inst0 + which
            doff[i] = page
        vol[i] = f.vol

    row_start = np.array([r.tick for r in E.rows], dtype=np.uint16)
    sections, last = [], None
    for i, r in enumerate(E.rows):
        if r.section != last:
            sections.append([i, r.section])
            last = r.section

    off = tuning_offset(E)
    data = {
        'meta': {
            'title': 'WE ARE VOCODERS', 'author': 'Dmitry Milk',
            'sr': round(E.sample_rate, 4), 'tps': E.tps, 'cpu': E.cpu,
            'frameLen': 216, 'speed': 5,
            'tickMs': round(E.tick_seconds * 1000, 4),
            'bpm': round(60 / (E.tick_seconds * 5 * 4), 1),
            'ticks': n_ticks, 'rows': n_rows,
            'dur': round(n_ticks * E.tick_seconds, 2),
            'tuning': round(440 * 2 ** (off / 12), 1),
            'tuneOffset': round(off, 4),
            'chan': CHANNEL_NAMES,
            'drumInst0': drum_inst0,
        },
        'inst': inst,
        'waves': _b64(np.concatenate(tables)),
        'drums': _b64(np.concatenate(drums)),
        'wpage': _b64(wpage.ravel()),
        'freq': _b64(freq.ravel()),
        'dpage': _b64(dpage),
        'doff': _b64(doff),
        'vol': _b64(vol),
        'rowStart': _b64(row_start),
        'sections': sections,
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(',', ':')))
    return data
