#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, hashlib, os, re
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import psycopg2

BASE = Path(__file__).resolve().parent
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "1cHEw8eLyw1K1L-Ti0uiTa9Lsb-HkYTlKwqgNBLHd0Fw")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

def client():
    cred = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
    token = Path(os.getenv("GOOGLE_TOKEN_FILE", "token.json"))
    if not cred.is_absolute(): cred = BASE / cred
    if not token.is_absolute(): token = BASE / token
    if not token.exists():
        raise RuntimeError("No existe token.json. El OAuth local debe estar autorizado primero.")
    c = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not c.valid:
        if c.expired and c.refresh_token:
            c.refresh(Request())
            token.write_text(c.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("token.json no es válido y no tiene refresh token.")
    return gspread.authorize(c)

def clean(v): return str(v or "").strip()

def parse_date(v):
    s = clean(v)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try: return datetime.strptime(s[:19], fmt).strftime("%Y-%m-%d")
        except Exception: pass
    try: return parsedate_to_datetime(s).strftime("%Y-%m-%d")
    except Exception: pass
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", s)
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""

def parse_value(v):
    s = clean(v).replace("$", "").replace("COP", "").replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s): s = s.replace(".", "")
    s = re.sub(r"[^\d-]", "", s)
    return int(s) if s not in ("", "-") else 0

def schema(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS facturas (
            id BIGSERIAL PRIMARY KEY, message_id TEXT NOT NULL, uid TEXT,
            invoice_number TEXT, email_date TEXT NOT NULL, stored_date TEXT NOT NULL,
            remitente_fijo TEXT NOT NULL, remitente_real TEXT, asunto TEXT,
            valor_entero BIGINT NOT NULL, moneda TEXT, palabra_clave TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_msg_valor ON facturas(stored_date,message_id,valor_entero)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_invoice ON facturas(stored_date,invoice_number,valor_entero)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_facturas_invoice_value ON facturas(invoice_number,valor_entero)")
    conn.commit()

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); args = ap.parse_args()
    db = os.getenv("DATABASE_URL", "").strip()
    if not db: raise SystemExit("Falta DATABASE_URL")
    ws = client().open_by_key(SPREADSHEET_ID).worksheet(os.getenv("GOOGLE_FACTURAS_SHEET", "hoja1"))
    rows = ws.get_all_values()[1:]
    conn = psycopg2.connect(db); schema(conn)
    candidates, seen = [], set()
    for row in rows:
        row = list(row) + [""] * max(0, 7-len(row))
        provider, d, inv, tech, value, details, kw = row[:7]
        d, value = parse_date(d), parse_value(value)
        if not d or not value or not clean(provider): continue
        key = (clean(inv).lower(), value, d, clean(provider).lower())
        if key in seen: continue
        seen.add(key)
        candidates.append((clean(provider), d, clean(inv), clean(tech), value, clean(details), clean(kw)))
    print(f"Filas de hoja1: {len(rows)}")
    print(f"Candidatos únicos: {len(candidates)}")
    if not args.apply:
        print("VISTA PREVIA: no se modificó Postgres. Usa --apply si los números son correctos.")
        conn.close(); return
    inserted = 0
    with conn.cursor() as cur:
        for provider, d, inv, tech, value, details, kw in candidates:
            fingerprint = hashlib.sha256("|".join(map(str, (provider,d,inv,value,details,kw))).encode()).hexdigest()
            mid = "sheet-history:" + fingerprint
            cur.execute("""INSERT INTO facturas(
                message_id,uid,invoice_number,email_date,stored_date,
                remitente_fijo,remitente_real,asunto,valor_entero,moneda,palabra_clave)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (mid, tech, inv, d, d, provider, provider, details, value, "COP", kw))
            inserted += cur.rowcount
    conn.commit(); conn.close()
    print(f"Importadas a Postgres: {inserted}")

if __name__ == "__main__": main()
