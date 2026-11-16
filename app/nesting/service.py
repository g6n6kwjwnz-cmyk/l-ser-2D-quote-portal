"""Anidados guardados: cálculo automático, validación de disposiciones, retales y exportación DXF."""
from __future__ import annotations

import io
import math
from dataclasses import dataclass

import ezdxf
from shapely import affinity
from shapely.geometry import Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Nest, NestSettings, Part, Remnant, SheetFormat
from .engine import NestItem, Placement, SheetStock, check_layout, nest, normalized, part_polygon, placed_shape

ROTATIONS_ALL = (0, 90, 180, 270)
ROTATIONS_NONE = (0,)


def settings(db: Session) -> NestSettings:
    s = db.scalar(select(NestSettings))
    if s is None:
        s = NestSettings()
        db.add(s)
        db.flush()
    return s


def default_format(db: Session) -> SheetFormat | None:
    fmts = db.scalars(select(SheetFormat).where(SheetFormat.active.is_(True))).all()
    return next((f for f in fmts if f.is_default), fmts[0] if fmts else None)


def part_contours(part: Part) -> list:
    """Contornos de la pieza: geometría detallada si existe (piezas nuevas) o la miniatura (piezas antiguas)."""
    m = part.metrics or {}
    return m.get("geometry") or m.get("preview") or []


@dataclass
class PartInfo:
    part: Part
    shape: Polygon
    detailed: bool


def load_parts(db: Session, nest_obj: Nest) -> dict[str, PartInfo]:
    out = {}
    for it in nest_obj.items:
        part = db.get(Part, it["part_id"])
        if part is None:
            continue
        shape = part_polygon(part_contours(part))
        if shape is None or shape.is_empty:
            continue
        out[part.id] = PartInfo(part, shape, bool((part.metrics or {}).get("geometry")))
    return out


def rotations(nest_obj: Nest) -> tuple[int, ...]:
    return ROTATIONS_ALL if nest_obj.allow_rotation else ROTATIONS_NONE


def available_remnants(db: Session, nest_obj: Nest) -> list[Remnant]:
    q = select(Remnant).where(Remnant.material_id == nest_obj.material_id, Remnant.status == "disponible")
    rems = [r for r in db.scalars(q) if abs(r.thickness_mm - nest_obj.thickness_mm) < 0.01]
    return sorted(rems, key=lambda r: r.width_mm * r.height_mm)   # primero los más pequeños


def run_auto(db: Session, nest_obj: Nest) -> dict:
    """Anidado automático: usa primero los retales del almacén (si se permite) y después el formato."""
    parts = load_parts(db, nest_obj)
    items = [NestItem(it["part_id"], parts[it["part_id"]].shape, int(it["quantity"])) for it in nest_obj.items if it["part_id"] in parts]
    stock = []
    if nest_obj.use_remnants:
        stock = [SheetStock(f"retal:{r.id}", r.width_mm, r.height_mm, 1) for r in available_remnants(db, nest_obj)]
    res = nest(items, nest_obj.sheet_w_mm, nest_obj.sheet_h_mm, nest_obj.gap_mm, nest_obj.edge_mm,
               rotations=rotations(nest_obj), stock=stock)
    sheets = []
    for src, placements in zip(res.sheet_sources, res.sheets):
        key = src["key"]
        source = {"kind": "retal", "remnant_id": int(key.split(":")[1])} if key.startswith("retal:") else {"kind": "formato"}
        source.update(w=src["w"], h=src["h"])
        sheets.append({"source": source, "placements": [p.to_dict() for p in placements]})
    nest_obj.sheets = sheets
    return evaluate(db, nest_obj, sheets, parts)


def _offcut(sheet_w: float, sheet_h: float, polys: list[Polygon], gap: float, min_side: float) -> dict | None:
    """Retal aprovechable: la mayor franja libre a la derecha o arriba de las piezas."""
    if not polys:
        return None
    maxx = max(p.bounds[2] for p in polys)
    maxy = max(p.bounds[3] for p in polys)
    right = {"x": maxx + gap, "y": 0.0, "w": sheet_w - maxx - gap, "h": sheet_h}
    top = {"x": 0.0, "y": maxy + gap, "w": sheet_w, "h": sheet_h - maxy - gap}
    best = max((right, top), key=lambda r: r["w"] * r["h"])
    if min(best["w"], best["h"]) < min_side:
        return None
    return {k: round(v, 1) for k, v in best.items()}


