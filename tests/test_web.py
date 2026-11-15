from tests.conftest import FIXTURES


def test_seeded_example_machine(client):
    r = client.get("/maquinas")
    assert r.status_code == 200 and "Fibra 6 kW" in r.text
    assert "Content-Security-Policy" in r.headers


def _machine_form(name="Fibra 3 kW pruebas", **extra):
    data = {"name": name, "laser_type": "fibra", "laser_power_kw": "3", "table_x_mm": "3000", "table_y_mm": "1500",
            "max_thickness_mm": "10", "rapid_speed_m_min": "100", "acceleration_g": "1", "positioning_accuracy_mm": "0.05",
            "speed_efficiency": "0.85", "small_contour_mm": "100", "small_contour_factor": "0.5", "head_overhead_s": "0.5",
            "sheet_change_min": "2", "hourly_rate_eur": "70", "power_consumption_kw": "18", "setup_cost_eur": "20",
            "active": "on",
            "p-0-material_family": "acero", "p-0-gas": "O2", "p-0-thickness_mm": "5", "p-0-cut_speed_m_min": "2,5",
            "p-0-pierce_time_s": "0.6", "p-0-gas_flow_m3_h": "2", "p-0-kerf_mm": "0.3"}
    data.update(extra)
    return data


def test_machine_crud(client):
    r = client.post("/maquinas/nueva", data=_machine_form(), follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    detail = client.get(url)
    assert "Fibra 3 kW pruebas" in detail.text and "2,50 m/min" in detail.text

    edit = _machine_form(hourly_rate_eur="75", **{"p-1-material_family": "inox", "p-1-gas": "N2",
                                                  "p-1-thickness_mm": "2", "p-1-cut_speed_m_min": "12",
                                                  "p-1-pierce_time_s": "0.2", "p-1-gas_flow_m3_h": "20",
                                                  "p-1-kerf_mm": "0.15"})
    assert client.post(url + "/editar", data=edit, follow_redirects=False).status_code == 303
    assert "75,00" in client.get(url).text

    assert client.post(url + "/estado", follow_redirects=False).status_code == 303   # baja
    assert "De baja" in client.get(url).text

    assert client.post(url + "/eliminar", data={"confirm_name": "otro"}).status_code == 400
    assert client.post(url + "/eliminar", data={"confirm_name": "Fibra 3 kW pruebas"},
                       follow_redirects=False).status_code == 303
    assert client.get(url).status_code == 404


def test_machine_validation(client):
    bad = _machine_form(name="Fibra 6 kW · Ejemplo", laser_power_kw="-1", **{"p-0-thickness_mm": "25"})
    r = client.post("/maquinas/nueva", data=bad)
    assert r.status_code == 422
    assert "Ya existe" in r.text and "Máximo 12.0" in r.text


def test_upload_and_quote(client):
    with open(FIXTURES / "placa_taladros.dxf", "rb") as fh:
        r = client.post("/piezas", files={"file": ("placa.dxf", fh, "application/dxf")}, follow_redirects=False)
    assert r.status_code == 303
    part_url = r.headers["location"]
    page = client.get(part_url)
    assert "<svg" in page.text and "Longitud de corte" in page.text
    q = client.post(part_url + "/calcular", data={"machine_id": 1, "material_id": 1, "gas": "O2",
                                                  "thickness_mm": "6", "quantity": "10"})
    assert q.status_code == 200 and "Desglose del pedido" in q.text
    bad = client.post(part_url + "/calcular", data={"machine_id": 1, "material_id": 1, "gas": "O2",
                                                    "thickness_mm": "15", "quantity": "1"})
    assert bad.status_code == 422


def test_upload_rejects_fake_dxf(client):
    r = client.post("/piezas", files={"file": ("malo.dxf", b"<html>no</html>", "application/dxf")})
    assert r.status_code == 422 and "no corresponde" in r.text
    r = client.post("/piezas", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 422


def test_cross_origin_post_blocked(client):
    r = client.post("/maquinas/1/estado", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_settings_discounts(client):
    page = client.get("/parametros")
    assert "10: 3" in page.text
    form = {"electricity_eur_kwh": "0.2", "gas_o2_eur_m3": "0.3", "gas_n2_eur_m3": "0.2", "gas_air_eur_m3": "0.02",
            "margin_pct": "30", "min_order_eur": "50", "sheet_x_mm": "3000", "sheet_y_mm": "1500", "part_gap_mm": "5",
            "sheet_edge_mm": "10", "scrap_recovery_pct": "20", "quantity_discounts": "20: 5\nmal"}
    assert client.post("/parametros", data=form).status_code == 422
    form["quantity_discounts"] = "20: 5\n100: 12"
    assert client.post("/parametros", data=form, follow_redirects=False).status_code == 303
    assert "100: 12" in client.get("/parametros").text
