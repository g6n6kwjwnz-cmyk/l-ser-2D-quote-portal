"""Flujo completo: petición pública → cálculo interno → oferta → respuesta del cliente."""
import re

import pytest

from tests.conftest import FIXTURES


def _submit(client, **extra):
    data = {"company": "Metalistería Prueba SL", "contact": "Ana", "email": "ana@cliente.es", "consent": "on",
            "item-0-material": "1", "item-0-thickness": "6", "item-0-quantity": "20", "item-0-supply": "nosotros",
            "item-1-material": "4", "item-1-thickness": "3", "item-1-quantity": "5", "item-1-supply": "cliente"}
    data.update(extra)
    files = [("item-0-file", ("placa.dxf", (FIXTURES / "placa_taladros.dxf").read_bytes(), "application/dxf")),
             ("item-1-file", ("brida.dxf", (FIXTURES / "brida_pulgadas.dxf").read_bytes(), "application/dxf"))]
    return client.post("/solicitar", data=data, files=files)


@pytest.fixture()
def anon(client):
    client.post("/logout")
    from app.routers import public
    public._hits.clear()
    return client


def _login(client):
    from tests.conftest import TEST_PASSWORD, TEST_USER
    client.post("/login", data={"email": TEST_USER, "password": TEST_PASSWORD})


def test_public_form_is_open_without_login(anon):
    r = anon.get("/solicitar")
    assert r.status_code == 200 and "Pedir oferta" in r.text
    assert "Fibra 6 kW" not in r.text and "€/kg" not in r.text   # nada de máquinas ni precios


def test_validation_and_consent(anon):
    r = _submit(anon, consent="", email="malo")
    assert r.status_code == 422 and "privacidad" in r.text and "Correo no válido" in r.text
    bad = anon.post("/solicitar", data={"company": "X", "contact": "Y", "email": "a@b.es", "consent": "on",
                                        "item-0-material": "1", "item-0-thickness": "6", "item-0-quantity": "1"},
                    files=[("item-0-file", ("x.dxf", b"no es un dxf", "application/dxf"))])
    assert bad.status_code == 422 and "no corresponde" in bad.text


def test_honeypot_and_rate_limit(anon, monkeypatch):
    r = _submit(anon, website="http://spam")
    assert r.status_code == 200 and "Petición recibida" in r.text
    from app.config import settings
    monkeypatch.setattr(settings, "requests_per_ip_hour", 1)
    assert _submit(anon).status_code == 200
    assert _submit(anon).status_code == 429


def test_full_flow(anon):
    r = _submit(anon)
    assert r.status_code == 200 and "P-" in r.text
    link = re.search(r'href="(http[^"]+/solicitud/[^"]+)"', r.text).group(1)
    path = link.split("8000", 1)[1] if "8000" in link else link
    status = anon.get(path)
    assert status.status_code == 200 and "Estamos preparando" in status.text
    assert anon.get(path + "/oferta.pdf").status_code == 404        # aún no hay oferta

    _login(anon)
    inbox = anon.get("/peticiones")
    assert "Metalistería Prueba SL" in inbox.text
    req_id = re.search(r'href="/peticiones/(\d+)"', inbox.text).group(1)
    det = anon.get(f"/peticiones/{req_id}")
    assert "Desglose interno" in det.text and "material del cliente" in det.text
    # precio manual en la línea 1
    item_ids = re.findall(r'name="i(\d+)-price"', det.text)
    form = {f"i{item_ids[0]}-price": "12,50", "accion": "guardar", "valid_days": "15", "lead_time": "5 días",
            "delivery": "Recogida", "payment": "Contado", "offer_notes": "Precio especial", "internal_notes": "ojo"}
    assert anon.post(f"/peticiones/{req_id}/guardar", data=form, follow_redirects=False).status_code == 303
    det = anon.get(f"/peticiones/{req_id}")
    assert 'value="12.50"' in det.text and "En estudio" in det.text
    assert anon.get(f"/peticiones/{req_id}/oferta.pdf").content.startswith(b"%PDF")

    sent = anon.post(f"/peticiones/{req_id}/enviar", follow_redirects=True)
    assert "Oferta O-" in sent.text and "/solicitud/" in sent.text
    new_link = re.search(r'<span class="mono">([^<]+)</span>', sent.text).group(1)
    new_path = new_link.split("8000", 1)[1]

    anon.post("/logout")
    assert anon.get(path).status_code == 404                         # el enlace antiguo deja de valer
    page = anon.get(new_path)
    assert page.status_code == 200 and "12,50 €" in page.text
    for secret in ("Desglose", "Fibra 6 kW", "margen", "ojo", "€/h"):
        assert secret not in page.text
    pdf = anon.get(new_path + "/oferta.pdf")
    assert pdf.content.startswith(b"%PDF")
    r = anon.post(new_path + "/responder", data={"action": "aceptar", "comment": "Pedido 123"}, follow_redirects=True)
    assert "Aceptada" in r.text and "Pedido 123" in r.text
    assert anon.post(new_path + "/responder", data={"action": "rechazar"}).status_code == 400

    _login(anon)
    assert "Pedido 123" in anon.get(f"/peticiones/{req_id}").text


def test_rgpd_delete(anon):
    _submit(anon)
    _login(anon)
    req_id = re.search(r'href="/peticiones/(\d+)"', anon.get("/peticiones").text).group(1)
    number = re.search(r"(P-\d{4}-\d{4})", anon.get(f"/peticiones/{req_id}").text).group(1)
    assert anon.post(f"/peticiones/{req_id}/eliminar", data={"confirm": "x"}).status_code == 400
    assert anon.post(f"/peticiones/{req_id}/eliminar", data={"confirm": number}, follow_redirects=False).status_code == 303
    assert anon.get(f"/peticiones/{req_id}").status_code == 404


def test_internal_pages_still_protected(anon):
    assert anon.get("/peticiones", follow_redirects=False).status_code == 303
    assert anon.get("/empresa", follow_redirects=False).status_code == 303


def test_company_profile(client):
    page = client.get("/empresa")
    assert "Razón social" in page.text
    data = {"name": "Mi Taller SL", "tax_id": "B123", "address": "C/ Uno", "valid_days": "20", "lead_time": "5 días",
            "delivery": "Portes pagados", "payment": "Contado", "vat_pct": "21", "response_days": "3"}
    assert client.post("/empresa", data=data, follow_redirects=False).status_code == 303
    assert "Mi Taller SL" in client.get("/solicitar").text


def test_offer_email_with_pdf(anon, monkeypatch):
    sent = []
    from app.routers import public, requests
    fake = lambda to, subject, body, attachments=None: sent.append((to, subject, body, attachments)) or True  # noqa: E731
    monkeypatch.setattr(public, "send_mail", fake)
    monkeypatch.setattr(requests, "send_mail", fake)
    _submit(anon)
    assert sent[0][0] == "ana@cliente.es" and "/solicitud/" in sent[0][2]
    _login(anon)
    req_id = re.search(r'href="/peticiones/(\d+)"', anon.get("/peticiones").text).group(1)
    anon.get(f"/peticiones/{req_id}")
    r = anon.post(f"/peticiones/{req_id}/enviar", follow_redirects=True)
    assert "Enviada por correo" in r.text
    to, subject, body, att = sent[-1]
    assert subject.startswith("Oferta O-") and att[0][1].startswith(b"%PDF") and "/solicitud/" in body
