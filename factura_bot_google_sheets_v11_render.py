#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bot de correo para buscar facturas/invoices, leer adjuntos PDF/XML/ZIP,
guardar valores en SQLite y enviar un resumen ejecutivo por correo.

Uso:
    python factura_bot_corregido_v7.py --run-now
    python factura_bot_corregido_v7.py --run-now --start-date 2026-03-01 --end-date 2026-03-31
    python factura_bot_corregido_v7.py --scheduler
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import email
import imaplib
import io
import logging
import os
import re
import sqlite3
import ssl
import sys
import time
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import smtplib
from pypdf import PdfReader

try:
    import schedule
except ImportError:
    schedule = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

try:
    import gspread
except ImportError:
    gspread = None

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    Request = None
    Credentials = None
    InstalledAppFlow = None


ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "facturas.db"
LOG_PATH = ROOT_DIR / "factura_bot.log"
ENV_PATH = ROOT_DIR / ".env"

KEYWORDS = ("factura", "invoice")
EXCLUDED_TERMS = ("dian", "token")
FIXED_SENDER_NAME = "sebas duncan sas"
REPORT_SUBJECT = "informe de facturas sas"
SPECIAL_SUPPLIER_TOKENS = ("vision perspectiva sas", "901734728")

TOTAL_LABEL_PATTERNS = [
    r"total\s*a\s*pagar",
    r"total\s*neto",
    r"valor\s*total",
    r"valor\s*a\s*pagar",
    r"total\s*de\s*la\s*factura",
    r"monto\s*total",
    r"total\s*factura",
    r"importe\s*total",
    r"\btotal\b",
]
IGNORE_LABEL_PATTERNS = [
    r"subtotal",
    r"iva",
    r"retefuente",
    r"retefuente",
    r"retenci[oó]n",
    r"descuento",
    r"saldo",
    r"anticipo",
    r"impuesto",
    r"base",
    r"valor unit",
    r"precio",
    r"vr unit",
    r"cantidad",
    r"por vencer",
    r"vencido",
    r"pagos?",
    r"importe adeudado",
]


@dataclass
class Config:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    email_address: str
    email_password: str
    inbox_folder: str
    report_to: str
    run_time: str
    use_ssl_imap: bool = True
    use_ssl_smtp: bool = False
    smtp_starttls: bool = True
    exclude_email: str = ""
    google_sheets_enabled: bool = False
    google_spreadsheet_id: str = ""
    google_credentials_file: str = ""
    google_facturas_sheet: str = "hoja1"
    google_analysis_sheet: str = "Análisis"
    database_url: str = ""


