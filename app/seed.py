"""Datos iniciales: una máquina de ejemplo (fibra 6 kW, mesa 3000×1500), materiales y parámetros económicos.

Los valores son orientativos, del orden de las tablas de fabricantes para fuentes de fibra de 6 kW;
están pensados para simular y deben ajustarse con los datos reales del taller desde el formulario.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CompanyProfile, CostSettings, CutParameter, Machine, Material

# (familia, gas, espesor mm, velocidad m/min, perforación s, caudal gas m3/h, kerf mm)
EXAMPLE_PARAMS = [
    # Acero al carbono con oxígeno (corte reactivo, espesores medios/gruesos)
    ("acero", "O2", 3, 4.2, 0.3, 1.5, 0.25), ("acero", "O2", 4, 3.6, 0.4, 1.8, 0.28),
    ("acero", "O2", 5, 3.2, 0.5, 2.0, 0.30), ("acero", "O2", 6, 2.8, 0.8, 2.2, 0.32),
    ("acero", "O2", 8, 2.2, 1.2, 2.5, 0.35), ("acero", "O2", 10, 1.6, 2.0, 2.8, 0.40),
    ("acero", "O2", 12, 1.3, 3.0, 3.0, 0.45),
    # Acero al carbono con nitrógeno (corte limpio, sin óxido)
    ("acero", "N2", 1, 35.0, 0.1, 18, 0.12), ("acero", "N2", 2, 18.0, 0.2, 22, 0.15),
    ("acero", "N2", 3, 9.5, 0.3, 26, 0.18), ("acero", "N2", 4, 6.0, 0.5, 30, 0.20),
    ("acero", "N2", 5, 4.2, 0.6, 34, 0.22), ("acero", "N2", 6, 3.0, 0.8, 38, 0.25),
    # Acero al carbono con aire
    ("acero", "AIRE", 1, 32.0, 0.1, 20, 0.12), ("acero", "AIRE", 2, 16.0, 0.2, 24, 0.15),
    ("acero", "AIRE", 3, 8.5, 0.3, 28, 0.18), ("acero", "AIRE", 4, 5.5, 0.5, 32, 0.20),
    ("acero", "AIRE", 6, 2.8, 0.8, 38, 0.25),
    # Inoxidable con nitrógeno
    ("inox", "N2", 1, 38.0, 0.1, 18, 0.12), ("inox", "N2", 2, 20.0, 0.2, 22, 0.15),
    ("inox", "N2", 3, 11.0, 0.3, 26, 0.18), ("inox", "N2", 4, 7.0, 0.5, 30, 0.20),
    ("inox", "N2", 5, 5.0, 0.7, 34, 0.22), ("inox", "N2", 6, 3.5, 1.0, 38, 0.25),
    ("inox", "N2", 8, 2.0, 1.5, 45, 0.30), ("inox", "N2", 10, 1.2, 2.2, 52, 0.35),
    ("inox", "N2", 12, 0.8, 3.0, 58, 0.40),
    # Aluminio con nitrógeno
    ("aluminio", "N2", 1, 35.0, 0.1, 18, 0.12), ("aluminio", "N2", 2, 18.0, 0.2, 22, 0.15),
    ("aluminio", "N2", 3, 10.0, 0.3, 26, 0.18), ("aluminio", "N2", 4, 6.5, 0.5, 30, 0.20),
    ("aluminio", "N2", 5, 4.5, 0.7, 34, 0.22), ("aluminio", "N2", 6, 3.0, 1.0, 38, 0.25),
    ("aluminio", "N2", 8, 1.6, 1.5, 45, 0.30), ("aluminio", "N2", 10, 1.0, 2.2, 52, 0.35),
    ("aluminio", "N2", 12, 0.6, 3.0, 58, 0.40),
    # Galvanizado con nitrógeno
    ("galvanizado", "N2", 1, 32.0, 0.1, 18, 0.12), ("galvanizado", "N2", 2, 16.0, 0.2, 22, 0.15),
    ("galvanizado", "N2", 3, 8.5, 0.3, 26, 0.18), ("galvanizado", "N2", 4, 5.5, 0.5, 30, 0.20),
]

EXAMPLE_MACHINE = dict(
    name="Fibra 6 kW · Ejemplo",
    manufacturer="Genérica (simulación)",
    model="FL-3015 6kW",
    laser_type="fibra",
    laser_power_kw=6.0,
    table_x_mm=3000, table_y_mm=1500,
    max_thickness_mm=12.0,          # limitado al alcance del portal (la fuente admite más en acero)
    rapid_speed_m_min=140, acceleration_g=1.5, positioning_accuracy_mm=0.03,
    power_consumption_kw=28.0,      # fuente + enfriadora + accionamientos + aspiración
    hourly_rate_eur=85.0,
    setup_cost_eur=25.0,
    head_overhead_s=0.6,
    sheet_change_min=2.0,           # mesa de intercambio
    speed_efficiency=0.85,
    small_contour_mm=100.0, small_contour_factor=0.5,
    notes="Máquina de ejemplo para simular ofertas. Ajustar con los datos reales del taller.",
)

MATERIALS = [
    ("S235JR", "Acero S235JR laminado en caliente", "acero", 7.85, 1.10),
    ("S355MC", "Acero S355MC", "acero", 7.85, 1.25),
    ("DC01", "Acero DC01 laminado en frío", "acero", 7.85, 1.20),
    ("AISI304", "Inoxidable AISI 304 2B", "inox", 7.93, 3.80),
    ("AISI316", "Inoxidable AISI 316L 2B", "inox", 7.98, 5.20),
    ("AL5754", "Aluminio 5754 H111", "aluminio", 2.66, 4.50),
    ("DX51D", "Galvanizado DX51D Z275", "galvanizado", 7.85, 1.35),
]


def seed(db: Session) -> None:
    if db.scalar(select(Machine).limit(1)) is None:
        m = Machine(**EXAMPLE_MACHINE)
        m.cut_params = [CutParameter(material_family=f, gas=g, thickness_mm=t, cut_speed_m_min=v,
                                     pierce_time_s=p, gas_flow_m3_h=q, kerf_mm=k)
                        for f, g, t, v, p, q, k in EXAMPLE_PARAMS]
        db.add(m)
    if db.scalar(select(Material).limit(1)) is None:
        db.add_all(Material(code=c, name=n, family=f, density_kg_dm3=d, price_eur_kg=p) for c, n, f, d, p in MATERIALS)
    if db.scalar(select(CostSettings).limit(1)) is None:
        db.add(CostSettings(
            electricity_eur_kwh=0.16, gas_o2_eur_m3=0.35, gas_n2_eur_m3=0.20, gas_air_eur_m3=0.02,
            margin_pct=25.0, min_order_eur=60.0, sheet_x_mm=3000, sheet_y_mm=1500,
            part_gap_mm=5, sheet_edge_mm=10, scrap_recovery_pct=30,
            quantity_discounts=[[10, 3], [50, 7], [100, 10], [500, 15]],
        ))
    if db.scalar(select(CompanyProfile).limit(1)) is None:
        # Huecos a completar desde «Empresa» en el menú
        db.add(CompanyProfile(
            name="NOMBRE DE LA EMPRESA, S.L.", tax_id="CIF pendiente", address="Dirección pendiente",
            phone="", email="", web="", valid_days=30,
            lead_time="7 días laborables desde la aceptación",
            delivery="Recogida en nuestras instalaciones (portes no incluidos)",
            payment="Transferencia a 30 días", vat_pct=21.0, response_days=2,
        ))
    db.commit()
