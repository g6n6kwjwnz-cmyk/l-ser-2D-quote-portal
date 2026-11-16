"""Lógica de peticiones de clientes: tokens de acceso, numeración, cálculo por línea y documento de oferta."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..costing.engine import CostingError, QuoteInput, compute_quote
from ..models import CompanyProfile, CostSettings, Machine, QuoteRequest, RequestItem
from .pdf import Company, Offer, OfferLine, build_offer_pdf

GAS_PREFERENCE = {
    # acero: oxígeno a partir de 4 mm, nitrógeno/aire en chapa fina (mejor acabado y más rápido)
    "acero": lambda t: ["O2", "N2", "AIRE"] if t >= 4 else ["N2", "AIRE", "O2"],
    "inox": lambda t: ["N2", "AIRE"],
    "aluminio": lambda t: ["N2", "AIRE"],
    "galvanizado": lambda t: ["N2", "AIRE", "O2"],
}


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(24)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def request_number(req: QuoteRequest) -> str:
    return f"P-{req.created_at.year}-{req.id:04d}"


def offer_number(req: QuoteRequest) -> str:
    return f"O-{datetime.now(timezone.utc).year}-{req.id:04d}"


def customer_url(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/solicitud/{token}"


def default_machine_and_gas(db: Session, family: str, thickness: float) -> tuple[Machine | None, str]:
    machines = db.scalars(select(Machine).where(Machine.active.is_(True)).order_by(Machine.id)).all()
    for gas in GAS_PREFERENCE.get(family, lambda t: ["N2", "O2", "AIRE"])(thickness):
        for m in machines:
            if thickness <= m.max_thickness_mm and m.interpolated_param(family, thickness, gas):
                return m, gas
    return (machines[0] if machines else None), ""


def price_item(db: Session, item: RequestItem, machine: Machine | None = None, gas: str | None = None,
               keep_manual_price: bool = True) -> None:
    """Calcula el precio de una línea con el motor de costes. El resultado interno queda en calc_snapshot."""
    fam, t = item.material.family, item.thickness_mm
    if machine is None or not gas:
        dm, dg = default_machine_and_gas(db, fam, t)
        machine, gas = machine or dm, gas or dg
    item.machine_id, item.gas = (machine.id if machine else None), gas
    if machine is None:
        item.calc_error, item.calc_unit_price, item.calc_snapshot = "No hay máquinas activas.", None, {}
        return
    material = item.material
    if item.supply == "cliente":  # material aportado por el cliente: no se cobra la chapa
        material = SimpleNamespace(family=fam, density_kg_dm3=material.density_kg_dm3, price_eur_kg=0.0)
    param = machine.interpolated_param(fam, t, gas)
    qin = QuoteInput(quantity=item.quantity, thickness_mm=t, gas=gas)
    from ..nesting.service import nest_for_item   # import local: evita dependencia circular
    nst = nest_for_item(db, item)
    if nst is not None and item.part.metrics.get("net_area_mm2", 0) > 0:
        # material según el anidado real: chapa consumida (sin los retales que vuelven al almacén) / área de piezas
        qin.nest_charged_area_mm2 = item.part.metrics["net_area_mm2"] * nst.material_factor
        qin.nest_sheets = sum(1 for s in nst.sheets if any(p["key"] == item.part_id for p in s["placements"]))
        qin.nest_utilization_pct = nst.utilization_pct
    try:
        q = compute_quote(item.part.metrics, machine, param, material, db.scalar(select(CostSettings)), qin)
    except CostingError as exc:
        item.calc_error, item.calc_unit_price, item.calc_snapshot = str(exc), None, {}
        return
    item.calc_error = ""
    item.calc_unit_price = round(q.unit_price, 2)
    item.calc_snapshot = q.to_dict()
    if item.unit_price is None or not keep_manual_price:
        item.unit_price = item.calc_unit_price


def company(db: Session) -> CompanyProfile:
    return db.scalar(select(CompanyProfile))


def build_pdf(db: Session, req: QuoteRequest, accept_url: str = "") -> bytes:
    prof = company(db)
    lines = []
    for it in req.items:
        m = it.part.metrics
        notes = []
        if it.supply == "cliente":
            notes.append("Material aportado por el cliente")
        lines.append(OfferLine(
            reference=it.part.filename, description=it.notes or "Pieza según plano",
            material=f"{it.material.code}", thickness_mm=it.thickness_mm, quantity=it.quantity,
            unit_price=round(it.unit_price or 0, 2), width_mm=m["bbox_width_mm"], height_mm=m["bbox_height_mm"],
            contours=m.get("preview", []), notes=" · ".join(notes)))
    issued = (req.offer_issued_at or datetime.now(timezone.utc)).date()
    extra = [c.strip() for c in (prof.extra_conditions or "").splitlines() if c.strip()]
    if req.offer_notes.strip():
        extra.append("<b>Observaciones:</b> " + req.offer_notes.strip().replace("\n", "<br/>"))
    offer = Offer(
        number=req.offer_number or "BORRADOR", request_number=req.number, issued=issued,
        valid_days=req.valid_days, customer_company=req.company,
        customer_contact=req.contact, customer_email=req.email, customer_reference=req.customer_reference,
        lines=lines, lead_time=req.lead_time, delivery=req.delivery, payment=req.payment,
        vat_pct=prof.vat_pct, accept_url=accept_url, notes=extra)
    comp = Company(prof.name, prof.tax_id, prof.address, prof.phone, prof.email, prof.web)
    return build_offer_pdf(offer, comp)