@dataclass
class InvoiceRecord:
    message_id: str
    invoice_number: str
    email_date: str
    stored_date: str
    remitente_fijo: str
    remitente_real: str
    asunto: str
    valor_entero: int
    moneda: str
    palabra_clave: str
    uid: str


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config() -> Config:
    if load_dotenv and ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    def get_required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise ValueError(f"Falta la variable de entorno obligatoria: {name}")
        return value

    email_address = get_required("EMAIL_ADDRESS")

    return Config(
        imap_host=get_required("IMAP_HOST"),
        imap_port=int(os.getenv("IMAP_PORT", "993")),
        smtp_host=get_required("SMTP_HOST"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        email_address=email_address,
        email_password=get_required("EMAIL_PASSWORD"),
        inbox_folder=os.getenv("INBOX_FOLDER", "INBOX"),
        report_to=os.getenv("REPORT_TO", "sebastiansalazar722@gmail.com"),
        run_time=os.getenv("RUN_TIME", "19:00"),
        use_ssl_imap=os.getenv("USE_SSL_IMAP", "true").lower() == "true",
        use_ssl_smtp=os.getenv("USE_SSL_SMTP", "false").lower() == "true",
        smtp_starttls=os.getenv("SMTP_STARTTLS", "true").lower() == "true",
        exclude_email=os.getenv("EXCLUDE_EMAIL", email_address).strip().lower(),
        google_sheets_enabled=os.getenv("GOOGLE_SHEETS_ENABLED", "false").lower() == "true",
        google_spreadsheet_id=os.getenv(
            "GOOGLE_SPREADSHEET_ID",
            "1cHEw8eLyw1K1L-Ti0uiTa9Lsb-HkYTlKwqgNBLHd0Fw",
        ).strip(),
        google_credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip(),
        google_facturas_sheet=os.getenv("GOOGLE_FACTURAS_SHEET", "hoja1").strip(),
        google_analysis_sheet=os.getenv("GOOGLE_ANALYSIS_SHEET", "Análisis").strip(),
        database_url=os.getenv("DATABASE_URL", "").strip(),
    )


def _database_url() -> str:
    """Return the cloud database URL when configured."""
    return os.getenv("DATABASE_URL", "").strip()


def _using_postgres() -> bool:
    return bool(_database_url())


def _pg_connect():
    if psycopg2 is None:
        raise RuntimeError(
            "Falta psycopg2-binary. Instala dependencias con: pip install psycopg2-binary"
        )
    return psycopg2.connect(_database_url())


def _sql(sql: str) -> str:
    """Translate the small set of SQLite-style placeholders used by this bot."""
    return sql.replace("?", "%s") if _using_postgres() else sql


def init_db(db_path: Path = DB_PATH) -> None:
    if _using_postgres():
        with closing(_pg_connect()) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS facturas (
                    id BIGSERIAL PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    uid TEXT,
                    invoice_number TEXT,
                    email_date TEXT NOT NULL,
                    stored_date TEXT NOT NULL,
                    remitente_fijo TEXT NOT NULL,
                    remitente_real TEXT,
                    asunto TEXT,
                    valor_entero BIGINT NOT NULL,
                    moneda TEXT,
                    palabra_clave TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_msg_valor
                ON facturas (stored_date, message_id, valor_entero)
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_invoice
                ON facturas (stored_date, invoice_number, valor_entero)
            """)
            # Cloud deduplication: invoice number + value should not be inserted
            # again merely because a previous historical import used another stored_date.
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_facturas_invoice_value
                ON facturas (invoice_number, valor_entero)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_facturas_message_id
                ON facturas (message_id)
            """)
            conn.commit()
        return

    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS facturas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                uid TEXT,
                invoice_number TEXT,
                email_date TEXT NOT NULL,
                stored_date TEXT NOT NULL,
                remitente_fijo TEXT NOT NULL,
                remitente_real TEXT,
                asunto TEXT,
                valor_entero INTEGER NOT NULL,
                moneda TEXT,
                palabra_clave TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("PRAGMA table_info(facturas)")
        cols = {row[1] for row in cur.fetchall()}
        if "invoice_number" not in cols:
            cur.execute("ALTER TABLE facturas ADD COLUMN invoice_number TEXT")
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_msg_valor
            ON facturas (stored_date, message_id, valor_entero)
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_invoice
            ON facturas (stored_date, invoice_number, valor_entero)
            """
        )
        conn.commit()


def decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>", "\n", text)
    text = re.sub(r"(?is)<.*?>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return normalize_spaces(text)


def get_message_text(msg: email.message.Message) -> str:
    plain_parts: List[str] = []
    html_parts: List[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition.lower():
                continue

            try:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace") if payload else ""
            except Exception:
                text = ""

            if content_type == "text/plain":
                plain_parts.append(text)
            elif content_type == "text/html":
                html_parts.append(text)
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            text = ""

        if msg.get_content_type() == "text/html":
            html_parts.append(text)
        else:
            plain_parts.append(text)

    final_text = "\n".join(plain_parts).strip()
    if not final_text and html_parts:
        final_text = "\n".join(html_to_text(h) for h in html_parts)
    return normalize_spaces(final_text)


def get_email_date(msg: email.message.Message) -> datetime:
    raw_date = msg.get("Date")
    if raw_date:
        try:
            dt = parsedate_to_datetime(raw_date)
            if dt.tzinfo:
                return dt.astimezone()
            return dt.astimezone()
        except Exception:
            pass
    return datetime.now().astimezone()


def build_day_range_local(target_date: datetime) -> Tuple[datetime, datetime]:
    start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def build_custom_range_local(start_date_str: str, end_date_str: str) -> Tuple[datetime, datetime, str]:
    start = datetime.strptime(start_date_str, "%Y-%m-%d").astimezone()
    end = datetime.strptime(end_date_str, "%Y-%m-%d").astimezone()
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
    stored_date = f"rango-{start_date_str}_a_{end_date_str}"
    return start, end, stored_date


def contains_keyword(text: str) -> Optional[str]:
    lower = text.lower()
    for kw in KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", lower):
            return kw
    return None


def contains_excluded_term(text: str) -> bool:
    lower = text.lower()
    return any(re.search(rf"\b{re.escape(term)}\b", lower) for term in EXCLUDED_TERMS)


def text_mentions_special_supplier(text: str) -> bool:
    lower = text.lower()
    return any(token in lower for token in SPECIAL_SUPPLIER_TOKENS)


def clean_currency_to_int(raw_value: str) -> int:
    value = raw_value.strip()
    cleaned = re.sub(r"[^\d,\.]", "", value)
    if not cleaned:
        raise ValueError("No se pudo limpiar el valor monetario.")
    cleaned = re.sub(r"([.,]\d{2})$", "", cleaned)
    cleaned = cleaned.replace(".", "").replace(",", "")
    if not cleaned.isdigit():
        raise ValueError(f"Valor no interpretable: {raw_value}")
    return int(cleaned)


def detect_currency(raw_value: str) -> str:
    upper = raw_value.upper()
    if "$" in raw_value or "COP" in upper:
        return "COP"
    if "USD" in upper or "US$" in upper:
        return "USD"
    if "EUR" in upper or "€" in raw_value:
        return "EUR"
    return "N/A"


def _compile_money_matches(text: str) -> List[Tuple[int, int, str]]:
    patterns = [
        r"(?:COP|USD|EUR|US\$|\$|€)\s*\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?",
        r"(?:COP|USD|EUR|US\$|\$|€)\s*\d+(?:[.,]\d{2})",
        r"\b\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?\b",
    ]

    raw_matches: List[Tuple[int, int, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            raw_matches.append((match.start(), match.end(), match.group(0).strip()))

    raw_matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    filtered: List[Tuple[int, int, str]] = []
    for start, end, literal in raw_matches:
        overlapped = False
        for existing_start, existing_end, _ in filtered:
            if start >= existing_start and end <= existing_end:
                overlapped = True
                break
        if not overlapped:
            filtered.append((start, end, literal))

    return filtered


def extract_invoice_values(text: str) -> List[Tuple[int, str, str]]:
    found: List[Tuple[int, str, str]] = []
    seen_values = set()
    for _, _, literal in _compile_money_matches(text):
        try:
            value_int = clean_currency_to_int(literal)
            currency = detect_currency(literal)
            key = (value_int, currency)
            if key not in seen_values:
                seen_values.add(key)
                found.append((value_int, currency, literal))
        except Exception:
            continue
    return found


def parse_value_from_text(raw: str) -> Optional[int]:
    try:
        return clean_currency_to_int(raw)
    except Exception:
        return None


def find_best_total_by_labels(text: str) -> Optional[Tuple[int, str, str]]:
    """
    Encuentra el valor TOTAL usando contexto de etiqueta.

    Importante: muchos PDF de facturas extraen la etiqueta y el valor en
    líneas/columnas separadas. Por eso no dependemos únicamente de que
    "TOTAL NETO" y "$100,000" estén en la misma línea.
    """
    if not text:
        return None

    normalized = text.replace("\r", "\n")
    lines = [normalize_spaces(x) for x in normalized.splitlines()]
    lines = [x for x in lines if x]

    # 1) Etiqueta y valor en la misma línea.
    same_line_patterns = [
        (r"total\s*a\s*pagar", 100),
        (r"total\s*neto", 95),
        (r"valor\s*total", 90),
        (r"monto\s*total", 90),
        (r"total\s*factura", 90),
        (r"importe\s*total", 90),
        (r"^\s*total\s*$", 85),
        (r"^\s*total\s*[:\-]", 85),
    ]

    candidates: List[Tuple[int, int, str, str]] = []

    for i, line in enumerate(lines):
        low = line.lower()
        for label_pattern, score in same_line_patterns:
            if re.search(label_pattern, low, re.I):
                # Do not treat subtotal/IVA/other component rows as TOTAL.
                if any(re.search(pat, low, re.I) for pat in IGNORE_LABEL_PATTERNS):
                    # "TOTAL NETO" is explicitly allowed.
                    if not re.search(r"total\s*neto|total\s*a\s*pagar|valor\s*total|total\s*factura|importe\s*total", low, re.I):
                        continue
                vals = extract_invoice_values(line)
                if vals:
                    value, currency, literal = max(vals, key=lambda x: x[0])
                    candidates.append((score, i, literal, currency))
                    # Keep actual value encoded through a separate lookup below.
                    candidates[-1] = (score, i, literal, currency)

    if candidates:
        # Priority by label strength, then prefer a larger explicitly labeled
        # total only as a tiebreaker.
        best = sorted(candidates, key=lambda x: (-x[0], x[1]))[0]
        value = clean_currency_to_int(best[2])
        return value, detect_currency(best[2]), best[2]

    # 2) Critical fallback: label and amount in adjacent PDF extraction lines.
    # This is the case for EFFISYSTEMS FE-94506:
    #   TOTAL NETO
    #   $100,000
    # or the amount may be 1-5 lines away because of PDF columns.
    label_patterns = [
        (r"total\s*a\s*pagar", 100),
        (r"total\s*neto", 95),
        (r"valor\s*total", 90),
        (r"monto\s*total", 90),
        (r"total\s*factura", 90),
        (r"importe\s*total", 90),
        (r"^\s*total\s*$", 85),
    ]

    for i, line in enumerate(lines):
        low = line.lower()
        label_score = None
        for pat, score in label_patterns:
            if re.search(pat, low, re.I):
                label_score = score
                break
        if label_score is None:
            continue

        # Ignore component labels, but never ignore explicit total-neto /
        # total-a-pagar rows.
        if any(re.search(pat, low, re.I) for pat in IGNORE_LABEL_PATTERNS):
            if not re.search(r"total\s*neto|total\s*a\s*pagar|valor\s*total|total\s*factura|importe\s*total", low, re.I):
                continue

        for j in range(i + 1, min(len(lines), i + 7)):
            candidate_line = lines[j]
            vals = extract_invoice_values(candidate_line)
            if not vals:
                # Sometimes the extractor keeps "$" on one line and the number
                # on the next. Try the combined local window.
                continue

            # Prefer values that look like money, and reject tiny isolated
            # quantities such as "1".
            vals = [v for v in vals if v[0] >= 10]
            if not vals:
                continue

            value, currency, literal = max(vals, key=lambda x: x[0])
            return value, currency, literal

        # Also inspect the same local block as a last resort. This catches
        # layouts where the total label is followed by several table cells
        # before the amount.
        block = "\n".join(lines[i:i+8])
        vals = [v for v in extract_invoice_values(block) if v[0] >= 10]
        if vals:
            value, currency, literal = max(vals, key=lambda x: x[0])
            return value, currency, literal

    return None

def _normalize_invoice_candidate(raw: str) -> str:
    """Normalize an invoice identifier while preserving meaningful prefix/number."""
    value = normalize_spaces(raw or "").strip(" :#.-")
    value = re.sub(r"\s*[-/]\s*", "-", value)
    # Common layout: FEPI 40716 -> FEPI 40716.
    m = re.fullmatch(r"([A-Z]{2,12})\s*(\d{1,20})", value, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1).upper()} {m.group(2)}"
    return value.upper().replace(" ", "")


def _is_bad_invoice_candidate(candidate: str, context: str = "") -> bool:
    """Reject values that are commonly authorization/NIT/CUFE/date artifacts."""
    c = _normalize_invoice_candidate(candidate)
    u = c.upper()
    ctx = (context or "").lower()

    bad_words = (
        "cufe", "uuid", "nit", "resolucion", "resolución", "autorizado",
        "autorizada", "rango", "desde", "hasta", "prefijo", "codigo qr",
        "código qr", "vencimiento", "telefono", "teléfono"
    )
    if any(w in ctx for w in bad_words):
        # Context alone is not enough to reject an invoice if the candidate
        # is immediately after an explicit invoice label.
        if not re.search(r"(factura(?:\s+electr[oó]nica)?(?:\s+de\s+venta)?|n[úu]mero)\b", ctx, re.I):
            return True

    if re.fullmatch(r"\d{1,4}", u):
        # Bare short numbers are very often item/page/quantity values.
        return True

    if re.fullmatch(r"\d{8,20}", u):
        # Bare long numeric values are more likely NIT, dates, amounts, etc.
        return True

    if any(u.startswith(prefix) for prefix in ("CUFE", "UUID", "NIT")):
        return True

    return False


def extract_invoice_number(text: str) -> str:
    """
    Extract the invoice number from heterogeneous Colombian e-invoice layouts.

    Priority:
      1) explicit invoice label + identifier;
      2) 'FACTURA ELECTRÓNICA DE VENTA' followed by identifier;
      3) known FE/FV/FC prefixes with nearby invoice context;
      4) conservative fallback.

    The previous implementation searched for generic FE* tokens first, which
    can select authorization/range artifacts from documents generated by
    different billing providers.
    """
    if not text:
        return ""

    normalized = text.replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    lines = [normalize_spaces(line) for line in normalized.splitlines() if normalize_spaces(line)]

    # 1) Strongest pattern: FACTURA ... [No/NÚMERO] <identifier>
    explicit_patterns = [
        r"\bfactura\s+electr[oó]nica\s+de\s+venta\s+(?:no\.?|n[úu]mero|num\.?)?\s*[:#]?\s*([A-Z]{1,12}\s*\d{1,20})\b",
        r"\bfactura\s+(?:no\.?|n[úu]mero|num\.?)\s*[:#]?\s*([A-Z]{1,12}\s*\d{1,20})\b",
        r"\bfactura\s+electr[oó]nica\s+(?:no\.?|n[úu]mero|num\.?)\s*[:#]?\s*([A-Z]{1,12}\s*\d{1,20})\b",
    ]
    for pattern in explicit_patterns:
        m = re.search(pattern, normalized, flags=re.IGNORECASE)
        if m:
            candidate = _normalize_invoice_candidate(m.group(1))
            if not _is_bad_invoice_candidate(candidate, m.group(0)):
                return candidate

    # 2) Some providers put "NÚMERO: EMD326070635" on a line that also
    # contains FACTURA ELECTRÓNICA DE VENTA. Keep this tied to the same line.
    for line in lines:
        low = line.lower()
        if "factura" not in low or "venta" not in low:
            continue
        m = re.search(
            r"\bn[úu]mero\s*[:#]?\s*([A-Z]{1,12}\s*\d{1,20})\b",
            line, flags=re.IGNORECASE
        )
        if m:
            candidate = _normalize_invoice_candidate(m.group(1))
            if not _is_bad_invoice_candidate(candidate, line):
                return candidate

        # "FACTURA ELECTRÓNICA DE VENTA FEPI 40716"
        m = re.search(
            r"\bfactura\s+electr[oó]nica\s+de\s+venta\s+([A-Z]{1,12}\s*\d{1,20})\b",
            line, flags=re.IGNORECASE
        )
        if m:
            candidate = _normalize_invoice_candidate(m.group(1))
            if not _is_bad_invoice_candidate(candidate, line):
                return candidate

    # 2b) Some layouts separate the prefix and number, e.g.
    # "No. 2586" with "FEEM" elsewhere in the invoice header.
    # Pair a nearby invoice prefix with a nearby No./Número value, while
    # explicitly avoiding DIAN authorization ranges.
    prefix_matches = list(re.finditer(r"\b((?:FEVP|FEPI|FEEM|FEA|FE|FVE|FV|FCME|FC|EC|CNFE|PV|AR|YA)[A-Z]?)\b", normalized, flags=re.I))
    no_matches = list(re.finditer(r"\b(?:no\.?|n[úu]mero|numero)\s*[:#]?\s*(\d{1,20})\b", normalized, flags=re.I))
    pair_candidates = []
    for pm in prefix_matches:
        for nm in no_matches:
            distance = abs(pm.start() - nm.start())
            if distance > 700:
                continue
            window = normalized[max(0, min(pm.start(), nm.start())-120):min(len(normalized), max(pm.end(), nm.end())+120)]
            low = window.lower()
            if any(x in low for x in ("autorización", "autorizacion", "habilita desde", "rango")):
                continue
            candidate = _normalize_invoice_candidate(f"{pm.group(1)} {nm.group(1)}")
            if _is_bad_invoice_candidate(candidate, window):
                continue
            # Prefer pairs where the prefix and No. are close and the context
            # contains invoice terminology.
            score = (5 if "factura" in low else 0) + (3 if "venta" in low else 0) - distance / 1000
            pair_candidates.append((score, -distance, candidate))
    if pair_candidates:
        pair_candidates.sort(reverse=True)
        return pair_candidates[0][2]


    # 2c) Another common layout puts the numeric consecutive immediately
    # before the seller name, while the FE prefix appears elsewhere in the
    # header (for example: "2586 / INTERCOMERCIO JAO SAS / ... / FEEM").
    for idx, line in enumerate(lines):
        if not re.search(r"\b(?:SAS|S\.?A\.?S\.?|LTDA|LIMITADA|S\.?A\.?)\b", line, flags=re.I):
            continue
        if idx == 0:
            continue
        prev = lines[idx-1]
        nm = re.fullmatch(r"(\d{1,20})", prev.strip())
        if not nm:
            continue
        candidate_number = nm.group(1)
        if len(candidate_number) > 20:
            continue
        # Prefer an isolated FE-style prefix elsewhere in the document.
        # Ignore prefixes occurring on authorization/range lines.
        prefix_lines = []
        for pidx, pline in enumerate(lines):
            mpre = re.fullmatch(r"((?:FEVP|FEPI|FEEM|FEA|FE|FVE|FV|FCME|FC|EC|CNFE|PV|AR|YA)[A-Z]?)", pline, flags=re.I)
            if mpre and not re.search(r"autoriz|habilita|rango|hasta", pline, flags=re.I):
                prefix_lines.append((abs(pidx-idx), mpre.group(1)))
        if prefix_lines:
            prefix_lines.sort(key=lambda x: x[0])
            return _normalize_invoice_candidate(f"{prefix_lines[0][1]} {candidate_number}")

    # 3) Search for invoice-like prefixes, but only when the token is near
    # invoice terminology. This prevents selecting random FE*/code tokens.
    prefix_pattern = re.compile(
        r"\b((?:FEVP|FEPI|FEEM|FEA|FE|FVE|FVE|FV|FCME|FC|EC|CNFE|PV|AR|YA)[A-Z]?\s*\d{1,20})\b",
        flags=re.IGNORECASE
    )
    scored = []
    for match in prefix_pattern.finditer(normalized):
        candidate = _normalize_invoice_candidate(match.group(1))
        if _is_bad_invoice_candidate(candidate, normalized[max(0, match.start()-80):match.end()+80]):
            continue
        window = normalized[max(0, match.start()-100):match.end()+100].lower()
        score = 0
        if "factura" in window:
            score += 5
        if "venta" in window:
            score += 3
        if re.search(r"\bno\.?\b|\bn[úu]mero\b", window):
            score += 2
        if "autoriz" in window or "resoluci" in window or "rango" in window:
            score -= 6
        scored.append((score, -match.start(), candidate))

    if scored:
        scored.sort(reverse=True)
        best_score, _, best_candidate = scored[0]
        if best_score >= 3:
            return best_candidate

    # 4) Conservative legacy-style fallback: only accept a labeled invoice
    # identifier, never a bare FE token.
    m = re.search(
        r"\b(?:n[úu]mero|numero|no\.)\s*[:#]?\s*([A-Z]{1,12}\s*\d{1,20})\b",
        normalized, flags=re.IGNORECASE
    )
    if m:
        candidate = _normalize_invoice_candidate(m.group(1))
        if not _is_bad_invoice_candidate(candidate, m.group(0)):
            return candidate

    return ""

def get_attachments(msg: email.message.Message) -> List[Tuple[str, bytes]]:
    attachments: List[Tuple[str, bytes]] = []
    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in disposition.lower():
            continue
        filename = decode_mime_header(part.get_filename() or "")
        payload = part.get_payload(decode=True)
        if filename and payload:
            attachments.append((filename, payload))
    return attachments


def extract_invoice_number_from_xml_bytes(xml_bytes: bytes) -> str:
    """Extract the DIAN invoice ID from XML before falling back to PDF text."""
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return ""

    # In UBL/DIAN XML the document identifier is normally cbc:ID directly
    # under Invoice. Avoid UUID/CUFE and other IDs nested in other structures.
    root_tag = root.tag.split("}")[-1].lower()
    if root_tag in ("invoice", "creditnote", "debitnote"):
        for child in list(root):
            if child.tag.split("}")[-1].lower() == "id":
                value = (child.text or "").strip()
                if value:
                    candidate = _normalize_invoice_candidate(value)
                    if not _is_bad_invoice_candidate(candidate, "XML Invoice ID"):
                        return candidate

    # Fallback: common invoice-number tags used by non-UBL providers.
    preferred_tags = {
        "invoicenumber", "invoiceno", "invoiceid", "documentnumber",
        "documentid", "numero", "numerofactura", "facturanumero"
    }
    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower()
        if tag in preferred_tags:
            value = (elem.text or "").strip()
            if value:
                candidate = _normalize_invoice_candidate(value)
                if not _is_bad_invoice_candidate(candidate, tag):
                    return candidate

    # Last XML fallback: run the contextual extractor over text content.
    blobs = []
    for elem in root.iter():
        value = (elem.text or "").strip()
        if value:
            blobs.append(f"{elem.tag.split('}')[-1]}: {value}")
    return extract_invoice_number("\n".join(blobs))



def extract_values_from_xml_bytes(xml_bytes: bytes) -> List[Tuple[int, str, str]]:
    """Extract the invoice total without confusing quantities/text for money.

    Supports both:
      1) a direct UBL Invoice XML
      2) a DIAN AttachedDocument whose real Invoice is embedded in CDATA.

    Priority:
      LegalMonetaryTotal/PayableAmount
      LegalMonetaryTotal/TaxInclusiveAmount
      global PayableAmount only as a conservative fallback
    """
    def _parse_invoice_root(root):
        # If this is already an Invoice, use it.
        root_tag = root.tag.split("}")[-1]
        if root_tag == "Invoice":
            return root

        # DIAN AttachedDocument: the real Invoice is commonly embedded inside
        # cac:ExternalReference/cbc:Description as CDATA.
        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag != "Description":
                continue
            inner = (elem.text or "").strip()
            if "<Invoice" not in inner:
                continue
            try:
                inner_root = ET.fromstring(inner)
            except Exception:
                continue
            if inner_root.tag.split("}")[-1] == "Invoice":
                return inner_root

        return root

    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []

    invoice_root = _parse_invoice_root(root)

    # 1) Strict UBL monetary total: this is the authoritative invoice total.
    legal_totals = [
        elem for elem in invoice_root.iter()
        if elem.tag.split("}")[-1] == "LegalMonetaryTotal"
    ]

    for legal_total in legal_totals:
        children = list(legal_total)
        for wanted_tag in ("PayableAmount", "TaxInclusiveAmount"):
            for child in children:
                if child.tag.split("}")[-1] != wanted_tag:
                    continue
                raw = (child.text or "").strip()
                if not raw:
                    continue
                try:
                    # XML monetary amounts use a decimal point.
                    value = Decimal(raw)
                except Exception:
                    continue
                if value < 0:
                    continue
                return [(int(value), child.attrib.get("currencyID", "N/A"), raw)]

    # 2) Conservative fallback: a direct PayableAmount on the invoice.
    for elem in invoice_root.iter():
        if elem.tag.split("}")[-1] != "PayableAmount":
            continue
        raw = (elem.text or "").strip()
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except Exception:
            continue
        if value < 0:
            continue
        return [(int(value), elem.attrib.get("currencyID", "N/A"), raw)]

    # 3) Last fallback: only a clearly labelled total in the XML text.
    text_blobs = []
    for elem in invoice_root.iter():
        value = (elem.text or "").strip()
        if value:
            text_blobs.append(f"{elem.tag.split('}')[-1]}: {value}")

    best = find_best_total_by_labels("\\n".join(text_blobs))
    return [best] if best else []

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    # PyMuPDF usually preserves the invoice header/layout better than pypdf.
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            texts = [page.get_text() or "" for page in doc]
            text = "\n".join(x for x in texts if x.strip())
            if text.strip():
                return "\n".join(normalize_spaces(line) for line in text.splitlines() if normalize_spaces(line))
        finally:
            doc.close()
    except Exception:
        pass

    try:
        with tempfile.NamedTemporaryFile(delete=True, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp.flush()
            reader = PdfReader(tmp.name)
            texts = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    texts.append(page_text)
            return "\n".join(normalize_spaces(line) for line in "\n".join(texts).splitlines() if normalize_spaces(line))
    except Exception:
        return ""


def iter_supported_attachment_files(msg: email.message.Message) -> List[Tuple[str, bytes]]:
    supported: List[Tuple[str, bytes]] = []
    for filename, payload in get_attachments(msg):
        lower_name = filename.lower()
        if lower_name.endswith(".xml") or lower_name.endswith(".pdf"):
            supported.append((filename, payload))
            continue
        if lower_name.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                    for inner_name in zf.namelist():
                        if inner_name.endswith("/") or "__MACOSX/" in inner_name:
                            continue
                        lower_inner = inner_name.lower()
                        if lower_inner.endswith(".xml") or lower_inner.endswith(".pdf"):
                            try:
                                supported.append((inner_name, zf.read(inner_name)))
                            except Exception:
                                continue
            except Exception:
                continue
    return supported


def extract_attachment_texts(msg: email.message.Message) -> List[Tuple[str, str]]:
    extracted: List[Tuple[str, str]] = []
    for filename, payload in iter_supported_attachment_files(msg):
        lower_name = filename.lower()
        text = ""
        if lower_name.endswith(".xml"):
            try:
                text = payload.decode("utf-8", errors="replace")
            except Exception:
                text = payload.decode(errors="replace")
        elif lower_name.endswith(".pdf"):
            text = extract_text_from_pdf_bytes(payload)
        if text:
            extracted.append((filename, normalize_spaces(text)))
    return extracted



def _clean_supplier_name(value: str) -> str:
    value = normalize_spaces(value or "")
    value = re.sub(r"\s*<[^>]+>\s*", " ", value)
    value = re.sub(r"^(?:raz[oó]n\s+social|proveedor|vendedor|emisor|empresa)\s*[:#-]?\s*", "", value, flags=re.I)
    return normalize_spaces(value).strip(" -:|;")


def _looks_like_technical_provider(name: str) -> bool:
    low = normalize_spaces(name).lower()
    technical = (
        "ateb", "cofidi", "siesa", "world office", "worldoffice",
        "siigo", "facturatech", "the factory hka", "hka", "edigital",
        "proveedor tecnologico", "proveedor tecnológico", "software"
    )
    return any(x in low for x in technical)


def extract_supplier_name_from_xml_bytes(xml_bytes: bytes) -> str:
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return ""
    for supplier in root.iter():
        if supplier.tag.split("}")[-1].lower() != "accountingsupplierparty":
            continue
        candidates = []
        for elem in supplier.iter():
            tag = elem.tag.split("}")[-1].lower()
            if tag in ("registrationname", "name"):
                value = _clean_supplier_name(elem.text or "")
                if value and not _looks_like_technical_provider(value):
                    candidates.append((0 if tag == "registrationname" else 1, value))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
    return ""


def extract_supplier_name_from_text(text: str) -> str:
    if not text:
        return ""
    lines = [normalize_spaces(x) for x in text.replace("\r", "\n").splitlines() if normalize_spaces(x)]
    candidates = []
    label_patterns = [
        r"(?:raz[oó]n\s+social|nombre\s+del\s+emisor|emisor|proveedor|vendedor|empresa)\s*[:#-]\s*(.+)$",
    ]
    for idx, line in enumerate(lines):
        for pat in label_patterns:
            m = re.search(pat, line, flags=re.I)
            if m:
                val = _clean_supplier_name(m.group(1))
                if len(val) >= 3 and not _looks_like_technical_provider(val):
                    candidates.append((8, val))
        # In many PDFs the seller name is immediately above the invoice title.
        if idx < 30 and re.search(r"factura\s+(?:electr[oó]nica\s+)?(?:de\s+venta)?", line, re.I):
            for prev in lines[max(0, idx-8):idx]:
                val = _clean_supplier_name(prev)
                if (len(val) >= 5 and not re.search(r"^(factura|nit|fecha|cliente|se[nñ]or|se[nñ]ores)\b", val, re.I)
                        and not _looks_like_technical_provider(val) and not re.search(r"\d{6,}", val)):
                    candidates.append((4, val))

        # Common DIAN PDF layout: company name followed by a line containing
        # "Nit". This is a strong seller signal and avoids the technical
        # provider that may appear elsewhere in the document.
        if idx + 1 < len(lines) and re.fullmatch(r"nit\.?", lines[idx+1], flags=re.I):
            val = _clean_supplier_name(line)
            if (len(val) >= 5 and not _looks_like_technical_provider(val)
                    and not re.search(r"^(factura|cliente|vendedor|direcci[oó]n|fecha)\b", val, re.I)
                    and not re.search(r"\d{6,}", val)):
                candidates.append((10, val))
    # A legal seller name commonly contains SAS/LTDA and is much stronger
    # evidence than labels such as DIRECCIÓN, NIT or FECHA.
    for line in lines[:60]:
        val = _clean_supplier_name(line)
        if (re.search(r"\b(?:SAS|S\.?A\.?S\.?|LTDA|LIMITADA|S\.?A\.)\b", val, flags=re.I)
                and len(val) >= 5 and not _looks_like_technical_provider(val)
                and not re.search(r"\d{6,}", val)):
            candidates.append((12, val))

    if candidates:
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][1]
    return ""


def extract_structured_email_invoice_and_supplier(subject: str, body_text: str) -> Tuple[str, str]:
    """Read the common DIAN forwarding subject format: NIT;SELLER;INVOICE;... .

    This is a fallback after XML/PDF, but before the generic email text parser.
    It is useful when the attachment is missing/unreadable while the forwarding
    subject still contains the seller and invoice consecutive.
    """
    texts = [subject or "", body_text or ""]
    for text in texts:
        for raw_line in re.split(r"[\r\n]+", text):
            line = normalize_spaces(raw_line)
            if not line or ";" not in line:
                continue
            parts = [normalize_spaces(x) for x in line.split(";")]
            if len(parts) < 3:
                continue
            nit = re.sub(r"\D", "", parts[0])
            seller = _clean_supplier_name(parts[1])
            candidate = parts[2].strip()
            if not (6 <= len(nit) <= 15):
                continue
            if len(seller) < 3 or _looks_like_technical_provider(seller):
                continue
            # Invoice IDs in these forwarded messages are alphanumeric. Reject
            # obvious non-invoice fields and authorization artifacts.
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9 ._-]{1,30}", candidate, flags=re.I):
                continue
            invoice = _normalize_invoice_candidate(candidate)
            # In this explicit NIT;SELLER;INVOICE structure, a numeric
            # consecutive is valid too (some providers use long numeric
            # invoice numbers). Do not apply the generic bare-number rejection
            # here; the field position gives it strong context.
            if not invoice or len(invoice.replace(" ", "")) > 30:
                continue
            if re.fullmatch(r"[A-Z0-9][A-Z0-9 ._-]{1,30}", candidate, flags=re.I) is None:
                continue
            # A seller should look like a legal/business name or at least not a
            # date/phone/address field.
            if re.fullmatch(r"[\d .()\-]+", seller):
                continue
            return invoice, seller
    return "", ""


def extract_supplier_from_asunto_detalle(asunto: str) -> str:
    """Extract the supplier from common invoice-forwarding subject formats.

    Do NOT assume the supplier is always the second field.

    Supported common patterns include:
        NIT;PROVEEDOR;FACTURA;...
        PROVEEDOR;NIT;FACTURA;...
        PROVEEDOR;FACTURA;...
        NIT;PROVEEDOR;FACTURA;...

    Strategy:
      1) Split the subject/detail into semicolon-separated fields.
      2) Identify NIT-like numeric fields.
      3) Identify invoice-like fields.
      4) Treat a nearby text field that looks like a company/legal name
         as the supplier.
      5) If there is an explicit NIT + supplier + invoice structure, use it.
      6) Otherwise return empty and let the existing XML/PDF/email fallback
         decide, rather than guessing and risking SEBAS DUNCAN SAS.
    """
    text = decode_mime_header(asunto or "").strip()
    if not text or ";" not in text:
        return ""

    def looks_like_invoice_field(part: str) -> bool:
        value = normalize_spaces(part)
        if not value:
            return False
        if re.fullmatch(r"[\d .()\-]+", value):
            return False
        if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", value):
            return False
        if re.search(r"@|https?://", value, re.I):
            return False
        # Typical invoice/consecutive formats: FE94506, FEA 325,
        # FEPI 40716, PV15077, EMD326070635, etc.
        return bool(re.search(r"[A-Za-z]{1,8}[- ]?\d{2,}", value))

    for raw_line in re.split(r"[\r\n]+", text):
        line = normalize_spaces(raw_line)
        if not line or ";" not in line:
            continue

        parts = [normalize_spaces(x) for x in line.split(";") if normalize_spaces(x)]
        if len(parts) < 2:
            continue

        # Remove obvious empty/technical noise but preserve original order.
        nit_indexes = []
        invoice_indexes = []

        for i, part in enumerate(parts):
            digits = re.sub(r"\D", "", part)

            # Colombian NIT-like field.
            if 6 <= len(digits) <= 15 and re.fullmatch(r"[\d .-]+", part):
                nit_indexes.append(i)

            # Invoice-like field: contains letters/numbers and is not merely
            # a date, phone, money amount, NIT or generic quantity.
            if looks_like_invoice_field(part):
                invoice_indexes.append(i)

        candidates = []

        # A supplier is commonly adjacent to the NIT and invoice.
        for nit_idx in nit_indexes:
            for inv_idx in invoice_indexes:
                if inv_idx == nit_idx:
                    continue

                for supplier_idx in range(len(parts)):
                    if supplier_idx in (nit_idx, inv_idx):
                        continue

                    supplier = _clean_supplier_name(parts[supplier_idx])
                    if len(supplier) < 3:
                        continue
                    if _looks_like_technical_provider(supplier):
                        continue
                    if re.fullmatch(r"[\d .()\-]+", supplier):
                        continue

                    # Prefer suppliers between NIT and invoice, then suppliers
                    # immediately adjacent to either field.
                    distance = abs(supplier_idx - nit_idx) + abs(supplier_idx - inv_idx)
                    between = min(nit_idx, inv_idx) < supplier_idx < max(nit_idx, inv_idx)

                    score = 0
                    if between:
                        score += 100
                    if supplier_idx in (nit_idx - 1, nit_idx + 1, inv_idx - 1, inv_idx + 1):
                        score += 30
                    score -= distance

                    # Legal/company-name signals.
                    upper = supplier.upper()
                    if any(term in upper for term in (
                        "SAS", "S.A.S", "LTDA", "S.A.", "S A S", "LIMITADA",
                        "COMERCIAL", "IMPORTADORA", "DISTRIBUIDORA",
                        "INDUSTRIAS", "TEXTILES", "SISTEMAS",
                    )):
                        score += 15

                    candidates.append((score, supplier))

        if candidates:
            candidates.sort(key=lambda x: (-x[0], x[1].lower()))
            return candidates[0][1]

        # Conservative fallback for a simple PROVEEDOR;FACTURA format.
        for i, part in enumerate(parts):
            supplier = _clean_supplier_name(part)
            if len(supplier) < 3:
                continue
            if _looks_like_technical_provider(supplier):
                continue
            if re.fullmatch(r"[\d .()\-]+", supplier):
                continue

            # If the next field looks like an invoice, this is a plausible
            # supplier-first structure. Require a company-name signal so we
            # don't accidentally classify arbitrary text as a provider.
            if i + 1 < len(parts) and looks_like_invoice_field(parts[i + 1]):
                upper = supplier.upper()
                if any(term in upper for term in (
                    "SAS", "S.A.S", "LTDA", "S.A.", "LIMITADA",
                    "COMERCIAL", "IMPORTADORA", "DISTRIBUIDORA",
                    "INDUSTRIAS", "TEXTILES", "SISTEMAS",
                )):
                    return supplier

    return ""

def extract_supplier_name_from_email_text(subject: str, body_text: str) -> str:
    """Find the seller in common forwarded invoice subject/body structures."""
    _, supplier = extract_structured_email_invoice_and_supplier(subject, body_text)
    return supplier


def extract_supplier_name_from_attachments(msg: email.message.Message) -> str:
    for filename, payload in iter_supported_attachment_files(msg):
        if filename.lower().endswith(".xml"):
            name = extract_supplier_name_from_xml_bytes(payload)
            if name:
                return name
    for filename, payload in iter_supported_attachment_files(msg):
        if filename.lower().endswith(".pdf"):
            name = extract_supplier_name_from_text(extract_text_from_pdf_bytes(payload))
            if name:
                return name
    return ""

def extract_invoice_number_from_attachments(msg: email.message.Message) -> str:
    # XML has priority because it contains the structured DIAN document ID.
    for filename, payload in iter_supported_attachment_files(msg):
        if filename.lower().endswith(".xml"):
            number = extract_invoice_number_from_xml_bytes(payload)
            if number:
                return number

    # Then use the PDF's visible invoice context.
    for _name, text in extract_attachment_texts(msg):
        number = extract_invoice_number(text)
        if number:
            return number
    return ""


def extract_best_value_from_attachments(msg: email.message.Message) -> Optional[Tuple[int, str, str]]:
    """Extract invoice total using labeled PDF evidence or structured XML."""
    attachment_files = iter_supported_attachment_files(msg)
    xml_candidates: List[Tuple[int, str, str]] = []
    pdf_candidates: List[Tuple[int, str, str]] = []

    # Collect XML candidates without returning immediately.
    for filename, payload in attachment_files:
        if filename.lower().endswith(".xml"):
            vals = extract_values_from_xml_bytes(payload)
            if vals:
                xml_candidates.extend(vals)

    # Collect only explicitly labeled totals from PDFs.
    for filename, payload in attachment_files:
        if filename.lower().endswith(".pdf"):
            pdf_text = extract_text_from_pdf_bytes(payload)
            if pdf_text:
                best = find_best_total_by_labels(pdf_text)
                if best:
                    pdf_candidates.append(best)

    # If the PDF explicitly says TOTAL NETO / TOTAL A PAGAR / TOTAL, use
    # that visible invoice total. This fixes EFFISYSTEMS FE-94506, where the
    # PDF shows TOTAL NETO $100,000 but an ambiguous XML field can be 1.
    if pdf_candidates:
        pdf_candidates.sort(key=lambda x: x[0], reverse=True)
        return pdf_candidates[0]

    # Otherwise use the structured XML amount.
    if xml_candidates:
        xml_candidates.sort(key=lambda x: x[0], reverse=True)
        return xml_candidates[0]

    return None

def attachments_contain_keyword(msg: email.message.Message) -> Optional[str]:
    for _, text in extract_attachment_texts(msg):
        keyword = contains_keyword(text)
        if keyword:
            return keyword
    return None


def attachments_mention_special_supplier(msg: email.message.Message) -> bool:
    for _, text in extract_attachment_texts(msg):
        if text_mentions_special_supplier(text):
            return True
    return False


def connect_imap(cfg: Config):
    if cfg.use_ssl_imap:
        client = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    else:
        client = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
    client.login(cfg.email_address, cfg.email_password)
    client.select(cfg.inbox_folder)
    return client


def connect_smtp(cfg: Config):
    if cfg.use_ssl_smtp:
        smtp = smtplib.SMTP_SSL(
            cfg.smtp_host,
            cfg.smtp_port,
            context=ssl.create_default_context(),
        )
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        smtp.ehlo()
        if cfg.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
    smtp.login(cfg.email_address, cfg.email_password)
    return smtp


def search_candidates(mail, start_dt: datetime, end_dt: datetime) -> List[bytes]:
    since_str = start_dt.strftime("%d-%b-%Y")
    before_str = (end_dt + timedelta(days=1)).strftime("%d-%b-%Y")
    status, data = mail.search(None, "SINCE", since_str, "BEFORE", before_str)
    if status != "OK":
        raise RuntimeError("No se pudo ejecutar la búsqueda IMAP.")
    return data[0].split()


def fetch_message(mail, num: bytes) -> Tuple[str, email.message.Message]:
    status, msg_data = mail.fetch(num, "(RFC822 UID)")
    if status != "OK":
        raise RuntimeError(f"No se pudo leer el mensaje IMAP: {num!r}")

    uid = ""
    raw_email = None
    for item in msg_data:
        if isinstance(item, tuple):
            meta = item[0].decode(errors="ignore")
            raw_email = item[1]
            m = re.search(r"UID (\d+)", meta)
            if m:
                uid = m.group(1)

    if raw_email is None:
        raise RuntimeError(f"Mensaje sin contenido: {num!r}")

    msg = email.message_from_bytes(raw_email)
    return uid, msg


def invoice_exists_today(db_path: Path, stored_date: str, message_id: str, value_int: int, invoice_number: str) -> bool:
    if _using_postgres():
        with closing(_pg_connect()) as conn:
            cur = conn.cursor()
            if invoice_number:
                cur.execute(
                    """
                    SELECT 1 FROM facturas
                    WHERE invoice_number = %s AND valor_entero = %s
                    LIMIT 1
                    """,
                    (invoice_number, value_int),
                )
                if cur.fetchone() is not None:
                    return True
            cur.execute(
                """
                SELECT 1 FROM facturas
                WHERE message_id = %s AND valor_entero = %s
                LIMIT 1
                """,
                (message_id, value_int),
            )
            return cur.fetchone() is not None

    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.cursor()
        if invoice_number:
            cur.execute(
                """
                SELECT 1 FROM facturas
                WHERE stored_date = ? AND invoice_number = ? AND valor_entero = ?
                LIMIT 1
                """,
                (stored_date, invoice_number, value_int),
            )
            if cur.fetchone() is not None:
                return True
        cur.execute(
            """
            SELECT 1 FROM facturas
            WHERE stored_date = ? AND message_id = ? AND valor_entero = ?
            LIMIT 1
            """,
            (stored_date, message_id, value_int),
        )
        return cur.fetchone() is not None


def insert_invoice(db_path: Path, rec: InvoiceRecord) -> bool:
    if invoice_exists_today(db_path, rec.stored_date, rec.message_id, rec.valor_entero, rec.invoice_number):
        logging.info(
            "Factura duplicada omitida: fecha=%s numero=%s valor=%s",
            rec.stored_date, rec.invoice_number, rec.valor_entero
        )
        return False

    sql = """
        INSERT INTO facturas (
            message_id, uid, invoice_number, email_date, stored_date, remitente_fijo,
            remitente_real, asunto, valor_entero, moneda, palabra_clave
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        rec.message_id, rec.uid, rec.invoice_number, rec.email_date, rec.stored_date,
        rec.remitente_fijo, rec.remitente_real, rec.asunto, rec.valor_entero,
        rec.moneda, rec.palabra_clave,
    )

    try:
        if _using_postgres():
            with closing(_pg_connect()) as conn:
                cur = conn.cursor()
                cur.execute(_sql(sql), params)
                inserted = cur.rowcount == 1
                conn.commit()
        else:
            with closing(sqlite3.connect(db_path)) as conn:
                cur = conn.cursor()
                cur.execute(sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1), params)
                inserted = cur.rowcount == 1
                conn.commit()
    except Exception:
        logging.exception("Error insertando factura %s", rec.invoice_number)
        raise

    if not inserted:
        logging.info(
            "Factura duplicada omitida: fecha=%s numero=%s valor=%s",
            rec.stored_date, rec.invoice_number, rec.valor_entero
        )
    return inserted


