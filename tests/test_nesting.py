"""Anidado: motor, separación estándar, editor (comprobar/guardar), retales, DXF y coste de material."""
import io
import re

import ezdxf
from shapely.geometry import Polygon, box

from app.nesting.engine import NestItem, SheetStock, check_layout, nest, part_polygon
from tests.conftest import FIXTURES
from tests.test_requests import _login, _submit, anon  # noqa: F401 (fixture)


def _ring(x0, y0, w, h):
    return [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h], [x0, y0]]


def test_part_polygon_keeps_holes():
    shape = part_polygon([[_ring(0, 0, 100, 100), True, "exterior"], [_ring(25, 25, 50, 50), True, "interior"]])
    assert abs(shape.area - (10000 - 2500)) < 1e-6


def test_nest_respects_gap_and_edge():
    items = [NestItem("a", box(0, 0, 200, 100), 12), NestItem("b", Polygon([(0, 0), (150, 0), (0, 150)]), 10)]
    res = nest(items, 1000, 500, gap=8, edge=10)
    assert not res.unplaced
    shapes = {"a": items[0].shape, "b": items[1].shape}
    for s in res.sheets:
        assert check_layout(shapes, [p.to_dict() for p in s], 1000, 500, 8, 10) == []


def test_ring_hosts_small_part_in_hole():
    ring = Polygon([(0, 0), (400, 0), (400, 400), (0, 400)], [[(50, 50), (350, 50), (350, 350), (50, 350)]])
    res = nest([NestItem("ring", ring, 1), NestItem("small", box(0, 0, 100, 100), 1)], 420, 420, gap=5, edge=5)
    assert len(res.sheets) == 1 and not res.unplaced    # la pequeña solo cabe dentro del aro


def test_remnants_used_first():
    items = [NestItem("a", box(0, 0, 100, 100), 3)]
    res = nest(items, 3000, 1500, gap=5, edge=10, stock=[SheetStock("retal:7", 600, 400, 1)])
    assert res.sheet_sources[0]["key"] == "retal:7" and len(res.sheets) == 1


def test_check_layout_flags_overlap_and_edge():
    shapes = {"a": box(0, 0, 100, 100)}
    pls = [{"key": "a", "x": 10, "y": 10, "rotation": 0}, {"key": "a", "x": 112, "y": 10, "rotation": 0},
           {"key": "a", "x": 950, "y": 10, "rotation": 0}]
    conf = check_layout(shapes, pls, 1000, 500, 5, 10)
    reasons = {c["index"]: c["reason"] for c in conf}
    assert "separación" in reasons[0] and "fuera" in reasons[2]


def test_standard_gap_table(client):
    from app.models import NestSettings
    s = NestSettings(gap_table=[[1, 2, 5], [3, 3, 5], [5, 5, 8], [8, 8, 10], [12, 12, 12]],
                     extra_gap_families=["inox"], extra_gap_mm=1, extra_gap_from_mm=6)
    assert s.gap_edge("acero", 0.8) == (2, 5)
    assert s.gap_edge("acero", 4) == (5, 8)
    assert s.gap_edge("acero", 6) == (8, 10)
    assert s.gap_edge("inox", 6) == (9, 10)
    assert s.gap_edge("inox", 3) == (3, 5)


def _upload(client, name="placa_taladros.dxf"):
    r = client.post("/piezas", files={"file": (name, (FIXTURES / name).read_bytes(), "application/dxf")},
                    follow_redirects=False)
    return r.headers["location"].rsplit("/", 1)[1]


