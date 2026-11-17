// Editor de plano: ver, medir y corregir la geometría analizada de una pieza.
// Sin dependencias. El servidor valida, reclasifica y recalcula métricas y comprobaciones de fabricabilidad.
(() => {
  "use strict";
  const app = document.getElementById("ed-app");
  if (!app) return;

  const PART = app.dataset.part;
  const LOCKED = app.dataset.locked === "1";
  const $ = (id) => document.getElementById(id);
  const canvas = $("ed-canvas");
  const ctx = canvas.getContext("2d");
  const fmt = (v, d = 2) => (v == null || !isFinite(v) ? "—" :
    Number(v).toLocaleString("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ARC_TOL = 0.01;
  const PX_PER_MM = 96 / 25.4;   // 100 % ≈ tamaño real en pantalla          // mm, igual que el análisis del DXF

  // ------------------------------------------------------------------ estado
  /** contornos: {pts: [[x,y],...], closed: bool, kind: string}. Cerrado ⇒ último punto = primero. */
  let C = [];
  let boxes = [];                 // envolventes por contorno [minx, miny, maxx, maxy]
  let sel = new Set();
  let issues = [];
  let metrics = null, baseMetrics = null;
  let tool = "select";
  const show = { grid: true, dirs: false, nums: false, pierce: true, nodes: false, dims: false };
  const view = { cx: 0, cy: 0, s: 1 };   // centro en mm y escala px/mm
  let undo = [], redo = [];
  let dirty = false, saving = false;
  let hover = -1;
  let measure = null;             // {a:[x,y], b:[x,y]|null}
  let snapPt = null;
  let drag = null;                // estado de arrastre del puntero
  let analysisSeq = 0, analysisTimer = null;
  let colors = {};

  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n, d) => (cs.getPropertyValue(n).trim() || d);
    colors = {
      bg: v("--surface", "#fff"), grid: v("--border", "#e2e5ea"), text: v("--muted", "#5f6b7a"),
      exterior: v("--cut-outer", "#0f6fde"), interior: v("--cut-inner", "#d6336c"), abierto: v("--cut-open", "#e08a00"),
      pierce: v("--pierce", "#1b9e5a"), sel: v("--primary", "#0f6fde"), err: v("--danger", "#c62828"), warn: v("--warn", "#a15c00"),
      ink: v("--text", "#16191f"), mark: "#8b5cf6",
    };
  }
  readColors();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => { readColors(); draw(); });

  // ------------------------------------------------------------------ geometría básica
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const clonePts = (pts) => pts.map((p) => [p[0], p[1]]);
  const lengthOf = (pts) => { let s = 0; for (let i = 1; i < pts.length; i++) s += dist(pts[i - 1], pts[i]); return s; };
  function bboxOf(pts) {
    let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
    for (const [x, y] of pts) { if (x < a) a = x; if (y < b) b = y; if (x > c) c = x; if (y > d) d = y; }
    return [a, b, c, d];
  }
  function areaOf(pts) {        // con signo (antihorario > 0)
    let s = 0; for (let i = 1; i < pts.length; i++) s += pts[i - 1][0] * pts[i][1] - pts[i][0] * pts[i - 1][1];
    return s / 2;
  }
  function circleOf(c) {        // detecta círculos (vértices equidistantes del centro)
    if (!c.closed || c.pts.length < 9) return null;
    const n = c.pts.length - 1;
    let cx = 0, cy = 0;
    for (let i = 0; i < n; i++) { cx += c.pts[i][0]; cy += c.pts[i][1]; }
    cx /= n; cy /= n;
    let r = 0; for (let i = 0; i < n; i++) r += dist(c.pts[i], [cx, cy]); r /= n;
    if (r <= 0) return null;
    for (let i = 0; i < n; i++) if (Math.abs(dist(c.pts[i], [cx, cy]) - r) > Math.max(0.02, r * 0.003)) return null;
    return { cx, cy, r };
  }
  function segDist(p, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const l2 = dx * dx + dy * dy;
    let t = l2 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2 : 0;
    t = Math.max(0, Math.min(1, t));
    const q = [a[0] + t * dx, a[1] + t * dy];
    return { d: dist(p, q), q, t };
  }
  function arcSegments(r, angle) {
    if (r <= 0) return 1;
    const step = 2 * Math.acos(Math.max(-1, 1 - ARC_TOL / r)) || angle;
    return Math.min(180, Math.max(4, Math.ceil(Math.abs(angle) / step)));
  }
  function circlePts(cx, cy, r) {
    const n = Math.max(16, arcSegments(r, 2 * Math.PI));
    const out = [];
    for (let i = 0; i < n; i++) { const a = (2 * Math.PI * i) / n; out.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]); }
    out.push([...out[0]]);
    return out;
  }
  function roundRectPts(x, y, w, h, r) {
    r = Math.max(0, Math.min(r, w / 2, h / 2));
    if (r < 1e-6) return [[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]];
    const out = [];
    const corners = [[x + w - r, y + r, -Math.PI / 2], [x + w - r, y + h - r, 0], [x + r, y + h - r, Math.PI / 2], [x + r, y + r, Math.PI]];
    const n = arcSegments(r, Math.PI / 2);
    for (const [cx, cy, a0] of corners) {
      for (let i = 0; i <= n; i++) { const a = a0 + (Math.PI / 2) * (i / n); out.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]); }
    }
    // quitar duplicados de tramos rectos nulos (coliso con r = h/2)
    const clean = [out[0]];
    for (const p of out.slice(1)) if (dist(p, clean[clean.length - 1]) > 1e-6) clean.push(p);
    clean.push([...clean[0]]);
    return clean;
  }
  function simplifyDP(pts, tol) {
    if (pts.length < 3) return pts;
    const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
    const stack = [[0, pts.length - 1]];
    while (stack.length) {
      const [a, b] = stack.pop();
      let maxD = -1, idx = -1;
      for (let i = a + 1; i < b; i++) { const d = segDist(pts[i], pts[a], pts[b]).d; if (d > maxD) { maxD = d; idx = i; } }
      if (maxD > tol && idx > 0) { keep[idx] = 1; stack.push([a, idx], [idx, b]); }
    }
    return pts.filter((_, i) => keep[i]);
  }
  function transformPts(pts, f) { return pts.map((p) => f(p)); }

  function refreshBoxes() { boxes = C.map((c) => bboxOf(c.pts)); }
  function allBox(indices) {
    const idx = indices ?? C.map((_, i) => i);
    let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
    for (const i of idx) { const bb = boxes[i]; if (!bb) continue; a = Math.min(a, bb[0]); b = Math.min(b, bb[1]); c = Math.max(c, bb[2]); d = Math.max(d, bb[3]); }
    return isFinite(a) ? [a, b, c, d] : [0, 0, 100, 100];
  }

  // ------------------------------------------------------------------ vista
  let W = 0, H = 0, dpr = 1;
  let autoFit = true;      // hasta que el usuario mueva o amplíe la vista, se reajusta al cambiar el tamaño
  function resize() {
    const r = canvas.getBoundingClientRect();
    dpr = window.devicePixelRatio || 1;
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    if (autoFit && C.length) fit(); else draw();
  }
  const toScreen = (x, y) => [(x - view.cx) * view.s + W / 2, H / 2 - (y - view.cy) * view.s];
  const toWorld = (sx, sy) => [(sx - W / 2) / view.s + view.cx, (H / 2 - sy) / view.s + view.cy];
  canvas.edToScreen = (x, y) => toScreen(x, y);   // para pruebas automáticas
  function fit(indices) {
    const [a, b, c, d] = allBox(indices);
    const w = Math.max(c - a, 1), h = Math.max(d - b, 1);
    view.cx = (a + c) / 2; view.cy = (b + d) / 2;
    view.s = Math.min((W - 40) / w, (H - 40) / h);
    if (!isFinite(view.s) || view.s <= 0) view.s = 1;
    if (indices) view.s = Math.min(view.s, 40);
    draw();
  }
  function zoomAt(sx, sy, factor) {
    autoFit = false;
    const [wx, wy] = toWorld(sx, sy);
    view.s = Math.max(0.005, Math.min(2000, view.s * factor));
    view.cx = wx - (sx - W / 2) / view.s;
    view.cy = wy + (sy - H / 2) / view.s;
    draw();
  }

  // ------------------------------------------------------------------ dibujo
  let drawQueued = false;
  function draw() {
    if (drawQueued) return;
    drawQueued = true;
    requestAnimationFrame(() => { drawQueued = false; paint(); });
  }
  function niceStep(px) {
    const raw = px / view.s; const p = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) if (m * p >= raw) return m * p;
    return 10 * p;
  }
  function paint() {
    const pct = $("ed-zpct");
    if (pct) { const z = (view.s / PX_PER_MM) * 100; pct.textContent = `${z >= 100 ? Math.round(z) : fmt(z, z < 10 ? 1 : 0)} %`; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = colors.bg; ctx.fillRect(0, 0, W, H);
    if (show.grid) paintGrid();
    const issueSet = new Map();
    for (const it of issues) for (const i of it.contours) if (!issueSet.has(i) || it.severity === "error") issueSet.set(i, it.severity);

    ctx.lineJoin = "round"; ctx.lineCap = "round";
    C.forEach((c, i) => {
      const bb = boxes[i];
      const [x0, y1] = toScreen(bb[0], bb[1]), [x1, y0] = toScreen(bb[2], bb[3]);
      if (x1 < -10 || x0 > W + 10 || y1 < -10 || y0 > H + 10) return;   // fuera de pantalla
      const isSel = sel.has(i);
      ctx.beginPath();
      c.pts.forEach((p, k) => { const [sx, sy] = toScreen(p[0], p[1]); k ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy); });
      if (isSel || i === hover) {
        ctx.strokeStyle = colors.sel; ctx.globalAlpha = isSel ? 0.35 : 0.2; ctx.lineWidth = 7; ctx.stroke(); ctx.globalAlpha = 1;
      }
      const mark = c.layer === "marcado";
      ctx.strokeStyle = mark ? colors.mark : (colors[c.kind] || colors.exterior);
      ctx.lineWidth = isSel ? 2.2 : 1.4;
      if (!c.closed) ctx.setLineDash([6, 4]);
      if (mark) ctx.setLineDash([2, 3]);
      ctx.stroke(); ctx.setLineDash([]);
      if (show.nodes && view.s * (bb[2] - bb[0] + bb[3] - bb[1]) > 30) {
        ctx.fillStyle = ctx.strokeStyle;
        for (const p of c.pts) { const [sx, sy] = toScreen(p[0], p[1]); ctx.fillRect(sx - 1.5, sy - 1.5, 3, 3); }
      }
      if (show.pierce && !mark) {
        const [sx, sy] = toScreen(c.pts[0][0], c.pts[0][1]);
        ctx.fillStyle = colors.pierce; ctx.beginPath(); ctx.arc(sx, sy, isSel ? 4 : 3, 0, 2 * Math.PI); ctx.fill();
      }
      if (show.dirs) paintArrows(c);
      if (!c.closed && !mark) {   // extremos de los abiertos
        const [ex, ey] = toScreen(c.pts[c.pts.length - 1][0], c.pts[c.pts.length - 1][1]);
        ctx.strokeStyle = colors.abierto; ctx.lineWidth = 1.5; ctx.strokeRect(ex - 4, ey - 4, 8, 8);
        const [sx, sy] = toScreen(c.pts[0][0], c.pts[0][1]); ctx.strokeRect(sx - 4, sy - 4, 8, 8);
      }
      if (show.nums || issueSet.has(i)) {
        const [lx, ly] = toScreen((bb[0] + bb[2]) / 2, bb[3]);
        ctx.font = "600 11px system-ui, sans-serif"; ctx.textAlign = "center";
        ctx.fillStyle = issueSet.get(i) === "error" ? colors.err : issueSet.has(i) ? colors.warn : colors.text;
        if (show.nums) ctx.fillText(String(i + 1), lx, ly - 4);
      }
    });
    // marcadores de problemas
    for (const it of issues) {
      const [sx, sy] = toScreen(it.x, it.y);
      if (sx < -20 || sx > W + 20 || sy < -20 || sy > H + 20) continue;
      ctx.strokeStyle = it.severity === "error" ? colors.err : it.severity === "aviso" ? colors.warn : colors.text;
      ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(sx, sy, 9, 0, 2 * Math.PI); ctx.stroke();
    }
    // medir
    if (measure) {
      const a = toScreen(...measure.a);
      const bw = measure.b || measure.cur;
      ctx.fillStyle = colors.ink; ctx.beginPath(); ctx.arc(a[0], a[1], 3.5, 0, 2 * Math.PI); ctx.fill();
      if (bw) {
        const b = toScreen(...bw);
        ctx.strokeStyle = colors.ink; ctx.lineWidth = 1.2; ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); ctx.setLineDash([]);
        ctx.beginPath(); ctx.arc(b[0], b[1], 3.5, 0, 2 * Math.PI); ctx.fill();
        const d = dist(measure.a, bw);
        const label = `${fmt(d)} mm`;
        ctx.font = "700 12px system-ui, sans-serif"; ctx.textAlign = "center";
        const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2 - 8;
        const tw = ctx.measureText(label).width + 10;
        ctx.fillStyle = colors.bg; ctx.fillRect(mx - tw / 2, my - 13, tw, 18);
        ctx.fillStyle = colors.ink; ctx.fillText(label, mx, my);
      }
    }
    if (snapPt) {
      const [sx, sy] = toScreen(...snapPt.p);
      ctx.strokeStyle = colors.pierce; ctx.lineWidth = 2;
      if (snapPt.kind === "centro") { ctx.beginPath(); ctx.arc(sx, sy, 6, 0, 2 * Math.PI); ctx.stroke(); }
      else ctx.strokeRect(sx - 5, sy - 5, 10, 10);
    }
    cadPaint();
    // ventana de selección
    if (drag && (drag.mode === "box" || drag.mode === "nodebox")) {
      const x = Math.min(drag.sx, drag.cx), y = Math.min(drag.sy, drag.cy);
      const w = Math.abs(drag.cx - drag.sx), h = Math.abs(drag.cy - drag.sy);
      const crossing = drag.cx < drag.sx;
      ctx.fillStyle = crossing ? "rgba(27,158,90,.08)" : "rgba(15,111,222,.08)";
      ctx.strokeStyle = crossing ? colors.pierce : colors.sel;
      ctx.setLineDash(crossing ? [5, 4] : []); ctx.lineWidth = 1;
      ctx.fillRect(x, y, w, h); ctx.strokeRect(x, y, w, h); ctx.setLineDash([]);
    }
  }
  function paintGrid() {
    const step = niceStep(25);
    const [x0, y0] = toWorld(0, H), [x1, y1] = toWorld(W, 0);
    ctx.lineWidth = 1;
    for (const major of [false, true]) {
      const st = major ? step * 5 : step;
      ctx.strokeStyle = colors.grid; ctx.globalAlpha = major ? 0.9 : 0.45;
      ctx.beginPath();
      for (let x = Math.ceil(x0 / st) * st; x <= x1; x += st) { const sx = Math.round(toScreen(x, 0)[0]) + 0.5; ctx.moveTo(sx, 0); ctx.lineTo(sx, H); }
      for (let y = Math.ceil(y0 / st) * st; y <= y1; y += st) { const sy = Math.round(toScreen(0, y)[1]) + 0.5; ctx.moveTo(0, sy); ctx.lineTo(W, sy); }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    const [ox, oy] = toScreen(0, 0);
    ctx.strokeStyle = colors.text; ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.moveTo(ox, oy); ctx.lineTo(ox + 30, oy); ctx.moveTo(ox, oy); ctx.lineTo(ox, oy - 30); ctx.stroke();
    ctx.font = "11px system-ui, sans-serif"; ctx.fillStyle = colors.text; ctx.textAlign = "left";
    ctx.fillText(`rejilla ${fmt(step, step < 1 ? 2 : 0)} mm`, 8, H - 8);
  }
  function paintArrows(c) {
    const total = lengthOf(c.pts);
    if (total * view.s < 40) return;
    const n = Math.max(1, Math.min(6, Math.floor((total * view.s) / 160)));
    ctx.fillStyle = colors[c.kind] || colors.exterior;
    for (let k = 0; k < n; k++) {
      let target = (total * (k + 0.5)) / n, acc = 0;
      for (let i = 1; i < c.pts.length; i++) {
        const l = dist(c.pts[i - 1], c.pts[i]);
        if (acc + l >= target && l > 0) {
          const t = (target - acc) / l;
          const p = [c.pts[i - 1][0] + t * (c.pts[i][0] - c.pts[i - 1][0]), c.pts[i - 1][1] + t * (c.pts[i][1] - c.pts[i - 1][1])];
          const [sx, sy] = toScreen(p[0], p[1]);
          const ang = Math.atan2(-(c.pts[i][1] - c.pts[i - 1][1]), c.pts[i][0] - c.pts[i - 1][0]);
          ctx.save(); ctx.translate(sx, sy); ctx.rotate(ang);
          ctx.beginPath(); ctx.moveTo(6, 0); ctx.lineTo(-4, -4.5); ctx.lineTo(-4, 4.5); ctx.closePath(); ctx.fill();
          ctx.restore();
          break;
        }
        acc += l;
      }
    }
  }

  // ------------------------------------------------------------------ selección e imán
  function hitTest(sx, sy, tolPx) {
    const p = toWorld(sx, sy), tol = tolPx / view.s;
    let best = -1, bestD = tol;
    C.forEach((c, i) => {
      const bb = boxes[i];
      if (p[0] < bb[0] - tol || p[0] > bb[2] + tol || p[1] < bb[1] - tol || p[1] > bb[3] + tol) return;
      for (let k = 1; k < c.pts.length; k++) {
        const d = segDist(p, c.pts[k - 1], c.pts[k]).d;
        if (d < bestD) { bestD = d; best = i; }
      }
    });
    return best;
  }
  function snapAt(sx, sy, tolPx) {
    const p = toWorld(sx, sy), tol = tolPx / view.s;
    let best = null, bestD = tol;
    C.forEach((c, i) => {
      const bb = boxes[i];
      if (p[0] < bb[0] - tol || p[0] > bb[2] + tol || p[1] < bb[1] - tol || p[1] > bb[3] + tol) {
        const circ = circleOf(c);
        if (!circ || dist(p, [circ.cx, circ.cy]) > tol) return;
      }
      const circ = circleOf(c);
      if (circ) { const d = dist(p, [circ.cx, circ.cy]); if (d < bestD) { bestD = d; best = { p: [circ.cx, circ.cy], kind: "centro" }; } }
      for (let k = 0; k < c.pts.length; k++) {
        const d = dist(p, c.pts[k]); if (d < bestD) { bestD = d; best = { p: c.pts[k], kind: "vértice" }; }
        if (k) {   // punto medio del tramo
          const m = [(c.pts[k - 1][0] + c.pts[k][0]) / 2, (c.pts[k - 1][1] + c.pts[k][1]) / 2];
          const dm = dist(p, m); if (dm < bestD * 0.8 && dist(c.pts[k - 1], c.pts[k]) * view.s > 30) { bestD = dm; best = { p: m, kind: "punto medio" }; }
        }
      }
    });
    return best;
  }
  function selectedOrAll() { return sel.size ? [...sel] : C.map((_, i) => i); }

  // ------------------------------------------------------------------ historial
  const snapshot = () => JSON.stringify(C.map((c) => [c.pts, c.closed, c.kind, c.layer || "corte"]));
  function restore(s) {
    C = JSON.parse(s).map(([pts, closed, kind, layer]) => ({ pts, closed, kind, layer }));
    sel = new Set([...sel].filter((i) => i < C.length));
    refreshBoxes();
  }
  function commit(label, fn) {
    const before = snapshot();
    const ok = fn();
    if (ok === false) return;
    undo.push(before); if (undo.length > 80) undo.shift();
    redo = [];
    refreshBoxes();
    setDirty(true, label);
    scheduleAnalysis();
    draw(); renderSide();
  }
  function doUndo() { if (!undo.length) return; redo.push(snapshot()); restore(undo.pop()); setDirty(true, "Deshecho"); scheduleAnalysis(); draw(); renderSide(); }
  function doRedo() { if (!redo.length) return; undo.push(snapshot()); restore(redo.pop()); setDirty(true, "Rehecho"); scheduleAnalysis(); draw(); renderSide(); }

  function setDirty(v, msg) {
    dirty = v;
    const st = $("ed-state");
    st.textContent = v ? `Cambios sin guardar${msg ? " · " + msg : ""}` : (msg || "Guardado");
    st.classList.toggle("dirty", v);
    document.querySelector('[data-act="undo"]').disabled = !undo.length;
    document.querySelector('[data-act="redo"]').disabled = !redo.length;
  }
  function flash(msg) { $("ed-hint").textContent = msg; }

  // ------------------------------------------------------------------ servidor
  async function api(url, body) {
    const r = await fetch(url, {
      method: body ? "POST" : "GET", credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await r.json(); } catch { /* respuesta no JSON (p. ej. sesión caducada) */ }
    if (!r.ok) throw new Error((data && data.error) || (r.status === 401 ? "Sesión caducada: vuelve a iniciar sesión." : `Error ${r.status}`));
    return data;
  }
  const rnd = (pts) => pts.map(([x, y]) => [Math.round(x * 1e4) / 1e4, Math.round(y * 1e4) / 1e4]);
  const payload = () => C.map((c) => ({ points: rnd(c.pts), closed: c.closed, layer: c.layer || "corte" }));
  const cutIndex = () => C.map((c, i) => (c.layer === "marcado" ? -1 : i)).filter((i) => i >= 0);
  function rulesBody() {
    return { thickness_mm: String($("ed-thick").value).replace(",", "."), family: $("ed-family").value };
  }
  function scheduleAnalysis() {
    clearTimeout(analysisTimer);
    analysisTimer = setTimeout(runAnalysis, 350);
  }
  async function runAnalysis() {
    if (!C.length) { metrics = null; issues = []; renderSide(); draw(); return; }
    const seq = ++analysisSeq;
    try {
      const map = cutIndex();
      if (!map.length) { metrics = null; issues = []; renderSide(); draw(); return; }
      const res = await api(`/piezas/${PART}/geometria/analizar`, { contours: map.map((i) => ({ points: rnd(C[i].pts), closed: C[i].closed })), ...rulesBody() });
      if (seq !== analysisSeq) return;     // llegó una respuesta antigua
      metrics = res.metrics;
      issues = res.issues.map((it) => ({ ...it, contours: it.contours.map((k) => map[k]) }));
      if (!baseMetrics) baseMetrics = metrics;
      if (res.kinds.length === map.length) res.kinds.forEach((k, j) => { if (C[map[j]]) C[map[j]].kind = k; });
      renderSide(); draw();
    } catch (e) {
      if (seq === analysisSeq) { $("ed-metrics").innerHTML = `<dt class="ed-err">${esc(e.message)}</dt><dd></dd>`; }
    }
  }
  async function save(asNew) {
    if (saving) return;
    if (!asNew && LOCKED) return;
    saving = true; $("ed-state").textContent = "Guardando…";
    try {
      const res = await api(`/piezas/${PART}/geometria`, { contours: payload(), as_new: !!asNew });
      dirty = false;
      if (res.redirect) { window.location.href = res.redirect; return; }
      C = res.contours.map((c) => ({ pts: c.points, closed: c.closed, kind: c.kind, layer: c.layer || "corte" }));
      sel.clear(); undo = []; redo = [];
      refreshBoxes(); setDirty(false, "Guardado. Recalcula el coste en la ficha de la pieza.");
      scheduleAnalysis(); draw(); renderSide();
    } catch (e) {
      setDirty(true, e.message);
    } finally { saving = false; }
  }

  // ------------------------------------------------------------------ paneles
  function delta(key, dec, scale = 1) {
    if (!baseMetrics || baseMetrics[key] == null || metrics[key] == null) return "";
    const d = (metrics[key] - baseMetrics[key]) * scale;
    if (Math.abs(d) < Math.pow(10, -dec) / 2) return "";
    return ` <span class="ed-delta ${d > 0 ? "up" : "down"}">${d > 0 ? "+" : ""}${fmt(d, dec)}</span>`;
  }
  function renderSide() {
    if (metrics) {
      const m = metrics;
      $("ed-metrics").innerHTML = [
        ["Longitud de corte", `${fmt(m.cut_length_mm, 1)} mm${delta("cut_length_mm", 1)}`],
        ["Perforaciones", `${m.pierce_count}${delta("pierce_count", 0)}`],
        ["Área neta", `${fmt(m.net_area_mm2 / 100, 2)} cm²${delta("net_area_mm2", 2, 0.01)}`],
        ["Envolvente", `${fmt(m.bbox_width_mm, 1)} × ${fmt(m.bbox_height_mm, 1)} mm`],
        ["Envolvente mínima", `${fmt(m.min_rect_width_mm, 1)} × ${fmt(m.min_rect_height_mm, 1)} mm`],
        ["Contornos / piezas", `${m.contour_count} / ${m.part_count}`],
        ["Abiertos", `${m.open_contours}`],
        ["Interior más pequeño", m.smallest_inner_feature_mm == null ? "—" : `${fmt(m.smallest_inner_feature_mm, 1)} mm`],
        ["Recorrido en vacío", `${fmt(m.rapid_length_mm, 0)} mm`],
      ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    }
    // problemas
    const n = { error: 0, aviso: 0, info: 0 };
    issues.forEach((it) => n[it.severity]++);
    $("ed-issues-sum").innerHTML = !metrics ? "" : (n.error + n.aviso + n.info === 0
      ? '<span class="badge ok">Sin problemas detectados</span>'
      : `${n.error ? `<span class="badge err">${n.error} error${n.error > 1 ? "es" : ""}</span> ` : ""}${n.aviso ? `<span class="badge warn">${n.aviso} aviso${n.aviso > 1 ? "s" : ""}</span> ` : ""}${n.info ? `<span class="badge off">${n.info} nota${n.info > 1 ? "s" : ""}</span>` : ""}`);
    $("ed-issues").innerHTML = issues.map((it, k) =>
      `<li><button type="button" class="ed-issue ${it.severity}" data-issue="${k}">${esc(it.msg)}</button></li>`).join("");
    // selección
    const idx = [...sel];
    if (!idx.length) $("ed-sel").innerHTML = "<dt>Nada seleccionado</dt><dd></dd>";
    else {
      const bb = allBox(idx);
      const len = idx.reduce((s, i) => s + lengthOf(C[i].pts), 0);
      const rows = [["Contornos", idx.length], ["Longitud", `${fmt(len, 2)} mm`],
        ["Tamaño", `${fmt(bb[2] - bb[0])} × ${fmt(bb[3] - bb[1])} mm`],
        ["Esquina inf. izq.", `${fmt(bb[0])} ; ${fmt(bb[1])}`]];
      if (idx.length === 1) {
        const c = C[idx[0]];
        rows.unshift(["Nº / tipo", `${idx[0] + 1} · ${c.kind}${c.closed ? "" : " (abierto)"}`]);
        if (c.closed) rows.push(["Área", `${fmt(Math.abs(areaOf(c.pts)) / 100, 3)} cm²`], ["Sentido", areaOf(c.pts) > 0 ? "antihorario" : "horario"]);
        else rows.push(["Separación de extremos", `${fmt(dist(c.pts[0], c.pts[c.pts.length - 1]), 3)} mm`]);
        const circ = circleOf(c);
        if (circ) rows.push(["Círculo", `Ø ${fmt(circ.r * 2, 3)} mm`], ["Centro", `${fmt(circ.cx)} ; ${fmt(circ.cy)}`]);
        rows.push(["Vértices", c.pts.length], ["Inicio de corte", `${fmt(c.pts[0][0])} ; ${fmt(c.pts[0][1])}`]);
      }
      $("ed-sel").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    }
    // lista
    const bad = new Map();
    issues.forEach((it) => it.contours.forEach((i) => { if (bad.get(i) !== "error") bad.set(i, it.severity); }));
    const rowsHtml = C.slice(0, 2000).map((c, i) =>
      `<tr data-row="${i}" class="${sel.has(i) ? "on" : ""}"><td>${i + 1}${bad.has(i) ? ` <span class="dot ${bad.get(i)}" title="${bad.get(i)}"></span>` : ""}</td>` +
      `<td><span class="sw ${c.layer === "marcado" ? "marcado" : c.kind}"></span>${c.layer === "marcado" ? "marcado" : c.kind}</td><td class="num">${fmt(lengthOf(c.pts), 1)}</td></tr>`).join("");
    $("ed-list").innerHTML = rowsHtml + (C.length > 2000 ? `<tr><td colspan="3" class="muted">… ${C.length - 2000} más</td></tr>` : "");
  }

  // ------------------------------------------------------------------ diálogo de parámetros
  const dlg = $("ed-dialog");
  let dlgApply = null;
  function ask(title, fields, apply) {
    $("ed-dialog-title").textContent = title;
    $("ed-dialog-error").textContent = "";
    $("ed-dialog-fields").innerHTML = fields.map((f) =>
      `<div class="field"><label for="dlg-${f.name}">${esc(f.label)}${f.unit ? ` <span class="unit">${esc(f.unit)}</span>` : ""}</label>` +
      (f.options ? `<select id="dlg-${f.name}" name="${f.name}">${f.options.map(([v, l]) => `<option value="${v}" ${String(v) === String(f.value) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`
        : `<input id="dlg-${f.name}" name="${f.name}" type="text" inputmode="decimal" value="${esc(String(f.value).replace(".", ","))}" autocomplete="off">`) +
      (f.help ? `<p class="help">${esc(f.help)}</p>` : "") + "</div>").join("");
    dlgApply = { fields, apply };
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    dlg.querySelector("input,select")?.focus();
  }
  $("ed-dialog-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!dlgApply) return;
    const vals = {};
    for (const f of dlgApply.fields) {
      const raw = dlg.querySelector(`[name="${f.name}"]`).value;
      if (f.options) { vals[f.name] = raw; continue; }
      const v = Number(String(raw).trim().replace(",", "."));
      if (!isFinite(v) || String(raw).trim() === "") { $("ed-dialog-error").textContent = `«${f.label}» debe ser un número.`; return; }
      if (f.min != null && v < f.min) { $("ed-dialog-error").textContent = `«${f.label}» debe ser como mínimo ${fmt(f.min, 2)}.`; return; }
      if (f.max != null && v > f.max) { $("ed-dialog-error").textContent = `«${f.label}» debe ser como máximo ${fmt(f.max, 2)}.`; return; }
      vals[f.name] = v;
    }
    const err = dlgApply.apply(vals);
    if (typeof err === "string") { $("ed-dialog-error").textContent = err; return; }
    dlg.close ? dlg.close() : dlg.removeAttribute("open");
    dlgApply = null;
  });
  dlg.querySelector("[data-dialog-cancel]").addEventListener("click", () => { dlg.close ? dlg.close() : dlg.removeAttribute("open"); dlgApply = null; });

  // ------------------------------------------------------------------ operaciones
  const scope = () => (sel.size ? `${sel.size} contorno${sel.size > 1 ? "s" : ""} seleccionado${sel.size > 1 ? "s" : ""}` : "toda la pieza");
  function centerOf(idx) { const b = allBox(idx); return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]; }
  function applyTransform(label, f) {
    const idx = selectedOrAll();
    commit(label, () => { idx.forEach((i) => { C[i].pts = transformPts(C[i].pts, f); }); });
  }
  function addContour(label, pts) {
    commit(label, () => { C.push({ pts, closed: true, kind: "interior" }); sel = new Set([C.length - 1]); });
    flash(`${label} añadido.`);
  }
  function joinOpen(tol) {
    const candidates = (sel.size ? [...sel] : C.map((_, i) => i)).filter((i) => !C[i].closed);
    if (!candidates.length) { flash("No hay contornos abiertos que unir."); return; }
    commit("Unir abiertos", () => {
      const rest = candidates.map((i) => clonePts(C[i].pts));
      const chains = [];
      while (rest.length) {
        let ch = rest.shift(), ext = true;
        while (ext) {
          ext = false;
          if (ch.length > 2 && dist(ch[0], ch[ch.length - 1]) <= tol) break;
          let bestI = -1, bestD = tol, how = 0;
          rest.forEach((s, i) => {
            const opts = [dist(ch[ch.length - 1], s[0]), dist(ch[ch.length - 1], s[s.length - 1]), dist(ch[0], s[s.length - 1]), dist(ch[0], s[0])];
            opts.forEach((d, k) => { if (d <= bestD) { bestD = d; bestI = i; how = k; } });
          });
          if (bestI < 0) break;
          const s = rest.splice(bestI, 1)[0];
          const drop = (a, b) => (dist(a, b) < 1e-6 ? 1 : 0);
          if (how === 0) ch = ch.concat(s.slice(drop(ch[ch.length - 1], s[0])));
          else if (how === 1) { const r = s.slice().reverse(); ch = ch.concat(r.slice(drop(ch[ch.length - 1], r[0]))); }
          else if (how === 2) ch = s.slice(0, s.length - drop(s[s.length - 1], ch[0])).concat(ch);
          else { const r = s.slice().reverse(); ch = r.slice(0, r.length - drop(r[r.length - 1], ch[0])).concat(ch); }
          ext = true;
        }
        let closed = ch.length > 2 && dist(ch[0], ch[ch.length - 1]) <= tol;
        if (closed) { if (dist(ch[0], ch[ch.length - 1]) > 1e-9) ch.push([...ch[0]]); else ch[ch.length - 1] = [...ch[0]]; }
        chains.push({ pts: ch, closed, kind: closed ? "exterior" : "abierto" });
      }
      const drop = new Set(candidates);
      const before = candidates.length;
      C = C.filter((_, i) => !drop.has(i)).concat(chains);
      sel = new Set(chains.map((_, k) => C.length - chains.length + k));
      flash(`${before} tramos → ${chains.length} contorno(s); ${chains.filter((c) => c.closed).length} cerrado(s).`);
    });
  }
  function setStart(sx, sy) {
    const i = hitTest(sx, sy, 12);
    if (i < 0) { flash("Pulsa sobre un contorno para fijar su punto de inicio."); return; }
    const p = toWorld(sx, sy);
    const c = C[i];
    if (!c.closed) {
      if (dist(p, c.pts[c.pts.length - 1]) < dist(p, c.pts[0])) commit("Inicio de corte", () => { c.pts.reverse(); });
      flash("En contornos abiertos el inicio solo puede ser uno de los extremos.");
      return;
    }
    let best = { d: Infinity, k: 1, q: null };
    for (let k = 1; k < c.pts.length; k++) { const r = segDist(p, c.pts[k - 1], c.pts[k]); if (r.d < best.d) best = { d: r.d, k, q: r.q }; }
    commit("Inicio de corte", () => {
      const ring = c.pts.slice(0, -1);
      let start;
      const a = ring[best.k - 1], b = ring[best.k % ring.length];
      if (dist(best.q, a) < 0.01) start = best.k - 1;
      else if (dist(best.q, b) < 0.01) start = best.k % ring.length;
      else { ring.splice(best.k, 0, best.q); start = best.k; }
      const rot = ring.slice(start).concat(ring.slice(0, start));
      rot.push([...rot[0]]);
      c.pts = rot;
      sel = new Set([i]);
    });
    flash(`Inicio de corte del contorno ${i + 1} en ${fmt(best.q[0])} ; ${fmt(best.q[1])}.`);
  }

  const actions = {
    delete() {
      if (!sel.size) { flash("Selecciona primero lo que quieres borrar."); return; }
      if (sel.size === C.length) { flash("No se puede borrar todo: la pieza necesita al menos un contorno."); return; }
      const n = sel.size;
      commit("Borrar", () => { C = C.filter((_, i) => !sel.has(i)); sel.clear(); });
      flash(`${n} contorno(s) borrado(s).`);
    },
    close() {
      const idx = (sel.size ? [...sel] : []).filter((i) => !C[i].closed);
      if (!idx.length) { flash("Selecciona uno o varios contornos abiertos (botón «Abiertos»)."); return; }
      const tooShort = idx.filter((i) => C[i].pts.length < 3);
      if (tooShort.length === idx.length) { flash("Un segmento suelto no se puede cerrar; únelo con otros tramos."); return; }
      commit("Cerrar", () => { idx.forEach((i) => { if (C[i].pts.length >= 3) { C[i].closed = true; C[i].pts.push([...C[i].pts[0]]); } }); });
      flash(`${idx.length - tooShort.length} contorno(s) cerrado(s) con un tramo recto.`);
    },
    join() {
      ask(`Unir tramos abiertos (${sel.size ? "selección" : "todos"})`, [
        { name: "tol", label: "Distancia máxima entre extremos", unit: "mm", value: 0.5, min: 0.001, max: 50,
          help: "Los extremos más cercanos que esta distancia se unen con un tramo recto." }],
      (v) => joinOpen(v.tol));
    },
    reverse() {
      const idx = selectedOrAll();
      commit("Invertir sentido", () => idx.forEach((i) => C[i].pts.reverse()));
      flash(`Sentido invertido en ${scope()}.`);
    },
    move() {
      ask(`Mover ${scope()}`, [{ name: "dx", label: "Desplazamiento X", unit: "mm", value: 0 }, { name: "dy", label: "Desplazamiento Y", unit: "mm", value: 0 }],
        (v) => applyTransform("Mover", ([x, y]) => [x + v.dx, y + v.dy]));
    },
    rotate() {
      ask(`Girar ${scope()}`, [{ name: "a", label: "Ángulo (antihorario)", unit: "°", value: 90, min: -360, max: 360 }], (v) => {
        const [cx, cy] = centerOf(selectedOrAll()), r = (v.a * Math.PI) / 180, co = Math.cos(r), si = Math.sin(r);
        applyTransform("Girar", ([x, y]) => [cx + (x - cx) * co - (y - cy) * si, cy + (x - cx) * si + (y - cy) * co]);
      });
    },
    mirrorx() { const [cx] = centerOf(selectedOrAll()); applyTransform("Simetría", ([x, y]) => [2 * cx - x, y]); flash(`Simetría horizontal de ${scope()}.`); },
    mirrory() { const [, cy] = centerOf(selectedOrAll()); applyTransform("Simetría", ([x, y]) => [x, 2 * cy - y]); flash(`Simetría vertical de ${scope()}.`); },
    scale() {
      ask(`Escalar ${scope()}`, [{ name: "f", label: "Factor", value: 1, min: 0.0001, max: 1000, help: "25,4 = pulgadas a mm · 10 = cm a mm · 0,1 = décimas a mm" },
        { name: "o", label: "Punto base", value: "c", options: [["c", "Centro de la selección"], ["0", "Origen (0,0)"]] }], (v) => {
        const [cx, cy] = v.o === "0" ? [0, 0] : centerOf(selectedOrAll());
        applyTransform("Escalar", ([x, y]) => [cx + (x - cx) * v.f, cy + (y - cy) * v.f]);
      });
    },
    copy() {
      if (!sel.size) { flash("Selecciona primero lo que quieres copiar."); return; }
      const b = allBox([...sel]);
      ask("Copiar la selección en fila", [{ name: "n", label: "Nº de copias", value: 1, min: 1, max: 200 },
        { name: "dx", label: "Paso X", unit: "mm", value: Math.round((b[2] - b[0] + 10) * 100) / 100 }, { name: "dy", label: "Paso Y", unit: "mm", value: 0 }], (v) => {
        const n = Math.round(v.n), src = [...sel];
        commit("Copiar", () => {
          const added = [];
          for (let k = 1; k <= n; k++) for (const i of src) { C.push({ pts: C[i].pts.map(([x, y]) => [x + v.dx * k, y + v.dy * k]), closed: C[i].closed, kind: C[i].kind }); added.push(C.length - 1); }
          sel = new Set(added);
        });
      });
    },
    origin() {
      const b = allBox();
      applyTransform("Al origen", ([x, y]) => [x - b[0], y - b[1]]);
      sel.clear(); fit();
      flash("Pieza llevada al origen (esquina inferior izquierda en 0,0).");
    },
    circle() {
      const [cx, cy] = sel.size ? centerOf([...sel]) : centerOf();
      ask("Añadir círculo", [{ name: "x", label: "Centro X", unit: "mm", value: Math.round(cx * 100) / 100 }, { name: "y", label: "Centro Y", unit: "mm", value: Math.round(cy * 100) / 100 },
        { name: "d", label: "Diámetro", unit: "mm", value: 10, min: 0.1, max: 20000 }], (v) => addContour("Círculo", circlePts(v.x, v.y, v.d / 2)));
    },
    rect() {
      const [cx, cy] = sel.size ? centerOf([...sel]) : centerOf();
      ask("Añadir rectángulo", [{ name: "x", label: "Centro X", unit: "mm", value: Math.round(cx * 100) / 100 }, { name: "y", label: "Centro Y", unit: "mm", value: Math.round(cy * 100) / 100 },
        { name: "w", label: "Ancho", unit: "mm", value: 20, min: 0.1, max: 20000 }, { name: "h", label: "Alto", unit: "mm", value: 10, min: 0.1, max: 20000 },
        { name: "r", label: "Radio de esquinas", unit: "mm", value: 0, min: 0, max: 10000 }],
      (v) => addContour("Rectángulo", roundRectPts(v.x - v.w / 2, v.y - v.h / 2, v.w, v.h, v.r)));
    },
    slot() {
      const [cx, cy] = sel.size ? centerOf([...sel]) : centerOf();
      ask("Añadir coliso", [{ name: "x", label: "Centro X", unit: "mm", value: Math.round(cx * 100) / 100 }, { name: "y", label: "Centro Y", unit: "mm", value: Math.round(cy * 100) / 100 },
        { name: "l", label: "Longitud total", unit: "mm", value: 30, min: 0.2, max: 20000 }, { name: "w", label: "Ancho", unit: "mm", value: 10, min: 0.1, max: 10000 },
        { name: "a", label: "Ángulo", unit: "°", value: 0, min: -360, max: 360 }], (v) => {
        if (v.l < v.w) return "La longitud debe ser mayor o igual que el ancho.";
        const pts = roundRectPts(-v.l / 2, -v.w / 2, v.l, v.w, v.w / 2);
        const r = (v.a * Math.PI) / 180, co = Math.cos(r), si = Math.sin(r);
        addContour("Coliso", pts.map(([x, y]) => [v.x + x * co - y * si, v.y + x * si + y * co]));
      });
    },
    simplify() {
      ask(`Simplificar ${scope()}`, [{ name: "t", label: "Tolerancia", unit: "mm", value: 0.01, min: 0.001, max: 1,
        help: "Quita vértices que se desvían menos que esto. 0,01 mm no altera la pieza." }], (v) => {
        const idx = selectedOrAll(); let before = 0, after = 0;
        commit("Simplificar", () => idx.forEach((i) => {
          const c = C[i]; before += c.pts.length;
          const s = simplifyDP(c.pts, v.t);
          if (!c.closed || s.length >= 4) c.pts = s;
          after += c.pts.length;
        }));
        flash(`Vértices: ${before} → ${after}.`);
      });
    },
    tiny() {
      ask("Quitar contornos diminutos", [{ name: "l", label: "Longitud menor que", unit: "mm", value: 0.5, min: 0.001, max: 100 }], (v) => {
        const drop = new Set(C.map((c, i) => (lengthOf(c.pts) < v.l ? i : -1)).filter((i) => i >= 0));
        if (!drop.size) { flash("No hay contornos tan pequeños."); return; }
        if (drop.size === C.length) return "Se borraría toda la pieza.";
        commit("Quitar diminutos", () => { C = C.filter((_, i) => !drop.has(i)); sel.clear(); });
        flash(`${drop.size} contorno(s) diminuto(s) borrado(s).`);
      });
    },
    selall() { sel = new Set(C.map((_, i) => i)); draw(); renderSide(); },
    selinv() { sel = new Set(C.map((_, i) => i).filter((i) => !sel.has(i))); draw(); renderSide(); },
    selopen() { sel = new Set(C.map((c, i) => (c.closed ? -1 : i)).filter((i) => i >= 0)); flash(`${sel.size} contorno(s) abierto(s).`); draw(); renderSide(); },
    selissues() { sel = new Set(issues.flatMap((it) => it.contours)); flash(`${sel.size} contorno(s) con problemas.`); draw(); renderSide(); },
    fit() { fit(); },
    zoomin() { zoomAt(W / 2, H / 2, 1.4); },
    zoom100() { zoomAt(W / 2, H / 2, PX_PER_MM / view.s); },
    zoomout() { zoomAt(W / 2, H / 2, 1 / 1.4); },
    undo: doUndo,
    redo: doRedo,
    save() { save(false); },
    saveas() { save(true); },
  };

  // ================================================================== herramientas CAD
  // Dibujo con coordenadas, nodos/estirar, empalme, chaflán, partir, recortar, alargar, ángulos,
  // equidistancia y booleanas (servidor), matrices, capas de corte/marcado, cotas e imanes.
  const snapCfg = { obj: true, grid: false, ortho: false };
  const cad = { pts: [], cur: null, nodeSel: new Set(), hoverNode: null, brk: null, angle: [] };
  const toolVals = { fillet: 5, chamfer: 3 };
  let lastPt = null;

  function cadReset() { cad.pts = []; cad.cur = null; cad.brk = null; cad.angle = []; cad.nodeSel.clear(); cad.hoverNode = null; }
  function toggleSnap(k) {
    snapCfg[k] = !snapCfg[k];
    document.querySelectorAll(`[data-snap="${k}"]`).forEach((b) => b.setAttribute("aria-pressed", String(snapCfg[k])));
    flash({ obj: "Imán a objetos", grid: "Imán a rejilla", ortho: "Orto (solo horizontal/vertical)" }[k] + (snapCfg[k] ? " activado." : " desactivado."));
  }
  app.querySelectorAll("[data-snap]").forEach((b) => b.addEventListener("click", () => toggleSnap(b.dataset.snap)));

  const ring = (c) => (c.closed ? c.pts.slice(0, -1) : c.pts.slice());
  const setRing = (c, r) => { c.pts = c.closed ? r.concat([[...r[0]]]) : r; };
  const unit = (v) => { const l = Math.hypot(v[0], v[1]) || 1; return [v[0] / l, v[1] / l]; };
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
  const lerp = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];

  function segInter(p, p2, q, q2) {   // intersección de segmentos: {t, u, pt} o null
    const r = sub(p2, p), s = sub(q2, q);
    const den = r[0] * s[1] - r[1] * s[0];
    if (Math.abs(den) < 1e-12) return null;
    const qp = sub(q, p);
    const t = (qp[0] * s[1] - qp[1] * s[0]) / den, u = (qp[0] * r[1] - qp[1] * r[0]) / den;
    if (t < -1e-9 || t > 1 + 1e-9 || u < -1e-9 || u > 1 + 1e-9) return null;
    return { t, u, pt: lerp(p, p2, t) };
  }
  function nearestOn(c, p) {          // punto más cercano de un contorno: {k (segmento), q, s (longitud acumulada), d}
    let best = { d: Infinity }, acc = 0;
    for (let k = 1; k < c.pts.length; k++) {
      const l = dist(c.pts[k - 1], c.pts[k]);
      const r = segDist(p, c.pts[k - 1], c.pts[k]);
      if (r.d < best.d) best = { k, q: r.q, s: acc + r.t * l, d: r.d };
      acc += l;
    }
    return best;
  }
  function subPath(pts, s0, s1) {      // tramo de la polilínea entre dos longitudes s0 < s1
    const out = []; let acc = 0;
    for (let k = 1; k < pts.length; k++) {
      const l = dist(pts[k - 1], pts[k]), a = acc, b = acc + l;
      if (b >= s0 && a <= s1 && l > 0) {
        if (!out.length) out.push(lerp(pts[k - 1], pts[k], Math.max(0, (s0 - a) / l)));
        if (b <= s1) out.push([...pts[k]]); else { out.push(lerp(pts[k - 1], pts[k], (s1 - a) / l)); break; }
      }
      acc = b;
    }
    return out.filter((p, i) => !i || dist(p, out[i - 1]) > 1e-7);
  }
  function crossings(i) {              // longitudes s donde el contorno i corta a los demás (y a sí mismo)
    const c = C[i], bb = boxes[i], out = [];
    let acc = 0;
    const segs = [];
    for (let k = 1; k < c.pts.length; k++) { const l = dist(c.pts[k - 1], c.pts[k]); segs.push([c.pts[k - 1], c.pts[k], acc, l]); acc += l; }
    C.forEach((o, j) => {
      const ob = boxes[j];
      if (ob[0] > bb[2] || ob[2] < bb[0] || ob[1] > bb[3] || ob[3] < bb[1]) return;
      for (const [a, b, s0, l] of segs) {
        const sa = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
        for (let k = 1; k < o.pts.length; k++) {
          const q = o.pts[k - 1], q2 = o.pts[k];
          if (Math.max(q[0], q2[0]) < sa[0] || Math.min(q[0], q2[0]) > sa[2] || Math.max(q[1], q2[1]) < sa[1] || Math.min(q[1], q2[1]) > sa[3]) continue;
          if (j === i && (Math.abs(s0 - (segs[k - 1] ? segs[k - 1][2] : -1)) < 1e-9 || dist(a, q2) < 1e-9 || dist(b, q) < 1e-9 || dist(a, q) < 1e-9)) continue;
          const r = segInter(a, b, q, q2);
          if (r) out.push(s0 + r.t * l);
        }
      }
    });
    return [...new Set(out.map((v) => Math.round(v * 1e6) / 1e6))].sort((x, y) => x - y);
  }
  function arc3(a, m, b) {             // arco por tres puntos
    const d = 2 * (a[0] * (m[1] - b[1]) + m[0] * (b[1] - a[1]) + b[0] * (a[1] - m[1]));
    if (Math.abs(d) < 1e-9) return [a, b];
    const ux = ((a[0] ** 2 + a[1] ** 2) * (m[1] - b[1]) + (m[0] ** 2 + m[1] ** 2) * (b[1] - a[1]) + (b[0] ** 2 + b[1] ** 2) * (a[1] - m[1])) / d;
    const uy = ((a[0] ** 2 + a[1] ** 2) * (b[0] - m[0]) + (m[0] ** 2 + m[1] ** 2) * (a[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (m[0] - a[0])) / d;
    const r = dist([ux, uy], a);
    const ang = (p) => Math.atan2(p[1] - uy, p[0] - ux);
    const a0 = ang(a), am = ang(m), a1 = ang(b);
    const norm = (x) => ((x % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
    let sweep = norm(a1 - a0);
    if (norm(am - a0) > sweep) sweep -= 2 * Math.PI;   // el punto medio decide el sentido
    const n = arcSegments(r, sweep);
    const out = [];
    for (let i = 0; i <= n; i++) { const t = a0 + (sweep * i) / n; out.push([ux + r * Math.cos(t), uy + r * Math.sin(t)]); }
    out[0] = [...a]; out[n] = [...b];
    return out;
  }
  function cornerAt(c, ri, mode, val) {   // empalme (radio) o chaflán (distancia) en el vértice ri del anillo
    const r = ring(c), n = r.length;
    if (!c.closed && (ri === 0 || ri === n - 1)) return "Los extremos de un contorno abierto no son esquinas.";
    const v = r[ri], a = r[(ri - 1 + n) % n], b = r[(ri + 1) % n];
    const u1 = unit(sub(a, v)), u2 = unit(sub(b, v));
    const cos = Math.max(-1, Math.min(1, u1[0] * u2[0] + u1[1] * u2[1]));
    const th = Math.acos(cos);
    if (th > Math.PI - 0.02) return "Esa esquina es prácticamente recta en línea; no hay nada que redondear.";
    const t = mode === "fillet" ? val / Math.tan(th / 2) : val;
    if (t > dist(a, v) + 1e-9 || t > dist(b, v) + 1e-9) return `El ${mode === "fillet" ? "radio" : "chaflán"} es demasiado grande para los tramos de esa esquina.`;
    const p1 = [v[0] + u1[0] * t, v[1] + u1[1] * t], p2 = [v[0] + u2[0] * t, v[1] + u2[1] * t];
    let ins = [p1, p2];
    if (mode === "fillet") {
      const bis = unit([u1[0] + u2[0], u1[1] + u2[1]]);
      const h = val / Math.sin(th / 2);
      const cc = [v[0] + bis[0] * h, v[1] + bis[1] * h];
      const s1 = Math.atan2(p1[1] - cc[1], p1[0] - cc[0]);
      let sw = Math.atan2(p2[1] - cc[1], p2[0] - cc[0]) - s1;
      while (sw > Math.PI) sw -= 2 * Math.PI;
      while (sw < -Math.PI) sw += 2 * Math.PI;
      const m = arcSegments(val, sw);
      ins = [];
      for (let i = 0; i <= m; i++) { const q = s1 + (sw * i) / m; ins.push([cc[0] + val * Math.cos(q), cc[1] + val * Math.sin(q)]); }
    }
    const nr = r.slice(0, ri).concat(ins, r.slice(ri + 1));
    const clean = nr.filter((p, i) => !i || dist(p, nr[i - 1]) > 1e-7);
    if (c.closed && dist(clean[0], clean[clean.length - 1]) < 1e-7) clean.pop();
    setRing(c, clean);
    return null;
  }
  function nearestNode(sx, sy, tolPx) {
    const p = toWorld(sx, sy), tol = tolPx / view.s;
    let best = null, bd = tol;
    C.forEach((c, i) => {
      const bb = boxes[i];
      if (p[0] < bb[0] - tol || p[0] > bb[2] + tol || p[1] < bb[1] - tol || p[1] > bb[3] + tol) return;
      ring(c).forEach((q, k) => { const d = dist(p, q); if (d < bd) { bd = d; best = { i, k }; } });
    });
    return best;
  }
  function cornerIndices(c) {           // esquinas «reales» (no los vértices de arcos discretizados)
    const r = ring(c), n = r.length, out = [];
    for (let k = 0; k < n; k++) {
      if (!c.closed && (k === 0 || k === n - 1)) continue;
      const a = r[(k - 1 + n) % n], b = r[(k + 1) % n];
      const u1 = unit(sub(r[k], a)), u2 = unit(sub(b, r[k]));
      const turn = Math.acos(Math.max(-1, Math.min(1, u1[0] * u2[0] + u1[1] * u2[1])));
      if (turn > (12 * Math.PI) / 180) out.push(k);
    }
    return out;
  }

  // ---------- captura de puntos con imanes, orto y línea de comandos
  function snapIntersection(p, tol) {
    const segs = [];
    C.forEach((c, i) => {
      const bb = boxes[i];
      if (p[0] < bb[0] - tol || p[0] > bb[2] + tol || p[1] < bb[1] - tol || p[1] > bb[3] + tol) return;
      for (let k = 1; k < c.pts.length && segs.length < 400; k++) {
        if (segDist(p, c.pts[k - 1], c.pts[k]).d < tol) segs.push([c.pts[k - 1], c.pts[k], i]);
      }
    });
    let best = null, bd = tol;
    for (let a = 0; a < segs.length; a++) for (let b = a + 1; b < segs.length; b++) {
      const r = segInter(segs[a][0], segs[a][1], segs[b][0], segs[b][1]);
      if (r && (r.t > 1e-6 && r.t < 1 - 1e-6 || segs[a][2] !== segs[b][2])) { const d = dist(p, r.pt); if (d < bd) { bd = d; best = r.pt; } }
    }
    return best;
  }
  function pickPoint(sx, sy, ev, tolPx) {
    let p = toWorld(sx, sy);
    snapPt = null;
    if (snapCfg.obj) {
      const inter = snapIntersection(p, (tolPx * 1.2) / view.s);
      const sn = snapAt(sx, sy, tolPx * 1.6);
      if (inter && (!sn || dist(p, inter) <= dist(p, sn.p))) snapPt = { p: inter, kind: "intersección" };
      else if (sn) snapPt = sn;
    }
    if (snapPt) return [...snapPt.p];
    const ref = cad.pts.length ? cad.pts[cad.pts.length - 1] : null;
    if (ref && (snapCfg.ortho || ev.shiftKey)) {
      if (Math.abs(p[0] - ref[0]) > Math.abs(p[1] - ref[1])) p = [p[0], ref[1]]; else p = [ref[0], p[1]];
    }
    if (snapCfg.grid) { const g = niceStep(25); p = [Math.round(p[0] / g) * g, Math.round(p[1] / g) * g]; }
    return p;
  }
  function parseCmd(txt) {             // «x;y» o «x,y» absoluto · «@dx;dy» relativo · «@d<ángulo» polar · «d» distancia en la dirección del cursor
    const t = txt.trim().replace(/\s+/g, "");
    if (!t) return null;
    const num = (v) => Number(v.replace(",", "."));
    const ref = cad.pts.length ? cad.pts[cad.pts.length - 1] : (lastPt || [0, 0]);
    let m;
    if ((m = t.match(/^@?(-?[\d.,]+)<(-?[\d.,]+)$/))) {
      const d = num(m[1]), a = (num(m[2]) * Math.PI) / 180;
      return [ref[0] + d * Math.cos(a), ref[1] + d * Math.sin(a)];
    }
    if ((m = t.match(/^(@?)(-?\d+(?:[.,]\d+)?)[;](-?\d+(?:[.,]\d+)?)$/)) || (m = t.match(/^(@?)(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$/))) {
      const x = num(m[2]), y = num(m[3]);
      return m[1] ? [ref[0] + x, ref[1] + y] : [x, y];
    }
    if ((m = t.match(/^(-?\d+(?:[.,]\d+)?)$/))) {
      const d = num(m[1]);
      if (CAD_TOOLS[tool].scalar) return { scalar: d };
      if (!cad.pts.length) return null;
      const dir = cad.cur ? unit(sub(cad.cur, ref)) : [1, 0];
      return [ref[0] + dir[0] * d, ref[1] + dir[1] * d];
    }
    return null;
  }
  const cmd = $("ed-cmd");
  cmd.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { cmd.value = ""; cadFinish(); canvas.focus(); return; }
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    const v = cmd.value.trim().toLowerCase();
    cmd.value = "";
    if (!v) { cadFinish(); return; }
    if (v === "c" || v === "cerrar") { cadFinish(true); return; }
    if (v === "d" || v === "deshacer") { cad.pts.pop(); draw(); return; }
    const p = parseCmd(v);
    if (!p) { flash("No entiendo esa entrada. Ejemplos: 100;50 · @20;0 · @30<45 · 25"); return; }
    cadPoint(p.scalar != null ? p : p, true);
  });

  // ---------- definición de herramientas
  const CAD_TOOLS = {
    draw: { points: true, hint: "Polilínea: pulsa puntos o escribe coordenadas (100;50 · @20;0 · @30<45 · 25). Pulsa el primer punto o escribe C para cerrar; Intro termina abierta." },
    arc: { points: true, hint: "Arco: inicio, un punto intermedio y final." },
    dcircle: { points: true, scalar: true, hint: "Círculo: pulsa el centro y luego un punto del borde, o escribe el radio." },
    drect: { points: true, hint: "Rectángulo: pulsa dos esquinas opuestas o escribe @ancho;alto para la segunda." },
    nodes: { hint: "Nodos / estirar: arrastra un vértice, o selecciona varios con ventana y arrástralos. Doble clic en un tramo: añadir vértice; doble clic en un vértice: quitarlo." },
    fillet: { hint: "Empalme: pulsa una esquina, o un tramo para redondear todas las esquinas de ese contorno.",
      ask: (done) => ask("Empalme (redondeo de esquinas)", [{ name: "v", label: "Radio", unit: "mm", value: toolVals.fillet, min: 0.01, max: 5000 }], (v) => { toolVals.fillet = v.v; CAD_TOOLS.fillet.asked = true; setTimeout(done); }) },
    chamfer: { hint: "Chaflán: pulsa una esquina, o un tramo para achaflanar todas las esquinas de ese contorno.",
      ask: (done) => ask("Chaflán", [{ name: "v", label: "Distancia", unit: "mm", value: toolVals.chamfer, min: 0.01, max: 5000 }], (v) => { toolVals.chamfer = v.v; CAD_TOOLS.chamfer.asked = true; setTimeout(done); }) },
    brk: { hint: "Partir: en un contorno abierto, pulsa dónde partirlo; en uno cerrado, pulsa dos puntos." },
    trim: { hint: "Recortar: pulsa el tramo que sobra; se corta hasta los cruces con otros contornos." },
    extend: { hint: "Alargar: pulsa cerca del extremo de un contorno abierto; se prolonga hasta el contorno más próximo." },
    angle: { points: true, hint: "Ángulo: pulsa el vértice y luego un punto en cada lado." },
  };
  // al volver a elegir empalme/chaflán se vuelve a preguntar el valor
  app.querySelectorAll('[data-tool="fillet"],[data-tool="chamfer"]').forEach((b) => b.addEventListener("click", () => {}, { capture: true }));
  app.querySelectorAll('[data-tool="fillet"],[data-tool="chamfer"]').forEach((b) => b.addEventListener("pointerdown", () => { CAD_TOOLS[b.dataset.tool].asked = false; }));

  function cadPoint(p, typed) {
    if (p.scalar != null) {             // radio escrito (círculo)
      if (tool === "dcircle" && cad.pts.length === 1 && p.scalar > 0) { addCircleAt(cad.pts[0], p.scalar); return; }
      flash("Escribe un punto."); return;
    }
    lastPt = p;
    if (tool === "draw") {
      if (cad.pts.length >= 2 && dist(p, cad.pts[0]) < 6 / view.s && !typed) { cadFinish(true); return; }
      cad.pts.push(p);
      flash(`${cad.pts.length} punto(s). Último: ${fmt(p[0])} ; ${fmt(p[1])}${cad.pts.length > 1 ? ` · tramo ${fmt(dist(cad.pts[cad.pts.length - 2], p))} mm` : ""}`);
    } else if (tool === "arc") {
      cad.pts.push(p);
      if (cad.pts.length === 3) {
        const pts = arc3(cad.pts[0], cad.pts[1], cad.pts[2]);
        commit("Arco", () => { C.push({ pts, closed: false, kind: "abierto", layer: "corte" }); sel = new Set([C.length - 1]); });
        cad.pts = [];
      }
    } else if (tool === "dcircle") {
      if (!cad.pts.length) cad.pts.push(p); else addCircleAt(cad.pts[0], dist(cad.pts[0], p));
    } else if (tool === "drect") {
      if (!cad.pts.length) cad.pts.push(p);
      else {
        const a = cad.pts[0];
        const x = Math.min(a[0], p[0]), y = Math.min(a[1], p[1]), w = Math.abs(p[0] - a[0]), h = Math.abs(p[1] - a[1]);
        if (w < 1e-3 || h < 1e-3) { flash("El rectángulo necesita ancho y alto."); return; }
        commit("Rectángulo", () => { C.push({ pts: roundRectPts(x, y, w, h, 0), closed: true, kind: "interior", layer: "corte" }); sel = new Set([C.length - 1]); });
        cad.pts = []; flash(`Rectángulo ${fmt(w)} × ${fmt(h)} mm.`);
      }
    } else if (tool === "angle") {
      cad.pts.push(p);
      if (cad.pts.length === 3) {
        const [v, a, b] = cad.pts;
        const u1 = unit(sub(a, v)), u2 = unit(sub(b, v));
        const ang = (Math.acos(Math.max(-1, Math.min(1, u1[0] * u2[0] + u1[1] * u2[1]))) * 180) / Math.PI;
        flash(`Ángulo: ${fmt(ang, 2)}° (complementario ${fmt(180 - ang, 2)}°, exterior ${fmt(360 - ang, 2)}°)`);
        cad.angle = cad.pts.slice(); cad.pts = [];
      }
    }
    draw();
  }
  function addCircleAt(c, r) {
    if (r < 0.01) { flash("Radio demasiado pequeño."); return; }
    commit("Círculo", () => { C.push({ pts: circlePts(c[0], c[1], r), closed: true, kind: "interior", layer: "corte" }); sel = new Set([C.length - 1]); });
    cad.pts = []; flash(`Círculo Ø ${fmt(r * 2, 3)} mm en ${fmt(c[0])} ; ${fmt(c[1])}.`);
  }
  function cadFinish(close) {
    if (tool === "draw" && cad.pts.length >= 2) {
      const pts = cad.pts.map((p) => [...p]);
      const closed = !!close && pts.length >= 3;
      if (closed) pts.push([...pts[0]]);
      commit("Polilínea", () => { C.push({ pts, closed, kind: closed ? "interior" : "abierto", layer: "corte" }); sel = new Set([C.length - 1]); });
      flash(closed ? "Contorno cerrado añadido." : "Polilínea abierta añadida.");
    }
    cad.pts = []; cad.cur = null; draw();
  }

  function cadDown(sx, sy, ev, tolPx) {
    const t = CAD_TOOLS[tool];
    if (t.points) { cadPoint(pickPoint(sx, sy, ev, tolPx), false); return; }
    const p = toWorld(sx, sy);
    if (tool === "nodes") {
      const nn = nearestNode(sx, sy, tolPx + 2);
      if (nn) {
        const key = `${nn.i}:${nn.k}`;
        if (!cad.nodeSel.has(key)) { if (!(ev.shiftKey || ev.ctrlKey)) cad.nodeSel.clear(); cad.nodeSel.add(key); }
        drag = { mode: "node", sx, sy, cx: sx, cy: sy, w0: p, before: snapshot(), moved: false,
          orig: [...cad.nodeSel].map((k) => { const [i, j] = k.split(":").map(Number); return [i, j, [...ring(C[i])[j]]]; }) };
      } else {
        drag = { mode: "nodebox", sx, sy, cx: sx, cy: sy, additive: ev.shiftKey || ev.ctrlKey };
      }
      draw(); return;
    }
    const hit = hitTest(sx, sy, tolPx);
    if (hit < 0) { flash("Pulsa sobre un contorno."); return; }
    const c = C[hit], near = nearestOn(c, p);
    if (tool === "fillet" || tool === "chamfer") {
      const val = toolVals[tool];
      const nn = nearestNode(sx, sy, tolPx + 4);
      if (nn && nn.i === hit) {
        commit(tool === "fillet" ? "Empalme" : "Chaflán", () => { const e = cornerAt(C[hit], nn.k, tool, val); if (e) { flash(e); return false; } });
      } else {
        const ks = cornerIndices(c).reverse();     // de atrás hacia delante para no mover los índices
        if (!ks.length) { flash("Ese contorno no tiene esquinas vivas."); return; }
        let ok = 0, ko = 0;
        commit(tool === "fillet" ? "Empalme" : "Chaflán", () => { for (const k of ks) { cornerAt(C[hit], k, tool, val) ? ko++ : ok++; } if (!ok) return false; });
        flash(`${ok} esquina(s) ${tool === "fillet" ? "redondeadas" : "achaflanadas"}${ko ? `; ${ko} no admiten ese valor` : ""}.`);
      }
      return;
    }
    if (tool === "brk") {
      if (!c.closed) {
        const L = lengthOf(c.pts);
        if (near.s < 1e-6 || near.s > L - 1e-6) { flash("Pulsa en un punto interior del tramo."); return; }
        commit("Partir", () => {
          const a = subPath(c.pts, 0, near.s), b = subPath(c.pts, near.s, L);
          C.splice(hit, 1, { pts: a, closed: false, kind: "abierto", layer: c.layer }, { pts: b, closed: false, kind: "abierto", layer: c.layer });
          sel = new Set([hit, hit + 1]);
        });
        flash("Contorno partido en dos."); return;
      }
      if (!cad.brk || cad.brk.i !== hit) { cad.brk = { i: hit, s: near.s, q: near.q }; flash("Ahora pulsa el segundo punto de corte en el mismo contorno."); draw(); return; }
      const L = lengthOf(c.pts), s0 = Math.min(cad.brk.s, near.s), s1 = Math.max(cad.brk.s, near.s);
      if (s1 - s0 < 1e-6) { flash("Los dos puntos coinciden."); return; }
      commit("Partir", () => {
        const a = subPath(c.pts, s0, s1);
        const b = subPath(c.pts, s1, L).concat(subPath(c.pts, 0, s0).slice(1));
        C.splice(hit, 1, { pts: a, closed: false, kind: "abierto", layer: c.layer }, { pts: b, closed: false, kind: "abierto", layer: c.layer });
        sel = new Set([hit, hit + 1]);
      });
      cad.brk = null; flash("Contorno cerrado partido en dos tramos abiertos."); return;
    }
    if (tool === "trim") {
      const xs = crossings(hit), L = lengthOf(c.pts);
      if (!xs.length || (c.closed && xs.length < 2)) { flash("Ese contorno no cruza con otros: no hay nada que recortar (usa Borrar)."); return; }
      commit("Recortar", () => {
        const before = xs.filter((v) => v < near.s - 1e-9), after = xs.filter((v) => v > near.s + 1e-9);
        const pieces = [];
        if (!c.closed) {
          const lo = before.length ? before[before.length - 1] : 0, hi = after.length ? after[0] : L;
          if (lo > 1e-6) pieces.push(subPath(c.pts, 0, lo));
          if (hi < L - 1e-6) pieces.push(subPath(c.pts, hi, L));
        } else {
          const lo = before.length ? before[before.length - 1] : xs[xs.length - 1];
          const hi = after.length ? after[0] : xs[0];
          // queda desde hi hasta lo (dando la vuelta si hace falta)
          const keep = hi < lo ? subPath(c.pts, hi, lo) : subPath(c.pts, hi, L).concat(subPath(c.pts, 0, lo).slice(1));
          pieces.push(keep);
        }
        const repl = pieces.filter((p) => p.length >= 2).map((p) => ({ pts: p, closed: false, kind: "abierto", layer: c.layer }));
        C.splice(hit, 1, ...repl);
        sel.clear();
      });
      flash("Tramo recortado."); return;
    }
    if (tool === "extend") {
      if (c.closed) { flash("Solo se alargan contornos abiertos."); return; }
      const L = lengthOf(c.pts);
      const atEnd = near.s > L / 2;
      const pts = c.pts, n = pts.length;
      const tip = atEnd ? pts[n - 1] : pts[0], prev = atEnd ? pts[n - 2] : pts[1];
      const dir = unit(sub(tip, prev));
      const far = [tip[0] + dir[0] * 1e5, tip[1] + dir[1] * 1e5];
      let best = null;
      C.forEach((o, j) => {
        for (let k = 1; k < o.pts.length; k++) {
          if (j === hit && (atEnd ? k >= n - 1 : k <= 1)) continue;
          const r = segInter(tip, far, o.pts[k - 1], o.pts[k]);
          if (r && r.t * 1e5 > 1e-6 && (!best || r.t < best.t)) best = r;
        }
      });
      if (!best) { flash("No hay ningún contorno en esa dirección."); return; }
      commit("Alargar", () => { if (atEnd) c.pts[n - 1] = best.pt; else c.pts[0] = best.pt; });
      flash(`Alargado ${fmt(best.t * 1e5, 3)} mm.`);
    }
  }
  function cadHover(sx, sy, ev) {
    const touch = ev.pointerType === "touch", tol = touch ? 16 : 7;
    if (CAD_TOOLS[tool].points) {
      cad.cur = pickPoint(sx, sy, ev, tol);
      if (cad.pts.length) {
        const ref = cad.pts[cad.pts.length - 1];
        const d = dist(ref, cad.cur), a = (Math.atan2(cad.cur[1] - ref[1], cad.cur[0] - ref[0]) * 180) / Math.PI;
        flash(`L ${fmt(d, 3)} mm · ${fmt(a, 1)}° · ΔX ${fmt(cad.cur[0] - ref[0])} · ΔY ${fmt(cad.cur[1] - ref[1])}${snapPt ? ` · imán: ${snapPt.kind}` : ""}`);
      } else if (snapPt) flash(`Imán: ${snapPt.kind}`);
    } else if (tool === "nodes") {
      cad.hoverNode = nearestNode(sx, sy, tol + 2);
      hover = cad.hoverNode ? -1 : hitTest(sx, sy, tol);
    } else {
      hover = hitTest(sx, sy, tol);
    }
    draw();
  }
  function cadDrag(sx, sy, ev) {
    const d = drag; d.cx = sx; d.cy = sy;
    if (d.mode === "nodebox") { draw(); return; }
    const far = Math.hypot(sx - d.sx, sy - d.sy) > 3;
    if (!far && !d.moved) return;
    d.moved = true;
    let target = toWorld(sx, sy);
    const sn = snapCfg.obj ? snapAt(sx, sy, 10) : null;
    let dx = target[0] - d.w0[0], dy = target[1] - d.w0[1];
    if (sn && d.orig.length === 1) { dx = sn.p[0] - d.orig[0][2][0]; dy = sn.p[1] - d.orig[0][2][1]; snapPt = sn; } else snapPt = null;
    if (snapCfg.ortho || ev.shiftKey) { if (Math.abs(dx) > Math.abs(dy)) dy = 0; else dx = 0; }
    for (const [i, k, o] of d.orig) {
      const c = C[i], r = ring(c);
      r[k] = [o[0] + dx, o[1] + dy];
      setRing(c, r);
    }
    refreshBoxes();
    flash(`Estirar: ΔX ${fmt(dx)} · ΔY ${fmt(dy)} mm (${d.orig.length} vértice(s))`);
    draw();
  }
  function cadUp(d) {
    snapPt = null;
    if (d.mode === "node" && d.moved) {
      undo.push(d.before); if (undo.length > 80) undo.shift(); redo = [];
      setDirty(true, "Estirar"); scheduleAnalysis(); renderSide();
    } else if (d.mode === "nodebox") {
      const far = Math.hypot(d.cx - d.sx, d.cy - d.sy) > 4;
      if (!d.additive) cad.nodeSel.clear();
      if (far) {
        const [ax, ay] = toWorld(Math.min(d.sx, d.cx), Math.max(d.sy, d.cy)), [bx, by] = toWorld(Math.max(d.sx, d.cx), Math.min(d.sy, d.cy));
        C.forEach((c, i) => ring(c).forEach((q, k) => { if (q[0] >= ax && q[0] <= bx && q[1] >= ay && q[1] <= by) cad.nodeSel.add(`${i}:${k}`); }));
        flash(`${cad.nodeSel.size} vértice(s) seleccionado(s): arrastra uno para estirar.`);
      }
    }
  }
  canvas.addEventListener("dblclick", (ev) => {
    if (tool !== "nodes") return;
    const [sx, sy] = local(ev);
    const nn = nearestNode(sx, sy, 9);
    if (nn) {
      const c = C[nn.i], r = ring(c);
      if (r.length <= (c.closed ? 3 : 2)) { flash("No se pueden quitar más vértices."); return; }
      commit("Quitar vértice", () => { r.splice(nn.k, 1); setRing(c, r); cad.nodeSel.clear(); });
      return;
    }
    const hit = hitTest(sx, sy, 9);
    if (hit < 0) return;
    const near = nearestOn(C[hit], toWorld(sx, sy));
    commit("Añadir vértice", () => { C[hit].pts.splice(near.k, 0, near.q); });
  });

  function cadPaint() {
    // vértices en la herramienta de nodos
    if (tool === "nodes") {
      C.forEach((c, i) => {
        const bb = boxes[i];
        if ((bb[2] - bb[0] + bb[3] - bb[1]) * view.s < 25) return;
        let lx = -1e9, ly = -1e9;
        ring(c).forEach((q, k) => {
          const [x, y] = toScreen(q[0], q[1]);
          if (x < -5 || y < -5 || x > W + 5 || y > H + 5) return;
          const on = cad.nodeSel.has(`${i}:${k}`), hv = cad.hoverNode && cad.hoverNode.i === i && cad.hoverNode.k === k;
          if (!on && !hv && Math.hypot(x - lx, y - ly) < 9) return;   // arcos discretizados: no saturar de nodos
          lx = x; ly = y;
          ctx.fillStyle = on ? colors.sel : colors.bg; ctx.strokeStyle = colors.sel; ctx.lineWidth = 1.2;
          const r = hv ? 5 : 3.5;
          ctx.fillRect(x - r, y - r, 2 * r, 2 * r); ctx.strokeRect(x - r, y - r, 2 * r, 2 * r);
        });
      });
    }
    // goma elástica de las herramientas de dibujo
    if (CAD_TOOLS[tool] && CAD_TOOLS[tool].points && (cad.pts.length || cad.cur)) {
      const pts = cad.pts.slice();
      ctx.strokeStyle = colors.sel; ctx.lineWidth = 1.6; ctx.setLineDash([]);
      let preview = [];
      if (tool === "draw" || tool === "angle") preview = cad.cur ? pts.concat([cad.cur]) : pts;
      else if (tool === "arc") preview = pts.length === 2 && cad.cur ? arc3(pts[0], pts[1], cad.cur) : (cad.cur ? pts.concat([cad.cur]) : pts);
      else if (tool === "dcircle" && pts.length && cad.cur) preview = circlePts(pts[0][0], pts[0][1], Math.max(dist(pts[0], cad.cur), 1e-6));
      else if (tool === "drect" && pts.length && cad.cur) { const a = pts[0], b = cad.cur; preview = [a, [b[0], a[1]], b, [a[0], b[1]], a]; }
      if (preview.length > 1) {
        ctx.beginPath();
        preview.forEach((p, k) => { const [x, y] = toScreen(p[0], p[1]); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
        ctx.stroke();
      }
      ctx.fillStyle = colors.sel;
      for (const p of pts) { const [x, y] = toScreen(p[0], p[1]); ctx.fillRect(x - 3, y - 3, 6, 6); }
      if (cad.cur) {
        const [x, y] = toScreen(cad.cur[0], cad.cur[1]);
        ctx.strokeStyle = colors.text; ctx.lineWidth = 1; ctx.globalAlpha = 0.6;
        ctx.beginPath(); ctx.moveTo(x - 12, y); ctx.lineTo(x + 12, y); ctx.moveTo(x, y - 12); ctx.lineTo(x, y + 12); ctx.stroke();
        ctx.globalAlpha = 1;
      }
    }
    if (cad.angle.length === 3) {
      const [v, a, b] = cad.angle.map((p) => toScreen(p[0], p[1]));
      ctx.strokeStyle = colors.ink; ctx.lineWidth = 1.2; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(v[0], v[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); ctx.setLineDash([]);
    }
    if (cad.brk) {
      const [x, y] = toScreen(cad.brk.q[0], cad.brk.q[1]);
      ctx.strokeStyle = colors.err; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x - 6, y - 6); ctx.lineTo(x + 6, y + 6); ctx.moveTo(x + 6, y - 6); ctx.lineTo(x - 6, y + 6); ctx.stroke();
    }
    if (show.dims && C.length) paintDims(sel.size ? [...sel] : null);
  }
  function paintDims(idx) {
    const [a, b, c, d] = allBox(idx);
    const [x0, y0] = toScreen(a, b), [x1, y1] = toScreen(c, d);
    ctx.strokeStyle = colors.text; ctx.fillStyle = colors.ink; ctx.lineWidth = 1;
    ctx.font = "600 12px system-ui, sans-serif"; ctx.textAlign = "center";
    const arrow = (x, y, ang) => { ctx.save(); ctx.translate(x, y); ctx.rotate(ang); ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(-8, -3); ctx.lineTo(-8, 3); ctx.closePath(); ctx.fill(); ctx.restore(); };
    const label = (txt, x, y) => { const w = ctx.measureText(txt).width + 8; ctx.fillStyle = colors.bg; ctx.fillRect(x - w / 2, y - 9, w, 16); ctx.fillStyle = colors.ink; ctx.fillText(txt, x, y + 4); };
    const yd = y0 + 26;   // cota horizontal bajo la pieza
    ctx.beginPath(); ctx.moveTo(x0, y0 + 4); ctx.lineTo(x0, yd + 5); ctx.moveTo(x1, y0 + 4); ctx.lineTo(x1, yd + 5); ctx.moveTo(x0, yd); ctx.lineTo(x1, yd); ctx.stroke();
    arrow(x0, yd, Math.PI); arrow(x1, yd, 0); label(`${fmt(c - a, 2)}`, (x0 + x1) / 2, yd);
    const xd = x1 + 26;   // cota vertical a la derecha
    ctx.beginPath(); ctx.moveTo(x1 + 4, y0); ctx.lineTo(xd + 5, y0); ctx.moveTo(x1 + 4, y1); ctx.lineTo(xd + 5, y1); ctx.moveTo(xd, y0); ctx.lineTo(xd, y1); ctx.stroke();
    arrow(xd, y1, -Math.PI / 2); arrow(xd, y0, Math.PI / 2);
    ctx.save(); ctx.translate(xd, (y0 + y1) / 2); ctx.rotate(-Math.PI / 2); label(`${fmt(d - b, 2)}`, 0, 0); ctx.restore();
    // diámetros de los círculos seleccionados
    if (idx) for (const i of idx) {
      const circ = circleOf(C[i]); if (!circ) continue;
      const [cx, cy] = toScreen(circ.cx, circ.cy), r = circ.r * view.s;
      if (r < 6) continue;
      ctx.strokeStyle = colors.ink; ctx.beginPath(); ctx.moveTo(cx - r * 0.707, cy + r * 0.707); ctx.lineTo(cx + r * 0.707 + 18, cy - r * 0.707 - 18); ctx.stroke();
      ctx.textAlign = "left"; label(`Ø${fmt(circ.r * 2, 2)}`, cx + r * 0.707 + 40, cy - r * 0.707 - 18); ctx.textAlign = "center";
    }
  }

  // ---------- operaciones de forma en el servidor, matrices, capas, limpieza y exportación
  async function serverOp(op, extra, label) {
    const idx = (sel.size ? [...sel] : []).filter((i) => C[i].layer !== "marcado");
    if (!idx.length) { flash("Selecciona primero los contornos cerrados."); return; }
    try {
      $("ed-state").textContent = "Calculando…";
      const res = await api(`/piezas/${PART}/geometria/operacion`, { op, ...extra, contours: idx.map((i) => ({ points: rnd(C[i].pts), closed: C[i].closed })) });
      commit(label, () => {
        const keep = extra.keep;
        const drop = new Set(keep ? [] : idx);
        C = C.filter((_, i) => !drop.has(i));
        const start = C.length;
        res.contours.forEach((c) => C.push({ pts: c.points, closed: c.closed, kind: "exterior", layer: "corte" }));
        sel = new Set(res.contours.map((_, k) => start + k));
      });
      flash(`${label}: ${res.contours.length} contorno(s) resultante(s).`);
    } catch (e) { setDirty(dirty, e.message); flash(e.message); }
  }
  Object.assign(actions, {
    offset() {
      if (!sel.size) { flash("Selecciona los contornos (cerrados) a los que quieres aplicar la equidistancia."); return; }
      ask("Equidistancia (offset)", [
        { name: "d", label: "Distancia", unit: "mm", value: 1, min: -1000, max: 1000, help: "Positiva hacia fuera de la pieza (crece); negativa hacia dentro (encoge). Los agujeros se comportan al revés, como en el corte." },
        { name: "j", label: "Esquinas", value: "redondo", options: [["redondo", "Redondeadas"], ["inglete", "Vivas (inglete)"], ["chaflan", "Achaflanadas"]] },
        { name: "k", label: "Original", value: "0", options: [["0", "Sustituir"], ["1", "Conservar y añadir la copia"]] }],
      (v) => { if (v.d === 0) return "La distancia no puede ser 0."; serverOp("offset", { distance: v.d, join: v.j, keep: v.k === "1" }, "Equidistancia"); });
    },
    union() { serverOp("union", {}, "Unir formas"); },
    diff() { serverOp("diferencia", {}, "Restar formas"); },
    inter() { serverOp("interseccion", {}, "Intersección"); },
    arrayrect() {
      if (!sel.size) { flash("Selecciona primero lo que quieres repetir."); return; }
      const b = allBox([...sel]);
      ask("Matriz rectangular", [{ name: "nx", label: "Columnas", value: 3, min: 1, max: 100 }, { name: "ny", label: "Filas", value: 1, min: 1, max: 100 },
        { name: "dx", label: "Paso X", unit: "mm", value: Math.round((b[2] - b[0] + 10) * 100) / 100 }, { name: "dy", label: "Paso Y", unit: "mm", value: Math.round((b[3] - b[1] + 10) * 100) / 100 }], (v) => {
        const nx = Math.round(v.nx), ny = Math.round(v.ny), src = [...sel];
        if (nx * ny * src.length > 3000) return "Demasiadas copias.";
        commit("Matriz rectangular", () => {
          const added = [...src];
          for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) {
            if (!i && !j) continue;
            for (const s of src) { C.push({ ...C[s], pts: C[s].pts.map(([x, y]) => [x + v.dx * i, y + v.dy * j]) }); added.push(C.length - 1); }
          }
          sel = new Set(added);
        });
      });
    },
    arraypolar() {
      if (!sel.size) { flash("Selecciona primero lo que quieres repetir."); return; }
      const all = centerOf();
      ask("Matriz polar", [{ name: "n", label: "Nº total de elementos", value: 6, min: 2, max: 360 }, { name: "a", label: "Ángulo total", unit: "°", value: 360, min: -360, max: 360 },
        { name: "x", label: "Centro X", unit: "mm", value: Math.round(all[0] * 100) / 100 }, { name: "y", label: "Centro Y", unit: "mm", value: Math.round(all[1] * 100) / 100 }], (v) => {
        const n = Math.round(v.n), src = [...sel];
        if (n * src.length > 3000) return "Demasiadas copias.";
        const step = Math.abs(v.a) >= 360 ? (2 * Math.PI) / n : ((v.a * Math.PI) / 180) / (n - 1);
        commit("Matriz polar", () => {
          const added = [...src];
          for (let k = 1; k < n; k++) {
            const co = Math.cos(step * k), si = Math.sin(step * k);
            for (const s of src) { C.push({ ...C[s], pts: C[s].pts.map(([x, y]) => [v.x + (x - v.x) * co - (y - v.y) * si, v.y + (x - v.x) * si + (y - v.y) * co]) }); added.push(C.length - 1); }
          }
          sel = new Set(added);
        });
      });
    },
    dedupe() {
      const sig = (c) => { const r = ring(c).map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`); const s = [...r].sort().join("|"); return `${c.closed}:${s}`; };
      const seen = new Set(), drop = new Set();
      C.forEach((c, i) => { const k = sig(c); if (seen.has(k)) drop.add(i); else seen.add(k); });
      if (!drop.size) { flash("No hay contornos duplicados."); return; }
      commit("Quitar duplicados", () => { C = C.filter((_, i) => !drop.has(i)); sel.clear(); });
      flash(`${drop.size} contorno(s) duplicado(s) eliminado(s).`);
    },
    tomark() {
      if (!sel.size) { flash("Selecciona lo que quieres pasar a marcado."); return; }
      commit("Capa marcado", () => sel.forEach((i) => { C[i].layer = "marcado"; }));
      flash("Pasado a la capa de marcado: no cuenta como corte ni perforación.");
    },
    tocut() {
      if (!sel.size) { flash("Selecciona lo que quieres pasar a corte."); return; }
      commit("Capa corte", () => sel.forEach((i) => { C[i].layer = "corte"; }));
    },
    selmark() { sel = new Set(C.map((c, i) => (c.layer === "marcado" ? i : -1)).filter((i) => i >= 0)); draw(); renderSide(); },
    export(fmtExt) {
      if (dirty) { flash("Guarda primero los cambios: la descarga usa la geometría guardada."); return; }
      window.location.href = `/piezas/${PART}/plano.${fmtExt || "dxf"}`;
    },
    exportdxf() { actions.export("dxf"); },
    exportdwg() { actions.export("dwg"); },
  });

  function setTool(t) {
    if (CAD_TOOLS[t] && CAD_TOOLS[t].ask && !CAD_TOOLS[t].asked) { CAD_TOOLS[t].ask(() => setTool(t)); return; }
    tool = t; measure = null; snapPt = null; cadReset();
    document.querySelectorAll("[data-tool]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tool === t)));
    canvas.dataset.tool = t;
    flash({ select: "Clic para seleccionar; arrastra lo seleccionado para moverlo.",
      measure: "Pulsa dos puntos (imán a vértices, puntos medios y centros).",
      start: "Pulsa sobre un contorno para fijar dónde perfora y empieza a cortar." }[t] || CAD_TOOLS[t]?.hint || "");
    $("ed-cmd-wrap").hidden = !(CAD_TOOLS[t] && CAD_TOOLS[t].points);
    draw();
  }

  // ------------------------------------------------------------------ eventos de la barra
  app.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
  app.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => actions[b.dataset.act]?.()));
  app.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.toggle; show[k] = !show[k]; b.setAttribute("aria-pressed", String(show[k])); draw();
  }));
  // menús desplegables: se cierran al elegir una opción, al abrir otro o al pulsar fuera
  const menus = [...app.querySelectorAll(".ed-menu")];
  menus.forEach((m) => {
    m.addEventListener("toggle", () => { if (m.open) menus.forEach((o) => { if (o !== m) o.open = false; }); });
    m.querySelectorAll(".ed-menu-pop button").forEach((b) => b.addEventListener("click", () => { m.open = false; }));
  });
  document.addEventListener("pointerdown", (ev) => { if (!ev.target.closest(".ed-menu")) menus.forEach((m) => (m.open = false)); });

  $("ed-issues").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-issue]"); if (!b) return;
    const it = issues[+b.dataset.issue];
    sel = new Set(it.contours.filter((i) => i < C.length));
    if (sel.size) fit([...sel]); else { view.cx = it.x; view.cy = it.y; draw(); }
    renderSide();
  });
  $("ed-list").addEventListener("click", (ev) => {
    const tr = ev.target.closest("[data-row]"); if (!tr) return;
    const i = +tr.dataset.row;
    if (ev.shiftKey || ev.ctrlKey || ev.metaKey) { sel.has(i) ? sel.delete(i) : sel.add(i); } else sel = new Set([i]);
    draw(); renderSide();
  });
  $("ed-list").addEventListener("dblclick", (ev) => { const tr = ev.target.closest("[data-row]"); if (tr) fit([+tr.dataset.row]); });
  ["ed-thick", "ed-family"].forEach((id) => $(id).addEventListener("change", () => {
    try { localStorage.setItem("ed-rules", JSON.stringify(rulesBody())); } catch { /* sin almacenamiento */ }
    scheduleAnalysis();
  }));
  $("ed-thick").addEventListener("input", scheduleAnalysis);
  try {
    const saved = JSON.parse(localStorage.getItem("ed-rules") || "null");
    if (saved) { $("ed-thick").value = String(saved.thickness_mm).replace(".", ","); $("ed-family").value = saved.family; }
  } catch { /* sin almacenamiento */ }
  document.querySelectorAll("form[data-confirm]").forEach((f) => f.addEventListener("submit", (ev) => {
    if (!window.confirm(f.dataset.confirm)) ev.preventDefault(); else dirty = false;
  }));

  // ------------------------------------------------------------------ puntero (ratón, lápiz y táctil)
  const pointers = new Map();
  let pinch = null;
  const local = (ev) => { const r = canvas.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
  canvas.addEventListener("contextmenu", (ev) => ev.preventDefault());
  canvas.addEventListener("pointerdown", (ev) => {
    canvas.focus({ preventScroll: true });
    canvas.setPointerCapture(ev.pointerId);
    const [sx, sy] = local(ev);
    pointers.set(ev.pointerId, [sx, sy]);
    if (pointers.size === 2) {           // pellizco: zoom + desplazamiento
      const [a, b] = [...pointers.values()];
      pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] };
      if (drag && drag.mode === "move" && drag.moved) { restore(drag.before); refreshBoxes(); }
      drag = null; return;
    }
    const touch = ev.pointerType === "touch";
    const tolPx = touch ? 16 : 7;
    if (ev.button === 1 || ev.button === 2) { drag = { mode: "pan", sx, sy, cx: sx, cy: sy, v: { ...view } }; return; }
    if (tool === "measure") {
      const sn = snapAt(sx, sy, tolPx * 1.6);
      const p = sn ? [...sn.p] : toWorld(sx, sy);
      if (!measure || measure.b) measure = { a: p, b: null, cur: null };
      else { measure.b = p; showMeasure(); }
      draw(); return;
    }
    if (tool === "start") { setStart(sx, sy); return; }
    if (CAD_TOOLS[tool]) { cadDown(sx, sy, ev, tolPx); return; }
    const hit = hitTest(sx, sy, tolPx);
    const additive = ev.shiftKey || ev.ctrlKey || ev.metaKey;
    if (hit >= 0 && sel.has(hit) && !additive) {
      drag = { mode: "move", sx, sy, cx: sx, cy: sy, w0: toWorld(sx, sy), before: snapshot(), moved: false, idx: [...sel], orig: [...sel].map((i) => clonePts(C[i].pts)) };
    } else if (hit >= 0) {
      if (additive) { sel.has(hit) ? sel.delete(hit) : sel.add(hit); } else sel = new Set([hit]);
      drag = touch ? null : { mode: "move", sx, sy, cx: sx, cy: sy, w0: toWorld(sx, sy), before: snapshot(), moved: false, idx: [...sel], orig: [...sel].map((i) => clonePts(C[i].pts)), fresh: true };
      renderSide(); draw();
    } else if (touch) {
      drag = { mode: "pan", sx, sy, cx: sx, cy: sy, v: { ...view }, tap: true };
    } else {
      drag = { mode: "box", sx, sy, cx: sx, cy: sy, additive };
    }
  });
  canvas.addEventListener("pointermove", (ev) => {
    const [sx, sy] = local(ev);
    if (pointers.has(ev.pointerId)) pointers.set(ev.pointerId, [sx, sy]);
    const w = toWorld(sx, sy);
    $("ed-coords").textContent = `X ${fmt(w[0])} · Y ${fmt(w[1])}`;
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]), mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      view.cx -= (mid[0] - pinch.mid[0]) / view.s; view.cy += (mid[1] - pinch.mid[1]) / view.s;
      if (pinch.d > 0) zoomAt(mid[0], mid[1], d / pinch.d);
      pinch = { d, mid }; draw(); return;
    }
    if (!drag && CAD_TOOLS[tool]) { cadHover(sx, sy, ev); return; }
    if (drag && (drag.mode === "node" || drag.mode === "nodebox")) { cadDrag(sx, sy, ev); return; }
    if (!drag) {
      if (tool === "measure") {
        snapPt = snapAt(sx, sy, ev.pointerType === "touch" ? 25 : 11);
        if (measure && !measure.b) measure.cur = snapPt ? snapPt.p : w;
        if (measure && !measure.b) showMeasure(true);
        draw();
      } else if (ev.pointerType !== "touch") {
        const h = hitTest(sx, sy, 7);
        if (h !== hover) { hover = h; draw(); }
      }
      return;
    }
    drag.cx = sx; drag.cy = sy;
    const far = Math.hypot(sx - drag.sx, sy - drag.sy) > 4;
    if (drag.mode === "pan") {
      autoFit = false;
      view.cx = drag.v.cx - (sx - drag.sx) / view.s; view.cy = drag.v.cy + (sy - drag.sy) / view.s;
      if (far) drag.tap = false;
      draw();
    } else if (drag.mode === "move" && (far || drag.moved)) {
      drag.moved = true;
      let dx = w[0] - drag.w0[0], dy = w[1] - drag.w0[1];
      if (ev.shiftKey) { if (Math.abs(dx) > Math.abs(dy)) dy = 0; else dx = 0; }
      drag.idx.forEach((i, k) => { C[i].pts = drag.orig[k].map(([x, y]) => [x + dx, y + dy]); });
      refreshBoxes();
      flash(`Mover: ΔX ${fmt(dx)} · ΔY ${fmt(dy)} mm`);
      draw();
    } else if (drag.mode === "box") draw();
  });
  function endPointer(ev) {
    pointers.delete(ev.pointerId);
    if (pointers.size < 2) pinch = null;
    if (!drag) return;
    const d = drag; drag = null;
    if (d.mode === "node" || d.mode === "nodebox") { cadUp(d); draw(); return; }
    if (d.mode === "move" && d.moved) {
      undo.push(d.before); if (undo.length > 80) undo.shift(); redo = [];
      setDirty(true, "Mover"); scheduleAnalysis(); renderSide();
    } else if (d.mode === "box") {
      const far = Math.hypot(d.cx - d.sx, d.cy - d.sy) > 4;
      if (!far) { if (!d.additive) sel.clear(); }
      else {
        const [ax, ay] = toWorld(Math.min(d.sx, d.cx), Math.max(d.sy, d.cy)), [bx, by] = toWorld(Math.max(d.sx, d.cx), Math.min(d.sy, d.cy));
        const crossing = d.cx < d.sx;
        if (!d.additive) sel.clear();
        boxes.forEach((bb, i) => {
          const inside = bb[0] >= ax && bb[2] <= bx && bb[1] >= ay && bb[3] <= by;
          const overlap = bb[0] <= bx && bb[2] >= ax && bb[1] <= by && bb[3] >= ay;
          if (inside || (crossing && overlap && C[i].pts.some((p) => p[0] >= ax && p[0] <= bx && p[1] >= ay && p[1] <= by))) sel.add(i);
        });
      }
      renderSide();
    } else if (d.mode === "pan" && d.tap) { sel.clear(); renderSide(); }
    draw();
  }
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);
  canvas.addEventListener("pointerleave", () => { if (hover >= 0) { hover = -1; draw(); } });
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const [sx, sy] = local(ev);
    zoomAt(sx, sy, Math.pow(1.0015, -ev.deltaY * (ev.deltaMode === 1 ? 33 : 1)));
  }, { passive: false });

  function showMeasure(live) {
    const b = measure.b || measure.cur; if (!b) return;
    const dx = b[0] - measure.a[0], dy = b[1] - measure.a[1];
    const ang = (Math.atan2(dy, dx) * 180) / Math.PI;
    flash(`${live ? "" : "Medida: "}${fmt(Math.hypot(dx, dy), 3)} mm · ΔX ${fmt(Math.abs(dx), 3)} · ΔY ${fmt(Math.abs(dy), 3)} · ${fmt(ang, 1)}°${snapPt && live ? ` · imán: ${snapPt.kind}` : ""}`);
  }

  // ------------------------------------------------------------------ teclado
  document.addEventListener("keydown", (ev) => {
    if (dlg.open) return;
    const t = ev.target;
    if (t && t.id === "ed-cmd") return;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
    const k = ev.key.toLowerCase(), mod = ev.ctrlKey || ev.metaKey;
    if (mod && k === "z") { ev.preventDefault(); ev.shiftKey ? doRedo() : doUndo(); return; }
    if (mod && k === "y") { ev.preventDefault(); doRedo(); return; }
    if (mod && k === "s") { ev.preventDefault(); save(LOCKED); return; }
    if (mod && k === "a") { ev.preventDefault(); actions.selall(); return; }
    if (mod) return;
    if (k === "delete" || k === "backspace") { ev.preventDefault(); actions.delete(); }
    else if (k === "escape") { if (cad.pts.length) { cadFinish(); return; } sel.clear(); measure = null; setTool("select"); renderSide(); }
    else if (k === "enter" && cad.pts.length) cadFinish();
    else if (k === "l") setTool("draw");
    else if (k === "n") setTool("nodes");
    else if (k === "t") setTool("trim");
    else if (k === "e") setTool("extend");
    else if (k === "o") actions.offset();
    else if (k === "f8") { ev.preventDefault(); toggleSnap("ortho"); }
    else if (k === "f3") { ev.preventDefault(); toggleSnap("obj"); }
    else if (k === "f9") { ev.preventDefault(); toggleSnap("grid"); }
    else if (k === "v") setTool("select");
    else if (k === "m") setTool("measure");
    else if (k === "p") setTool("start");
    else if (k === "c" && !CAD_TOOLS[tool]) actions.close();
    else if (k === "j") actions.join();
    else if (k === "r") actions.reverse();
    else if (k === "f" && !ev.shiftKey) fit();
    else if (k === "+" || k === "=") actions.zoomin();
    else if (k === "-") actions.zoomout();
    else if (k.startsWith("arrow") && sel.size) {
      ev.preventDefault();
      const st = ev.shiftKey ? 10 : 1;
      const [dx, dy] = { arrowleft: [-st, 0], arrowright: [st, 0], arrowup: [0, st], arrowdown: [0, -st] }[k];
      applyTransform("Mover", ([x, y]) => [x + dx, y + dy]);
    }
  });

  window.addEventListener("beforeunload", (ev) => { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });
  new ResizeObserver(resize).observe(canvas);

  // ------------------------------------------------------------------ arranque
  (async () => {
    try {
      const data = await api(`/piezas/${PART}/geometria.json`);
      C = data.contours.map((c) => ({ pts: c.points, closed: c.closed, kind: c.kind, layer: c.layer || "corte" }));
      refreshBoxes();
      resize(); fit(); setTool("select"); setDirty(false, "Sin cambios");
      renderSide(); runAnalysis();
    } catch (e) {
      $("ed-state").textContent = e.message;
    }
  })();
})();
