"""Anidado automático por forma real (true-shape) con separación mínima entre piezas.

Algoritmo «bottom-left» con candidatos y deslizamiento:
  - Cada pieza se representa con su silueta real (exterior con agujeros) ampliada media separación (buffer g/2),
    de modo que dos piezas válidas quedan siempre a ≥ g. Los agujeros siguen libres: una pieza pequeña puede ir
    dentro del hueco de una grande si cabe con su separación.
  - Se colocan de mayor a menor área. Para cada giro permitido se prueban posiciones candidatas (esquinas de
    lo ya colocado, interiores de huecos y origen), se toma la primera válida y se desliza hacia abajo y a la
    izquierda. Se elige la colocación más a la izquierda/abajo.
  - Si una pieza no cabe en la chapa actual se abre otra chapa.

Es un anidado para presupuestar y preparar trabajo; el CAM de la máquina hace el anidado definitivo.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

def part_polygon(contours: list) -> Polygon | None:
    """Silueta de una pieza a partir de sus contornos [(puntos, cerrado, tipo)]. Une exteriores, resta interiores."""
    outers, inners = [], []
    for pts, closed, kind in contours:
        if not closed or len(pts) < 3:
            continue
        poly = Polygon(pts).buffer(0)
        if poly.is_empty:
            continue
        (outers if kind == "exterior" else inners).append(poly)
    if not outers:
        return None
    shape = unary_union(outers)
    if inners:
        shape = shape.difference(unary_union(inners))
    if shape.geom_type == "MultiPolygon":  # varias piezas en un plano: se tratan como un bloque
        shape = shape.convex_hull if len(shape.geoms) > 1 else shape.geoms[0]
    return shape


@dataclass
class NestItem:
    key: str                # identificador de la pieza (p. ej. id de Part)
    shape: Polygon          # silueta en mm, en su sistema de coordenadas
    quantity: int


@dataclass
class Placement:
    key: str
    x: float                # desplazamiento aplicado tras girar y normalizar a (0,0)
    y: float
    rotation: int           # grados

    def to_dict(self):
        return {"key": self.key, "x": round(self.x, 3), "y": round(self.y, 3), "rotation": self.rotation}


@dataclass
class NestResult:
    sheet_w: float
    sheet_h: float
    gap: float
    edge: float
    sheets: list[list[Placement]] = field(default_factory=list)
    unplaced: dict[str, int] = field(default_factory=dict)
    part_areas: dict[str, float] = field(default_factory=dict)
    sheet_sources: list[dict] = field(default_factory=list)

    def sheet_area(self, i: int) -> float:
        src = self.sheet_sources[i] if i < len(self.sheet_sources) else {"w": self.sheet_w, "h": self.sheet_h}
        return src["w"] * src["h"]

    def sheet_utilization(self, i: int) -> float:
        area = sum(self.part_areas[p.key] for p in self.sheets[i])
        return 100 * area / self.sheet_area(i)

    def used_length(self, i: int, shapes: dict[str, Polygon]) -> float:
        """Longitud de chapa ocupada (en X) en la chapa i, para valorar el retal sobrante."""
        xs = [placed_shape(shapes[p.key], p).bounds[2] for p in self.sheets[i]]
        return min(self.sheet_w, (max(xs) if xs else 0) + self.edge)

    @property
    def total_utilization(self) -> float:
        if not self.sheets:
            return 0.0
        area = sum(self.part_areas[p.key] for s in self.sheets for p in s)
        return 100 * area / sum(self.sheet_area(i) for i in range(len(self.sheets)))

    def to_dict(self):
        return {"sheet_w": self.sheet_w, "sheet_h": self.sheet_h, "gap": self.gap, "edge": self.edge,
                "sheets": [[p.to_dict() for p in s] for s in self.sheets], "unplaced": self.unplaced,
                "sources": self.sheet_sources}


def normalized(shape: Polygon, rotation: int) -> Polygon:
    """Gira la silueta y la lleva a que su esquina inferior izquierda quede en (0,0)."""
    g = affinity.rotate(shape, rotation, origin=(0, 0), use_radians=False) if rotation else shape
    minx, miny, _, _ = g.bounds
    return affinity.translate(g, -minx, -miny)


def placed_shape(shape: Polygon, p: Placement) -> Polygon:
    return affinity.translate(normalized(shape, p.rotation), p.x, p.y)


class _Sheet:
    def __init__(self, w: float, h: float, edge: float, gap: float, outline: Polygon | None = None,
                 r_test: float | None = None, spacing: float | None = None):
        self.w, self.h, self.edge, self.gap = w, h, edge, gap
        r = gap / 2 if r_test is None else r_test
        self.spacing = gap if spacing is None else spacing   # distancia efectiva entre siluetas
        self.bounds = (edge - r, edge - r, w - edge + r, h - edge + r)
        # retal con forma irregular: zona útil = forma reducida por el margen de borde
        self.outline = outline.buffer(-(edge - r)) if outline is not None else None
        if self.outline is not None:
            shapely.prepare(self.outline)
        self.buffered: list[Polygon] = []
        self.tree: STRtree | None = None
        self.candidates: set[tuple[float, float]] = {(edge, edge)}
        self.placements: list[Placement] = []

    def fits(self, buf: Polygon) -> bool:
        bx0, by0, bx1, by1 = buf.bounds
        x0, y0, x1, y1 = self.bounds
        if bx0 < x0 - 1e-6 or by0 < y0 - 1e-6 or bx1 > x1 + 1e-6 or by1 > y1 + 1e-6:
            return False
        if self.outline is not None and not self.outline.contains(buf):
            return False
        if self.tree is None:
            return True
        # los almacenados llevan un margen algo mayor que el de prueba: basta con «no se tocan»
        return len(self.tree.query(buf, predicate="intersects")) == 0

    def add(self, poly: Polygon, buf: Polygon, placement: Placement):
        self.buffered.append(buf)
        self.tree = STRtree(self.buffered)
        self.placements.append(placement)
        minx, miny, maxx, maxy = poly.bounds
        g, e = self.spacing, self.edge
        self.candidates |= {(maxx + g, miny), (minx, maxy + g), (maxx + g, e), (e, maxy + g)}
        for interior in getattr(poly, "interiors", []):  # huecos interiores: posibles alojamientos
            ix, iy, _, _ = Polygon(interior).bounds
            self.candidates.add((ix + g, iy + g))


def _move(g, dx: float, dy: float):
    return shapely.transform(g, lambda c: c + np.array([dx, dy]))


def _slide(sheet: _Sheet, buf0: Polygon, x: float, y: float) -> tuple[float, float]:
    """Desliza hacia abajo y a la izquierda mientras no choque (búsqueda binaria)."""
    ok = lambda xx, yy: sheet.fits(_move(buf0, xx, yy))  # noqa: E731
    for _ in range(2):
        lo, hi = sheet.edge, y
        while hi - lo > 0.5:
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if ok(x, mid) else (mid, hi)
        y = hi if ok(x, hi) else y
        lo, hi = sheet.edge, x
        while hi - lo > 0.5:
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if ok(mid, y) else (mid, hi)
        x = hi if ok(hi, y) else x
    return x, y


@dataclass
class SheetStock:
    """Chapa disponible: formato nuevo (ilimitado) o retal concreto (cantidad 1)."""
    key: str
    w: float
    h: float
    limit: int | None = None          # None = sin límite (chapa nueva)
    outline: Polygon | None = None    # forma del retal si no es rectangular


def nest(items: list[NestItem], sheet_w: float, sheet_h: float, gap: float, edge: float,
         rotations: tuple[int, ...] = (0, 90, 180, 270), max_sheets: int = 50, slide: bool = True,
         stock: list[SheetStock] | None = None) -> NestResult:
    """Anida las piezas. `stock` permite usar primero retales y después el formato (sheet_w × sheet_h)."""
    res = NestResult(sheet_w, sheet_h, gap, edge, part_areas={it.key: it.shape.area for it in items})
    queue = []
    for it in sorted(items, key=lambda i: i.shape.area, reverse=True):
        queue += [it] * it.quantity
    # Margen de seguridad: el buffer poligonal (quad_segs=4) queda ~2 % por dentro del círculo y la
    # silueta se simplifica 0,05 mm; se amplía para que la disposición cumpla la separación exacta.
    tol = 0.05
    r_store = (gap / 2) / math.cos(math.pi / 16) + tol + 0.02
    r_test = r_store - 0.02
    prep = {}
    for it in items:
        simple = it.shape.simplify(tol, preserve_topology=True)
        for r in rotations:
            rot_orig = affinity.rotate(it.shape, r, origin=(0, 0)) if r else it.shape
            minx, miny, _, _ = rot_orig.bounds   # misma referencia que normalized() con la silueta original
            rot = affinity.rotate(simple, r, origin=(0, 0)) if r else simple
            shape0 = affinity.translate(rot, -minx, -miny)
            prep[(it.key, r)] = (shape0, shape0.buffer(r_store, quad_segs=4), shape0.buffer(r_test, quad_segs=4))
    remaining = list(stock or [])
    remaining.append(SheetStock("formato", sheet_w, sheet_h, None))
    sheets: list[_Sheet] = []
    sheet_src: list[SheetStock] = []

    def open_sheet() -> _Sheet | None:
        for st in remaining:
            if st.limit is None or st.limit > 0:
                if st.limit is not None:
                    st.limit -= 1
                sh = _Sheet(st.w, st.h, edge, gap, st.outline, r_test, r_store + r_test + 0.01)
                sheets.append(sh)
                sheet_src.append(st)
                return sh
        return None

    for it in queue:
        placed = False
        tried_new = 0
        for sheet in list(sheets) + [None]:
            if sheet is None:
                if len(sheets) >= max_sheets:
                    break
                sheet = open_sheet()
                if sheet is None:
                    break
                tried_new += 1
            best = None
            for r in rotations:
                shape0, _, buf0 = prep[(it.key, r)]
                for cx, cy in sorted(sheet.candidates, key=lambda c: (c[0], c[1])):
                    if best and cx > best[0] + 1e-6:
                        break
                    if sheet.fits(_move(buf0, cx, cy)):
                        x, y = _slide(sheet, buf0, cx, cy) if slide else (cx, cy)
                        if best is None or (round(x, 1), round(y, 1)) < (round(best[0], 1), round(best[1], 1)):
                            best = (x, y, r)
                        break
            if best:
                x, y, r = best
                shape0, bstore, _ = prep[(it.key, r)]
                sheet.add(_move(shape0, x, y), _move(bstore, x, y), Placement(it.key, x, y, r))
                placed = True
                break
            if not sheet.placements:
                # no cabe ni vacía: se descarta esa chapa; si era un retal se prueba la siguiente opción
                i = sheets.index(sheet)
                sheets.pop(i)
                src = sheet_src.pop(i)
                if src.limit is not None:
                    src.limit += 1
                    src.limit = 0  # este retal no sirve para esta pieza; se reserva para no reintentar
                if tried_new > 3 or src.limit is None:
                    break
        if not placed:
            res.unplaced[it.key] = res.unplaced.get(it.key, 0) + 1
    keep = [(s, src) for s, src in zip(sheets, sheet_src) if s.placements]
    res.sheets = [s.placements for s, _ in keep]
    res.sheet_sources = [{"key": src.key, "w": src.w, "h": src.h} for _, src in keep]
    return res


def check_layout(items: dict[str, Polygon], placements: list[dict], sheet_w: float, sheet_h: float,
                 gap: float, edge: float) -> list[dict]:
    """Valida una disposición editada a mano. Devuelve conflictos [{index, reason}]."""
    area = box(edge, edge, sheet_w - edge, sheet_h - edge)
    polys = []
    for p in placements:
        shape = items.get(p["key"])
        polys.append(placed_shape(shape, Placement(p["key"], p["x"], p["y"], int(p["rotation"]))) if shape else None)
    conflicts = []
    for i, a in enumerate(polys):
        if a is None:
            conflicts.append({"index": i, "reason": "pieza desconocida"})
            continue
        if not area.buffer(0.1).contains(a):
            conflicts.append({"index": i, "reason": "fuera de chapa o dentro del margen de borde"})
        for j in range(i + 1, len(polys)):
            b = polys[j]
            if b is not None and a.distance(b) < gap - 0.05:
                d = a.distance(b)
                conflicts.append({"index": i, "other": j,
                                  "reason": "solape" if d == 0 else f"separación {d:.1f} mm < {gap:g} mm"})
                conflicts.append({"index": j, "other": i,
                                  "reason": "solape" if d == 0 else f"separación {d:.1f} mm < {gap:g} mm"})
    return conflicts
