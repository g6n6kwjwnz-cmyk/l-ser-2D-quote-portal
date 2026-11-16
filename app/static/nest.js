// Editor de anidado: arrastrar, girar, quitar y añadir piezas; el servidor valida separaciones y calcula aprovechamiento.
(() => {
  "use strict";
  const app = document.getElementById("nest-app");
  if (!app) return;
  const D = JSON.parse(app.dataset.nest);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("nest-svg");
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 1) => Number(v).toLocaleString("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d });
  const editable = D.editable;
  if (!editable) document.querySelectorAll("[data-edit]").forEach((el) => (el.hidden = true));

  let sheets = D.sheets.map((s) => ({ source: s.source, placements: s.placements.map((p) => ({ ...p })) }));
  let ev = D.eval;
  let cur = 0;
  let sel = null;          // índice de la pieza seleccionada en la chapa actual
  let dirty = false;
  let checkTimer = null;

  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };

  function setDirty(v) {
    dirty = v;
    $("save-state").textContent = v ? "Cambios sin guardar" : "Guardado";
    const c = $("btn-confirm");
    if (c) c.disabled = v || (ev && ev.conflicts > 0);
  }

  // ---------------------------------------------------------------- dibujo
  function render() {
    const sh = sheets[cur];
    if (!sh) { svg.innerHTML = ""; return; }
    const W = sh.source.w, H = sh.source.h, m = Math.max(W, H) * 0.02;
    svg.setAttribute("viewBox", `${-m} ${-m} ${W + 2 * m} ${H + 2 * m}`);
    svg.innerHTML = "";
    const g = el("g", { transform: `translate(0 ${H}) scale(1 -1)`, id: "world" }, svg);
    el("rect", { x: 0, y: 0, width: W, height: H, class: "sheet" }, g);
    el("rect", { x: D.edge, y: D.edge, width: Math.max(0, W - 2 * D.edge), height: Math.max(0, H - 2 * D.edge), class: "sheet-usable" }, g);
    const info = ev && ev.sheets[cur];
    if (info && info.offcut) {
      const o = info.offcut;
      el("rect", { x: o.x, y: o.y, width: o.w, height: o.h, class: "offcut" }, g);
    }
    const bad = new Set(info ? info.conflicts : []);
    sh.placements.forEach((p, i) => {
      const part = D.parts[p.key];
      if (!part) return;
      const r = part.rot[p.rotation] || part.rot[0];
      const pg = el("g", { transform: `translate(${p.x} ${p.y})`, class: "piece" + (bad.has(i) ? " bad" : "") + (sel === i ? " sel" : ""),
        "data-i": i, tabindex: editable ? 0 : -1 }, g);
      el("path", { d: r.d, "fill-rule": "evenodd" }, pg);
      const t = el("title", {}, pg); t.textContent = part.name;
    });
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
      b.textContent = `Chapa ${i + 1}` + (s.source.kind === "retal" ? " (retal)" : "") + (info ? ` · ${fmt(info.utilization)} %` : "");
      if (info && info.conflicts.length) b.textContent += " ⚠";
      b.onclick = () => { cur = i; sel = null; render(); };
      tabs.appendChild(b);
    });
  }

  function renderSide() {
    if (ev) {
      $("st-sheets").textContent = ev.sheet_count;
      $("st-util").textContent = fmt(ev.utilization) + " %";
      $("st-sheet-util").textContent = ev.sheets[cur] ? fmt(ev.sheets[cur].utilization) + " %" : "—";
      $("st-consumed").textContent = fmt(ev.consumed_m2, 3) + " m²";
      $("st-conf").textContent = ev.conflicts;
      $("st-conf").className = ev.conflicts ? "error-text" : "";
    }
    const ul = $("pending");
    ul.innerHTML = "";
    const pend = ev ? ev.pending : {};
    const keys = Object.keys(pend).filter((k) => pend[k] !== 0);
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
    const card = $("sel-card");
    const p = sel !== null && sheets[cur] ? sheets[cur].placements[sel] : null;
    card.hidden = !p;
    if (p) {
      const part = D.parts[p.key];
      $("sel-name").textContent = `${part.name} · x ${fmt(p.x)} · y ${fmt(p.y)} mm · ${p.rotation}°`;
      const reason = ev && ev.sheets[cur] && ev.sheets[cur].reasons[sel];
      $("sel-reason").textContent = reason ? "⚠ " + reason : "";
      $("btn-rot").disabled = !D.rotation;
      const mv = $("sel-move");
      mv.innerHTML = "";
      sheets.forEach((s, i) => { const o = new Option(`Chapa ${i + 1}`, i, false, i === cur); mv.add(o); });
    }
  }

  // ---------------------------------------------------------------- validación en servidor
  function scheduleCheck() {
    setDirty(true);
    clearTimeout(checkTimer);
    checkTimer = setTimeout(check, 350);
  }

  async function post(url) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
      body: JSON.stringify({ sheets }) });
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
    try { ev = await post(`/anidado/${D.id}/guardar`); setDirty(false); render(); location.reload(); }
    catch (e) { $("save-state").textContent = e.message; }
  }

  // ---------------------------------------------------------------- edición
  function addPiece(key) {
    const sh = sheets[cur];
    if (!sh) return;
    sh.placements.push({ key, x: D.edge, y: D.edge, rotation: 0 });
    sel = sh.placements.length - 1;
    render(); scheduleCheck();
  }

  function rotateSel() {
    if (sel === null || !D.rotation) return;
    const p = sheets[cur].placements[sel];
    p.rotation = (p.rotation + 90) % 360;
    render(); scheduleCheck();
  }

  function deleteSel() {
    if (sel === null) return;
    sheets[cur].placements.splice(sel, 1);
    sel = null; render(); scheduleCheck();
  }

  function toSheet(evt) {
    const world = $("world");
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    return pt.matrixTransform(world.getScreenCTM().inverse());
  }

  let drag = null;
  svg.addEventListener("pointerdown", (e) => {
    const g = e.target.closest(".piece");
    if (!g) { sel = null; render(); return; }
    sel = Number(g.dataset.i);
    if (!editable) { render(); return; }
    const p = sheets[cur].placements[sel];
    const s = toSheet(e);
    drag = { p, dx: s.x - p.x, dy: s.y - p.y, g, moved: false };
    svg.setPointerCapture(e.pointerId);
    render();
    drag.g = svg.querySelector(`.piece[data-i="${sel}"]`);
    e.preventDefault();
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const s = toSheet(e);
    drag.p.x = Math.round((s.x - drag.dx) * 2) / 2;
    drag.p.y = Math.round((s.y - drag.dy) * 2) / 2;
    drag.moved = true;
    drag.g.setAttribute("transform", `translate(${drag.p.x} ${drag.p.y})`);
  });
  const endDrag = () => { if (drag && drag.moved) { render(); scheduleCheck(); } drag = null; };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);

  document.addEventListener("keydown", (e) => {
    if (!editable || sel === null || e.target.closest("input, select, textarea")) return;
    const p = sheets[cur].placements[sel];
    const step = e.shiftKey ? 10 : 1;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (moves[e.key]) { p.x += moves[e.key][0]; p.y += moves[e.key][1]; render(); scheduleCheck(); e.preventDefault(); }
    else if (e.key === "r" || e.key === "R") { rotateSel(); e.preventDefault(); }
    else if (e.key === "Delete" || e.key === "Backspace") { deleteSel(); e.preventDefault(); }
    else if (e.key === "Escape") { sel = null; render(); }
  });

  $("btn-rot").onclick = rotateSel;
  $("btn-del").onclick = deleteSel;
  $("sel-move").onchange = (e) => {
    const to = Number(e.target.value);
    if (sel === null || to === cur) return;
    const [p] = sheets[cur].placements.splice(sel, 1);
    p.x = D.edge; p.y = D.edge;
    sheets[to].placements.push(p);
    cur = to; sel = sheets[to].placements.length - 1;
    render(); scheduleCheck();
  };
  $("btn-add-sheet").onclick = () => {
    sheets.push({ source: { kind: "formato", w: D.format.w, h: D.format.h }, placements: [] });
    cur = sheets.length - 1; sel = null; render(); scheduleCheck();
  };
  $("btn-del-sheet").onclick = () => {
    if (!sheets[cur] || sheets[cur].placements.length) { $("save-state").textContent = "Solo se pueden quitar chapas vacías."; return; }
    sheets.splice(cur, 1); cur = Math.max(0, cur - 1); render(); scheduleCheck();
  };
  $("btn-save").onclick = save;
  document.querySelectorAll("[data-confirm-dirty]").forEach((b) => b.addEventListener("click", (e) => {
    if (dirty && !window.confirm("Hay cambios sin guardar que se perderán. ¿Continuar?")) e.preventDefault();
  }));
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  setDirty(false);
  $("save-state").textContent = "";
  render();
})();
