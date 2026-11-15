"""Zona pública para clientes: pedir oferta y consultar/aceptar la oferta con un enlace privado.

El cliente nunca ve máquinas, tiempos, costes ni márgenes: solo sus piezas, el estado y, una vez enviada,
la oferta con precios de venta.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..mailer import send_mail
from ..models import SUPPLY_OPTIONS, Material, QuoteRequest, RequestItem
from ..quotes.service import build_pdf, company, customer_url, new_token, request_number, token_hash
from ..web import templates
from .parts import UploadError, part_from_upload

router = APIRouter(tags=["clientes"])

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    return len(q) >= settings.requests_per_ip_hour


def _materials(db: Session):
    return db.scalars(select(Material).where(Material.active.is_(True)).order_by(Material.family, Material.code)).all()


def _form(request: Request, db: Session, values=None, errors=None, item_errors=None, status=200):
    prof = company(db)
    return templates.TemplateResponse(request, "public/request_form.html", {
        "materials": _materials(db), "values": values or {}, "errors": errors or {},
        "item_errors": item_errors or {}, "supply": SUPPLY_OPTIONS, "company": prof,
        "max_items": settings.max_items_per_request, "max_mb": settings.max_upload_mb,
        "min_t": settings.min_thickness_mm, "max_t": settings.max_thickness_mm}, status_code=status)


@router.get("/solicitar")
def request_form(request: Request, db: Session = Depends(get_db)):
    return _form(request, db, values={"items": [{}]})


@router.post("/solicitar")
async def submit_request(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ip = _client_ip(request)
    if form.get("website"):  # campo trampa invisible: solo lo rellenan robots
        return RedirectResponse("/solicitar/gracias", status_code=303)
    if _rate_limited(ip):
        return PlainError("Has enviado demasiadas peticiones. Inténtalo de nuevo dentro de una hora.", 429)

    g = lambda k, n=200: str(form.get(k, "")).strip()[:n]  # noqa: E731
    values = {k: g(k) for k in ("company", "tax_id", "contact", "email", "phone", "address",
                                "customer_reference", "desired_date")}
    values["notes"] = g("notes", 2000)
    errors = {}
    for k, label in (("company", "Empresa"), ("contact", "Persona de contacto"), ("email", "Correo")):
        if not values[k]:
            errors[k] = f"{label}: obligatorio."
    if values["email"] and not EMAIL_RE.match(values["email"]):
        errors["email"] = "Correo no válido."
    if values["desired_date"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", values["desired_date"]):
        errors["desired_date"] = "Fecha no válida."
    if not form.get("consent"):
        errors["consent"] = "Debes aceptar la política de privacidad para enviar la petición."

    # líneas: item-<i>-campo
    idx = sorted({int(k.split("-")[1]) for k in form.keys() if re.fullmatch(r"item-\d+-\w+", k)})
    materials = {m.id: m for m in _materials(db)}
    items, item_errors, item_values = [], {}, []
    for i in idx[: settings.max_items_per_request]:
        f = form.get(f"item-{i}-file")
        raw = {k: str(form.get(f"item-{i}-{k}", "")).strip() for k in ("material", "thickness", "quantity", "supply", "notes")}
        has_file = hasattr(f, "filename") and f.filename
        if not has_file and not any(raw[k] for k in ("thickness", "quantity", "notes")):
            continue  # fila vacía
        item_values.append(raw)
        pos = len(item_values) - 1
        e = {}
        try:
            mat = materials[int(raw["material"])]
        except (ValueError, KeyError):
            mat, e["material"] = None, "Elige un material."
        try:
            t = float(raw["thickness"].replace(",", "."))
            if not settings.min_thickness_mm <= t <= settings.max_thickness_mm:
                raise ValueError
        except ValueError:
            t, e["thickness"] = None, f"Espesor entre {settings.min_thickness_mm:g} y {settings.max_thickness_mm:g} mm."
        try:
            qty = int(raw["quantity"])
            if not 1 <= qty <= 100000:
                raise ValueError
        except ValueError:
            qty, e["quantity"] = None, "Cantidad entre 1 y 100 000."
        supply = raw["supply"] if raw["supply"] in SUPPLY_OPTIONS else "nosotros"
        part = None
        if not has_file:
            e["file"] = "Adjunta el plano DXF o DWG."
        else:
            try:
                part = await part_from_upload(f, "auto")
            except UploadError as exc:
                e["file"] = f"{f.filename}: {exc}"
        if e:
            item_errors[pos] = e
        else:
            items.append(dict(part=part, material=mat, thickness=t, qty=qty, supply=supply, notes=raw["notes"][:500]))
    if not item_values:
        errors["items"] = "Añade al menos una pieza con su plano."
    values["items"] = item_values or [{}]
    if errors or item_errors:
        if item_errors:
            errors["items"] = "Revisa las piezas marcadas. Por seguridad, vuelve a adjuntar los planos."
        return _form(request, db, values, errors, item_errors, status=422)

    _hits[ip].append(time.monotonic())
    token, th = new_token()
    req = QuoteRequest(access_token_hash=th, consent_at=datetime.now(timezone.utc), created_ip=ip,
                       **{k: v for k, v in values.items() if k != "items"})
    prof = company(db)
    req.valid_days, req.lead_time, req.delivery, req.payment = prof.valid_days, prof.lead_time, prof.delivery, prof.payment
    for n, it in enumerate(items, 1):
        db.add(it["part"])
        req.items.append(RequestItem(position=n, part=it["part"], material=it["material"], thickness_mm=it["thickness"],
                                     quantity=it["qty"], supply=it["supply"], notes=it["notes"]))
    db.add(req)
    db.flush()
    req.number = request_number(req)
    db.commit()

    link = customer_url(token)
    send_mail(req.email, f"Hemos recibido su petición {req.number}",
              f"Hola {req.contact}:\n\nHemos recibido su petición de oferta {req.number} con {len(items)} pieza(s).\n"
              f"Le enviaremos la oferta en un plazo aproximado de {prof.response_days} días laborables.\n\n"
              f"Puede consultar el estado en este enlace privado (no lo comparta):\n{link}\n\n{prof.name}")
    if settings.notify_email:
        send_mail(settings.notify_email, f"Nueva petición {req.number} · {req.company}",
                  f"{req.company} ({req.contact}, {req.email}) ha enviado {len(items)} pieza(s).\n"
                  f"{settings.public_base_url.rstrip('/')}/peticiones/{req.id}")
    return templates.TemplateResponse(request, "public/thanks.html", {
        "req": req, "link": link, "company": prof})


@router.get("/solicitar/gracias")
def thanks_bot(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "public/thanks.html", {"req": None, "company": company(db)})


def _by_token(db: Session, token: str) -> QuoteRequest:
    if not token or len(token) > 100:
        raise HTTPException(404)
    req = db.scalar(select(QuoteRequest).where(QuoteRequest.access_token_hash == token_hash(token)))
    if req is None or req.status == "anulada":
        raise HTTPException(404, "Enlace no válido o caducado.")
    if req.is_expired:
        req.status = "caducada"
        db.commit()
    return req


OFFER_VISIBLE = ("enviada", "cambios", "aceptada", "rechazada", "caducada")


@router.get("/solicitud/{token}")
def customer_view(token: str, request: Request, db: Session = Depends(get_db), r: str = ""):
    req = _by_token(db, token)
    return templates.TemplateResponse(request, "public/status.html", {
        "req": req, "token": token, "company": company(db), "show_offer": req.status in OFFER_VISIBLE,
        "responded": r})


@router.get("/solicitud/{token}/oferta.pdf")
def customer_pdf(token: str, db: Session = Depends(get_db)):
    req = _by_token(db, token)
    if req.status not in OFFER_VISIBLE:
        raise HTTPException(404)
    return Response(build_pdf(db, req, customer_url(token)), media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="Oferta_{req.offer_number}.pdf"', "Cache-Control": "no-store"})


@router.post("/solicitud/{token}/responder")
async def customer_respond(token: str, request: Request, db: Session = Depends(get_db)):
    req = _by_token(db, token)
    form = await request.form()
    action = form.get("action")
    if req.status not in ("enviada", "cambios") or action not in ("aceptar", "cambios", "rechazar"):
        raise HTTPException(400, "Esta oferta ya no admite respuesta.")
    req.status = {"aceptar": "aceptada", "cambios": "cambios", "rechazar": "rechazada"}[action]
    req.customer_response = str(form.get("comment", "")).strip()[:2000]
    req.responded_at = datetime.now(timezone.utc)
    db.commit()
    if settings.notify_email:
        send_mail(settings.notify_email, f"Oferta {req.offer_number}: {req.status} por {req.company}",
                  f"{req.company} ha respondido: {req.status}.\nComentario: {req.customer_response or '—'}\n"
                  f"{settings.public_base_url.rstrip('/')}/peticiones/{req.id}")
    return RedirectResponse(f"/solicitud/{token}?r={action}", status_code=303)


class PlainError(Response):
    def __init__(self, msg: str, status: int):
        super().__init__(msg, status_code=status, media_type="text/plain; charset=utf-8")
