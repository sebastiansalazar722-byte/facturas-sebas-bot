# Pruebas del bot de facturas

No tocan Gmail, Google Sheets, Postgres ni `facturas.db`: usan un correo
falso, una hoja falsa y una base temporal. Se pueden correr cuantas veces
se quiera.

## Cómo correrlas

```
pip install -r requirements-dev.txt
python -m pytest tests -v
```

Hazlo sin PyMuPDF instalado (o no importa si lo está: las pruebas lo
bloquean) porque producción lee los PDF solo con `pypdf`.

## Qué hay

| Archivo | Qué protege | Resultado esperado hoy |
|---|---|---|
| `test_regresion.py` | Lo que ya funciona (casos A–G de CLAUDE.md, estructura, XML, flujo, hash de la columna D) | todas `passed` |
| `test_bugs_conocidos.py` | Bugs encontrados en la auditoría, escritos con el resultado correcto | todas `xfailed` o `skipped` |
| `test_casos_reales.py` | Facturas reales que pongas en `casos_reales/` | `skipped` hasta que agregues casos |

## Cómo leer el resultado

- `passed`: bien.
- `failed` en `test_regresion.py`: el último cambio rompió algo que servía. No se ajusta la prueba; se revisa el cambio.
- `xfailed`: bug conocido que sigue sin arreglar. No es un error.
- `XPASS(strict)` / `failed` en `test_bugs_conocidos.py`: el bug quedó arreglado. Se quita la marca `@bug(...)` y la prueba se pasa a `test_regresion.py`.
- `skipped`: decisión de negocio pendiente o falta un caso real.

## Regla de trabajo

Antes de cada commit que toque el bot:

```
python -m py_compile factura_bot_google_sheets_v11_render.py
python -m pytest tests -q
```
