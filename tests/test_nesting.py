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


# ------------------------------------------------------------------ optimizador y editor ampliado
def test_optimize_engine_valid_and_not_worse():
    from app.nesting.engine import optimize, score
    items = [NestItem("a", box(0, 0, 300, 120), 10), NestItem("b", Polygon([(0, 0), (200, 0), (0, 200)]), 12)]
    base = nest(items, 1500, 1000, 5, 8)
    shapes = {"a": items[0].shape, "b": items[1].shape}
    alts = optimize(items, [(1500, 1000), (1000, 500)], 5, 8, (0, 90, 180, 270), time_budget=10, workers=1)
    best = alts[0]
    assert best["score"]["consumed_mm2"] <= score(base, shapes, 300)["consumed_mm2"] + 1
    for pls, src in zip(best["result"].sheets, best["result"].sheet_sources):
        assert check_layout(shapes, [p.to_dict() for p in pls], src["w"], src["h"], 5, 8) == []


def test_compact_slides_left_without_conflicts():
    from app.nesting.engine import Placement, compact
    shapes = {"a": box(0, 0, 100, 100)}
    pls = [Placement("a", 400, 300, 0), Placement("a", 600, 50, 0)]
    out = compact(shapes, pls, 1000, 500, 5, 10)
    assert all(p.x < 200 for p in out)
    assert check_layout(shapes, [p.to_dict() for p in out], 1000, 500, 5, 10) == []


def _make_nest(client, qty=8):
    pid = _upload(client)
    r = client.post("/anidado", data={"material_id": "1", "thickness_mm": "4", "format_id": "1",
                                      f"qty-{pid}": str(qty)}, follow_redirects=False)
    nid = int(r.headers["location"].rsplit("/", 1)[1])
    from app import main
    from app.models import Nest
    with main.SessionLocal() as db:
        return pid, nid, db.get(Nest, nid).sheets


def test_optimize_endpoint_and_alternatives(client):
    pid, nid, _ = _make_nest(client)
    r = client.post(f"/anidado/{nid}/optimizar", data={"priority": "material", "budget": "10",
                                                       "rotation_step": "45", "formats": "todos"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("optimizado")
    page = client.get(f"/anidado/{nid}").text
    assert "Propuestas" in page and "En uso" in page
    from app import main
    from app.models import Nest
    with main.SessionLocal() as db:
        n = db.get(Nest, nid)
        assert n.conflicts == 0 and n.options["rotation_step"] == 45 and n.options["alternatives"]
        alts = len(n.options["alternatives"])
    if alts > 1:
        assert client.post(f"/anidado/{nid}/propuesta/1", follow_redirects=False).status_code == 303
    assert client.post(f"/anidado/{nid}/propuesta/9", follow_redirects=False).status_code == 422


def test_editor_operations(client):
    pid, nid, sheets = _make_nest(client, qty=6)
    # quitar dos piezas y moverlas lejos; compactar; colocar pendientes
    sh = [{"source": sheets[0]["source"], "placements": [dict(p) for p in sheets[0]["placements"][:4]]}]
    sh[0]["placements"][0].update(x=1500, y=700)
    r = client.post(f"/anidado/{nid}/operacion", json={"op": "compactar", "sheet": 0, "sheets": sh}).json()
    assert r["eval"]["conflicts"] == 0 and r["sheets"][0]["placements"][0]["x"] < 1500
    r = client.post(f"/anidado/{nid}/operacion", json={"op": "pendientes", "sheets": r["sheets"]}).json()
    assert r["eval"]["pending"] == {} and r["eval"]["conflicts"] == 0
    assert r["eval"]["sheets"][0]["plan"]["time_s"] > 0 and r["eval"]["time_s"] > 0
    # bloquear una pieza y recolocar: la bloqueada no se mueve
    s2 = r["sheets"]
    s2[0]["placements"][0].update(x=900, y=600, locked=True)
    r = client.post(f"/anidado/{nid}/operacion", json={"op": "recolocar", "sheet": 0, "sheets": s2}).json()
    locked = [p for p in r["sheets"][0]["placements"] if p.get("locked")]
    assert locked and (locked[0]["x"], locked[0]["y"]) == (900, 600) and r["eval"]["conflicts"] == 0
    # imán sobre una selección
    r2 = client.post(f"/anidado/{nid}/operacion", json={"op": "iman", "sheet": 0, "selected": [], "sheets": r["sheets"]})
    assert r2.status_code == 422
    assert client.post(f"/anidado/{nid}/operacion", json={"op": "borrar_todo", "sheets": r["sheets"]}).status_code == 422
    # giro libre (37°) guardado y validado
    s3 = r["sheets"]
    s3[0]["placements"][1]["rotation"] = 37
    ev = client.post(f"/anidado/{nid}/comprobar", json={"sheets": s3})
    assert ev.status_code == 200


def test_grain_restriction(client):
    pid, nid, sheets = _make_nest(client, qty=4)
    client.post(f"/anidado/{nid}/piezas", data={f"qty-{pid}": "4", f"giro-{pid}": "fijo"})
    bad = [{"source": sheets[0]["source"], "placements": [dict(sheets[0]["placements"][0], rotation=90)]}]
    r = client.post(f"/anidado/{nid}/comprobar", json={"sheets": bad})
    assert r.status_code == 422 and "girar" in r.json()["error"]
    client.post(f"/anidado/{nid}/auto")
    from app import main
    from app.models import Nest
    with main.SessionLocal() as db:
        n = db.get(Nest, nid)
        assert all(p["rotation"] == 0 for s in n.sheets for p in s["placements"])
        assert n.items[0]["giro"] == "fijo"
