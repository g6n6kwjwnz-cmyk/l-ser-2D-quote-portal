"""Anidado de chapa: automático + editor manual, formatos de chapa y almacén de retales."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings as app_settings
from ..db import get_db
from ..models import NEST_STATUSES, REMNANT_STATUSES, Material, Nest, Part, QuoteRequest, Remnant, SheetFormat
from ..nesting import service as svc
from ..web import templates

router = APIRouter(tags=["anidado"])
MAX_PARTS_PER_NEST = 2000


def _nest_or_404(db: Session, nest_id: int) -> Nest:
    n = db.get(Nest, nest_id)
    if not n:
        raise HTTPException(404, "Anidado no encontrado")
    return n


def _num(raw, default=None) -> float | None:
    try:
        return float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default


def _new_nest(db: Session, name: str, material: Material, thickness: float, fmt: SheetFormat,
              items: list[dict], use_remnants: bool, request_id: int | None = None) -> Nest:
    cfg = svc.settings(db)
    gap, edge = cfg.gap_edge(material.family, thickness)
    n = Nest(name=name[:120], material_id=material.id, thickness_mm=thickness, gap_mm=gap, edge_mm=edge,
             sheet_w_mm=fmt.width_mm, sheet_h_mm=fmt.height_mm, use_remnants=use_remnants,
             allow_rotation=cfg.allow_rotation, items=items, request_id=request_id)
    db.add(n)
    db.flush()
    svc.run_auto(db, n)
    return n


def _reprice(db: Session, request_id: int | None) -> None:
    """Recalcula las líneas de la petición para aplicar el aprovechamiento del anidado (respeta precios manuales)."""
    if not request_id:
        return
    from ..quotes.service import price_item
    from .requests import EDITABLE
    req = db.get(QuoteRequest, request_id)
    if not req or req.status not in EDITABLE:
        return
    db.flush()
    for it in req.items:
        auto = it.unit_price is None or it.calc_unit_price is None or abs(it.unit_price - it.calc_unit_price) < 0.005
        price_item(db, it, it.machine, it.gas or None, keep_manual_price=not auto)


# ------------------------------------------------------------------ listado y alta
@router.get("/anidado")
def nest_list(request: Request, db: Session = Depends(get_db), error: str = ""):
    nests = db.scalars(select(Nest).order_by(Nest.updated_at.desc()).limit(200)).all()
    materials = db.scalars(select(Material).where(Material.active.is_(True)).order_by(Material.family, Material.code)).all()
    formats = db.scalars(select(SheetFormat).where(SheetFormat.active.is_(True)).order_by(SheetFormat.width_mm.desc())).all()
    parts = db.scalars(select(Part).order_by(Part.created_at.desc()).limit(100)).all()
    return templates.TemplateResponse(request, "nesting/list.html", {
        "nests": nests, "materials": materials, "formats": formats, "parts": parts, "statuses": NEST_STATUSES,
        "error": error, "default_format": svc.default_format(db)})


@router.post("/anidado")
async def nest_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    material = db.get(Material, int(_num(form.get("material_id"), 0)))
    fmt = db.get(SheetFormat, int(_num(form.get("format_id"), 0)))
    thickness = _num(form.get("thickness_mm"))
    err = None
    items = []
    for key, val in form.items():
        m = re.fullmatch(r"qty-([0-9a-f\-]{36})", key)
        q = int(_num(val, 0) or 0)
        if m and q > 0 and db.get(Part, m.group(1)):
            items.append({"part_id": m.group(1), "quantity": min(q, MAX_PARTS_PER_NEST)})
    if not material:
        err = "Selecciona un material."
    elif not fmt:
        err = "Selecciona un formato de chapa."
    elif thickness is None or not (app_settings.min_thickness_mm <= thickness <= app_settings.max_thickness_mm):
        err = f"El espesor debe estar entre {app_settings.min_thickness_mm:g} y {app_settings.max_thickness_mm:g} mm."
    elif not items:
        err = "Indica la cantidad de al menos una pieza."
    elif sum(i["quantity"] for i in items) > MAX_PARTS_PER_NEST:
        err = f"Máximo {MAX_PARTS_PER_NEST} piezas por anidado."
    if err:
        return RedirectResponse(f"/anidado?error={err}", status_code=303)
    name = (str(form.get("name") or "").strip() or f"{material.code} {thickness:g} mm")[:120]
    n = _new_nest(db, name, material, thickness, fmt, items, form.get("use_remnants") == "on")
    db.commit()
    return RedirectResponse(f"/anidado/{n.id}", status_code=303)


@router.post("/anidado/desde-peticion/{req_id}")
def nest_from_request(req_id: int, db: Session = Depends(get_db)):
    """Un anidado por cada combinación material + espesor de la petición (se rehacen si ya existían en borrador)."""
    req = db.get(QuoteRequest, req_id)
    if not req:
        raise HTTPException(404, "Petición no encontrada")
    fmt = svc.default_format(db)
    if fmt is None:
        return RedirectResponse("/anidado/ajustes?error=Da de alta un formato de chapa.", status_code=303)
    groups: dict[tuple[int, float], list] = {}
    for it in req.items:
        groups.setdefault((it.material_id, round(it.thickness_mm, 2)), []).append(it)
    first = None
    for (mat_id, t), its in groups.items():
        material = db.get(Material, mat_id)
        old = [n for n in db.scalars(select(Nest).where(Nest.request_id == req.id, Nest.material_id == mat_id))
               if abs(n.thickness_mm - t) < 0.01]
        if any(n.status == "confirmado" for n in old):
            first = first or next(n for n in old if n.status == "confirmado")
            continue
        for n in old:
            db.delete(n)
        qty: dict[str, int] = {}
        for it in its:
            qty[it.part_id] = qty.get(it.part_id, 0) + it.quantity
        n = _new_nest(db, f"{req.number} · {material.code} {t:g} mm", material, t, fmt,
                      [{"part_id": k, "quantity": v} for k, v in qty.items()],
                      use_remnants=not any(it.supply == "cliente" for it in its), request_id=req.id)
        first = first or n
    _reprice(db, req.id)
    db.commit()
    return RedirectResponse(f"/anidado/{first.id}" if first else "/anidado", status_code=303)


# ------------------------------------------------------------------ editor
def _editor_ctx(db: Session, n: Nest) -> dict:
    parts = svc.load_parts(db, n)
    ev = svc.evaluate(db, n, n.sheets, parts)
    remnants = {r.id: r for r in db.scalars(select(Remnant).where(Remnant.id.in_(
        [s["source"]["remnant_id"] for s in n.sheets if s["source"].get("kind") == "retal"] or [0])))}
    siblings = []
    if n.request_id:
        siblings = db.scalars(select(Nest).where(Nest.request_id == n.request_id).order_by(Nest.id)).all()
    data = {"id": n.id, "gap": n.gap_mm, "edge": n.edge_mm, "editable": n.status == "borrador",
            "rotation": n.allow_rotation, "format": {"w": n.sheet_w_mm, "h": n.sheet_h_mm},
            "items": n.items, "parts": svc.display_paths(parts, svc.rotations(n)), "sheets": n.sheets, "eval": ev}
    return {"n": n, "ev": ev, "parts": parts, "remnants": remnants, "siblings": siblings,
            "statuses": NEST_STATUSES, "data_json": json.dumps(data),
            "old_parts": [p for p in parts.values() if not p.detailed]}


@router.get("/anidado/{nest_id:int}")
def nest_editor(nest_id: int, request: Request, db: Session = Depends(get_db), msg: str = ""):
    n = _nest_or_404(db, nest_id)
    ctx = _editor_ctx(db, n)
    db.rollback()   # la evaluación no modifica nada al mostrar
    return templates.TemplateResponse(request, "nesting/editor.html", {**ctx, "msg": msg})


@router.post("/anidado/{nest_id:int}/auto")
def nest_auto(nest_id: int, db: Session = Depends(get_db)):
    n = _nest_or_404(db, nest_id)
    if n.status != "borrador":
        raise HTTPException(409, "El anidado está confirmado.")
    svc.run_auto(db, n)
    _reprice(db, n.request_id)
    db.commit()
    return RedirectResponse(f"/anidado/{n.id}?msg=auto", status_code=303)


async def _sheets_from_json(request: Request, db: Session, n: Nest) -> list:
    if int(request.headers.get("content-length") or 0) > 2_000_000:
        raise ValueError("Petición demasiado grande.")
    body = await request.json()
    keys = {it["part_id"] for it in n.items}
    rem_ids = {s["source"]["remnant_id"] for s in n.sheets if s["source"].get("kind") == "retal"}
    rem_ids |= {r.id for r in svc.available_remnants(db, n)}
    return svc.clean_sheets(body.get("sheets"), n, keys, rem_ids)


@router.post("/anidado/{nest_id:int}/comprobar")
async def nest_check(nest_id: int, request: Request, db: Session = Depends(get_db)):
    n = _nest_or_404(db, nest_id)
    try:
        sheets = await _sheets_from_json(request, db, n)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return JSONResponse({"error": str(exc) or "Datos no válidos."}, status_code=422)
    ev = svc.evaluate(db, n, sheets)
    db.rollback()
    return ev


@router.post("/anidado/{nest_id:int}/guardar")
async def nest_save(nest_id: int, request: Request, db: Session = Depends(get_db)):
    n = _nest_or_404(db, nest_id)
    if n.status != "borrador":
        return JSONResponse({"error": "El anidado está confirmado: vuelve a borrador para editarlo."}, status_code=409)
    try:
        sheets = await _sheets_from_json(request, db, n)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return JSONResponse({"error": str(exc) or "Datos no válidos."}, status_code=422)
    n.sheets = sheets
    ev = svc.evaluate(db, n, sheets)
    _reprice(db, n.request_id)
    db.commit()
    return ev


@router.post("/anidado/{nest_id:int}/confirmar")
async def nest_confirm(nest_id: int, request: Request, db: Session = Depends(get_db)):
    """Confirma: los retales usados salen del almacén y los sobrantes marcados entran como retales nuevos."""
    n = _nest_or_404(db, nest_id)
    if n.status != "borrador":
        return RedirectResponse(f"/anidado/{n.id}", status_code=303)
    form = await request.form()
    ev = svc.evaluate(db, n, n.sheets)
    if ev["conflicts"]:
        db.rollback()
        return RedirectResponse(f"/anidado/{n.id}?msg=conflictos", status_code=303)
    location = str(form.get("location") or "").strip()[:80]
    for i, sh in enumerate(n.sheets):
        if sh["source"].get("kind") == "retal":
            r = db.get(Remnant, sh["source"]["remnant_id"])
            if r and r.status == "disponible":
                r.status, r.used_in_nest_id = "usado", n.id
        off = ev["sheets"][i]["offcut"]
        if off and form.get(f"offcut-{i}") == "on":
            db.add(Remnant(code=svc.next_remnant_code(db), material_id=n.material_id, thickness_mm=n.thickness_mm,
                           width_mm=off["w"], height_mm=off["h"], location=location, source_nest_id=n.id,
                           notes=f"Sobrante de «{n.name}», chapa {i + 1}"[:300]))
            db.flush()
    n.status = "confirmado"
    _reprice(db, n.request_id)
    db.commit()
    return RedirectResponse(f"/anidado/{n.id}?msg=confirmado", status_code=303)


@router.post("/anidado/{nest_id:int}/borrador")
def nest_reopen(nest_id: int, db: Session = Depends(get_db)):
    """Vuelve a borrador: devuelve al almacén los retales que usaba (los sobrantes ya creados se mantienen)."""
    n = _nest_or_404(db, nest_id)
    for r in db.scalars(select(Remnant).where(Remnant.used_in_nest_id == n.id)):
        r.status, r.used_in_nest_id = "disponible", None
    n.status = "borrador"
    db.commit()
    return RedirectResponse(f"/anidado/{n.id}", status_code=303)


@router.get("/anidado/{nest_id:int}/dxf")
def nest_dxf(nest_id: int, db: Session = Depends(get_db)):
    n = _nest_or_404(db, nest_id)
    data = svc.export_dxf(db, n)
    db.rollback()
    name = re.sub(r"[^\w\-]+", "_", n.name)[:60] or "anidado"
    return Response(data, media_type="application/dxf", headers={
        "Content-Disposition": f'attachment; filename="anidado_{n.id}_{name}.dxf"', "Cache-Control": "no-store"})


@router.post("/anidado/{nest_id:int}/eliminar")
def nest_delete(nest_id: int, db: Session = Depends(get_db)):
    n = _nest_or_404(db, nest_id)
    for r in db.scalars(select(Remnant).where(Remnant.used_in_nest_id == n.id)):
        r.status, r.used_in_nest_id = "disponible", None
    rid = n.request_id
    db.delete(n)
    db.flush()
    _reprice(db, rid)
    db.commit()
    return RedirectResponse("/anidado", status_code=303)


# ------------------------------------------------------------------ ajustes: separaciones y formatos
def _parse_gap_table(text: str) -> list:
    rows = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        nums = [_num(x) for x in re.split(r"[;\s]+", line.strip().replace(",", "."))]
        if len(nums) != 3 or any(v is None or v < 0 for v in nums) or nums[0] <= 0:
            raise ValueError(f"Línea no válida: «{line.strip()}». Formato: espesor_máx separación borde")
        rows.append(nums)
    if not rows:
        raise ValueError("La tabla no puede quedar vacía.")
    return sorted(rows)


@router.get("/anidado/ajustes")
def nest_settings_page(request: Request, db: Session = Depends(get_db), error: str = "", ok: str = ""):
    cfg = svc.settings(db)
    db.commit()
    formats = db.scalars(select(SheetFormat).order_by(SheetFormat.width_mm.desc())).all()
    table_text = "\n".join(" ".join(f"{v:g}" for v in r) for r in sorted(cfg.gap_table))
    return templates.TemplateResponse(request, "nesting/settings.html", {
        "cfg": cfg, "formats": formats, "table_text": table_text, "error": error, "ok": ok})


@router.post("/anidado/ajustes")
async def nest_settings_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    cfg = svc.settings(db)
    try:
        cfg.gap_table = _parse_gap_table(str(form.get("gap_table") or ""))
        extra = _num(form.get("extra_gap_mm"))
        from_mm = _num(form.get("extra_gap_from_mm"))
        rmin = _num(form.get("remnant_min_mm"))
        if None in (extra, from_mm, rmin) or extra < 0 or from_mm < 0 or rmin < 50:
            raise ValueError("Revisa los valores numéricos (retal mínimo ≥ 50 mm).")
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/anidado/ajustes?error={exc}", status_code=303)
    cfg.extra_gap_mm, cfg.extra_gap_from_mm, cfg.remnant_min_mm = extra, from_mm, rmin
    cfg.extra_gap_families = [f for f in ("acero", "inox", "aluminio", "galvanizado") if form.get(f"fam-{f}") == "on"]
    cfg.allow_rotation = form.get("allow_rotation") == "on"
    db.commit()
    return RedirectResponse("/anidado/ajustes?ok=1", status_code=303)


@router.post("/anidado/formatos")
async def format_add(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    w, h = _num(form.get("width_mm")), _num(form.get("height_mm"))
    if not w or not h or not (100 <= w <= 20000 and 100 <= h <= 20000):
        return RedirectResponse("/anidado/ajustes?error=Medidas de formato no válidas (100–20000 mm).", status_code=303)
    w, h = max(w, h), min(w, h)
    name = (str(form.get("name") or "").strip() or f"{w:g}×{h:g}")[:60]
    db.add(SheetFormat(name=name, width_mm=w, height_mm=h))
    db.commit()
    return RedirectResponse("/anidado/ajustes", status_code=303)


@router.post("/anidado/formatos/{fmt_id:int}")
async def format_update(fmt_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    fmt = db.get(SheetFormat, fmt_id)
    if not fmt:
        raise HTTPException(404)
    action = form.get("accion")
    if action == "predeterminado":
        for f in db.scalars(select(SheetFormat)):
            f.is_default = f.id == fmt.id
        fmt.active = True
    elif action == "estado":
        fmt.active = not fmt.active
        if not fmt.active:
            fmt.is_default = False
    db.commit()
    return RedirectResponse("/anidado/ajustes", status_code=303)


# ------------------------------------------------------------------ almacén de retales
@router.get("/retales")
def remnant_list(request: Request, db: Session = Depends(get_db), estado: str = "disponible", error: str = ""):
    q = select(Remnant).order_by(Remnant.created_at.desc())
    if estado in REMNANT_STATUSES:
        q = q.where(Remnant.status == estado)
    materials = db.scalars(select(Material).where(Material.active.is_(True)).order_by(Material.family, Material.code)).all()
    return templates.TemplateResponse(request, "nesting/remnants.html", {
        "remnants": db.scalars(q.limit(500)).all(), "materials": materials, "statuses": REMNANT_STATUSES,
        "estado": estado, "error": error})


@router.post("/retales")
async def remnant_add(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    material = db.get(Material, int(_num(form.get("material_id"), 0)))
    t, w, h = (_num(form.get(k)) for k in ("thickness_mm", "width_mm", "height_mm"))
    if not material or t is None or not (app_settings.min_thickness_mm <= t <= app_settings.max_thickness_mm) \
            or not w or not h or not (20 <= w <= 20000 and 20 <= h <= 20000):
        return RedirectResponse("/retales?error=Revisa material, espesor y medidas.", status_code=303)
    db.add(Remnant(code=svc.next_remnant_code(db), material_id=material.id, thickness_mm=t, width_mm=max(w, h),
                   height_mm=min(w, h), location=str(form.get("location") or "").strip()[:80],
                   notes=str(form.get("notes") or "").strip()[:300]))
    db.commit()
    return RedirectResponse("/retales", status_code=303)


@router.post("/retales/{rem_id:int}")
async def remnant_update(rem_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    r = db.get(Remnant, rem_id)
    if not r:
        raise HTTPException(404)
    action = form.get("accion")
    if action == "eliminar":
        db.delete(r)
    elif action in REMNANT_STATUSES:
        r.status = action
        if action == "disponible":
            r.used_in_nest_id = None
    db.commit()
    return RedirectResponse("/retales", status_code=303)
