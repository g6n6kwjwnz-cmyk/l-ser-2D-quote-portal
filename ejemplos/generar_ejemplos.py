"""Genera piezas DXF de ejemplo para probar el portal (python ejemplos/generar_ejemplos.py)."""
import math
from pathlib import Path

import ezdxf

OUT = Path(__file__).parent


def new(version="R2018", units=4):
    doc = ezdxf.new(version, setup=True)
    doc.units = units
    return doc, doc.modelspace()


def rounded_rect(msp, x0, y0, w, h, r):
    pts = [(x0 + r, y0, 0), (x0 + w - r, y0, math.tan(math.radians(22.5))),
           (x0 + w, y0 + r, 0), (x0 + w, y0 + h - r, math.tan(math.radians(22.5))),
           (x0 + w - r, y0 + h, 0), (x0 + r, y0 + h, math.tan(math.radians(22.5))),
           (x0, y0 + h - r, 0), (x0, y0 + r, math.tan(math.radians(22.5)))]
    msp.add_lwpolyline(pts, format="xyb", close=True)


def placa_anclaje():
    """01 · Placa de anclaje 250x250 con 4 taladros Ø18 y taladro central Ø60 — DXF R2018."""
    doc, msp = new()
    rounded_rect(msp, 0, 0, 250, 250, 15)
    for x, y in [(30, 30), (220, 30), (220, 220), (30, 220)]:
        msp.add_circle((x, y), 9)
    msp.add_circle((125, 125), 30)
    doc.layers.add("COTAS")
    msp.add_text("PLACA ANCLAJE 250x250", dxfattribs={"layer": "COTAS", "height": 5}).set_placement((0, -15))
    # cotas reales (entidades DIMENSION) en una capa cualquiera: no deben contarse como corte
    msp.add_linear_dim(base=(125, 280), p1=(0, 250), p2=(250, 250), dimstyle="EZDXF").render()
    msp.add_linear_dim(base=(280, 125), p1=(250, 0), p2=(250, 250), angle=90, dimstyle="EZDXF").render()
    doc.saveas(OUT / "01_placa_anclaje_250x250_R2018.dxf")


def escuadra():
    """02 · Escuadra en L con radio interior y dos colisos — DXF R2000, entidades sueltas (líneas+arcos)."""
    doc, msp = new("R2000")
    L = [(0, 0), (200, 0), (200, 40), (60, 40)]
    for a, b in zip(L, L[1:]):
        msp.add_line(a, b)
    msp.add_arc((60, 60), 20, 180, 270)          # radio interior
    msp.add_line((40, 60), (40, 150))
    msp.add_line((40, 150), (0, 150))
    msp.add_line((0, 150), (0, 0))
    for cx, cy, horiz in [(130, 20, True), (20, 105, False)]:   # colisos 30x10
        if horiz:
            msp.add_line((cx - 15, cy - 5), (cx + 15, cy - 5)); msp.add_line((cx + 15, cy + 5), (cx - 15, cy + 5))
            msp.add_arc((cx + 15, cy), 5, -90, 90); msp.add_arc((cx - 15, cy), 5, 90, 270)
        else:
            msp.add_line((cx + 5, cy - 15), (cx + 5, cy + 15)); msp.add_line((cx - 5, cy + 15), (cx - 5, cy - 15))
            msp.add_arc((cx, cy + 15), 5, 0, 180); msp.add_arc((cx, cy - 15), 5, 180, 360)
    doc.saveas(OUT / "02_escuadra_L_colisos_R2000.dxf")


def brida():
    """03 · Brida Ø150 con 6 taladros Ø11 en Ø120 y centro Ø60 — dibujada en PULGADAS, DXF R2000."""
    doc, msp = new("R2000", units=1)
    inch = 25.4
    msp.add_circle((0, 0), 75 / inch)
    msp.add_circle((0, 0), 30 / inch)
    for i in range(6):
        a = math.radians(60 * i)
        msp.add_circle((60 / inch * math.cos(a), 60 / inch * math.sin(a)), 5.5 / inch)
    doc.saveas(OUT / "03_brida_D150_pulgadas_R2000.dxf")


