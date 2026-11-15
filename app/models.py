from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

MATERIAL_FAMILIES = {
    "acero": "Acero al carbono",
    "inox": "Acero inoxidable",
    "aluminio": "Aluminio",
    "galvanizado": "Acero galvanizado",
}
GASES = {"O2": "Oxígeno", "N2": "Nitrógeno", "AIRE": "Aire comprimido"}
LASER_TYPES = {"fibra": "Fibra", "co2": "CO₂"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    manufacturer: Mapped[str] = mapped_column(String(120), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    laser_type: Mapped[str] = mapped_column(String(10), default="fibra")
    laser_power_kw: Mapped[float] = mapped_column(Float)
    table_x_mm: Mapped[float] = mapped_column(Float)
    table_y_mm: Mapped[float] = mapped_column(Float)
    max_thickness_mm: Mapped[float] = mapped_column(Float, default=12.0)
    rapid_speed_m_min: Mapped[float] = mapped_column(Float, default=120.0)
    acceleration_g: Mapped[float] = mapped_column(Float, default=1.0)
    positioning_accuracy_mm: Mapped[float] = mapped_column(Float, default=0.05)
    power_consumption_kw: Mapped[float] = mapped_column(Float, doc="Consumo eléctrico total medio")
    hourly_rate_eur: Mapped[float] = mapped_column(Float, doc="Coste/hora máquina (amortización, mano de obra, mantenimiento)")
    setup_cost_eur: Mapped[float] = mapped_column(Float, default=0.0, doc="Preparación/programación por línea de pedido")
    head_overhead_s: Mapped[float] = mapped_column(Float, default=0.5, doc="Subida/bajada de cabezal por contorno")
    sheet_change_min: Mapped[float] = mapped_column(Float, default=2.0)
    speed_efficiency: Mapped[float] = mapped_column(Float, default=0.85, doc="Factor de velocidad real/nominal")
    small_contour_mm: Mapped[float] = mapped_column(Float, default=100.0, doc="Perímetro por debajo del cual se reduce la velocidad")
    small_contour_factor: Mapped[float] = mapped_column(Float, default=0.5)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    cut_params: Mapped[list[CutParameter]] = relationship(
        back_populates="machine", cascade="all, delete-orphan",
        order_by="(CutParameter.material_family, CutParameter.gas, CutParameter.thickness_mm)")

    def param_for(self, family: str, thickness: float, gas: str) -> CutParameter | None:
        exact = [p for p in self.cut_params if p.material_family == family and p.gas == gas
                 and abs(p.thickness_mm - thickness) < 1e-6]
        return exact[0] if exact else None

    def interpolated_param(self, family: str, thickness: float, gas: str) -> CutParameter | None:
        """Parámetro exacto o interpolado linealmente entre los dos espesores tabulados más próximos."""
        exact = self.param_for(family, thickness, gas)
        if exact:
            return exact
        rows = sorted((p for p in self.cut_params if p.material_family == family and p.gas == gas),
                      key=lambda p: p.thickness_mm)
        lower = [p for p in rows if p.thickness_mm < thickness]
        upper = [p for p in rows if p.thickness_mm > thickness]
        if not lower or not upper:
            return None
        a, b = lower[-1], upper[0]
        t = (thickness - a.thickness_mm) / (b.thickness_mm - a.thickness_mm)
        lerp = lambda x, y: x + (y - x) * t  # noqa: E731
        param = CutParameter(material_family=family, gas=gas, thickness_mm=thickness,
                             cut_speed_m_min=lerp(a.cut_speed_m_min, b.cut_speed_m_min),
                             pierce_time_s=lerp(a.pierce_time_s, b.pierce_time_s),
                             gas_flow_m3_h=lerp(a.gas_flow_m3_h, b.gas_flow_m3_h),
                             kerf_mm=lerp(a.kerf_mm, b.kerf_mm))
        param.interpolated = True
        return param

    def gases_for(self, family: str) -> list[str]:
        return sorted({p.gas for p in self.cut_params if p.material_family == family})


class CutParameter(Base):
    __tablename__ = "cut_parameters"
    __table_args__ = (UniqueConstraint("machine_id", "material_family", "thickness_mm", "gas"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id", ondelete="CASCADE"))
    material_family: Mapped[str] = mapped_column(String(20))
    thickness_mm: Mapped[float] = mapped_column(Float)
    gas: Mapped[str] = mapped_column(String(10))
    cut_speed_m_min: Mapped[float] = mapped_column(Float)
    pierce_time_s: Mapped[float] = mapped_column(Float)
    gas_flow_m3_h: Mapped[float] = mapped_column(Float)
    kerf_mm: Mapped[float] = mapped_column(Float, default=0.2)

    machine: Mapped[Machine] = relationship(back_populates="cut_params")

    interpolated = False  # atributo de instancia, no persistido


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    family: Mapped[str] = mapped_column(String(20))
    density_kg_dm3: Mapped[float] = mapped_column(Float)
    price_eur_kg: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CostSettings(Base):
    """Parámetros económicos globales (una sola fila)."""
    __tablename__ = "cost_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    electricity_eur_kwh: Mapped[float] = mapped_column(Float)
    gas_o2_eur_m3: Mapped[float] = mapped_column(Float)
    gas_n2_eur_m3: Mapped[float] = mapped_column(Float)
    gas_air_eur_m3: Mapped[float] = mapped_column(Float)
    margin_pct: Mapped[float] = mapped_column(Float)
    min_order_eur: Mapped[float] = mapped_column(Float)
    sheet_x_mm: Mapped[float] = mapped_column(Float, default=3000)
    sheet_y_mm: Mapped[float] = mapped_column(Float, default=1500)
    part_gap_mm: Mapped[float] = mapped_column(Float, default=5)
    sheet_edge_mm: Mapped[float] = mapped_column(Float, default=10)
    scrap_recovery_pct: Mapped[float] = mapped_column(Float, default=0, doc="% del retal que se repercute (0 = solo envolvente)")
    quantity_discounts: Mapped[list] = mapped_column(JSON, default=list, doc="[[cantidad_min, % dto], ...]")

    def gas_price(self, gas: str) -> float:
        return {"O2": self.gas_o2_eur_m3, "N2": self.gas_n2_eur_m3, "AIRE": self.gas_air_eur_m3}[gas]


class Part(Base):
    """Pieza analizada. NO se guarda el fichero original, solo métricas y vista previa."""
    __tablename__ = "parts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON)
    svg: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
