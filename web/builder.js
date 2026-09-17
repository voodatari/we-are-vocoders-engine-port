/* ---------------------------------------------------------------------------
   builder.js — Builds the song data in the visitor's browser, straight from the
   author's own repository.

   WHY THIS EXISTS
   ---------------
   The wavetables, the drum samples and the note data belong to Dmitry Milk and
   his repository carries no licence, so this project does not redistribute them.
   Instead of shipping a pre-built song.json, the page fetches the original
   sources from raw.githubusercontent.com and does here exactly what the Python
   tool does offline: assemble the .asm data into a 64 KB image, interpret the
   note stream, and collect the engine's state on every tick.

   Nothing of his is stored on this site's server. The only copy a visitor gets
   comes from his own published repository, and it is cached in that visitor's
   browser afterwards.

   This is a port of tools/engine.py + tools/webexport.py. Keep them in step.
   --------------------------------------------------------------------------- */
'use strict';

const SRC = {
  owner: 'DmitryMilk',
  repo: 'we_are_vocoders',
  ref: 'master',
};
const RAW = p => `https://raw.githubusercontent.com/${SRC.owner}/${SRC.repo}/${SRC.ref}/${p}`;
const CACHE_KEY = 'we-are-vocoders-song-v1';

// Same layout as tools/engine.py — it only has to be self-consistent.
const B = {
  FRAMEPAD: 40, FRAMELEN: 216, TICKS_PER_ROW: 5,
  FRAMES: 0x6000, SEQUENCES: 0x6600, NOTEDATA: 0x7000,
  ZEROS: 0xAC00, WAVES: 0xB100,
};
const CHANNEL_NAMES = ['BASS', 'VOX L', 'VOX R', 'SWEEP A', 'SWEEP B', 'DRUMS'];

/* ------------------------------------------------------------ expressions -- */
/* A tiny safe evaluator. The data uses only + - * / and parentheses, and in
   assembly `/` is integer division. Never eval(): the input is fetched text. */
function evalExpr(src, sym) {
  const toks = src.replace(/\b(\d[0-9A-Fa-f]*)h\b/g, (_, h) => String(parseInt(h, 16)))
                  .match(/[A-Za-z_]\w*|\d+|[+\-*/()]/g) || [];
  let i = 0;
  const peek = () => toks[i];
  const expr = () => {
    let v = term();
    while (peek() === '+' || peek() === '-') v = toks[i++] === '+' ? v + term() : v - term();
    return v;
  };
  const term = () => {
    let v = unary();
    while (peek() === '*' || peek() === '/')
      v = toks[i++] === '*' ? v * unary() : Math.trunc(v / unary());
    return v;
  };
  const unary = () => {
    if (peek() === '-') { i++; return -unary(); }
    const t = toks[i++];
    if (t === '(') { const v = expr(); i++; return v; }
    if (/^\d+$/.test(t)) return parseInt(t, 10);
    if (t in sym) return sym[t];
    throw new Error('unknown symbol in expression: ' + t);
  };
  return expr();
}

/* -------------------------------------------------------------- assembler -- */
const strip = l => l.split(';')[0].replace(/\s+$/, '');
const RE_LABEL = /^(\w+):\s*$/;
const RE_DATA = /^(\w+)?\s*(db|dw)\s+(.*)$/;
const RE_INCBIN = /^(\w+)\s+incbin\s+(\S+)/;

function scan(lines, base, sym, sizes, align) {
  let pc = base;
  const labels = {};
  for (const line of lines) {
    const s = line.trim();
    if (!s) continue;
    let m;
    if ((m = RE_LABEL.exec(s))) { labels[m[1]] = pc; }
    else if ((m = RE_DATA.exec(s))) {
      if (m[1]) labels[m[1]] = pc;
      const n = m[3].split(',').filter(o => o.trim()).length;
      pc += n * (m[2] === 'db' ? 1 : 2);
    } else if ((m = RE_INCBIN.exec(s))) {
      if (align) pc = Math.ceil(pc / align) * align;
      labels[m[1]] = pc;
      pc += sizes[m[2]];
    } else throw new Error('unrecognised line: ' + s);
  }
  Object.assign(sym, labels);
  return [labels, pc];
}

