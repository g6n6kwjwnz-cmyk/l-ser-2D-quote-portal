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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="admin")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


REQUEST_STATUSES = {
    "recibida": "Recibida",
    "en_estudio": "En estudio",
    "enviada": "Oferta enviada",
    "cambios": "Cambios solicitados",
    "aceptada": "Aceptada",
    "rechazada": "Rechazada",
    "caducada": "Caducada",
    "anulada": "Anulada",
}
SUPPLY_OPTIONS = {"nosotros": "Material incluido en la oferta", "cliente": "Material aportado por el cliente"}


class QuoteRequest(Base):
    """Petición de oferta de un cliente externo y, después, la oferta que se le envía."""
    __tablename__ = "quote_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True, default="")
    status: Mapped[str] = mapped_column(String(20), default="recibida")
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # cliente
    company: Mapped[str] = mapped_column(String(160))
    tax_id: Mapped[str] = mapped_column(String(30), default="")
    contact: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    customer_reference: Mapped[str] = mapped_column(String(120), default="")
    desired_date: Mapped[str] = mapped_column(String(20), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # oferta (uso interno hasta que se envía)
    offer_number: Mapped[str] = mapped_column(String(20), default="")
    offer_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_days: Mapped[int] = mapped_column(Integer, default=30)
    lead_time: Mapped[str] = mapped_column(String(200), default="")
    delivery: Mapped[str] = mapped_column(String(200), default="")
    payment: Mapped[str] = mapped_column(String(200), default="")
    offer_notes: Mapped[str] = mapped_column(Text, default="")
    internal_notes: Mapped[str] = mapped_column(Text, default="")
    customer_response: Mapped[str] = mapped_column(Text, default="")
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[RequestItem]] = relationship(back_populates="request", cascade="all, delete-orphan",
                                                    order_by="RequestItem.position")

    @property
    def offer_subtotal(self) -> float:
        return round(sum((i.unit_price or 0) * i.quantity for i in self.items), 2)

    @property
    def valid_until(self):
        if not self.offer_issued_at:
            return None
        from datetime import timedelta
        issued = self.offer_issued_at if self.offer_issued_at.tzinfo else self.offer_issued_at.replace(tzinfo=timezone.utc)
        return issued + timedelta(days=self.valid_days)

    @property
    def is_expired(self) -> bool:
        vu = self.valid_until
        return bool(vu and self.status == "enviada" and vu < _now())


class RequestItem(Base):
    __tablename__ = "request_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("quote_requests.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=1)
    part_id: Mapped[str] = mapped_column(ForeignKey("parts.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"))
    thickness_mm: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    supply: Mapped[str] = mapped_column(String(20), default="nosotros")
    notes: Mapped[str] = mapped_column(String(500), default="")
    # cálculo interno
    machine_id: Mapped[int | None] = mapped_column(ForeignKey("machines.id", ondelete="SET NULL"), nullable=True)
    gas: Mapped[str] = mapped_column(String(10), default="")
    calc_unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    calc_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    calc_error: Mapped[str] = mapped_column(String(300), default="")

    request: Mapped[QuoteRequest] = relationship(back_populates="items")
    part: Mapped[Part] = relationship()
    material: Mapped[Material] = relationship()
    machine: Mapped[Machine | None] = relationship()


class CompanyProfile(Base):
    """Datos de la empresa y condiciones por defecto que aparecen en el PDF de oferta (una fila)."""
    __tablename__ = "company_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    tax_id: Mapped[str] = mapped_column(String(30), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    web: Mapped[str] = mapped_column(String(120), default="")
    valid_days: Mapped[int] = mapped_column(Integer, default=30)
    lead_time: Mapped[str] = mapped_column(String(200), default="")
    delivery: Mapped[str] = mapped_column(String(200), default="")
    payment: Mapped[str] = mapped_column(String(200), default="")
    vat_pct: Mapped[float] = mapped_column(Float, default=21.0)
    extra_conditions: Mapped[str] = mapped_column(Text, default="")
    response_days: Mapped[int] = mapped_column(Integer, default=2, doc="Días laborables en que se promete responder")


