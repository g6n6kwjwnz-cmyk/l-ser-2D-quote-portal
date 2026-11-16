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
  const ARC_TOL = 0.01;          // mm, igual que el análisis del DXF

  // ------------------------------------------------------------------ estado
  /** contornos: {pts: [[x,y],...], closed: bool, kind: string}. Cerrado ⇒ último punto = primero. */
  let C = [];
  let boxes = [];                 // envolventes por contorno [minx, miny, maxx, maxy]
  let sel = new Set();
  let issues = [];
  let metrics = null, baseMetrics = null;
  let tool = "select";
  const show = { grid: true, dirs: false, nums: false, pierce: true, nodes: false };
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
      ink: v("--text", "#16191f"),
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
  function resize() {
    const r = canvas.getBoundingClientRect();
    dpr = window.devicePixelRatio || 1;
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    draw();
  }
  const toScreen = (x, y) => [(x - view.cx) * view.s + W / 2, H / 2 - (y - view.cy) * view.s];
  const toWorld = (sx, sy) => [(sx - W / 2) / view.s + view.cx, (H / 2 - sy) / view.s + view.cy];
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
      ctx.strokeStyle = colors[c.kind] || colors.exterior;
      ctx.lineWidth = isSel ? 2.2 : 1.4;
      if (!c.closed) ctx.setLineDash([6, 4]);
      ctx.stroke(); ctx.setLineDash([]);
      if (show.nodes && view.s * (bb[2] - bb[0] + bb[3] - bb[1]) > 30) {
        ctx.fillStyle = ctx.strokeStyle;
        for (const p of c.pts) { const [sx, sy] = toScreen(p[0], p[1]); ctx.fillRect(sx - 1.5, sy - 1.5, 3, 3); }
      }
      if (show.pierce) {
        const [sx, sy] = toScreen(c.pts[0][0], c.pts[0][1]);
        ctx.fillStyle = colors.pierce; ctx.beginPath(); ctx.arc(sx, sy, isSel ? 4 : 3, 0, 2 * Math.PI); ctx.fill();
      }
      if (show.dirs) paintArrows(c);
      if (!c.closed) {   // extremos de los abiertos
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
    // ventana de selección
    if (drag && drag.mode === "box") {
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
  const snapshot = () => JSON.stringify(C.map((c) => [c.pts, c.closed, c.kind]));
  function restore(s) {
    C = JSON.parse(s).map(([pts, closed, kind]) => ({ pts, closed, kind }));
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
  const payload = () => C.map((c) => ({ points: c.pts.map(([x, y]) => [Math.round(x * 1e4) / 1e4, Math.round(y * 1e4) / 1e4]), closed: c.closed }));
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
      const res = await api(`/piezas/${PART}/geometria/analizar`, { contours: payload(), ...rulesBody() });
      if (seq !== analysisSeq) return;     // llegó una respuesta antigua
      metrics = res.metrics; issues = res.issues;
      if (!baseMetrics) baseMetrics = metrics;
      if (res.kinds.length === C.length) res.kinds.forEach((k, i) => (C[i].kind = k));
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
      C = res.contours.map((c) => ({ pts: c.points, closed: c.closed, kind: c.kind }));
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
      `<td><span class="sw ${c.kind}"></span>${c.kind}</td><td class="num">${fmt(lengthOf(c.pts), 1)}</td></tr>`).join("");
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
    zoomout() { zoomAt(W / 2, H / 2, 1 / 1.4); },
    undo: doUndo,
    redo: doRedo,
    save() { save(false); },
    saveas() { save(true); },
  };

  function setTool(t) {
    tool = t; measure = null; snapPt = null;
    document.querySelectorAll("[data-tool]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tool === t)));
    canvas.dataset.tool = t;
    flash({ select: "Clic para seleccionar; arrastra lo seleccionado para moverlo.",
      measure: "Pulsa dos puntos (imán a vértices, puntos medios y centros).",
      start: "Pulsa sobre un contorno para fijar dónde perfora y empieza a cortar." }[t]);
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
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
    const k = ev.key.toLowerCase(), mod = ev.ctrlKey || ev.metaKey;
    if (mod && k === "z") { ev.preventDefault(); ev.shiftKey ? doRedo() : doUndo(); return; }
    if (mod && k === "y") { ev.preventDefault(); doRedo(); return; }
    if (mod && k === "s") { ev.preventDefault(); save(LOCKED); return; }
    if (mod && k === "a") { ev.preventDefault(); actions.selall(); return; }
    if (mod) return;
    if (k === "delete" || k === "backspace") { ev.preventDefault(); actions.delete(); }
    else if (k === "escape") { sel.clear(); measure = null; setTool("select"); renderSide(); }
    else if (k === "v") setTool("select");
    else if (k === "m") setTool("measure");
    else if (k === "p") setTool("start");
    else if (k === "c") actions.close();
    else if (k === "j") actions.join();
    else if (k === "r") actions.reverse();
    else if (k === "f") fit();
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
      C = data.contours.map((c) => ({ pts: c.points, closed: c.closed, kind: c.kind }));
      refreshBoxes();
      resize(); fit(); setTool("select"); setDirty(false, "Sin cambios");
      renderSide(); runAnalysis();
    } catch (e) {
      $("ed-state").textContent = e.message;
    }
  })();
})();
