#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vocoders.py — Command line for the toolkit.

    python3 vocoders.py info      --repo ../we_are_vocoders
    python3 vocoders.py render    --repo ../we_are_vocoders --out out/
    python3 vocoders.py score     --repo ../we_are_vocoders --text 832 24
    python3 vocoders.py webdata   --repo ../we_are_vocoders --out ../web/data/song.json
    python3 vocoders.py calibrate --repo ../we_are_vocoders --ref recording.mp3

Run `python3 vocoders.py <command> -h` for every option.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from engine import CPU_DEFAULT, TPS_DEFAULT, Engine

VERSIONS = {
    'exact':      'the Z80 engine, sample for sample (clipping, 129 levels)',
    'beeper':     '1-bit output reconstructed at 3.5 MHz',
    'antialias':  'same rate and clipping, no foldover (except where it is the timbre)',
    'remaster':   'native re-synthesis at 44.1 kHz, floating point',
    'production': 'remaster plus morphing, stereo, reverb and lift',
}
CHANNELS = {1: 'voice 1 (bass)', 2: 'voice 2 (vocal L)', 3: 'voice 3 (vocal R)',
            4: 'voice 4 (sweep A)', 5: 'voice 5 (sweep B)', 6: 'drums'}


def build_engine(args) -> Engine:
    eng = Engine(args.repo, tps=args.tps, cpu=args.cpu)
    t0 = time.time()
    eng.run()
    if not getattr(args, 'quiet', False):
        print(f'[interpreter] {len(eng.frames)} ticks in {time.time()-t0:.1f} s',
              file=sys.stderr)
    return eng


def parse_voices(args) -> set[int] | None:
    """--voices 2,3 keeps only those; --without 6 drops the drums."""
    voices = set(range(1, 7))
    if args.voices:
        voices = {int(v) for v in args.voices.replace(',', ' ').split()}
    if args.without:
        voices -= {int(v) for v in args.without.replace(',', ' ').split()}
    if not voices:
        raise SystemExit('No voices left to render.')
    return voices if voices != set(range(1, 7)) else None


# --------------------------------------------------------------------------- #
def cmd_info(args) -> None:
    eng = build_engine(args)
    print(eng.summary())
    from score import tuning_offset
    off = tuning_offset(eng)
    print(f'tuning         A4 = {440*2**(off/12):.1f} Hz ({off:+.2f} semitones)')
    fams: dict[str, int] = {}
    for f in eng.frames:
        for w in f.waves:
            n = eng.page_name.get(w, '?')
            if n != 'zeros':
                fams[n.rstrip('0123456789')] = fams.get(n.rstrip('0123456789'), 0) + 1
    print('wave families  ' + ', '.join(f'{k}x{v}' for k, v in
                                        sorted(fams.items(), key=lambda kv: -kv[1])))


def cmd_render(args) -> None:
    from synth import Renderer, to_int16
    eng = build_engine(args)
    voices = parse_voices(args)
    if voices:
        print('[voices] active: ' + ', '.join(CHANNELS[v] for v in sorted(voices)),
              file=sys.stderr)
    r = Renderer(eng, voices)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    suffix = args.suffix or ('_' + '-'.join(map(str, sorted(voices))) if voices else '')

    levels = None
    for v in args.versions:
        t0 = time.time()
        if v == 'beeper':                      # reuse the exact simulation
            levels = levels if levels is not None else r.exact()
            y = r.beeper(levels)
        elif v == 'exact':
            levels = r.exact()
            y = r.to_44k(levels)
        else:
            y = r.render(v)
        path = out / f'we_are_vocoders_{v}{suffix}.wav'
        wavfile.write(path, 44100, to_int16(y))
        n = np.shape(y)[-1]
        print(f'{path}  {n/44100:.1f} s  {"stereo" if np.ndim(y) > 1 else "mono"}'
              f'  ({time.time()-t0:.1f} s)')


def cmd_score(args) -> None:
    from score import as_text
    eng = build_engine(args)
    first, count = ((args.text or []) + [0, 64])[:2]
    print(as_text(eng, int(first), int(count)))


def cmd_webdata(args) -> None:
    from webexport import export_web
    eng = build_engine(args)
    d = export_web(eng, args.out)
    m = d['meta']
    print(f'{args.out}: {m["ticks"]} ticks, {m["rows"]} rows, '
          f'{len(d["inst"])} instruments (wavetables and samples included), '
          f'{Path(args.out).stat().st_size/1024:.0f} KB')
    print('\nThis is what the web app loads by default (USE_LOCAL_DATA = true at\n'
          'the top of web/app.js). It is a derivative of the original work, used\n'
          'under its MIT licence - see NOTICE.md.', file=sys.stderr)


def cmd_calibrate(args) -> None:
    from calibrate import calibrate, report
    from synth import Renderer, to_int16
    eng = build_engine(args)
    mine = Path(args.temp or 'calib_exact.wav')
    if not mine.exists() or args.regenerate:
        print('[calibrate] rendering the exact version to compare against...',
              file=sys.stderr)
        r = Renderer(eng)
        wavfile.write(mine, 44100, to_int16(r.to_44k(r.exact())))
    res = calibrate(args.ref, mine, eng.tps, eng.cpu)
    print(report(res, eng.tps, eng.cpu))
    if res['tps'] != eng.tps:
        print(f'\nTo apply it: re-run with --tps {res["tps"]}, '
              f'or change TPS_DEFAULT in engine.py')


# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(
        description='Reconstruction of the "We Are Vocoders" beeper engine.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Versions available to render:\n' +
               '\n'.join(f'  {k:<11} {v}' for k, v in VERSIONS.items()))
    # Common options are attached to every subcommand so they can be written
    # after the command (vocoders.py render --repo ...).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--repo', default='../we_are_vocoders',
                        help='path to the original repository (default: %(default)s)')
    common.add_argument('--tps', type=int, default=TPS_DEFAULT,
                        help='T-states per sample (default: %(default)s, calibrated)')
    common.add_argument('--cpu', type=int, default=CPU_DEFAULT,
                        help='clock in Hz (default: %(default)s)')
    common.add_argument('-q', '--quiet', action='store_true')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('info', parents=[common], help='engine and song summary')
    s.set_defaults(func=cmd_info)

    s = sub.add_parser('render', parents=[common], help='render the WAV files')
    s.add_argument('--out', default='out', help='output directory')
    s.add_argument('--versions', nargs='+', default=list(VERSIONS), choices=list(VERSIONS))
    s.add_argument('--voices', help='keep only these voices, e.g. "2,3"')
    s.add_argument('--without', help='drop these voices, e.g. "6"')
    s.add_argument('--suffix', help='suffix for the output file names')
    s.set_defaults(func=cmd_render)

    s = sub.add_parser('score', parents=[common], help='print the score as text')
    s.add_argument('--text', nargs='*', metavar='N',
                   help='which rows: --text [first] [count]')
    s.set_defaults(func=cmd_score)

    s = sub.add_parser('webdata', parents=[common],
                       help='export song.json for the web app')
    s.add_argument('--out', default='../web/data/song.json')
    s.set_defaults(func=cmd_webdata)

    s = sub.add_parser('calibrate', parents=[common],
                       help='measure the real period against a recording')
    s.add_argument('--ref', required=True, help='reference recording (wav/mp3/...)')
    s.add_argument('--temp', help='intermediate exact WAV (reused if present)')
    s.add_argument('--regenerate', action='store_true')
    s.set_defaults(func=cmd_calibrate)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
