# Piezas de ejemplo

Planos DXF sencillos para probar el portal (regenerables con `python ejemplos/generar_ejemplos.py`).

| Fichero | Qué prueba |
|---|---|
| 01_placa_anclaje_250x250_R2018 | Placa con esquinas redondeadas, 5 taladros y cotas/textos que deben ignorarse |
| 02_escuadra_L_colisos_R2000 | Contorno hecho con líneas y arcos sueltos que el motor une; 2 colisos |
| 03_brida_D150_pulgadas_R2000 | Plano dibujado en pulgadas: debe salir Ø150 mm |
| 04_rueda_dentada_Z24_R2010 | Contorno complejo (24 dientes) y chavetero |
| 05_tapa_curva_spline_R2018 | Contorno curvo (spline) |
| 06_chapa_perforada_bloques_R2018 | 40 taladros insertados como bloque |
| 07_tres_arandelas_binario | DXF binario con 3 piezas en el mismo plano |
| 08_pieza_con_errores_R2018 | Contorno abierto, línea duplicada y taladro Ø3 (avisos) |
| 09_soporte_trapecio_R12 | Formato antiguo R12, sin unidades |

DWG: el portal los convierte con LibreDWG (`dwg2dxf`). Indica su ruta en `.env` con `PORTAL_DWG_CONVERTER=C:\ruta\dwg2dxf.exe`.
