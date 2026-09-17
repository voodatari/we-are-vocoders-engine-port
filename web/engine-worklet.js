/* ---------------------------------------------------------------------------
   engine-worklet.js — The ZX Spectrum beeper engine, running live in the browser.

   This is the same algorithm as the offline Python renderer, re-implemented as
   an AudioWorkletProcessor so that channels can be muted and the synthesis mode
   switched while the music plays.

   Timing
   ------
   The engine produces one sample every TPS T-states (423 by default), i.e.
   8274.23 Hz, which is unrelated to the browser's sample rate. Engine samples
   are generated on demand into a ring buffer and interpolated to the output
   rate with a 24-tap / 64-phase polyphase FIR built at startup.

   Modes
   -----
   exact      every voice read nearest-neighbour from its 256-byte table (so the
              original foldover survives), 8-bit accumulator that wraps, clip to
              ±64, global volume. The engine, sample for sample.
   beeper     same levels, but the 1-bit pulse train is reconstructed: each
              output sample integrates the exact pulse areas analytically, which
              is a closed-form box-filtered reconstruction (no oversampling).
   antialias  band-limited oscillators at the engine rate, same clipping.
   remaster   band-limited oscillators at the OUTPUT rate, floating point.
   production remaster + per-voice panning (effects live in the audio graph).

   In antialias/remaster/production, any voice whose fundamental is above
   sampleRate/8 keeps the nearest-neighbour path: that foldover is the
   ring-modulator effect the author actually composed with.
   --------------------------------------------------------------------------- */

const FRAME_LEN = 216;          // engine samples per tick
const FRAMEPAD = 40;            // drum sample reads bytes 40..255 of each page
const ATT = [1, 179 / 256, 102 / 256, 64 / 256, 30 / 256, 12 / 256];
const P1MIN = 18, P1TOT = 212, P2MIN = 22;   // pulse windows, in T-states

const NTAPS = 24, NPHASES = 64, RING = 128;
const PAN = [0.0, -0.5, 0.5, 0.35, -0.35];   // voices 2/3 and 4/5 are detuned pairs
const MIP_H = [128, 91, 64, 45, 32, 23, 16, 11, 8, 6, 4, 3, 2, 1];
const MIP_LEN = 1024;

// Output gain per mode so that every version sits at the same loudness.
// Measured from the offline renders: peak-normalising would leave 8 dB between
// them, and in an A/B test that reads as quality instead of level.
const MODE_GAIN = { exact: 0.485, beeper: 0.371, antialias: 0.485,
                    remaster: 0.612, production: 0.612 };

