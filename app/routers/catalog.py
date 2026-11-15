"""Materiales y parámetros económicos globales (edición sencilla)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MATERIAL_FAMILIES, CostSettings, Material
from ..web import FieldSpec, parse_fields, templates

router = APIRouter(tags=["catálogo"])

MATERIAL_FIELDS = [
    FieldSpec("code", "Código", "str"),
    FieldSpec("name", "Descripción", "str"),
    FieldSpec("family", "Familia", "select", options=MATERIAL_FAMILIES),
    FieldSpec("density_kg_dm3", "Densidad", unit="kg/dm³", min=0.5, max=25),
    FieldSpec("price_eur_kg", "Precio", unit="€/kg", min=0, max=1000),
    FieldSpec("active", "Activo", "bool"),
]

SETTINGS_SECTIONS = [
    ("Energía y gases", [
        FieldSpec("electricity_eur_kwh", "Electricidad", unit="€/kWh", min=0, max=5),
        FieldSpec("gas_o2_eur_m3", "Oxígeno (O₂)", unit="€/m³", min=0, max=50),
        FieldSpec("gas_n2_eur_m3", "Nitrógeno (N₂)", unit="€/m³", min=0, max=50),
        FieldSpec("gas_air_eur_m3", "Aire comprimido", unit="€/m³", min=0, max=50),
    ]),
    ("Comercial", [
        FieldSpec("margin_pct", "Margen comercial", unit="%", min=0, max=500),
        FieldSpec("min_order_eur", "Importe mínimo por línea", unit="€", min=0, max=100000),
    ]),
    ("Chapa y anidado", [
        FieldSpec("sheet_x_mm", "Formato chapa X", unit="mm", min=100, max=20000),
        FieldSpec("sheet_y_mm", "Formato chapa Y", unit="mm", min=100, max=5000),
        FieldSpec("part_gap_mm", "Separación entre piezas", unit="mm", min=0, max=100),
        FieldSpec("sheet_edge_mm", "Margen de borde de chapa", unit="mm", min=0, max=200),
        FieldSpec("scrap_recovery_pct", "Retal repercutido", unit="%", min=0, max=100,
                  help="Parte del sobrante de chapa que se cobra a la pieza (0 = solo su envolvente)."),
    ]),
]
SETTINGS_FIELDS = [f for _, fs in SETTINGS_SECTIONS for f in fs]


# ------------------------------------------------------------------ materiales
@router.get("/materiales")
def list_materials(request: Request, db: Session = Depends(get_db)):
    items = db.scalars(select(Material).order_by(Material.family, Material.code)).all()
    return templates.TemplateResponse(request, "materials/list.html", {"items": items})


def _material_form(request, item, values, errors=None, status=200):
    return templates.TemplateResponse(request, "materials/form.html", {
        "item": item, "values": values, "fields": MATERIAL_FIELDS, "errors": errors or {}}, status_code=status)


@router.get("/materiales/nuevo")
def new_material(request: Request):
    return _material_form(request, None, {"active": True, "family": "acero", "density_kg_dm3": 7.85})


@router.get("/materiales/{item_id}")
def edit_material(item_id: int, request: Request, db: Session = Depends(get_db)):
    item = db.get(Material, item_id) or _404()
    return _material_form(request, item, {f.name: getattr(item, f.name) for f in MATERIAL_FIELDS})


@router.post("/materiales/nuevo")
@router.post("/materiales/{item_id:int}")
async def save_material(request: Request, item_id: int | None = None, db: Session = Depends(get_db)):
    item = (db.get(Material, item_id) or _404()) if item_id else None
    values, errors = parse_fields(await request.form(), MATERIAL_FIELDS)
    dup = db.scalar(select(Material).where(Material.code == values.get("code")))
    if dup and (item is None or dup.id != item.id):
        errors["code"] = "Código ya existente."
    if errors:
        return _material_form(request, item, values, errors, 422)
    if item is None:
        item = Material()
        db.add(item)
    for k, v in values.items():
        setattr(item, k, v)
    db.commit()
    return RedirectResponse("/materiales", status_code=303)


@router.post("/materiales/{item_id}/eliminar")
def delete_material(item_id: int, db: Session = Depends(get_db)):
    db.delete(db.get(Material, item_id) or _404())
    db.commit()
    return RedirectResponse("/materiales", status_code=303)


# ------------------------------------------------------------------ parámetros económicos
def _parse_discounts(raw: str) -> tuple[list, str | None]:
    tiers = []
    for line in (raw or "").replace(";", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            qty, pct = [x.strip().replace(",", ".") for x in line.split(":")]
            qty, pct = int(float(qty)), float(pct)
        except ValueError:
            return [], f"Formato no válido en «{line}». Usa «cantidad: %», una por línea."
        if qty < 1 or not 0 <= pct <= 90:
            return [], f"Valores fuera de rango en «{line}»."
        tiers.append([qty, pct])
    return sorted(tiers), None


@router.get("/parametros")
def edit_settings(request: Request, db: Session = Depends(get_db), guardado: int = 0):
    s = db.scalar(select(CostSettings))
    values = {f.name: getattr(s, f.name) for f in SETTINGS_FIELDS}
    values["quantity_discounts"] = "\n".join(f"{q}: {p:g}" for q, p in s.quantity_discounts)
    return templates.TemplateResponse(request, "settings/form.html", {
        "values": values, "sections": SETTINGS_SECTIONS, "errors": {}, "saved": guardado})


@router.post("/parametros")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values, errors = parse_fields(form, SETTINGS_FIELDS)
    tiers, terr = _parse_discounts(form.get("quantity_discounts", ""))
    values["quantity_discounts"] = form.get("quantity_discounts", "")
    if terr:
        errors["quantity_discounts"] = terr
    if errors:
        return templates.TemplateResponse(request, "settings/form.html", {
            "values": values, "sections": SETTINGS_SECTIONS, "errors": errors, "saved": 0}, status_code=422)
    s = db.scalar(select(CostSettings))
    for f in SETTINGS_FIELDS:
        setattr(s, f.name, values[f.name])
    s.quantity_discounts = tiers
    db.commit()
    return RedirectResponse("/parametros?guardado=1", status_code=303)


def _404():
    raise HTTPException(404, "No encontrado")