function emit(lines, base, mem, sym, blobs, align) {
  let pc = base;
  for (const line of lines) {
    const s = line.trim();
    if (!s || RE_LABEL.test(s)) continue;
    let m;
    if ((m = RE_DATA.exec(s))) {
      for (const o of m[3].split(',')) {
        if (!o.trim()) continue;
        const v = evalExpr(o, sym);
        mem[pc++] = v & 0xFF;
        if (m[2] === 'dw') mem[pc++] = (v >> 8) & 0xFF;
      }
    } else if ((m = RE_INCBIN.exec(s))) {
      if (align) pc = Math.ceil(pc / align) * align;
      mem.set(blobs[m[2]], pc);
      pc += blobs[m[2]].length;
    }
  }
  return pc;
}

/* --------------------------------------------------------------- fetching -- */
async function fetchText(path) {
  const r = await fetch(RAW(path));
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return (await r.text()).split(/\r?\n/).map(strip);
}

async function fetchBinary(path) {
  const r = await fetch(RAW(path));
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return new Uint8Array(await r.arrayBuffer());
}

/* Run `jobs` with a bounded number in flight — 59 wavetables is a lot of
   requests and there is no reason to open them all at once. */
async function pool(jobs, limit, onDone) {
  const out = new Array(jobs.length);
  let next = 0, done = 0;
  await Promise.all(Array.from({ length: Math.min(limit, jobs.length) }, async () => {
    while (next < jobs.length) {
      const i = next++;
      out[i] = await jobs[i]();
      if (onDone) onDone(++done, jobs.length);
    }
  }));
  return out;
}

/* ---------------------------------------------------------- the interpreter */
function parseNotecode(lines) {
  const ops = [], labelAt = {};
  for (const raw of lines) {
    const s = raw.trim();
    if (!s) continue;
    let m;
    if ((m = /^(\w+):$/.exec(s))) { labelAt[m[1]] = ops.length; continue; }
    let op = ['nop'];
    if (/^pop\s+hl$/.test(s)) op = ['pop'];
    else if (/^pop\s+af$/.test(s)) op = ['popaf'];
    else if ((m = /^ld\s+\(([^)]+)\),\s*hl$/.exec(s))) op = ['store', m[1].replace(/\s/g, '')];
    else if ((m = /^ld\s+\(([^)]+)\),\s*a$/.exec(s))) op = ['store_a', m[1].replace(/\s/g, '')];
    else if ((m = /^ld\s+hl,\s*(\S.*)$/.exec(s))) op = ['ldhl', m[1].replace(/\s/g, '')];
    else if ((m = /^ld\s+\(hl\),\s*\(\S+\s*\+\s*(\w+)\)\s*\/\s*256$/.exec(s))) op = ['voltbl', m[1]];
    else if ((m = /^ld\s+\((callsave\d)\),\s*sp$/.exec(s))) op = ['save_sp', m[1]];
    else if ((m = /^ld\s+sp,\s*\((callsave\d)\)$/.exec(s))) op = ['load_sp', m[1]];
    else if (/^ld\s+sp,\s*hl$/.test(s)) op = ['jump_sp'];
    else if (/^jp\s+noteexit$/.test(s)) op = ['end_note'];
    else if (/^jp\s+nextnote_inner$/.test(s)) op = ['end_cont'];
    else if (/^jp\s+exit$/.test(s)) op = ['end_quit'];
    ops.push(op);
  }
  return { ops, labelAt };
}

