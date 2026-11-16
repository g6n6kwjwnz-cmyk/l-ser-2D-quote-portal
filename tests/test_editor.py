"""Editor de plano: validación, recálculo, fabricabilidad, guardado, restauración y bloqueo por oferta enviada."""
import math

import pytest

from app.geometry import editing
from tests.conftest import FIXTURES


def _upload(client):
    with open(FIXTURES / "placa_taladros.dxf", "rb") as fh:
        r = client.post("/piezas", files={"file": ("placa.dxf", fh, "application/dxf")}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def _square(x, y, s, closed=True):
    pts = [[x, y], [x + s, y], [x + s, y + s], [x, y + s]]
    return {"points": pts + ([pts[0]] if closed else []), "closed": closed}


def _circle(cx, cy, r, n=64):
    pts = [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)] for i in range(n)]
    return {"points": pts + [pts[0]], "closed": True}


# ---------------------------------------------------------------- unidades

def test_parse_rejects_bad_input():
    for bad in [None, [], {"a": 1}, [{"points": [[0, 0], [1, "x"]], "closed": False}],
                [{"points": [[0, 0], [float("inf"), 1]], "closed": False}],
                [{"points": [[0, 0], [1e9, 1]], "closed": False}],
                [{"points": [[0, 0], [True, 1]], "closed": False}],
                [{"points": [[0, 0]] * 3, "closed": "si"}]]:
        with pytest.raises(editing.EditError):
            editing.parse_contours(bad)
    with pytest.raises(editing.EditError):
        editing.parse_contours([_square(0, 0, 1)] * (editing.MAX_CONTOURS + 1))


