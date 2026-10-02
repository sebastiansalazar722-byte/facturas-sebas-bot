# -*- coding: utf-8 -*-
"""Utilidades compartidas por la suite de pruebas del bot de facturas.

Estas pruebas NO tocan Gmail, Google Sheets, Postgres ni el archivo
facturas.db real: usan un IMAP falso, una hoja falsa y un SQLite temporal.
"""
import email
import email.policy
import io
import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# Nunca usar Postgres en pruebas.
os.environ.pop("DATABASE_URL", None)

# Producción (Render) no instala PyMuPDF: requirements.txt solo trae pypdf.
# Se bloquea "fitz" para que los PDF se lean igual que en producción.
sys.modules["fitz"] = None

import factura_bot_google_sheets_v11_render as _bot  # noqa: E402

SIN_PLEGADO = email.policy.default.clone(max_line_length=None)

NS = (
    'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
    'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"'
)


def xml_factura(numero="FE94506", proveedor="EFFISYSTEMS S.A.S.", total="180000.00",
                linea="2840.00", con_total_legal=True):
    """Invoice UBL mínima: una línea con un valor pequeño y el total legal aparte."""
    total_legal = (
        f'<cac:LegalMonetaryTotal>'
        f'<cbc:LineExtensionAmount currencyID="COP">151260.50</cbc:LineExtensionAmount>'
        f'<cbc:TaxInclusiveAmount currencyID="COP">{total}</cbc:TaxInclusiveAmount>'
        f'<cbc:PayableAmount currencyID="COP">{total}</cbc:PayableAmount>'
        f'</cac:LegalMonetaryTotal>'
    ) if con_total_legal else f'<cbc:PayableAmount currencyID="COP">{total}</cbc:PayableAmount>'
    return (
        f'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" {NS}>'
        f'<cbc:ID>{numero}</cbc:ID>'
        f'<cac:AccountingSupplierParty><cac:Party>'
        f'<cac:PartyTaxScheme><cbc:RegistrationName>{proveedor}</cbc:RegistrationName></cac:PartyTaxScheme>'
        f'</cac:Party></cac:AccountingSupplierParty>'
        f'<cac:AccountingCustomerParty><cac:Party>'
        f'<cac:PartyTaxScheme><cbc:RegistrationName>SEBAS DUNCAN SAS</cbc:RegistrationName></cac:PartyTaxScheme>'
        f'</cac:Party></cac:AccountingCustomerParty>'
        f'<cac:TaxTotal><cbc:TaxAmount currencyID="COP">{linea}</cbc:TaxAmount></cac:TaxTotal>'
        f'<cac:InvoiceLine><cbc:ID>1</cbc:ID><cbc:InvoicedQuantity>1.000</cbc:InvoicedQuantity>'
        f'<cbc:LineExtensionAmount currencyID="COP">{linea}</cbc:LineExtensionAmount></cac:InvoiceLine>'
        f'{total_legal}'
        f'</Invoice>'
    ).encode("utf-8")


def xml_attached(numero="FE94506", proveedor="EFFISYSTEMS S.A.S.", total="180000.00"):
    """AttachedDocument DIAN con la Invoice real embebida en CDATA."""
    interna = xml_factura(numero, proveedor, total).decode("utf-8")
    return (
        f'<AttachedDocument xmlns="urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2" {NS}>'
        f'<cbc:ID>{numero}</cbc:ID>'
        f'<cac:SenderParty><cac:PartyTaxScheme><cbc:RegistrationName>{proveedor}</cbc:RegistrationName>'
        f'</cac:PartyTaxScheme></cac:SenderParty>'
        f'<cac:ReceiverParty><cac:PartyTaxScheme><cbc:RegistrationName>SEBAS DUNCAN SAS</cbc:RegistrationName>'
        f'</cac:PartyTaxScheme></cac:ReceiverParty>'
        f'<cac:Attachment><cac:ExternalReference><cbc:Description>'
        f'<![CDATA[<?xml version="1.0" encoding="UTF-8"?>{interna}]]>'
        f'</cbc:Description></cac:ExternalReference></cac:Attachment>'
        f'<cac:ParentDocumentLineReference><cbc:LineID>1</cbc:LineID><cac:DocumentReference>'
        f'<cbc:ID>{numero}</cbc:ID></cac:DocumentReference></cac:ParentDocumentLineReference>'
        f'</AttachedDocument>'
    ).encode("utf-8")


def zip_con(nombre, contenido):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(nombre, contenido)
    return buf.getvalue()


_contador = {"n": 0}


def correo(asunto, cuerpo="Adjunto documento.", adjuntos=(), remitente="Proveedor X <facturas@proveedor.test>",
           fecha="Tue, 29 Sep 2026 10:00:00 -0500", message_id=None, asunto_crudo=None):
    """Arma un correo y lo devuelve como bytes, igual que lo entrega IMAP.

    asunto_crudo permite inyectar el encabezado Subject tal cual (por ejemplo
    partido en dos líneas con \\r\\n, como llegan los asuntos largos).
    """
    _contador["n"] += 1
    msg = EmailMessage()
    msg["Subject"] = "ASUNTO_PROVISIONAL" if asunto_crudo is not None else asunto
    msg["From"] = remitente
    msg["Date"] = fecha
    msg["Message-ID"] = message_id or f"<prueba-{_contador['n']:04d}@correo.test>"
    msg.set_content(cuerpo)
    for nombre, datos in adjuntos:
        msg.add_attachment(datos, maintype="application", subtype="octet-stream", filename=nombre)
    crudo = msg.as_bytes(policy=SIN_PLEGADO)
    if asunto_crudo is not None:
        crudo = crudo.replace(b"Subject: ASUNTO_PROVISIONAL", b"Subject: " + asunto_crudo, 1)
    return crudo


