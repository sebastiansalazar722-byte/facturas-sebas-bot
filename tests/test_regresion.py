# -*- coding: utf-8 -*-
"""REGRESIÓN: comportamiento que HOY funciona y debe conservarse.

Si una de estas pruebas falla después de un cambio, el cambio rompió algo
que ya servía. No se ajusta la prueba: se revisa el cambio.

Los XML y textos de PDF son sintéticos (armados a mano con la misma forma
que los reales). Los documentos reales se prueban en test_casos_reales.py.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import ImapFalso, correo, xml_attached, xml_factura, zip_con

RELLENOS = "43048845;RELLENOS Y FIBRAS;FE10647;01;LUZ EDIT GIL DUQUE"
PLASTISOL = "800074822;Plastisol sas;PV15077;1;Plastisol SAS"
TEXTIFILH = "901529331;TEXTIFILH SAS;FE622716;01;TEXTIFILH SAS"


# ---------------------------------------------------------------- normalización
@pytest.mark.parametrize("crudo, esperado", [
    ("FE10647", "FE 10647"),
    ("PV15077", "PV 15077"),
    ("FE622716", "FE 622716"),
    ("FE94506", "FE 94506"),
    ("EMD326070635", "EMD 326070635"),
    ("FEPI 40716", "FEPI 40716"),
    ("feem2586", "FEEM 2586"),
])
def test_normaliza_numero_de_factura(bot, crudo, esperado):
    assert bot._normalize_invoice_candidate(crudo) == esperado


@pytest.mark.parametrize("candidato", ["1", "01", "2840", "901529331", "43048845", "CUFE123", "NIT900"])
def test_rechaza_numeros_que_no_son_factura(bot, candidato):
    assert bot._is_bad_invoice_candidate(candidato)


# ------------------------------------------------ estructura NIT;PROVEEDOR;FACTURA
@pytest.mark.parametrize("linea, esperado", [
    (RELLENOS, ("FE 10647", "RELLENOS Y FIBRAS")),      # CLAUDE.md caso B
    (PLASTISOL, ("PV 15077", "Plastisol sas")),          # CLAUDE.md caso C
    (TEXTIFILH, ("FE 622716", "TEXTIFILH SAS")),         # CLAUDE.md caso D
    ("901173460;EFFISYSTEMS S.A.S.;FE94506;01;EFFISYSTEMS S.A.S.;", ("FE 94506", "EFFISYSTEMS S.A.S.")),
    ("830080489;INTERCOMERCIO JAO SAS;FEEM2586;01;INTERCOMERCIO JAO SAS;", ("FEEM 2586", "INTERCOMERCIO JAO SAS")),
    ("901472531;EL PUNTO DE LOS INSUMOS SAS;FEPI40716;01;EL PUNTO DE LOS INSUMOS SAS;",
     ("FEPI 40716", "EL PUNTO DE LOS INSUMOS SAS")),
])
def test_estructura_en_asunto(bot, linea, esperado):
    assert bot.extract_structured_email_invoice_and_supplier(linea, "") == esperado


def test_estructura_en_cuerpo(bot):
    assert bot.extract_structured_email_invoice_and_supplier("Fwd", RELLENOS) == ("FE 10647", "RELLENOS Y FIBRAS")


@pytest.mark.parametrize("asunto", [
    "Factura de venta de septiembre",
    "Hola; qué tal; bien",                    # sin NIT
    "123;AB;FE1",                             # NIT demasiado corto
    "900123456;ATEB COFIDI4;FE100;01",        # proveedor técnico, no vendedor
    "900123456;12345678;FE100;01",            # "proveedor" que es solo números
])
def test_estructura_no_acepta_lineas_invalidas(bot, asunto):
    assert bot.extract_structured_email_invoice_and_supplier(asunto, "") == ("", "")


# --------------------------------------------------------------------- valor XML
def test_xml_usa_total_legal_y_no_el_valor_de_linea(bot):
    """CLAUDE.md caso G: línea de 2840 y LegalMonetaryTotal/PayableAmount de 180000."""
    valores = bot.extract_values_from_xml_bytes(xml_factura(total="180000.00", linea="2840.00"))
    assert [v[0] for v in valores] == [180000]


def test_xml_attached_document_con_factura_en_cdata(bot):
    """CLAUDE.md caso A: AttachedDocument con la Invoice embebida en CDATA."""
    valores = bot.extract_values_from_xml_bytes(xml_attached(total="100000.00"))
    assert [v[0] for v in valores] == [100000]


def test_xml_montos_con_decimales_ubl(bot):
    valores = bot.extract_values_from_xml_bytes(xml_factura(total="100000.0000"))
    assert valores[0][0] == 100000 and valores[0][1] == "COP"


def test_xml_sin_total_legal_usa_payable_de_la_factura(bot):
    valores = bot.extract_values_from_xml_bytes(xml_factura(total="81000.00", con_total_legal=False))
    assert valores[0][0] == 81000


def test_xml_invalido_no_revienta(bot):
    assert bot.extract_values_from_xml_bytes(b"esto no es xml") == []
    assert bot.extract_invoice_number_from_xml_bytes(b"esto no es xml") == ""
    assert bot.extract_supplier_name_from_xml_bytes(b"esto no es xml") == ""


# ----------------------------------------------------- número y proveedor del XML
def test_xml_directo_numero_y_proveedor(bot):
    xml = xml_factura(numero="FE94506", proveedor="EFFISYSTEMS S.A.S.")
    assert bot.extract_invoice_number_from_xml_bytes(xml) == "FE 94506"
    assert bot.extract_supplier_name_from_xml_bytes(xml) == "EFFISYSTEMS S.A.S."


@pytest.mark.parametrize("nombre, es_tecnico", [
    ("ATEB", True), ("COFIDI4", True), ("Siigo", True), ("FACTURATECH SA", True),
    ("EL PUNTO DE LOS INSUMOS SAS", False), ("RELLENOS Y FIBRAS", False),
])
def test_proveedor_tecnico(bot, nombre, es_tecnico):
    """CLAUDE.md sección 9: ATEB/COFIDI4 no es el vendedor."""
    assert bot._looks_like_technical_provider(nombre) is es_tecnico


# ------------------------------------------------------------- totales en PDF/texto
def test_pdf_total_neto_effisystems(bot):
    """CLAUDE.md caso F."""
    texto = "Total items: 1\nSUBTOTAL $84,034\nIVA $15,966\nTOTAL NETO $100,000\n"
    assert bot.find_best_total_by_labels(texto)[0] == 100000


def test_pdf_total_de_la_operacion_en_la_misma_linea(bot):
    """CLAUDE.md caso E, cuando etiqueta y valor salen en la misma línea."""
    texto = "SUBTOTAL 229.800\nIVA 43.662\nTotal líneas o ítems: 1\nTOTAL DE LA OPERACIÓN COP 273.462\n"
    assert bot.find_best_total_by_labels(texto)[0] == 273462


def test_pdf_conteos_no_son_total(bot):
    texto = "Total items: 1\nTotal líneas o ítems: 1\nTotal unidades: 3"
    assert bot.find_best_total_by_labels(texto) is None


def test_pdf_total_a_pagar_gana_sobre_valor_total_de_linea(bot):
    texto = "Cant Descripción Valor total\n1 Plan 84.034\nSUBTOTAL 84.034\nTOTAL A PAGAR 100.000"
    assert bot.find_best_total_by_labels(texto)[0] == 100000


@pytest.mark.parametrize("crudo, esperado", [
    ("$100,000", 100000), ("273.462", 273462), ("81.000", 81000),
    ("100.000,00", 100000), ("100,000.00", 100000), ("$ 1.230.000", 1230000),
])
def test_limpieza_de_valores(bot, crudo, esperado):
    assert bot.clean_currency_to_int(crudo) == esperado


# ------------------------------------------------------- número de factura en texto
@pytest.mark.parametrize("texto, esperado", [
    ("FACTURA ELECTRÓNICA DE VENTA FEPI 40716", "FEPI 40716"),
    ("FACTURA ELECTRÓNICA DE VENTA NÚMERO: EMD326070635", "EMD 326070635"),
    ("Factura No. FE 94506", "FE 94506"),
])
def test_numero_de_factura_en_texto(bot, texto, esperado):
    assert bot.extract_invoice_number(texto) == esperado


def test_rango_de_autorizacion_no_es_factura(bot):
    assert bot.extract_invoice_number("Resolución DIAN 18764 autoriza rango FE 1 hasta FE 100000") == ""


# ------------------------------------------------- flujo completo _scan_and_store()
def test_flujo_estructura_sin_keyword_se_acepta(escanear):
    """CLAUDE.md sección 5: el reenvío estructurado entra aunque no diga 'factura'."""
    xml = xml_factura(numero="FE10647", proveedor="RELLENOS Y FIBRAS", total="1230000.00")
    insertadas, _, filas = escanear([correo(RELLENOS, adjuntos=[("fv.xml", xml)])])
    assert insertadas == 1
    assert filas[0]["factura"] == "FE 10647"
    assert filas[0]["proveedor"] == "RELLENOS Y FIBRAS"
    assert filas[0]["valor"] == 1230000
    assert filas[0]["keyword"] == "factura"


def test_flujo_effisystems_attached_document(escanear):
    """CLAUDE.md caso A, con XML sintético: FE 94506 por 100000."""
    asunto = "901173460;EFFISYSTEMS S.A.S.;FE94506;01;EFFISYSTEMS S.A.S.;"
    xml = xml_attached(numero="FE94506", proveedor="EFFISYSTEMS S.A.S.", total="100000.00")
    _, _, filas = escanear([correo(asunto, adjuntos=[("ad.xml", xml)])])
    assert (filas[0]["factura"], filas[0]["proveedor"], filas[0]["valor"]) == (
        "FE 94506", "EFFISYSTEMS S.A.S.", 100000)


def test_flujo_sin_keyword_ni_estructura_se_ignora(escanear):
    insertadas, excluidas, filas = escanear([correo("Reunión del lunes", "Nos vemos. Costo $50.000")])
    assert (insertadas, excluidas, filas) == (0, 0, [])


def test_flujo_keyword_en_cuerpo_y_xml(escanear):
    xml = xml_factura(numero="FEPI40716", proveedor="EL PUNTO DE LOS INSUMOS SAS", total="81000.00")
    _, _, filas = escanear([correo("Documento electrónico", "Adjuntamos su factura.", [("fv.xml", xml)])])
    assert (filas[0]["factura"], filas[0]["proveedor"], filas[0]["valor"]) == (
        "FEPI 40716", "EL PUNTO DE LOS INSUMOS SAS", 81000)


def test_flujo_xml_dentro_de_zip(escanear):
    xml = xml_factura(numero="FE622716", proveedor="TEXTIFILH SAS", total="2153781.00")
    _, _, filas = escanear([correo(TEXTIFILH, adjuntos=[("docs.zip", zip_con("fv.xml", xml))])])
    assert (filas[0]["factura"], filas[0]["valor"]) == ("FE 622716", 2153781)


def test_flujo_numero_del_adjunto_gana_sobre_el_asunto(escanear):
    """Prioridad de factura: adjunto > estructura > extractor tradicional."""
    xml = xml_factura(numero="FE99999", proveedor="RELLENOS Y FIBRAS", total="820000.00")
    _, _, filas = escanear([correo(RELLENOS, adjuntos=[("fv.xml", xml)])])
    assert filas[0]["factura"] == "FE 99999"


def test_flujo_sin_adjuntos_usa_estructura(escanear):
    """Sin XML/PDF: factura y proveedor salen del asunto estructurado."""
    _, _, filas = escanear([correo(RELLENOS, "Valor de la compra. Total a pagar $ 820.000")])
    assert (filas[0]["factura"], filas[0]["proveedor"], filas[0]["valor"]) == (
        "FE 10647", "RELLENOS Y FIBRAS", 820000)


def test_flujo_sin_estructura_ni_adjunto_usa_remitente(escanear):
    _, _, filas = escanear([correo("Invoice for 08/2026", "Total a pagar $ 50.000",
                                   remitente="Proveedor Demo <cobros@demo.test>")])
    assert filas[0]["proveedor"] == "Proveedor Demo"
    assert filas[0]["keyword"] == "invoice"


def test_flujo_excluye_correos_propios_e_informes(escanear):
    propio = correo("Factura FE1", "Total a pagar $ 10.000", remitente="Bot <bot@sebasduncan.test>")
    informe = correo("informe de facturas sas", "Suma total de las facturas: 100.000")
    assert escanear([propio, informe])[0] == 0


def test_flujo_termino_excluido(escanear):
    insertadas, excluidas, _ = escanear([correo("Factura: su token de acceso", "Total a pagar $ 10.000")])
    assert (insertadas, excluidas) == (0, 1)


def test_flujo_proveedor_especial_no_se_excluye(escanear):
    """VISION PERSPECTIVA (901734728) entra aunque el correo mencione DIAN."""
    asunto = "901734728;VISION PERSPECTIVA SAS;FEVP268;01;VISION PERSPECTIVA SAS;"
    xml = xml_factura(numero="FEVP268", proveedor="VISION PERSPECTIVA SAS", total="952000.00")
    insertadas, excluidas, filas = escanear([correo(asunto, "Factura validada por la DIAN.", [("fv.xml", xml)])])
    assert (insertadas, excluidas) == (1, 0)
    assert (filas[0]["factura"], filas[0]["valor"]) == ("FEVP 268", 952000)


def test_flujo_fuera_del_rango_de_fechas_se_ignora(escanear):
    viejo = correo(RELLENOS, "Total a pagar $ 820.000", fecha="Mon, 03 Aug 2026 10:00:00 -0500")
    assert escanear([viejo])[0] == 0


def test_flujo_segunda_pasada_no_duplica(escanear):
    xml = xml_factura(numero="FE10647", proveedor="RELLENOS Y FIBRAS", total="1230000.00")
    crudo = correo(RELLENOS, adjuntos=[("fv.xml", xml)])
    assert escanear([crudo])[0] == 1
    insertadas, _, filas = escanear([crudo])
    assert insertadas == 0 and len(filas) == 1


def test_flujo_un_correo_roto_no_detiene_los_demas(escanear):
    xml = xml_factura(numero="FE10647", proveedor="RELLENOS Y FIBRAS", total="1230000.00")
    roto = correo("Factura FE1", "Total a pagar $ 10.000", adjuntos=[("mala.zip", b"no soy un zip")])
    bueno = correo(RELLENOS, adjuntos=[("fv.xml", xml)])
    _, _, filas = escanear([roto, bueno])
    assert "FE 10647" in [f["factura"] for f in filas]


# ----------------------------------------------------------------- Google Sheets
def test_clave_columna_d_es_estable(bot):
    """El hash de la columna D es lo único que evita filas duplicadas en hoja1.

    Si este valor cambia, TODA la hoja se duplicaría en la siguiente corrida.
    """
    assert bot._sheet_message_key("<prueba-001@correo.test>", "FE 10647", 1230000) == \
        "22e2c281e669f1057e7f00df237d184c"


def _registro(bot, message_id="<a@correo.test>", factura="FE 10647", valor=1230000,
              proveedor="RELLENOS Y FIBRAS", fecha="Tue, 29 Sep 2026 12:53:59 +0000"):
    return bot.InvoiceRecord(
        message_id=message_id, invoice_number=factura, email_date=fecha, stored_date="2026-09-29",
        remitente_fijo="sebas duncan sas", remitente_real=proveedor, asunto="asunto de prueba",
        valor_entero=valor, moneda="COP", palabra_clave="factura", uid="1",
    )


def test_sheets_fila_con_columnas_a_g(bot, sheets_falso):
    cfg, hoja1, _ = sheets_falso
    hoja1.filas = [list(bot.SHEET_HEADERS), ["X", "f", "FE 1", "hash-existente", "1", "d", "factura"]]
    bot.sync_to_google_sheets(cfg, [_registro(bot)])
    assert hoja1.filas[1] == [
        "RELLENOS Y FIBRAS", "Tue, 29 Sep 2026 12:53:59 +0000", "FE 10647",
        bot._sheet_message_key("<a@correo.test>", "FE 10647", 1230000),
        1230000, "asunto de prueba", "factura",
    ]


def test_sheets_no_duplica_filas_existentes(bot, sheets_falso):
    cfg, hoja1, _ = sheets_falso
    registros = [_registro(bot), _registro(bot, message_id="<b@correo.test>", factura="PV 15077", valor=468000)]
    assert bot.sync_to_google_sheets(cfg, registros)["inserted"] == 2
    filas_antes = len(hoja1.filas)
    assert bot.sync_to_google_sheets(cfg, registros)["inserted"] == 0
    assert len(hoja1.filas) == filas_antes


def test_sheets_factura_sin_numero_se_escribe_na(bot, sheets_falso):
    cfg, hoja1, _ = sheets_falso
    bot.sync_to_google_sheets(cfg, [_registro(bot, factura="")])
    assert hoja1.filas[-1][2] == "N/A"


HASH_VIEJO_1 = "a" * 32
HASH_VIEJO_2 = "b" * 32


def test_sheets_hoja_vacia_recibe_encabezado(bot, sheets_falso):
    cfg, hoja1, _ = sheets_falso
    bot.sync_to_google_sheets(cfg, [_registro(bot)])
    assert hoja1.filas[0] == ["Proveedor", "Fecha correo", "Factura", "ID documento", "Valor",
                              "Asunto / detalle", "Keyword", "Notas"]
    assert hoja1.filas[1][2] == "FE 10647"


def test_sheets_hoja_con_datos_y_sin_encabezado_lo_recibe_arriba(bot, sheets_falso):
    """Caso de la hoja real hoy: tiene filas pero no tiene títulos."""
    cfg, hoja1, _ = sheets_falso
    vieja = ["PROVEEDOR VIEJO", "Tue, 01 Sep 2026 15:25:09 +0000", "FE 1", HASH_VIEJO_1, "1000", "x", "factura", "NOTA A MANO"]
    hoja1.filas = [list(vieja)]
    bot.sync_to_google_sheets(cfg, [])
    assert hoja1.filas == [bot.SHEET_HEADERS, vieja]


def test_sheets_encabezado_no_se_duplica(bot, sheets_falso):
    cfg, hoja1, _ = sheets_falso
    for _ in range(3):
        bot.sync_to_google_sheets(cfg, [_registro(bot)])
    assert [f[0] for f in hoja1.filas] == ["Proveedor", "RELLENOS Y FIBRAS"]


def test_sheets_respeta_un_encabezado_escrito_a_mano(bot, sheets_falso):
    cfg, hoja1, _ = sheets_falso
    hoja1.filas = [["EMPRESA", "FECHA", "No. FACTURA", "ID", "VALOR", "DETALLE", "TIPO"]]
    bot.sync_to_google_sheets(cfg, [_registro(bot)])
    assert hoja1.filas[0][0] == "EMPRESA" and len(hoja1.filas) == 2


def test_sheets_facturas_nuevas_entran_arriba(bot, sheets_falso):
    """Las nuevas van en la fila 2; las que ya estaban bajan intactas y en su mismo orden."""
    cfg, hoja1, _ = sheets_falso
    vieja_1 = ["A", "Tue, 01 Sep 2026 15:25:09 +0000", "FE 1", HASH_VIEJO_1, "1000", "x", "factura", "NOTA A MANO"]
    vieja_2 = ["B", "Mon, 03 Aug 2026 15:38:26 +0000", "FE 2", HASH_VIEJO_2, "2000", "x", "factura"]
    hoja1.filas = [list(bot.SHEET_HEADERS), list(vieja_1), list(vieja_2)]
    bot.sync_to_google_sheets(cfg, [_registro(bot)])
    assert hoja1.filas[0] == bot.SHEET_HEADERS
    assert hoja1.filas[1][2] == "FE 10647"
    assert hoja1.filas[2:] == [vieja_1, vieja_2]


def test_sheets_lote_nuevo_queda_de_la_mas_reciente_a_la_mas_antigua(bot, sheets_falso):
    cfg, hoja1, _ = sheets_falso
    registros = [
        _registro(bot, message_id="<1@x>", factura="FE 100", fecha="Tue, 01 Sep 2026 10:00:00 +0000"),
        _registro(bot, message_id="<3@x>", factura="FE 300", fecha="Thu, 01 Oct 2026 16:18:11 +0000"),
        _registro(bot, message_id="<2@x>", factura="FE 200", fecha="Tue, 29 Sep 2026 18:00:00 -0500"),
    ]
    bot.sync_to_google_sheets(cfg, registros)
    assert [f[2] for f in hoja1.filas[1:]] == ["FE 300", "FE 200", "FE 100"]


def test_sheets_corridas_sucesivas_dejan_lo_ultimo_arriba(bot, sheets_falso):
    cfg, hoja1, _ = sheets_falso
    ayer = _registro(bot, message_id="<1@x>", factura="FE 100", fecha="Wed, 30 Sep 2026 15:00:00 +0000")
    hoy = _registro(bot, message_id="<2@x>", factura="FE 200", fecha="Thu, 01 Oct 2026 15:00:00 +0000")
    bot.sync_to_google_sheets(cfg, [ayer])
    bot.sync_to_google_sheets(cfg, [ayer, hoy])
    assert [f[2] for f in hoja1.filas[1:]] == ["FE 200", "FE 100"]


def test_sheets_desactivado_no_hace_nada(bot):
    from conftest import config_prueba
    assert bot.sync_to_google_sheets(config_prueba(), []) == {"enabled": False, "inserted": 0}


def test_analisis_suma_por_proveedor_y_mes(bot):
    registros = [
        _registro(bot, valor=820000, fecha="Tue, 01 Sep 2026 19:48:02 +0000"),
        _registro(bot, message_id="<b@x>", factura="FE 10426", valor=1230000, fecha="Fri, 04 Sep 2026 16:31:45 +0000"),
        _registro(bot, message_id="<c@x>", factura="FE 10148", valor=820000, fecha="Tue, 04 Aug 2026 13:14:24 +0000"),
    ]
    encabezados, filas = bot._analysis_data(registros)
    assert encabezados[0] == "Proveedor" and encabezados[-2:] == ["Total", "Δ mes ant"]
    assert filas[0][:4] == ["RELLENOS Y FIBRAS", 820000, 2050000, 2870000]
    assert filas[-1][0] == "TOTAL" and filas[-1][3] == 2870000


# ------------------------------------------------- ventana de la corrida diaria
CORRIDAS_UTC = (1, 13, 19)   # cron de Render: 01:00, 13:00 y 19:00 UTC


@pytest.mark.parametrize("hora_correo_utc", [0, 6, 12, 14, 18, 19, 20, 21, 22, 23])
def test_ventana_diaria_cubre_todas_las_horas(bot, hora_correo_utc):
    """Todo correo debe caer en la ventana de alguna corrida POSTERIOR a su llegada.

    Antes del arreglo, los de 19:00 a 23:59 UTC (2 pm a 7 pm Colombia) se perdían.
    """
    llegada = datetime(2026, 9, 9, hora_correo_utc, 30, tzinfo=timezone.utc)
    corridas = [datetime(2026, 9, dia, h, 0, tzinfo=timezone.utc) for dia in (9, 10) for h in CORRIDAS_UTC]
    cubierto = any(
        ahora >= llegada and inicio <= llegada <= fin
        for ahora in corridas
        for inicio, fin in [bot.build_daily_scan_range_local(ahora)]
    )
    assert cubierto


def test_ventana_diaria_es_ayer_y_hoy(bot):
    ahora = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)
    inicio, fin = bot.build_daily_scan_range_local(ahora)
    assert inicio == datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)
    assert fin == datetime(2026, 9, 10, 23, 59, 59, 999999, tzinfo=timezone.utc)


@pytest.fixture
def corrida_diaria(bot, monkeypatch, tmp_path, sheets_falso):
    """Ejecuta process_mail_once() real en modo diario, como el cron de Render.

    Cada corrida arranca con una base vacía, igual que en Render sin DATABASE_URL.
    """
    cfg, hoja1, analisis = sheets_falso
    # Como la hoja real: ya tiene filas y no tiene encabezado.
    hoja1.filas = [["PROVEEDOR VIEJO", "Tue, 01 Sep 2026 15:25:09 +0000", "FE 1", "c" * 32, "1000", "x", "factura"]]
    monkeypatch.setattr(bot, "send_summary_email", lambda *a, **k: None)
    contador = {"n": 0}

    def _correr(crudos, ahora, **opciones):
        contador["n"] += 1
        db = tmp_path / f"corrida_{contador['n']}.db"
        bot.init_db(db)
        monkeypatch.setattr(bot, "DB_PATH", db)

        class FechaFija(datetime):
            @classmethod
            def now(cls, tz=None):
                return ahora

        monkeypatch.setattr(bot, "datetime", FechaFija)
        monkeypatch.setattr(bot, "connect_imap", lambda _cfg: ImapFalso(crudos))
        return bot.process_mail_once(cfg, recheck=True, **opciones)

    return _correr, hoja1


def test_corrida_diaria_recoge_correo_de_la_tarde_anterior(corrida_diaria):
    """Correo de las 4:30 pm Colombia (21:30 UTC); la siguiente corrida es a la 01:00 UTC."""
    correr, hoja1 = corrida_diaria
    xml = xml_factura(numero="FE10647", proveedor="RELLENOS Y FIBRAS", total="1230000.00")
    crudo = correo(RELLENOS, adjuntos=[("fv.xml", xml)], fecha="Wed, 09 Sep 2026 21:30:00 +0000")
    resultado = correr([crudo], datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc))
    assert resultado["inserted_first_pass"] == 1
    assert [f[2] for f in hoja1.filas] == ["Factura", "FE 10647", "FE 1"]


def test_corridas_diarias_repetidas_no_duplican_hoja1(corrida_diaria):
    """La misma factura la ven varias corridas (ayer + hoy): hoja1 la tiene una sola vez."""
    correr, hoja1 = corrida_diaria
    xml = xml_factura(numero="FE10647", proveedor="RELLENOS Y FIBRAS", total="1230000.00")
    crudo = correo(RELLENOS, adjuntos=[("fv.xml", xml)], fecha="Wed, 09 Sep 2026 21:30:00 +0000")
    for hora in (1, 13, 19):
        correr([crudo], datetime(2026, 9, 10, hora, 0, tzinfo=timezone.utc))
    assert [f[2] for f in hoja1.filas] == ["Factura", "FE 10647", "FE 1"]


def test_corrida_diaria_no_recoge_correos_de_anteayer(corrida_diaria):
    correr, hoja1 = corrida_diaria
    crudo = correo(RELLENOS, "Total a pagar $ 820.000", fecha="Mon, 07 Sep 2026 15:00:00 +0000")
    resultado = correr([crudo], datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc))
    assert resultado["inserted_first_pass"] == 0
    assert [f[2] for f in hoja1.filas] == ["Factura", "FE 1"]


# =============================================================================
# Arreglos de la auditoría (antes eran bugs conocidos en test_bugs_conocidos.py)
# =============================================================================

# -------------------------------------------------------------------- proveedor
PDF_RELLENOS = """RELLENOS Y FIBRAS
LUZ EDIT GIL DUQUE
NIT 43048845-1
FACTURA ELECTRÓNICA DE VENTA FE 10647
Cliente
SEBAS DUNCAN SAS
NIT 901234567
TOTAL A PAGAR 1.230.000"""


def test_proveedor_del_pdf_no_es_el_comprador(bot):
    assert "SEBAS DUNCAN" not in bot.extract_supplier_name_from_text(PDF_RELLENOS).upper()


def test_proveedor_del_pdf_no_depende_del_orden_alfabetico(bot):
    texto = PDF_RELLENOS.replace("RELLENOS Y FIBRAS\nLUZ EDIT GIL DUQUE", "TEXTIFILH SAS")
    assert bot.extract_supplier_name_from_text(texto) == "TEXTIFILH SAS"


@pytest.mark.parametrize("nombre", [
    "SEBAS DUNCAN SAS", "Sebas Duncan sas", "SEBAS DUNCAN S.A.S",
    "SEBAS DUNCAN SAS . FECHA DE EXPEDICIÓN : 2026/09/18 11:48:45",
])
def test_el_comprador_nunca_es_proveedor(bot, nombre):
    assert bot._is_own_company(nombre)
    assert bot.extract_supplier_name_from_text("FACTURA\n" + nombre) == ""


def test_razon_social_de_proveedor_tecnico(bot):
    assert bot._looks_like_technical_provider("Sistemas de Informacion Empresarial S.A.S.")


def test_attached_document_proveedor(bot):
    assert bot.extract_supplier_name_from_xml_bytes(xml_attached(proveedor="EFFISYSTEMS S.A.S.")) == "EFFISYSTEMS S.A.S."


def test_flujo_proveedor_del_asunto_gana_sobre_el_pdf(bot, escanear, monkeypatch):
    """Sin XML: el proveedor del asunto estructurado manda sobre lo que se lea del PDF."""
    monkeypatch.setattr(bot, "extract_text_from_pdf_bytes", lambda _b: "ALMACENES OTRO NOMBRE SAS\nTOTAL A PAGAR 468.000")
    _, _, filas = escanear([correo(PLASTISOL, adjuntos=[("fv.pdf", b"%PDF-falso")])])
    assert filas[0]["proveedor"] == "Plastisol sas"


def test_flujo_proveedor_del_xml_gana_sobre_el_asunto(escanear):
    xml = xml_factura(numero="FE10647", proveedor="RELLENOS Y FIBRAS", total="820000.00")
    asunto = "43048845;LUZ EDIT GIL DUQUE;FE10647;01;RELLENOS Y FIBRAS"
    _, _, filas = escanear([correo(asunto, adjuntos=[("fv.xml", xml)])])
    assert filas[0]["proveedor"] == "RELLENOS Y FIBRAS"


# --------------------------------------------------------------- número de factura
def test_attached_document_numero(bot):
    assert bot.extract_invoice_number_from_xml_bytes(xml_attached(numero="FE94506")) == "FE 94506"


def test_normaliza_numero_con_guion(bot):
    assert bot._normalize_invoice_candidate("FE-94506") == "FE 94506"


def test_estructura_con_asunto_partido(escanear):
    """Asunto largo partido en dos líneas (\\r\\n): antes la factura quedaba N/A."""
    partido = (b"900418527;IMPORTADORA DE INSUMOS EL MAYORISTA\r\n"
               b" SAS;E4MD56131511;01;IMPORTADORA DE INSUMOS EL MAYORISTA SAS")
    _, _, filas = escanear([correo("", "Adjunto documento. Total a pagar $ 340.000", asunto_crudo=partido)])
    assert filas[0]["factura"] == "E4MD56131511"
    assert filas[0]["proveedor"] == "IMPORTADORA DE INSUMOS EL MAYORISTA SAS"


def test_estructura_falso_positivo(escanear):
    insertadas, _, _ = escanear([correo("Pedido 1234567; Juan Perez; enviado", "Tu pedido llega mañana. Valor $50.000")])
    assert insertadas == 0


@pytest.mark.parametrize("asunto", [
    "900123456;PROVEEDOR DEMO SAS;PENDIENTE;01",      # tercer campo sin dígitos
    "900123456;PROVEEDOR DEMO SAS;FE100;urgente",     # cuarto campo no es un código
])
def test_estructura_exige_forma_de_factura(bot, asunto):
    assert bot.extract_structured_email_invoice_and_supplier(asunto, "") == ("", "")


def test_flujo_numero_del_asunto_gana_sobre_el_texto_del_pdf(bot, escanear, monkeypatch):
    """Caso ANTIOQUEÑA DE MAQUINAS: el PDF daba FES 1067300 y el asunto dice FES106730."""
    monkeypatch.setattr(bot, "extract_text_from_pdf_bytes",
                        lambda _b: "FACTURA ELECTRÓNICA DE VENTA FES 1067300\nTOTAL A PAGAR 1.300.000")
    asunto = "800025054;ANTIOQUEÑA DE MAQUINAS Y CIA S.A.S;FES106730;01;ANTIOQUENA DE MAQUINAS"
    _, _, filas = escanear([correo(asunto, adjuntos=[("fv.pdf", b"%PDF-falso")])])
    assert (filas[0]["factura"], filas[0]["valor"]) == ("FES 106730", 1300000)


# ------------------------------------------------------------------------- valor
def test_valor_del_xml_gana_sobre_el_cuerpo_del_correo(escanear):
    xml = xml_factura(numero="FE10647", proveedor="RELLENOS Y FIBRAS", total="180000.00")
    _, _, filas = escanear([correo("Factura FE10647", "Total a pagar $2.840 por flete.", [("fv.xml", xml)])])
    assert filas[0]["valor"] == 180000


PDF_EN_LINEAS = "SUBTOTAL\n229.800\nIVA\n43.662\nTotal líneas o ítems: 1\nTOTAL DE LA OPERACIÓN\n273.462\nNIT 900.123.456-7\n"


def test_pdf_total_con_etiqueta_y_valor_en_lineas_distintas(bot):
    assert bot.find_best_total_by_labels(PDF_EN_LINEAS)[0] == 273462


def test_pdf_respaldo_no_toma_un_nit_como_valor(bot):
    valores = bot.extract_invoice_values(PDF_EN_LINEAS)
    assert max(v[0] for v in valores) == 273462


def test_un_total_de_1_no_es_valor_de_factura(bot):
    assert bot.find_best_total_by_labels("Total: 1") is None


# ------------------------------------------------------------------ deduplicación
def test_dos_facturas_sin_numero_con_el_mismo_valor(escanear):
    a = correo("Invoice de agosto", "Total a pagar $ 50.000", remitente="Proveedor A <a@a.test>")
    b = correo("Invoice de agosto", "Total a pagar $ 50.000", remitente="Proveedor B <b@b.test>")
    insertadas, _, _ = escanear([a, b])
    assert insertadas == 2


# ----------------------------------------------------------------------- Análisis
def _hoy_octubre(bot):
    return bot.InvoiceRecord(
        message_id="<hoy@correo.test>", invoice_number="PV 15701", email_date="Thu, 01 Oct 2026 16:18:11 +0000",
        stored_date="2026-10-01", remitente_fijo="sebas duncan sas", remitente_real="Plastisol sas",
        asunto="x", valor_entero=456000, moneda="COP", palabra_clave="factura", uid="1",
    )


def test_analisis_no_pierde_la_historia_de_hoja1(bot, sheets_falso):
    """Análisis se arma desde hoja1: la base puede traer solo los registros de hoy."""
    cfg, hoja1, analisis = sheets_falso
    hoja1.filas = [
        list(bot.SHEET_HEADERS),
        ["RELLENOS Y FIBRAS", "Tue, 01 Sep 2026 19:48:02 +0000", "FE 10392", "d" * 32, "820000", "x", "factura"],
    ]
    bot.sync_to_google_sheets(cfg, [_hoy_octubre(bot)])
    assert analisis.filas[0][:3] == ["Proveedor", "Sep 2026", "Oct 2026"]
    por_proveedor = {f[0]: f[1:3] for f in analisis.filas[1:]}
    assert por_proveedor["RELLENOS Y FIBRAS"] == [820000, 0]
    assert por_proveedor["Plastisol sas"] == [0, 456000]
    assert por_proveedor["TOTAL"] == [820000, 456000]


def test_analisis_respeta_correcciones_hechas_a_mano(bot, sheets_falso):
    """Valor en blanco (nota crédito anotada a mano) no suma; '1,230,000' se lee como número."""
    cfg, hoja1, analisis = sheets_falso
    hoja1.filas = [
        list(bot.SHEET_HEADERS),
        ["CAYLI TEXTILES SAS", "Mon, 28 Sep 2026 19:27:55 +0000", "NC 287", "e" * 32, "", "x", "factura", "NOTA CREDITO"],
        ["CAYLI TEXTILES SAS", "Mon, 28 Sep 2026 18:47:12 +0000", "FEA 322", "f" * 32, "850000", "x", "factura"],
        ["RELLENOS Y FIBRAS", "Tue, 29 Sep 2026 12:53:59 +0000", "FE 10647", "0" * 32, "1,230,000", "x", "factura"],
    ]
    bot.sync_to_google_sheets(cfg, [])
    por_proveedor = {f[0]: f[1] for f in analisis.filas[1:]}
    assert por_proveedor == {"CAYLI TEXTILES SAS": 850000, "RELLENOS Y FIBRAS": 1230000, "TOTAL": 2080000}


@pytest.mark.parametrize("celda, esperado", [
    ("820000", 820000), ("1,230,000", 1230000), ("273.462", 273462), ("$ 81.000", 81000),
    ("100000.00", 100000), ("-850000", -850000), ("", None), ("Valor", None), (None, None),
])
def test_valor_de_celda_de_hoja1(bot, celda, esperado):
    assert bot._sheet_value_to_int(celda) == esperado


# ------------------------------------------------------------------ lectura IMAP
def test_el_buzon_se_abre_en_solo_lectura(bot, monkeypatch):
    """El bot no debe marcar como leídos los correos que revisa."""
    from conftest import config_prueba
    llamadas = {}

    class ImapSimulado:
        def __init__(self, host, port):
            pass

        def login(self, usuario, clave):
            pass

        def select(self, carpeta, readonly=False):
            llamadas["readonly"] = readonly

    monkeypatch.setattr(bot.imaplib, "IMAP4_SSL", ImapSimulado)
    bot.connect_imap(config_prueba())
    assert llamadas["readonly"] is True


# ------------------------------------------------- corrida por rango (reconstrucción)
def test_corrida_por_rango_deja_hoja1_de_la_mas_reciente_a_la_mas_antigua(corrida_diaria):
    """Reconstruir la hoja con --start-date/--end-date: 30 facturas, lotes de 25, orden final correcto."""
    correr, hoja1 = corrida_diaria
    hoja1.filas = []
    crudos = [
        correo(f"900123456;PROVEEDOR DEMO SAS;FE{1000 + dia};01;PROVEEDOR DEMO SAS", "Total a pagar $ 100.000",
               fecha=f"{dia:02d} Sep 2026 12:00:00 +0000")
        for dia in range(1, 31)
    ]
    resultado = correr(crudos, datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc),
                       start_date="2026-09-01", end_date="2026-09-30")
    assert resultado["inserted_first_pass"] == 30
    assert hoja1.filas[0][0] == "Proveedor"
    assert [f[2] for f in hoja1.filas[1:]] == [f"FE {1000 + dia}" for dia in range(30, 0, -1)]
