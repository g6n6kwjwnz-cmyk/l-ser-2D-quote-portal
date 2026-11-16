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
from .engine import (NestItem, Placement, SheetStock, check_layout, compact, nest, offcut, optimize,
                     part_polygon, placed_shape)

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


ROTATION_STEPS = {90: "Cada 90°", 45: "Cada 45°", 15: "Cada 15°"}
ITEM_ROTATIONS = {"libre": "Libre", "180": "Solo 180°", "fijo": "Fijo"}
PRIORITIES = {"material": "Menos chapa consumida", "chapas": "Menos chapas", "retal": "Retal sobrante más grande"}
ORDERS_LABEL = {"area": "de mayor a menor superficie", "envolvente": "por rectángulo envolvente",
                "lado_largo": "por lado más largo", "perimetro": "por perímetro", "ancho": "por ancho"}


def rotations(nest_obj: Nest) -> tuple:
    if not nest_obj.allow_rotation:
        return ROTATIONS_NONE
    step = int((nest_obj.options or {}).get("rotation_step", 90))
    step = step if step in ROTATION_STEPS else 90
    return tuple(range(0, 360, step))


def item_rotations(nest_obj: Nest) -> dict[str, tuple]:
    """Giros permitidos por pieza (veta, acabado direccional…)."""
    out = {}
    for it in nest_obj.items:
        g = it.get("giro", "libre")
        if g == "180":
            out[it["part_id"]] = (0, 180) if nest_obj.allow_rotation else (0,)
        elif g == "fijo":
            out[it["part_id"]] = (0,)
    return out


def allowed_rotation(nest_obj: Nest, key: str, rot: float) -> bool:
    if not nest_obj.allow_rotation:
        return rot == 0
    g = next((it.get("giro", "libre") for it in nest_obj.items if it["part_id"] == key), "libre")
    return {"fijo": rot == 0, "180": rot in (0, 180)}.get(g, True)


def _to_sheets(res) -> list:
    sheets = []
    for src, placements in zip(res.sheet_sources, res.sheets):
        key = src["key"]
        source = {"kind": "retal", "remnant_id": int(key.split(":")[1])} if key.startswith("retal:") else {"kind": "formato"}
        source.update(w=src["w"], h=src["h"])
        sheets.append({"source": source, "placements": [p.to_dict() for p in placements]})
    return sheets


def _items(nest_obj: Nest, parts: dict) -> list[NestItem]:
    return [NestItem(it["part_id"], parts[it["part_id"]].shape, int(it["quantity"]))
            for it in nest_obj.items if it["part_id"] in parts]


def _stock(db: Session, nest_obj: Nest) -> list[SheetStock]:
    if not nest_obj.use_remnants:
        return []
    return [SheetStock(f"retal:{r.id}", r.width_mm, r.height_mm, 1) for r in available_remnants(db, nest_obj)]


def available_remnants(db: Session, nest_obj: Nest) -> list[Remnant]:
    q = select(Remnant).where(Remnant.material_id == nest_obj.material_id, Remnant.status == "disponible")
    rems = [r for r in db.scalars(q) if abs(r.thickness_mm - nest_obj.thickness_mm) < 0.01]
    return sorted(rems, key=lambda r: r.width_mm * r.height_mm)   # primero los más pequeños


def run_auto(db: Session, nest_obj: Nest) -> dict:
    """Anidado automático: usa primero los retales del almacén (si se permite) y después el formato."""
    parts = load_parts(db, nest_obj)
    res = nest(_items(nest_obj, parts), nest_obj.sheet_w_mm, nest_obj.sheet_h_mm, nest_obj.gap_mm, nest_obj.edge_mm,
               rotations=rotations(nest_obj), stock=_stock(db, nest_obj), item_rotations=item_rotations(nest_obj))
    nest_obj.sheets = _to_sheets(res)
    opts = dict(nest_obj.options or {})
    opts.pop("alternatives", None)
    nest_obj.options = opts
    return evaluate(db, nest_obj, nest_obj.sheets, parts)


