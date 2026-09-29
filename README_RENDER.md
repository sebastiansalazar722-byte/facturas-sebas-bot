# Bot de facturas — Render V11

Conserva la lógica actual de extracción. Si `DATABASE_URL` existe, usa Render Postgres; localmente puede seguir usando `facturas.db`.

Archivos:
- `factura_bot_google_sheets_v11_render.py`: bot.
- `requirements.txt`: dependencias.
- `render.yaml`: Cron Job + Postgres.
- `migrar_hoja1_a_postgres.py`: migración de `hoja1` limpia.
- `.gitignore`: evita subir secretos y base local.

Nunca subas `.env`, `credentials.json`, `token.json` ni contraseñas a GitHub.

Los horarios del ejemplo son 13:00, 19:00 y 01:00 UTC = 08:00, 14:00 y 20:00 en Colombia.