def test_manual_nest_editor_save_and_dxf(client):
    pid = _upload(client)
    r = client.post("/anidado", data={"material_id": "1", "thickness_mm": "4", "format_id": "1",
                                      f"qty-{pid}": "6", "use_remnants": "on"}, follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    page = client.get(url)
    assert page.status_code == 200 and "separación" in page.text and "nest.js" in page.text
    nid = int(url.rsplit("/", 1)[1])
    from app import main
    from app.models import Nest
    with main.SessionLocal() as db:
        n = db.get(Nest, nid)
        assert (n.gap_mm, n.edge_mm) == (5, 8) and n.conflicts == 0 and n.utilization_pct > 0
        sheets = n.sheets
    assert sum(len(s["placements"]) for s in sheets) == 6
    # el editor mueve una pieza encima de otra: el servidor marca conflicto
    bad = [{"source": sheets[0]["source"], "placements": [dict(p) for p in sheets[0]["placements"]]}]
    bad[0]["placements"][1].update(x=bad[0]["placements"][0]["x"], y=bad[0]["placements"][0]["y"])
    ev = client.post(f"/anidado/{nid}/comprobar", json={"sheets": bad}).json()
    assert ev["conflicts"] >= 2 and 0 in ev["sheets"][0]["conflicts"]
    # datos manipulados: pieza ajena o giro raro
    forged = [{"source": sheets[0]["source"], "placements": [{"key": "otra", "x": 0, "y": 0, "rotation": 0}]}]
    assert client.post(f"/anidado/{nid}/guardar", json={"sheets": forged}).status_code == 422
    # quitar una pieza y guardar: queda pendiente
    ok = [{"source": sheets[0]["source"], "placements": sheets[0]["placements"][1:]}]
    ev = client.post(f"/anidado/{nid}/guardar", json={"sheets": ok}).json()
    assert ev["pending"] == {pid: 1} and ev["conflicts"] == 0
    dxf = client.get(f"/anidado/{nid}/dxf")
    assert dxf.status_code == 200 and "attachment" in dxf.headers["content-disposition"]
    doc = ezdxf.read(io.StringIO(dxf.content.decode()))
    layers = {e.dxf.layer for e in doc.modelspace()}
    assert {"CHAPA", "CORTE_EXT", "CORTE_INT"} <= layers
    assert len(doc.modelspace().query('LWPOLYLINE[layer=="CORTE_EXT"]')) == 5


def test_confirm_creates_and_uses_remnants(client):
    pid = _upload(client)
    # retal en almacén del mismo material/espesor
    client.post("/retales", data={"material_id": "1", "thickness_mm": "4", "width_mm": "900", "height_mm": "600",
                                  "location": "E-1"})
    assert "R-0001" in client.get("/retales").text
    r = client.post("/anidado", data={"material_id": "1", "thickness_mm": "4", "format_id": "1",
                                      f"qty-{pid}": "2", "use_remnants": "on"}, follow_redirects=False)
    nid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/anidado/{nid}").text
    assert "R-0001" in page and "Guardar sobrante" in page
    client.post(f"/anidado/{nid}/confirmar", data={"offcut-0": "on", "location": "E-2"})
    from app import main
    from app.models import Nest, Remnant
    with main.SessionLocal() as db:
        assert db.get(Nest, nid).status == "confirmado"
        rems = db.query(Remnant).order_by(Remnant.id).all()
        assert rems[0].status == "usado" and rems[0].used_in_nest_id == nid
        assert rems[1].source_nest_id == nid and rems[1].location == "E-2" and rems[1].status == "disponible"
    # confirmado = solo lectura
    assert client.post(f"/anidado/{nid}/guardar", json={"sheets": []}).status_code == 409
    client.post(f"/anidado/{nid}/borrador")
    with main.SessionLocal() as db:
        assert db.get(Remnant, 1).status == "disponible"


def test_settings_and_formats(client):
    r = client.post("/anidado/ajustes", data={"gap_table": "2 3 6\n12 10 12", "extra_gap_mm": "1",
                                               "extra_gap_from_mm": "6", "remnant_min_mm": "250",
                                               "fam-inox": "on", "allow_rotation": "on"}, follow_redirects=False)
    assert r.headers["location"].endswith("ok=1")
    page = client.get("/anidado/ajustes").text
    assert "2 3 6" in page
    r = client.post("/anidado/ajustes", data={"gap_table": "dos tres", "extra_gap_mm": "1",
                                               "extra_gap_from_mm": "6", "remnant_min_mm": "250"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    client.post("/anidado/formatos", data={"name": "Grande", "width_mm": "2000", "height_mm": "4000"})
    assert "4000 × 2000 mm" in client.get("/anidado/ajustes").text


def test_request_nest_drives_material_cost(anon):
    assert _submit(anon).status_code == 200
    _login(anon)
    from app import main
    from app.models import QuoteRequest
    with main.SessionLocal() as db:
        rid, number = db.query(QuoteRequest.id, QuoteRequest.number).one()
    anon.get(f"/peticiones/{rid}")   # cálculo estimado
    r = anon.post(f"/anidado/desde-peticion/{rid}", follow_redirects=False)
    assert r.status_code == 303 and re.search(r"/anidado/\d+$", r.headers["location"])
    from app.models import Nest
    with main.SessionLocal() as db:
        nests = db.query(Nest).filter_by(request_id=rid).all()
        assert len(nests) == 2   # dos combinaciones material/espesor
        req = db.get(QuoteRequest, rid)
        steel = next(i for i in req.items if i.supply == "nosotros")
        assert steel.calc_snapshot["material_source"] == "anidado"
    assert "material según anidado" in anon.get(f"/peticiones/{rid}").text
    # borrar la petición (RGPD) arrastra sus anidados
    assert anon.post(f"/peticiones/{rid}/eliminar", data={"confirm": number}, follow_redirects=False).status_code == 303
    with main.SessionLocal() as db:
        assert db.get(QuoteRequest, rid) is None
        assert db.query(Nest).filter_by(request_id=rid).count() == 0


def test_nesting_requires_login(anon):
    anon.post("/logout")
    assert anon.get("/anidado", follow_redirects=False).status_code == 303
    assert anon.get("/retales", follow_redirects=False).status_code == 303
    assert anon.post("/anidado/1/comprobar", json={"sheets": []}).status_code == 401
