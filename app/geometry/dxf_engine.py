"""Motor geométrico: analiza un DXF 2D y extrae los datos necesarios para ofertar corte láser.

Resultado principal (GeometryResult):
  - longitud total de corte (mm) y por contorno
  - nº de perforaciones (un pierce por contorno)
  - área neta de la pieza (contornos exteriores menos agujeros)
  - envolvente rectangular (alineada y mínima rotada)
  - recorrido en vacío estimado entre contornos
  - diámetro/tamaño del contorno interior más pequeño (para avisos de viabilidad)
  - avisos (contornos abiertos, entidades duplicadas, unidades no definidas...)

El fichero original no se guarda: solo se conservan las métricas y la vista previa SVG.
"""
from __future__ import annotations

import io
import math
import os
import tempfile
from dataclasses import dataclass, field, asdict

import ezdxf
from ezdxf import recover, units as dxf_units
from ezdxf.path import make_path
from shapely.geometry import Polygon, Point
from shapely.validation import make_valid

# Tolerancias (mm)
FLATTEN_TOL = 0.01      # desviación máxima de cuerda al discretizar arcos/splines
JOIN_TOL = 0.05         # distancia máxima para unir extremos de entidades abiertas

CUTTABLE_TYPES = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "LWPOLYLINE", "POLYLINE"}
IGNORED_LAYER_KEYWORDS = ("defpoints", "cota", "dim", "text", "anot", "rotul", "marco", "cajetin")


class GeometryError(ValueError):
    """Error comprensible para el usuario al analizar un plano."""


@dataclass
class Contour:
    points: list[tuple[float, float]]
    closed: bool
    length: float
    kind: str = "exterior"      # exterior | interior | abierto
    area: float = 0.0

    @property
    def start(self) -> tuple[float, float]:
        return self.points[0]


@dataclass
class GeometryResult:
    units_source: str
    contour_count: int
    part_count: int
    cut_length_mm: float
    pierce_count: int
    net_area_mm2: float
    gross_outer_area_mm2: float
    bbox_width_mm: float
    bbox_height_mm: float
    min_rect_width_mm: float
    min_rect_height_mm: float
    rapid_length_mm: float
    smallest_inner_feature_mm: float | None
    contour_lengths_mm: list[float]
    open_contours: int
    duplicates_removed: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- lectura

BINARY_SENTINEL = b"AutoCAD Binary DXF"


def _read_document(data: bytes):
    try:
        if data.startswith(BINARY_SENTINEL):
            # DXF binario: ezdxf lo lee desde fichero; se usa un temporal que se borra al momento
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "plano.dxf")
                with open(path, "wb") as fh:
                    fh.write(data)
                doc = ezdxf.readfile(path)
            return doc
        doc, auditor = recover.read(io.BytesIO(data))
    except (IOError, ezdxf.DXFStructureError, ValueError) as exc:
        raise GeometryError(f"El fichero no es un DXF válido ({exc}).") from exc
    if auditor.has_errors:
        # recover ya intenta reparar; se continúa y se avisa
        pass
    return doc


def _layer_is_ignored(doc, layer_name: str) -> bool:
    name = (layer_name or "").lower()
    if any(k in name for k in IGNORED_LAYER_KEYWORDS):
        return True
    layer = doc.layers.get(layer_name) if layer_name in doc.layers else None
    if layer is not None and (layer.is_off() or layer.is_frozen()):
        return True
    return False


UNIT_CHOICES = {"auto": None, "mm": 4, "cm": 5, "m": 6, "in": 1}


def _unit_factor(doc, warnings: list[str], override: int | None = None) -> tuple[float, str]:
    if override:
        return dxf_units.conversion_factor(override, dxf_units.MM), f"{dxf_units.unit_name(override)} (indicado al subir)"
    code = doc.units
    if code in (0, None):
        warnings.append("El DXF no define unidades ($INSUNITS); se asumen milímetros. "
                        "Si el plano está en pulgadas, vuelve a subirlo indicando la unidad.")
        return 1.0, "sin definir (mm asumidos)"
    try:
        factor = dxf_units.conversion_factor(code, dxf_units.MM)
    except Exception:  # unidad exótica
        warnings.append(f"Unidad DXF {code} no reconocida; se asumen milímetros.")
        return 1.0, f"código {code} (mm asumidos)"
    return factor, dxf_units.unit_name(code)


