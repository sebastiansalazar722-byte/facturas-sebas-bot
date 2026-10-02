# -*- coding: utf-8 -*-
"""BUGS CONOCIDOS: comportamiento que HOY está mal, encontrado en la auditoría.

Cada prueba describe el resultado CORRECTO y está marcada como xfail estricto:
  - hoy falla, y pytest la reporta como "xfailed" (esperado, no rompe la suite);
  - el día que se arregle el bug, pytest la reportará como error "XPASS(strict)".
    Ese es el aviso para quitar la marca xfail y pasarla a test_regresion.py.

Las marcadas con skip son decisiones de negocio pendientes, no bugs claros.
"""
import pytest

from conftest import correo, xml_attached, xml_factura

RELLENOS = "43048845;RELLENOS Y FIBRAS;FE10647;01;LUZ EDIT GIL DUQUE"


def bug(razon):
    return pytest.mark.xfail(strict=True, reason=razon)


# -------------------------------------------------------------------- proveedor
PDF_RELLENOS = """RELLENOS Y FIBRAS
LUZ EDIT GIL DUQUE
NIT 43048845-1
FACTURA ELECTRÓNICA DE VENTA FE 10647
Cliente
SEBAS DUNCAN SAS
NIT 901234567
TOTAL A PAGAR 1.230.000"""


@bug("El comprador (SEBAS DUNCAN SAS) queda como proveedor: en hoja1 pasa en 24 de 62 filas.")
def test_proveedor_del_pdf_no_es_el_comprador(bot):
    assert "SEBAS DUNCAN" not in bot.extract_supplier_name_from_text(PDF_RELLENOS).upper()


@bug("Entre dos nombres con 'SAS' gana el primero en orden alfabético, no el vendedor.")
def test_proveedor_del_pdf_no_depende_del_orden_alfabetico(bot):
    texto = PDF_RELLENOS.replace("RELLENOS Y FIBRAS\nLUZ EDIT GIL DUQUE", "TEXTIFILH SAS")
    assert bot.extract_supplier_name_from_text(texto) == "TEXTIFILH SAS"


@bug("La razón social de un proveedor técnico no está en la lista (fila CALIPLASTICOS en hoja1).")
def test_razon_social_de_proveedor_tecnico(bot):
    assert bot._looks_like_technical_provider("Sistemas de Informacion Empresarial S.A.S.")


@bug("En un AttachedDocument el proveedor no se lee del XML (solo se lee el valor).")
def test_attached_document_proveedor(bot):
    assert bot.extract_supplier_name_from_xml_bytes(xml_attached(proveedor="EFFISYSTEMS S.A.S.")) == "EFFISYSTEMS S.A.S."


# --------------------------------------------------------------- número de factura
@bug("En un AttachedDocument el número no se lee del XML (solo se lee el valor).")
def test_attached_document_numero(bot):
    assert bot.extract_invoice_number_from_xml_bytes(xml_attached(numero="FE94506")) == "FE 94506"


@bug("CLAUDE.md trata FE-94506 y FE94506 como la misma factura; el código deja el guion.")
def test_normaliza_numero_con_guion(bot):
    assert bot._normalize_invoice_candidate("FE-94506") == "FE 94506"


@bug("Asunto largo partido en dos líneas (\\r\\n): la estructura no se detecta y la factura queda N/A.")
def test_estructura_con_asunto_partido(escanear):
    partido = (b"900418527;IMPORTADORA DE INSUMOS EL MAYORISTA\r\n"
               b" SAS;E4MD56131511;01;IMPORTADORA DE INSUMOS EL MAYORISTA SAS")
    _, _, filas = escanear([correo("", "Adjunto documento. Total a pagar $ 340.000", asunto_crudo=partido)])
    assert filas and filas[0]["factura"] == "E4MD56131511"


@bug("La estructura no valida el tercer campo: cualquier texto entra como número de factura.")
def test_estructura_falso_positivo(escanear):
    insertadas, _, _ = escanear([correo("Pedido 1234567; Juan Perez; enviado", "Tu pedido llega mañana. Valor $50.000")])
    assert insertadas == 0