function runEngine(mem, sym, addrtbl, ops, labelAt, pageName, zeroPage, noteLabels) {
  const frames = [], rows = [];
  let sp = sym['section_main'], seqptr = sym['seq_default'];
  const freq = [0, 0x100, 0x100, 0x100, 0x100, 0x100];
  const wave = [zeroPage, zeroPage, zeroPage, zeroPage, zeroPage, zeroPage];
  const frmdst = [true, true, true];
  const callsave = {};
  let vol = 0, drum = zeroPage, frmptr = 0, quit = false;

  const sortedLabels = Object.entries(noteLabels).sort((a, b) => a[1] - b[1]);
  const sectionOf = addr => {
    let best = '';
    for (const [n, a] of sortedLabels) { if (a <= addr) best = n; else break; }
    return best.replace('section_', '');
  };
  const rd8 = () => mem[sp++];
  const rd16 = () => { const v = mem[sp] | (mem[sp + 1] << 8); sp += 2; return v; };
  const emitFrame = () => frames.push([wave[1], wave[2], wave[3], wave[4], wave[5],
                                       freq[1], freq[2], freq[3], freq[4], freq[5],
                                       drum, vol]);
  const loadRow = a => {
    if (frmdst[0]) wave[1] = mem[a];
    if (frmdst[1]) wave[2] = mem[a + 1];
    if (frmdst[2]) wave[3] = mem[a + 2];
    frmptr = a + 3;
  };
  const playRow = idx => {
    const row = { tick: frames.length, n: 0, section: sectionOf(sp) };
    rows.push(row);
    const q = sym[addrtbl[idx]];
    drum = (mem[seqptr] | (mem[seqptr + 1] << 8)) >> 8;
    seqptr += 2;
    loadRow(q); emitFrame();
    while (mem[frmptr] !== 0) { drum = (drum + 1) & 0xFF; loadRow(frmptr); emitFrame(); }
    row.n = frames.length - row.tick;
  };
  const runRoutine = name => {
    let i = labelAt[name], val = null, hl = null;
    for (;;) {
      const [kind, arg] = ops[i++];
      if (kind === 'pop' || kind === 'popaf') { val = rd16(); hl = null; }
      else if (kind === 'ldhl') { hl = arg; val = null; }
      else if (kind === 'store') {
        if (val !== null) {
          if (arg.startsWith('freq')) freq[+arg[4]] = val;
          else if (arg === 'wave4+1') wave[4] = val & 0xFF;
          else if (arg === 'wave5') wave[5] = val >> 8;
          else if (arg === 'wave2+1') wave[2] = val & 0xFF;
          else if (arg === 'wave3') wave[3] = val >> 8;
          else if (arg === 'seqptr') seqptr = val;
        } else if (hl !== null && arg.startsWith('frmdst')) {
          const n = +arg[6];
          if (n <= 3) frmdst[n - 1] = (hl !== 'fakevar');
        }
      }
      else if (kind === 'store_a') { if (arg === 'wave1+1') wave[1] = (val >> 8) & 0xFF; }
      else if (kind === 'voltbl') vol = +arg[arg.length - 1];
      else if (kind === 'save_sp') callsave[arg] = sp;
      else if (kind === 'load_sp') sp = callsave[arg];
      else if (kind === 'jump_sp') sp = val;
      else if (kind === 'end_note') { playRow(rd8()); return; }
      else if (kind === 'end_cont') return;
      else if (kind === 'end_quit') { quit = true; return; }
    }
  };

  while (!quit) {
    const b = rd8();
    if (b < 0x80) playRow(b);
    else { const n = addrtbl[b]; if (n === 'nts_quit') break; runRoutine(n); }
  }
  return { frames, rows };
}