NON_CUT_CONTAINERS = {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION", "LEADER", "MLEADER",
                      "MULTILEADER", "TOLERANCE", "TEXT", "MTEXT", "ATTRIB", "ATTDEF", "HATCH", "VIEWPORT"}


def _cut_entities(doc, entities, parent_layer: str | None = None, depth: int = 0):
    """Recorre el dibujo explotando bloques, descartando cotas/textos y capas no cortables.

    Las entidades en capa «0» dentro de un bloque heredan la capa del bloque insertado (regla de AutoCAD).
    """
    if depth > 16:  # protección frente a bloques recursivos o anidamientos absurdos
        return
    for e in entities:
        etype = e.dxftype()
        if etype in NON_CUT_CONTAINERS:
            continue
        layer = e.dxf.get("layer", "0")
        if parent_layer and layer == "0":
            layer = parent_layer
        if _layer_is_ignored(doc, layer):
            continue
        if etype == "INSERT":
            try:
                children = list(e.virtual_entities())
            except Exception:
                continue
            yield from _cut_entities(doc, children, layer, depth + 1)
        elif etype in CUTTABLE_TYPES:
            yield e


def _extract_polylines(doc, factor: float, warnings: list[str]):
    """Devuelve lista de (puntos, cerrado) en mm a partir del espacio modelo (bloques explotados)."""
    raw: list[tuple[list[tuple[float, float]], bool]] = []
    skipped_3d = 0
    for entity in _cut_entities(doc, doc.modelspace()):
        try:
            path = make_path(entity)
        except Exception:
            continue
        for sub in path.sub_paths() if path.has_sub_paths else [path]:
            verts = list(sub.flattening(distance=FLATTEN_TOL / max(factor, 1e-9)))
            if len(verts) < 2:
                continue
            if any(abs(v.z) > 1e-6 for v in verts):
                skipped_3d += 1
            pts = [(v.x * factor, v.y * factor) for v in verts]
            closed = math.dist(pts[0], pts[-1]) <= JOIN_TOL
            if closed:
                pts[-1] = pts[0]
            raw.append((pts, closed))
    if skipped_3d:
        warnings.append(f"{skipped_3d} entidades con coordenada Z distinta de 0; se han proyectado al plano XY.")
    return raw


# --------------------------------------------------------------------------- limpieza y unión

def _signature(pts: list[tuple[float, float]]) -> tuple:
    rounded = tuple((round(x, 2), round(y, 2)) for x, y in pts)
    return min(rounded, tuple(reversed(rounded)))


def _remove_duplicates(polys):
    seen: set = set()
    out = []
    removed = 0
    for pts, closed in polys:
        sig = (_signature(pts), closed)
        if sig in seen:
            removed += 1
            continue
        seen.add(sig)
        out.append((pts, closed))
    return out, removed


def _chain_open(open_polys: list[list[tuple[float, float]]]):
    """Une tramos abiertos por sus extremos para formar contornos."""
    remaining = [list(p) for p in open_polys]
    chains: list[tuple[list[tuple[float, float]], bool]] = []
    while remaining:
        chain = remaining.pop(0)
        extended = True
        while extended:
            extended = False
            if math.dist(chain[0], chain[-1]) <= JOIN_TOL and len(chain) > 2:
                break
            for i, seg in enumerate(remaining):
                if math.dist(chain[-1], seg[0]) <= JOIN_TOL:
                    chain = chain + seg[1:]
                elif math.dist(chain[-1], seg[-1]) <= JOIN_TOL:
                    chain = chain + list(reversed(seg))[1:]
                elif math.dist(chain[0], seg[-1]) <= JOIN_TOL:
                    chain = seg[:-1] + chain
                elif math.dist(chain[0], seg[0]) <= JOIN_TOL:
                    chain = list(reversed(seg))[:-1] + chain
                else:
                    continue
                remaining.pop(i)
                extended = True
                break
        closed = len(chain) > 2 and math.dist(chain[0], chain[-1]) <= JOIN_TOL
        if closed:
            chain[-1] = chain[0]
        chains.append((chain, closed))
    return chains


