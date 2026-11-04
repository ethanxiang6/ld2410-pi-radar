#!/usr/bin/env bash
# Shows the radar full-screen on a monitor plugged into the Pi (Chromium kiosk).
# Needs Raspberry Pi OS with the desktop (labwc, auto-login). Run as your normal user, not sudo:
#     bash setup-kiosk.sh            # install
#     bash setup-kiosk.sh --remove   # undo
set -euo pipefail

PORT=${HTTP_PORT:-8080}
FILE="$HOME/.config/labwc/autostart"
MARK="# ld2410-radar kiosk"

if [ "$(id -u)" -eq 0 ]; then echo "Run as your normal user, not root." >&2; exit 1; fi

if [ "${1:-}" = "--remove" ]; then
  [ -f "$FILE" ] && sed -i "/$MARK/,/^) &\$/d" "$FILE"
  echo "Kiosk removed. Takes effect at next login."
  exit 0
fi

command -v chromium >/dev/null || command -v chromium-browser >/dev/null \
  || { echo "Chromium not found: sudo apt install chromium" >&2; exit 1; }
BROWSER=$(command -v chromium || command -v chromium-browser)

mkdir -p "$(dirname "$FILE")"
touch "$FILE"
if grep -q "$MARK" "$FILE"; then echo "Kiosk already installed in $FILE"; exit 0; fi
cat >> "$FILE" <<EOT
$MARK: wait for the local radar server, then open it full-screen
# 4K at 30 Hz is too much for a Pi 4's browser; 1080p is smooth (skips if the monitor has no such mode)
wlr-randr --output \$(wlr-randr | awk 'NR==1{print \$1}') --mode 1920x1080@60 2>/dev/null
(
  until curl -sf http://localhost:$PORT/api/state >/dev/null; do sleep 1; done
  exec $BROWSER --kiosk --noerrdialogs --disable-infobars --no-first-run \\
    --password-store=basic --ozone-platform=wayland http://localhost:$PORT
) &
EOT
chmod +x "$FILE"
echo "Kiosk installed. Reboot (or log out and in) to start it. Alt+F4 closes it."