/* ------------------------------------------------------------------ build -- */
async function buildSong(progress = () => {}) {
  progress('reading the note data…', 0);

  const mem = new Uint8Array(65536);
  const sym = { FRAMEPAD: B.FRAMEPAD, zeros: B.ZEROS };

  const names = ['waves', 'samples', 'frames', 'sequences', 'notedata', 'addrtbl', 'notecode'];
  const asm = {};
  (await pool(names.map(n => () => fetchText(`song_data/${n}.asm`)), 4))
    .forEach((v, i) => { asm[names[i]] = v; });

  // Which .dat files does the data reference, and how big are they?
  const incbins = [];
  for (const which of ['waves', 'samples'])
    for (const line of asm[which]) {
      const m = RE_INCBIN.exec(line.trim());
      if (m) incbins.push(m[2]);
    }
  progress('downloading wavetables…', 0.1);
  const blobsArr = await pool(incbins.map(p => () => fetchBinary(p)), 8,
    (d, t) => progress(`downloading wavetables… ${d}/${t}`, 0.1 + 0.7 * d / t));
  const blobs = {}, sizes = {};
  incbins.forEach((p, i) => { blobs[p] = blobsArr[i]; sizes[p] = blobsArr[i].length; });

  progress('assembling…', 0.85);
  const [waveLabels, waveEnd] = scan(asm.waves, B.WAVES, sym, sizes, 256);
  const smpBase = Math.ceil(waveEnd / 256) * 256;
  const [sampleLabels] = scan(asm.samples, smpBase, sym, sizes, 256);
  emit(asm.waves, B.WAVES, mem, sym, blobs, 256);
  emit(asm.samples, smpBase, mem, sym, blobs, 256);
  scan(asm.frames, B.FRAMES, sym, sizes); emit(asm.frames, B.FRAMES, mem, sym, blobs);
  scan(asm.sequences, B.SEQUENCES, sym, sizes); emit(asm.sequences, B.SEQUENCES, mem, sym, blobs);
  const [noteLabels] = scan(asm.notedata, B.NOTEDATA, sym, sizes);
  emit(asm.notedata, B.NOTEDATA, mem, sym, blobs);

  const pageName = {};
  for (const [n, a] of Object.entries({ ...waveLabels, ...sampleLabels })) pageName[a >> 8] = n;
  const zeroPage = B.ZEROS >> 8;
  for (let p = zeroPage; p < zeroPage + B.TICKS_PER_ROW; p++) pageName[p] = 'zeros';

  const addrtbl = [];
  for (const line of asm.addrtbl) {
    const m = /^\s*db\s+(\w+)\s*%\s*256/.exec(line);
    if (m) addrtbl.push(m[1]);
  }
  if (addrtbl.length !== 256) throw new Error('addrtbl.asm should hold 256 entries');

  progress('running the note stream…', 0.92);
  const { ops, labelAt } = parseNotecode(asm.notecode);
  const { frames, rows } = runEngine(mem, sym, addrtbl, ops, labelAt, pageName,
                                     zeroPage, noteLabels);

  progress('building the score…', 0.97);
  return pack(mem, sym, pageName, zeroPage, frames, rows);
}

/* Same output shape as tools/webexport.py, so app.js does not care which of the
   two produced it. */
