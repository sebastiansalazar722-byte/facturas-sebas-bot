# -*- coding: utf-8 -*-
"""PENDIENTES: comportamientos que dependen de una decisión de negocio.

Los bugs encontrados en la auditoría ya están arreglados y sus pruebas viven
en test_regresion.py. Aquí queda lo que todavía no se ha decidido cómo debe
funcionar. Cuando se decida, se escribe la prueba con el resultado esperado.

Si aparece un bug nuevo, se anota aquí con la marca @bug(...): la prueba
describe el resultado CORRECTO, hoy falla ("xfailed") y el día que se arregle
pytest avisa con "XPASS(strict)" para pasarla a test_regresion.py.
"""
import pytest


def bug(razon):
    return pytest.mark.xfail(strict=True, reason=razon)


@pytest.mark.skip(reason="DECISIÓN PENDIENTE: ¿cómo registrar notas crédito (tipo 91)? "
                         "Hoy entran como factura positiva y se anotan a mano en la columna H.")
def test_nota_credito_tipo_91():
    pass


@pytest.mark.skip(reason="DECISIÓN PENDIENTE: un reenvío estructurado válido cuyo cuerpo diga 'DIAN' "
                         "se descarta por la exclusión. CLAUDE.md pone exclusiones antes de la estructura.")
def test_estructura_valida_con_la_palabra_dian():
    pass


@pytest.mark.skip(reason="DECISIÓN PENDIENTE: la misma factura puede entrar dos veces si llega en dos "
                         "correos con valores distintos (caso FE 10148: 820.000 y 802.773). ¿Cuál valor manda?")
def test_misma_factura_con_dos_valores():
    pass
