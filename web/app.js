/* ---------------------------------------------------------------------------
   app.js — Loads the engine data, builds the band-limited tables, wires up the
   AudioWorklet and the effects graph, and draws the tracker view.

   There are no audio files: the worklet synthesises the music from the same
   data the Python renderer uses, which is what makes muting channels and
   switching synthesis mode work while the music plays.
   --------------------------------------------------------------------------- */
'use strict';

const MODES = [
  { id:'exact',      name:'EXACT',      desc:'the Z80 engine, sample for sample' },
  { id:'beeper',     name:'1-BIT BEEPER',desc:'pulse train reconstructed' },
  { id:'antialias',  name:'ANTI-ALIAS', desc:'same rate, no foldover' },
  { id:'remaster',   name:'REMASTER',   desc:'band-limited at 44.1 kHz' },
  { id:'production', name:'PRODUCTION', desc:'stereo, reverb, drive' },
];
const NOTES = ['C-','C#','D-','D#','E-','F-','F#','G-','G#','A-','A#','B-'];
const CHCOL = ['--c1','--c2','--c3','--c4','--c5','--c6'];
const MIP_H = [128, 91, 64, 45, 32, 23, 16, 11, 8, 6, 4, 3, 2, 1];
const MIP_LEN = 1024;
const WINDOW_ROWS = 61, HALF = 30;

const $ = s => document.querySelector(s);
const pad = (v, n) => String(v).padStart(n, '0');
const fmt = s => `${(s / 60) | 0}:${pad(Math.max(0, s | 0) % 60, 2)}`;
const noteName = n => n < 0 ? null : NOTES[((n % 12) + 12) % 12] + Math.max(0, (n / 12 | 0) - 1);

let S, W = {};                         // song data + decoded typed arrays
let rows = [], tickToRow, nWave = 0;
let ctx, node, fx = {}, started = false, playing = false;
let mode = 'exact', mute = [0,0,0,0,0,0], soloMask = null;
let tps = 423, engineRate = 8274.23;
let pos = { tick: 0, inTick: 0 }, lastRow = -1;
let rowEls = [], scopeCtx = [], lvlEls = [], vu = [0,0,0,0,0,0], lastInst = [-1,-1,-1,-1,-1,-1];

const b64 = (s, T) => { const b = atob(s), a = new Uint8Array(b.length);
  for (let i = 0; i < b.length; i++) a[i] = b.charCodeAt(i);
  return new T(a.buffer, 0, a.byteLength / T.BYTES_PER_ELEMENT); };

/* ----------------------------------------------------------------- boot --- */
fetch('data/song.json').then(r => r.json()).then(init).catch(err => {
  $('#boot').innerHTML = '<div style="color:#ff5f6b;max-width:40ch;text-align:center">' +
    'Could not load the song data. This page must be served over HTTP ' +
    '(Netlify, or <code>python3 -m http.server</code>) — not opened as a local file.</div>';
  console.error(err);
});

function init(song) {
  S = song;
  W.waves = b64(S.waves, Int8Array);
  W.drums = b64(S.drums, Int8Array);
  W.wpage = b64(S.wpage, Int8Array);
  W.freq  = b64(S.freq,  Uint16Array);
  W.dpage = b64(S.dpage, Int8Array);
  W.doff  = b64(S.doff,  Uint8Array);
  W.vol   = b64(S.vol,   Uint8Array);
  W.rowStart = b64(S.rowStart, Uint16Array);
  nWave = W.waves.length / 256;
  tps = S.meta.tps; engineRate = S.meta.sr;

  buildRowsData();
  W.mips = buildMips();

  buildModes(); buildChannels(); buildHead(); buildRowEls(); buildMeta(); wire();
  drawRows(0);
  $('#boot').classList.add('gone');
  requestAnimationFrame(loop);
}

/* Derive the tracker score from the per-tick engine state. A note is shown when
   a voice is sounding and either changed pitch or was silent on the row before. */