def test_parse_normalizes_closed():
    polys = editing.parse_contours([{"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "closed": True},
                                    {"points": [[0, 0], [5, 0]], "closed": True}])
    assert polys[0][0][0] == polys[0][0][-1] and polys[0][1] is True
    assert polys[1][1] is False      # dos puntos no forman un contorno cerrado


def test_classify_and_rules():
    polys = editing.parse_contours([_square(0, 0, 100), _circle(50, 50, 1), _circle(20, 20, 4),
                                    _square(95.5, 40, 3), {"points": [[10, 90], [30, 90]], "closed": False}])
    kinds = editing.classify(polys)
    assert kinds == ["exterior", "interior", "interior", "interior", "abierto"]
    issues = editing.check_manufacturability(polys, kinds, editing.Rules(thickness=5, family="inox"))
    codes = {(i["code"], i["severity"]) for i in issues}
    assert ("agujero", "error") in codes          # Ø2 en 5 mm
    assert ("agujero", "aviso") in codes          # cuadrado de 3 mm < 7,5 mm recomendado en inox
    assert ("puente", "error") in codes           # cuadrado a 1,5 mm del borde
    assert ("abierto", "aviso") in codes
    assert issues[0]["severity"] == "error"


def test_crossing_contours_detected():
    polys = editing.parse_contours([_square(0, 0, 100), _square(90, 10, 20)])
    kinds = editing.classify(polys)
    issues = editing.check_manufacturability(polys, kinds, editing.Rules(thickness=2))
    assert any(i["code"] == "cruce" for i in issues)


# ---------------------------------------------------------------- web

def test_editor_page_and_geometry(client):
    pid = _upload(client)
    r = client.get(f"/piezas/{pid}/editor")
    assert r.status_code == 200 and 'id="ed-app"' in r.text and "/static/editor.js?v=" in r.text
    assert f'href="/piezas/{pid}/editor"' in client.get(f"/piezas/{pid}").text
    g = client.get(f"/piezas/{pid}/geometria.json").json()
    assert len(g["contours"]) == 6
    assert client.get("/static/editor.js").status_code == 200


def test_analyze_endpoint(client):
    pid = _upload(client)
    contours = client.get(f"/piezas/{pid}/geometria.json").json()["contours"]
    r = client.post(f"/piezas/{pid}/geometria/analizar",
                    json={"contours": contours, "thickness_mm": "12", "family": "acero"})
    assert r.status_code == 200
    data = r.json()
    assert data["metrics"]["pierce_count"] == 6 and len(data["kinds"]) == 6
    assert any(i["code"] == "agujero" for i in data["issues"])     # Ø10 con 12 mm
    # sin espesor válido no hay comprobaciones, pero sí métricas
    r = client.post(f"/piezas/{pid}/geometria/analizar", json={"contours": contours, "thickness_mm": "99"})
    assert r.status_code == 200 and r.json()["issues"] == []


def test_json_only_and_size_limit(client):
    pid = _upload(client)
    r = client.post(f"/piezas/{pid}/geometria", data={"contours": "x"})
    assert r.status_code == 422
    r = client.post(f"/piezas/{pid}/geometria", content=b"{", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    r = client.post(f"/piezas/{pid}/geometria", json={"contours": []})
    assert r.status_code == 422 and "sin contornos" in r.json()["error"]
    r = client.post(f"/piezas/{pid}/geometria", json={"contours": [_square(0, 0, 10)]},
                    headers={"Origin": "https://malicioso.example"})
    assert r.status_code == 403


def test_save_edit_and_restore(client):
    pid = _upload(client)
    contours = client.get(f"/piezas/{pid}/geometria.json").json()["contours"]
    before = client.get(f"/piezas/{pid}").text
    # borrar un taladro y añadir otro cuadrado
    edited = [{"points": c["points"], "closed": c["closed"]} for c in contours[:-1]] + [_square(50, 20, 10)]
    r = client.post(f"/piezas/{pid}/geometria", json={"contours": edited})
    assert r.status_code == 200 and r.json()["ok"]
    page = client.get(f"/piezas/{pid}").text
    assert "Plano editado" in page and "Plano editado" not in before
    ed = client.get(f"/piezas/{pid}/editor").text
    assert "test@example.com" in ed and "Restaurar original" in ed
    r = client.post(f"/piezas/{pid}/geometria/restaurar", follow_redirects=False)
    assert r.status_code == 303
    g = client.get(f"/piezas/{pid}/geometria.json").json()["contours"]
    assert len(g) == len(contours)
    assert "restaurar original" in client.get(f"/piezas/{pid}/editor").text


def test_save_as_new(client):
    pid = _upload(client)
    contours = client.get(f"/piezas/{pid}/geometria.json").json()["contours"]
    r = client.post(f"/piezas/{pid}/geometria", json={"contours": contours[:1], "as_new": True})
    assert r.status_code == 200
    url = r.json()["redirect"]
    new_id = url.split("/")[2]
    assert new_id != pid
    assert len(client.get(f"/piezas/{new_id}/geometria.json").json()["contours"]) == 1
    assert len(client.get(f"/piezas/{pid}/geometria.json").json()["contours"]) == 6
    assert "(editada)" in client.get(f"/piezas/{new_id}").text


def test_locked_when_offer_sent(client):
    from datetime import datetime, timezone
    from app import main
    from app.models import Material, QuoteRequest, RequestItem
    pid = _upload(client)
    with main.SessionLocal() as db:
        mat = db.query(Material).first()
        req = QuoteRequest(number="P-TEST-1", status="enviada", access_token_hash="h" * 64, company="ACME",
                           contact="Ana", email="ana@example.com", consent_at=datetime.now(timezone.utc))
        req.items.append(RequestItem(part_id=pid, material_id=mat.id, thickness_mm=3, quantity=1))
        db.add(req)
        db.commit()
    contours = client.get(f"/piezas/{pid}/geometria.json").json()["contours"]
    r = client.post(f"/piezas/{pid}/geometria", json={"contours": contours[:1]})
    assert r.status_code == 409 and "P-TEST-1" in r.json()["error"]
    page = client.get(f"/piezas/{pid}/editor").text
    assert "pieza nueva" in page and 'data-act="save"' not in page
    assert client.post(f"/piezas/{pid}/geometria", json={"contours": contours[:1], "as_new": True}).status_code == 200


def test_editor_requires_login(client):
    pid = _upload(client)
    client.cookies.clear()
    assert client.post(f"/piezas/{pid}/geometria/analizar", json={"contours": []}).status_code == 401
    r = client.get(f"/piezas/{pid}/editor", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
