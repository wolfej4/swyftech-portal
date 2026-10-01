#!/usr/bin/env bash
# SwyfTech Client Portal: Proxmox VE LXC installer
#
# Run on the Proxmox host shell:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/wolfej4/swyftech-portal/main/ct/swyftech-portal.sh)"
#
# Run the same command (or just `update`) inside the container's console to update.
#
# Modeled on community-scripts/ProxmoxVE, but self-contained: their build.func fetches
# install scripts from their own repository, so a custom app needs its own launcher.

set -Eeuo pipefail

APP="SwyfTech Portal"
NSAPP="swyftech-portal"
REPO="${REPO:-wolfej4/swyftech-portal}"
BRANCH="${BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"

var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-1024}"
var_disk="${var_disk:-4}"
var_hostname="${var_hostname:-swyftech-portal}"
var_tags="${var_tags:-msp;portal}"
var_bridge="${var_bridge:-vmbr0}"
var_port="${var_port:-8000}"

# ---- look and feel ----------------------------------------------------------
YW=$'\033[33m'; GN=$'\033[1;92m'; RD=$'\033[01;31m'; BL=$'\033[36m'; DGN=$'\033[32m'; CL=$'\033[m'; BOLD=$'\033[1m'
CM="${GN}✔️${CL}"; CROSS="${RD}✖️${CL}"; INFO="${BL}💡${CL}"

msg_info()  { printf " %s⏳ %s...%s" "$YW" "$1" "$CL"; }
msg_ok()    { printf "\r\033[K %s %s%s%s\n" "$CM" "$GN" "$1" "$CL"; }
msg_error() { printf "\r\033[K %s %s%s%s\n" "$CROSS" "$RD" "$1" "$CL"; }

header_info() {
  clear
  cat <<'EOF'
   ____               __ _____         _
  / ___|_      ___   _/ _|_   _|__  ___| |__
  \___ \ \ /\ / / | | | |_  | |/ _ \/ __| '_ \
   ___) \ V  V /| |_| |  _| | |  __/ (__| | | |
  |____/ \_/\_/  \__, |_|   |_|\___|\___|_| |_|
                 |___/      Client Portal
EOF
  echo
}

on_error() {
  local code=$? line=$1
  msg_error "Failed on line ${line} (exit ${code}). Nothing after this step ran."
  if [[ -n "${CTID:-}" ]] && pct status "$CTID" &>/dev/null; then
    echo -e " ${INFO} The container ${CTID} was left in place so you can inspect it. Remove it with: ${BOLD}pct destroy ${CTID} --purge${CL}"
  fi
  exit "$code"
}
trap 'on_error $LINENO' ERR
cancelled() { echo -e "\n ${YW}Cancelled. Nothing was created.${CL}"; exit 0; }

# ---- update mode (run inside the container) ---------------------------------
update_script() {
  header_info
  local dir=/opt/swyftech-portal
  [[ -d "$dir/.git" ]] || { msg_error "No ${APP} installation found in ${dir}"; exit 1; }
  msg_info "Backing up the database"
  local stamp; stamp=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$dir/data/backups"
  sqlite3 "$dir/data/portal.db" ".backup '$dir/data/backups/pre-update-${stamp}.db'"
  chown -R swyftech:swyftech "$dir/data/backups"
  msg_ok "Backed up to data/backups/pre-update-${stamp}.db"

  msg_info "Downloading the latest version"
  git -C "$dir" fetch --quiet origin "$BRANCH"
  local before after
  before=$(git -C "$dir" rev-parse --short HEAD)
  git -C "$dir" reset --quiet --hard "origin/${BRANCH}"
  after=$(git -C "$dir" rev-parse --short HEAD)
  msg_ok "Code at ${after} (was ${before})"

  msg_info "Updating Python packages"
  "$dir/.venv/bin/pip" install --quiet --upgrade -r "$dir/requirements.txt"
  msg_ok "Packages updated"

  if [[ ! -f /etc/cron.d/swyftech-portal-reports ]]; then
    printf '%s\n' '# Monthly client reports: on the 1st at 13:00 UTC (about 8 AM Central), send them or email you to review.' 'SHELL=/bin/bash' 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' '0 13 1 * * root /usr/local/bin/portal-cli monthly-reports >> /var/log/swyftech-portal-reports.log 2>&1' >/etc/cron.d/swyftech-portal-reports
    chmod 644 /etc/cron.d/swyftech-portal-reports
    msg_ok "Added the monthly report schedule"
  fi

  msg_info "Restarting the portal"
  systemctl restart swyftech-portal
  sleep 2
  systemctl is-active --quiet swyftech-portal || { msg_error "Portal didn't start. Check: journalctl -u swyftech-portal -n 50"; exit 1; }
  msg_ok "Updated successfully"
  exit 0
}