function buildRowsData() {
  const off = S.meta.tuneOffset, sr = S.meta.sr;
  const noteOf = f => { if (!f) return -1;
    const hz = f * sr / 65536; if (hz < 15) return -1;
    return Math.round(12 * Math.log2(hz / 440) + 69 - off); };
  const nR = S.meta.rows, nT = S.meta.ticks;
  const sect = new Array(nR);
  let si = 0;
  for (let i = 0; i < nR; i++) {
    while (si + 1 < S.sections.length && S.sections[si + 1][0] <= i) si++;
    sect[i] = S.sections[si] ? S.sections[si][1] : '';
  }
  const prevNote = [-1,-1,-1,-1,-1], prevOn = [0,0,0,0,0];
  tickToRow = new Int16Array(nT);
  for (let i = 0; i < nR; i++) {
    const a = W.rowStart[i], b = (i + 1 < nR ? W.rowStart[i + 1] : nT);
    for (let t = a; t < b; t++) tickToRow[t] = i;
    const cells = [];
    for (let v = 0; v < 5; v++) {
      let on = 0, ins = -1;
      for (let t = a; t < b; t++) { const p = W.wpage[t * 5 + v];
        if (p >= 0) { on = 1; if (ins < 0) ins = p; } }
      const n = noteOf(W.freq[a * 5 + v]);
      const trig = on && (n !== prevNote[v] || !prevOn[v]);
      cells.push([trig ? n : -1, ins, 0]);
      if (on) { prevNote[v] = n; prevOn[v] = 1; } else prevOn[v] = 0;
    }
    cells.push([W.dpage[a] >= 0 ? 60 : -1, W.dpage[a], 0]);
    rows.push({ t: a, n: b - a, c: cells, s: sect[i], v: W.vol[a] });
  }
  let pv = 0;
  for (const r of rows) { if (r.v !== pv) r.c[0][2] = 0xF0 + r.v; pv = r.v; }
}

/* Band-limited mipmaps: one table per (instrument, harmonic limit). Built with
   a 1024-entry cosine table so no trig is needed in the inner loop. */
function buildMips() {
  const N = MIP_LEN, cos = new Float32Array(N), sin = new Float32Array(N);
  for (let i = 0; i < N; i++) { cos[i] = Math.cos(2 * Math.PI * i / N);
                                sin[i] = Math.sin(2 * Math.PI * i / N); }
  const out = new Float32Array(nWave * MIP_H.length * N);
  const acc = new Float32Array(N);
  for (let w = 0; w < nWave; w++) {
    const re = new Float32Array(129), im = new Float32Array(129);
    for (let k = 1; k <= 128; k++) {                     // DFT of the 256 bytes
      let a = 0, b = 0;
      for (let i = 0; i < 256; i++) {
        const idx = (k * i * 4) & (N - 1);               // 256 points -> stride 4
        a += W.waves[w * 256 + i] * cos[idx];
        b -= W.waves[w * 256 + i] * sin[idx];
      }
      re[k] = 2 * a / 256; im[k] = 2 * b / 256;
    }
    acc.fill(0);
    let k = 1;
    for (let m = MIP_H.length - 1; m >= 0; m--) {        // dullest first, add up
      for (; k <= MIP_H[m]; k++) {
        if (Math.abs(re[k]) < 1e-4 && Math.abs(im[k]) < 1e-4) continue;
        for (let i = 0; i < N; i++) {
          const idx = (k * i) & (N - 1);
          acc[i] += re[k] * cos[idx] - im[k] * sin[idx];
        }
      }
      out.set(acc, (w * MIP_H.length + m) * N);
    }
  }
  return out;
}

/* ------------------------------------------------------------ interface --- */
function buildModes() {
  $('#modes').innerHTML = MODES.map((m, i) =>
    `<button class="btn mbtn" type="button" data-mode="${m.id}" aria-pressed="${i === 0}">
       <span class="mn">${i + 1}. ${m.name}</span><span class="md">${m.desc}</span></button>`).join('');
  $('#modes').onclick = e => { const b = e.target.closest('[data-mode]'); if (b) setMode(b.dataset.mode); };
}

