"""Genera DXF de prueba con geometría conocida."""
import ezdxf
from pathlib import Path

OUT = Path(__file__).parent / "fixtures"

def plate_with_holes(path):
    # Placa 200x100 mm con 4 taladros Ø10 y un coliso 40x10 (líneas+arcos sueltos)
    doc = ezdxf.new(units=4)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    for x, y in [(15, 15), (185, 15), (185, 85), (15, 85)]:
        msp.add_circle((x, y), 5)
    # coliso: dos líneas + dos semicírculos, entidades separadas
    msp.add_line((80, 45), (120, 45))
    msp.add_arc((120, 50), 5, -90, 90)
    msp.add_line((120, 55), (80, 55))
    msp.add_arc((80, 50), 5, 90, 270)
    # duplicado de un taladro (error típico)
    msp.add_circle((15, 15), 5)
    # texto y cotas ignoradas
    doc.layers.add("COTAS")
    msp.add_line((0, -10), (200, -10), dxfattribs={"layer": "COTAS"})
    msp.add_text("PIEZA-01")
    doc.saveas(path)

def flange_inches(path):
    # Brida en pulgadas: disco Ø4" con agujero Ø2"
    doc = ezdxf.new(units=1)
    msp = doc.modelspace()
    msp.add_circle((0, 0), 2)
    msp.add_circle((0, 0), 1)
    doc.saveas(path)

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    plate_with_holes(OUT / "placa_taladros.dxf")
    flange_inches(OUT / "brida_pulgadas.dxf")
    print("ok")
