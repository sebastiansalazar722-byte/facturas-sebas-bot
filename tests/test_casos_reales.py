# -*- coding: utf-8 -*-
"""CASOS REALES: facturas verdaderas (XML/PDF/ZIP) pasadas por el bot completo.

Cada caso es una carpeta dentro de tests/casos_reales/ con:
  - los adjuntos originales del correo (.xml, .pdf o .zip), sin modificar;
  - un archivo esperado.json, por ejemplo:
        {
          "asunto": "901173460;EFFISYSTEMS S.A.S.;FE94506;01;EFFISYSTEMS S.A.S.;",
          "cuerpo": "Adjunto documento.",
          "factura": "FE 94506",
          "proveedor": "EFFISYSTEMS S.A.S.",
          "valor": 100000
        }
    "asunto" es obligatorio. "cuerpo" es opcional. De "factura", "proveedor"
    y "valor" solo se comparan los que estén escritos.

Esa carpeta está excluida de git (el repo es público): las facturas reales
se quedan solo en tu computador. Sin casos, estas pruebas se saltan.
"""
import json
from pathlib import Path

import pytest

from conftest import correo

CARPETA = Path(__file__).resolve().parent / "casos_reales"
CASOS = sorted(p.parent for p in CARPETA.glob("*/esperado.json"))


@pytest.mark.skipif(not CASOS, reason="No hay casos reales en tests/casos_reales/ (ver LEEME.md)")
@pytest.mark.parametrize("caso", CASOS or [None], ids=[c.name for c in CASOS] or ["sin-casos"])
def test_caso_real(caso, escanear):
    esperado = json.loads((caso / "esperado.json").read_text(encoding="utf-8"))
    adjuntos = [
        (archivo.name, archivo.read_bytes())
        for archivo in sorted(caso.iterdir())
        if archivo.suffix.lower() in (".xml", ".pdf", ".zip")
    ]
    crudo = correo(esperado["asunto"], esperado.get("cuerpo", "Adjunto documento."), adjuntos)
    _, _, filas = escanear([crudo])

    assert filas, "El bot no registró ninguna factura para este correo"
    fila = filas[0]
    for campo in ("factura", "proveedor", "valor"):
        if campo in esperado:
            assert fila[campo] == esperado[campo], f"{campo}: obtuvo {fila[campo]!r}, se esperaba {esperado[campo]!r}"