function buildChannels() {
  $('#chans').innerHTML = S.meta.chan.map((c, i) =>
    `<button class="btn chan" type="button" data-ch="${i}" data-mute="0"
       style="color:var(${CHCOL[i]})"><span class="lbl">${c}</span>
       <span class="lvl"><i></i></span></button>`).join('');
  lvlEls = [...document.querySelectorAll('.chan .lvl i')];
  $('#chans').onclick = e => {
    const b = e.target.closest('[data-ch]'); if (!b) return;
    const i = +b.dataset.ch;
    if (e.shiftKey) {                       // solo: this one only, or restore
      const isSolo = soloMask === i;
      soloMask = isSolo ? null : i;
      mute = mute.map((_, k) => soloMask === null ? 0 : (k === soloMask ? 0 : 1));
    } else { soloMask = null; mute[i] = mute[i] ? 0 : 1; }
    document.querySelectorAll('.chan').forEach((el, k) => el.dataset.mute = mute[k]);
    send({ mute });
  };
}

function buildHead() {
  $('#chanhead').innerHTML = '<div>ROW</div>' + S.meta.chan.map((c, i) =>
    `<div style="color:var(${CHCOL[i]})">${c}</div>`).join('');
  $('#scopes').innerHTML = S.meta.chan.map((c, i) =>
    `<figure><canvas width="200" height="26"></canvas>
     <figcaption id="sc${i}" style="color:var(${CHCOL[i]})">${c}</figcaption></figure>`).join('');
  scopeCtx = [...document.querySelectorAll('.scopes canvas')].map(c => c.getContext('2d'));
}

function buildRowEls() {
  $('#rows').innerHTML = Array.from({ length: WINDOW_ROWS }, () =>
    '<div class="row"><div class="n"></div>' +
    S.meta.chan.map((_, i) => `<div class="c" style="color:var(${CHCOL[i]})"></div>`).join('') +
    '</div>').join('');
  rowEls = [...$('#rows').children].map(el =>
    ({ el, n: el.firstChild, c: [...el.querySelectorAll('.c')] }));
}

function buildMeta() {
  const m = S.meta;
  $('#sub').textContent = `Dmitry Milk · DiHalt 2026 — ${m.bpm} BPM · speed ${m.speed} · ` +
    `A4 = ${m.tuning} Hz · ${m.rows} rows / ${m.ticks} ticks · ${S.inst.length} wavetables`;
  $('#dur').textContent = '/ ' + fmt(m.dur);
}

/* ---------------------------------------------------------------- audio --- */
async function start() {
  if (started) return;
  started = true;
  ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.audioWorklet.addModule('engine-worklet.js');
  node = new AudioWorkletNode(ctx, 'vocoders-engine', {
    numberOfInputs: 0, outputChannelCount: [2],
    processorOptions: {
      waves: W.waves.buffer, drums: W.drums.buffer, mips: W.mips.buffer,
      wpage: W.wpage.buffer, freq: W.freq.buffer, dpage: W.dpage.buffer,
      doff: W.doff.buffer, vol: W.vol.buffer,
      nTicks: S.meta.ticks, drumInst0: S.meta.drumInst0,
      sampleRate: S.meta.sr, tps: S.meta.tps, cpu: S.meta.cpu,
    },
  });
  node.port.onmessage = e => {
    if (e.data.type === 'pos') pos = e.data;
    else if (e.data.type === 'end') pause();
  };

  fx.drive = ctx.createWaveShaper();
  fx.tone = ctx.createBiquadFilter(); fx.tone.type = 'highshelf'; fx.tone.frequency.value = 3000;
  fx.dry = ctx.createGain(); fx.wet = ctx.createGain();
  fx.rev = ctx.createConvolver(); fx.rev.buffer = makeIR(ctx);
  fx.master = ctx.createGain();
  node.connect(fx.drive); fx.drive.connect(fx.tone);
  fx.tone.connect(fx.dry); fx.dry.connect(fx.master);
  fx.tone.connect(fx.rev); fx.rev.connect(fx.wet); fx.wet.connect(fx.master);
  fx.master.connect(ctx.destination);
  applyFx();
  send({ mode, mute, tps });
}

function makeIR(ctx) {
  const n = ctx.sampleRate * 1.1, b = ctx.createBuffer(2, n, ctx.sampleRate);
  for (let c = 0; c < 2; c++) {
    const d = b.getChannelData(c);
    let y = 0;
    for (let i = 0; i < n; i++) {
      const env = Math.exp(-i / (ctx.sampleRate * 0.20));
      y = 0.12 * (Math.random() * 2 - 1) + 0.88 * y;     // low-passed noise
      d[i] = i < ctx.sampleRate * 0.012 ? 0 : y * env;   // pre-delay
    }
  }
  return b;
}

