// Editor de anidado tipo CAD. El navegador dibuja y mueve piezas; el servidor valida separaciones, calcula
// aprovechamiento y orden de corte, y hace las operaciones geométricas (compactar, imán, colocar pendientes…).
(() => {
  "use strict";
  const app = document.getElementById("nest-app");
  if (!app) return;
  const D = JSON.parse(app.dataset.nest);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("nest-svg");
  const wrap = document.getElementById("svg-wrap");
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 1) => Number(v).toLocaleString("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d });
  const dur = (s) => { if (s == null) return "—"; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min ${String(r).padStart(2, "0")} s`; };
  const editable = D.editable;
  if (!editable) document.querySelectorAll("[data-edit]").forEach((el) => (el.hidden = true));

  // colores por pieza (paleta legible en claro y oscuro)
  const PALETTE = [["#bfdbfe", "#1d4ed8"], ["#bbf7d0", "#15803d"], ["#fde68a", "#b45309"], ["#fbcfe8", "#be185d"],
    ["#c7d2fe", "#4338ca"], ["#a5f3fc", "#0e7490"], ["#fed7aa", "#c2410c"], ["#e9d5ff", "#7e22ce"], ["#d9f99d", "#4d7c0f"], ["#fecdd3", "#be123c"]];
  const colorOf = {};
  D.items.forEach((it, i) => (colorOf[it.part_id] = PALETTE[i % PALETTE.length]));

  let sheets = clone(D.sheets);
  let ev = D.eval;
  let cur = 0;
  let sel = [];
  let dirty = false;
  let checkTimer = null;
  let view = null;          // viewBox {x, y, w, h} en coordenadas SVG (eje Y hacia abajo, chapa volteada)
  let tool = "select";      // select | hand | measure
  let showOrder = false, showGrid = false, showLabels = false;
  let clipboard = null;     // {items:[{key,rotation,dx,dy}], w, h}
  let lastWorld = null;     // última posición del puntero sobre la chapa (para pegar)
  let measure = null;
  let spaceDown = false;
  const undo = [], redo = [];

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };
  const H = () => (sheets[cur] ? sheets[cur].source.h : 0);

  // ---------------------------------------------------------------- geometría (giro igual que el servidor)
  const shapeCache = new Map();
  function shapeFor(key, rot) {
    const ck = key + "@" + rot;
    if (shapeCache.has(ck)) return shapeCache.get(ck);
    const part = D.parts[key];
    const a = (rot * Math.PI) / 180, c = Math.cos(a), s = Math.sin(a);
    const rings = part.rings.map((r) => r.map(([x, y]) => [x * c - y * s, x * s + y * c]));
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    rings.forEach((r) => r.forEach(([x, y]) => { minx = Math.min(minx, x); miny = Math.min(miny, y); maxx = Math.max(maxx, x); maxy = Math.max(maxy, y); }));
    const d = rings.map((r) => "M" + r.map(([x, y]) => `${(x - minx).toFixed(2)} ${(y - miny).toFixed(2)}`).join("L") + "Z").join("");
    const out = { d, w: maxx - minx, h: maxy - miny };
    shapeCache.set(ck, out);
    return out;
  }
  const bbox = (p) => { const s = shapeFor(p.key, p.rotation); return { x0: p.x, y0: p.y, x1: p.x + s.w, y1: p.y + s.h }; };

  // ---------------------------------------------------------------- historial
  function snapshot() { undo.push(JSON.stringify(sheets)); if (undo.length > 100) undo.shift(); redo.length = 0; }
  function restore(from, to) {
    if (!from.length) return;
    to.push(JSON.stringify(sheets));
    sheets = JSON.parse(from.pop());
    cur = Math.min(cur, sheets.length - 1); sel = [];
    render(); scheduleCheck();
  }
  function setDirty(v) {
    dirty = v;
    $("save-state").textContent = v ? "Cambios sin guardar" : "";
    const c = $("btn-confirm");
    if (c) c.disabled = v || (ev && ev.conflicts > 0);
  }
  const say = (t) => { $("save-state").textContent = t; };

  // ---------------------------------------------------------------- vista: zoom, encuadre, reglas
  function aspect() { const r = wrap.getBoundingClientRect(); return r.width > 0 && r.height > 0 ? r.height / r.width : 0.5; }
  function fitBox(x0, y0, x1, y1, pad = 0.04) {
    const w = (x1 - x0) * (1 + 2 * pad) || 100, h = (y1 - y0) * (1 + 2 * pad) || 100, a = aspect();
    const vw = Math.max(w, h / a), vh = vw * a;
    view = { x: (x0 + x1) / 2 - vw / 2, y: (y0 + y1) / 2 - vh / 2, w: vw, h: vh };
  }
  function fitView() { const sh = sheets[cur]; if (sh) fitBox(0, 0, sh.source.w, sh.source.h); }
  function fitSelection() {
    const ps = selected(); if (!ps.length) { fitView(); return; }
    const bs = ps.map(bbox), h = H();
    fitBox(Math.min(...bs.map((b) => b.x0)), h - Math.max(...bs.map((b) => b.y1)), Math.max(...bs.map((b) => b.x1)), h - Math.min(...bs.map((b) => b.y0)), 0.3);
    applyView();
  }
  function scale() { return wrap.getBoundingClientRect().width / view.w; }   // px por mm
  function applyView() {
    if (![view.x, view.y, view.w, view.h].every(Number.isFinite) || view.w <= 0 || view.h <= 0) fitView();
    svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
    const sh = sheets[cur];
    if (sh) $("zoom-pct").textContent = Math.round((100 * (sh.source.w * 1.08)) / view.w) + " %";
    drawRulers(); drawGrid();
  }
  function zoomAt(k, fx = 0.5, fy = 0.5) {
    if (![k, fx, fy, view.w, view.h].every(Number.isFinite) || view.w <= 0) return;
    const nw = Math.min(Math.max(view.w * k, 5), 200000), nh = nw * (view.h / view.w);
    view.x += (view.w - nw) * fx; view.y += (view.h - nh) * fy; view.w = nw; view.h = nh;
    applyView();
  }
  function niceStep(pxPerMm, minPx) {
    const raw = minPx / pxPerMm, p = Math.pow(10, Math.floor(Math.log10(raw)));
    return [1, 2, 5, 10].map((m) => m * p).find((s) => s >= raw);
  }
  function drawRulers() {
    const rx = $("ruler-x"), ry = $("ruler-y");
    const r = wrap.getBoundingClientRect(); if (!r.width) return;
    const k = scale(), step = niceStep(k, 70), minor = step / 5, h = H();
    rx.setAttribute("viewBox", `0 0 ${r.width} 24`); ry.setAttribute("viewBox", `0 0 24 ${r.height}`);
    let ox = "", oy = "";
    for (let v = Math.floor(view.x / minor) * minor; v <= view.x + view.w; v += minor) {
      const px = (v - view.x) * k, major = Math.abs(v / step - Math.round(v / step)) < 1e-6;
      ox += `<line x1="${px}" x2="${px}" y1="${major ? 10 : 18}" y2="24"/>` + (major ? `<text x="${px + 3}" y="10">${Math.round(v)}</text>` : "");
    }
    // eje Y: el SVG está volteado; el valor mostrado es la Y de la chapa (0 abajo)
    const y0w = h - (view.y + view.h), y1w = h - view.y;
    for (let v = Math.floor(y0w / minor) * minor; v <= y1w; v += minor) {
      const py = (h - v - view.y) * k, major = Math.abs(v / step - Math.round(v / step)) < 1e-6;
      oy += `<line y1="${py}" y2="${py}" x1="${major ? 10 : 18}" x2="24"/>` + (major ? `<text x="11" y="${py - 3}" transform="rotate(-90 11 ${py - 3})">${Math.round(v)}</text>` : "");
    }
    rx.innerHTML = ox; ry.innerHTML = oy;
  }
  function drawGrid() {
    const g = $("grid"); if (!g) return;
    g.innerHTML = "";
    if (!showGrid || !sheets[cur]) return;
    const { w, h } = sheets[cur].source, step = niceStep(scale(), 28);
    let s = "";
    for (let x = 0; x <= w + 1e-6; x += step) s += `<line class="${Math.round(x / step) % 5 === 0 ? "major" : ""}" x1="${x}" y1="0" x2="${x}" y2="${h}"/>`;
    for (let y = 0; y <= h + 1e-6; y += step) s += `<line class="${Math.round(y / step) % 5 === 0 ? "major" : ""}" x1="0" y1="${y}" x2="${w}" y2="${y}"/>`;
    g.innerHTML = s;
  }

  // ---------------------------------------------------------------- dibujo
  function render() {
    const sh = sheets[cur];
    svg.innerHTML = "";
    if (!sh) return;
    if (!view) fitView();
    const W = sh.source.w, Hh = sh.source.h;
    const defs = el("defs", {}, svg);
    const pat = el("pattern", { id: "hatch", width: 24, height: 24, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
    el("rect", { width: 24, height: 24, fill: "#d1fae5", "fill-opacity": 0.55 }, pat);
    el("line", { x1: 0, y1: 0, x2: 0, y2: 24, stroke: "#1b7f4b", "stroke-width": 3, "stroke-opacity": 0.35 }, pat);
    const g = el("g", { transform: `translate(0 ${Hh}) scale(1 -1)`, id: "world" }, svg);
    el("rect", { x: 0, y: 0, width: W, height: Hh, class: "sheet", fill: "#e9ecef", stroke: "#6b7280" }, g);
    el("g", { id: "grid", class: "grid" }, g);
    el("rect", { x: D.edge, y: D.edge, width: Math.max(0, W - 2 * D.edge), height: Math.max(0, Hh - 2 * D.edge), class: "sheet-usable", fill: "none", stroke: "#6b7280" }, g);
    const info = ev && ev.sheets[cur];
    if (info && info.offcut) {
      const o = info.offcut;
      el("rect", { x: o.x, y: o.y, width: o.w, height: o.h, class: "offcut", fill: "#d1fae5", stroke: "#1b7f4b" }, g);
    }
    const bad = new Set(info ? info.conflicts : []);
    sh.placements.forEach((p, i) => {
      if (!D.parts[p.key]) return;
      const s = shapeFor(p.key, p.rotation), [fill, stroke] = colorOf[p.key] || PALETTE[0];
      const cls = "piece" + (bad.has(i) ? " bad" : "") + (sel.includes(i) ? " sel" : "") + (p.locked ? " locked" : "");
      const pg = el("g", { transform: `translate(${p.x} ${p.y})`, class: cls, "data-i": i }, g);
      el("path", { d: s.d, "fill-rule": "evenodd", style: `fill:${fill};stroke:${stroke}` }, pg);
    });
    if (showLabels) {
      const minFs = Math.max(W, Hh) / 400;   // más pequeño no se lee ni con zoom: se omite
      sh.placements.forEach((p) => {
        if (!D.parts[p.key]) return;
        const b = bbox(p), bw = b.x1 - b.x0, bh = b.y1 - b.y0;
        let name = D.parts[p.key].name.replace(/\.(dxf|dwg)$/i, "");
        if (name.length > 16) name = name.slice(0, 15) + "…";
        const fs = Math.min(bh / 3, (bw * 0.9) / (name.length * 0.58), 40);
        if (fs < minFs) return;
        const t = el("text", { x: (b.x0 + b.x1) / 2, y: -(b.y0 + b.y1) / 2, transform: "scale(1 -1)", class: "piece-label", "font-size": fs,
          "text-anchor": "middle", "dominant-baseline": "central" }, g);
        t.textContent = name;
      });
    }
    if (showOrder && info && info.plan && info.plan.order.length) {
      const pts = info.plan.order.filter((i) => sh.placements[i]).map((i) => { const b = bbox(sh.placements[i]); return [(b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2]; });
      el("polyline", { points: [[0, 0], ...pts].map((p) => p.join(",")).join(" "), class: "cut-path", fill: "none", stroke: "#d6336c" }, g);
      const r = Math.max(W, Hh) / 160;
      pts.forEach(([x, y], n) => {
        el("circle", { cx: x, cy: y, r, class: "cut-dot", fill: "#d6336c" }, g);
        const t = el("text", { x, y: -y, transform: "scale(1 -1)", class: "cut-num", "font-size": r * 1.2, "text-anchor": "middle", "dominant-baseline": "central", fill: "#fff" }, g);
        t.textContent = n + 1;
      });
    }
    if (measure && measure.a) {
      const b = measure.b || measure.a;
      el("line", { x1: measure.a[0], y1: measure.a[1], x2: b[0], y2: b[1], class: "measure", stroke: "#a15c00" }, g);
      if (measure.b) {
        const dist = Math.hypot(b[0] - measure.a[0], b[1] - measure.a[1]);
        const t = el("text", { x: (measure.a[0] + b[0]) / 2, y: -(measure.a[1] + b[1]) / 2, transform: "scale(1 -1)", class: "measure-txt", "font-size": view.w / 60, fill: "#a15c00" }, g);
        t.textContent = `${fmt(dist)} mm`;
        $("measure-out").textContent = `Distancia: ${fmt(dist)} mm · ΔX ${fmt(Math.abs(b[0] - measure.a[0]))} · ΔY ${fmt(Math.abs(b[1] - measure.a[1]))} mm`;
      }
    }
    el("rect", { id: "boxsel", class: "boxsel", x: 0, y: 0, width: 0, height: 0, visibility: "hidden" }, g);
    applyView(); renderTabs(); renderSide();
  }

  function renderTabs() {
    const tabs = $("sheet-tabs");
    tabs.innerHTML = "";
    sheets.forEach((s, i) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn small-btn" + (i === cur ? " primary" : " ghost");
      b.setAttribute("role", "tab"); b.setAttribute("aria-selected", i === cur);
      const info = ev && ev.sheets[i];
      let label = `Chapa ${i + 1}`;
      if (s.source.kind === "retal") label += " (retal)";
      else if (s.source.w !== D.format.w || s.source.h !== D.format.h) label += ` (${s.source.w}×${s.source.h})`;
      if (info) label += ` · ${fmt(info.utilization)} %`;
      if (info && info.conflicts.length) label += " ⚠";
      b.textContent = label;
      b.onclick = () => { cur = i; sel = []; view = null; render(); };
      tabs.appendChild(b);
    });
  }

  function thumbSvg(key) {
    const s = shapeFor(key, 0), [fill, stroke] = colorOf[key] || PALETTE[0], m = Math.max(s.w, s.h) * 0.08;
    return `<svg viewBox="${-m} ${-m} ${s.w + 2 * m} ${s.h + 2 * m}" aria-hidden="true"><g transform="translate(0 ${s.h}) scale(1 -1)"><path d="${s.d}" fill-rule="evenodd" style="fill:${fill};stroke:${stroke};stroke-width:${Math.max(s.w, s.h) / 50}"/></g></svg>`;
  }

  function renderSide() {
    if (ev) {
      $("st-sheets").textContent = ev.sheet_count;
      $("st-util").textContent = fmt(ev.utilization) + " %";
      $("st-consumed").textContent = fmt(ev.consumed_m2, 3) + " m²";
      $("st-conf").textContent = ev.conflicts;
      $("st-conf").className = ev.conflicts ? "error-text" : "";
      $("st-time").textContent = dur(ev.time_s);
      $("st-cut").textContent = `${fmt(ev.cut_m, 1)} m · ${ev.pierces} perf.`;
      const info = ev.sheets[cur];
      $("st-sheet").textContent = info ? `${fmt(info.utilization)} % · ${dur(info.plan && info.plan.time_s)}` : "—";
    }
    const ul = $("pending");
    ul.innerHTML = "";
    const pend = ev ? ev.pending : {};
    const keys = Object.keys(pend).filter((k) => pend[k] !== 0 && D.parts[k]);
    if (!keys.length) { const li = document.createElement("li"); li.className = "muted small"; li.textContent = "Todas las piezas están colocadas."; ul.appendChild(li); }
    keys.forEach((k) => {
      const li = document.createElement("li");
      const n = pend[k];
      const b = document.createElement("button");
      b.type = "button"; b.className = "thumb-btn";
      b.innerHTML = thumbSvg(k) + `<span></span><span class="cnt"></span>`;
      b.children[1].textContent = D.parts[k].name;
      b.children[2].textContent = n > 0 ? `faltan ${n}` : `sobran ${-n}`;
      b.title = n > 0 ? "Arrástrala a la chapa o púlsala para añadirla" : "Hay más piezas colocadas que las pedidas";
      b.disabled = !editable || n < 0;
      b.addEventListener("pointerdown", (e) => startThumbDrag(e, k, b));
      li.appendChild(b); ul.appendChild(li);
    });
    $("st-selcount").textContent = sel.length ? `${sel.length} seleccionada${sel.length > 1 ? "s" : ""}` : "";
    const sh = sheets[cur];
    const card = $("sel-card");
    card.hidden = !sel.length || !sh;
    const allLocked = sh && sel.length && sel.every((i) => sh.placements[i] && sh.placements[i].locked);
    $("btn-lock").setAttribute("aria-pressed", allLocked ? "true" : "false");
    ["btn-rot", "btn-rot-ccw", "btn-rot-180", "btn-rot-free", "btn-dup", "btn-copy", "btn-array", "btn-same", "btn-lock", "btn-magnet", "btn-del", "btn-zoom-sel"]
      .forEach((id) => { if ($(id)) $(id).disabled = !sel.length; });
    document.querySelectorAll("[data-align]").forEach((b) => (b.disabled = sel.length < (b.dataset.align.startsWith("repartir") ? 3 : 2)));
    ["btn-rot", "btn-rot-ccw", "btn-rot-free"].forEach((id) => { if ($(id)) $(id).disabled = !sel.length || !D.rotation; });
    if ($("btn-rot-180")) $("btn-rot-180").disabled = !sel.length || !D.rotation;
    if ($("btn-paste")) $("btn-paste").disabled = !clipboard;
    if (!sel.length && $("array-form")) $("array-form").hidden = true;
    if (!sel.length || !sh) return;
    const p = sh.placements[sel[0]];
    if (!p) { sel = []; card.hidden = true; return; }
    $("sel-name").textContent = sel.length > 1 ? `${sel.length} piezas seleccionadas` : D.parts[p.key].name;
    const single = sel.length === 1;
    document.querySelectorAll("[data-single]").forEach((e) => (e.hidden = !single || !editable));
    if (single) { $("sel-x").value = String(Math.round(p.x * 10) / 10).replace(".", ","); $("sel-y").value = String(Math.round(p.y * 10) / 10).replace(".", ","); $("sel-rot").value = p.rotation; }
    const info = ev && ev.sheets[cur];
    const reasons = sel.map((i) => info && info.reasons[i]).filter(Boolean);
    $("sel-reason").textContent = reasons.length ? "⚠ " + [...new Set(reasons)].join(" · ") : "";
    const mv = $("sel-move");
    mv.innerHTML = "";
    sheets.forEach((s, i) => mv.add(new Option(`Chapa ${i + 1}`, i, false, i === cur)));
  }

  // ---------------------------------------------------------------- servidor
  function scheduleCheck() { setDirty(true); clearTimeout(checkTimer); checkTimer = setTimeout(check, 350); }
  async function post(url, extra = {}) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
      body: JSON.stringify({ sheets, ...extra }) });
    const data = await r.json().catch(() => ({ error: "Respuesta no válida del servidor." }));
    if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
    return data;
  }
  async function check() {
    try { ev = await post(`/anidado/${D.id}/comprobar`); render(); if (dirty) say("Cambios sin guardar"); }
    catch (e) { say(e.message); }
  }
  async function save() {
    say("Guardando…");
    try { ev = await post(`/anidado/${D.id}/guardar`); setDirty(false); window.location.reload(); }
    catch (e) { say(e.message); }
  }
  async function op(name, extra = {}) {
    say("Calculando…");
    try {
      const r = await post(`/anidado/${D.id}/operacion`, { op: name, sheet: cur, ...extra });
      snapshot();
      sheets = r.sheets; ev = r.eval;
      cur = Math.min(cur, sheets.length - 1);
      setDirty(true); render();
    } catch (e) { say(e.message); }
  }

  // ---------------------------------------------------------------- edición
  function selected() { return sheets[cur] ? sel.map((i) => sheets[cur].placements[i]).filter(Boolean) : []; }
  function addPiece(key, at) {
    const sh = sheets[cur];
    if (!sh || !editable) return;
    snapshot();
    const s = shapeFor(key, 0);
    const x = at ? Math.round((at[0] - s.w / 2) * 2) / 2 : D.edge, y = at ? Math.round((at[1] - s.h / 2) * 2) / 2 : D.edge;
    sh.placements.push({ key, x, y, rotation: 0 });
    sel = [sh.placements.length - 1];
    render(); scheduleCheck();
  }
  function rotateSel(delta) {
    if (!sel.length || !D.rotation) return;
    snapshot();
    selected().forEach((p) => {
      if (p.locked) return;
      const b = bbox(p), cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
      p.rotation = ((Math.round((p.rotation + delta) * 10) / 10) % 360 + 360) % 360;
      const s = shapeFor(p.key, p.rotation);
      p.x = Math.round((cx - s.w / 2) * 2) / 2; p.y = Math.round((cy - s.h / 2) * 2) / 2;   // gira sobre su centro
    });
    render(); scheduleCheck();
  }
  function deleteSel() {
    if (!sel.length) return;
    snapshot();
    const set = new Set(sel);
    sheets[cur].placements = sheets[cur].placements.filter((p, i) => !set.has(i) || p.locked);
    sel = []; render(); scheduleCheck();
  }
  function duplicateSel() {
    const ps = selected(); if (!ps.length) return;
    snapshot();
    const bs = ps.map(bbox), dx = Math.max(...bs.map((b) => b.x1)) - Math.min(...bs.map((b) => b.x0)) + D.gap + 1;
    const start = sheets[cur].placements.length;
    ps.forEach((p) => sheets[cur].placements.push({ key: p.key, x: p.x + dx, y: p.y, rotation: p.rotation }));
    sel = ps.map((_, k) => start + k);
    render(); scheduleCheck();
  }
  function toggleLock() {
    const ps = selected(); if (!ps.length) return;
    const lock = !ps.every((p) => p.locked);
    snapshot(); ps.forEach((p) => { if (lock) p.locked = true; else delete p.locked; });
    render(); setDirty(true);
  }
  function align(side) {
    const ps = selected().filter((p) => !p.locked);
    if (side.startsWith("repartir")) { distribute(ps, side === "repartir-x" ? "x" : "y"); return; }
    if (ps.length < 2) { say("Selecciona dos o más piezas."); return; }
    snapshot();
    const bs = ps.map(bbox);
    if (side.startsWith("centro")) {
      const ax = side === "centro-x";
      const c = ax ? (Math.min(...bs.map((b) => b.x0)) + Math.max(...bs.map((b) => b.x1))) / 2
        : (Math.min(...bs.map((b) => b.y0)) + Math.max(...bs.map((b) => b.y1))) / 2;
      ps.forEach((p, i) => { const b = bs[i]; if (ax) p.x = Math.round((p.x + c - (b.x0 + b.x1) / 2) * 2) / 2; else p.y = Math.round((p.y + c - (b.y0 + b.y1) / 2) * 2) / 2; });
      render(); scheduleCheck(); return;
    }
    const target = { izq: Math.min(...bs.map((b) => b.x0)), der: Math.max(...bs.map((b) => b.x1)),
      abajo: Math.min(...bs.map((b) => b.y0)), arriba: Math.max(...bs.map((b) => b.y1)) }[side];
    ps.forEach((p, i) => {
      const b = bs[i];
      if (side === "izq") p.x += target - b.x0;
      if (side === "der") p.x += target - b.x1;
      if (side === "abajo") p.y += target - b.y0;
      if (side === "arriba") p.y += target - b.y1;
    });
    render(); scheduleCheck();
  }

  // reparte las piezas con el mismo hueco entre ellas, sin mover la primera ni la última
  function distribute(ps, axis) {
    if (ps.length < 3) { say("Selecciona tres o más piezas para repartir."); return; }
    snapshot();
    const a0 = axis === "x" ? "x0" : "y0", a1 = axis === "x" ? "x1" : "y1";
    const items = ps.map((p) => ({ p, b: bbox(p) })).sort((u, v) => u.b[a0] - v.b[a0]);
    const span = items[items.length - 1].b[a1] - items[0].b[a0];
    const sizes = items.reduce((t, it) => t + it.b[a1] - it.b[a0], 0);
    const gap = (span - sizes) / (items.length - 1);
    let pos = items[0].b[a0];
    items.forEach((it) => {
      const d = pos - it.b[a0];
      if (axis === "x") it.p.x = Math.round((it.p.x + d) * 10) / 10; else it.p.y = Math.round((it.p.y + d) * 10) / 10;
      pos += it.b[a1] - it.b[a0] + gap;
    });
    if (gap < D.gap) say(`Atención: el hueco repartido (${fmt(gap)} mm) es menor que la separación de ${fmt(D.gap)} mm.`);
    render(); scheduleCheck();
  }
  function groupBox(ps) {
    const bs = ps.map(bbox);
    return { x0: Math.min(...bs.map((b) => b.x0)), y0: Math.min(...bs.map((b) => b.y0)), x1: Math.max(...bs.map((b) => b.x1)), y1: Math.max(...bs.map((b) => b.y1)) };
  }
  function copySel() {
    const ps = selected(); if (!ps.length) return;
    const g = groupBox(ps);
    clipboard = { items: ps.map((p) => ({ key: p.key, rotation: p.rotation, dx: p.x - g.x0, dy: p.y - g.y0 })), w: g.x1 - g.x0, h: g.y1 - g.y0 };
    say(`${ps.length} pieza${ps.length > 1 ? "s" : ""} copiada${ps.length > 1 ? "s" : ""}. Pega con Ctrl+V (también en otra chapa).`);
    renderSide();
  }
  function paste(at) {
    const sh = sheets[cur];
    if (!clipboard || !sh || !editable) return;
    snapshot();
    let x0 = D.edge, y0 = D.edge;
    if (at) { x0 = Math.round((at[0] - clipboard.w / 2) * 2) / 2; y0 = Math.round((at[1] - clipboard.h / 2) * 2) / 2; }
    const start = sh.placements.length;
    clipboard.items.forEach((c) => sh.placements.push({ key: c.key, rotation: c.rotation, x: x0 + c.dx, y: y0 + c.dy }));
    sel = clipboard.items.map((_, k) => start + k);
    render(); scheduleCheck();
  }
  function makeArray(rows, cols, extra) {
    const ps = selected(); const sh = sheets[cur];
    if (!ps.length || !sh) return;
    rows = Math.max(1, Math.min(50, rows | 0)); cols = Math.max(1, Math.min(50, cols | 0));
    if (rows * cols * ps.length > 1000) { say("Demasiadas copias: como máximo 1000 piezas por matriz."); return; }
    if (rows * cols === 1) return;
    snapshot();
    const g = groupBox(ps), sx = g.x1 - g.x0 + D.gap + extra + 0.5, sy = g.y1 - g.y0 + D.gap + extra + 0.5;
    const out = sel.slice();
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      if (!r && !c) continue;
      ps.forEach((p) => { sh.placements.push({ key: p.key, rotation: p.rotation, x: Math.round((p.x + c * sx) * 10) / 10, y: Math.round((p.y + r * sy) * 10) / 10 }); out.push(sh.placements.length - 1); });
    }
    sel = out; render(); scheduleCheck();
    say("Matriz creada. Las piezas que no quepan en la chapa salen en rojo.");
  }
  function selectSame() {
    const sh = sheets[cur]; if (!sh || !sel.length) return;
    const keys = new Set(selected().map((p) => p.key));
    sel = sh.placements.map((p, i) => (keys.has(p.key) ? i : -1)).filter((i) => i >= 0);
    render();
  }
  function duplicateSheet() {
    const sh = sheets[cur]; if (!sh || !editable) return;
    if (!sh.placements.length) { say("La chapa está vacía."); return; }
    snapshot();
    const copy = clone(sh);
    if (copy.source.kind === "retal") copy.source = { kind: "formato", w: copy.source.w, h: copy.source.h };
    copy.placements.forEach((p) => delete p.locked);
    sheets.splice(cur + 1, 0, copy);
    cur += 1; sel = []; view = null; render(); scheduleCheck();
    say("Chapa duplicada. Si sobran piezas, aparecen en «Pendientes» como «sobran».");
  }
  const nudge = () => { const v = parseFloat(String($("nudge") ? $("nudge").value : "1").replace(",", ".")); return Number.isFinite(v) && v > 0 ? Math.min(v, 500) : 1; };

  // menú contextual (clic derecho)
  const ctx = $("ctx-menu");
  let ctxAt = null;
  function hideCtx() { if (ctx) ctx.hidden = true; }
  function showCtx(e) {
    if (!ctx || !editable) return;
    const r = wrap.getBoundingClientRect();
    ctx.querySelectorAll("[data-act]").forEach((b) => { const t = $(b.dataset.act); b.disabled = !t || t.disabled; });
    ctx.hidden = false;
    const mw = ctx.offsetWidth, mh = ctx.offsetHeight;
    ctx.style.left = Math.max(4, Math.min(e.clientX - r.left, r.width - mw - 4)) + "px";
    ctx.style.top = Math.max(4, Math.min(e.clientY - r.top, r.height - mh - 4)) + "px";
    const first = ctx.querySelector("button:not(:disabled)"); if (first) first.focus({ preventScroll: true });
  }
  if (ctx) {
    ctx.addEventListener("click", (e) => {
      const b = e.target.closest("[data-act]"); if (!b || b.disabled) return;
      hideCtx();
      if (b.dataset.act === "btn-paste") { paste(ctxAt); return; }
      const t = $(b.dataset.act); if (t) t.click();
    });
    ctx.addEventListener("keydown", (e) => {
      const items = [...ctx.querySelectorAll("button:not(:disabled)")], i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { items[(i + 1) % items.length].focus(); e.preventDefault(); }
      if (e.key === "ArrowUp") { items[(i - 1 + items.length) % items.length].focus(); e.preventDefault(); }
      if (e.key === "Escape") { hideCtx(); e.stopPropagation(); }
    });
    document.addEventListener("pointerdown", (e) => { if (!ctx.hidden && !e.target.closest("#ctx-menu")) hideCtx(); });
    wrap.addEventListener("wheel", hideCtx, { passive: true });
  }
  svg.addEventListener("contextmenu", (e) => {
    if (!editable || !sheets[cur]) return;
    e.preventDefault();
    const g = e.target.closest(".piece");
    if (g) { const i = Number(g.dataset.i); if (!sel.includes(i)) { sel = [i]; render(); } }
    const w = toWorld(e); ctxAt = [w.x, w.y];
    showCtx(e);
  });

  // ---------------------------------------------------------------- herramientas
  const TOOL_NAMES = { select: "Seleccionar", hand: "Desplazar", measure: "Medir" };
  function setTool(t) {
    tool = t; measure = null; $("measure-out").textContent = t === "measure" ? "Pulsa dos puntos sobre la chapa." : "";
    document.querySelectorAll("[data-tool]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.tool === t));
    svg.classList.toggle("tool-hand", t === "hand"); svg.classList.toggle("tool-measure", t === "measure");
    $("st-tool").textContent = TOOL_NAMES[t];
    render();
  }

  // ---------------------------------------------------------------- ratón
  function toWorld(evt) {
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    return pt.matrixTransform($("world").getScreenCTM().inverse());
  }
  let drag = null;
  let lastEmpty = null;
  svg.addEventListener("pointerdown", (e) => {
    if (!sheets[cur]) return;
    const w = toWorld(e);
    const panning = e.button === 1 || tool === "hand" || spaceDown;
    if (panning) {
      drag = { pan: true, s0: { x: e.clientX, y: e.clientY }, v0: { ...view }, k: 1 / scale() };
      svg.classList.add("panning"); svg.setPointerCapture(e.pointerId); e.preventDefault(); return;
    }
    if (e.button !== 0) return;
    if (tool === "measure") { measure = !measure || measure.b ? { a: [w.x, w.y] } : { a: measure.a, b: [w.x, w.y] }; render(); return; }
    const g = e.target.closest(".piece");
    const add = e.shiftKey || e.ctrlKey || e.metaKey;
    if (!g) {
      const now = Date.now();
      if (lastEmpty && now - lastEmpty.t < 400 && Math.hypot(e.clientX - lastEmpty.x, e.clientY - lastEmpty.y) < 6) {
        lastEmpty = null; fitView(); applyView(); return;      // doble clic en vacío: encajar la chapa
      }
      lastEmpty = { t: now, x: e.clientX, y: e.clientY };
      if (!add) sel = [];
      drag = { box: true, w0: [w.x, w.y], add, base: [...sel] };
      svg.setPointerCapture(e.pointerId); render(); return;
    }
    const i = Number(g.dataset.i);
    if (add) sel = sel.includes(i) ? sel.filter((j) => j !== i) : [...sel, i];
    else if (!sel.includes(i)) sel = [i];
    if (!editable) { render(); return; }
    const ps = selected().filter((p) => !p.locked);
    drag = { pieces: ps.map((p) => ({ p, x0: p.x, y0: p.y })), w0: w, moved: false, before: JSON.stringify(sheets) };
    svg.setPointerCapture(e.pointerId);
    render();
    e.preventDefault();
  });
  svg.addEventListener("pointermove", (e) => {
    const w = toWorld(e);
    const sh = sheets[cur];
    if (sh) { $("st-xy").textContent = `X ${fmt(w.x)} · Y ${fmt(w.y)} mm`; lastWorld = [w.x, w.y]; }
    const g = e.target.closest && e.target.closest(".piece");
    const tip = $("tip");
    if (g && !drag && sh && sh.placements[Number(g.dataset.i)]) {
      const p = sh.placements[Number(g.dataset.i)];
      const r = wrap.getBoundingClientRect();
      tip.textContent = D.parts[p.key].name + (p.locked ? " · bloqueada" : "") + (p.rotation ? ` · ${p.rotation}°` : "");
      tip.style.left = e.clientX - r.left + "px"; tip.style.top = e.clientY - r.top + "px"; tip.hidden = false;
      $("st-hover").textContent = D.parts[p.key].name;
    } else { tip.hidden = true; $("st-hover").textContent = ""; }
    if (tool === "measure" && measure && measure.a && !measure.b) {
      const line = svg.querySelector(".measure");
      if (line) { line.setAttribute("x2", w.x); line.setAttribute("y2", w.y); }
      $("measure-out").textContent = `Distancia: ${fmt(Math.hypot(w.x - measure.a[0], w.y - measure.a[1]))} mm`;
      return;
    }
    if (!drag) return;
    if (drag.pan) {
      view.x = drag.v0.x - (e.clientX - drag.s0.x) * drag.k;
      view.y = drag.v0.y - (e.clientY - drag.s0.y) * drag.k;
      applyView(); return;
    }
    if (drag.box) {
      const r = $("boxsel"), [x0, y0] = drag.w0;
      r.setAttribute("x", Math.min(x0, w.x)); r.setAttribute("y", Math.min(y0, w.y));
      r.setAttribute("width", Math.abs(w.x - x0)); r.setAttribute("height", Math.abs(w.y - y0));
      r.setAttribute("visibility", "visible");
      r.classList.toggle("crossing", w.x < x0);
      drag.w1 = [w.x, w.y];
      return;
    }
    const dx = Math.round((w.x - drag.w0.x) * 2) / 2, dy = Math.round((w.y - drag.w0.y) * 2) / 2;
    drag.moved = drag.moved || dx !== 0 || dy !== 0;
    drag.pieces.forEach(({ p, x0, y0 }) => { p.x = x0 + dx; p.y = y0 + dy; });
    svg.querySelectorAll(".piece.sel").forEach((gg) => {
      const p = sheets[cur].placements[Number(gg.dataset.i)];
      gg.setAttribute("transform", `translate(${p.x} ${p.y})`);
    });
  });
  const endDrag = () => {
    svg.classList.remove("panning");
    if (drag && drag.box && drag.w1) {
      const [ax, ay] = drag.w0, [bx, by] = drag.w1;
      const x0 = Math.min(ax, bx), x1 = Math.max(ax, bx), y0 = Math.min(ay, by), y1 = Math.max(ay, by);
      const crossing = bx < ax;   // de derecha a izquierda: lo que toca; de izquierda a derecha: lo que queda dentro
      const hit = [];
      sheets[cur].placements.forEach((p, i) => {
        const b = bbox(p);
        const inside = b.x0 >= x0 && b.x1 <= x1 && b.y0 >= y0 && b.y1 <= y1;
        const touch = b.x0 <= x1 && b.x1 >= x0 && b.y0 <= y1 && b.y1 >= y0;
        if (crossing ? touch : inside) hit.push(i);
      });
      sel = drag.add ? [...new Set([...drag.base, ...hit])] : hit;
      drag = null; render(); return;
    }
    if (drag && drag.pieces && drag.moved) {
      undo.push(drag.before); if (undo.length > 100) undo.shift(); redo.length = 0;
      drag = null; render(); scheduleCheck(); return;
    }
    if (drag && drag.box) render();
    drag = null;
  };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  svg.addEventListener("pointerleave", () => { $("tip").hidden = true; lastWorld = null; });
  svg.addEventListener("auxclick", (e) => e.preventDefault());
  wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = svg.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return;   // SVG oculto o sin pintar: no hay punto de zoom
    const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
    const k = Math.exp(Math.max(-1, Math.min(1, e.deltaY / (e.deltaMode ? 3 : 120))) * 0.18);
    zoomAt(k, fx, fy);
  }, { passive: false });
  // arrastrar miniaturas a la chapa (con puntero: funciona con ratón, lápiz y pantalla táctil)
  function startThumbDrag(e, key, btn) {
    if (!editable || btn.disabled || e.button !== 0) return;
    e.preventDefault();
    const ghost = document.createElement("div");
    ghost.className = "drag-ghost"; ghost.innerHTML = thumbSvg(key); ghost.hidden = true;
    document.body.appendChild(ghost);
    const x0 = e.clientX, y0 = e.clientY;
    let moved = false;
    const move = (ev) => {
      if (!moved && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 5) return;
      moved = true; ghost.hidden = false;
      ghost.style.left = ev.clientX + "px"; ghost.style.top = ev.clientY + "px";
      const r = wrap.getBoundingClientRect();
      wrap.classList.toggle("drop-target", ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom);
    };
    const up = (ev) => {
      document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", up);
      ghost.remove(); wrap.classList.remove("drop-target");
      if (!moved) { addPiece(key); return; }
      const r = wrap.getBoundingClientRect();
      if (ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom) {
        const w = toWorld(ev); addPiece(key, [w.x, w.y]);
      }
    };
    document.addEventListener("pointermove", move); document.addEventListener("pointerup", up);
  }
  new ResizeObserver(() => { if (!view) return; const a = aspect(); view.h = view.w * a; applyView(); }).observe(wrap);

  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea")) return;
    const mod = e.ctrlKey || e.metaKey, key = e.key.toLowerCase();
    if (e.key === " ") { spaceDown = true; svg.classList.add("tool-hand"); e.preventDefault(); return; }
    if (mod && key === "z") { restore(e.shiftKey ? redo : undo, e.shiftKey ? undo : redo); e.preventDefault(); return; }
    if (mod && key === "y") { restore(redo, undo); e.preventDefault(); return; }
    if (mod && key === "a" && sheets[cur]) { sel = sheets[cur].placements.map((_, i) => i); render(); e.preventDefault(); return; }
    if (mod && key === "d") { if (editable) duplicateSel(); e.preventDefault(); return; }
    if (mod && key === "c") { if (sel.length) { copySel(); e.preventDefault(); } return; }
    if (mod && key === "v") { if (clipboard && editable) { paste(lastWorld); e.preventDefault(); } return; }
    if (mod) return;
    if (e.key === "Escape") { hideCtx(); sel = []; measure = null; render(); return; }
    if (key === "n") { $("btn-labels").click(); return; }
    if (key === "v") { setTool("select"); return; }
    if (key === "h") { setTool("hand"); return; }
    if (key === "m") { setTool("measure"); return; }
    if (key === "f") { fitView(); applyView(); return; }
    if (key === "g") { $("btn-grid").click(); return; }
    if (e.key === "+" || e.key === "=") { zoomAt(1 / 1.25); return; }
    if (e.key === "-") { zoomAt(1.25); return; }
    if (!editable || !sel.length) return;
    if (key === "a" && e.shiftKey) { selectSame(); return; }
    const step = nudge() * (e.shiftKey ? 10 : 1);
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (moves[e.key]) {
      snapshot();
      selected().forEach((p) => { if (!p.locked) { p.x += moves[e.key][0]; p.y += moves[e.key][1]; } });
      render(); scheduleCheck(); e.preventDefault();
    } else if (key === "r") { rotateSel(e.shiftKey ? -90 : 90); e.preventDefault(); }
    else if (key === "l") { toggleLock(); }
    else if (e.key === "Delete" || e.key === "Backspace") { deleteSel(); e.preventDefault(); }
  });
  document.addEventListener("keyup", (e) => { if (e.key === " ") { spaceDown = false; svg.classList.toggle("tool-hand", tool === "hand"); } });

  // ---------------------------------------------------------------- botones
  const on = (id, fn) => { const b = $(id); if (b) b.onclick = fn; };
  document.querySelectorAll("[data-tool]").forEach((b) => (b.onclick = () => setTool(b.dataset.tool)));
  on("btn-rot", () => rotateSel(90));
  on("btn-rot-free", () => {
    if (!sel.length) return;
    const v = window.prompt("Ángulo a girar (grados, sentido antihorario; negativo = horario):", "45");
    const a = v == null ? NaN : parseFloat(v.replace(",", "."));
    if (Number.isFinite(a) && a) rotateSel(a);
  });
  on("btn-dup", duplicateSel);
  on("btn-rot-ccw", () => rotateSel(-90));
  on("btn-rot-180", () => rotateSel(180));
  on("btn-copy", copySel);
  on("btn-paste", () => paste(null));
  on("btn-same", selectSame);
  on("btn-dup-sheet", duplicateSheet);
  on("btn-array", () => { const f = $("array-form"); if (!f || !sel.length) return; f.hidden = !f.hidden; if (!f.hidden) $("arr-rows").focus(); });
  on("btn-array-go", () => {
    const ex = parseFloat(String($("arr-gap").value).replace(",", "."));
    makeArray(parseInt($("arr-rows").value, 10), parseInt($("arr-cols").value, 10), Number.isFinite(ex) && ex > 0 ? Math.min(ex, 1000) : 0);
  });
  on("btn-labels", () => { showLabels = !showLabels; $("btn-labels").setAttribute("aria-pressed", showLabels); render(); });
  on("btn-del", deleteSel);
  on("btn-lock", toggleLock);
  on("btn-undo", () => restore(undo, redo));
  on("btn-redo", () => restore(redo, undo));
  on("btn-zoom-in", () => zoomAt(1 / 1.3));
  on("btn-zoom-out", () => zoomAt(1.3));
  on("btn-fit", () => { fitView(); applyView(); });
  on("btn-zoom-sel", fitSelection);
  on("btn-compact", () => op("compactar"));
  on("btn-pending", () => op("pendientes"));
  on("btn-reflow", () => op("recolocar"));
  on("btn-magnet", () => op("iman", { selected: sel }));
  on("btn-order", () => { showOrder = !showOrder; $("btn-order").setAttribute("aria-pressed", showOrder); render(); });
  on("btn-grid", () => { showGrid = !showGrid; $("btn-grid").setAttribute("aria-pressed", showGrid); drawGrid(); });
  on("btn-full", () => {
    const c = $("nest-canvas");
    if (document.fullscreenElement) document.exitFullscreen(); else if (c.requestFullscreen) c.requestFullscreen();
  });
  document.addEventListener("fullscreenchange", () => setTimeout(() => { fitView(); applyView(); }, 50));
  document.querySelectorAll("[data-align]").forEach((b) => (b.onclick = () => align(b.dataset.align)));
  on("btn-apply-pos", () => {
    const p = selected()[0]; if (!p) return;
    const x = parseFloat($("sel-x").value.replace(",", ".")), y = parseFloat($("sel-y").value.replace(",", "."));
    const r = parseFloat($("sel-rot").value.replace(",", "."));
    if (![x, y, r].every(Number.isFinite)) { say("Revisa X, Y y ángulo."); return; }
    if (r !== p.rotation && !D.rotation) { say("Este anidado no permite girar piezas."); return; }
    snapshot(); p.x = x; p.y = y; p.rotation = ((Math.round(r * 10) / 10) % 360 + 360) % 360;
    render(); scheduleCheck();
  });
  $("sel-move").onchange = (e) => {
    const to = Number(e.target.value);
    if (!sel.length || to === cur) return;
    snapshot();
    const set = new Set(sel);
    const moving = sheets[cur].placements.filter((p, i) => set.has(i) && !p.locked);
    sheets[cur].placements = sheets[cur].placements.filter((p, i) => !set.has(i) || p.locked);
    moving.forEach((p) => { p.x = D.edge; p.y = D.edge; sheets[to].placements.push(p); });
    cur = to; sel = moving.map((_, k) => sheets[to].placements.length - moving.length + k);
    view = null; render(); scheduleCheck();
  };
  on("btn-add-sheet", () => {
    snapshot();
    const v = $("new-sheet-format") ? $("new-sheet-format").value.split("x").map(Number) : [D.format.w, D.format.h];
    sheets.push({ source: { kind: "formato", w: v[0], h: v[1] }, placements: [] });
    cur = sheets.length - 1; sel = []; view = null; render(); scheduleCheck();
  });
  on("btn-del-sheet", () => {
    if (!sheets[cur] || sheets[cur].placements.length) { say("Solo se pueden quitar chapas vacías."); return; }
    snapshot(); sheets.splice(cur, 1); cur = Math.max(0, cur - 1); view = null; render(); scheduleCheck();
  });
  on("btn-save", save);
  document.querySelectorAll("[data-confirm-dirty]").forEach((b) => b.addEventListener("click", (e) => {
    if (dirty && !window.confirm("Hay cambios sin guardar que se perderán. ¿Continuar?")) e.preventDefault();
  }));
  document.querySelectorAll("form[data-busy]").forEach((f) => f.addEventListener("submit", () => {
    const b = f.querySelector("button[type=submit]");
    if (b) { b.disabled = true; b.textContent = f.dataset.busy; }
  }));
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  setDirty(false);
  render();
})();
