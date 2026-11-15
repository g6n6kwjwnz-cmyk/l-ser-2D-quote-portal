import math

import ezdxf
import pytest

from app.geometry.dxf_engine import GeometryError, analyze_dxf, render_svg
from tests.conftest import FIXTURES


def _read(name):
    return (FIXTURES / name).read_bytes()


def test_plate_with_holes_metrics():
    g, contours = analyze_dxf(_read("placa_taladros.dxf"))
    slot = 2 * 40 + math.pi * 10
    expected_len = 600 + 4 * math.pi * 10 + slot
    assert g.cut_length_mm == pytest.approx(expected_len, rel=1e-3)
    assert g.pierce_count == 6                      # 1 exterior + 4 taladros + 1 coliso (unido de 4 entidades)
    assert g.part_count == 1
    expected_area = 200 * 100 - 4 * math.pi * 25 - (40 * 10 + math.pi * 25)
    assert g.net_area_mm2 == pytest.approx(expected_area, rel=1e-3)
    assert (g.bbox_width_mm, g.bbox_height_mm) == (200, 100)   # la capa COTAS se ignora
    assert g.duplicates_removed == 1
    assert g.smallest_inner_feature_mm == pytest.approx(10, abs=0.01)
    kinds = sorted(c.kind for c in contours)
    assert kinds.count("interior") == 5 and kinds.count("exterior") == 1
    assert render_svg(contours).startswith("<svg")


def test_inches_are_converted_to_mm():
    g, _ = analyze_dxf(_read("brida_pulgadas.dxf"))
    assert g.cut_length_mm == pytest.approx(math.pi * (101.6 + 50.8), rel=1e-3)
    assert g.net_area_mm2 == pytest.approx(math.pi * (50.8 ** 2 - 25.4 ** 2), rel=1e-3)


def test_open_contour_warns(tmp_path):
    doc = ezdxf.new(units=4)
    doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50)])
    p = tmp_path / "abierto.dxf"
    doc.saveas(p)
    g, _ = analyze_dxf(p.read_bytes())
    assert g.open_contours == 1
    assert any("abierto" in w for w in g.warnings)


def test_island_inside_hole_counts_as_part(tmp_path):
    doc = ezdxf.new(units=4)
    msp = doc.modelspace()
    msp.add_circle((0, 0), 50)
    msp.add_circle((0, 0), 30)
    msp.add_circle((0, 0), 10)   # isla dentro del agujero
    p = tmp_path / "isla.dxf"
    doc.saveas(p)
    g, _ = analyze_dxf(p.read_bytes())
    assert g.part_count == 2
    assert g.net_area_mm2 == pytest.approx(math.pi * (2500 - 900 + 100), rel=1e-3)


def test_blocks_are_exploded(tmp_path):
    doc = ezdxf.new(units=4)
    blk = doc.blocks.new("TALADRO")
    blk.add_circle((0, 0), 4)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True)
    for x in (20, 50, 80):
        msp.add_blockref("TALADRO", (x, 50))
    p = tmp_path / "bloques.dxf"
    doc.saveas(p)
    g, _ = analyze_dxf(p.read_bytes())
    assert g.pierce_count == 4


def test_invalid_file():
    with pytest.raises(GeometryError):
        analyze_dxf(b"esto no es un dxf")
