from types import SimpleNamespace as NS

import pytest

from app.costing.engine import CostingError, QuoteInput, compute_quote
from app.models import CostSettings, CutParameter, Machine
from app.seed import EXAMPLE_MACHINE, EXAMPLE_PARAMS

GEOM = {  # placa 200x100 simplificada: un contorno de 600 mm y 4 de 31,4 mm
    "contour_lengths_mm": [600.0, 31.4, 31.4, 31.4, 31.4], "pierce_count": 5, "contour_count": 5,
    "rapid_length_mm": 500.0, "min_rect_width_mm": 200.0, "min_rect_height_mm": 100.0,
    "net_area_mm2": 19686.0, "smallest_inner_feature_mm": 10.0,
}


def machine():
    m = Machine(**EXAMPLE_MACHINE)
    m.cut_params = [CutParameter(material_family=f, gas=g, thickness_mm=t, cut_speed_m_min=v,
                                 pierce_time_s=p, gas_flow_m3_h=q, kerf_mm=k) for f, g, t, v, p, q, k in EXAMPLE_PARAMS]
    return m


def settings(**kw):
    base = dict(electricity_eur_kwh=0.16, gas_o2_eur_m3=0.35, gas_n2_eur_m3=0.20, gas_air_eur_m3=0.02,
                margin_pct=25.0, min_order_eur=0, sheet_x_mm=3000, sheet_y_mm=1500, part_gap_mm=5,
                sheet_edge_mm=10, scrap_recovery_pct=0, quantity_discounts=[[10, 5]])
    return CostSettings(**(base | kw))


STEEL = NS(family="acero", density_kg_dm3=7.85, price_eur_kg=1.10)


def quote(qty=1, t=6, gas="O2", **kw):
    m = machine()
    return compute_quote(GEOM, m, m.interpolated_param("acero", t, gas), STEEL, settings(**kw),
                         QuoteInput(quantity=qty, thickness_mm=t, gas=gas))


def test_cut_time_follows_speed_table():
    q = quote()
    v = 2.8 * 1000 / 60 * 0.85
    expected = 600 / v + 4 * 31.4 / (v * 0.5)
    assert q.cut_time_s == pytest.approx(expected, abs=0.1)
    assert q.pierce_time_s == pytest.approx(5 * 0.8)


def test_weight_and_material():
    q = quote()
    assert q.net_weight_kg == pytest.approx(19686 * 6 * 7.85 / 1e6, abs=1e-3)
    assert q.charged_weight_kg == pytest.approx(205 * 105 * 6 * 7.85 / 1e6, abs=1e-3)


def test_totals_are_consistent():
    q = quote(qty=5)
    assert q.total_cost == pytest.approx(q.machine_cost + q.gas_cost + q.energy_cost + q.material_cost
                                         + q.setup_cost, abs=0.02)
    assert q.total_price == pytest.approx(q.total_cost * 1.25, abs=0.02)


def test_quantity_discount_and_unit_price_drop():
    one, many = quote(qty=1), quote(qty=50)
    assert many.discount_pct == 5
    assert many.unit_price < one.unit_price          # la preparación se reparte


def test_minimum_order():
    q = quote(min_order_eur=500)
    assert q.min_order_applied and q.total_price == 500


def test_interpolation_between_thicknesses():
    q = quote(t=7)
    assert q.param_interpolated
    assert q.cut_speed_m_min == pytest.approx((2.8 + 2.2) / 2)


def test_missing_parameters_raise():
    with pytest.raises(CostingError):
        quote(t=10, gas="AIRE")


def test_thin_hole_warning():
    q = quote(t=12)
    assert any("menores que el espesor" in w for w in q.warnings)