def _row_to_invoice(row) -> InvoiceRecord:
    if isinstance(row, dict):
        get = row.get
    else:
        get = lambda k: row[k]
    return InvoiceRecord(
        message_id=get("message_id"),
        uid=get("uid") or "",
        invoice_number=get("invoice_number") or "",
        email_date=get("email_date"),
        stored_date=get("stored_date"),
        remitente_fijo=get("remitente_fijo"),
        remitente_real=get("remitente_real") or "",
        asunto=get("asunto") or "",
        valor_entero=get("valor_entero"),
        moneda=get("moneda") or "N/A",
        palabra_clave=get("palabra_clave") or "",
    )


def get_stored_invoices(db_path: Path, stored_date: str) -> List[InvoiceRecord]:
    sql = """
        SELECT message_id, uid, COALESCE(invoice_number, '') AS invoice_number,
               email_date, stored_date, remitente_fijo, remitente_real,
               asunto, valor_entero, moneda, palabra_clave
        FROM facturas WHERE stored_date = ? ORDER BY id ASC
    """
    if _using_postgres():
        with closing(_pg_connect()) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(_sql(sql), (stored_date,))
                rows = cur.fetchall()
        return [_row_to_invoice(row) for row in rows]

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql, (stored_date,))
        rows = cur.fetchall()
    return [_row_to_invoice(row) for row in rows]