def optimize_nest(db: Session, nest_obj: Nest, priority: str, budget: float, formats: list[tuple[float, float]],
                  rotation_step: int) -> dict:
    """Busca la mejor propuesta probando estrategias y formatos; guarda las 3 mejores para poder elegir."""
    opts = dict(nest_obj.options or {})
    opts.update(priority=priority if priority in PRIORITIES else "material",
                rotation_step=rotation_step if rotation_step in ROTATION_STEPS else 90, budget=budget)
    nest_obj.options = opts
    parts = load_parts(db, nest_obj)
    cfg = settings(db)
    alts = optimize(_items(nest_obj, parts), formats, nest_obj.gap_mm, nest_obj.edge_mm, rotations(nest_obj),
                    stock_factory=lambda: _stock(db, nest_obj), min_side=cfg.remnant_min_mm,
                    priority=opts["priority"], time_budget=budget, item_rotations=item_rotations(nest_obj))
    saved = []
    for a in alts[:3]:
        sc, lb = a["score"], a["label"]
        saved.append({"sheets": _to_sheets(a["result"]), "sheet_count": sc["sheets"], "unplaced": sc["unplaced"],
                      "consumed_m2": round(sc["consumed_mm2"] / 1e6, 3), "utilization": sc["utilization"],
                      "offcut_m2": round(sc["biggest_offcut_mm2"] / 1e6, 3), "format": lb["format"],
                      "last_format": lb.get("ultima_en"),
                      "strategy": f"{ORDERS_LABEL.get(lb['order'], lb['order'])}, llenado hacia "
                                  f"{'la izquierda' if lb['gravity'] == 'x' else 'abajo'}"})
    opts["alternatives"] = saved
    opts["tried"] = len(alts)
    nest_obj.options = opts
    if saved:
        return apply_alternative(db, nest_obj, 0, parts)
    return evaluate(db, nest_obj, nest_obj.sheets, parts)


def apply_alternative(db: Session, nest_obj: Nest, idx: int, parts=None) -> dict:
    alts = (nest_obj.options or {}).get("alternatives") or []
    if not 0 <= idx < len(alts):
        raise ValueError("Propuesta no válida.")
    alt = alts[idx]
    nest_obj.sheet_w_mm, nest_obj.sheet_h_mm = alt["format"]
    nest_obj.sheets = alt["sheets"]
    opts = dict(nest_obj.options)
    opts["chosen"] = idx
    nest_obj.options = opts
    return evaluate(db, nest_obj, nest_obj.sheets, parts)


def _placements(sh: dict) -> list[Placement]:
    return [Placement(p["key"], p["x"], p["y"], p["rotation"]) for p in sh["placements"]]


def op_compact(db: Session, nest_obj: Nest, sheets: list, idx: int, selected: list[int] | None = None) -> list:
    """Arrima las piezas de una chapa (o solo las seleccionadas) hacia abajo/izquierda: efecto «imán»."""
    parts = load_parts(db, nest_obj)
    sh = sheets[idx]
    pls = _placements(sh)
    locked = {i for i, p in enumerate(sh["placements"]) if p.get("locked")}
    if selected is not None:
        locked |= set(range(len(pls))) - set(selected)
    new = compact({k: v.shape for k, v in parts.items()}, pls, sh["source"]["w"], sh["source"]["h"],
                  nest_obj.gap_mm, nest_obj.edge_mm, locked=locked)
    for p, n in zip(sh["placements"], new):
        p["x"], p["y"] = round(n.x, 3), round(n.y, 3)
    return sheets


def _counts(sheets: list) -> dict[str, int]:
    c: dict[str, int] = {}
    for sh in sheets:
        for p in sh["placements"]:
            c[p["key"]] = c.get(p["key"], 0) + 1
    return c


def op_place_pending(db: Session, nest_obj: Nest, sheets: list) -> list:
    """Coloca las piezas pendientes en los huecos de las chapas actuales y, si no caben, en chapas nuevas."""
    parts = load_parts(db, nest_obj)
    placed = _counts(sheets)
    items = []
    for it in nest_obj.items:
        k = it["part_id"]
        if k in parts:
            items.append(NestItem(k, parts[k].shape, max(0, int(it["quantity"]) - placed.get(k, 0))))
    initial = [{"w": sh["source"]["w"], "h": sh["source"]["h"], "placements": _placements(sh)} for sh in sheets]
    res = nest(items, nest_obj.sheet_w_mm, nest_obj.sheet_h_mm, nest_obj.gap_mm, nest_obj.edge_mm,
               rotations=rotations(nest_obj), initial=initial, item_rotations=item_rotations(nest_obj))
    out = []
    for i, pls in enumerate(res.sheets):
        if i < len(sheets):
            old = sheets[i]["placements"]
            extra = [p.to_dict() for p in pls[len(old):]]
            out.append({"source": sheets[i]["source"], "placements": old + extra})
        else:
            src = res.sheet_sources[i]
            out.append({"source": {"kind": "formato", "w": src["w"], "h": src["h"]},
                        "placements": [p.to_dict() for p in pls]})
    return out


