# Portal de ofertas · Corte láser 2D

Portal para recibir peticiones de oferta de corte láser 2D (espesores hasta 12 mm), analizar los planos
y calcular el coste de fabricación.

## Fase 1 (este código)

| Módulo | Qué hace |
|---|---|
| **Motor geométrico** (`app/geometry/dxf_engine.py`) | Lee DXF (y DWG con conversor), explota bloques, convierte unidades a mm, ignora capas de cotas/texto, elimina duplicados, une entidades sueltas en contornos, clasifica exterior/interior/isla y calcula longitud de corte, nº de perforaciones, área neta, envolvente (alineada y mínima rotada), recorrido en vacío y el interior más pequeño. Genera la vista previa SVG. |
| **Motor de costes** (`app/costing/engine.py`) | Tiempo de corte por velocidad según material/espesor/gas (con interpolación entre espesores y penalización de contornos pequeños), perforado, vacío y cambios de chapa; coste de máquina, gas, energía, material (envolvente + separación + parte del retal), preparación; margen, descuentos por cantidad e importe mínimo. Aprovechamiento de chapa por anidado rectangular. |
| **Máquinas** (`/maquinas`) | Alta, consulta, edición, duplicado, baja/reactivación y eliminación, con su tabla de parámetros de corte. Incluye una máquina de ejemplo: fibra 6 kW, mesa 3000×1500. |
| **Piezas** (`/piezas`) | Subida de planos con validación (extensión + contenido real + tamaño), análisis y vista previa, y calculadora de oferta. |
| **Materiales y parámetros** | Precio/densidad de materiales; precios de gas y electricidad, margen, mínimo, formato de chapa, separación y tramos de descuento. |

### Stack

- **Python 3.13 + FastAPI**: Python tiene las mejores librerías CAD/geometría (ezdxf, Shapely) y así el motor
  y la web comparten código. FastAPI es rápido, tipado y genera la API para la futura integración (ERP, portal cliente).
- **ezdxf + Shapely**: lectura DXF robusta y operaciones geométricas fiables (GEOS).
- **SQLAlchemy 2**: SQLite en desarrollo, PostgreSQL en producción con solo cambiar `PORTAL_DATABASE_URL`.
- **HTML renderizado en servidor (Jinja2) + JS mínimo sin dependencias**: menos superficie de ataque,
  CSP estricta (`script-src 'self'`) y sin build de frontend.

### Seguridad ya incluida

- El plano original **no se guarda**: solo métricas, huella SHA-256 y la vista previa SVG generada en servidor.
- Validación de subida por extensión, firma del contenido y tamaño máximo.
- Cabeceras CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`; rechazo de POST de otro origen (CSRF básico).
- Identificadores de pieza no predecibles (UUID).

Pendiente para fases siguientes: autenticación con roles y 2FA para administración, cifrado en reposo,
registro de auditoría, política de retención RGPD, portal de cliente, estados de la petición y PDF de oferta.

## Puesta en marcha

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
# http://localhost:8000
pytest -q
```

La primera arrancada crea la base de datos y carga la máquina de ejemplo, materiales y parámetros económicos.

**DWG**: instala LibreDWG (`dwg2dxf`) en el servidor, o exporta a DXF. Sin conversor, el portal lo indica al subir.

> Los datos de la máquina de ejemplo son orientativos (orden de magnitud de tablas de fibra 6 kW).
> Ajústalos con los valores reales del taller desde el formulario de máquina.
