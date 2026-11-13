#!/bin/bash
# EC2 startup script (user data) for one lookup server. Commit 5.
#
# Do not run this by hand on your laptop: 05_servers.sh fills in the
# __PLACEHOLDERS__ (the app itself is embedded as a compressed archive, so the
# server needs no access to GitHub, even for a private repo) and passes the
# result to EC2, which runs it once as root on first boot.
#
# What it does on Amazon Linux 2023:
#   1. installs Python's pip
#   2. unpacks the lookup app into /opt/cowin-lookup
#   3. creates a virtualenv and installs FastAPI + uvicorn
#   4. runs the app on port 8000 as an unprivileged user, under systemd,
#      restarting it if it crashes and starting it again after a reboot
#
# Boot log: /var/log/cowin-user-data.log on the server, or from your machine:
#   ./infra/aws/05_servers.sh logs a
set -euo pipefail
exec > >(tee -a "${LOG_FILE:-/var/log/cowin-user-data.log}") 2>&1
echo "=== cowin user-data start $(date -u +%FT%TZ) ==="

APP_DIR="${APP_DIR:-/opt/cowin-lookup}"
APP_USER="${APP_USER:-cowin}"
APP_PORT="__APP_PORT__"
APP_VERSION="__APP_VERSION__"

# 1. Packages (dnf exists on Amazon Linux; skipped elsewhere)
if command -v dnf >/dev/null; then
  dnf install -y python3 python3-pip
fi

# 2. Unpack the app (lookup/app + lookup/requirements.txt, embedded below)
mkdir -p "$APP_DIR"
base64 -d > /tmp/cowin-lookup.tar.gz <<'APP_TARBALL'
__APP_TARBALL_B64__
APP_TARBALL
tar -xzf /tmp/cowin-lookup.tar.gz -C "$APP_DIR"
rm -f /tmp/cowin-lookup.tar.gz
echo "$APP_VERSION" > "$APP_DIR/VERSION"

# 3. Virtualenv with the app's dependencies
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

id "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin "$APP_USER"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

START_CMD="$APP_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $APP_PORT --workers 2 --no-access-log"

# 4. Run it under systemd (fallback: plain background process, used only when
#    this script is tested outside EC2 on a machine without systemd)
if [[ -d /run/systemd/system ]]; then
  cat > /etc/systemd/system/cowin-lookup.service <<EOF
[Unit]
Description=CoWIN IT457 vaccine center lookup service
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$START_CMD
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now cowin-lookup.service
else
  echo "no systemd: starting in the background (test mode)"
  cd "$APP_DIR" && nohup $START_CMD > "$APP_DIR/app.log" 2>&1 &
fi

# Wait until the app answers, so the boot log says clearly whether it worked.
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null 2>&1; then
    echo "=== cowin-lookup is UP on port $APP_PORT (version $APP_VERSION) ==="
    curl -s "http://127.0.0.1:$APP_PORT/health"; echo
    exit 0
  fi
  sleep 2
done
echo "=== cowin-lookup did NOT start; see: journalctl -u cowin-lookup ==="
exit 1
