#!/usr/bin/env bash
# SwyfTech Client Portal: installs inside a fresh Debian 12 LXC.
# Called by ct/swyftech-portal.sh, but can also be run by hand on any Debian 12 box:
#   STAFF_EMAIL=you@example.com bash swyftech-portal-install.sh

set -Eeuo pipefail

REPO="${REPO:-wolfej4/swyftech-portal}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-8000}"
STAFF_EMAIL="${STAFF_EMAIL:-}"
STAFF_NAME="${STAFF_NAME:-}"
BASE_URL="${BASE_URL:-}"
APP_DIR=/opt/swyftech-portal
SVC_USER=swyftech

YW=$'\033[33m'; GN=$'\033[1;92m'; RD=$'\033[01;31m'; CL=$'\033[m'
msg_info()  { printf " %s⏳ %s...%s" "$YW" "$1" "$CL"; }
msg_ok()    { printf "\r\033[K %s✔️ %s%s\n" "$GN" "$1" "$CL"; }
msg_error() { printf "\r\033[K %s✖️ %s%s\n" "$RD" "$1" "$CL"; }
trap 'msg_error "Install failed on line $LINENO"' ERR

export DEBIAN_FRONTEND=noninteractive
# The Proxmox shell passes its own language setting (often en_US.UTF-8) into the container, but the
# Debian template doesn't include that locale yet. C.UTF-8 is always built in, so use it until
# en_US.UTF-8 is generated below. This keeps apt and perl from printing locale warnings.
export LANG=C.UTF-8 LC_ALL=C.UTF-8
unset LANGUAGE

msg_info "Setting up the system language"
apt-get update -qq
apt-get install -y -qq locales >/dev/null
sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
locale-gen >/dev/null
update-locale LANG=en_US.UTF-8
msg_ok "System language set to en_US.UTF-8"

msg_info "Updating the OS"
apt-get update -qq
apt-get -y -qq -o Dpkg::Options::="--force-confold" upgrade >/dev/null
msg_ok "OS updated"

msg_info "Installing dependencies"
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates sqlite3 tzdata cron >/dev/null
msg_ok "Dependencies installed"

msg_info "Creating the service account"
id "$SVC_USER" &>/dev/null || useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
msg_ok "Service account ready"

msg_info "Downloading ${REPO}"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --quiet --ff-only
else
  git clone --quiet --depth 1 --branch "$BRANCH" "https://github.com/${REPO}.git" "$APP_DIR"
fi
msg_ok "Code downloaded to ${APP_DIR}"

msg_info "Installing Python packages"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
msg_ok "Python packages installed"

msg_info "Writing settings"
IP=$(hostname -I | awk '{print $1}')
[[ -n "$BASE_URL" ]] || BASE_URL="http://${IP}:${PORT}"
mkdir -p "$APP_DIR/data/uploads" "$APP_DIR/data/backups"
# Written here rather than copied from example.env, so the install works even if that
# file didn't make it into the repo (GitHub's web upload skips files starting with a dot).
# An empty .env (left by an earlier failed run) counts as missing.
if [[ ! -s "$APP_DIR/.env" ]]; then
  SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
  cat >"$APP_DIR/.env" <<ENVEOF
# SwyfTech client portal settings. After editing: systemctl restart swyftech-portal
# Every option is explained in example.env and the README.

# Public address clients use. Must be https in production (Stripe and secure cookies need it).
BASE_URL=${BASE_URL}
SECRET_KEY=${SECRET}
TIMEZONE=America/Chicago

BUSINESS_NAME=SwyfTech LLC
SUPPORT_EMAIL=${STAFF_EMAIL}
SUPPORT_PHONE=
STAFF_NOTIFY_EMAIL=${STAFF_EMAIL}
TAGLINE=IT support for businesses across Okaloosa, Santa Rosa, Walton and Escambia counties.

INVOICE_PREFIX=SWY
DEFAULT_NET_DAYS=15