def get_all_stored_invoices(db_path: Path = DB_PATH) -> List[InvoiceRecord]:
    sql = """
        SELECT message_id, uid, COALESCE(invoice_number, '') AS invoice_number,
               email_date, stored_date, remitente_fijo, remitente_real,
               asunto, valor_entero, moneda, palabra_clave
        FROM facturas ORDER BY id ASC
    """
    if _using_postgres():
        with closing(_pg_connect()) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        return [_row_to_invoice(row) for row in rows]

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
    return [_row_to_invoice(row) for row in rows]


def process_mail_once(
    cfg: Config,
    recheck: bool = True,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    if start_date and end_date:
        start_dt, end_dt, stored_date = build_custom_range_local(start_date, end_date)
        logging.info("Iniciando proceso para rango %s - %s", start_date, end_date)
    else:
        now = datetime.now().astimezone()
        stored_date = now.strftime("%Y-%m-%d")
        start_dt, end_dt = build_day_range_local(now)
        logging.info("Iniciando proceso para el día %s", stored_date)

    inserted_first_pass = 0
    inserted_second_pass = 0
    skipped_excluded = 0
    candidate_messages = 0

    # Historical rebuilds can contain hundreds of PDF/XML attachments.
    # Process them in small batches so a 512 MB Render instance does not
    # accumulate parsed email/PDF objects until the very end. Each batch is
    # persisted and synced to Sheets before moving on to the next one.
    batch_size = 25 if (start_date and end_date) else 50

    with connect_imap(cfg) as mail:
        ids = search_candidates(mail, start_dt, end_dt)
        candidate_messages = len(ids)
        logging.info(
            "Mensajes candidatos: %s | procesamiento por lotes de %s",
            candidate_messages, batch_size,
        )

        for batch_start in range(0, len(ids), batch_size):
            batch = ids[batch_start:batch_start + batch_size]
            batch_number = batch_start // batch_size + 1
            total_batches = (len(ids) + batch_size - 1) // batch_size
            logging.info(
                "Procesando lote %s/%s (%s mensajes)...",
                batch_number, total_batches, len(batch),
            )

            batch_inserted, batch_skipped = _scan_and_store(
                mail=mail,
                ids=batch,
                db_path=DB_PATH,
                stored_date=stored_date,
                start_dt=start_dt,
                end_dt=end_dt,
                cfg=cfg,
            )
            inserted_first_pass += batch_inserted
            skipped_excluded += batch_skipped

            # Release the batch's message/attachment objects before the next
            # batch. gc.collect() is intentional here because PDF parsers can
            # retain cyclic references for a while.
            del batch
            gc.collect()

            # For historical/custom ranges, the first pass is enough. The old
            # second pass doubled the amount of PDF/XML work and was a major
            # source of memory pressure. Normal daily runs keep recheck=True.
            if cfg.google_sheets_enabled:
                try:
                    batch_records = get_all_stored_invoices(DB_PATH)
                    sync_result = sync_to_google_sheets(cfg, batch_records)
                    logging.info(
                        "Lote %s/%s sincronizado: %s",
                        batch_number, total_batches, sync_result,
                    )
                    del batch_records
                except Exception as exc:
                    logging.exception(
                        "No fue posible sincronizar Google Sheets tras el lote %s: %s",
                        batch_number, exc,
                    )
                gc.collect()

        if recheck and not (start_date and end_date):
            logging.info("Ejecutando segunda verificación del filtro...")
            ids_again = search_candidates(mail, start_dt, end_dt)
            inserted_second_pass, _ = _scan_and_store(
                mail=mail,
                ids=ids_again,
                db_path=DB_PATH,
                stored_date=stored_date,
                start_dt=start_dt,
                end_dt=end_dt,
                cfg=cfg,
            )
            del ids_again
            gc.collect()

    stored_records = get_stored_invoices(DB_PATH, stored_date)
    total_count = len(stored_records)
    total_sum = sum(r.valor_entero for r in stored_records)

    sheets_result = {"enabled": False, "inserted": 0}
    try:
        # Sync the complete SQLite history, not only today's records, so the
        # monthly analysis can be rebuilt consistently after every run.
        all_records = get_all_stored_invoices(DB_PATH)
        sheets_result = sync_to_google_sheets(cfg, all_records)
    except Exception as exc:
        logging.exception("No fue posible sincronizar Google Sheets: %s", exc)

    send_summary_email(cfg, stored_date, total_count, total_sum, stored_records)

    result = {
        "stored_date": stored_date,
        "candidate_messages": candidate_messages,
        "skipped_excluded": skipped_excluded,
        "inserted_first_pass": inserted_first_pass,
        "inserted_second_pass": inserted_second_pass,
        "total_count": total_count,
        "total_sum": total_sum,
        "google_sheets": sheets_result,
    }

    logging.info("Proceso finalizado: %s", result)
    return result


def _scan_and_store(
    mail,
    ids: Iterable[bytes],
    db_path: Path,
    stored_date: str,
    start_dt: datetime,
    end_dt: datetime,
    cfg: Config,
) -> Tuple[int, int]:
    inserted = 0
    skipped_excluded = 0

    for num in ids:
        try:
            uid, msg = fetch_message(mail, num)
            msg_date = get_email_date(msg).astimezone()

            if not (start_dt <= msg_date <= end_dt):
                continue

            subject = decode_mime_header(msg.get("Subject", ""))
            from_real = decode_mime_header(msg.get("From", ""))
            message_id = msg.get("Message-ID", "").strip() or f"NO_MESSAGE_ID_{uid}"
            body_text = get_message_text(msg)

            if cfg.exclude_email and cfg.exclude_email in from_real.lower():
                continue

            if REPORT_SUBJECT.lower() in subject.lower():
                continue

            haystack = f"{subject}\n{body_text}"
            body_keyword = contains_keyword(haystack)

            special_supplier = text_mentions_special_supplier(haystack)
            attachment_keyword = None
            attachment_special = False

            if special_supplier:
                attachment_keyword = attachments_contain_keyword(msg) if not body_keyword else None
            else:
                attachment_special = attachments_mention_special_supplier(msg)
                if attachment_special:
                    attachment_keyword = attachments_contain_keyword(msg) if not body_keyword else None

            is_special_case = special_supplier or attachment_special

            if not is_special_case and (contains_excluded_term(subject) or contains_excluded_term(body_text)):
                skipped_excluded += 1
                continue

            keyword = body_keyword or attachment_keyword
            if not keyword:
                continue

            # The invoice document is the source of truth. Prefer XML/PDF
            # over the email subject/body because email templates vary by sender.
            attachment_names = [name for name, _payload in iter_supported_attachment_files(msg)]
            # Some forwarding systems include a highly reliable structured
            # record in the email body/subject: NIT;PROVEEDOR;FACTURA;...
            # When present, this structured seller/consecutive pair takes
            # priority over the PDF/XML extraction because it is already the
            # normalized DIAN forwarding data used by the sender.
            structured_invoice, structured_supplier = extract_structured_email_invoice_and_supplier(
                subject, body_text
            )

            attachment_names = [name for name, _payload in iter_supported_attachment_files(msg)]
            invoice_number = structured_invoice or extract_invoice_number_from_attachments(msg)
            supplier_name = structured_supplier or extract_supplier_name_from_attachments(msg)
            logging.info(
                "DIAGNOSTICO ADJUNTOS | uid=%s | archivos=%s | factura_doc=%s | proveedor_doc=%s | estructurado=%s",
                uid, attachment_names, invoice_number or "VACIO", supplier_name or "VACIO",
                "SI" if structured_invoice or structured_supplier else "NO",
            )

            if not invoice_number:
                invoice_number = extract_invoice_number(haystack)
                if invoice_number:
                    logging.info("DIAGNOSTICO FACTURA | fuente=email | factura=%s", invoice_number)
            # The forwarding subject/detail is the preferred supplier source.
            # It commonly has: NIT;PROVEEDOR;FACTURA;...
            # This avoids labeling every invoice as SEBAS DUNCAN SAS merely
            # because the forwarding mailbox sent the email.
            asunto_supplier = extract_supplier_from_asunto_detalle(subject)
            if asunto_supplier:
                supplier_name = asunto_supplier
                logging.info(
                    "DIAGNOSTICO PROVEEDOR | fuente=asunto_detalle | proveedor=%s",
                    supplier_name,
                )

            if not supplier_name:
                supplier_name = extract_supplier_name_from_email_text(subject, body_text)
                if supplier_name:
                    logging.info(
                        "DIAGNOSTICO PROVEEDOR | fuente=email_estructurado | proveedor=%s",
                        supplier_name,
                    )
            if not supplier_name:
                supplier_name = _supplier_name(from_real)
                logging.info("DIAGNOSTICO PROVEEDOR | fuente=remitente | proveedor=%s", supplier_name)

            # The document is the source of truth for the amount too. Prefer
            # XML/PDF TOTAL/PayableAmount over the email body, because email
            # templates often contain unrelated numbers such as NITs, IDs or
            # message metadata. Only fall back to the email when the attachment
            # has no usable total.
            best_value = extract_best_value_from_attachments(msg)
            if best_value is None:
                best_value = find_best_total_by_labels(haystack)

            if best_value is None:
                logging.info(
                    "DIAGNOSTICO VALOR | uid=%s | valor=VACIO | motivo=sin_total_etiquetado",
                    uid,
                )
                continue

            value_int, currency, literal = best_value
            logging.info(
                "DIAGNOSTICO VALOR | uid=%s | valor=%s | moneda=%s | literal=%r",
                uid, value_int, currency, literal,
            )

            for value_int, currency, _literal in [best_value]:
                rec = InvoiceRecord(
                    message_id=message_id,
                    uid=uid,
                    invoice_number=invoice_number,
                    email_date=msg_date.strftime("%a, %d %b %Y %H:%M:%S %z"),
                    stored_date=stored_date,
                    remitente_fijo=FIXED_SENDER_NAME,
                    remitente_real=supplier_name or from_real,
                    asunto=subject,
                    valor_entero=value_int,
                    moneda=currency,
                    palabra_clave=keyword,
                )
                if insert_invoice(db_path, rec):
                    inserted += 1

        except Exception as exc:
            logging.exception("Error procesando mensaje %r: %s", num, exc)

    return inserted, skipped_excluded


def _require_google_sheets(cfg: Config):
    """Create a gspread client using Google OAuth user credentials.

    The first execution opens the browser for authorization and stores the
    refresh token locally in token.json. Later executions reuse token.json.
    """
    if not gspread:
        raise RuntimeError(
            "Falta la librería gspread. Instala dependencias con: pip install -r requirements.txt"
        )
    if not Credentials or not InstalledAppFlow:
        raise RuntimeError(
            "Faltan las librerías OAuth de Google. Instala: "
            "pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )
    if not cfg.google_spreadsheet_id:
        raise RuntimeError("Falta GOOGLE_SPREADSHEET_ID en .env")

    credentials_path = Path(cfg.google_credentials_file or "credentials.json")
    if not credentials_path.is_absolute():
        credentials_path = ROOT_DIR / credentials_path
    if not credentials_path.exists():
        raise RuntimeError(
            f"No existe el archivo de credenciales OAuth: {credentials_path}"
        )

    token_env = os.getenv("GOOGLE_TOKEN_FILE", "").strip()
    token_path = Path(token_env) if token_env else (ROOT_DIR / "token.json")
    if not token_path.is_absolute():
        token_path = ROOT_DIR / token_path

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except Exception:
            logging.warning("No se pudo leer token.json; se solicitará autorización nuevamente.")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            if Request is None:
                raise RuntimeError("Falta google-auth para renovar token.json.")
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), scopes
            )
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json(), encoding="utf-8")

    return gspread.authorize(creds)

