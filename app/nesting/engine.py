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
import time
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
    rotation: float         # grados (0–359)

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


def _slide(sheet: _Sheet, buf0: Polygon, x: float, y: float, gravity: str = "x") -> tuple[float, float]:
    """Desliza hacia abajo y a la izquierda (o al revés con gravity «y») mientras no choque (búsqueda binaria)."""
    ok = lambda xx, yy: sheet.fits(_move(buf0, xx, yy))  # noqa: E731

    def down(x, y):
        lo, hi = min(sheet.edge, y), y
        while hi - lo > 0.5:
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if ok(x, mid) else (mid, hi)
        return hi if ok(x, hi) else y

    def left(x, y):
        lo, hi = min(sheet.edge, x), x
        while hi - lo > 0.5:
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if ok(mid, y) else (mid, hi)
        return hi if ok(hi, y) else x

    for _ in range(2):
        if gravity == "x":
            y = down(x, y)
            x = left(x, y)
        else:
            x = left(x, y)
            y = down(x, y)
    return x, y


@dataclass
class SheetStock:
    """Chapa disponible: formato nuevo (ilimitado) o retal concreto (cantidad 1)."""
    key: str
    w: float
    h: float
    limit: int | None = None          # None = sin límite (chapa nueva)
    outline: Polygon | None = None    # forma del retal si no es rectangular


class NestTimeout(Exception):
    """Se agotó el tiempo de búsqueda de una estrategia."""


ORDERS = {
    "area": lambda s: s.area,
    "envolvente": lambda s: (s.bounds[2] - s.bounds[0]) * (s.bounds[3] - s.bounds[1]),
    "lado_largo": lambda s: max(s.bounds[2] - s.bounds[0], s.bounds[3] - s.bounds[1]),
    "perimetro": lambda s: s.length,
    "ancho": lambda s: min(s.bounds[2] - s.bounds[0], s.bounds[3] - s.bounds[1]),
}


def safety_radii(gap: float, tol: float = 0.05) -> tuple[float, float]:
    """Radios de buffer para guardar y probar. El buffer poligonal (quad_segs=4) queda ~2 % por dentro del círculo
    y la silueta se simplifica `tol`: se amplía para que la disposición cumpla la separación exacta."""
    r_store = (gap / 2) / math.cos(math.pi / 16) + tol + 0.02
    return r_store, r_store - 0.02


class _Prep:
    """Siluetas simplificadas, giradas y con buffer, calculadas una sola vez por (pieza, giro)."""

    def __init__(self, shapes: dict[str, Polygon], gap: float, tol: float = 0.05):
        self.shapes, self.tol = shapes, tol
        self.r_store, self.r_test = safety_radii(gap, tol)
        self.simple = {k: s.simplify(tol, preserve_topology=True) for k, s in shapes.items()}
        self.cache: dict = {}

    def get(self, key: str, r: float):
        k = (key, r)
        if k not in self.cache:
            orig = self.shapes[key]
            rot_orig = affinity.rotate(orig, r, origin=(0, 0)) if r else orig
            minx, miny, _, _ = rot_orig.bounds   # misma referencia que normalized() con la silueta original
            simple = self.simple[key]
            rot = affinity.rotate(simple, r, origin=(0, 0)) if r else simple
            shape0 = affinity.translate(rot, -minx, -miny)
            self.cache[k] = (shape0, shape0.buffer(self.r_store, quad_segs=4), shape0.buffer(self.r_test, quad_segs=4))
        return self.cache[k]


def _best_spot(sheet: _Sheet, prep: _Prep, key: str, rotations, gravity: str, slide: bool):
    """Mejor posición de una pieza en una chapa: la más a la izquierda (gravity x) o la más abajo (gravity y)."""
    best = None
    order = (lambda c: (c[0], c[1])) if gravity == "x" else (lambda c: (c[1], c[0]))
    cands = sorted(sheet.candidates, key=order)
    for r in rotations:
        _, _, buf0 = prep.get(key, r)
        for c in cands:
            if best and order(c)[0] > order(best)[0] + 1e-6:
                break
            if sheet.fits(_move(buf0, c[0], c[1])):
                x, y = _slide(sheet, buf0, c[0], c[1], gravity) if slide else c
                if best is None or (round(order((x, y))[0], 1), round(order((x, y))[1], 1)) < \
                        (round(order(best)[0], 1), round(order(best)[1], 1)):
                    best = (x, y, r)
                break
    return best


