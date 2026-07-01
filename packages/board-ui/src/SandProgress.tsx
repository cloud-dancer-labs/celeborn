"use client";

import { useEffect, useRef } from "react";

/**
 * Sand-fill progress bar for In-Progress cards (CELE-t106). A canvas grain simulation:
 * sand pours from a spout that tracks the fill level, heaps, tumbles, and avalanches along an
 * angle-of-repose slope that eases toward the toe — so the leading edge spills into a gentle concave
 * curve, not a flat cliff. Color tracks the card's status. Respects prefers-reduced-motion.
 *
 * Ported from the design handoff prototype (design/t106/index.html). This is the CANONICAL shared
 * copy (CELE-t98 / t112) consumed by both the local board (board/) and the hosted board (web/) via
 * @celeborn/board-ui — edit here once and both pick it up (transpilePackages, no build step).
 */
// "blocked" (red) was retired with the Blocked column (CELE-t135). "solving" (yellow) stays — a
// DOING card with unresolved blocked_by dependencies still pours yellow; "moving" (green) otherwise.
export type SandStatus = "moving" | "solving";

const COLORS: Record<SandStatus, { base: [number, number, number]; hi: [number, number, number] }> = {
  moving: { base: [88, 176, 108], hi: [199, 247, 205] },
  solving: { base: [214, 179, 47], hi: [255, 232, 138] },
};

const GRAINW = 2, DEP = 1.15, MAXDIFF = 0.95, GRAV = 0.55, SPAWN = 7, RELAX = 4, TUMBLE = 1.1, CONCAVE = 0.6;

// --- Quote watermark (CELE-t108) ---------------------------------------------------------------
// The sand quietly spells a line of Alan Watts — not as type laid on top, but as the sand itself. The
// letters are painted in the *same* color as the sand, so they read only by flattening the per-column
// grain texture where they fall (smooth letterforms standing out from the surrounding grain), sized to
// span the full bar height (glyph bottom = bar bottom, glyph top = bar top). Composited source-atop so
// glyphs live *inside* the poured sand and never on the empty vessel; left-aligned so words surface as
// the pour front advances under them. Each card picks one quote (stable by display_id); a quote too
// long for one bar-width is split into lines, and the line shown advances with the fill fraction — so
// subsequent pours reveal the next words. Tune WM_* to taste.
const WM_ALPHA = 1, WM_MARGIN = 4;
// Eroded all-caps face (Eater, SIL OFL) — bundled at public/fonts, declared @font-face in globals.css.
// The quote is upper-cased before render. Canvas can't use it until the webfont is actually loaded, so
// the Sim waits on document.fonts before rendering the watermark (sand still draws meanwhile).
const WM_FAMILY = "Eater";

// Curated short lines (bundled, not fetched: Celeborn's core stays network-free and deterministic —
// a runtime download of "100 quotes" would be fragile and offline-hostile). Add freely; keep them short.
const QUOTES = [
  "You are an aperture through which the universe looks at itself.",
  "The only way to make sense out of change is to plunge into it.",
  "This is the real secret of life — to be completely engaged with what you are doing in the here and now.",
  "Trying to define yourself is like trying to bite your own teeth.",
  "Muddy water is best cleared by leaving it alone.",
  "The meaning of life is just to be alive. It is so plain and so obvious and so simple.",
  "No valid plans for the future can be made by those who have no capacity for living now.",
  "We do not come into this world; we come out of it, as leaves from a tree.",
  "You are a function of what the whole universe is doing in the same way that a wave is a function of what the whole ocean is doing.",
  "The more a thing tends to be permanent, the more it tends to be lifeless.",
  "Things are as they are. Looking out into the universe at night, we make no comparisons between right and wrong stars.",
  "To have faith is to trust yourself to the water.",
  "Tomorrow and plans for tomorrow can have no significance at all unless you are in full contact with the reality of the present.",
  "The art of living is neither careless drifting nor fearful clinging.",
  "A scholar tries to learn something everyday; a student of Buddhism tries to unlearn something daily.",
  "Man suffers only because he takes seriously what the gods made for fun.",
  "Wisdom does not look for anything; it has found.",
  "When we attempt to exercise power or control over someone else, we cannot avoid giving that person the very same power over us.",
];

