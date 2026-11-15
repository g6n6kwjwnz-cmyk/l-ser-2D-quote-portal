"""Utilidades compartidas de la capa web: plantillas, formato numérico en español y parseo de formularios."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .config import settings
from .models import GASES, LASER_TYPES, MATERIAL_FAMILIES

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def fmt_num(value, decimals: int = 2) -> str:
    if value is None:
        return "—"
    s = f"{float(value):,.{decimals}f}"
    return s.replace(",", " ").replace(".", ",")


def fmt_eur(value) -> str:
    return f"{fmt_num(value, 2)} €"


def fmt_duration(seconds) -> str:
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{fmt_num(seconds, 1)} s"
    m, s = divmod(round(seconds), 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


templates.env.filters.update(num=fmt_num, eur=fmt_eur, dur=fmt_duration)
templates.env.globals.update(app_name=settings.app_name, FAMILIES=MATERIAL_FAMILIES, GASES=GASES,
                             LASER_TYPES=LASER_TYPES)


@dataclass
class FieldSpec:
    name: str
    label: str
    kind: str = "float"        # float | int | str | text | select | bool
    unit: str = ""
    min: float | None = 0
    max: float | None = None
    required: bool = True
    help: str = ""
    options: dict | None = None
    step: str = "any"


class FormErrors(dict):
    pass


def parse_fields(form, specs: list[FieldSpec]) -> tuple[dict, FormErrors]:
    values: dict = {}
    errors = FormErrors()
    for f in specs:
        raw = form.get(f.name)
        if f.kind == "bool":
            values[f.name] = raw in ("on", "true", "1")
            continue
        raw = (raw or "").strip()
        if not raw:
            if f.required:
                errors[f.name] = "Obligatorio."
            values[f.name] = "" if f.kind in ("str", "text", "select") else None
            continue
        if f.kind in ("float", "int"):
            try:
                num = float(raw.replace(",", "."))
                if f.kind == "int":
                    num = int(num)
            except ValueError:
                errors[f.name] = "Debe ser un número."
                values[f.name] = raw
                continue
            if f.min is not None and num < f.min:
                errors[f.name] = f"Mínimo {f.min}."
            if f.max is not None and num > f.max:
                errors[f.name] = f"Máximo {f.max}."
            values[f.name] = num
        elif f.kind == "select":
            if f.options and raw not in f.options:
                errors[f.name] = "Opción no válida."
            values[f.name] = raw
        else:
            limit = 2000 if f.kind == "text" else 120
            if len(raw) > limit:
                errors[f.name] = f"Máximo {limit} caracteres."
            values[f.name] = raw
    return values, errors
