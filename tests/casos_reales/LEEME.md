# Casos reales

Aquí van las facturas verdaderas que protegen al bot contra regresiones.
Todo lo que pongas en esta carpeta queda fuera de git (el repo es público).

Por cada factura, crea una carpeta y copia dentro:

1. Los adjuntos originales del correo (`.xml`, `.pdf` o `.zip`), sin cambiarlos.
2. Un `esperado.json` con el asunto exacto del correo y el resultado correcto:

```json
{
  "asunto": "901173460;EFFISYSTEMS S.A.S.;FE94506;01;EFFISYSTEMS S.A.S.;",
  "factura": "FE 94506",
  "proveedor": "EFFISYSTEMS S.A.S.",
  "valor": 100000
}
```

Casos que conviene tener primero (los de CLAUDE.md sección 10):

| Carpeta | Factura | Valor |
|---|---|---|
| `effisystems-fe94506` | FE 94506 | 100000 |
| `intercomercio-feem2586` | FEEM 2586 | 273462 |
| `elpunto-fepi40716` | FEPI 40716 | 81000 |
| `rellenos-fe10647` | FE 10647 | 1230000 |
| `plastisol-pv15077` | PV 15077 | 468000 |
| `textifilh-fe622716` | FE 622716 | 2153781 |

Si un caso real falla hoy por el proveedor (bug conocido), deja fuera la
línea `"proveedor"` del JSON hasta que se arregle; factura y valor se
siguen protegiendo.