class VocodersEngine extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const d = options.processorOptions;
    this.waves = new Int8Array(d.waves);        // nInst * 256
    this.drums = new Int8Array(d.drums);        // 2 * 1280
    this.mips = new Float32Array(d.mips);       // nInst * MIP_H.length * MIP_LEN
    this.wpage = new Int8Array(d.wpage);
    this.freq = new Uint16Array(d.freq);
    this.dpage = new Int8Array(d.dpage);
    this.doff = new Uint8Array(d.doff);
    this.vol = new Uint8Array(d.vol);
    this.nTicks = d.nTicks;
    this.drumInst0 = d.drumInst0;
    this.baseRate = d.sampleRate;               // engine Hz at the nominal TPS
    this.tps = d.tps; this.cpu = d.cpu;

    // ---- state (the initial mode/playing can arrive in processorOptions so an
    // OfflineAudioContext render does not depend on a port message being
    // delivered before the first quantum)
    this.mode = d.mode || 'exact';
    this.mute = [0, 0, 0, 0, 0, 0];
    this.gain = 1;
    this.spread = 1;                            // stereo width multiplier
    this.doClip = 1;
    this.keepAlias = 1;
    this.playing = !!d.playing;
    this.rate = this.baseRate;

    this.phase = new Uint16Array(5);            // 16-bit phase accumulators
    this.fphase = new Float64Array(5);          // fractional phase, output-rate path
    this.tick = 0; this.inTick = 0;
    this.enginePos = 0;                         // fractional engine-sample cursor
    this.written = 0;                           // engine samples generated so far
    this.ring = new Float32Array(RING);
    this.ringL = new Float32Array(RING);        // panned buses for production
    this.ringR = new Float32Array(RING);
    this.levels = new Float32Array(RING);       // engine levels, for beeper mode
    this.dcx = 0; this.dcy = 0;

    this.filt = this.buildFilter();
    this.port.onmessage = e => this.onMessage(e.data);
  }

  /* Windowed-sinc polyphase interpolation filter (engine rate -> output rate) */
  buildFilter() {
    const f = new Float32Array(NPHASES * NTAPS);
    const fc = 0.46;                            // of the engine sample rate
    for (let p = 0; p < NPHASES; p++) {
      const frac = p / NPHASES;
      let sum = 0;
      for (let t = 0; t < NTAPS; t++) {
        const x = t - NTAPS / 2 + 1 - frac;
        const s = x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x);
        const w = 0.42 - 0.5 * Math.cos(2 * Math.PI * (t + 0.5) / NTAPS)
                       + 0.08 * Math.cos(4 * Math.PI * (t + 0.5) / NTAPS);
        const v = s * w;
        f[p * NTAPS + t] = v; sum += v;
      }
      for (let t = 0; t < NTAPS; t++) f[p * NTAPS + t] /= sum;   // unity DC gain
    }
    return f;
  }

  onMessage(m) {
    if (m.type === 'set') {
      if (m.mode !== undefined) this.mode = m.mode;
      if (m.mute !== undefined) this.mute = m.mute;
      if (m.gain !== undefined) this.gain = m.gain;
      if (m.spread !== undefined) this.spread = m.spread;
      if (m.clip !== undefined) this.doClip = m.clip ? 1 : 0;
      if (m.keepAlias !== undefined) this.keepAlias = m.keepAlias ? 1 : 0;
      if (m.tps !== undefined) { this.tps = m.tps; this.rate = this.cpu / m.tps; }
      if (m.playing !== undefined) this.playing = m.playing;
    } else if (m.type === 'seek') {
      this.tick = Math.max(0, Math.min(this.nTicks - 1, m.tick | 0));
      this.inTick = 0; this.enginePos = 0; this.written = 0;
      this.ring.fill(0); this.ringL.fill(0); this.ringR.fill(0); this.levels.fill(0);
    }
  }

  /* -------------------------------------------------- one engine sample ---- */
  genEngineSample() {
    if (this.inTick >= FRAME_LEN) { this.inTick = 0; this.tick++; }
    if (this.tick >= this.nTicks) { this.port.postMessage({ type: 'end' }); this.tick = this.nTicks - 1; }
    const tk = this.tick, base5 = tk * 5;
    const att = ATT[this.vol[tk]] * this.gain;
    const mode = this.mode;
    const outRate = (mode === 'remaster' || mode === 'production');
    const aliasTh = this.rate / 8;
    let sum = 0, sumL = 0, sumR = 0;

    for (let v = 0; v < 5; v++) {
      const inst = this.wpage[base5 + v];
      const f16 = this.freq[base5 + v];
      this.phase[v] = (this.phase[v] + f16) & 0xFFFF;
      if (inst < 0 || this.mute[v]) continue;
      const hz = f16 * this.rate / 65536;
      const foldover = this.keepAlias && hz > aliasTh;
      if (outRate && !foldover) continue;        // handled at the output rate
      let s;
      if (mode === 'exact' || foldover) {
        s = this.waves[inst * 256 + (this.phase[v] >> 8)];
      } else {                                   // band-limited, engine rate
        s = this.readMip(inst, hz, this.rate / 2, this.phase[v] / 65536);
      }
      if (outRate) {
        const p = PAN[v] * this.spread;
        sumL += s * att * Math.sqrt((1 - p) / 2);
        sumR += s * att * Math.sqrt((1 + p) / 2);
      } else sum += s;
    }

    const di = this.dpage[tk];
    if (di >= 0 && !this.mute[5]) {
      const d = this.drums[(di - this.drumInst0) * 1280 + this.doff[tk] * 256
                           + FRAMEPAD + this.inTick];
      if (outRate) { sumL += d * att * 0.7071; sumR += d * att * 0.7071; }
      else sum += d;
    }

    let lvl = 0;
    if (!outRate) {
      sum = Math.round(sum);
      sum = ((sum + 128) % 256 + 256) % 256 - 128;     // 8-bit accumulator wrap
      if (this.doClip) sum = sum < -64 ? -64 : (sum > 64 ? 64 : sum);
      lvl = sum;
      sum *= att;
    }

    const w = this.written & (RING - 1);
    this.ring[w] = sum; this.ringL[w] = sumL; this.ringR[w] = sumR;
    this.levels[w] = lvl;
    this.written++; this.inTick++;
  }

  /* ---- band-limited table read (mipmap by fundamental frequency) ---------- */
  readMip(inst, hz, nyq, ph) {
    const maxH = Math.max(1, Math.min(128, Math.floor(nyq / Math.max(hz, 1e-6))));
    let m = MIP_H.length - 1;
    while (m > 0 && MIP_H[m] < maxH) m--;              // brightest mip that fits
    const off = (inst * MIP_H.length + m) * MIP_LEN;
    const x = ph * MIP_LEN, i0 = x | 0, fr = x - i0;
    const a = this.mips[off + i0], b = this.mips[off + ((i0 + 1) & (MIP_LEN - 1))];
    return a + (b - a) * fr;
  }

  /* ---- exact area of the 1-bit pulse train between two engine times ------- */
  pwmArea(t0, t1) {
    let area = 0;
    let k = Math.floor(t0);
    while (k < t1) {
      const lvl = this.levels[((k % RING) + RING) % RING] + 64;
      const w1 = (P1MIN + lvl) / this.tps;
      const s2 = P1TOT / this.tps, w2 = (P2MIN + lvl) / this.tps;
      const a = Math.max(t0, k), b = Math.min(t1, k + 1);
      area += Math.max(0, Math.min(b, k + w1) - Math.max(a, k));
      area += Math.max(0, Math.min(b, k + s2 + w2) - Math.max(a, k + s2));
      k++;
    }
    return area / (t1 - t0);
  }

  /* ------------------------------------------------------------ process --- */
  process(inputs, outputs) {
    const outL = outputs[0][0], outR = outputs[0][1] || outputs[0][0];
    const n = outL.length;
    if (!this.playing) { outL.fill(0); if (outR !== outL) outR.fill(0); return true; }

    const step = this.rate / sampleRate;
    const mode = this.mode;
    const outRate = (mode === 'remaster' || mode === 'production');
    const g = MODE_GAIN[mode] ?? 1;
    const aliasTh = this.rate / 8;
    const nyq = sampleRate / 2;

    for (let i = 0; i < n; i++) {
      const need = Math.floor(this.enginePos) + NTAPS / 2 + 2;
      while (this.written < need) this.genEngineSample();

      let l = 0, r = 0;
      if (mode === 'beeper') {
        const v = this.pwmArea(this.enginePos, this.enginePos + step) - 0.5;
        this.dcy = v - this.dcx + 0.999 * this.dcy; this.dcx = v;   // DC blocker
        l = r = this.dcy;
      } else {
        const base = Math.floor(this.enginePos) - NTAPS / 2 + 1;
        const ph = ((this.enginePos - Math.floor(this.enginePos)) * NPHASES) | 0;
        const fo = ph * NTAPS;
        for (let t = 0; t < NTAPS; t++) {
          const idx = ((base + t) % RING + RING) % RING;
          const c = this.filt[fo + t];
          if (outRate) { l += this.ringL[idx] * c; r += this.ringR[idx] * c; }
          else { l += this.ring[idx] * c; }
        }
        if (!outRate) r = l;
        if (outRate) {                       // band-limited voices, output rate
          const tk = this.tick, b5 = tk * 5;
          const att = ATT[this.vol[tk]] * this.gain;
          for (let v = 0; v < 5; v++) {
            const inst = this.wpage[b5 + v];
            const f16 = this.freq[b5 + v];
            const hz = f16 * this.rate / 65536;
            const inc = hz / sampleRate;
            this.fphase[v] += inc; if (this.fphase[v] >= 1) this.fphase[v] -= 1;
            if (inst < 0 || this.mute[v]) continue;
            if (this.keepAlias && hz > aliasTh) continue;   // already folded above
            const s = this.readMip(inst, hz, nyq, this.fphase[v]) * att;
            const p = PAN[v] * this.spread;
            l += s * Math.sqrt((1 - p) / 2);
            r += s * Math.sqrt((1 + p) / 2);
          }
        }
      }
      // the beeper path is already in ±0.5; the others are in engine units (±64)
      const scale = mode === 'beeper' ? g : g / 64;
      outL[i] = l * scale;
      if (outR !== outL) outR[i] = r * scale;
      this.enginePos += step;
    }

    // Keep the cursor small. The step must be a whole number of RING slots so
    // that every modular index keeps pointing at the same sample.
    if (this.enginePos > 1e7) {
      const k = Math.floor(this.enginePos / RING) * RING;
      this.enginePos -= k; this.written -= k;
    }
    // report the playhead ~every 12 ms (one message per block would be 344/s)
    if ((this.blocks = (this.blocks | 0) + 1) % 4 === 0)
      this.port.postMessage({ type: 'pos', tick: this.tick, inTick: this.inTick });
    return true;
  }
}

registerProcessor('vocoders-engine', VocodersEngine);
