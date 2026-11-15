"""CRUD de máquinas: alta, consulta, edición, baja/reactivación, duplicado y eliminación."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import GASES, LASER_TYPES, MATERIAL_FAMILIES, CutParameter, Machine
from ..web import FieldSpec, FormErrors, parse_fields, templates

router = APIRouter(prefix="/maquinas", tags=["máquinas"])

MACHINE_SECTIONS: list[tuple[str, list[FieldSpec]]] = [
    ("Identificación", [
        FieldSpec("name", "Nombre interno", "str", help="Único. Aparece en las ofertas internas."),
        FieldSpec("manufacturer", "Fabricante", "str", required=False),
        FieldSpec("model", "Modelo", "str", required=False),
        FieldSpec("laser_type", "Tipo de fuente", "select", options=LASER_TYPES),
    ]),
    ("Capacidad", [
        FieldSpec("laser_power_kw", "Potencia láser", unit="kW", min=0.5, max=60),
        FieldSpec("table_x_mm", "Mesa X (largo)", unit="mm", min=100, max=20000),
        FieldSpec("table_y_mm", "Mesa Y (ancho)", unit="mm", min=100, max=5000),
        FieldSpec("max_thickness_mm", "Espesor máximo ofertable", unit="mm", min=0.5, max=settings.max_thickness_mm,
                  help=f"El portal está limitado a {settings.max_thickness_mm:g} mm."),
    ]),
    ("Dinámica", [
        FieldSpec("rapid_speed_m_min", "Velocidad en vacío", unit="m/min", min=1, max=400),
        FieldSpec("acceleration_g", "Aceleración", unit="G", min=0.1, max=10),
        FieldSpec("positioning_accuracy_mm", "Precisión de posicionado", unit="mm", min=0, max=1),
        FieldSpec("speed_efficiency", "Eficiencia de velocidad", unit="0–1", min=0.1, max=1,
                  help="Velocidad media real / nominal (esquinas, aceleraciones)."),
        FieldSpec("small_contour_mm", "Umbral contorno pequeño", unit="mm", min=0, max=2000,
                  help="Contornos con perímetro menor se cortan a velocidad reducida."),
        FieldSpec("small_contour_factor", "Factor velocidad contorno pequeño", unit="0–1", min=0.05, max=1),
        FieldSpec("head_overhead_s", "Tiempo de cabezal por contorno", unit="s", min=0, max=30,
                  help="Subida, desplazamiento corto y aproximación por cada perforación."),
        FieldSpec("sheet_change_min", "Cambio de chapa", unit="min", min=0, max=120),
    ]),
    ("Costes", [
        FieldSpec("hourly_rate_eur", "Coste hora máquina", unit="€/h", min=0, max=2000,
                  help="Amortización, operario y mantenimiento. Gas y electricidad se calculan aparte."),
        FieldSpec("power_consumption_kw", "Consumo eléctrico medio", unit="kW", min=0, max=500),
        FieldSpec("setup_cost_eur", "Preparación por pedido", unit="€", min=0, max=10000,
                  help="Programación y puesta a punto, fijo por línea."),
    ]),
    ("Estado", [
        FieldSpec("active", "Activa (disponible para ofertar)", "bool"),
        FieldSpec("notes", "Notas", "text", required=False),
    ]),
]
MACHINE_FIELDS = [f for _, fs in MACHINE_SECTIONS for f in fs]

PARAM_FIELDS = [
    FieldSpec("material_family", "Material", "select", options=MATERIAL_FAMILIES),
    FieldSpec("gas", "Gas", "select", options=GASES),
    FieldSpec("thickness_mm", "Espesor", unit="mm", min=settings.min_thickness_mm, max=settings.max_thickness_mm),
    FieldSpec("cut_speed_m_min", "Velocidad", unit="m/min", min=0.05, max=150),
    FieldSpec("pierce_time_s", "Perforación", unit="s", min=0, max=60),
    FieldSpec("gas_flow_m3_h", "Caudal gas", unit="m³/h", min=0, max=500),
    FieldSpec("kerf_mm", "Kerf", unit="mm", min=0, max=2),
]


def _get(db: Session, machine_id: int) -> Machine:
    m = db.get(Machine, machine_id)
    if not m:
        raise HTTPException(404, "Máquina no encontrada")
    return m


def _parse_params(form) -> tuple[list[dict], dict[int, FormErrors], list[str]]:
    """Filas `p-<i>-<campo>` de la tabla de parámetros de corte. Las filas vacías se ignoran."""
    indexes = sorted({int(k.split("-")[1]) for k in form.keys() if k.startswith("p-") and k.split("-")[1].isdigit()})
    rows, row_errors, global_errors = [], {}, []
    seen = set()
    for i in indexes:
        sub = {f.name: form.get(f"p-{i}-{f.name}") for f in PARAM_FIELDS}
        if form.get(f"p-{i}-delete") or not any((sub.get(k) or "").strip()
                                                for k in ("thickness_mm", "cut_speed_m_min")):
            continue
        values, errors = parse_fields(sub, PARAM_FIELDS)
        key = (values.get("material_family"), values.get("gas"), values.get("thickness_mm"))
        if not errors and key in seen:
            errors["thickness_mm"] = "Fila duplicada (material + gas + espesor)."
        seen.add(key)
        rows.append(values)
        if errors:
            row_errors[len(rows) - 1] = errors
    if not rows:
        global_errors.append("Añade al menos un parámetro de corte.")
    return rows, row_errors, global_errors


def _render_form(request: Request, machine: Machine | None, values: dict, params: list[dict],
                 errors: dict | None = None, row_errors: dict | None = None, global_errors=None, status=200):
    return templates.TemplateResponse(request, "machines/form.html", {
        "machine": machine, "values": values, "params": params, "sections": MACHINE_SECTIONS,
        "param_fields": PARAM_FIELDS, "errors": errors or {}, "row_errors": row_errors or {},
        "global_errors": global_errors or [],
    }, status_code=status)


def _machine_values(m: Machine) -> dict:
    return {f.name: getattr(m, f.name) for f in MACHINE_FIELDS}


def _param_values(m: Machine) -> list[dict]:
    return [{f.name: getattr(p, f.name) for f in PARAM_FIELDS} for p in m.cut_params]


async def _save(request: Request, db: Session, machine: Machine | None):
    form = await request.form()
    values, errors = parse_fields(form, MACHINE_FIELDS)
    params, row_errors, global_errors = _parse_params(form)
    dup = db.scalar(select(Machine).where(Machine.name == values.get("name")))
    if dup and (machine is None or dup.id != machine.id):
        errors["name"] = "Ya existe una máquina con ese nombre."
    if errors or row_errors or global_errors:
        return _render_form(request, machine, values, params, errors, row_errors, global_errors, status=422)
    if machine is None:
        machine = Machine()
        db.add(machine)
    for k, v in values.items():
        setattr(machine, k, v)
    machine.cut_params.clear()
    db.flush()  # borra las filas antiguas antes de insertar las nuevas (restricción única)
    machine.cut_params.extend(CutParameter(**p) for p in params)
    db.commit()
    return RedirectResponse(f"/maquinas/{machine.id}", status_code=303)


@router.get("")
def list_machines(request: Request, db: Session = Depends(get_db)):
    machines = db.scalars(select(Machine).order_by(Machine.active.desc(), Machine.name)).all()
    return templates.TemplateResponse(request, "machines/list.html", {"machines": machines})


@router.get("/nueva")
def new_machine(request: Request, desde: int | None = None, db: Session = Depends(get_db)):
    if desde:  # duplicar una existente como punto de partida
        src = _get(db, desde)
        values = _machine_values(src) | {"name": f"{src.name} (copia)", "active": True}
        return _render_form(request, None, values, _param_values(src))
    defaults = {"laser_type": "fibra", "active": True, "max_thickness_mm": settings.max_thickness_mm,
                "speed_efficiency": 0.85, "small_contour_mm": 100, "small_contour_factor": 0.5,
                "head_overhead_s": 0.5, "sheet_change_min": 2, "acceleration_g": 1, "positioning_accuracy_mm": 0.05,
                "setup_cost_eur": 0}
    return _render_form(request, None, defaults, [])


@router.post("/nueva")
async def create_machine(request: Request, db: Session = Depends(get_db)):
    return await _save(request, db, None)


@router.get("/{machine_id}")
def machine_detail(machine_id: int, request: Request, db: Session = Depends(get_db)):
    m = _get(db, machine_id)
    grouped: dict[tuple[str, str], list[CutParameter]] = {}
    for p in m.cut_params:
        grouped.setdefault((p.material_family, p.gas), []).append(p)
    return templates.TemplateResponse(request, "machines/detail.html", {
        "m": m, "sections": MACHINE_SECTIONS, "grouped": grouped})


@router.get("/{machine_id}/editar")
def edit_machine(machine_id: int, request: Request, db: Session = Depends(get_db)):
    m = _get(db, machine_id)
    return _render_form(request, m, _machine_values(m), _param_values(m))


@router.post("/{machine_id}/editar")
async def update_machine(machine_id: int, request: Request, db: Session = Depends(get_db)):
    return await _save(request, db, _get(db, machine_id))


@router.post("/{machine_id}/estado")
def toggle_machine(machine_id: int, db: Session = Depends(get_db)):
    m = _get(db, machine_id)
    m.active = not m.active
    db.commit()
    return RedirectResponse(f"/maquinas/{m.id}", status_code=303)


@router.post("/{machine_id}/eliminar")
async def delete_machine(machine_id: int, request: Request, db: Session = Depends(get_db)):
    m = _get(db, machine_id)
    form = await request.form()
    if form.get("confirm_name") != m.name:
        raise HTTPException(400, "Para eliminar escribe el nombre exacto de la máquina.")
    db.delete(m)
    db.commit()
    return RedirectResponse("/maquinas", status_code=303)
