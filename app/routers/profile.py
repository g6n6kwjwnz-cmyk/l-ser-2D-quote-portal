"""Datos de la empresa y condiciones por defecto de las ofertas."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..quotes.service import company
from ..web import FieldSpec, parse_fields, templates

router = APIRouter(tags=["empresa"])

SECTIONS = [
    ("Datos que aparecen en la oferta", [
        FieldSpec("name", "Razón social", "str"),
        FieldSpec("tax_id", "CIF", "str"),
        FieldSpec("address", "Dirección", "str"),
        FieldSpec("phone", "Teléfono", "str", required=False),
        FieldSpec("email", "Correo", "str", required=False),
        FieldSpec("web", "Web", "str", required=False),
    ]),
    ("Condiciones por defecto", [
        FieldSpec("valid_days", "Validez de la oferta", "int", unit="días", min=1, max=365),
        FieldSpec("lead_time", "Plazo de entrega", "str"),
        FieldSpec("delivery", "Entrega / portes", "str"),
        FieldSpec("payment", "Forma de pago", "str"),
        FieldSpec("vat_pct", "IVA", unit="%", min=0, max=50),
        FieldSpec("response_days", "Plazo de respuesta prometido al cliente", "int", unit="días lab.", min=1, max=30),
        FieldSpec("extra_conditions", "Condiciones adicionales (una por línea)", "text", required=False),
    ]),
]
FIELDS = [f for _, fs in SECTIONS for f in fs]


@router.get("/empresa")
def edit(request: Request, db: Session = Depends(get_db), guardado: int = 0):
    p = company(db)
    return templates.TemplateResponse(request, "settings/company.html", {
        "values": {f.name: getattr(p, f.name) for f in FIELDS}, "sections": SECTIONS, "errors": {}, "saved": guardado})


@router.post("/empresa")
async def save(request: Request, db: Session = Depends(get_db)):
    values, errors = parse_fields(await request.form(), FIELDS)
    if errors:
        return templates.TemplateResponse(request, "settings/company.html", {
            "values": values, "sections": SECTIONS, "errors": errors, "saved": 0}, status_code=422)
    p = company(db)
    for k, v in values.items():
        setattr(p, k, v)
    db.commit()
    return RedirectResponse("/empresa?guardado=1", status_code=303)