function pack(mem, sym, pageName, zeroPage, frames, rows) {
  const nT = frames.length, nR = rows.length;
  const CPU = 3_500_000, TPS = 423, FRAMELEN = B.FRAMELEN;
  const sr = CPU / TPS, tickS = FRAMELEN / sr;

  const pages = [...new Set(frames.flatMap(f => f.slice(0, 5)))].sort((a, b) => a - b);
  const inst = [], iid = {}, tables = [];
  for (const p of pages) {
    const name = pageName[p];
    if (!name || name === 'zeros') continue;
    const short = name.replace('wave', '');
    const tbl = new Int8Array(mem.buffer, p << 8, 256);
    iid[p] = inst.length;
    inst.push({ n: short.toUpperCase().slice(0, 8),
                f: short.replace(/\d+$/, '') || short,
                pk: Math.max(...Array.from(tbl, v => Math.abs(v))) });
    tables.push(new Int8Array(tbl));
  }
  const drumBase = {}, drums = [];
  for (const label of ['sample_kick', 'sample_snare']) {
    const base = sym[label] >> 8;
    for (let k = 0; k < 5; k++) drumBase[base + k] = [drums.length, k];
    const blk = new Int8Array(mem.buffer.slice(base << 8, (base << 8) + 1280));
    drums.push(blk);
    inst.push({ n: label.replace('sample_', '').toUpperCase(), f: label.slice(7),
                pk: Math.max(...Array.from(blk, v => Math.abs(v))) });
  }
  const drumInst0 = inst.length - 2;

  const wpage = new Int8Array(nT * 5).fill(-1);
  const freq = new Uint16Array(nT * 5);
  const dpage = new Int8Array(nT).fill(-1);
  const doff = new Uint8Array(nT);
  const vol = new Uint8Array(nT);
  frames.forEach((f, i) => {
    for (let v = 0; v < 5; v++) {
      const p = f[v];
      if (p !== zeroPage && pageName[p] !== 'zeros') wpage[i * 5 + v] = iid[p] ?? -1;
      freq[i * 5 + v] = f[5 + v];
    }
    if (f[10] in drumBase) { const [w, pg] = drumBase[f[10]]; dpage[i] = drumInst0 + w; doff[i] = pg; }
    vol[i] = f[11];
  });

  const rowStart = new Uint16Array(rows.map(r => r.tick));
  const sections = [];
  let last = null;
  rows.forEach((r, i) => { if (r.section !== last) { sections.push([i, r.section]); last = r.section; } });

  // tuning offset: the grid the song is actually in, nearest to A440
  const used = [...new Set(Array.from(freq).filter(f => f > 0))]
    .map(f => f * sr / 65536).filter(h => h > 20)
    .map(h => 12 * Math.log2(h / 440) + 69);
  let off = 0, bestErr = Infinity;
  for (let o = -0.5; o < 0.5; o += 0.001) {
    const e = used.reduce((s, x) => s + Math.abs((x - o) - Math.round(x - o)), 0) / used.length;
    if (e < bestErr) { bestErr = e; off = o; }
  }

  const cat = arrs => { const n = arrs.reduce((s, a) => s + a.length, 0);
    const out = new Int8Array(n); let o = 0;
    for (const a of arrs) { out.set(a, o); o += a.length; } return out; };
  const b64 = a => { const u = new Uint8Array(a.buffer, a.byteOffset, a.byteLength);
    let s = ''; for (let i = 0; i < u.length; i++) s += String.fromCharCode(u[i]);
    return btoa(s); };

  return {
    meta: {
      title: 'WE ARE VOCODERS', author: 'Dmitry Milk',
      sr: +sr.toFixed(4), tps: TPS, cpu: CPU, frameLen: FRAMELEN, speed: 5,
      tickMs: +(tickS * 1000).toFixed(4),
      bpm: +(60 / (tickS * 5 * 4)).toFixed(1),
      ticks: nT, rows: nR, dur: +(nT * tickS).toFixed(2),
      tuning: +(440 * 2 ** (off / 12)).toFixed(1), tuneOffset: +off.toFixed(4),
      chan: CHANNEL_NAMES, drumInst0: drumInst0,
    },
    inst,
    waves: b64(cat(tables)), drums: b64(cat(drums)),
    wpage: b64(wpage), freq: b64(freq), dpage: b64(dpage),
    doff: b64(doff), vol: b64(vol), rowStart: b64(rowStart),
    sections,
  };
}

/* ------------------------------------------------------------------ cache -- */
function loadCached() {
  try {
    const s = localStorage.getItem(CACHE_KEY);
    return s ? JSON.parse(s) : null;
  } catch (_) { return null; }
}

function saveCached(data) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(data)); } catch (_) { /* full or blocked */ }
}

window.SongBuilder = { buildSong, loadCached, saveCached, SRC, CACHE_KEY };