function driveCurve(amount) {
  const k = 1 + amount * 7, n = 1024, c = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = i / (n - 1) * 2 - 1;
    c[i] = Math.tanh(k * x) / Math.tanh(k);
  }
  return c;
}

function applyFx() {
  if (!started) return;
  const prod = mode === 'production';
  const rv = +$('#reverb').value / 100, dr = +$('#drive').value / 100, tn = +$('#tone').value;
  fx.wet.gain.value = prod ? rv : 0;
  fx.dry.gain.value = prod ? 1 - rv * 0.35 : 1;
  fx.drive.curve = driveCurve(prod ? dr : 0);
  fx.tone.gain.value = prod ? tn : 0;
  send({ spread: prod ? +$('#width').value / 100 : 1 });
  $('#fxpanel').classList.toggle('off', !prod);
}

const send = m => { if (node) node.port.postMessage({ type: 'set', ...m }); };

function setMode(id) {
  mode = id;
  document.querySelectorAll('[data-mode]').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.mode === id));
  $('#fxhint').textContent = id === 'production' ? '— active' : '— production mode only';
  send({ mode });
  applyFx();
}

async function play() {
  await start();
  await ctx.resume();
  playing = true; send({ playing: true }); $('#play').textContent = '❚❚';
}
function pause() { playing = false; send({ playing: false }); $('#play').textContent = '▶'; }
function seek(tick) {
  pos = { tick: Math.max(0, Math.min(S.meta.ticks - 1, tick | 0)), inTick: 0 };
  if (node) node.port.postMessage({ type: 'seek', tick: pos.tick });
}

/* --------------------------------------------------------------- events --- */
function wire() {
  $('#play').onclick = () => playing ? pause() : play();
  $('#stop').onclick = () => { pause(); seek(0); };

  const sk = $('#seek');
  const to = e => { const r = sk.getBoundingClientRect();
    seek(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * S.meta.ticks); };
  sk.addEventListener('pointerdown', e => { sk.setPointerCapture(e.pointerId); to(e); });
  sk.addEventListener('pointermove', e => { if (sk.hasPointerCapture(e.pointerId)) to(e); });
  sk.addEventListener('keydown', e => {
    const d = { ArrowLeft:-40, ArrowRight:40, ArrowDown:-200, ArrowUp:200 }[e.key];
    if (d) { e.preventDefault(); seek(pos.tick + d); } });

  $('#patternview').addEventListener('click', e => {
    const r = $('#patternview').getBoundingClientRect();
    const rh = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--rowh'));
    const target = tickToRow[pos.tick] + Math.round((e.clientY - r.top - r.height / 2) / rh);
    if (rows[target]) seek(rows[target].t);
  });

  $('#tps').oninput = e => {
    tps = +e.target.value; engineRate = S.meta.cpu / tps;
    $('#tpsv').textContent = tps;
    $('#srv').textContent = Math.round(engineRate);
    $('#dur').textContent = '/ ' + fmt(S.meta.ticks * 216 / engineRate);
    send({ tps });
  };
  $('#clip').onclick = e => { const on = e.target.getAttribute('aria-pressed') !== 'true';
    e.target.setAttribute('aria-pressed', on); send({ clip: on }); };
  $('#alias').onclick = e => { const on = e.target.getAttribute('aria-pressed') !== 'true';
    e.target.setAttribute('aria-pressed', on); send({ keepAlias: on }); };

  for (const id of ['reverb','width','drive','tone']) {
    $('#' + id).oninput = () => {
      const v = +$('#' + id).value;
      $('#' + id + 'v').textContent = id === 'tone' ? (v > 0 ? '+' : '') + v + ' dB' : v + '%';
      applyFx();
    };
  }

  addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT') return;
    if (e.code === 'Space') { e.preventDefault(); playing ? pause() : play(); }
    else if (e.key >= '1' && e.key <= '5') setMode(MODES[+e.key - 1].id);
    else if (e.key >= '6' && e.key <= '9') { const i = +e.key - 6;
      mute[i] = mute[i] ? 0 : 1;
      document.querySelectorAll('.chan').forEach((el, k) => el.dataset.mute = mute[k]);
      send({ mute }); }
  });
  addEventListener('resize', () => { sizeRaster(); sizeVU(); });
}