def op_reflow(db: Session, nest_obj: Nest, sheets: list, idx: int) -> list:
    """Vuelve a anidar una chapa respetando las piezas bloqueadas; lo que no quepa queda pendiente."""
    parts = load_parts(db, nest_obj)
    sh = sheets[idx]
    keep = [p for p in sh["placements"] if p.get("locked")]
    free: dict[str, int] = {}
    for p in sh["placements"]:
        if not p.get("locked"):
            free[p["key"]] = free.get(p["key"], 0) + 1
    items = [NestItem(k, parts[k].shape, q) for k, q in free.items() if k in parts]
    for k in {p["key"] for p in keep} - set(free):
        items.append(NestItem(k, parts[k].shape, 0))
    initial = [{"w": sh["source"]["w"], "h": sh["source"]["h"],
                "placements": [Placement(p["key"], p["x"], p["y"], p["rotation"]) for p in keep]}]
    res = nest(items, nest_obj.sheet_w_mm, nest_obj.sheet_h_mm, nest_obj.gap_mm, nest_obj.edge_mm,
               rotations=rotations(nest_obj), initial=initial, allow_new=False,
               item_rotations=item_rotations(nest_obj))
    new = res.sheets[0][len(keep):] if res.sheets else []
    sheets[idx] = {"source": sh["source"], "placements": keep + [p.to_dict() for p in new]}
    return sheets