def nest(items: list[NestItem], sheet_w: float, sheet_h: float, gap: float, edge: float,
         rotations: tuple = (0, 90, 180, 270), max_sheets: int = 50, slide: bool = True,
         stock: list[SheetStock] | None = None, order: str = "area", gravity: str = "x",
         initial: list[dict] | None = None, allow_new: bool = True,
         item_rotations: dict[str, tuple] | None = None, deadline: float | None = None,
         stop_on_unplaced: bool = False) -> NestResult:
    """Anida las piezas.

    - `stock`: retales a usar antes que el formato (sheet_w × sheet_h).
    - `order`: criterio de orden de colocación (ver ORDERS); `gravity`: «x» llena hacia la izquierda (deja
      el retal a la derecha), «y» llena hacia abajo (deja el retal arriba).
    - `initial`: chapas ya existentes con piezas fijas [{"w","h","key","placements":[Placement]}]; las piezas
      nuevas se encajan en sus huecos. `allow_new=False` impide abrir chapas nuevas.
    - `item_rotations`: giros permitidos por pieza (p. ej. piezas con veta).
    """
    shapes = {it.key: it.shape for it in items}
    res = NestResult(sheet_w, sheet_h, gap, edge, part_areas={k: s.area for k, s in shapes.items()})
    keyf = ORDERS.get(order, ORDERS["area"])
    queue = []
    for it in sorted(items, key=lambda i: keyf(i.shape), reverse=True):
        queue += [it] * it.quantity
    prep = _Prep(shapes, gap)
    spacing = prep.r_store + prep.r_test + 0.01
    remaining = list(stock or [])
    if allow_new:
        remaining.append(SheetStock("formato", sheet_w, sheet_h, None))
    sheets: list[_Sheet] = []
    sheet_src: list[SheetStock] = []
    for ini in initial or []:
        sh = _Sheet(ini["w"], ini["h"], edge, gap, None, prep.r_test, spacing)
        for p in ini["placements"]:
            shape0, bstore, _ = prep.get(p.key, p.rotation)
            sh.add(_move(shape0, p.x, p.y), _move(bstore, p.x, p.y), p)
        sheets.append(sh)
        sheet_src.append(SheetStock(ini.get("key", "formato"), ini["w"], ini["h"], 0))

    def open_sheet() -> _Sheet | None:
        for st in remaining:
            if st.limit is None or st.limit > 0:
                if st.limit is not None:
                    st.limit -= 1
                sh = _Sheet(st.w, st.h, edge, gap, st.outline, prep.r_test, spacing)
                sheets.append(sh)
                sheet_src.append(st)
                return sh
        return None

    for it in queue:
        if deadline is not None and time.time() > deadline:
            raise NestTimeout()
        rots = (item_rotations or {}).get(it.key, rotations)
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
            best = _best_spot(sheet, prep, it.key, rots, gravity, slide)
            if best:
                x, y, r = best
                shape0, bstore, _ = prep.get(it.key, r)
                sheet.add(_move(shape0, x, y), _move(bstore, x, y), Placement(it.key, x, y, r))
                placed = True
                break
            if not sheet.placements:
                # no cabe ni vacía: se descarta esa chapa; si era un retal se reserva para no reintentar
                i = sheets.index(sheet)
                sheets.pop(i)
                src = sheet_src.pop(i)
                if src.limit is not None:
                    src.limit = 0
                if tried_new > 3 or src.limit is None:
                    break
        if not placed:
            res.unplaced[it.key] = res.unplaced.get(it.key, 0) + 1
            if stop_on_unplaced:
                break
    keep = [(s, src) for s, src in zip(sheets, sheet_src) if s.placements or initial]
    res.sheets = [s.placements for s, _ in keep]
    res.sheet_sources = [{"key": src.key, "w": src.w, "h": src.h} for _, src in keep]
    return res


def offcut(sheet_w: float, sheet_h: float, polys: list, gap: float, min_side: float) -> dict | None:
    """Retal aprovechable: la mayor franja libre a la derecha o arriba de las piezas (rectangular, reutilizable)."""
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


def score(res: NestResult, shapes: dict[str, Polygon], min_side: float, priority: str = "material") -> dict:
    """Valora un resultado: chapa consumida (descontando el retal reutilizable), nº de chapas, retal mayor."""
    consumed = 0.0
    biggest = 0.0
    for i, pls in enumerate(res.sheets):
        src = res.sheet_sources[i]
        polys = [placed_shape(shapes[p.key], p) for p in pls]
        off = offcut(src["w"], src["h"], polys, res.gap, min_side)
        off_area = off["w"] * off["h"] if off else 0.0
        consumed += src["w"] * src["h"] - off_area
        biggest = max(biggest, off_area)
    unplaced = sum(res.unplaced.values())
    n = len(res.sheets)
    if priority == "chapas":
        key = (unplaced, n, round(consumed / 1e4), -biggest)
    elif priority == "retal":
        key = (unplaced, n, -round(biggest / 1e4), consumed)
    else:   # material
        key = (unplaced, round(consumed / 1e4), n, -biggest)
    return {"key": key, "consumed_mm2": consumed, "sheets": n, "unplaced": unplaced, "biggest_offcut_mm2": biggest,
            "utilization": round(res.total_utilization, 1)}


