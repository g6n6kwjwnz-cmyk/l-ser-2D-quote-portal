// Editor de anidado. El navegador solo dibuja y mueve piezas; el servidor valida separaciones, calcula
// aprovechamiento y orden de corte, y hace las operaciones geométricas (compactar, imán, colocar pendientes…).
(() => {
  "use strict";
  const app = document.getElementById("nest-app");
  if (!app) return;
  const D = JSON.parse(app.dataset.nest);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("nest-svg");
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 1) => Number(v).toLocaleString("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d });
  const dur = (s) => { if (s == null) return "—"; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min ${String(r).padStart(2, "0")} s`; };
  const editable = D.editable;
  if (!editable) document.querySelectorAll("[data-edit]").forEach((el) => (el.hidden = true));

  let sheets = clone(D.sheets);
  let ev = D.eval;
  let cur = 0;
  let sel = [];               // índices seleccionados en la chapa actual
  let dirty = false;
  let checkTimer = null;
  let view = null;            // {x, y, w, h} del viewBox
  let tool = "mover";         // mover | medir
  let showOrder = false;
  let measure = null;         // {a, b}
  const undo = [], redo = [];

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };

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

  // ---------------------------------------------------------------- dibujo
  function fitView() {
    const sh = sheets[cur];
    if (!sh) return;
    const m = Math.max(sh.source.w, sh.source.h) * 0.02;
    view = { x: -m, y: -m, w: sh.source.w + 2 * m, h: sh.source.h + 2 * m };
  }

  function render() {
    const sh = sheets[cur];
    svg.innerHTML = "";
    if (!sh) return;
    if (!view) fitView();
    const W = sh.source.w, H = sh.source.h;
    svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
    const g = el("g", { transform: `translate(0 ${H}) scale(1 -1)`, id: "world" }, svg);
    el("rect", { x: 0, y: 0, width: W, height: H, class: "sheet", fill: "#e9ecef", stroke: "#6b7280" }, g);
    el("rect", { x: D.edge, y: D.edge, width: Math.max(0, W - 2 * D.edge), height: Math.max(0, H - 2 * D.edge), class: "sheet-usable", fill: "none", stroke: "#6b7280" }, g);
    const info = ev && ev.sheets[cur];
    if (info && info.offcut) {
      const o = info.offcut;
      el("rect", { x: o.x, y: o.y, width: o.w, height: o.h, class: "offcut", fill: "#d1fae5", stroke: "#1b7f4b" }, g);
    }
    const bad = new Set(info ? info.conflicts : []);
    sh.placements.forEach((p, i) => {
      if (!D.parts[p.key]) return;
      const s = shapeFor(p.key, p.rotation);
      const cls = "piece" + (bad.has(i) ? " bad" : "") + (sel.includes(i) ? " sel" : "") + (p.locked ? " locked" : "");
      const pg = el("g", { transform: `translate(${p.x} ${p.y})`, class: cls, "data-i": i }, g);
      el("path", { d: s.d, "fill-rule": "evenodd", fill: "#bfdbfe", stroke: "#0f6fde" }, pg);
      const t = el("title", {}, pg); t.textContent = D.parts[p.key].name + (p.locked ? " (bloqueada)" : "");
    });
    if (showOrder && info && info.plan && info.plan.order.length) {
      const pts = info.plan.order.filter((i) => sh.placements[i]).map((i) => { const b = bbox(sh.placements[i]); return [(b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2]; });
      el("polyline", { points: [[0, 0], ...pts].map((p) => p.join(",")).join(" "), class: "cut-path", fill: "none", stroke: "#d6336c" }, g);
      const r = Math.max(W, H) / 160;
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
    renderTabs(); renderSide();
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
      b.onclick = () => { cur = i; sel = []; fitView(); render(); };
      tabs.appendChild(b);
    });
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
      $("st-sheet").textContent = info ? `${fmt(info.utilization)} % · ${dur(info.plan && info.plan.time_s)} · ${fmt(info.plan ? info.plan.cut_m : 0, 1)} m` : "—";
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
      b.type = "button"; b.className = "btn small-btn ghost";
      b.textContent = n > 0 ? `${D.parts[k].name} · faltan ${n}` : `${D.parts[k].name} · sobran ${-n}`;
      b.disabled = !editable || n < 0;
      b.onclick = () => addPiece(k);
      li.appendChild(b); ul.appendChild(li);
    });
    const sh = sheets[cur];
    const card = $("sel-card");
    card.hidden = !sel.length || !sh;
    if (!sel.length || !sh) return;
    const p = sh.placements[sel[0]];
    if (!p) { sel = []; card.hidden = true; return; }
    $("sel-name").textContent = sel.length > 1 ? `${sel.length} piezas seleccionadas` : D.parts[p.key].name;
    const single = sel.length === 1;
    document.querySelectorAll("[data-single]").forEach((e) => (e.hidden = !single || !editable));
    if (single) { $("sel-x").value = fmt(p.x, 1).replace(/\s/g, ""); $("sel-y").value = fmt(p.y, 1).replace(/\s/g, ""); $("sel-rot").value = p.rotation; }
    const info = ev && ev.sheets[cur];
    const reasons = sel.map((i) => info && info.reasons[i]).filter(Boolean);
    $("sel-reason").textContent = reasons.length ? "⚠ " + [...new Set(reasons)].join(" · ") : "";
    $("btn-lock").textContent = sel.every((i) => sh.placements[i].locked) ? "Desbloquear" : "Bloquear";
    $("btn-rot").disabled = !D.rotation;
    const mv = $("sel-move");
    mv.innerHTML = "";
    sheets.forEach((s, i) => mv.add(new Option(`Chapa ${i + 1}`, i, false, i === cur)));
  }

  // ---------------------------------------------------------------- servidor
  function scheduleCheck() {
    setDirty(true);
    clearTimeout(checkTimer);
    checkTimer = setTimeout(check, 350);
  }
  async function post(url, extra = {}) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
      body: JSON.stringify({ sheets, ...extra }) });
    const data = await r.json().catch(() => ({ error: "Respuesta no válida del servidor." }));
    if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
    return data;
  }
  async function check() {
    try { ev = await post(`/anidado/${D.id}/comprobar`); render(); }
    catch (e) { $("save-state").textContent = e.message; }
  }
  async function save() {
    $("save-state").textContent = "Guardando…";
    try { ev = await post(`/anidado/${D.id}/guardar`); setDirty(false); window.location.reload(); }
    catch (e) { $("save-state").textContent = e.message; }
  }
  async function op(name, extra = {}) {
    $("save-state").textContent = "Calculando…";
    try {
      const r = await post(`/anidado/${D.id}/operacion`, { op: name, sheet: cur, ...extra });
      snapshot();
      sheets = r.sheets; ev = r.eval;
      cur = Math.min(cur, sheets.length - 1);
      setDirty(true); render();
    } catch (e) { $("save-state").textContent = e.message; }
  }

  // ---------------------------------------------------------------- edición
  function addPiece(key) {
    const sh = sheets[cur];
    if (!sh) return;
    snapshot();
    sh.placements.push({ key, x: D.edge, y: D.edge, rotation: 0 });
    sel = [sh.placements.length - 1];
    render(); scheduleCheck();
  }
  function selected() { return sel.map((i) => sheets[cur].placements[i]).filter(Boolean); }
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
  function align(side) {
    const ps = selected().filter((p) => !p.locked);
    if (ps.length < 2) { $("save-state").textContent = "Selecciona dos o más piezas (Mayús + clic)."; return; }
    snapshot();
    const bs = ps.map(bbox);
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

  // ---------------------------------------------------------------- ratón: mover, seleccionar, desplazar, zoom, medir
  function toWorld(evt) {
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    return pt.matrixTransform($("world").getScreenCTM().inverse());
  }
  let drag = null;
  svg.addEventListener("pointerdown", (e) => {
    if (!sheets[cur]) return;
    const w = toWorld(e);
    if (tool === "medir") {
      measure = !measure || measure.b ? { a: [w.x, w.y] } : { a: measure.a, b: [w.x, w.y] };
      render(); return;
    }
    const g = e.target.closest(".piece");
    if (!g) {
      if (!e.shiftKey) sel = [];
      const s0 = { x: e.clientX, y: e.clientY };
      drag = { pan: true, s0, v0: { ...view }, k: view.w / svg.getBoundingClientRect().width, moved: false };
      svg.setPointerCapture(e.pointerId);
      render(); return;
    }
    const i = Number(g.dataset.i);
    if (e.shiftKey) sel = sel.includes(i) ? sel.filter((j) => j !== i) : [...sel, i];
    else if (!sel.includes(i)) sel = [i];
    if (!editable) { render(); return; }
    const ps = selected().filter((p) => !p.locked);
    drag = { pieces: ps.map((p) => ({ p, x0: p.x, y0: p.y })), w0: w, moved: false, before: JSON.stringify(sheets) };
    svg.setPointerCapture(e.pointerId);
    render();
    e.preventDefault();
  });
  svg.addEventListener("pointermove", (e) => {
    if (tool === "medir" && measure && measure.a && !measure.b) {
      const w = toWorld(e); measure.tmp = [w.x, w.y];
      const line = svg.querySelector(".measure");
      if (line) { line.setAttribute("x2", w.x); line.setAttribute("y2", w.y); }
      return;
    }
    if (!drag) return;
    if (drag.pan) {
      view.x = drag.v0.x - (e.clientX - drag.s0.x) * drag.k;
      view.y = drag.v0.y - (e.clientY - drag.s0.y) * drag.k;
      drag.moved = true;
      svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
      return;
    }
    const w = toWorld(e);
    const dx = Math.round((w.x - drag.w0.x) * 2) / 2, dy = Math.round((w.y - drag.w0.y) * 2) / 2;
    drag.moved = drag.moved || dx !== 0 || dy !== 0;
    drag.pieces.forEach(({ p, x0, y0 }) => { p.x = x0 + dx; p.y = y0 + dy; });
    svg.querySelectorAll(".piece.sel").forEach((g) => {
      const p = sheets[cur].placements[Number(g.dataset.i)];
      g.setAttribute("transform", `translate(${p.x} ${p.y})`);
    });
  });
  const endDrag = () => {
    if (drag && !drag.pan && drag.moved) {
      undo.push(drag.before); if (undo.length > 100) undo.shift(); redo.length = 0;
      render(); scheduleCheck();
    }
    drag = null;
  };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = svg.getBoundingClientRect();
    const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
    const k = e.deltaY > 0 ? 1.15 : 1 / 1.15;
    zoom(k, fx, fy);
  }, { passive: false });
  function zoom(k, fx = 0.5, fy = 0.5) {
    const nw = view.w * k, nh = view.h * k;
    view.x += (view.w - nw) * fx; view.y += (view.h - nh) * fy; view.w = nw; view.h = nh;
    svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
  }

  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea")) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z") { restore(e.shiftKey ? redo : undo, e.shiftKey ? undo : redo); e.preventDefault(); return; }
    if (mod && e.key.toLowerCase() === "y") { restore(redo, undo); e.preventDefault(); return; }
    if (mod && e.key.toLowerCase() === "a" && sheets[cur]) { sel = sheets[cur].placements.map((_, i) => i); render(); e.preventDefault(); return; }
    if (e.key === "Escape") { sel = []; measure = null; render(); return; }
    if (!editable || !sel.length) return;
    const step = e.shiftKey ? 10 : 1;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (moves[e.key]) {
      snapshot();
      selected().forEach((p) => { if (!p.locked) { p.x += moves[e.key][0]; p.y += moves[e.key][1]; } });
      render(); scheduleCheck(); e.preventDefault();
    } else if (e.key === "r" || e.key === "R") { rotateSel(e.shiftKey ? -90 : 90); e.preventDefault(); }
    else if (e.key === "Delete" || e.key === "Backspace") { deleteSel(); e.preventDefault(); }
  });

  // ---------------------------------------------------------------- botones
  const on = (id, fn) => { const b = $(id); if (b) b.onclick = fn; };
  on("btn-rot", () => rotateSel(90));
  on("btn-del", deleteSel);
  on("btn-undo", () => restore(undo, redo));
  on("btn-redo", () => restore(redo, undo));
  on("btn-zoom-in", () => { zoom(1 / 1.3); });
  on("btn-zoom-out", () => { zoom(1.3); });
  on("btn-fit", () => { fitView(); render(); });
  on("btn-compact", () => op("compactar"));
  on("btn-pending", () => op("pendientes"));
  on("btn-reflow", () => op("recolocar"));
  on("btn-magnet", () => op("iman", { selected: sel }));
  on("btn-lock", () => {
    const ps = selected(); const lock = !ps.every((p) => p.locked);
    snapshot(); ps.forEach((p) => { if (lock) p.locked = true; else delete p.locked; });
    render(); setDirty(true);
  });
  on("btn-order", () => { showOrder = !showOrder; $("btn-order").setAttribute("aria-pressed", showOrder); render(); });
  on("btn-measure", () => {
    tool = tool === "medir" ? "mover" : "medir"; measure = null; $("measure-out").textContent = tool === "medir" ? "Pulsa dos puntos sobre la chapa." : "";
    $("btn-measure").setAttribute("aria-pressed", tool === "medir"); svg.classList.toggle("measuring", tool === "medir"); render();
  });
  document.querySelectorAll("[data-align]").forEach((b) => (b.onclick = () => align(b.dataset.align)));
  on("btn-apply-pos", () => {
    const p = selected()[0]; if (!p) return;
    const x = parseFloat($("sel-x").value.replace(",", ".")), y = parseFloat($("sel-y").value.replace(",", "."));
    const r = parseFloat($("sel-rot").value.replace(",", "."));
    if (![x, y, r].every(Number.isFinite)) { $("save-state").textContent = "Revisa X, Y y ángulo."; return; }
    if (r !== p.rotation && !D.rotation) { $("save-state").textContent = "Este anidado no permite girar piezas."; return; }
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
    fitView(); render(); scheduleCheck();
  };
  on("btn-add-sheet", () => {
    snapshot();
    const v = $("new-sheet-format") ? $("new-sheet-format").value.split("x").map(Number) : [D.format.w, D.format.h];
    sheets.push({ source: { kind: "formato", w: v[0], h: v[1] }, placements: [] });
    cur = sheets.length - 1; sel = []; fitView(); render(); scheduleCheck();
  });
  on("btn-del-sheet", () => {
    if (!sheets[cur] || sheets[cur].placements.length) { $("save-state").textContent = "Solo se pueden quitar chapas vacías."; return; }
    snapshot(); sheets.splice(cur, 1); cur = Math.max(0, cur - 1); fitView(); render(); scheduleCheck();
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