if ! command -v pveversion &>/dev/null; then
  if [[ -d /opt/swyftech-portal ]]; then
    update_script
  fi
  echo "Run this on a Proxmox VE host (or inside an existing ${APP} container to update)."
  exit 1
fi

# ---- preflight on the Proxmox host -----------------------------------------
header_info
[[ $EUID -eq 0 ]] || { msg_error "Run this as root on the Proxmox host"; exit 1; }
command -v whiptail &>/dev/null || { msg_error "whiptail is missing (apt install whiptail)"; exit 1; }

whiptail --backtitle "Proxmox VE Helper" --title "${APP} LXC" --yesno \
  "This will create a new ${APP} container.\n\nProceed?" 10 58 || exit 0

# ---- pick settings ----------------------------------------------------------
CTID=$(pvesh get /cluster/nextid)
HN="$var_hostname"; CORES="$var_cpu"; RAM="$var_ram"; DISK="$var_disk"; BRIDGE="$var_bridge"
NET="dhcp"; GATE=""; TAGS="$var_tags"

pick_storage() {  # $1 = content type, $2 = label
  local content=$1 label=$2 list=() count=0 name type free
  while read -r name type _ _ _ free _; do
    list+=("$name" "${type}, $((free / 1024 / 1024)) GB free" "OFF"); count=$((count + 1))
  done < <(pvesm status -content "$content" | awk 'NR>1 && $3 == "active"')
  if [[ $count -eq 0 ]]; then msg_error "No active storage allows ${label}" >&2; return 1; fi
  if [[ $count -eq 1 ]]; then echo "${list[0]}"; return; fi
  list[2]="ON"
  whiptail --backtitle "Proxmox VE Helper" --title "Storage" --radiolist \
    "Where should the ${label} go?" 16 64 6 "${list[@]}" 3>&1 1>&2 2>&3 || return 1
}

if whiptail --backtitle "Proxmox VE Helper" --title "Settings" --yes-button "Default" --no-button "Advanced" --yesno \
  "Use default settings?\n\n  Container ID: ${CTID}\n  Hostname:     ${HN}\n  CPU / RAM:    ${CORES} core, ${RAM} MB\n  Disk:         ${DISK} GB\n  Network:      ${BRIDGE}, DHCP\n  OS:           Debian 12, unprivileged" 16 58; then
  :
else
  CTID=$(whiptail --title "Container ID" --inputbox "Container ID" 8 58 "$CTID" 3>&1 1>&2 2>&3) || cancelled
  HN=$(whiptail --title "Hostname" --inputbox "Hostname" 8 58 "$HN" 3>&1 1>&2 2>&3) || cancelled
  CORES=$(whiptail --title "CPU" --inputbox "CPU cores" 8 58 "$CORES" 3>&1 1>&2 2>&3) || cancelled
  RAM=$(whiptail --title "Memory" --inputbox "RAM in MB" 8 58 "$RAM" 3>&1 1>&2 2>&3) || cancelled
  DISK=$(whiptail --title "Disk" --inputbox "Disk size in GB" 8 58 "$DISK" 3>&1 1>&2 2>&3) || cancelled
  BRIDGE=$(whiptail --title "Network" --inputbox "Bridge" 8 58 "$BRIDGE" 3>&1 1>&2 2>&3) || cancelled
  NET=$(whiptail --title "IP address" --inputbox "Static IP with CIDR (e.g. 10.0.10.50/24), or dhcp" 8 64 "dhcp" 3>&1 1>&2 2>&3) || cancelled
  if [[ "$NET" != "dhcp" ]]; then
    GATE=$(whiptail --title "Gateway" --inputbox "Gateway IP" 8 58 "" 3>&1 1>&2 2>&3) || cancelled
  fi
