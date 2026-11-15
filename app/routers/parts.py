"""Carga de piezas (DXF/DWG), análisis geométrico, vista previa y cálculo de coste."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..costing.engine import CostingError, QuoteInput, compute_quote
from ..db import get_db
from ..geometry.dxf_engine import UNIT_CHOICES, UNIT_CODES, GeometryError, analyze_dxf, read_header_units, render_svg
from ..models import CostSettings, Machine, Material, Part
from ..web import templates

router = APIRouter(prefix="/piezas", tags=["piezas"])

ALLOWED_EXT = {".dxf", ".dwg"}
_SAFE_NAME = re.compile(r"[^\w.\- ()]+", re.UNICODE)


def _sniff(data: bytes) -> str | None:
    """Comprueba el contenido real del fichero, no solo la extensión."""
    if re.match(rb"^AC1\d{3}", data[:6]):  # cabecera de versión DWG (AC1015 = 2000 ... AC1032 = 2018+)
        return "dwg"
    if data.startswith(b"AutoCAD Binary DXF"):
        return "dxf"
    head = data[:2048].lstrip().decode("latin-1", errors="ignore")
    if re.match(r"^(999\s*\r?\n.*\r?\n\s*)?0\s*\r?\nSECTION", head, re.DOTALL):
        return "dxf"
    return None


def _run_dwg2dxf(exe: str, data: bytes, minimal: bool) -> bytes | None:
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp, "in.dwg"), Path(tmp, "out.dxf")
        src.write_bytes(data)
        cmd = [exe, "-y"] + (["-m"] if minimal else []) + ["-o", str(dst), str(src)]
        try:
            subprocess.run(cmd, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            return None
        # dwg2dxf puede devolver código de error con avisos aunque genere el DXF: se valida el resultado
        return dst.read_bytes() if dst.exists() and dst.stat().st_size > 0 else None


def _analyze_dwg(data: bytes, units: str):
    """DWG → DXF con LibreDWG. Primero DXF completo; si no se puede leer, DXF mínimo conservando unidades."""
    exe = shutil.which(settings.dwg_converter)
    if not exe:
        raise GeometryError("Los ficheros DWG necesitan un conversor (LibreDWG «dwg2dxf» u ODA File Converter) "
                            "que no está instalado en este servidor. Mientras tanto, exporta el plano a DXF.")
    full = _run_dwg2dxf(exe, data, minimal=False)
    if full:
        try:
            return analyze_dxf(full, units)
        except GeometryError:
            pass
    minimal = _run_dwg2dxf(exe, data, minimal=True)
    if not minimal:
        raise GeometryError("No se ha podido convertir el DWG a DXF. Exporta el plano a DXF desde tu programa de CAD.")
    if units == "auto" and full:
        units = UNIT_CODES.get(read_header_units(full) or 0, "auto")
    geom, contours = analyze_dxf(minimal, units)
    geom.warnings.append("DWG leído en modo simplificado: revisa la vista previa (los bloques podrían no aparecer).")
    return geom, contours


def _part_or_404(db: Session, part_id: str) -> Part:
    p = db.get(Part, part_id)
    if not p:
        raise HTTPException(404, "Pieza no encontrada")
    return p


def _quote_options(db: Session) -> dict:
    """Combinaciones disponibles por máquina para filtrar los desplegables en el navegador."""
    out = {}
    for m in db.scalars(select(Machine).where(Machine.active.is_(True)).order_by(Machine.name)):
        fam: dict[str, dict[str, list[float]]] = {}
        for p in m.cut_params:
            fam.setdefault(p.material_family, {}).setdefault(p.gas, []).append(p.thickness_mm)
        out[m.id] = {"name": m.name, "max": m.max_thickness_mm, "families": fam}
    return out


@router.get("")
def list_parts(request: Request, db: Session = Depends(get_db)):
    parts = db.scalars(select(Part).order_by(Part.created_at.desc()).limit(100)).all()
    return templates.TemplateResponse(request, "parts/list.html", {
        "parts": parts, "max_mb": settings.max_upload_mb})


@router.post("")
async def upload_part(request: Request, file: UploadFile = File(...), units: str = Form("auto"),
                      db: Session = Depends(get_db)):
    def fail(msg: str, status: int = 422):
        parts = db.scalars(select(Part).order_by(Part.created_at.desc()).limit(100)).all()
        return templates.TemplateResponse(request, "parts/list.html", {
            "parts": parts, "max_mb": settings.max_upload_mb, "error": msg}, status_code=status)

    name = _SAFE_NAME.sub("_", os.path.basename(file.filename or "pieza"))[:200] or "pieza"
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        return fail("Formato no admitido. Sube un plano DXF o DWG.")
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        return fail(f"El fichero supera el máximo de {settings.max_upload_mb} MB.", 413)
    kind = _sniff(data)
    if kind is None or f".{kind}" != ext:
        return fail("El contenido del fichero no corresponde a un DXF/DWG válido.")
    try:
        if units not in UNIT_CHOICES:
            return fail("Unidad no válida.")
        geom, contours = _analyze_dwg(data, units) if kind == "dwg" else analyze_dxf(data, units)
        svg = render_svg(contours)
    except GeometryError as exc:
        return fail(str(exc))
    part = Part(filename=name, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
                metrics=geom.to_dict(), svg=svg)
    db.add(part)
    db.commit()
    # El fichero original no se almacena: solo métricas y vista previa
    return RedirectResponse(f"/piezas/{part.id}", status_code=303)


@router.get("/{part_id}")
def part_detail(part_id: str, request: Request, db: Session = Depends(get_db)):
    part = _part_or_404(db, part_id)
    materials = db.scalars(select(Material).where(Material.active.is_(True)).order_by(Material.family, Material.code)).all()
    options = _quote_options(db)
    return templates.TemplateResponse(request, "parts/detail.html", {
        "part": part, "g": part.metrics, "materials": materials,
        "options_json": json.dumps(options), "has_machines": bool(options),
        "materials_json": json.dumps({m.id: m.family for m in materials}),
    })


@router.post("/{part_id}/calcular")
async def calculate(part_id: str, request: Request, db: Session = Depends(get_db)):
    part = _part_or_404(db, part_id)
    form = await request.form()

    def err(msg):
        return templates.TemplateResponse(request, "parts/_quote.html", {"error": msg}, status_code=422)

    try:
        machine = db.get(Machine, int(form.get("machine_id", 0)))
        material = db.get(Material, int(form.get("material_id", 0)))
        thickness = float(str(form.get("thickness_mm", "")).replace(",", "."))
        quantity = int(form.get("quantity", 1))
        gas = str(form.get("gas", ""))
    except ValueError:
        return err("Revisa los datos: espesor y cantidad deben ser numéricos.")
    if not machine or not machine.active:
        return err("Selecciona una máquina activa.")
    if not material:
        return err("Selecciona un material.")
    if not (settings.min_thickness_mm <= thickness <= settings.max_thickness_mm):
        return err(f"El espesor debe estar entre {settings.min_thickness_mm:g} y {settings.max_thickness_mm:g} mm.")
    if not (1 <= quantity <= 100000):
        return err("La cantidad debe estar entre 1 y 100 000.")
    cost_settings = db.scalar(select(CostSettings))
    param = machine.interpolated_param(material.family, thickness, gas)
    try:
        q = compute_quote(part.metrics, machine, param, material, cost_settings,
                          QuoteInput(quantity=quantity, thickness_mm=thickness, gas=gas))
    except CostingError as exc:
        return err(str(exc))
    return templates.TemplateResponse(request, "parts/_quote.html", {
        "q": q, "machine": machine, "material": material, "thickness": thickness, "gas": gas,
        "param": param, "s": cost_settings})


@router.post("/{part_id}/eliminar")
def delete_part(part_id: str, db: Session = Depends(get_db)):
    db.delete(_part_or_404(db, part_id))
    db.commit()
    return RedirectResponse("/piezas", status_code=303)