def cut_plan(db: Session, nest_obj: Nest, sheets: list, parts: dict) -> list[dict]:
    """Orden de corte orientativo y tiempo por chapa con la máquina y el gas por defecto.

    Orden: por franjas de 300 mm en X recorridas en zigzag (menos recorrido en vacío); dentro de cada pieza,
    primero los contornos interiores y después el exterior (así la pieza no se mueve antes de terminarla).
    """
    from ..quotes.service import default_machine_and_gas
    machine, gas = default_machine_and_gas(db, nest_obj.material.family, nest_obj.thickness_mm)
    param = machine.interpolated_param(nest_obj.material.family, nest_obj.thickness_mm, gas) if machine and gas else None
    out = []
    for sh in sheets:
        pts = []
        for i, p in enumerate(sh["placements"]):
            info = parts.get(p["key"])
            if info is None:
                continue
            c = placed_shape(info.shape, Placement(p["key"], p["x"], p["y"], p["rotation"])).bounds
            pts.append((i, (c[0] + c[2]) / 2, (c[1] + c[3]) / 2))
        band = 300.0
        pts.sort(key=lambda t: (int(t[1] // band), t[2] if int(t[1] // band) % 2 == 0 else -t[2]))
        order = [t[0] for t in pts]
        rapid = 0.0
        prev = (0.0, 0.0)
        for _, x, y in pts:
            rapid += math.hypot(x - prev[0], y - prev[1])
            prev = (x, y)
        cut = pierces = 0.0
        t = 0.0
        for i in order:
            m = parts[sh["placements"][i]["key"]].part.metrics
            cut += m.get("cut_length_mm", 0)
            pierces += m.get("pierce_count", 0)
            if param and machine:
                v = param.cut_speed_m_min * 1000 / 60 * machine.speed_efficiency
                for length in m.get("contour_lengths_mm", []):
                    f = machine.small_contour_factor if length < machine.small_contour_mm else 1.0
                    t += length / (v * f)
                t += m.get("pierce_count", 0) * param.pierce_time_s
                t += m.get("rapid_length_mm", 0) / (machine.rapid_speed_m_min * 1000 / 60)
                t += m.get("contour_count", 0) * machine.head_overhead_s
        if machine:
            t += rapid / (machine.rapid_speed_m_min * 1000 / 60) + machine.sheet_change_min * 60
        out.append({"order": order, "cut_m": round(cut / 1000, 2), "pierces": int(pierces),
                    "rapid_m": round(rapid / 1000, 2), "time_s": round(t) if param else None})
    return out


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
        polys = [placed_shape(shapes[p["key"]], Placement(p["key"], p["x"], p["y"], p["rotation"])) for p in pls]
        area = sum(parts[p["key"]].part.metrics.get("net_area_mm2", shapes[p["key"]].area) for p in pls)
        off = offcut(w, h, polys, nest_obj.gap_mm, cfg.remnant_min_mm)
        for p in pls:
            placed_count[p["key"]] = placed_count.get(p["key"], 0) + 1
        parts_area += area
        sheet_area += w * h
        consumed += w * h - (off["w"] * off["h"] if off else 0)
        total_conf += len(bad)
        out_sheets.append({"w": w, "h": h, "source": sh["source"], "conflicts": bad,
                           "reasons": {c["index"]: c["reason"] for c in conflicts},
                           "utilization": round(100 * area / (w * h), 1) if w * h else 0, "offcut": off,
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
    plans = cut_plan(db, nest_obj, sheets, parts)
    for o, pl in zip(out_sheets, plans):
        o["plan"] = pl
    times = [pl["time_s"] for pl in plans if pl["time_s"] is not None]
    return {"sheets": out_sheets, "pending": pending, "utilization": nest_obj.utilization_pct,
            "material_factor": nest_obj.material_factor, "conflicts": total_conf,
            "sheet_count": len(sheets), "consumed_m2": round(consumed / 1e6, 3),
            "time_s": sum(times) if times else None, "cut_m": round(sum(pl["cut_m"] for pl in plans), 2),
            "pierces": sum(pl["pierces"] for pl in plans)}


def clean_sheets(raw, nest_obj: Nest, valid_keys: set[str], remnant_ids: set[int],
                 formats: set[tuple[float, float]] | None = None) -> list:
    """Valida la disposición enviada por el editor (no se confía en el navegador)."""
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("Disposición no válida.")
    fmts = set(formats or set()) | {(nest_obj.sheet_w_mm, nest_obj.sheet_h_mm)}
    out = []
    for sh in raw:
        src = sh.get("source") or {}
        if src.get("kind") == "retal":
            rid = int(src.get("remnant_id", 0))
            if rid not in remnant_ids:
                raise ValueError("Retal no válido.")
            source = {"kind": "retal", "remnant_id": rid, "w": float(src["w"]), "h": float(src["h"])}
        else:
            wh = (float(src.get("w", 0)), float(src.get("h", 0)))
            if wh not in fmts:
                wh = (nest_obj.sheet_w_mm, nest_obj.sheet_h_mm)
            source = {"kind": "formato", "w": wh[0], "h": wh[1]}
        pls = []
        for p in (sh.get("placements") or [])[:5000]:
            key = str(p.get("key"))
            rot = round(float(p.get("rotation", 0)) % 360, 1)
            x, y = float(p.get("x")), float(p.get("y"))
            if key not in valid_keys or not (math.isfinite(x) and math.isfinite(y) and math.isfinite(rot)) \
                    or abs(x) > 1e6 or abs(y) > 1e6:
                raise ValueError("Pieza o posición no válida.")
            if not allowed_rotation(nest_obj, key, rot):
                raise ValueError("Esa pieza no se puede girar a ese ángulo (veta o giro desactivado).")
            item = {"key": key, "x": round(x, 3), "y": round(y, 3), "rotation": int(rot) if rot.is_integer() else rot}
            if p.get("locked"):
                item["locked"] = True
            pls.append(item)
        out.append({"source": source, "placements": pls})
    return out


def display_paths(parts: dict[str, PartInfo]) -> dict:
    """Siluetas para el editor en mm (eje Y hacia arriba), sin girar; el navegador las gira y normaliza igual que
    el servidor (giro respecto al origen y esquina inferior izquierda a 0,0)."""
    out = {}
    for key, info in parts.items():
        simple = info.shape.simplify(0.1, preserve_topology=True)
        polys = getattr(simple, "geoms", [simple])
        rings = [[[round(x, 2), round(y, 2)] for x, y in ring.coords]
                 for poly in polys for ring in [poly.exterior, *poly.interiors]]
        b = info.shape.bounds
        out[key] = {"name": info.part.filename, "rings": rings, "detailed": info.detailed,
                    "w": round(b[2] - b[0], 1), "h": round(b[3] - b[1], 1),
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
            r = float(p["rotation"])
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
