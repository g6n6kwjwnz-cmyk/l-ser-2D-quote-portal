"""Documento PDF de oferta para el cliente.

Solo incluye lo que el cliente debe ver: piezas, material, cantidades, precios y condiciones.
Nunca costes internos, máquina, tiempos ni márgenes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from reportlab.graphics.shapes import Drawing, PolyLine, Polygon, Rect
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

INK = colors.HexColor("#16191f")
MUTED = colors.HexColor("#5f6b7a")
LINE = colors.HexColor("#d9dde3")
ACCENT = colors.HexColor("#0f6fde")
BAND = colors.HexColor("#f3f6fa")


@dataclass
class Company:
    name: str
    tax_id: str
    address: str
    phone: str
    email: str
    web: str = ""


@dataclass
class OfferLine:
    reference: str
    description: str
    material: str
    thickness_mm: float
    quantity: int
    unit_price: float
    width_mm: float
    height_mm: float
    contours: list = field(default_factory=list)   # [(points, closed, kind)] para la miniatura
    notes: str = ""

    @property
    def amount(self) -> float:
        return round(self.unit_price * self.quantity, 2)


@dataclass
class Offer:
    number: str
    request_number: str
    issued: date
    valid_days: int
    customer_company: str
    customer_contact: str
    customer_email: str
    customer_reference: str
    lines: list[OfferLine]
    lead_time: str
    delivery: str
    payment: str
    vat_pct: float = 21.0
    accept_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return round(sum(l.amount for l in self.lines), 2)

    @property
    def vat(self) -> float:
        return round(self.subtotal * self.vat_pct / 100, 2)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.vat, 2)


def eur(v: float) -> str:
    return f"{v:,.2f} €".replace(",", " ").replace(".", ",")


def num(v: float, d: int = 1) -> str:
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")


def _thumb(contours, size=22 * mm) -> Drawing:
    d = Drawing(size, size)
    d.add(Rect(0, 0, size, size, fillColor=BAND, strokeColor=None))
    pts = [p for c in contours for p in c[0]]
    if not pts:
        return d
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    minx, miny, w, h = min(xs), min(ys), max(xs) - min(xs) or 1, max(ys) - min(ys) or 1
    pad = 2 * mm
    k = (size - 2 * pad) / max(w, h)
    ox, oy = pad + (size - 2 * pad - w * k) / 2, pad + (size - 2 * pad - h * k) / 2
    for points, closed, kind in contours:
        step = max(1, len(points) // 400)
        flat = []
        for x, y in points[::step]:
            flat += [ox + (x - minx) * k, oy + (y - miny) * k]
        color = ACCENT if kind == "exterior" else colors.HexColor("#d6336c")
        shape = Polygon(flat) if closed else PolyLine(flat)
        shape.strokeColor, shape.strokeWidth = color, 0.6
        shape.fillColor = None
        d.add(shape)
    return d


def build_offer_pdf(offer: Offer, company: Company) -> bytes:
    buf = BytesIO()
    st = {
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=INK),
        "b": ParagraphStyle("b", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK),
        "bb": ParagraphStyle("bb", fontName="Helvetica-Bold", fontSize=9, leading=12.5, textColor=INK),
        "m": ParagraphStyle("m", fontName="Helvetica", fontSize=8, leading=11, textColor=MUTED),
        "mr": ParagraphStyle("mr", fontName="Helvetica", fontSize=8, leading=11, textColor=MUTED, alignment=TA_RIGHT),
        "lbl": ParagraphStyle("lbl", fontName="Helvetica-Bold", fontSize=7, leading=10, textColor=MUTED),
        "co": ParagraphStyle("co", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK),
    }

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(18 * mm, 16 * mm, A4[0] - 18 * mm, 16 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 11 * mm, f"{company.name} · {company.tax_id} · Oferta {offer.number}. "
                          "Documento confidencial dirigido exclusivamente al destinatario.")
        canvas.drawRightString(A4[0] - 18 * mm, 11 * mm, f"Página {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=20 * mm, title=f"Oferta {offer.number}", author=company.name)
    W = A4[0] - 36 * mm
    story = []

    # Cabecera: empresa (izq.) y datos de la oferta (dcha.)
    valid_until = date.fromordinal(offer.issued.toordinal() + offer.valid_days)
    left = [Paragraph(company.name, st["co"]),
            Paragraph(f"{company.address}<br/>{company.tax_id} · {company.phone}<br/>{company.email}"
                      + (f" · {company.web}" if company.web else ""), st["m"])]
    right = Table([
        [Paragraph("OFERTA", st["lbl"]), Paragraph(f"<b>{offer.number}</b>", st["b"])],
        [Paragraph("FECHA", st["lbl"]), Paragraph(offer.issued.strftime("%d/%m/%Y"), st["b"])],
        [Paragraph("VÁLIDA HASTA", st["lbl"]), Paragraph(valid_until.strftime("%d/%m/%Y"), st["b"])],
        [Paragraph("SU PETICIÓN", st["lbl"]), Paragraph(offer.request_number, st["b"])],
    ], colWidths=[28 * mm, 34 * mm])
    right.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                               ("TOPPADDING", (0, 0), (-1, -1), 1)]))
    head = Table([[left, right]], colWidths=[W - 64 * mm, 64 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 6 * mm), Paragraph("Oferta de corte por láser", st["h1"]), Spacer(1, 3 * mm)]

    cust = Table([[
        [Paragraph("CLIENTE", st["lbl"]), Paragraph(f"<b>{offer.customer_company}</b><br/>"
                                                    f"{offer.customer_contact} · {offer.customer_email}", st["b"])],
        [Paragraph("SU REFERENCIA", st["lbl"]), Paragraph(offer.customer_reference or "—", st["b"])],
    ]], colWidths=[W * 0.62, W * 0.38])
    cust.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BAND), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    story += [cust, Spacer(1, 6 * mm)]

    # Líneas
    rows = [[Paragraph(h, st["lbl"]) for h in ("POS.", "PIEZA", "", "MATERIAL", "CANT.", "PRECIO UD.", "IMPORTE")]]
    for i, l in enumerate(offer.lines, 1):
        desc = (f"<b>{l.reference}</b><br/>{l.description}<br/>"
                f"<font color='#5f6b7a'>Dimensiones {num(l.width_mm)} × {num(l.height_mm)} mm</font>")
        if l.notes:
            desc += f"<br/><font color='#a15c00'>{l.notes}</font>"
        rows.append([Paragraph(f"{i:02d}", st["b"]), _thumb(l.contours), Paragraph(desc, st["b"]),
                     Paragraph(f"{l.material}<br/>{num(l.thickness_mm)} mm", st["b"]),
                     Paragraph(f"{l.quantity}", st["mr"].clone("q", textColor=INK, fontSize=9)),
                     Paragraph(eur(l.unit_price), st["mr"].clone("u", textColor=INK, fontSize=9)),
                     Paragraph(f"<b>{eur(l.amount)}</b>", st["mr"].clone("a", textColor=INK, fontSize=9))])
    cols = [11 * mm, 25 * mm, None, 30 * mm, 14 * mm, 23 * mm, 25 * mm]
    fixed = sum(c for c in cols if c)
    cols[2] = W - fixed
    lines = Table(rows, colWidths=cols, repeatRows=1)
    lines.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK), ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 1), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5), ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
    ]))
    story += [lines, Spacer(1, 4 * mm)]

    tot = Table([
        [Paragraph("Base imponible", st["b"]), Paragraph(eur(offer.subtotal), st["mr"].clone("t1", textColor=INK, fontSize=9))],
        [Paragraph(f"IVA {num(offer.vat_pct, 0)} %", st["b"]), Paragraph(eur(offer.vat), st["mr"].clone("t2", textColor=INK, fontSize=9))],
        [Paragraph("<b>TOTAL</b>", st["b"].clone("tt", fontSize=11)),
         Paragraph(f"<b>{eur(offer.total)}</b>", st["mr"].clone("t3", textColor=INK, fontSize=12, leading=15))],
    ], colWidths=[40 * mm, 35 * mm], hAlign="RIGHT")
    tot.setStyle(TableStyle([("LINEABOVE", (0, 2), (-1, 2), 0.8, INK), ("TOPPADDING", (0, 2), (-1, 2), 5)]))
    story += [tot, Spacer(1, 5 * mm)]

    cond = [
        f"<b>Plazo de entrega:</b> {offer.lead_time}",
        f"<b>Entrega:</b> {offer.delivery}",
        f"<b>Forma de pago:</b> {offer.payment}",
        "<b>Incluye:</b> material y corte por láser según los planos recibidos. No incluye plegado, "
        "mecanizado, roscado, desbarbado ni tratamientos superficiales salvo que se indique.",
        "<b>Calidad:</b> tolerancias de corte según UNE-EN ISO 9013. Las cotas se toman del fichero CAD "
        "facilitado; el cliente es responsable de su exactitud.",
        f"<b>Validez:</b> {offer.valid_days} días. Precios sujetos a la evolución del coste de la chapa pasado ese plazo.",
    ] + offer.notes
    story.append(KeepTogether([Paragraph("Condiciones", st["bb"]), Spacer(1, 2 * mm)]
                              + [Paragraph(c, st["b"]) for c in cond]))
    story.append(Spacer(1, 5 * mm))

    acc = [[Paragraph("<b>Cómo aceptar esta oferta</b><br/>"
                      + (f"Acéptela online en <font color='#0f6fde'>{offer.accept_url}</font> "
                         "o responda a este correo indicando el número de oferta." if offer.accept_url else
                         "Responda a este correo indicando el número de oferta."), st["b"]),
            Paragraph("Conforme cliente (firma y sello)<br/><br/><br/>_______________________________", st["m"])]]
    acc_t = Table(acc, colWidths=[W * 0.6, W * 0.4])
    acc_t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [KeepTogether([acc_t]), Spacer(1, 5 * mm),
              Paragraph("Protección de datos: sus datos y planos se tratan con la única finalidad de gestionar "
                        "esta oferta y, en su caso, el pedido, conforme al RGPD (UE) 2016/679. Puede ejercer sus "
                        f"derechos escribiendo a {company.email}.", st["m"])]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