# Email (Microsoft 365: smtp.office365.com, 587, starttls)
SMTP_HOST=
SMTP_PORT=587
SMTP_TLS=starttls
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=

# Stripe
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=

# Snipe-IT (optional)
SNIPEIT_URL=
SNIPEIT_API_TOKEN=
SNIPEIT_VERIFY_TLS=true

# S3-compatible storage (optional; blank keeps files on this container's disk)
S3_ENDPOINT_URL=
S3_BUCKET=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_REGION=us-east-1
S3_PREFIX=swyftech-portal/

MAX_UPLOAD_MB=25
MAX_FILES_PER_MESSAGE=5
ENVEOF
fi
chown root:"$SVC_USER" "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR/data"
chmod 750 "$APP_DIR/data"
msg_ok "Settings written to ${APP_DIR}/.env"

msg_info "Creating the service"
cat >/etc/systemd/system/swyftech-portal.service <<EOF
[Unit]
Description=SwyfTech client portal
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 2
Restart=on-failure
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --quiet --now swyftech-portal
msg_ok "Service running on port ${PORT}"

msg_info "Adding helper commands"
cat >/usr/local/bin/portal-cli <<EOF
#!/usr/bin/env bash
# SwyfTech portal admin commands. Try: portal-cli --help
cd ${APP_DIR} && exec runuser -u ${SVC_USER} -- ${APP_DIR}/.venv/bin/python -m app.cli "\$@"
EOF
chmod 755 /usr/local/bin/portal-cli

cat >/usr/bin/update <<EOF
#!/usr/bin/env bash
# Update the SwyfTech portal to the latest version from GitHub.
REPO=${REPO} BRANCH=${BRANCH} bash -c "\$(curl -fsSL https://raw.githubusercontent.com/${REPO}/${BRANCH}/ct/swyftech-portal.sh)"
EOF
chmod 755 /usr/bin/update

cat >/etc/cron.daily/swyftech-portal-backup <<EOF
#!/usr/bin/env bash
# Nightly copy of the portal database. Keeps 14 days. Uploaded files live in ${APP_DIR}/data/uploads.
set -e
dest=${APP_DIR}/data/backups/portal-\$(date +%F).db
runuser -u ${SVC_USER} -- sqlite3 ${APP_DIR}/data/portal.db ".backup '\$dest'"
find ${APP_DIR}/data/backups -name 'portal-*.db' -mtime +14 -delete
EOF
chmod 755 /etc/cron.daily/swyftech-portal-backup

printf '%s\n' '# Monthly client reports: on the 1st at 13:00 UTC (about 8 AM Central), send them or email you to review.' 'SHELL=/bin/bash' 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' '0 13 1 * * root /usr/local/bin/portal-cli monthly-reports >> /var/log/swyftech-portal-reports.log 2>&1' >/etc/cron.d/swyftech-portal-reports
chmod 644 /etc/cron.d/swyftech-portal-reports
msg_ok "Added portal-cli, update, nightly database backups, and monthly reports"

if [[ -n "$STAFF_EMAIL" ]]; then
  msg_info "Creating your staff login"
  sleep 2  # let the service create the database
  if OUT=$(/usr/local/bin/portal-cli create-staff --email "$STAFF_EMAIL" --name "$STAFF_NAME" 2>&1); then
    {
      echo "SwyfTech Client Portal"
      echo "URL: ${BASE_URL}"
      echo "$OUT"
    } >/root/swyftech-portal.creds
    chmod 600 /root/swyftech-portal.creds
    msg_ok "Staff login created"
    echo
    echo "$OUT" | sed 's/^/   /'
    echo
  else
    msg_error "Couldn't create the staff login: ${OUT}"
    echo "   Run later: portal-cli create-staff --email you@example.com --name \"Your Name\""
  fi
fi

msg_info "Cleaning up"
apt-get -y -qq autoremove >/dev/null
apt-get -y -qq autoclean >/dev/null
rm -f /root/swyftech-portal-install.sh
msg_ok "Cleaned up"
