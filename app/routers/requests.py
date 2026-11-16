"""Bandeja interna de peticiones: calcular, ajustar precios, generar y enviar la oferta."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..mailer import mail_enabled, send_mail
from ..models import REQUEST_STATUSES, SUPPLY_OPTIONS, Machine, Nest, QuoteRequest, Remnant
from ..quotes.service import build_pdf, company, customer_url, new_token, offer_number, price_item
from ..web import templates

router = APIRouter(prefix="/peticiones", tags=["peticiones"])
EDITABLE = ("recibida", "en_estudio", "cambios")


def _get(db: Session, req_id: int) -> QuoteRequest:
    req = db.get(QuoteRequest, req_id)
    if not req:
        raise HTTPException(404, "Petición no encontrada")
    if req.is_expired:
        req.status = "caducada"
        db.commit()
    return req


def _machines(db: Session):
    return db.scalars(select(Machine).where(Machine.active.is_(True)).order_by(Machine.name)).all()


@router.get("")
def inbox(request: Request, estado: str = "", db: Session = Depends(get_db)):
    q = select(QuoteRequest).order_by(QuoteRequest.created_at.desc())
    if estado in REQUEST_STATUSES:
        q = q.where(QuoteRequest.status == estado)
    reqs = db.scalars(q.limit(300)).all()
    counts = dict(db.execute(select(QuoteRequest.status, func.count()).group_by(QuoteRequest.status)).all())
    return templates.TemplateResponse(request, "requests/list.html", {
        "reqs": reqs, "estado": estado, "statuses": REQUEST_STATUSES, "counts": counts})


@router.get("/{req_id}")
def detail(req_id: int, request: Request, db: Session = Depends(get_db), enviado: str = ""):
    req = _get(db, req_id)
    if any(i.calc_unit_price is None and not i.calc_error for i in req.items) and req.status in EDITABLE:
        for it in req.items:
            if it.calc_unit_price is None:
                price_item(db, it)
        db.commit()
    options = {m.id: {fam: sorted({p.gas for p in m.cut_params if p.material_family == fam})
                      for fam in {p.material_family for p in m.cut_params}} for m in _machines(db)}
    return templates.TemplateResponse(request, "requests/detail.html", {
        "req": req, "statuses": REQUEST_STATUSES, "supply": SUPPLY_OPTIONS, "machines": _machines(db),
        "options": options, "editable": req.status in EDITABLE, "mail_on": mail_enabled(),
        "sent_link": request.cookies.get(f"sent_{req.id}") if enviado else None, "enviado": enviado})


async def _apply_form(req: QuoteRequest, request: Request, db: Session, recalc: bool) -> list[str]:
    form = await request.form()
    errors = []
    machines = {m.id: m for m in _machines(db)}
    for it in req.items:
        mid, gas = form.get(f"i{it.id}-machine"), str(form.get(f"i{it.id}-gas", ""))
        machine = machines.get(int(mid)) if mid and str(mid).isdigit() else None
        if not recalc:
            raw = str(form.get(f"i{it.id}-price", "")).strip().replace(",", ".")
            if raw:
                try:
                    v = round(float(raw), 2)
                    if v < 0 or v > 1_000_000:
                        raise ValueError
                    it.unit_price = v
                except ValueError:
                    errors.append(f"Precio no válido en la línea {it.position}.")
        if recalc or (machine and (machine.id != it.machine_id or gas != it.gas)):
            price_item(db, it, machine, gas, keep_manual_price=not recalc)
    for k in ("lead_time", "delivery", "payment"):
        setattr(req, k, str(form.get(k, getattr(req, k))).strip()[:200])
    req.offer_notes = str(form.get("offer_notes", req.offer_notes)).strip()[:2000]
    req.internal_notes = str(form.get("internal_notes", req.internal_notes)).strip()[:4000]
    try:
        req.valid_days = max(1, min(365, int(form.get("valid_days", req.valid_days))))
    except ValueError:
        errors.append("Validez no válida.")
    if req.status == "recibida":
        req.status = "en_estudio"
    return errors


@router.post("/{req_id}/guardar")
async def save(req_id: int, request: Request, db: Session = Depends(get_db)):
    req = _get(db, req_id)
    if req.status not in EDITABLE:
        raise HTTPException(400, "La oferta ya está enviada; cámbiala a «En estudio» para editarla.")
    form = await request.form()
    recalc = form.get("accion") == "recalcular"
    await _apply_form(req, request, db, recalc)
    db.commit()
    return RedirectResponse(f"/peticiones/{req.id}", status_code=303)


@router.get("/{req_id}/oferta.pdf")
def pdf_preview(req_id: int, db: Session = Depends(get_db)):
    req = _get(db, req_id)
    return Response(build_pdf(db, req), media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="Oferta_{req.offer_number or req.number + "_borrador"}.pdf"',
        "Cache-Control": "no-store"})


@router.post("/{req_id}/enviar")
def send_offer(req_id: int, request: Request, db: Session = Depends(get_db)):
    req = _get(db, req_id)
    if req.status not in EDITABLE:
        raise HTTPException(400, "Esta petición no está en un estado que permita enviar oferta.")
    missing = [it.position for it in req.items if it.unit_price is None]
    if missing:
        raise HTTPException(400, f"Faltan precios en las líneas {missing}.")
    req.offer_number = req.offer_number or offer_number(req)
    req.offer_issued_at = datetime.now(timezone.utc)
    req.status = "enviada"
    req.customer_response, req.responded_at = "", None
    # Se genera un enlace nuevo para el cliente (solo se guarda su hash): se muestra una vez y va en el correo/PDF
    token, th = new_token()
    req.access_token_hash = th
    link = customer_url(token)
    db.commit()
    prof = company(db)
    sent = send_mail(req.email, f"Oferta {req.offer_number} · {prof.name}",
                     f"Hola {req.contact}:\n\nAdjuntamos la oferta {req.offer_number} correspondiente a su petición "
                     f"{req.number}.\n\nPuede aceptarla, pedir cambios o rechazarla en este enlace privado:\n{link}\n\n"
                     f"La oferta es válida durante {req.valid_days} días.\n\nUn saludo,\n{prof.name}",
                     [(f"Oferta_{req.offer_number}.pdf", build_pdf(db, req, link), "application/pdf")])
    if sent:
        req.email_sent_at = datetime.now(timezone.utc)
        db.commit()
    resp = RedirectResponse(f"/peticiones/{req.id}?enviado={'correo' if sent else 'manual'}", status_code=303)
    # el enlace se muestra una sola vez al usuario interno (cookie corta, solo HTTP, solo esta ruta)
    resp.set_cookie(f"sent_{req.id}", link, max_age=120, httponly=True, samesite="strict", path=f"/peticiones/{req.id}")
    return resp


@router.post("/{req_id}/estado")
async def change_status(req_id: int, request: Request, db: Session = Depends(get_db)):
    req = _get(db, req_id)
    new = (await request.form()).get("status")
    if new not in REQUEST_STATUSES:
        raise HTTPException(400, "Estado no válido.")
    req.status = new
    db.commit()
    return RedirectResponse(f"/peticiones/{req.id}", status_code=303)


@router.post("/{req_id}/eliminar")
async def delete_request(req_id: int, request: Request, db: Session = Depends(get_db)):
    """Supresión completa (derecho de supresión RGPD): petición, líneas y piezas analizadas."""
    req = _get(db, req_id)
    if (await request.form()).get("confirm") != req.number:
        raise HTTPException(400, "Escribe el número de la petición para confirmar.")
    parts = [it.part for it in req.items]
    # anidados de la petición (contienen la disposición de sus piezas); sus retales usados vuelven al almacén
    for n in db.scalars(select(Nest).where(Nest.request_id == req.id)):
        for r in db.scalars(select(Remnant).where(Remnant.used_in_nest_id == n.id)):
            r.status, r.used_in_nest_id = "disponible", None
        db.delete(n)
    db.flush()
    db.delete(req)
    db.flush()
    for p in parts:
        db.delete(p)
    db.commit()
    return RedirectResponse("/peticiones", status_code=303)