def evaluate(db: Session, nest_obj: Nest, sheets: list, parts: dict[str, PartInfo] | None = None) -> dict:
    """Comprueba la disposición, calcula aprovechamiento y retales y actualiza los totales del anidado."""
    parts = parts if parts is not None else load_parts(db, nest_obj)
    shapes = {k: v.shape for k, v in parts.items()}
    cfg = settings(db)
    out_sheets, total_conf = [], 0
    parts_area = sheet_area = consumed = 0.0
    placed_count: dict[str, int] = {}
    for sh in sheets:
        w, h = float(sh["source"]["w"]), float(sh["source"]["h"])
        pls = [p for p in sh["placements"] if p["key"] in shapes]
        conflicts = check_layout(shapes, pls, w, h, nest_obj.gap_mm, nest_obj.edge_mm)
        bad = sorted({c["index"] for c in conflicts})
        polys = [placed_shape(shapes[p["key"]], Placement(p["key"], p["x"], p["y"], int(p["rotation"]))) for p in pls]
        area = sum(parts[p["key"]].part.metrics.get("net_area_mm2", shapes[p["key"]].area) for p in pls)
        offcut = _offcut(w, h, polys, nest_obj.gap_mm, cfg.remnant_min_mm)
        for p in pls:
            placed_count[p["key"]] = placed_count.get(p["key"], 0) + 1
        parts_area += area
        sheet_area += w * h
        consumed += w * h - (offcut["w"] * offcut["h"] if offcut else 0)
        total_conf += len(bad)
        out_sheets.append({"w": w, "h": h, "source": sh["source"], "conflicts": bad,
                           "reasons": {c["index"]: c["reason"] for c in conflicts},
                           "utilization": round(100 * area / (w * h), 1) if w * h else 0, "offcut": offcut,
                           "count": len(pls)})
    pending = {}
    for it in nest_obj.items:
        if it["part_id"] in parts:
            missing = int(it["quantity"]) - placed_count.get(it["part_id"], 0)
            if missing:
                pending[it["part_id"]] = missing
    nest_obj.utilization_pct = round(100 * parts_area / sheet_area, 1) if sheet_area else 0.0
    nest_obj.material_factor = round(consumed / parts_area, 4) if parts_area else 0.0
    nest_obj.conflicts = total_conf
    return {"sheets": out_sheets, "pending": pending, "utilization": nest_obj.utilization_pct,
            "material_factor": nest_obj.material_factor, "conflicts": total_conf,
            "sheet_count": len(sheets), "consumed_m2": round(consumed / 1e6, 3)}


def clean_sheets(raw, nest_obj: Nest, valid_keys: set[str], remnant_ids: set[int]) -> list:
    """Valida la disposición enviada por el editor (no se confía en el navegador)."""
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("Disposición no válida.")
    out = []
    for sh in raw:
        src = sh.get("source") or {}
        if src.get("kind") == "retal":
            rid = int(src.get("remnant_id", 0))
            if rid not in remnant_ids:
                raise ValueError("Retal no válido.")
            source = {"kind": "retal", "remnant_id": rid, "w": float(src["w"]), "h": float(src["h"])}
        else:
            source = {"kind": "formato", "w": nest_obj.sheet_w_mm, "h": nest_obj.sheet_h_mm}
        pls = []
        for p in (sh.get("placements") or [])[:5000]:
            key, rot = str(p.get("key")), int(p.get("rotation", 0)) % 360
            x, y = float(p.get("x")), float(p.get("y"))
            if key not in valid_keys or rot not in ROTATIONS_ALL or not (math.isfinite(x) and math.isfinite(y)):
                raise ValueError("Pieza o posición no válida.")
            if not nest_obj.allow_rotation and rot:
                raise ValueError("Este anidado no permite girar piezas.")
            pls.append({"key": key, "x": round(x, 3), "y": round(y, 3), "rotation": rot})
        out.append({"source": source, "placements": pls})
    return out


