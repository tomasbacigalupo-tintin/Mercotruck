# Tablero de Supervisión de Programación — Mercotruck

Sitio estático (GitHub Pages) que muestra los KPIs de programación de cargas.
Se **auto-actualiza** vía GitHub Actions (cron cada ~20 min) leyendo Airtable, y el
tablero abierto se **refresca solo cada 5 min**. Los datos viajan **cifrados**
(AES-256-GCM) y se descifran en el navegador con una contraseña — el repo es
público pero los números (renta, ventas, clientes) no se leen sin la clave.

## Cómo funciona
- `build.py` — lee Airtable (Operaciones + Solicitudes de Programación), recalcula
  los KPIs, cifra el JSON con `SITE_PASSWORD` y escribe `public/index.html` +
  `public/data.enc.json`.
- `index_template.html` — el tablero (ECharts). Pide contraseña, hace `fetch` del
  `data.enc.json`, lo descifra (WebCrypto) y dibuja. Drill-down con doble clic.
- `.github/workflows/refresh.yml` — corre `build.py` y publica `public/` en Pages.

## Setup (una sola vez)
1. **Settings → Secrets and variables → Actions → New repository secret**, crear:
   - `AIRTABLE_PAT` — token de Airtable de **solo lectura** a la base *Mercotruck 1.1*
     (crear en https://airtable.com/create/tokens, scope `data.records:read` +
     `schema.bases:read`, acceso a la base Mercotruck 1.1).
   - `SITE_PASSWORD` — la contraseña con la que se entra al tablero.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. **Actions → Refrescar Tablero Mercotruck → Run workflow** (o esperá al cron).

URL: `https://<usuario>.github.io/<repo>/`

## Ajustar frecuencia
Editá el `cron` en `refresh.yml` (formato UTC). Ej. `*/15 * * * *` = cada 15 min.
