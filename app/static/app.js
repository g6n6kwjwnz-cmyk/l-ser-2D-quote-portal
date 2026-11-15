// JS mínimo sin dependencias: filas dinámicas, carga de fichero y cálculo de oferta sin recargar.
document.addEventListener("DOMContentLoaded", () => {
  // ----- tabla de parámetros de corte: añadir fila
  document.querySelectorAll("[data-add-row]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tbody = document.querySelector(`#${btn.dataset.addRow} tbody`);
      const tpl = document.getElementById("param-row-template");
      const next = Math.max(-1, ...[...tbody.querySelectorAll("[name^='p-']")].map((el) => +el.name.split("-")[1])) + 1;
      const html = tpl.innerHTML.replaceAll("__i__", String(next));
      tbody.insertAdjacentHTML("beforeend", html);
      // copiar material y gas de la fila anterior para agilizar
      const rows = tbody.querySelectorAll("tr");
      if (rows.length > 1) {
        const prev = rows[rows.length - 2], last = rows[rows.length - 1];
        ["material_family", "gas"].forEach((f) => {
          const src = prev.querySelector(`[name$='-${f}']`), dst = last.querySelector(`[name$='-${f}']`);
          if (src && dst) dst.value = src.value;
        });
      }
      tbody.lastElementChild.querySelector("input")?.focus();
    });
  });

  // ----- carga de plano: nombre del fichero y arrastrar/soltar
  const upload = document.querySelector("[data-upload]");
  if (upload) {
    const input = upload.querySelector("input[type=file]");
    const zone = upload.querySelector(".dropzone");
    const label = upload.querySelector("[data-filename]");
    const show = () => { label.textContent = input.files[0]?.name || ""; };
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach((e) => zone.addEventListener(e, (ev) => { ev.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((e) => zone.addEventListener(e, () => zone.classList.remove("drag")));
    zone.addEventListener("drop", (ev) => { ev.preventDefault(); input.files = ev.dataTransfer.files; show(); });
  }

  // ----- formulario de cálculo
  const form = document.getElementById("quote-form");
  if (!form) return;
  const options = JSON.parse(form.dataset.options);
  const materialFamily = JSON.parse(form.dataset.materials);
  const $ = (id) => form.querySelector("#" + id);
  const machineSel = $("machine_id"), materialSel = $("material_id"), gasSel = $("gas"), thick = $("thickness_mm");
  const gasNames = { O2: "Oxígeno (O₂)", N2: "Nitrógeno (N₂)", AIRE: "Aire comprimido" };
  const help = form.querySelector("[data-thickness-help]");

  Object.entries(options).forEach(([id, m]) => machineSel.add(new Option(m.name, id)));

  function refresh() {
    const m = options[machineSel.value];
    const fam = materialFamily[materialSel.value];
    const gases = (m && m.families[fam]) || {};
    const prevGas = gasSel.value;
    gasSel.innerHTML = "";
    Object.keys(gases).forEach((g) => gasSel.add(new Option(gasNames[g] || g, g)));
    if (gases[prevGas]) gasSel.value = prevGas;
    const list = gases[gasSel.value] || [];
    document.getElementById("thickness-list").innerHTML = list.map((t) => `<option value="${t}">`).join("");
    if (!Object.keys(gases).length) help.textContent = "Esta máquina no tiene parámetros para este material.";
    else help.textContent = list.length ? `Tabulado: ${Math.min(...list)}–${Math.max(...list)} mm` : "";
    thick.max = m ? Math.min(12, m.max) : 12;
  }
  machineSel.addEventListener("change", refresh);
  materialSel.addEventListener("change", refresh);
  gasSel.addEventListener("change", refresh);
  refresh();

  const out = document.getElementById("quote-result");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    out.classList.add("loading");
    try {
      const res = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } });
      out.innerHTML = await res.text();
    } catch {
      out.innerHTML = '<div class="alert error">No se ha podido calcular. Revisa la conexión.</div>';
    } finally {
      out.classList.remove("loading");
    }
  });
});
