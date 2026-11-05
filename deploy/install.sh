#!/usr/bin/env bash
# =====================================================================
#  PM-WANI backend installer for Ubuntu EC2 (FreeRADIUS 3 already installed)
#
#  - PostgreSQL database "pmwani" (app tables + FreeRADIUS tables)
#  - FreeRADIUS "sql" module switched to PostgreSQL
#  - FastAPI backend as systemd service "pmwani-api" on 127.0.0.1:8000
#  - nginx on port 80 -> backend (API at /api/v1, docs at /docs, portal at /chilli)
#  - stops the old Flask POC portal (pmwani-portal)
#
#  Usage (from the project folder):
#     sudo PDOA_PHONES=9876543210 bash deploy/install.sh
#  PDOA_PHONES = mobile number(s) that become PDOA admins when they log in (comma separated).
#  Safe to re-run: keeps existing passwords/secrets, updates the code.
# =====================================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/pmwani-api
ENV_FILE=/etc/pmwani-api.env
RAD_DIR=/etc/freeradius/3.0
SQLMOD="$RAD_DIR/mods-available/sql"
DB_NAME=pmwani
DB_USER=pmwani

[[ $EUID -eq 0 ]] || { echo "Please run with sudo"; exit 1; }
[[ -f "$SRC_DIR/app/main.py" ]] || { echo "Run this from the pmwani-backend folder"; exit 1; }
[[ -d $RAD_DIR ]] || { echo "FreeRADIUS 3 not found at $RAD_DIR"; exit 1; }

getenv() { [[ -f $ENV_FILE ]] && sed -n "s/^$1=//p" "$ENV_FILE" | head -n1 || true; }
DB_PASS=$(getenv DB_PASS); DB_PASS=${DB_PASS:-$(openssl rand -hex 16)}
JWT_SECRET=$(getenv JWT_SECRET); JWT_SECRET=${JWT_SECRET:-$(openssl rand -hex 32)}
UAMSECRET=$(getenv CHILLI_UAMSECRET)
if [[ -z $UAMSECRET && -f /etc/pmwani-portal.env ]]; then
  UAMSECRET=$(sed -n 's/^CHILLI_UAMSECRET=//p' /etc/pmwani-portal.env | tail -n1)
