"""Motor de costes de corte láser 2D.

Modelo (por línea de pedido = una pieza x cantidad):

  Tiempo por pieza
    corte      = Σ longitud_contorno / (v_corte · eficiencia · [factor contorno pequeño])
    perforado  = nº perforaciones · t_perforación
    vacío      = recorrido en vacío / v_rápida + nº contornos · t_cabezal
  Tiempo de pedido = cantidad · tiempo pieza + nº chapas · t_cambio_chapa

  Costes
    máquina    = horas · coste/hora máquina
    gas        = horas de haz (corte + perforado) · caudal (m³/h) · precio gas (€/m³)
    energía    = horas · consumo eléctrico (kW) · €/kWh
    material   = peso repercutido (envolvente + separación + parte de retal) · €/kg
    preparación= coste fijo por línea de pedido (programación, puesta a punto)

  Precio = coste · (1 + margen) − descuento por cantidad (sobre la parte variable),
           con un importe mínimo de pedido.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict


class CostingError(ValueError):
    pass


@dataclass
class QuoteInput:
    quantity: int
    thickness_mm: float
    gas: str
    # Resultado de un anidado real (opcional): sustituye la estimación por envolvente rectangular
    nest_charged_area_mm2: float | None = None   # área de chapa consumida por pieza
    nest_sheets: int | None = None
    nest_utilization_pct: float | None = None


@dataclass
class QuoteResult:
    quantity: int
    # tiempos (s)
    cut_time_s: float
    pierce_time_s: float
    rapid_time_s: float
    part_time_s: float
    order_time_h: float
    # chapa / material
    parts_per_sheet: int
    sheets_needed: int
    sheet_utilization_pct: float
    envelope_utilization_pct: float
    net_weight_kg: float
    charged_weight_kg: float
    # costes del pedido (€)
    machine_cost: float
    gas_cost: float
    energy_cost: float
    material_cost: float
    setup_cost: float
    total_cost: float
    margin_pct: float
    discount_pct: float
    discount_eur: float
    min_order_applied: bool
    total_price: float
    unit_price: float
    cut_speed_m_min: float
    param_interpolated: bool
    warnings: list[str] = field(default_factory=list)
    material_source: str = "estimado"   # «estimado» (envolvente) o «anidado» (anidado real)

    def to_dict(self) -> dict:
        return asdict(self)


def _parts_per_sheet(w: float, h: float, usable_x: float, usable_y: float) -> int:
    if w <= 0 or h <= 0:
        return 0
    a = math.floor(usable_x / w) * math.floor(usable_y / h)
    b = math.floor(usable_x / h) * math.floor(usable_y / w)
    return max(a, b)


def _discount_for(qty: int, tiers: list) -> float:
    pct = 0.0
    for min_qty, d in sorted(tiers or [], key=lambda t: t[0]):
        if qty >= min_qty:
            pct = float(d)
    return pct


def compute_quote(geom: dict, machine, param, material, settings, q: QuoteInput) -> QuoteResult:
    if q.quantity < 1:
        raise CostingError("La cantidad debe ser al menos 1.")
    if param is None:
        raise CostingError(f"La máquina «{machine.name}» no tiene parámetros de corte para "
                           f"{material.family} {q.thickness_mm} mm con {q.gas} (ni espesores para interpolar).")
    if param.cut_speed_m_min <= 0:
        raise CostingError("La velocidad de corte debe ser mayor que 0.")

    warnings: list[str] = []
    if q.thickness_mm > machine.max_thickness_mm:
        warnings.append(f"El espesor {q.thickness_mm} mm supera el máximo de la máquina ({machine.max_thickness_mm} mm).")
    if getattr(param, "interpolated", False):
        warnings.append("Parámetros de corte interpolados entre espesores tabulados.")

    # ---------------------------------------------------------------- tiempos por pieza
    v_mm_s = param.cut_speed_m_min * 1000 / 60 * machine.speed_efficiency
    cut_time = 0.0
    small = 0
    for length in geom["contour_lengths_mm"]:
        factor = 1.0
        if length < machine.small_contour_mm:
            factor = machine.small_contour_factor
            small += 1
        cut_time += length / (v_mm_s * factor)
    pierce_time = geom["pierce_count"] * param.pierce_time_s
    rapid_time = geom["rapid_length_mm"] / (machine.rapid_speed_m_min * 1000 / 60) \
        + geom["contour_count"] * machine.head_overhead_s
    part_time = cut_time + pierce_time + rapid_time

    # ---------------------------------------------------------------- encaje en chapa
    w, h = geom["min_rect_width_mm"], geom["min_rect_height_mm"]
    if not (max(w, h) <= max(machine.table_x_mm, machine.table_y_mm) and
            min(w, h) <= min(machine.table_x_mm, machine.table_y_mm)):
        warnings.append(f"La pieza ({w:.0f}×{h:.0f} mm) no cabe en la mesa de la máquina "
                        f"({machine.table_x_mm:.0f}×{machine.table_y_mm:.0f} mm).")
    gap, edge = settings.part_gap_mm, settings.sheet_edge_mm
    sheet_x = min(settings.sheet_x_mm, max(machine.table_x_mm, machine.table_y_mm))
    sheet_y = min(settings.sheet_y_mm, min(machine.table_x_mm, machine.table_y_mm))
    fw, fh = w + gap, h + gap
    n_sheet = _parts_per_sheet(fw, fh, sheet_x - 2 * edge + gap, sheet_y - 2 * edge + gap)
    sheet_area = sheet_x * sheet_y
    if n_sheet == 0:
        warnings.append("La pieza no cabe en la chapa estándar; se repercute su envolvente completa.")
        n_sheet = 1
        sheets_needed = q.quantity
        charged_area = fw * fh
    else:
        sheets_needed = math.ceil(q.quantity / n_sheet)
        leftover_share = max(sheet_area - n_sheet * fw * fh, 0) / n_sheet
        charged_area = fw * fh + leftover_share * settings.scrap_recovery_pct / 100

    net_area = geom["net_area_mm2"]
    if net_area <= 0:
        warnings.append("Área neta nula (sin contorno exterior cerrado): el material se calcula con la envolvente.")
    to_kg = q.thickness_mm * material.density_kg_dm3 / 1e6
    net_weight = net_area * to_kg
    charged_weight = charged_area * to_kg
    # aprovechamiento del anidado: área neta de una chapa completa de piezas / área de la chapa
    # (en lotes pequeños el sobrante vuelve a almacén como retal; se repercute según scrap_recovery_pct)
    sheet_util = 100 * net_area * n_sheet / sheet_area if sheet_area else 0
    env_util = 100 * net_area / (w * h) if w * h else 0
    material_source = "estimado"
    if q.nest_charged_area_mm2:
        charged_area = q.nest_charged_area_mm2
        charged_weight = charged_area * to_kg
        sheets_needed = q.nest_sheets or sheets_needed
        if q.nest_utilization_pct is not None:
            sheet_util = q.nest_utilization_pct
        material_source = "anidado"

    # ---------------------------------------------------------------- viabilidad
    smallest = geom.get("smallest_inner_feature_mm")
    if smallest is not None and smallest < q.thickness_mm:
        warnings.append(f"Hay agujeros/interiores de {smallest:.1f} mm, menores que el espesor ({q.thickness_mm} mm): "
                        "calidad no garantizada, revisar con el cliente.")
    if small:
        warnings.append(f"{small} contorno(s) pequeños (< {machine.small_contour_mm:.0f} mm) cortados a velocidad reducida.")

    # ---------------------------------------------------------------- costes del pedido
    order_time_s = q.quantity * part_time + sheets_needed * machine.sheet_change_min * 60
    order_h = order_time_s / 3600
    beam_h = q.quantity * (cut_time + pierce_time) / 3600

    machine_cost = order_h * machine.hourly_rate_eur
    gas_cost = beam_h * param.gas_flow_m3_h * settings.gas_price(q.gas)
    energy_cost = order_h * machine.power_consumption_kw * settings.electricity_eur_kwh
    material_cost = q.quantity * charged_weight * material.price_eur_kg
    setup_cost = machine.setup_cost_eur
    total_cost = machine_cost + gas_cost + energy_cost + material_cost + setup_cost

    margin = settings.margin_pct / 100
    variable_price = (total_cost - setup_cost) * (1 + margin)
    discount_pct = _discount_for(q.quantity, settings.quantity_discounts)
    discount_eur = variable_price * discount_pct / 100
    price = variable_price - discount_eur + setup_cost * (1 + margin)
    min_applied = price < settings.min_order_eur
    if min_applied:
        price = settings.min_order_eur

    r2 = lambda x: round(x, 2)  # noqa: E731
    return QuoteResult(
        quantity=q.quantity,
        cut_time_s=round(cut_time, 1), pierce_time_s=round(pierce_time, 1), rapid_time_s=round(rapid_time, 1),
        part_time_s=round(part_time, 1), order_time_h=round(order_h, 3),
        parts_per_sheet=n_sheet, sheets_needed=sheets_needed,
        sheet_utilization_pct=round(sheet_util, 1), envelope_utilization_pct=round(env_util, 1),
        net_weight_kg=round(net_weight, 3), charged_weight_kg=round(charged_weight, 3),
        machine_cost=r2(machine_cost), gas_cost=r2(gas_cost), energy_cost=r2(energy_cost),
        material_cost=r2(material_cost), setup_cost=r2(setup_cost), total_cost=r2(total_cost),
        margin_pct=settings.margin_pct, discount_pct=discount_pct, discount_eur=r2(discount_eur),
        min_order_applied=min_applied, total_price=r2(price), unit_price=round(price / q.quantity, 3),
        cut_speed_m_min=round(param.cut_speed_m_min, 2), param_interpolated=getattr(param, "interpolated", False),
        warnings=warnings, material_source=material_source,
    )