# ------------------------------------------------------------------------- valor
@bug("Un 'total' escrito en el cuerpo del correo le gana al total legal del XML.")
def test_valor_del_xml_gana_sobre_el_cuerpo_del_correo(escanear):
    xml = xml_factura(numero="FE10647", proveedor="RELLENOS Y FIBRAS", total="180000.00")
    _, _, filas = escanear([correo("Factura FE10647", "Total a pagar $2.840 por flete.", [("fv.xml", xml)])])
    assert filas[0]["valor"] == 180000


PDF_EN_LINEAS = "SUBTOTAL\n229.800\nIVA\n43.662\nTotal líneas o ítems: 1\nTOTAL DE LA OPERACIÓN\n273.462\nNIT 900.123.456-7\n"


@bug("Si la etiqueta y el valor quedan en líneas distintas, no se reconoce TOTAL DE LA OPERACIÓN.")
def test_pdf_total_con_etiqueta_y_valor_en_lineas_distintas(bot):
    mejor = bot.find_best_total_by_labels(PDF_EN_LINEAS)
    assert mejor is not None and mejor[0] == 273462


@bug("Sin etiqueta reconocida se toma el número más grande del PDF, que puede ser un NIT.")
def test_pdf_respaldo_no_toma_un_nit_como_valor(bot):
    valores = bot.extract_invoice_values(PDF_EN_LINEAS)
    assert max(v[0] for v in valores) == 273462


@bug("'Total: 1' se acepta como valor (fila EFFISYSTEMS con valor 1 en hoja1).")
def test_un_total_de_1_no_es_valor_de_factura(bot):
    assert bot.find_best_total_by_labels("Total: 1") is None


# ------------------------------------------------------------------ deduplicación
@bug("Dos facturas distintas sin número y con igual valor en la misma corrida: la segunda se pierde.")
def test_dos_facturas_sin_numero_con_el_mismo_valor(escanear):
    a = correo("Invoice de agosto", "Total a pagar $ 50.000", remitente="Proveedor A <a@a.test>")
    b = correo("Invoice de agosto", "Total a pagar $ 50.000", remitente="Proveedor B <b@b.test>")
    insertadas, _, _ = escanear([a, b])
    assert insertadas == 2


# ----------------------------------------------------------------------- Análisis
@bug("Análisis se borra y se rehace solo con lo que hay en la base de datos: "
     "si la base tiene solo los registros de hoy, se pierde la historia de la pestaña.")
def test_analisis_no_pierde_la_historia_de_hoja1(bot, sheets_falso):
    cfg, hoja1, analisis = sheets_falso
    hoja1.filas = [["RELLENOS Y FIBRAS", "Tue, 01 Sep 2026 19:48:02 +0000", "FE 10392", "hash-1", "820000", "x", "factura"]]
    hoy = bot.InvoiceRecord(
        message_id="<hoy@correo.test>", invoice_number="PV 15701", email_date="Thu, 01 Oct 2026 16:18:11 +0000",
        stored_date="2026-10-01", remitente_fijo="sebas duncan sas", remitente_real="Plastisol sas",
        asunto="x", valor_entero=456000, moneda="COP", palabra_clave="factura", uid="1",
    )
    bot.sync_to_google_sheets(cfg, [hoy])
    assert any("Sep" in str(celda) for celda in analisis.filas[0])


# ------------------------------------------------- decisiones de negocio pendientes
@pytest.mark.skip(reason="DECISIÓN PENDIENTE: ¿cómo registrar notas crédito (tipo 91)? "
                         "Hoy entran como factura positiva y se anotan a mano en la columna H.")
def test_nota_credito_tipo_91():
    pass


@pytest.mark.skip(reason="DECISIÓN PENDIENTE: un reenvío estructurado válido cuyo cuerpo diga 'DIAN' "
                         "se descarta por la exclusión. CLAUDE.md pone exclusiones antes de la estructura.")
def test_estructura_valida_con_la_palabra_dian():
    pass


@pytest.mark.skip(reason="DECISIÓN PENDIENTE: facturas en USD se suman como COP en Análisis "
                         "(la moneda no se escribe en la hoja).")
def test_facturas_en_dolares():
    pass