def compact(shapes: dict[str, Polygon], placements: list[Placement], sheet_w: float, sheet_h: float,
            gap: float, edge: float, gravity: str = "x", locked: set[int] | None = None) -> list[Placement]:
    """Arrima las piezas hacia abajo/izquierda sin cambiar su giro. Todas las piezas son obstáculo desde el
    principio: cada una se desliza respecto a las demás en su posición actual (nunca invade el sitio de otra)."""
    prep = _Prep(shapes, gap)
    sheet = _Sheet(sheet_w, sheet_h, edge, gap, None, prep.r_test, prep.r_store + prep.r_test + 0.01)
    locked = locked or set()
    cur = list(placements)
    sheet.buffered = [_move(prep.get(p.key, p.rotation)[1], p.x, p.y) for p in cur]
    order = sorted((i for i in range(len(cur)) if i not in locked),
                   key=lambda i: cur[i].x + cur[i].y * (0.2 if gravity == "x" else 5))
    empty = Polygon()
    for i in order:
        p = cur[i]
        _, bstore, buf0 = prep.get(p.key, p.rotation)
        saved = sheet.buffered[i]
        sheet.buffered[i] = empty
        sheet.tree = STRtree(sheet.buffered)
        x, y = p.x, p.y
        if sheet.fits(_move(buf0, x, y)):
            x, y = _slide(sheet, buf0, x, y, gravity)
            cur[i] = Placement(p.key, x, y, p.rotation)
            sheet.buffered[i] = _move(bstore, x, y)
        else:   # ya estaba en conflicto o demasiado justa: no se mueve
            sheet.buffered[i] = saved
    return cur


def _run_strategy(args):
    """Ejecuta una estrategia (función de nivel de módulo para poder usarla en otro proceso)."""
    items, sheet_w, sheet_h, gap, edge, kw, min_side, priority, label = args
    try:
        res = nest(items, sheet_w, sheet_h, gap, edge, **kw)
    except NestTimeout:
        return None
    sc = score(res, {it.key: it.shape for it in items}, min_side, priority)
    return label, kw, res, sc


def _shrink_last_sheet(items, res: NestResult, formats, gap, edge, kw, min_side, priority, item_rotations):
    if not res.sheets or res.unplaced or res.sheet_sources[-1]["key"] != "formato":
        return None
    last = res.sheets[-1]
    src = res.sheet_sources[-1]
    shapes = {it.key: it.shape for it in items}
    counts: dict[str, int] = {}
    for p in last:
        counts[p.key] = counts.get(p.key, 0) + 1
    sub = [NestItem(k, shapes[k], q) for k, q in counts.items()]
    base = NestResult(src["w"], src["h"], gap, edge, [last], {}, res.part_areas, [src])
    best_res, best_key = None, score(base, shapes, min_side, "material")["consumed_mm2"]
    for fw, fh in formats:
        usable = (fw - 2 * edge) * (fh - 2 * edge)
        if fw * fh >= src["w"] * src["h"] or sum(shapes[p.key].area for p in last) > 0.85 * usable:
            continue
        try:
            r = nest(sub, fw, fh, gap, edge, rotations=kw["rotations"], order=kw["order"], gravity=kw["gravity"],
                     max_sheets=1, item_rotations=item_rotations, deadline=time.time() + 10, stop_on_unplaced=True)
        except NestTimeout:
            continue
        if r.unplaced or len(r.sheets) != 1:
            continue
        c = score(r, shapes, min_side, "material")["consumed_mm2"]
        if c < best_key - 1:
            best_key, best_res = c, r
    if best_res is None:
        return None
    return NestResult(res.sheet_w, res.sheet_h, gap, edge, res.sheets[:-1] + best_res.sheets, {}, res.part_areas,
                      res.sheet_sources[:-1] + best_res.sheet_sources)


