"""Edición de la geometría de una pieza ya analizada (editor de plano).

El navegador envía los contornos editados como polilíneas en mm; aquí se validan, se clasifican
(exterior / interior / abierto), se recalculan las métricas de corte y se comprueba la fabricabilidad
con reglas habituales de corte láser 2D según el espesor y el material.

Seguridad: la entrada se trata como no confiable (límites de tamaño, números finitos y rango de
coordenadas). No se reconstruye ni se guarda el fichero original del cliente.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points

from .dxf_engine import Contour, GeometryError, _remove_duplicates, analyze_contours, render_svg

MAX_CONTOURS = 5000
MAX_POINTS_PER_CONTOUR = 20000
MAX_TOTAL_POINTS = 400_000
MAX_COORD = 100_000.0      # mm (100 m): cualquier cosa mayor es un error de unidades o datos manipulados
MAX_ISSUES = 300


class EditError(ValueError):
    """Datos de edición no válidos (mensaje comprensible para el usuario)."""


# --------------------------------------------------------------------------- validación

def parse_contours(payload) -> list[tuple[list[tuple[float, float]], bool]]:
    """Valida la lista [{points: [[x, y], ...], closed: bool}, ...] enviada por el editor."""
    if not isinstance(payload, list):
        raise EditError("Formato de geometría no válido.")
    if not payload:
        raise EditError("La pieza no puede quedarse sin contornos.")
    if len(payload) > MAX_CONTOURS:
        raise EditError(f"Demasiados contornos (máximo {MAX_CONTOURS}).")
    out: list[tuple[list[tuple[float, float]], bool]] = []
    total = 0
    for item in payload:
        if not isinstance(item, dict):
            raise EditError("Formato de contorno no válido.")
        raw = item.get("points")
        closed = item.get("closed")
        if not isinstance(raw, list) or not isinstance(closed, bool):
            raise EditError("Formato de contorno no válido.")
        if len(raw) > MAX_POINTS_PER_CONTOUR:
            raise EditError(f"Un contorno supera el máximo de {MAX_POINTS_PER_CONTOUR} puntos; simplifícalo.")
        total += len(raw)
        if total > MAX_TOTAL_POINTS:
            raise EditError(f"La geometría supera el máximo de {MAX_TOTAL_POINTS} puntos; simplifícala.")
        pts: list[tuple[float, float]] = []
        for p in raw:
            if not (isinstance(p, (list, tuple)) and len(p) == 2):
                raise EditError("Punto no válido.")
            x, y = p
            if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) \
                    or not isinstance(y, (int, float)):
                raise EditError("Punto no válido.")
            x, y = float(x), float(y)
            if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > MAX_COORD or abs(y) > MAX_COORD:
                raise EditError("Coordenada fuera de rango.")
            pts.append((round(x, 4), round(y, 4)))
        # quitar puntos consecutivos repetidos
        clean = [pts[0]] if pts else []
        for p in pts[1:]:
            if math.dist(p, clean[-1]) > 1e-6:
                clean.append(p)
        if closed:
            if len(clean) > 1 and math.dist(clean[0], clean[-1]) <= 1e-6:
                clean.pop()
            if len(clean) < 3:
                closed = False      # un «cerrado» de 2 puntos es un segmento
            else:
                clean.append(clean[0])
        if len(clean) < 2:
            continue
        out.append((clean, closed))
    if not out:
        raise EditError("La pieza no puede quedarse sin contornos.")
    return out


def _length(pts) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


# --------------------------------------------------------------------------- clasificación por contorno

def classify(polys) -> list[str]:
    """Tipo de cada contorno en el mismo orden que la entrada (sin unir ni reordenar)."""
    kinds = ["abierto"] * len(polys)
    shapes: list[tuple[int, Polygon]] = []
    for i, (pts, closed) in enumerate(polys):
        if not closed:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.geom_type != "Polygon" or poly.is_empty:
                continue
        shapes.append((i, poly))
    shapes.sort(key=lambda s: s[1].area, reverse=True)
    for k, (i, poly) in enumerate(shapes):
        rp = poly.representative_point()
        depth = sum(1 for _, other in shapes[:k] if other.area > poly.area and other.contains(rp))
        kinds[i] = "exterior" if depth % 2 == 0 else "interior"
    return kinds


# --------------------------------------------------------------------------- fabricabilidad

@dataclass
class Rules:
    """Reglas prácticas de corte láser 2D (fibra/CO2) en función del espesor.

    - Agujero mínimo: ≥ 1 × espesor en acero/galvanizado; ≥ 1,5 × espesor en inoxidable y aluminio
      (mayor reflexión/conductividad). Por debajo de 0,5 × espesor (o 1 mm) no es fiable.
    - Puente mínimo entre contornos (o al borde): ≥ 1 × espesor; por debajo de 0,5 × espesor el material
      se deforma o se quema.
    - Contornos de menos de 0,5 mm suelen ser restos del dibujo.
    """
    thickness: float
    family: str = "acero"

    @property
    def hole_factor(self) -> float:
        return 1.5 if self.family in ("inox", "aluminio") else 1.0

    @property
    def min_hole(self) -> float:
        return max(self.thickness * self.hole_factor, 0.5)

    @property
    def critical_hole(self) -> float:
        return max(self.thickness * 0.5, 1.0)

    @property
    def min_web(self) -> float:
        return max(self.thickness, 0.5)

    @property
    def critical_web(self) -> float:
        return max(self.thickness * 0.5, 0.3)


def _fmt(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def check_manufacturability(polys, kinds: list[str], rules: Rules) -> list[dict]:
    issues: list[dict] = []

    def add(severity: str, code: str, msg: str, at, idx: list[int]):
        if len(issues) < MAX_ISSUES:
            issues.append({"severity": severity, "code": code, "msg": msg,
                           "x": round(at[0], 3), "y": round(at[1], 3), "contours": idx})

    lines: list[LineString] = []
    polygons: dict[int, Polygon] = {}
    for i, (pts, closed) in enumerate(polys):
        lines.append(LineString(pts))
        if closed:
            poly = Polygon(pts)
            if not poly.is_valid:
                add("error", "autointerseccion", f"Contorno {i + 1}: se corta a sí mismo; el láser no puede "
                    "separar la pieza correctamente.", pts[0], [i])
            else:
                polygons[i] = poly

    for i, (pts, closed) in enumerate(polys):
        length = _length(pts)
        if not closed:
            gap = math.dist(pts[0], pts[-1])
            add("aviso", "abierto",
                f"Contorno {i + 1} abierto (extremos a {_fmt(gap)} mm): ciérralo o únelo a otro.", pts[0], [i])
        if length < 0.5:
            add("aviso", "diminuto", f"Contorno {i + 1} de solo {_fmt(length)} mm: probablemente es un resto "
                "del dibujo.", pts[0], [i])
        if kinds[i] == "interior" and i in polygons:
            minx, miny, maxx, maxy = polygons[i].bounds
            size = min(maxx - minx, maxy - miny)
            c = polygons[i].centroid
            if size < rules.critical_hole:
                add("error", "agujero", f"Interior {i + 1} de {_fmt(size)} mm: demasiado pequeño para "
                    f"{_fmt(rules.thickness)} mm de espesor (no fiable por debajo de {_fmt(rules.critical_hole)} mm). "
                    "Valorar taladrar o marcar.", (c.x, c.y), [i])
            elif size < rules.min_hole:
                add("aviso", "agujero", f"Interior {i + 1} de {_fmt(size)} mm: por debajo del mínimo recomendado "
                    f"({_fmt(rules.min_hole)} mm); la calidad del agujero puede ser baja.", (c.x, c.y), [i])

    # Puentes: distancia mínima entre contornos (STRtree para no comparar todos con todos)
    if len(lines) > 1:
        tree = STRtree(lines)
        seen: set[tuple[int, int]] = set()
        for i, line in enumerate(lines):
            for j in tree.query(line, predicate="dwithin", distance=rules.min_web):
                j = int(j)
                if j <= i or (i, j) in seen:
                    continue
                seen.add((i, j))
                other = lines[j]
                if line.intersects(other):
                    pt = line.intersection(other).representative_point()
                    add("error", "cruce", f"Los contornos {i + 1} y {j + 1} se cruzan o se tocan.", (pt.x, pt.y), [i, j])
                    continue
                if not (polys[i][1] and polys[j][1]):
                    continue        # tramos abiertos: ya avisados como «abierto»; se resuelven uniéndolos
                d = line.distance(other)
                a, b = nearest_points(line, other)
                mid = ((a.x + b.x) / 2, (a.y + b.y) / 2)
                if d < rules.critical_web:
                    add("error", "puente", f"Solo {_fmt(d)} mm de material entre los contornos {i + 1} y {j + 1} "
                        f"(mínimo crítico {_fmt(rules.critical_web)} mm): se quemará o deformará.", mid, [i, j])
                else:
                    add("aviso", "puente", f"Puente de {_fmt(d)} mm entre los contornos {i + 1} y {j + 1}, menor que "
                        f"el espesor ({_fmt(rules.min_web)} mm recomendado).", mid, [i, j])

    # Piezas sueltas dentro de un agujero (exteriores con nivel > 0)
    for i, poly in polygons.items():
        if kinds[i] != "exterior":
            continue
        rp = poly.representative_point()
        if any(j != i and kinds[j] == "interior" and q.area > poly.area and q.contains(rp)
               for j, q in polygons.items()):
            add("info", "isla", f"Contorno {i + 1}: pieza suelta dentro de un agujero (se oferta como pieza "
                "aparte; comprobar si es un retal que debe caer).", (rp.x, rp.y), [i])

    if not any(k == "exterior" for k in kinds):
        add("error", "sin_exterior", "No hay ningún contorno exterior cerrado: no se puede calcular área ni peso.",
            polys[0][0][0], [])
    order = {"error": 0, "aviso": 1, "info": 2}
    issues.sort(key=lambda it: order[it["severity"]])
    return issues


# --------------------------------------------------------------------------- recálculo

def _preview(contours: list[Contour], max_points: int) -> list:
    out = []
    for c in contours[:300] if max_points <= 200 else contours:
        step = max(1, len(c.points) // max_points)
        pts = [[round(x, 3), round(y, 3)] for x, y in c.points[::step]]
        if c.closed and pts[-1] != pts[0]:
            pts.append(pts[0])
        out.append([pts, c.closed, c.kind])
    return out


def rebuild(polys, units_source: str) -> tuple[dict, str]:
    """Recalcula métricas, vista previa y SVG como si el plano se hubiera subido así."""
    polys, dups = _remove_duplicates(polys)
    try:
        geom, contours = analyze_contours(polys, [], units_source=units_source, duplicates_removed=dups)
    except GeometryError as exc:
        raise EditError(str(exc)) from exc
    metrics = geom.to_dict()
    metrics["preview"] = _preview(contours, 200)
    metrics["geometry"] = _preview(contours, MAX_POINTS_PER_CONTOUR)
    return metrics, render_svg(contours)


def analyze(polys, rules: Rules | None) -> dict:
    """Análisis sin guardar para el editor: métricas, tipo por contorno y comprobaciones."""
    kinds = classify(polys)
    metrics, _ = rebuild(polys, "mm")
    keys = ("cut_length_mm", "pierce_count", "net_area_mm2", "bbox_width_mm", "bbox_height_mm",
            "min_rect_width_mm", "min_rect_height_mm", "contour_count", "part_count", "open_contours",
            "rapid_length_mm", "smallest_inner_feature_mm", "duplicates_removed")
    return {"metrics": {k: metrics[k] for k in keys}, "kinds": kinds,
            "issues": check_manufacturability(polys, kinds, rules) if rules else []}


# --------------------------------------------------------------------------- operaciones de forma (servidor)

from shapely.ops import unary_union  # noqa: E402

MAX_OFFSET = 1000.0
JOINS = {"redondo": 1, "inglete": 2, "chaflan": 3}


def _quad_segs(r: float) -> int:
    r = abs(r)
    if r <= 0:
        return 8
    step = 2 * math.acos(max(-1.0, 1 - 0.01 / r))
    return int(min(64, max(8, math.ceil((math.pi / 2) / step))))


def _closed_polygons(polys) -> list[Polygon]:
    out = []
    for pts, closed in polys:
        if not closed:
            raise EditError("Esta operación solo admite contornos cerrados: cierra o une antes los abiertos.")
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            out.append(poly)
    if not out:
        raise EditError("Selecciona al menos un contorno cerrado.")
    return out


def _rings(geom) -> list[dict]:
    rings: list[dict] = []
    geoms = getattr(geom, "geoms", [geom])
    for g in geoms:
        if g.is_empty or g.geom_type != "Polygon" or g.area < 1e-6:
            continue
        for ring in [g.exterior, *g.interiors]:
            pts = [[round(x, 4), round(y, 4)] for x, y in ring.coords]
            if len(pts) >= 4:
                rings.append({"points": pts, "closed": True})
    if not rings:
        raise EditError("El resultado de la operación queda vacío.")
    return rings


def shape_operation(polys, op: str, distance: float = 0.0, join: str = "redondo") -> list[dict]:
    """Equidistancia (offset) y operaciones booleanas sobre contornos cerrados.

    Los contornos se interpretan con la regla par-impar (un agujero dentro de un exterior resta), igual que el corte.
    """
    shapes = _closed_polygons(polys)
    if op == "offset":
        if not math.isfinite(distance) or abs(distance) > MAX_OFFSET or abs(distance) < 1e-4:
            raise EditError(f"La distancia debe estar entre 0,0001 y {MAX_OFFSET:g} mm.")
        region = shapes[0]
        for s in shapes[1:]:
            region = region.symmetric_difference(s)
        js = JOINS.get(join, 1)
        res = region.buffer(distance, quad_segs=_quad_segs(distance), join_style=js, mitre_limit=10)
        return _rings(res)
    if len(shapes) < 2:
        raise EditError("Selecciona al menos dos contornos cerrados.")
    if op == "union":
        return _rings(unary_union(shapes))
    if op == "diferencia":
        shapes.sort(key=lambda s: s.area, reverse=True)
        return _rings(shapes[0].difference(unary_union(shapes[1:])))
    if op == "interseccion":
        res = shapes[0]
        for s in shapes[1:]:
            res = res.intersection(s)
        return _rings(res)
    raise EditError("Operación no válida.")


def to_dxf(contours: list, units_source: str = "mm") -> bytes:
    """DXF limpio (R2010, mm) con una polilínea por contorno y capas por tipo. Sin datos del fichero original."""
    import io

    import ezdxf
    doc = ezdxf.new("R2010", units=4)
    doc.header["$MEASUREMENT"] = 1
    for name, color in (("CORTE_EXTERIOR", 5), ("CORTE_INTERIOR", 1), ("ABIERTO", 30), ("MARCADO", 6)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()
    layer = {"exterior": "CORTE_EXTERIOR", "interior": "CORTE_INTERIOR", "marcado": "MARCADO"}
    for pts, closed, kind in contours:
        p = [(x, y) for x, y in pts]
        if closed and len(p) > 1 and p[0] == p[-1]:
            p = p[:-1]
        if len(p) < 2:
            continue
        msp.add_lwpolyline(p, close=bool(closed), dxfattribs={"layer": layer.get(kind, "ABIERTO")})
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")