fi

if pct status "$CTID" &>/dev/null || qm status "$CTID" &>/dev/null; then
  msg_error "ID ${CTID} is already in use"; exit 1
fi

STAFF_EMAIL=$(whiptail --title "Your staff login" --inputbox \
  "Email for your SwyfTech staff account.\nYou'll sign in with this and set up your authenticator app." 10 64 "" 3>&1 1>&2 2>&3) || cancelled
STAFF_NAME=$(whiptail --title "Your name" --inputbox "Your name (shown to clients on replies)" 8 58 "Jacob" 3>&1 1>&2 2>&3) || cancelled
PUBLIC_URL=$(whiptail --title "Public address" --inputbox \
  "The https address clients will use, e.g. https://portal.swyftech.net\n\nLeave blank to use http://<container-ip>:${var_port} for now.\nYou can change BASE_URL in .env later." 12 70 "" 3>&1 1>&2 2>&3) || cancelled

ROOTFS_STORAGE=$(pick_storage rootdir "container disk") || cancelled
TEMPLATE_STORAGE=$(pick_storage vztmpl "OS template") || cancelled

echo -e " ${DGN}Container ID:${CL} ${CTID}   ${DGN}Hostname:${CL} ${HN}   ${DGN}Storage:${CL} ${ROOTFS_STORAGE}"

# ---- template ----------------------------------------------------------------
msg_info "Updating the template list"
pveam update >/dev/null
TEMPLATE=$(pveam available -section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -n1)
[[ -n "$TEMPLATE" ]] || { msg_error "No Debian 12 template found"; exit 1; }
msg_ok "Using ${TEMPLATE}"

if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -qF "$TEMPLATE"; then
  msg_info "Downloading the Debian 12 template"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
  msg_ok "Template downloaded"
fi

# ---- create & start ------------------------------------------------------------
msg_info "Creating LXC container ${CTID}"
NET0="name=eth0,bridge=${BRIDGE},ip=${NET}"
[[ -n "$GATE" ]] && NET0+=",gw=${GATE}"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$HN" \
  --cores "$CORES" --memory "$RAM" --swap 512 \
  --rootfs "${ROOTFS_STORAGE}:${DISK}" \
  --net0 "$NET0" \
  --ostype debian --unprivileged 1 --features nesting=1 \
  --onboot 1 --tags "$TAGS" \
  --description "<div align='center'><h2>SwyfTech Client Portal</h2><p>Update: run <code>update</code> in the console.<br>Admin: <code>portal-cli --help</code></p></div>" \
  >/dev/null
msg_ok "Created LXC container ${CTID}"

msg_info "Starting the container"
pct start "$CTID"
for _ in $(seq 1 30); do
  if pct exec "$CTID" -- getent hosts deb.debian.org &>/dev/null; then break; fi
  sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org &>/dev/null || { msg_error "The container has no internet access. Check the bridge, VLAN and DHCP."; exit 1; }
msg_ok "Container is online"

# ---- install inside --------------------------------------------------------------
msg_info "Fetching the installer from ${REPO}"
INSTALLER=$(mktemp)
curl -fsSL "${RAW}/install/${NSAPP}-install.sh" -o "$INSTALLER"
pct push "$CTID" "$INSTALLER" /root/${NSAPP}-install.sh --perms 0700
rm -f "$INSTALLER"
msg_ok "Installer ready"

pct exec "$CTID" -- env LANG=C.UTF-8 LC_ALL=C.UTF-8 REPO="$REPO" BRANCH="$BRANCH" PORT="$var_port" \
  STAFF_EMAIL="$STAFF_EMAIL" STAFF_NAME="$STAFF_NAME" BASE_URL="$PUBLIC_URL" \
  bash /root/${NSAPP}-install.sh

IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
echo
msg_ok "Completed successfully!"
echo -e " ${INFO} ${YW}${APP} is running at:${CL} ${BOLD}http://${IP}:${var_port}${CL}"
echo -e " ${INFO} ${YW}Your staff login is saved in the container at${CL} /root/swyftech-portal.creds"
echo -e " ${INFO} ${YW}Next: put it behind HTTPS (Nginx Proxy Manager, Caddy or a Cloudflare Tunnel), then set BASE_URL in /opt/swyftech-portal/.env${CL}"
