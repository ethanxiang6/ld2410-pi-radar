#!/usr/bin/env bash
# Installs the LD2410C radar server as a systemd service and enables the GPIO UART.
#   sudo bash install.sh
#   sudo SERIAL=/dev/ttyUSB0 bash install.sh      # sensor on a USB-TTL adapter instead
set -euo pipefail

SERIAL=${SERIAL:-/dev/serial0}
BAUD=${BAUD:-256000}
HTTP_PORT=${HTTP_PORT:-8080}
DEST=/opt/ld2410-radar
RUN_USER=${SUDO_USER:-pi}
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo: sudo bash install.sh"; exit 1; }

if ! python3 -c "import serial" 2>/dev/null; then
  echo "==> Installing pyserial"
  apt-get update -q && apt-get install -y -q python3-serial
fi

if [[ "$SERIAL" == /dev/serial0 || "$SERIAL" == /dev/ttyAMA* || "$SERIAL" == /dev/ttyS0 ]] && command -v raspi-config >/dev/null; then
  echo "==> Enabling GPIO UART and removing the login console from it"
  if raspi-config nonint do_serial_hw 0 2>/dev/null; then
    raspi-config nonint do_serial_cons 1
  else
    raspi-config nonint do_serial 2   # older raspi-config: hw on, console off
  fi
fi

echo "==> Copying files to $DEST (owned by $RUN_USER, so later updates need no sudo)"
install -d -o "$RUN_USER" -g "$RUN_USER" "$DEST" "$DEST/web"
install -m 644 -o "$RUN_USER" -g "$RUN_USER" "$SRC/ld2410.py" "$SRC/radar_server.py" "$DEST/"
install -m 644 -o "$RUN_USER" -g "$RUN_USER" "$SRC/web/index.html" "$DEST/web/"

# Restart the server whenever its Python files change (index.html is re-read on every request).
cat > /etc/systemd/system/ld2410-radar-reload.path <<EOF
[Unit]
Description=Restart the radar server when its code changes

[Path]
PathChanged=$DEST/radar_server.py
PathChanged=$DEST/ld2410.py

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/ld2410-radar-reload.service <<'EOF'
[Unit]
Description=Restart the radar server after a code update

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 1
ExecStart=/usr/bin/systemctl restart ld2410-radar.service
EOF

echo "==> Creating systemd service (runs as $RUN_USER)"
cat > /etc/systemd/system/ld2410-radar.service <<EOF
[Unit]
Description=LD2410C radar web server
After=network.target

[Service]
ExecStart=/usr/bin/python3 $DEST/radar_server.py --serial $SERIAL --baud $BAUD --port $HTTP_PORT
Restart=always
RestartSec=2
User=$RUN_USER
SupplementaryGroups=dialout

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ld2410-radar.service ld2410-radar-reload.path
systemctl restart ld2410-radar.service ld2410-radar-reload.path

echo
echo "Done. Radar UI: http://$(hostname).local:$HTTP_PORT  (or http://10.42.0.1:$HTTP_PORT over USB)"
echo "If the UART was just enabled, reboot once:  sudo reboot"
echo "Logs:  journalctl -u ld2410-radar -f"
