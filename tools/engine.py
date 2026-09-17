#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py — Core: assembles the song data and runs the note-stream interpreter
of the "We Are Vocoders" beeper engine (Dmitry Milk).

WHAT THIS FILE DOES
===================
The original repository contains no music file: the song lives spread across
several .asm sources that the assembler turns into bytes. Two things happen
here:

  1. A miniature assembler (`Assembler`) reads those .asm files and produces a
     64 KB memory image, the way sjasmplus would. It only understands what we
     need: labels, `db`, `dw` and `incbin`, plus the expressions the data uses
     (`wave5bp0 / 256 + wavebp0`, `sample_kick + FRAMEPAD`, `947h`, …).

  2. An interpreter (`Engine.run`) reproduces the Z80's logic: it reads the note
     stream, executes the commands and records the engine's state on every frame.

HOW THE ORIGINAL ENGINE WORKS (summary)
=======================================
  · 6 voices are summed sample by sample: 5 wavetable oscillators plus 1 PCM
    channel for the drums. Each oscillator has a 16-bit phase accumulator whose
    high byte indexes a 256-byte table of signed values.
  · The sum lands in the Z80's A register (8-bit, it WRAPS) and is used as the
    index into a jump table that picks one of 129 code "variants" generated at
    startup; each one emits a different pulse width. That is where the ±64
    clipping comes from (MIN_VARIANT..MAX_VARIANT).
  · Time is organised in FRAMES of 216 samples (~26 ms). In tracker terms that
    is a "tick". Five ticks make a ROW (~130 ms), the musical unit.
  · The note data is a byte stream: a byte < 0x80 is the index of a "qframe"
    (5 rows of 3 bytes holding the wave pages of voices 1-3); a byte >= 0x80 is
    a command that reads its parameters from the stream itself.
  · Envelopes are not multiplied: the engine switches wavetable (bass2 → bass1 →
    bass0 are the same wave at lower amplitude — and also more filtered).

SAMPLE RATE
===========
The loop takes TPS T-states per sample. The nominal value you would deduce from
the generator's constants is 212 + 178 + 52 (iloop) = 442, but 10 T must be
subtracted for the `ret` that is accounted for and never actually executes, and
about 9 T still go unexplained by static analysis. The default, 423, is
CALIBRATED against a recording of real hardware (see calibrate.py): it matches
in tempo to within 0.025% and in pitch to within 0.002 semitones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Engine constants (from song_data/engine_defs.asm and core_defs.asm)
# --------------------------------------------------------------------------- #
FRAMEPAD = 40           # register C starts here and counts up to 256
FRAMELEN = 216          # 256 - FRAMEPAD = samples per frame (tick)
TICKS_PER_ROW = 5       # QFRAME_LEN: ticks in one row
TPS_DEFAULT = 423       # T-states per sample (calibrated, see docstring)
CPU_DEFAULT = 3_500_000 # ZX Spectrum 48K clock

MIN_VARIANT, MAX_VARIANT = 64, 193      # level = sum + 128, limited to [64,192]
CLIP_LO, CLIP_HI = MIN_VARIANT - 128, MAX_VARIANT - 1 - 128     # -64 .. +64

# The six jump tables are six global volume levels (rtcg_att_table), over 256.
ATT = (256, 179, 102, 64, 30, 12)

# Synthetic memory layout. It does not have to match the real binary: it only
# needs to be self-consistent, with the wavetables aligned to 256.
ADDR_FRAMES    = 0x6000
ADDR_SEQUENCES = 0x6600
ADDR_NOTEDATA  = 0x7000
ADDR_ZEROS     = 0xAC00     # 5 pages of silence (mute wave and mute drum)
ADDR_WAVES     = 0xB100


