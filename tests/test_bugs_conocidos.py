# -*- coding: utf-8 -*-
"""BUGS CONOCIDOS Y PENDIENTES.

Hoy no hay ninguno: los bugs de la auditoría y las decisiones de negocio
(notas crédito, exclusión por DIAN, facturas repetidas, dólares) ya están
resueltos y sus pruebas viven en test_regresion.py.

Si aparece un bug nuevo, se anota aquí con la marca @bug(...): la prueba
describe el resultado CORRECTO, hoy falla ("xfailed") y el día que se arregle
pytest avisa con "XPASS(strict)" para pasarla a test_regresion.py.
"""
import pytest


def bug(razon):
    return pytest.mark.xfail(strict=True, reason=razon)