def _length(pts) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


# --------------------------------------------------------------------------- análisis

def _nearest_neighbour_route(points: list[tuple[float, float]], origin: tuple[float, float]) -> float:
    todo = list(points)
    cur = origin
    total = 0.0
    while todo:
        idx = min(range(len(todo)), key=lambda i: math.dist(cur, todo[i]))
        total += math.dist(cur, todo[idx])
        cur = todo.pop(idx)
    return total + math.dist(cur, origin)


def analyze_contours(polys: list[tuple[list[tuple[float, float]], bool]], warnings: list[str],
                     units_source: str = "mm", duplicates_removed: int = 0) -> tuple[GeometryResult, list[Contour]]:
    closed = [(p, c) for p, c in polys if c]
    open_ = [p for p, c in polys if not c]
    chained = _chain_open(open_) if open_ else []
    all_polys = closed + chained

    contours: list[Contour] = []
    for pts, is_closed in all_polys:
        length = _length(pts)
        if length < 1e-6:
            continue
        contours.append(Contour(points=pts, closed=is_closed, length=length))

    if not contours:
        raise GeometryError("No se han encontrado contornos de corte en el plano "
                            "(líneas, arcos, círculos, polilíneas o splines).")

    # Polígonos de los contornos cerrados y su anidamiento (exterior/interior/isla)
    shapes: list[tuple[Contour, Polygon]] = []
    for c in contours:
        if not c.closed:
            c.kind = "abierto"
            continue
        poly = Polygon(c.points)
        if not poly.is_valid:
            poly = make_valid(poly)
            if poly.geom_type != "Polygon":
                poly = max(getattr(poly, "geoms", [poly]), key=lambda g: getattr(g, "area", 0))
                if poly.geom_type != "Polygon":
                    c.kind = "abierto"
                    continue
        c.area = poly.area
        shapes.append((c, poly))

    shapes.sort(key=lambda s: s[1].area, reverse=True)
    net_area = 0.0
    gross_outer = 0.0
    parts = 0
    inner_sizes: list[float] = []
    for i, (c, poly) in enumerate(shapes):
        rp = poly.representative_point()
        depth = sum(1 for j, (_, other) in enumerate(shapes[:i]) if other.area > poly.area and other.contains(rp))
        if depth % 2 == 0:
            c.kind = "exterior"
            net_area += poly.area
            parts += 1
            if depth == 0:
                gross_outer += poly.area
        else:
            c.kind = "interior"
            net_area -= poly.area
            minx, miny, maxx, maxy = poly.bounds
            inner_sizes.append(min(maxx - minx, maxy - miny))

    open_count = sum(1 for c in contours if c.kind == "abierto")
    if open_count:
        warnings.append(f"{open_count} contorno(s) abierto(s): revisar el plano. Se cortan y cuentan, "
                        "pero no aportan área.")
    if duplicates_removed:
        warnings.append(f"Se han eliminado {duplicates_removed} entidades duplicadas/superpuestas.")
    if parts == 0:
        warnings.append("No hay ningún contorno exterior cerrado: no se puede calcular área ni peso.")
    if parts > 1:
        warnings.append(f"El plano contiene {parts} piezas independientes; se ofertan como un conjunto.")

    all_pts = [pt for c in contours for pt in c.points]
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)

    hull = Polygon(all_pts).convex_hull if len(all_pts) >= 3 else None
    if hull is not None and hull.geom_type == "Polygon":
        mrr = hull.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
        e1 = math.dist(coords[0], coords[1])
        e2 = math.dist(coords[1], coords[2])
        min_w, min_h = max(e1, e2), min(e1, e2)
    else:
        min_w, min_h = maxx - minx, maxy - miny

    # Recorrido en vacío: interiores primero, exteriores al final (práctica habitual de corte)
    starts = [c.start for c in contours if c.kind != "exterior"] + [c.start for c in contours if c.kind == "exterior"]
    rapid = _nearest_neighbour_route(starts, (minx, miny))

    result = GeometryResult(
        units_source=units_source,
        contour_count=len(contours),
        part_count=parts,
        cut_length_mm=round(sum(c.length for c in contours), 3),
        pierce_count=len(contours),
        net_area_mm2=round(max(net_area, 0.0), 3),
        gross_outer_area_mm2=round(gross_outer, 3),
        bbox_width_mm=round(maxx - minx, 3),
        bbox_height_mm=round(maxy - miny, 3),
        min_rect_width_mm=round(min_w, 3),
        min_rect_height_mm=round(min_h, 3),
        rapid_length_mm=round(rapid, 3),
        smallest_inner_feature_mm=round(min(inner_sizes), 3) if inner_sizes else None,
        contour_lengths_mm=[round(c.length, 3) for c in contours],
        open_contours=open_count,
        duplicates_removed=duplicates_removed,
        warnings=warnings,
    )
    return result, contours