fi
UAMSECRET=${UAMSECRET:-$(openssl rand -hex 12)}
PDOA_PHONES=${PDOA_PHONES:-$(getenv PDOA_PHONES)}
PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-$(getenv PUBLIC_BASE_URL)}
if [[ -z $PUBLIC_BASE_URL ]]; then
  IP=$(curl -fsS --max-time 3 https://checkip.amazonaws.com 2>/dev/null || true)
  PUBLIC_BASE_URL="http://${IP:-3.7.159.119}"
fi

echo "==> [1/7] Packages"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql freeradius-postgresql nginx python3-venv
systemctl enable --now postgresql

echo "==> [2/7] PostgreSQL database"
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  ELSE
    ALTER ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  END IF;
END \$\$;
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
fi
export PGPASSWORD="$DB_PASS"
PSQL=(psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -q)
if [[ $("${PSQL[@]}" -tAc "SELECT to_regclass('public.radacct') IS NOT NULL") != "t" ]]; then
  "${PSQL[@]}" -f "$RAD_DIR/mods-config/sql/main/postgresql/schema.sql"
  echo "    FreeRADIUS schema imported"
fi
unset PGPASSWORD

echo "==> [3/7] FreeRADIUS sql module -> PostgreSQL"
[[ -f ${SQLMOD}.orig ]] || cp "$SQLMOD" "${SQLMOD}.orig"       # pristine stock file
cp "$SQLMOD" "${SQLMOD}.before-postgres" 2>/dev/null || true    # whatever was running (e.g. MariaDB)
cp "${SQLMOD}.orig" "$SQLMOD"
sed -i -E \
  -e 's/^(\s*)driver = "rlm_sql_null"/\1driver = "rlm_sql_${dialect}"/' \
  -e 's/^(\s*)dialect = "sqlite"/\1dialect = "postgresql"/' \
  -e 's/^(\s*)#\s*server = "localhost"/\1server = "127.0.0.1"/' \
  -e 's/^(\s*)#\s*port = 3306/\1port = 5432/' \
  -e "s/^(\s*)#\s*login = \"radius\"/\1login = \"${DB_USER}\"/" \
  -e "s/^(\s*)#\s*password = \"radpass\"/\1password = \"${DB_PASS}\"/" \
  -e "s/^(\s*)radius_db = \"radius\"/\1radius_db = \"${DB_NAME}\"/" \
  "$SQLMOD"
for pat in 'dialect = "postgresql"' 'driver = "rlm_sql_\$\{dialect\}"' "password = \"${DB_PASS}\"" \
           "radius_db = \"${DB_NAME}\"" 'port = 5432'; do
  grep -qE "^\s*${pat}" "$SQLMOD" || { echo "!! could not patch $SQLMOD ($pat)"; \
    cp "${SQLMOD}.before-postgres" "$SQLMOD"; exit 1; }
done
chown freerad:freerad "$SQLMOD"; chmod 640 "$SQLMOD"
ln -sf ../mods-available/sql "$RAD_DIR/mods-enabled/sql"
if ! freeradius -XC > /tmp/freeradius-check.log 2>&1; then
  echo "!! FreeRADIUS config check failed, restoring previous config. Last lines:"
  tail -n 25 /tmp/freeradius-check.log
  cp "${SQLMOD}.before-postgres" "$SQLMOD"
  exit 1
fi
systemctl restart freeradius
echo "    FreeRADIUS now uses PostgreSQL"

echo "==> [4/7] Backend code"
id -u pmwani >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin pmwani
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/app"
cp -r "$SRC_DIR/app" "$SRC_DIR/requirements.txt" "$APP_DIR/"
[[ -d $APP_DIR/venv ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R pmwani:pmwani "$APP_DIR"

echo "==> [5/7] Configuration"
if [[ ! -f $ENV_FILE ]]; then
  cat > "$ENV_FILE" <<EOF
# PM-WANI backend settings (restart after editing: sudo systemctl restart pmwani-api)
DB_PASS=${DB_PASS}
DATABASE_URL=postgresql+psycopg://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}
JWT_SECRET=${JWT_SECRET}
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
PDOA_PHONES=${PDOA_PHONES}
CHILLI_UAMSECRET=${UAMSECRET}
OTP_DEMO=true
SMS_PROVIDER=demo
MSG91_AUTHKEY=
MSG91_TEMPLATE_ID=
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=
EOF
else
  # keep the file; only update values passed on this run
  [[ -n ${PDOA_PHONES} ]] && sed -i "s/^PDOA_PHONES=.*/PDOA_PHONES=${PDOA_PHONES}/" "$ENV_FILE"
fi
chown root:pmwani "$ENV_FILE"; chmod 640 "$ENV_FILE"

echo "==> [6/7] Database tables + default plans"
sudo -u pmwani bash -c "cd $APP_DIR && set -a && . $ENV_FILE && set +a && \
  ./venv/bin/python -c 'from app.main import init_db; init_db()'"

echo "==> [7/7] Services"
cat > /etc/systemd/system/pmwani-api.service <<EOF
[Unit]
Description=PM-WANI backend API
After=network.target postgresql.service

[Service]
User=pmwani
Group=pmwani
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always

[Install]
WantedBy=multi-user.target
EOF
systemctl disable --now pmwani-portal 2>/dev/null || true      # old Flask POC portal

mkdir -p /var/www/pmwani-files
cat > /etc/nginx/sites-available/pmwani-api <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    client_max_body_size 2m;

    location /files/ {
        alias /var/www/pmwani-files/;
        default_type text/plain;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/pmwani-portal
ln -sf /etc/nginx/sites-available/pmwani-api /etc/nginx/sites-enabled/pmwani-api
systemctl daemon-reload
systemctl enable pmwani-api
systemctl restart pmwani-api
nginx -t && systemctl reload nginx
sleep 3
if curl -fsS http://127.0.0.1/healthz | grep -q ok; then echo "    Backend is up"; else
  echo "!! Backend not responding: sudo journalctl -u pmwani-api -n 50"; fi

cat <<EOF

=====================================================================
 Done.
 API docs (open in browser):  ${PUBLIC_BASE_URL}/docs
 API base URL for the app:    ${PUBLIC_BASE_URL}/api/v1
 Captive portal:              ${PUBLIC_BASE_URL}/chilli
 PDOA admin numbers:          ${PDOA_PHONES:-<none - set PDOA_PHONES in $ENV_FILE>}
 UAM secret (same as on AP):  ${UAMSECRET}
 Settings file:               ${ENV_FILE}
 Logs:                        sudo journalctl -u pmwani-api -f

 On the AP, make CoovaChilli report its NAS id as Called-Station-Id:
   uci set chilli.@chilli[0].radiuscalled='pmwani-ap1'; uci commit chilli; /etc/init.d/chilli restart
=====================================================================
EOF
