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

- **Acceso con usuario y contraseña** en todas las páginas. Contraseñas en hash scrypt con sal (nunca en claro,
  ni en el repositorio), sesiones en servidor con cookie `HttpOnly`/`SameSite`, caducidad a las 12 h,
  bloqueo de 15 min tras 5 intentos fallidos y mismo mensaje para usuario inexistente o clave errónea.
  Usuarios gestionados con `python -m app.usuarios crear|listar|desactivar`.
  La contraseña creada por script es provisional: en el primer acceso el portal obliga a cambiarla
  (con indicador de fortaleza y botón «Sugerir contraseña segura»). También se puede cambiar desde el menú. En producción con HTTPS: `PORTAL_COOKIE_SECURE=true`.
- El plano original **no se guarda**: solo métricas, huella SHA-256, la vista previa SVG y, para el anidado, los
  contornos de corte como polilíneas (sin capas, textos, cotas, cajetín ni metadatos del fichero). Se borran con la
  petición (RGPD) y solo son accesibles con sesión iniciada.
- Validación de subida por extensión, firma del contenido y tamaño máximo.
- Cabeceras CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`; rechazo de POST de otro origen (CSRF básico).
- Identificadores de pieza no predecibles (UUID).

Pendiente para fases siguientes: roles y 2FA para administración, cifrado en reposo,
registro de auditoría, política de retención RGPD, portal de cliente, estados de la petición y PDF de oferta.

## Peticiones de clientes y ofertas

- **Zona pública** (`/solicitar`, sin login): el cliente deja sus datos y sus piezas (plano DXF/DWG, material, espesor,
  cantidad, quién pone la chapa). Se validan los planos al momento, se pide consentimiento RGPD y hay protección
  antispam (campo trampa + límite por IP). Recibe un número de petición y un **enlace privado** (`/solicitud/<token>`)
  para ver el estado; nunca ve máquinas, tiempos, costes ni márgenes.
- **Bandeja interna** (`/peticiones`): el portal calcula cada línea con la máquina y gas por defecto, se puede cambiar
  máquina/gas, ajustar el precio, las condiciones y notas internas. «Generar oferta» crea el PDF (`O-AAAA-NNNN`),
  renueva el enlace del cliente y, si hay SMTP, lo envía por correo con el PDF adjunto.
- **Cliente**: descarga el PDF y acepta, pide cambios o rechaza desde su enlace. Las ofertas caducan solas.
- **RGPD**: supresión completa de una petición y sus piezas desde la ficha interna. Los tokens de acceso se guardan
  solo como hash.
- **Empresa** (`/empresa`): razón social, CIF, dirección y condiciones por defecto que salen en el PDF.

## Anidado de chapa

- **Automático** (`/anidado`): anidado por forma real (las piezas pequeñas pueden ir dentro de los huecos de las
  grandes), giros de 90° y separación mínima garantizada entre siluetas y al borde. Desde una petición, «Anidar chapa»
  crea un anidado por cada material y espesor.
- **Separación estándar según espesor** (`/anidado/ajustes`): tabla editable espesor máx. → separación y borde
  (de partida: ≤1 mm 2/5 · ≤3 mm 3/5 · ≤5 mm 5/8 · ≤8 mm 8/10 · ≤12 mm 12/12, +1 mm en inox/aluminio desde 6 mm).
  Formatos de chapa (3000×1500, 2500×1250, 2000×1000 y los que se añadan).
- **Editor manual**: arrastrar, girar, quitar, añadir pendientes, mover entre chapas. El servidor comprueba cada
  cambio y marca en rojo solapes, separaciones insuficientes o piezas fuera del borde útil.
- **Retales** (`/retales`): se usan primero los retales disponibles del mismo material y espesor. Al confirmar un
  anidado, los retales usados salen del almacén y los sobrantes aprovechables (lado ≥ retal mínimo) se dan de alta.
- **Coste**: si la petición tiene anidado sin conflictos, el material de la oferta se calcula con la chapa realmente
  consumida (sin los sobrantes que vuelven a almacén) en lugar de la estimación por envolvente.
- **Exportar DXF**: chapas en fila con capas CHAPA, CORTE_EXT, CORTE_INT, RETAL y TEXTO, en mm. El CAM de la máquina
  sigue haciendo el programa definitivo (entradas, microuniones, orden de corte).

Correo (opcional) en `.env`: `PORTAL_SMTP_HOST`, `PORTAL_SMTP_PORT`, `PORTAL_SMTP_USER`, `PORTAL_SMTP_PASSWORD`,
`PORTAL_SMTP_FROM`, `PORTAL_NOTIFY_EMAIL` y `PORTAL_PUBLIC_BASE_URL` (dirección pública del portal para los enlaces).

## Puesta en marcha

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
# Crear los usuarios (pide la contraseña sin mostrarla; solo se guarda su hash)
python -m app.usuarios crear unioncad@unioncad.es
python -m app.usuarios crear dramajo@unioncad.es
uvicorn app.main:app --reload
# http://localhost:8000
pytest -q
```

La primera arrancada crea la base de datos y carga la máquina de ejemplo, materiales y parámetros económicos.

**DWG**: instala LibreDWG (`dwg2dxf`) en el servidor, o exporta a DXF. Sin conversor, el portal lo indica al subir.

> Los datos de la máquina de ejemplo son orientativos (orden de magnitud de tablas de fibra 6 kW).
> Ajústalos con los valores reales del taller desde el formulario de máquina.
