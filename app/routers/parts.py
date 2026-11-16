"""Carga de piezas (DXF/DWG), análisis geométrico, vista previa y cálculo de coste."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..costing.engine import CostingError, QuoteInput, compute_quote
from ..db import get_db
from ..geometry import editing
from ..geometry.dxf_engine import UNIT_CHOICES, UNIT_CODES, GeometryError, analyze_dxf, read_header_units, render_svg
from ..models import MATERIAL_FAMILIES, REQUEST_STATUSES, CostSettings, Machine, Material, Part, QuoteRequest, RequestItem
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
    try:
        geom, contours = analyze_dxf(minimal, units)
    except GeometryError as exc:
        raise GeometryError("No se ha podido leer la geometría de este DWG (el conversor no la ha extraído). "
                            "Exporta el plano a DXF desde tu programa de CAD y súbelo de nuevo.") from exc
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


class UploadError(ValueError):
    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


def _preview_contours(contours, max_points: int = 200) -> list:
    """Contornos simplificados para miniaturas (PDF de oferta). Sin datos del fichero original."""
    out = []
    for c in contours[:300]:
        step = max(1, len(c.points) // max_points)
        pts = [[round(x, 2), round(y, 2)] for x, y in c.points[::step]]
        out.append([pts, c.closed, c.kind])
    return out


async def part_from_upload(file: UploadFile, units: str = "auto") -> Part:
    """Valida y analiza un plano subido. Devuelve la pieza (sin guardar) o lanza UploadError.

    El fichero original nunca se guarda: solo métricas, vista previa y huella SHA-256.
    """
    name = _SAFE_NAME.sub("_", os.path.basename(file.filename or "pieza"))[:200] or "pieza"
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise UploadError("Formato no admitido. Sube un plano DXF o DWG.")
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise UploadError(f"El fichero supera el máximo de {settings.max_upload_mb} MB.", 413)
    kind = _sniff(data)
    if kind is None or f".{kind}" != ext:
        raise UploadError("El contenido del fichero no corresponde a un DXF/DWG válido.")
    if units not in UNIT_CHOICES:
        raise UploadError("Unidad no válida.")
    try:
        geom, contours = _analyze_dwg(data, units) if kind == "dwg" else analyze_dxf(data, units)
        svg = render_svg(contours)
    except GeometryError as exc:
        raise UploadError(str(exc)) from exc
    metrics = geom.to_dict()
    metrics["preview"] = _preview_contours(contours)
    metrics["geometry"] = _preview_contours(contours, max_points=4000)   # para anidado y exportación DXF
    return Part(filename=name, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
                metrics=metrics, svg=svg)


@router.post("")
async def upload_part(request: Request, file: UploadFile = File(...), units: str = Form("auto"),
                      db: Session = Depends(get_db)):
    try:
        part = await part_from_upload(file, units)
    except UploadError as exc:
        parts = db.scalars(select(Part).order_by(Part.created_at.desc()).limit(100)).all()
        return templates.TemplateResponse(request, "parts/list.html", {
            "parts": parts, "max_mb": settings.max_upload_mb, "error": str(exc)}, status_code=exc.status)
    db.add(part)
    db.commit()
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


# --------------------------------------------------------------------------- editor de plano

LOCKED_STATUSES = ("enviada", "aceptada")
MAX_EDIT_BODY = 12 * 1024 * 1024
EDIT_LOG_KEEP = 30


def _linked_requests(db: Session, part_id: str) -> list[QuoteRequest]:
    return list(db.scalars(select(QuoteRequest).join(RequestItem).where(RequestItem.part_id == part_id)
                           .distinct().order_by(QuoteRequest.created_at.desc())))


def _locked_by(reqs: list[QuoteRequest]) -> list[QuoteRequest]:
    return [r for r in reqs if r.status in LOCKED_STATUSES]


def _editor_contours(part: Part) -> list:
    m = part.metrics or {}
    return [{"points": pts, "closed": bool(closed), "kind": kind}
            for pts, closed, kind in (m.get("geometry") or m.get("preview") or [])]


async def _json_body(request: Request) -> dict:
    """Solo JSON (un formulario de otra web no puede enviarlo sin CORS) y con tamaño limitado."""
    if not (request.headers.get("content-type") or "").startswith("application/json"):
        raise editing.EditError("Tipo de contenido no admitido.")
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_EDIT_BODY:
        raise editing.EditError("La geometría enviada es demasiado grande; simplifícala.")
    body = await request.body()
    if len(body) > MAX_EDIT_BODY:
        raise editing.EditError("La geometría enviada es demasiado grande; simplifícala.")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise editing.EditError("Datos no válidos.") from exc
    if not isinstance(data, dict):
        raise editing.EditError("Datos no válidos.")
    return data


def _rules(data: dict) -> editing.Rules | None:
    try:
        t = float(data.get("thickness_mm") or 0)
    except (TypeError, ValueError):
        return None
    if not (settings.min_thickness_mm <= t <= settings.max_thickness_mm):
        return None
    fam = data.get("family") if data.get("family") in MATERIAL_FAMILIES else "acero"
    return editing.Rules(thickness=t, family=fam)


def _err(msg: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


@router.get("/{part_id}/editor")
def part_editor(part_id: str, request: Request, db: Session = Depends(get_db)):
    part = _part_or_404(db, part_id)
    reqs = _linked_requests(db, part.id)
    return templates.TemplateResponse(request, "parts/editor.html", {
        "part": part, "g": part.metrics, "reqs": reqs, "locked": _locked_by(reqs), "statuses": REQUEST_STATUSES,
        "families": MATERIAL_FAMILIES, "edits": (part.metrics or {}).get("edits") or {},
        "has_original": bool((part.metrics or {}).get("original")),
        "min_t": settings.min_thickness_mm, "max_t": settings.max_thickness_mm})


@router.get("/{part_id}/geometria.json")
def part_geometry(part_id: str, db: Session = Depends(get_db)):
    part = _part_or_404(db, part_id)
    return JSONResponse({"contours": _editor_contours(part)}, headers={"Cache-Control": "no-store"})


@router.post("/{part_id}/geometria/analizar")
async def analyze_geometry(part_id: str, request: Request, db: Session = Depends(get_db)):
    _part_or_404(db, part_id)
    try:
        data = await _json_body(request)
        polys = editing.parse_contours(data.get("contours"))
        return JSONResponse(editing.analyze(polys, _rules(data)))
    except editing.EditError as exc:
        return _err(str(exc))


def _save_geometry(part: Part, polys, user_email: str, action: str) -> None:
    old = part.metrics or {}
    metrics, svg = editing.rebuild(polys, old.get("units_source", "mm"))
    keep_original = old.get("original")
    if not keep_original:
        # copia de la geometría tal como se analizó al subir el plano, para poder restaurarla
        keep_original = {k: v for k, v in old.items() if k not in ("edits", "original")}
        keep_original["svg"] = part.svg
    metrics["units_source"] = old.get("units_source", metrics["units_source"])
    metrics["original"] = keep_original
    log = list((old.get("edits") or {}).get("log") or [])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.append({"at": now, "by": user_email, "action": action})
    metrics["edits"] = {"count": int((old.get("edits") or {}).get("count", 0)) + 1, "last_at": now,
                        "last_by": user_email, "log": log[-EDIT_LOG_KEEP:]}
    metrics["warnings"] = metrics.get("warnings", []) + ["Geometría modificada en el editor de plano."]
    part.metrics = metrics      # asignar un dict nuevo para que SQLAlchemy detecte el cambio
    part.svg = svg


def _user_email(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return getattr(user, "email", "") or "desconocido"


@router.post("/{part_id}/geometria")
async def save_geometry(part_id: str, request: Request, db: Session = Depends(get_db)):
    part = _part_or_404(db, part_id)
    try:
        data = await _json_body(request)
        polys = editing.parse_contours(data.get("contours"))
        as_new = bool(data.get("as_new"))
        if as_new:
            base = Path(part.filename)
            new = Part(filename=f"{base.stem} (editada){base.suffix}"[:255], sha256=part.sha256,
                       size_bytes=part.size_bytes, svg=part.svg,
                       metrics={k: v for k, v in (part.metrics or {}).items() if k not in ("edits", "source_part_id")})
            _save_geometry(new, polys, _user_email(request), "guardar como nueva pieza")
            new.metrics = {**new.metrics, "source_part_id": part.id}
            db.add(new)
            db.commit()
            return JSONResponse({"ok": True, "redirect": f"/piezas/{new.id}/editor"})
        locked = _locked_by(_linked_requests(db, part.id))
        if locked:
            nums = ", ".join(r.number or str(r.id) for r in locked)
            return _err(f"La pieza está en ofertas ya enviadas o aceptadas ({nums}). Guárdala como pieza nueva "
                        "para no alterar lo ofertado.", 409)
        _save_geometry(part, polys, _user_email(request), "editar")
    except editing.EditError as exc:
        return _err(str(exc))
    db.commit()
    return JSONResponse({"ok": True, "contours": _editor_contours(part)})


@router.post("/{part_id}/geometria/restaurar")
def restore_geometry(part_id: str, request: Request, db: Session = Depends(get_db)):
    part = _part_or_404(db, part_id)
    original = (part.metrics or {}).get("original")
    if not original:
        return RedirectResponse(f"/piezas/{part.id}/editor", status_code=303)
    if _locked_by(_linked_requests(db, part.id)):
        raise HTTPException(409, "La pieza está en ofertas ya enviadas o aceptadas; no se puede restaurar.")
    restored = {k: v for k, v in original.items() if k != "svg"}
    edits = dict((part.metrics or {}).get("edits") or {})
    log = list(edits.get("log") or [])
    log.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": _user_email(request),
                "action": "restaurar original"})
    edits["log"] = log[-EDIT_LOG_KEEP:]
    restored["edits"] = edits
    part.metrics = restored
    part.svg = original.get("svg") or part.svg
    db.commit()
    return RedirectResponse(f"/piezas/{part.id}/editor", status_code=303)


@router.post("/{part_id}/eliminar")
def delete_part(part_id: str, db: Session = Depends(get_db)):
    db.delete(_part_or_404(db, part_id))
    db.commit()
    return RedirectResponse("/piezas", status_code=303)