class ImapFalso:
    def __init__(self, crudos):
        self.crudos = {str(i + 1).encode(): c for i, c in enumerate(crudos)}

    def ids(self):
        return list(self.crudos)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def search(self, _charset, _since, desde, _before, antes):
        """Imita SINCE/BEFORE de IMAP: filtra por la fecha del encabezado Date."""
        d1 = datetime.strptime(desde, "%d-%b-%Y").date()
        d2 = datetime.strptime(antes, "%d-%b-%Y").date()
        ids = []
        for num, crudo in self.crudos.items():
            fecha = parsedate_to_datetime(email.message_from_bytes(crudo)["Date"]).date()
            if d1 <= fecha < d2:
                ids.append(num)
        return "OK", [b" ".join(ids)]

    def fetch(self, num, _spec):
        n = int(num)
        return "OK", [(f"{n} (UID {100 + n} RFC822 {{1}}".encode(), self.crudos[num]), b")"]


def config_prueba(**extra):
    base = dict(
        imap_host="imap.test", imap_port=993, smtp_host="smtp.test", smtp_port=587,
        email_address="bot@sebasduncan.test", email_password="x", inbox_folder="INBOX",
        report_to="informe@sebasduncan.test", run_time="08:00",
        exclude_email="bot@sebasduncan.test",
    )
    base.update(extra)
    return _bot.Config(**base)


@pytest.fixture
def bot():
    return _bot


@pytest.fixture(autouse=True)
def _sin_postgres(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def escanear(tmp_path):
    """Pasa correos por _scan_and_store() real y devuelve (insertadas, excluidas, filas)."""
    db = tmp_path / "facturas_prueba.db"
    _bot.init_db(db)

    def _escanear(crudos, inicio=None, fin=None, stored_date="prueba"):
        inicio = inicio or datetime(2026, 9, 28).astimezone()
        fin = fin or datetime(2026, 9, 30, 23, 59, 59).astimezone()
        mail = ImapFalso(crudos)
        insertadas, excluidas = _bot._scan_and_store(
            mail=mail, ids=mail.ids(), db_path=db, stored_date=stored_date,
            start_dt=inicio, end_dt=fin, cfg=config_prueba(),
        )
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        filas = [dict(r) for r in con.execute(
            "SELECT invoice_number AS factura, remitente_real AS proveedor, "
            "valor_entero AS valor, palabra_clave AS keyword, message_id FROM facturas ORDER BY id"
        )]
        con.close()
        return insertadas, excluidas, filas

    return _escanear


class HojaFalsa:
    """Imita las operaciones de gspread que usa el bot sobre una pestaña."""

    def __init__(self, filas=None, sheet_id=0):
        self.filas = [list(f) for f in (filas or [])]
        self.id = sheet_id
        self.spreadsheet = self

    def get_all_values(self):
        return [list(f) for f in self.filas]

    def append_row(self, fila, value_input_option=None):
        self.filas.append(list(fila))

    def append_rows(self, filas, value_input_option=None):
        self.filas.extend(list(f) for f in filas)

    def batch_update(self, body):
        """Solo insertDimension de filas: abre filas vacías y baja las existentes."""
        for peticion in body["requests"]:
            rango = peticion["insertDimension"]["range"]
            assert rango["sheetId"] == self.id and rango["dimension"] == "ROWS"
            inicio, fin = rango["startIndex"], rango["endIndex"]
            while len(self.filas) < inicio:
                self.filas.append([])
            self.filas[inicio:inicio] = [[] for _ in range(fin - inicio)]

    def clear(self):
        self.filas = []

    def update(self, range_name=None, values=None, value_input_option=None):
        """Escribe a partir de la celda A<n>, sobrescribiendo solo esas filas."""
        inicio = int(range_name.lstrip("A")) - 1
        for i, fila in enumerate(values or []):
            while len(self.filas) <= inicio + i:
                self.filas.append([])
            self.filas[inicio + i] = list(fila)

    def freeze(self, rows=None):
        pass

    def format(self, *_a, **_k):
        pass


class LibroFalso:
    def __init__(self, hojas):
        self.hojas = hojas

    def worksheet(self, titulo):
        return self.hojas[titulo]

    def add_worksheet(self, title, rows, cols):
        self.hojas[title] = HojaFalsa()
        return self.hojas[title]


@pytest.fixture
def sheets_falso(monkeypatch):
    """Reemplaza Google Sheets por hojas en memoria. Devuelve (cfg, hoja1, analisis)."""
    hoja1, analisis = HojaFalsa(), HojaFalsa()
    libro = LibroFalso({"hoja1": hoja1, "Análisis": analisis})

    class Cliente:
        def open_by_key(self, _id):
            return libro

    monkeypatch.setattr(_bot, "_require_google_sheets", lambda cfg: Cliente())
    cfg = config_prueba(google_sheets_enabled=True, google_spreadsheet_id="hoja-de-prueba")
    return cfg, hoja1, analisis