/* -------------------------------------------------------------- drawing --- */
function cellHTML(c) {
  if (c[1] < 0 && c[0] < 0) return '<span class="dim">··· ····</span>';
  const n = c[0] >= 0 ? `<span class="note">${noteName(c[0])}</span>` : '<span class="dim">···</span>';
  const i = c[1] >= 0 ? S.inst[c[1]].n : '<span class="dim">····</span>';
  const f = c[2] ? `<span class="fx">${c[2].toString(16).toUpperCase()}</span>` : '';
  return `${n} ${i} ${f}`;
}

function drawRows(center) {
  for (let i = 0; i < WINDOW_ROWS; i++) {
    const idx = center - HALF + i, R = rowEls[i];
    if (idx < 0 || idx >= rows.length) {
      R.el.className = 'row'; R.n.textContent = '';
      R.c.forEach(c => c.innerHTML = ''); continue;
    }
    const row = rows[idx];
    R.el.className = 'row' + (idx % 16 === 0 ? ' bar' : (idx % 4 === 0 ? ' beat' : ''));
    R.n.textContent = pad(idx.toString(16).toUpperCase(), 3);
    for (let ch = 0; ch < 6; ch++) R.c[ch].innerHTML = cellHTML(row.c[ch]);
  }
  $('#rownum').textContent = pad(center.toString(16).toUpperCase(), 3);
  $('#secname').textContent = rows[center].s || '—';
  drawPhonemes(center);
}

function drawPhonemes(center) {
  const from = Math.max(0, rows[center].t - 26), to = Math.min(S.meta.ticks, rows[center].t + 6);
  let out = '', prev = -2, shown = 0;
  for (let t = from; t < to; t++) {
    const id = W.wpage[t * 5 + 1];
    if (id === prev) continue;
    prev = id;
    if (id < 0) { out += ' · '; continue; }
    const nm = S.inst[id].n;
    if (!nm.startsWith('PH_')) continue;
    const ch = nm.slice(3).toLowerCase();
    out += (t <= pos.tick && t + 6 > pos.tick) ? `<b>${ch}</b>` : ch;
    shown += ch.length;
  }
  $('#phon').innerHTML = out || '<span style="color:#3b414d">—</span>';
}

function drawScope(ch, id) {
  const c = scopeCtx[ch], Wd = 200, H = 26;
  c.clearRect(0, 0, Wd, H);
  document.getElementById('sc' + ch).textContent =
    id >= 0 ? S.meta.chan[ch] + ' · ' + S.inst[id].n : S.meta.chan[ch];
  if (id < 0) return;
  const drum = id >= S.meta.drumInst0;
  const src = drum ? W.drums : W.waves;
  const off = drum ? (id - S.meta.drumInst0) * 1280 : id * 256;
  const len = drum ? 1280 : 256;
  c.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue(CHCOL[ch]).trim();
  c.lineWidth = 1; c.beginPath();
  for (let x = 0; x < Wd; x++) {
    const y = H / 2 - src[off + ((x * len / Wd) | 0)] * (H / 2 - 1) / 64;
    x ? c.lineTo(x + .5, y) : c.moveTo(x + .5, y);
  }
  c.stroke();
}