# ------------------------------------------------------------------ anidado de chapa
# Separación estándar entre piezas y margen de borde según espesor (orientativo, ajustable en «Anidado › Ajustes»):
# [espesor máx. mm, separación mm, borde mm]
DEFAULT_NEST_GAPS = [[1.0, 2.0, 5.0], [3.0, 3.0, 5.0], [5.0, 5.0, 8.0], [8.0, 8.0, 10.0], [12.0, 12.0, 12.0]]
REMNANT_STATUSES = {"disponible": "Disponible", "reservado": "Reservado", "usado": "Usado"}
NEST_STATUSES = {"borrador": "Borrador", "confirmado": "Confirmado"}


class NestSettings(Base):
    """Reglas de anidado (una fila)."""
    __tablename__ = "nest_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    gap_table: Mapped[list] = mapped_column(JSON, default=lambda: [r[:] for r in DEFAULT_NEST_GAPS])
    extra_gap_families: Mapped[list] = mapped_column(JSON, default=lambda: ["inox", "aluminio"])
    extra_gap_mm: Mapped[float] = mapped_column(Float, default=1.0, doc="Separación adicional en esas familias")
    extra_gap_from_mm: Mapped[float] = mapped_column(Float, default=6.0, doc="A partir de este espesor")
    remnant_min_mm: Mapped[float] = mapped_column(Float, default=300.0, doc="Lado mínimo para guardar un retal")
    allow_rotation: Mapped[bool] = mapped_column(Boolean, default=True)

    def gap_edge(self, family: str, thickness: float) -> tuple[float, float]:
        rows = sorted(self.gap_table or DEFAULT_NEST_GAPS)
        row = next((r for r in rows if thickness <= r[0] + 1e-9), rows[-1])
        gap, edge = float(row[1]), float(row[2])
        if family in (self.extra_gap_families or []) and thickness >= self.extra_gap_from_mm - 1e-9:
            gap += self.extra_gap_mm
        return gap, edge


class SheetFormat(Base):
    """Formato comercial de chapa entera."""
    __tablename__ = "sheet_formats"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    width_mm: Mapped[float] = mapped_column(Float)
    height_mm: Mapped[float] = mapped_column(Float)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def label(self) -> str:
        return f"{self.width_mm:g} × {self.height_mm:g} mm"


class Remnant(Base):
    """Retal de chapa en almacén (rectangular)."""
    __tablename__ = "remnants"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), default="")
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"))
    thickness_mm: Mapped[float] = mapped_column(Float)
    width_mm: Mapped[float] = mapped_column(Float)
    height_mm: Mapped[float] = mapped_column(Float)
    location: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="disponible")
    source_nest_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_in_nest_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    material: Mapped[Material] = relationship()


class Nest(Base):
    """Anidado de un material y espesor: piezas a colocar y disposición por chapa."""
    __tablename__ = "nests"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="borrador")
    request_id: Mapped[int | None] = mapped_column(ForeignKey("quote_requests.id", ondelete="CASCADE"), nullable=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"))
    thickness_mm: Mapped[float] = mapped_column(Float)
    gap_mm: Mapped[float] = mapped_column(Float)
    edge_mm: Mapped[float] = mapped_column(Float)
    sheet_w_mm: Mapped[float] = mapped_column(Float)
    sheet_h_mm: Mapped[float] = mapped_column(Float)
    use_remnants: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_rotation: Mapped[bool] = mapped_column(Boolean, default=True)
    # [{"part_id", "quantity"}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    # [{"source": {"kind": "formato"|"retal", "remnant_id", "w", "h"}, "placements": [{key,x,y,rotation}]}]
    sheets: Mapped[list] = mapped_column(JSON, default=list)
    utilization_pct: Mapped[float] = mapped_column(Float, default=0.0)
    material_factor: Mapped[float] = mapped_column(Float, default=0.0, doc="Área consumida / área neta de piezas")
    conflicts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    material: Mapped[Material] = relationship()
    request: Mapped[QuoteRequest | None] = relationship()