def analyze_dxf(data: bytes, units: str = "auto") -> tuple[GeometryResult, list[Contour]]:
    warnings: list[str] = []
    doc = _read_document(data)
    factor, unit_name = _unit_factor(doc, warnings, UNIT_CHOICES.get(units))
    polys = _extract_polylines(doc, factor, warnings)
    polys, dups = _remove_duplicates(polys)
    result, contours = analyze_contours(polys, warnings, units_source=unit_name, duplicates_removed=dups)
    size = max(result.bbox_width_mm, result.bbox_height_mm)
    if size < 10:
        warnings.append(f"La pieza mide solo {size:.1f} mm: comprueba las unidades (¿pulgadas o metros?).")
    elif size > 6000:
        warnings.append(f"La pieza mide {size:.0f} mm: comprueba las unidades (¿dibujada en décimas o cm?).")
    return result, contours


# --------------------------------------------------------------------------- vista previa

def render_svg(contours: list[Contour], max_points_per_contour: int = 4000) -> str:
    """SVG autocontenido (eje Y invertido como en CAD). Sin datos del fichero original."""
    xs = [p[0] for c in contours for p in c.points]
    ys = [p[1] for c in contours for p in c.points]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = max(maxx - minx, 1e-3), max(maxy - miny, 1e-3)
    pad = max(w, h) * 0.04
    stroke = max(w, h) / 400
    colors = {"exterior": "var(--cut-outer, #2563eb)", "interior": "var(--cut-inner, #dc2626)",
              "abierto": "var(--cut-open, #d97706)"}
    parts = []
    for c in contours:
        pts = c.points
        if len(pts) > max_points_per_contour:
            step = math.ceil(len(pts) / max_points_per_contour)
            pts = pts[::step] + [pts[-1]]
        d = "M" + " L".join(f"{x - minx:.3f},{maxy - y:.3f}" for x, y in pts)
        if c.closed:
            d += " Z"
        parts.append(f'<path d="{d}" fill="none" stroke="{colors[c.kind]}" stroke-width="{stroke:.3f}" '
                     f'stroke-linejoin="round"/>')
        sx, sy = c.start
        parts.append(f'<circle cx="{sx - minx:.3f}" cy="{maxy - sy:.3f}" r="{stroke * 2.2:.3f}" '
                     f'fill="var(--pierce, #16a34a)"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad:.3f} {-pad:.3f} {w + 2 * pad:.3f} '
            f'{h + 2 * pad:.3f}" preserveAspectRatio="xMidYMid meet" role="img" '
            f'aria-label="Vista previa de la pieza">{"".join(parts)}</svg>')