def soporte_r12():
    """09 · Soporte trapezoidal con 2 taladros — DXF R12 (formato antiguo, sin unidades: se asumen mm)."""
    doc, msp = new("R12")
    msp.add_polyline2d([(0, 0), (180, 0), (140, 90), (40, 90)], close=True)
    msp.add_circle((50, 30), 7)
    msp.add_circle((130, 30), 7)
    doc.saveas(OUT / "09_soporte_trapecio_R12.dxf")


def engranaje():
    """04 · Rueda dentada simplificada (24 dientes) con chavetero — polilínea compleja, DXF R2010."""
    doc, msp = new("R2010")
    z, r_ext, r_int = 24, 60, 52
    pts = []
    for i in range(z):
        a0 = 2 * math.pi * i / z
        step = 2 * math.pi / z
        for frac, r in [(0.0, r_int), (0.15, r_ext), (0.5, r_ext), (0.65, r_int)]:
            a = a0 + frac * step
            pts.append((r * math.cos(a), r * math.sin(a)))
    msp.add_lwpolyline(pts, close=True)
    # agujero con chavetero
    msp.add_lwpolyline([(-3, 10.6), (-3, 13), (3, 13), (3, 10.6)], close=False)
    msp.add_arc((0, 0), 11, math.degrees(math.atan2(10.6, 3)), 360 + math.degrees(math.atan2(10.6, -3)))
    doc.saveas(OUT / "04_rueda_dentada_Z24_R2010.dxf")


def tapa_spline():
    """05 · Tapa con contorno curvo (spline) y 3 taladros — DXF R2018."""
    doc, msp = new()
    fit = [(0, 0), (80, -10), (160, 0), (180, 60), (150, 120), (80, 135), (10, 120), (-20, 60), (0, 0)]
    msp.add_spline(fit_points=fit).closed = False
    for x, y in [(30, 60), (80, 100), (140, 60)]:
        msp.add_circle((x, y), 6)
    doc.saveas(OUT / "05_tapa_curva_spline_R2018.dxf")


def bloques():
    """06 · Chapa perforada 300x200 con 40 taladros Ø8 insertados como BLOQUE — DXF R2018."""
    doc, msp = new()
    blk = doc.blocks.new("TALADRO_D8")
    blk.add_circle((0, 0), 4)
    msp.add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200)], close=True)
    for i in range(8):
        for j in range(5):
            msp.add_blockref("TALADRO_D8", (30 + i * 34, 30 + j * 35))
    doc.saveas(OUT / "06_chapa_perforada_bloques_R2018.dxf")


def varias_piezas():
    """07 · Tres arandelas en el mismo plano (varias piezas) — DXF binario."""
    doc, msp = new()
    for k, x in enumerate((0, 70, 140)):
        msp.add_circle((x, 0), 30)
        msp.add_circle((x, 0), 13)
    doc.saveas(OUT / "07_tres_arandelas_binario.dxf", fmt="bin")


def con_errores():
    """08 · Pieza con errores típicos: contorno abierto, línea duplicada y taladro Ø3 en chapa gruesa."""
    doc, msp = new()
    msp.add_line((0, 0), (120, 0)); msp.add_line((120, 0), (120, 80))
    msp.add_line((120, 0), (120, 80))                       # duplicada
    msp.add_line((120, 80), (0, 80)); msp.add_line((0, 80), (0, 1.5))  # hueco de 1,5 mm: no cierra
    msp.add_circle((60, 40), 1.5)                           # taladro Ø3
    doc.saveas(OUT / "08_pieza_con_errores_R2018.dxf")


if __name__ == "__main__":
    for f in (placa_anclaje, escuadra, brida, engranaje, tapa_spline, bloques, varias_piezas, con_errores, soporte_r12):
        f()
        print("OK", f.__doc__.strip().splitlines()[0])