# --------------------------------------------------------------------------- #
# Miniature assembler
# --------------------------------------------------------------------------- #
class Assembler:
    """Turns the repository's data .asm files into a 64 KB image.

    Only the subset the data files use is supported:
      label:               defines a symbol
      label db a, b, c     bytes
      label dw a, b        little-endian words
      label incbin path    binary contents (with optional alignment)

    Two passes are needed because expressions may refer to labels that have not
    been seen yet.
    """

    RE_LABEL  = re.compile(r'^(\w+):\s*$')
    RE_DATA   = re.compile(r'^(\w+)?\s*(db|dw)\s+(.*)$')
    RE_INCBIN = re.compile(r'^(\w+)\s+incbin\s+(\S+)')
    RE_HEX    = re.compile(r'\b(\d[0-9A-Fa-f]*)h\b')
    RE_IDENT  = re.compile(r'[A-Za-z_]\w*')

    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self.mem = bytearray(65536)
        self.sym: dict[str, int] = {'FRAMEPAD': FRAMEPAD, 'zeros': ADDR_ZEROS}

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _strip(line: str) -> str:
        """Drop the comment (';' to end of line) and trailing whitespace."""
        return line.split(';')[0].rstrip()

    def eval(self, expr: str) -> int:
        """Evaluate an assembler expression.

        Converts `947h` to 2375, substitutes labels for their address, and uses
        integer division (in assembly `/` is integer division).
        """
        e = self.RE_HEX.sub(lambda m: str(int(m.group(1), 16)), expr)
        e = self.RE_IDENT.sub(lambda m: str(self.sym[m.group(0)]), e)
        return eval(e.replace('/', '//'))       # noqa: S307 - controlled input

    # -- passes -------------------------------------------------------------
    def scan(self, path: Path, base: int, align: int | None = None):
        """First pass: work out the address of every label.

        Returns (labels, end_address, lines) so the second pass can reuse the
        lines without re-reading the file.
        """
        lines = [self._strip(l) for l in open(path)]
        pc, labels = base, {}
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if (m := self.RE_LABEL.match(s)):
                labels[m.group(1)] = pc
            elif (m := self.RE_DATA.match(s)):
                if m.group(1):
                    labels[m.group(1)] = pc
                n = len([o for o in m.group(3).split(',') if o.strip()])
                pc += n * (1 if m.group(2) == 'db' else 2)
            elif (m := self.RE_INCBIN.match(s)):
                if align:
                    pc = -(-pc // align) * align
                labels[m.group(1)] = pc
                pc += (self.repo / m.group(2)).stat().st_size
            else:
                raise RuntimeError(f'unrecognised line in {path}: {s!r}')
        self.sym.update(labels)
        return labels, pc, lines

    def emit(self, lines, base: int, align: int | None = None) -> int:
        """Second pass: write the bytes into the memory image."""
        pc = base
        for line in lines:
            s = line.strip()
            if not s or self.RE_LABEL.match(s):
                continue
            if (m := self.RE_DATA.match(s)):
                for operand in (o for o in m.group(3).split(',') if o.strip()):
                    v = self.eval(operand)
                    self.mem[pc] = v & 0xFF
                    pc += 1
                    if m.group(2) == 'dw':
                        self.mem[pc] = (v >> 8) & 0xFF
                        pc += 1
            elif (m := self.RE_INCBIN.match(s)):
                if align:
                    pc = -(-pc // align) * align
                data = (self.repo / m.group(2)).read_bytes()
                self.mem[pc:pc + len(data)] = data
                pc += len(data)
        return pc


# --------------------------------------------------------------------------- #
# Engine state for one tick
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Frame:
    """The engine's state during one frame (216 samples)."""
    waves: tuple[int, ...]   # wave page of voices 1..5 (high byte of the pointer)
    freqs: tuple[int, ...]   # 16-bit phase increment of voices 1..5
    drum: int                # page of the drum sample currently being read
    vol: int                 # global volume table index (0..5)


@dataclass
class Row:
    """One musical row: up to 5 ticks."""
    tick: int                # index of the first tick
    n: int                   # how many ticks it lasts
    section: str = ''        # label of the note-stream section it came from


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class Engine:
    """Assembles the data and runs the note-stream interpreter.

    Typical use:

        eng = Engine('path/to/we_are_vocoders')
        eng.run()
        print(len(eng.frames), 'ticks;', len(eng.rows), 'rows')
        print(eng.sample_rate, 'Hz')
    """

    def __init__(self, repo, tps: int = TPS_DEFAULT, cpu: int = CPU_DEFAULT):
        self.repo = Path(repo)
        if not (self.repo / 'song_data').is_dir():
            raise SystemExit(
                f'Cannot find {self.repo}/song_data. Clone the original repository:\n'
                '  git clone https://github.com/DmitryMilk/we_are_vocoders')
        self.tps, self.cpu = tps, cpu
        self.sample_rate = cpu / tps
        self.tick_seconds = FRAMELEN / self.sample_rate

        self.asm = Assembler(self.repo)
        self.mem = self.asm.mem
        self.sym = self.asm.sym
        self.frames: list[Frame] = []
        self.rows: list[Row] = []

        self._assemble()
        self._parse_notecode()

    # ------------------------------------------------------------- assembly
    def _assemble(self) -> None:
        sd = self.repo / 'song_data'
        a = self.asm

        # Wavetables and samples must land on 256-byte boundaries: the engine
        # indexes with the low byte of a pointer whose high byte is "the page".
        self.wave_labels, wave_end, wave_lines = a.scan(sd / 'waves.asm', ADDR_WAVES, 256)
        smp_base = -(-wave_end // 256) * 256
        self.sample_labels, _, smp_lines = a.scan(sd / 'samples.asm', smp_base, 256)
        a.emit(wave_lines, ADDR_WAVES, 256)
        a.emit(smp_lines, smp_base, 256)

        _, _, frame_lines = a.scan(sd / 'frames.asm', ADDR_FRAMES)
        a.emit(frame_lines, ADDR_FRAMES)
        _, _, seq_lines = a.scan(sd / 'sequences.asm', ADDR_SEQUENCES)
        a.emit(seq_lines, ADDR_SEQUENCES)
        self.note_labels, _, note_lines = a.scan(sd / 'notedata.asm', ADDR_NOTEDATA)
        a.emit(note_lines, ADDR_NOTEDATA)

        # page -> readable name, for reports and for the score
        self.page_name: dict[int, str] = {}
        for name, addr in {**self.wave_labels, **self.sample_labels}.items():
            self.page_name[addr >> 8] = name
        for p in range(ADDR_ZEROS >> 8, (ADDR_ZEROS >> 8) + TICKS_PER_ROW):
            self.page_name[p] = 'zeros'
        self.zero_page = ADDR_ZEROS >> 8

        # addrtbl: 256 entries. 0..127 are qframes, 128..255 are nts_* routines.
        self.addrtbl = [m.group(1) for line in open(sd / 'addrtbl.asm')
                        if (m := re.match(r'\s*db\s+(\w+)\s*%\s*256', Assembler._strip(line)))]
        assert len(self.addrtbl) == 256, 'addrtbl.asm should hold 256 entries'

    # ---------------------------------------------------- notecode commands
    def _parse_notecode(self) -> None:
        """Translate notecode.asm into a flat list of micro-operations.

        Every `nts_*` routine is a run of instructions that falls through into
        the next one: `nts_seq_f3_f2_f1_wp45_end` continues inside
        `nts_f3_f2_f1_wp45_end`. That is why this does NOT store one list per
        routine but a single global `self.ops` plus a label -> position index:
        to run a routine you start at its position and walk forward until you
        hit a terminator, which is exactly what the Z80 does.

        Routine names describe their parameters in order: `f3` loads freq3,
        `wp45` a word holding the wave pages of voices 4 and 5, `seq` the drum
        pattern, `w1` the page of voice 1, and `v3i`/`v3w` connect or disconnect
        voice 3 from the frame data.
        """
        ops, label_at = [], {}
        for raw in open(self.repo / 'song_data' / 'notecode.asm'):
            s = Assembler._strip(raw).strip()
            if not s:
                continue
            if (m := re.match(r'^(\w+):$', s)):
                label_at[m.group(1)] = len(ops)
                continue
            op = ('nop',)
            if re.match(r'^pop\s+hl$', s):
                op = ('pop',)                       # read a word from the stream
            elif re.match(r'^pop\s+af$', s):
                op = ('popaf',)                     # same, the high byte is used
            elif (m := re.match(r'^ld\s+\(([^)]+)\),\s*hl$', s)):
                op = ('store', m.group(1).replace(' ', ''))
            elif (m := re.match(r'^ld\s+\(([^)]+)\),\s*a$', s)):
                op = ('store_a', m.group(1).replace(' ', ''))
            elif (m := re.match(r'^ld\s+hl,\s*(\S.*)$', s)):
                op = ('ldhl', m.group(1).replace(' ', ''))
            elif (m := re.match(r'^ld\s+\(hl\),\s*\(\S+\s*\+\s*(\w+)\)\s*/\s*256$', s)):
                op = ('voltbl', m.group(1))         # global volume change
            elif (m := re.match(r'^ld\s+\((callsave\d)\),\s*sp$', s)):
                op = ('save_sp', m.group(1))        # "call" inside the stream
            elif (m := re.match(r'^ld\s+sp,\s*\((callsave\d)\)$', s)):
                op = ('load_sp', m.group(1))        # "ret"
            elif re.match(r'^ld\s+sp,\s*hl$', s):
                op = ('jump_sp',)
            elif re.match(r'^jp\s+noteexit$', s):
                op = ('end_note',)                  # a qframe follows the params
            elif re.match(r'^jp\s+nextnote_inner$', s):
                op = ('end_cont',)                  # keep reading commands
            elif re.match(r'^jp\s+exit$', s):
                op = ('end_quit',)
            ops.append(op)
        self.ops, self.label_at = ops, label_at

    # ----------------------------------------------------------- utilities
    def rd8(self) -> int:
        v = self.mem[self._sp]
        self._sp += 1
        return v

    def rd16(self) -> int:
        v = self.mem[self._sp] | (self.mem[self._sp + 1] << 8)
        self._sp += 2
        return v

    def wave_data(self, page: int) -> np.ndarray:
        """The 256 signed bytes of a wave page, as float64."""
        raw = bytes(self.mem[page << 8:(page << 8) + 256])
        return np.frombuffer(raw, dtype=np.int8).astype(np.float64)

    def section_of(self, addr: int) -> str:
        """Note-stream section label containing that address."""
        best = ''
        for a, n in sorted((v, k) for k, v in self.note_labels.items()):
            if a <= addr:
                best = n
        return best.replace('section_', '')

    # ------------------------------------------------------------- run loop
    def run(self) -> 'Engine':
        """Play the whole song, filling `frames` and `rows`."""
        self._sp = self.sym['section_main']      # note-stream pointer
        self.seqptr = self.sym['seq_default']    # drum-pattern pointer
        self.freq = [0] + [0x100] * 5            # freq[1..5]
        self.wave = [self.zero_page] * 6         # wave[1..5]
        self.frmdst = [True, True, True]         # do frames drive v1, v2, v3?
        self.callsave = {'callsave1': 0, 'callsave2': 0}
        self.vol = 0
        self.drum = self.zero_page
        self.frmptr = 0
        self.quit = False
        self.frames, self.rows = [], []

        while not self.quit:
            b = self.rd8()
            if b < 0x80:
                self._play_row(b)                # qframe index
            else:
                name = self.addrtbl[b]           # command
                if name == 'nts_quit':
                    break
                self._run_routine(name)
        return self

    def _emit(self) -> None:
        self.frames.append(Frame(tuple(self.wave[1:6]), tuple(self.freq[1:6]),
                                 self.drum, self.vol))

    def _load_frame_row(self, addr: int) -> None:
        """Load the 3 bytes of a qframe row: the pages of voices 1, 2 and 3.

        A voice whose `frmdst` is disconnected ignores the frame data and keeps
        whatever page a note command gave it.
        """
        b = self.mem[addr:addr + 3]
        if self.frmdst[0]: self.wave[1] = b[0]
        if self.frmdst[1]: self.wave[2] = b[1]
        if self.frmdst[2]: self.wave[3] = b[2]
        self.frmptr = addr + 3

    def _play_row(self, idx: int) -> None:
        """Play a whole qframe (up to 5 ticks) and advance the drum sequencer."""
        self.rows.append(Row(len(self.frames), 0, self.section_of(self._sp)))
        qaddr = self.sym[self.addrtbl[idx]]
        # the sequencer hands over this row's drum pointer
        self.drum = (self.mem[self.seqptr] | (self.mem[self.seqptr + 1] << 8)) >> 8
        self.seqptr += 2
        self._load_frame_row(qaddr)
        self._emit()
        while self.mem[self.frmptr] != 0:        # 0 terminates the qframe
            self.drum = (self.drum + 1) & 0xFF   # next page of the sample
            self._load_frame_row(self.frmptr)
            self._emit()
        self.rows[-1].n = len(self.frames) - self.rows[-1].tick

    def _run_routine(self, name: str) -> None:
        """Run an `nts_*` routine from its label until the terminator."""
        i = self.label_at[name]
        val = hl = None
        while True:
            kind, *rest = self.ops[i]
            i += 1
            if kind in ('pop', 'popaf'):
                val, hl = self.rd16(), None
            elif kind == 'ldhl':
                hl, val = rest[0], None
            elif kind == 'store':
                target = rest[0]
                if val is not None:              # the value came from the stream
                    if target.startswith('freq'):
                        self.freq[int(target[4])] = val
                    elif target == 'wave4+1':    # packed v4/v5 word
                        self.wave[4] = val & 0xFF
                    elif target == 'wave5':
                        self.wave[5] = val >> 8
                    elif target == 'wave2+1':    # packed v2/v3 word
                        self.wave[2] = val & 0xFF
                    elif target == 'wave3':
                        self.wave[3] = val >> 8
                    elif target == 'seqptr':
                        self.seqptr = val
                elif hl is not None and target.startswith('frmdst'):
                    n = int(target[6])           # frmdst4/5 point at fakevar: inert
                    if n <= 3:
                        self.frmdst[n - 1] = (hl != 'fakevar')
            elif kind == 'store_a':
                if rest[0] == 'wave1+1':
                    self.wave[1] = (val >> 8) & 0xFF
            elif kind == 'voltbl':
                self.vol = int(rest[0][-1])
            elif kind == 'save_sp':
                self.callsave[rest[0]] = self._sp
            elif kind == 'load_sp':
                self._sp = self.callsave[rest[0]]
            elif kind == 'jump_sp':
                self._sp = val
            elif kind == 'end_note':
                self._play_row(self.rd8())       # next byte is the qframe index
                return
            elif kind == 'end_cont':
                return
            elif kind == 'end_quit':
                self.quit = True
                return

    # -------------------------------------------------------------- reports
    def summary(self) -> str:
        nt, nr = len(self.frames), len(self.rows)
        dur = nt * self.tick_seconds
        return '\n'.join([
            f'repository     {self.repo}',
            f'sample rate    {self.sample_rate:.2f} Hz  ({self.tps} T @ {self.cpu/1e6:.4f} MHz)',
            f'tick / row     {self.tick_seconds*1000:.2f} ms / {self.tick_seconds*5*1000:.1f} ms',
            f'tempo          {60/(self.tick_seconds*5*4):.1f} BPM',
            f'ticks / rows   {nt} / {nr}',
            f'duration       {dur:.2f} s  ({int(dur//60)}:{dur%60:04.1f})',
            f'samples        {nt*FRAMELEN}',
            f'wavetables     {len(self.wave_labels)} + {len(self.sample_labels)} samples',
        ])