def _supplier_name(from_header: str) -> str:
    """Reduce 'NOMBRE <correo>' a 'NOMBRE', como se muestra en la hoja histórica."""
    text = decode_mime_header(from_header or "").strip()
    text = re.sub(r"\s*<[^>]+>\s*", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text or from_header or "Desconocido"


def _sheet_message_key(message_id: str, invoice_number: str, value: int) -> str:
    """Stable compact key for column D without exposing the full Message-ID."""
    raw = f"{message_id}|{invoice_number}|{value}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:32]


def _ensure_header_row(ws) -> None:
    """Create headers only when the worksheet is empty."""
    if not ws.get_all_values():
        ws.append_row(
            [
                "Proveedor",
                "Fecha correo",
                "Factura",
                "ID documento",
                "Valor",
                "Asunto / detalle",
                "Keyword",
            ],
            value_input_option="USER_ENTERED",
        )


def _load_existing_sheet_keys(ws) -> set:
    values = ws.get_all_values()
    keys = set()
    for row in values:
        if len(row) >= 4 and row[3]:
            keys.add(str(row[3]).strip())
    return keys


def _month_key_from_email_date(email_date: str, stored_date: str) -> str:
    """Return YYYY-MM from ISO dates, email dates, or legacy stored values."""
    # The existing SQLite database stores email_date as YYYY-MM-DD.
    # parsedate_to_datetime() is for RFC 2822 mail dates and does not handle
    # a plain ISO date reliably, so try ISO first.
    for value in (email_date, stored_date):
        if not value:
            continue
        value = str(value).strip()
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m")
        except Exception:
            pass
        try:
            dt = parsedate_to_datetime(value)
            return dt.strftime("%Y-%m")
        except Exception:
            pass

    # Legacy values such as "rango-2026-03-01_a_2026-03-31".
    match = re.search(r"(20\d{2})-(\d{2})", str(stored_date or ""))
    if match:
        return f"{match.group(1)}-{match.group(2)}"

    # Never let a malformed legacy value reach datetime.strptime().
    return ""


def _analysis_data(records: List[InvoiceRecord]) -> Tuple[List[str], List[List[object]]]:
    """Build the provider/month matrix used by the original 'Análisis' sheet."""
    totals: Dict[str, Dict[str, int]] = {}
    months = set()

    for rec in records:
        provider = extract_supplier_from_asunto_detalle(rec.asunto) or _supplier_name(rec.remitente_real)
        month = _month_key_from_email_date(rec.email_date, rec.stored_date)
        if not month:
            logging.warning(
                "Factura %s omitida del análisis por fecha no reconocida: email_date=%r stored_date=%r",
                rec.invoice_number, rec.email_date, rec.stored_date
            )
            continue
        months.add(month)
        totals.setdefault(provider, {})[month] = totals.setdefault(provider, {}).get(month, 0) + rec.valor_entero

    month_list = sorted(months)
    if not month_list:
        return ["Proveedor", "Total"], []

    # Keep the most recent months visible first? The historical sheet uses Apr, May.
    # We keep chronological order because it makes month-over-month comparison unambiguous.
    headers = ["Proveedor"] + [datetime.strptime(m, "%Y-%m").strftime("%b %Y").title() for m in month_list] + ["Total", "Δ mes ant"]

    rows: List[List[object]] = []
    for provider in sorted(totals, key=lambda x: x.lower()):
        monthly = [totals[provider].get(m, 0) for m in month_list]
        total = sum(monthly)
        if len(monthly) < 2 or monthly[-2] == 0:
            delta = "NUEVO" if monthly[-1] > 0 else 0
        else:
            delta = (monthly[-1] - monthly[-2]) / monthly[-2]
        rows.append([provider] + monthly + [total, delta])

    total_months = [sum(totals[p].get(m, 0) for p in totals) for m in month_list]
    grand_total = sum(total_months)
    if len(total_months) < 2 or total_months[-2] == 0:
        grand_delta = "NUEVO" if total_months[-1] > 0 else 0
    else:
        grand_delta = (total_months[-1] - total_months[-2]) / total_months[-2]
    rows.append(["TOTAL"] + total_months + [grand_total, grand_delta])
    return headers, rows


def sync_to_google_sheets(cfg: Config, records: List[InvoiceRecord]) -> dict:
    """Sync SQLite records to the invoice sheet and rebuild the monthly analysis."""
    if not cfg.google_sheets_enabled:
        return {"enabled": False, "inserted": 0}

    client = _require_google_sheets(cfg)
    spreadsheet = client.open_by_key(cfg.google_spreadsheet_id)

    try:
        facturas_ws = spreadsheet.worksheet(cfg.google_facturas_sheet)
    except Exception:
        facturas_ws = spreadsheet.add_worksheet(title=cfg.google_facturas_sheet, rows=1000, cols=10)

    try:
        analysis_ws = spreadsheet.worksheet(cfg.google_analysis_sheet)
    except Exception:
        analysis_ws = spreadsheet.add_worksheet(title=cfg.google_analysis_sheet, rows=1000, cols=20)

    _ensure_header_row(facturas_ws)
    existing_keys = _load_existing_sheet_keys(facturas_ws)
    rows_to_append = []

    for rec in records:
        key = _sheet_message_key(rec.message_id, rec.invoice_number, rec.valor_entero)
        if key in existing_keys:
            continue

        # Column F stores the original subject/detail. If it contains the
        # forwarding structure NIT;PROVEEDOR;FACTURA;..., use that supplier
        # for column A. Otherwise keep the supplier already stored in DB.
        sheet_supplier = extract_supplier_from_asunto_detalle(rec.asunto)
        if not sheet_supplier:
            sheet_supplier = _supplier_name(rec.remitente_real)

        rows_to_append.append([
            sheet_supplier,
            rec.email_date,
            rec.invoice_number or "N/A",
            key,
            rec.valor_entero,
            rec.asunto or "",
            rec.palabra_clave or "",
        ])
        existing_keys.add(key)

    if rows_to_append:
        facturas_ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")

    # Rebuild analysis from SQLite so it always represents the complete history.
    headers, analysis_rows = _analysis_data(records)
    analysis_ws.clear()
    all_rows = [headers] + analysis_rows
    analysis_ws.update(range_name="A1", values=all_rows, value_input_option="USER_ENTERED")

    # Basic formatting matching the historical sheet: dark header + percentage on last column.
    try:
        facturas_ws.freeze(rows=1)
        analysis_ws.freeze(rows=1)
        analysis_ws.format("1:1", {
            "backgroundColor": {"red": 0.12, "green": 0.27, "blue": 0.47},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "horizontalAlignment": "CENTER",
        })
        analysis_ws.format(f"B2:{chr(65 + len(headers) - 1)}{len(all_rows)}", {
            "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}
        })
        if len(headers) >= 3:
            last_col = chr(65 + len(headers) - 1)
            analysis_ws.format(f"{last_col}2:{last_col}{len(all_rows)}", {
                "numberFormat": {"type": "PERCENT", "pattern": "0%"}
            })
    except Exception as exc:
        logging.warning("No se pudo aplicar formato de Google Sheets: %s", exc)

    result = {
        "enabled": True,
        "inserted": len(rows_to_append),
        "analysis_rows": len(analysis_rows),
        "spreadsheet_id": cfg.google_spreadsheet_id,
        "facturas_sheet": cfg.google_facturas_sheet,
        "analysis_sheet": cfg.google_analysis_sheet,
    }
    logging.info("Google Sheets sincronizado: %s", result)
    return result


def format_number(n: int) -> str:
    return f"{n:,}"


def send_summary_email(
    cfg: Config,
    stored_date: str,
    total_count: int,
    total_sum: int,
    records: List[InvoiceRecord],
) -> None:
    lines = [
        "Informe ejecutivo de facturas SAS",
        "",
        f"Fecha del proceso: {stored_date}",
        f"Cantidad de facturas generadas en el día: {total_count}",
        f"Suma total de las facturas: {format_number(total_sum)}",
        "",
        "Detalle discriminado por factura/invoice guardada:",
    ]

    if records:
        for idx, rec in enumerate(records, start=1):
            extra_num = f" | Factura: {rec.invoice_number}" if rec.invoice_number else ""
            lines.append(
                f"{idx}. Valor: {format_number(rec.valor_entero)}{extra_num} | "
                f"Remitente fijo: {rec.remitente_fijo} | "
                f"Asunto: {rec.asunto or '(sin asunto)'} | "
                f"Keyword: {rec.palabra_clave}"
            )
    else:
        lines.append("No se encontraron facturas/invoices válidas para guardar hoy.")

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["From"] = cfg.email_address
    msg["To"] = cfg.report_to
    msg["Subject"] = REPORT_SUBJECT
    msg.set_content(body)

    with connect_smtp(cfg) as smtp:
        smtp.send_message(msg)

    logging.info("Resumen enviado a %s", cfg.report_to)


def run_scheduler(cfg: Config) -> None:
    if schedule is None:
        raise RuntimeError(
            "No está instalada la librería 'schedule'. Instálala con: pip install schedule"
        )

    logging.info("Programando ejecución diaria a las %s", cfg.run_time)
    schedule.every().day.at(cfg.run_time).do(process_mail_once, cfg=cfg, recheck=True)

    while True:
        schedule.run_pending()
        time.sleep(15)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bot diario de facturas por correo")
    parser.add_argument("--run-now", action="store_true", help="Ejecuta el proceso inmediatamente")
    parser.add_argument("--scheduler", action="store_true", help="Deja el proceso programado en ejecución")
    parser.add_argument("--sync-sheets", action="store_true", help="Sincroniza SQLite con Google Sheets sin buscar nuevos correos")
    parser.add_argument("--start-date", type=str, help="Fecha inicial en formato YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, help="Fecha final en formato YYYY-MM-DD")
    return parser.parse_args()


def main() -> int:
    setup_logging()

    try:
        init_db()
        cfg = load_config()
        args = parse_args()

        if (args.start_date and not args.end_date) or (args.end_date and not args.start_date):
            raise ValueError("Debes usar --start-date y --end-date juntos.")

        if args.run_now:
            # Custom historical ranges are processed in a single pass.
            # Daily runs retain the original second verification.
            historical_range = bool(args.start_date and args.end_date)
            result = process_mail_once(
                cfg,
                recheck=not historical_range,
                start_date=args.start_date,
                end_date=args.end_date,
            )
            print(result)
            return 0

        if args.sync_sheets:
            records = get_all_stored_invoices(DB_PATH)
            result = sync_to_google_sheets(cfg, records)
            print(result)
            return 0

        if args.scheduler:
            run_scheduler(cfg)
            return 0

        print("Debes indicar una opción: --run-now, --sync-sheets o --scheduler")
        return 1

    except Exception as exc:
        logging.exception("Error fatal: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