function pickQuote(seed: string): string {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i); h = Math.imul(h, 16777619); }
  return QUOTES[(h >>> 0) % QUOTES.length];
}

class Sim {
  canvas: HTMLCanvasElement;
  ctx: CanvasRenderingContext2D;
  status: SandStatus;
  reduce: boolean;
  h = new Float32Array(0);
  grains: { x: number; y: number; vy: number; rolls?: number }[] = [];
  pouring = false;
  raf = 0;
  areaTarget = 0;
  spawnRate = SPAWN;
  dpr = 1; W = 0; H = 0; cols = 0; maxArea = 1; spoutCol = 0;
  onResize: () => void;
  // Quote watermark (t108): `lines` is the chosen quote wrapped to the current bar width; `mask` holds
  // the rendered glyphs for the line currently in view. Rebuilt only when width or line index changes.
  quote: string;
  lines: string[] = [];
  linesW = -1;
  mask: HTMLCanvasElement | null = null;
  maskKey = ""; // `${lineIdx}|${status}` the mask was last rendered for
  fontReady = false; // the Eater webfont is loaded → safe to measure/render the watermark

  constructor(canvas: HTMLCanvasElement, status: SandStatus, reduce: boolean, quote: string) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d")!;
    this.status = status;
    this.reduce = reduce;
    this.quote = quote;
    this.onResize = () => this.resize();
    addEventListener("resize", this.onResize);
    this.resize();
    // Load the eroded webfont, then re-render the watermark with real metrics. On failure, mark ready
    // anyway so the watermark still renders in the fallback face rather than never appearing.
    const done = () => { this.fontReady = true; this.linesW = -1; this.maskKey = ""; this.draw(); };
    try { document.fonts.load(`16px "${WM_FAMILY}"`).then(done, done); } catch { done(); }
  }
  destroy() { cancelAnimationFrame(this.raf); removeEventListener("resize", this.onResize); }
  resize() {
    const r = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(2, devicePixelRatio || 1);
    this.W = Math.max(40, Math.round(r.width));
    this.H = Math.max(8, Math.round(r.height));
    this.canvas.width = this.W * this.dpr;
    this.canvas.height = this.H * this.dpr;
    this.cols = Math.floor(this.W / GRAINW);
    if (this.h.length !== this.cols) this.h = new Float32Array(this.cols);
    this.maxArea = this.cols * this.H;
    this.linesW = -1; this.maskKey = ""; // width changed → rewrap + re-render the watermark
    this.draw();
  }
  color() { return COLORS[this.status]; }
  area() { let s = 0; for (let i = 0; i < this.cols; i++) s += this.h[i]; return s; }
  setStatus(s: SandStatus) { this.status = s; this.draw(); }

  // Watermark font is tied to the bar height so glyphs reach full height. Eater first; sans fallback.
  font() { return `${this.H}px "${WM_FAMILY}", ui-sans-serif, system-ui, sans-serif`; }
  // Greedily wrap the (upper-cased) quote into lines that fit the current bar width (minus margins).
  // Cheap; only re-run when the width changes (guarded by linesW).
  buildLines() {
    const ctx = this.ctx, avail = this.W - WM_MARGIN * 2;
    ctx.font = this.font();
    const words = this.quote.toUpperCase().split(/\s+/).filter(Boolean);
    const lines: string[] = [];
    let cur = "";
    for (const w of words) {
      const trial = cur ? cur + " " + w : w;
      if (cur && ctx.measureText(trial).width > avail) { lines.push(cur); cur = w; }
      else cur = trial;
    }
    if (cur) lines.push(cur);
    this.lines = lines.length ? lines : [""];
    this.linesW = this.W;
  }
  // Which line of the quote is on display: the fill fraction walks the quote so each subsequent pour
  // reveals the next words. Tied to areaTarget (the committed value), not the live heap, so it never
  // flickers while grains settle.
  lineIdx() {
    const n = this.lines.length;
    if (n <= 1) return 0;
    return Math.max(0, Math.min(n - 1, Math.floor((this.areaTarget / this.maxArea) * n)));
  }
  // Letter color: exactly the sand's base hue — the glyphs aren't a different shade, they read purely as
  // smooth (un-textured) sand against the grainy surround.
  letterColor() { const b = this.color().base; return `rgb(${b[0]},${b[1]},${b[2]})`; }
  // Render the current line into an offscreen mask, left-aligned so words surface as the pour front
  // advances. The line's ink is vertically stretched to span the full bar height (bottom of glyphs →
  // bar bottom, top → bar top), measured per-line from its own glyph metrics. Device-res for crispness.
  buildMask(idx: number) {
    const off = this.mask ?? (this.mask = document.createElement("canvas"));
    off.width = this.W * this.dpr; off.height = this.H * this.dpr;
    const m = off.getContext("2d")!;
    m.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    m.clearRect(0, 0, this.W, this.H);
    const line = this.lines[idx] ?? "";
    m.font = this.font();
    m.textAlign = "left";
    m.textBaseline = "alphabetic";
    const tm = m.measureText(line || "X");
    const asc = tm.actualBoundingBoxAscent, desc = tm.actualBoundingBoxDescent;
    const scaleY = this.H / Math.max(1, asc + desc); // stretch the ink box to exactly fill [0, H]
    m.save();
    m.translate(WM_MARGIN, 0);
    m.scale(1, scaleY);
    m.fillStyle = this.letterColor();
    m.fillText(line, 0, asc); // baseline at `asc` → ink top lands at y=0, ink bottom at y=H
    m.restore();
    this.maskKey = idx + "|" + this.status;
  }

  pourTo(pct: number) {
    const target = (Math.max(0, Math.min(100, pct)) / 100) * this.maxArea;
    if (target < this.area() - DEP) { this.h.fill(0); this.grains.length = 0; }
    this.areaTarget = target;
    if (this.reduce) { this.fillInstant(); return; }
    this.pouring = true;
    this.start();
  }
  fillInstant() {
    const cols = Math.round(this.areaTarget / this.H);
    this.h.fill(0);
    for (let i = 0; i < Math.min(cols, this.cols); i++) this.h[i] = this.H;
    this.draw();
  }
  start() { if (!this.raf) this.raf = requestAnimationFrame(() => this.step()); }

  spawn(n: number) {
    const edge = this.area() / this.H;
    const targetCol = this.areaTarget / this.H;
    const spout = Math.max(0, Math.min(this.cols - 1, Math.min(edge + 1.5, targetCol)));
    this.spoutCol = spout;
    for (let i = 0; i < n; i++) {
      const col = Math.max(0, Math.min(this.cols - 1, Math.round(spout + (Math.random() - 0.5) * 5)));
      this.grains.push({ x: col * GRAINW + Math.random() * GRAINW, y: -2 - Math.random() * 6, vy: 0.6 + Math.random() * 0.9 });
    }
  }
  relax(passes: number) {
    let movedAny = false;
    for (let p = 0; p < passes; p++) {
      let moved = false;
      const fwd = p % 2 === 0;
      for (let j = 0; j < this.cols - 1; j++) {
        const i = fwd ? j : this.cols - 2 - j;
        const diff = this.h[i] - this.h[i + 1];
        const ratio = ((this.h[i] + this.h[i + 1]) * 0.5) / this.H;
        const allow = MAXDIFF * (CONCAVE + (1 - CONCAVE) * ratio);
        const f = 0.4 + Math.random() * 0.25;
        if (diff > allow) { const m = (diff - allow) * f; this.h[i] -= m; this.h[i + 1] += m; moved = true; }
        else if (diff < -allow) { const m = (-diff - allow) * f; this.h[i] += m; this.h[i + 1] -= m; moved = true; }
      }
      if (moved) movedAny = true; else break;
    }
    for (let i = 0; i < this.cols; i++) if (this.h[i] > this.H) this.h[i] = this.H;
    return movedAny;
  }
  step() {
    this.raf = 0;
    if (this.pouring && this.area() < this.areaTarget) this.spawn(this.spawnRate);
    else this.pouring = false;
    const live: typeof this.grains = [];
    for (const g of this.grains) {
      g.vy += GRAV; g.y += g.vy;
      const col = Math.max(0, Math.min(this.cols - 1, Math.floor(g.x / GRAINW)));
      const surf = this.H - this.h[col];
      if (g.y >= surf) {
        if (col < this.cols - 1 && this.h[col] - this.h[col + 1] > TUMBLE && (g.rolls || 0) < 9 && Math.random() < 0.72) {
          g.rolls = (g.rolls || 0) + 1;
          g.x += GRAINW * (0.7 + Math.random() * 0.7);
          g.y = this.H - this.h[col] - 1.2; g.vy = 0.35;
          live.push(g);
        } else this.h[col] += DEP;
      } else live.push(g);
    }
    this.grains = live;
    const settling = this.relax(RELAX);
    this.draw();
    if (this.pouring || this.grains.length || settling) this.start();
  }
  draw() {
    const { ctx, dpr, W, H, cols } = this;
    if (!cols) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const c = this.color();
    // Textured sand: each column a hashed grain shade, plus a bright surface line along the top edge.
    // The same-color watermark below reads by flattening this texture where the letters fall.
    for (let i = 0; i < cols; i++) {
      const hgt = this.h[i];
      if (hgt <= 0) continue;
      const x = i * GRAINW, top = H - hgt;
      const n = (((i * 2654435761) >>> 0) % 100) / 100;
      const k = 0.82 + n * 0.3;
      ctx.fillStyle = `rgb(${(c.base[0] * k) | 0},${(c.base[1] * k) | 0},${(c.base[2] * k) | 0})`;
      ctx.fillRect(x, top, GRAINW + 0.5, hgt);
      ctx.fillStyle = `rgba(${c.hi[0]},${c.hi[1]},${c.hi[2]},0.85)`;
      ctx.fillRect(x, top, GRAINW + 0.5, Math.min(1.6, hgt));
    }
    // Quote watermark (t108): composite the current line over the poured sand only. source-atop keeps
    // glyphs inside the heap (never on the empty vessel), so words reveal as the sand front advances.
    if (this.W > 0 && this.fontReady) {
      if (this.linesW !== this.W) this.buildLines();
      const idx = this.lineIdx();
      if (this.maskKey !== idx + "|" + this.status || !this.mask) this.buildMask(idx);
      if (this.mask) {
        ctx.save();
        ctx.globalCompositeOperation = "source-atop";
        ctx.globalAlpha = WM_ALPHA;
        ctx.drawImage(this.mask, 0, 0, W, H);
        ctx.restore();
      }
    }
    if (!this.reduce) {
      ctx.fillStyle = `rgb(${c.hi[0]},${c.hi[1]},${c.hi[2]})`;
      for (const g of this.grains) ctx.fillRect(g.x, g.y, 1.8, 2.2);
    }
  }
}

export function SandProgress({ value, status, seed }: { value: number; status: SandStatus; seed?: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const simRef = useRef<Sim | null>(null);
  const c = COLORS[status];
  const pctColor = `rgb(${c.hi[0]},${c.hi[1]},${c.hi[2]})`;

  useEffect(() => {
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const sim = new Sim(canvasRef.current!, status, reduce, pickQuote(seed || "celeborn"));
    simRef.current = sim;
    sim.pourTo(value);
    return () => { sim.destroy(); simRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { simRef.current?.pourTo(value); }, [value]);
  useEffect(() => { simRef.current?.setStatus(status); }, [status]);

  return (
    <div className="card-progress" title={`${value}% complete`}>
      <div className="sand-vessel">
        <canvas ref={canvasRef} />
      </div>
      <span className="sand-pct" style={{ color: pctColor }}>{value}%</span>
    </div>
  );
}