def display_paths(parts: dict[str, PartInfo], rots: tuple[int, ...]) -> dict:
    """Siluetas para el editor (SVG, mm, eje Y hacia arriba), ya normalizadas por giro."""
    out = {}
    for key, info in parts.items():
        simple = info.shape.simplify(0.2, preserve_topology=True)
        per_rot = {}
        for r in rots:
            ref = normalized(info.shape, r)
            rot = affinity.rotate(simple, r, origin=(0, 0)) if r else simple
            minx, miny, _, _ = (affinity.rotate(info.shape, r, origin=(0, 0)) if r else info.shape).bounds
            g = affinity.translate(rot, -minx, -miny)
            per_rot[r] = {"d": _svg_path(g), "w": round(ref.bounds[2], 2), "h": round(ref.bounds[3], 2)}
        out[key] = {"name": info.part.filename, "rot": per_rot, "detailed": info.detailed,
                    "area": round(info.part.metrics.get("net_area_mm2", info.shape.area), 1)}
    return out


def _svg_path(g) -> str:
    polys = getattr(g, "geoms", [g])
    parts = []
    for poly in polys:
        for ring in [poly.exterior, *poly.interiors]:
            pts = list(ring.coords)
            parts.append("M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in pts) + "Z")
    return "".join(parts)


def export_dxf(db: Session, nest_obj: Nest) -> bytes:
    """DXF con todas las chapas en fila: capa CHAPA (contorno), CORTE_EXT / CORTE_INT (piezas), RETAL y TEXTO."""
    parts = load_parts(db, nest_obj)
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    doc.header["$INSUNITS"] = 4
    for name, color in (("CHAPA", 8), ("CORTE_EXT", 1), ("CORTE_INT", 5), ("RETAL", 3), ("TEXTO", 7)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()
    ev = evaluate(db, nest_obj, nest_obj.sheets, parts)
    ox = 0.0
    for i, sh in enumerate(nest_obj.sheets):
        w, h = float(sh["source"]["w"]), float(sh["source"]["h"])
        msp.add_lwpolyline([(ox, 0), (ox + w, 0), (ox + w, h), (ox, h)], close=True, dxfattribs={"layer": "CHAPA"})
        label = f"Chapa {i + 1} · {w:g}x{h:g}"
        if sh["source"].get("kind") == "retal":
            label += f" · retal #{sh['source'].get('remnant_id')}"
        msp.add_text(label, height=max(10, h / 60), dxfattribs={"layer": "TEXTO"}).set_placement((ox, h + 10))
        for p in sh["placements"]:
            info = parts.get(p["key"])
            if info is None:
                continue
            r = int(p["rotation"])
            minx, miny, _, _ = (affinity.rotate(info.shape, r, origin=(0, 0)) if r else info.shape).bounds
            cos, sin = math.cos(math.radians(r)), math.sin(math.radians(r))
            for pts, closed, kind in part_contours(info.part):
                tp = [(ox + x * cos - y * sin - minx + p["x"], x * sin + y * cos - miny + p["y"]) for x, y in pts]
                if len(tp) >= 2:
                    layer = "CORTE_EXT" if kind == "exterior" else "CORTE_INT"
                    msp.add_lwpolyline(tp, close=bool(closed), dxfattribs={"layer": layer})
        off = ev["sheets"][i]["offcut"] if i < len(ev["sheets"]) else None
        if off:
            x0, y0 = ox + off["x"], off["y"]
            msp.add_lwpolyline([(x0, y0), (x0 + off["w"], y0), (x0 + off["w"], y0 + off["h"]), (x0, y0 + off["h"])],
                               close=True, dxfattribs={"layer": "RETAL"})
        ox += w + 200
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


def nest_for_item(db: Session, item) -> Nest | None:
    """Anidado guardado de la petición que contiene esta pieza (mismo material y espesor), sin conflictos."""
    if not item.request_id:
        return None
    q = select(Nest).where(Nest.request_id == item.request_id, Nest.material_id == item.material_id,
                           Nest.conflicts == 0).order_by(Nest.status.desc(), Nest.updated_at.desc())
    for n in db.scalars(q):
        if abs(n.thickness_mm - item.thickness_mm) < 0.01 and n.material_factor > 0 \
                and any(it["part_id"] == item.part_id for it in n.items):
            return n
    return None


def next_remnant_code(db: Session) -> str:
    n = db.scalar(select(Remnant.id).order_by(Remnant.id.desc()).limit(1)) or 0
    return f"R-{n + 1:04d}"