def optimize(items: list[NestItem], formats: list[tuple[float, float]], gap: float, edge: float,
             rotations: tuple, stock_factory=None, min_side: float = 300, priority: str = "material",
             time_budget: float = 20.0, workers: int | None = None, item_rotations=None) -> list[dict]:
    """Prueba varias estrategias (orden de piezas, dirección de llenado, formato de chapa) y devuelve las
    alternativas ordenadas de mejor a peor. El anidado óptimo es un problema NP-difícil: se busca la mejor
    propuesta dentro del tiempo disponible."""
    import os
    from concurrent.futures import ProcessPoolExecutor, as_completed

    deadline = time.time() + time_budget
    tasks = []
    for fw, fh in formats:
        for order in ("area", "envolvente", "lado_largo", "perimetro", "ancho"):
            for gravity in ("x", "y"):
                kw = dict(rotations=rotations, order=order, gravity=gravity, item_rotations=item_rotations,
                          stock=stock_factory() if stock_factory else None, deadline=deadline)
                label = {"format": [fw, fh], "order": order, "gravity": gravity}
                tasks.append((items, fw, fh, gap, edge, kw, min_side, priority, label))
    # primero las estrategias habituales en el primer formato
    tasks.sort(key=lambda t: (t[8]["order"] != "area", t[8]["gravity"] != "x"))
    results = []
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    try:
        if workers <= 1:
            raise RuntimeError("secuencial")
        import multiprocessing
        # «spawn» igual en Windows y Linux: seguro con el servidor multihilo
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as ex:
            futs = [ex.submit(_run_strategy, t) for t in tasks]
            try:
                for f in as_completed(futs, timeout=max(1.0, time_budget) + 5):
                    results.append(f.result())
            except TimeoutError:
                pass
            for f in futs:
                f.cancel()
    except Exception:   # sin multiproceso disponible: secuencial dentro del tiempo
        results = []
        for i, t in enumerate(tasks):
            if i and time.time() > deadline:
                break
            if i == 0:   # la estrategia básica siempre se completa
                t = (*t[:5], {**t[5], "deadline": None}, *t[6:])
            results.append(_run_strategy(t))
    results = [r for r in results if r is not None]
    if not results:   # ninguna terminó a tiempo: estrategia básica sin límite
        t = tasks[0]
        results = [_run_strategy((*t[:5], {**t[5], "deadline": None}, *t[6:]))]
    results.sort(key=lambda r: r[3]["key"])
    # mejora: la última chapa (la menos llena) en un formato más pequeño si consume menos material
    improved = []
    for label, kw, res, sc in results[:3]:
        if time.time() > deadline + 10:
            break
        better = _shrink_last_sheet(items, res, formats, gap, edge, kw, min_side, priority, item_rotations)
        if better is not None:
            improved.append(({**label, "ultima_en":
                              [better.sheet_sources[-1]["w"], better.sheet_sources[-1]["h"]]}, kw, better,
                             score(better, {it.key: it.shape for it in items}, min_side, priority)))
    results = sorted(results + improved, key=lambda r: r[3]["key"])
    out, seen = [], set()
    for label, kw, res, sc in results:
        sig = (sc["sheets"], round(sc["consumed_mm2"] / 1e4), round(sc["biggest_offcut_mm2"] / 1e4), sc["unplaced"])
        if sig in seen:
            continue
        seen.add(sig)
        out.append({"label": label, "result": res, "score": sc})
    return out


def check_layout(items: dict[str, Polygon], placements: list[dict], sheet_w: float, sheet_h: float,
                 gap: float, edge: float) -> list[dict]:
    """Valida una disposición editada a mano. Devuelve conflictos [{index, reason}]."""
    area = box(edge, edge, sheet_w - edge, sheet_h - edge)
    polys = []
    for p in placements:
        shape = items.get(p["key"])
        polys.append(placed_shape(shape, Placement(p["key"], p["x"], p["y"], p["rotation"])) if shape else None)
    conflicts = []
    valid = [i for i, a in enumerate(polys) if a is not None]
    tree = STRtree([polys[i] for i in valid]) if valid else None
    zone = area.buffer(0.1)
    for i, a in enumerate(polys):
        if a is None:
            conflicts.append({"index": i, "reason": "pieza desconocida"})
            continue
        if not zone.contains(a):
            conflicts.append({"index": i, "reason": "fuera de chapa o dentro del margen de borde"})
        near = tree.query(a, predicate="dwithin", distance=max(gap - 0.05, 0)) if gap > 0.05 else \
            tree.query(a, predicate="intersects")
        for j in sorted(valid[int(k)] for k in near):
            if j <= i:
                continue
            b = polys[j]
            if b is not None and a.distance(b) < gap - 0.05:
                d = a.distance(b)
                conflicts.append({"index": i, "other": j,
                                  "reason": "solape" if d == 0 else f"separación {d:.1f} mm < {gap:g} mm"})
                conflicts.append({"index": j, "other": i,
                                  "reason": "solape" if d == 0 else f"separación {d:.1f} mm < {gap:g} mm"})
    return conflicts