/* ---- VU meters drawn over the pattern, one per channel column ------------- */
const vuc = $('#vu').getContext('2d');
let vuW = 0, vuH = 0, vuGrad = null;
function sizeVU() {
  const el = $('#vu'), d = Math.min(2, devicePixelRatio || 1);
  vuW = el.clientWidth; vuH = el.clientHeight;
  el.width = vuW * d; el.height = vuH * d; vuc.setTransform(d, 0, 0, d, 0, 0);
  const g = vuc.createLinearGradient(0, vuH / 2, 0, vuH * 0.06);
  g.addColorStop(0.00, '#1f9c3a'); g.addColorStop(0.45, '#8ad63c');
  g.addColorStop(0.68, '#ffe23c'); g.addColorStop(0.86, '#ff9a20');
  g.addColorStop(1.00, '#ff3b30');
  vuGrad = g;
}
function drawVU() {
  if (!vuW) sizeVU();
  vuc.clearRect(0, 0, vuW, vuH);
  const colW = (vuW - 44) / 6, bw = Math.min(26, colW * 0.30);
  const base = vuH / 2, maxH = vuH * 0.44;
  vuc.globalAlpha = 0.85;
  for (let i = 0; i < 6; i++) {
    const h = vu[i] * maxH;
    if (h < 1) continue;
    const x = 44 + colW * (i + 0.5) - bw / 2;
    vuc.fillStyle = vuGrad;
    vuc.fillRect(x, base - h, bw, h);
    vuc.fillStyle = '#fff';
    vuc.fillRect(x, base - h, bw, 1.5);            // bright cap
  }
  vuc.globalAlpha = 1;
}

/* ------------------------------------------------------------- rasters ---- */
const rc = $('#raster').getContext('2d');
let rw = 0, rh = 0;
function sizeRaster() {
  const c = $('#raster'), d = Math.min(2, devicePixelRatio || 1);
  rw = c.clientWidth; rh = c.clientHeight;
  c.width = rw * d; c.height = rh * d; rc.setTransform(d, 0, 0, d, 0, 0);
}
function drawRaster(t, energy) {
  if (!rw) sizeRaster();
  rc.fillStyle = '#05060a'; rc.fillRect(0, 0, rw, rh);
  const bars = 7, amp = rh * 0.30 * (0.55 + energy * 0.7);
  for (let b = 0; b < bars; b++) {
    const ph = b * 0.9;
    const y = rh / 2 + Math.sin(t * 0.8 + ph) * amp + Math.sin(t * 1.7 + ph * 2.1) * amp * 0.35;
    const hue = (b * 34 + t * 22) % 360, hgt = 8 + 4 * Math.sin(t * 2 + b);
    for (let i = -hgt; i <= hgt; i++) {
      rc.fillStyle = `hsl(${hue} 92% ${12 + (1 - Math.abs(i) / hgt) * 52}%)`;
      rc.fillRect(0, Math.round(y + i), rw, 1);
    }
  }
  rc.globalAlpha = 0.2; rc.fillStyle = '#000';
  for (let y = 0; y < rh; y += 3) rc.fillRect(0, y, rw, 1);
  rc.globalAlpha = 1;
}

/* ----------------------------------------------------------- frame loop --- */
function loop(ts) {
  const tick = Math.max(0, Math.min(S.meta.ticks - 1, pos.tick));
  const row = tickToRow[tick], R = rows[row];
  if (row !== lastRow) { drawRows(row); lastRow = row; }

  const frac = Math.min(1, ((tick - R.t) + pos.inTick / 216) / R.n);
  const rh2 = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--rowh'));
  $('#rows').style.transform = `translateY(${-(HALF + frac) * rh2}px)`;

  const secs = (tick * 216 + pos.inTick) / engineRate;
  $('#time').textContent = fmt(secs);
  $('#ticknum').textContent = tick;
  const p = tick / S.meta.ticks;
  $('#seekfill').style.width = (p * 100) + '%';
  $('#seekhead').style.left = (p * 100) + '%';

  const att = [1, .70, .40, .25, .117, .047][W.vol[tick]] ?? 1;
  let energy = 0;
  for (let v = 0; v < 6; v++) {
    const id = v < 5 ? W.wpage[tick * 5 + v] : W.dpage[tick];
    const target = (id >= 0 && !mute[v] && playing) ? Math.min(1, S.inst[id].pk / 52 * att) : 0;
    vu[v] = Math.max(target, vu[v] * 0.90);
    energy += vu[v] / 6;
    lvlEls[v].style.width = (vu[v] * 100) + '%';
    if (id !== lastInst[v]) { drawScope(v, id); lastInst[v] = id; }
  }
  drawVU();
  drawRaster(ts / 1000, playing ? energy : 0.15);
  requestAnimationFrame(loop);
}
