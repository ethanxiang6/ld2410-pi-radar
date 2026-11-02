#!/usr/bin/env bash
# Turns the Pi's USB-C / OTG data port into a USB network adapter ("USB tethering"):
# plug it into the PC and the PC gets an IP from the Pi; the Pi is always 10.42.0.1.
# Works on Pi Zero / Zero 2 W / 4 / 5 (Pi 3 and earlier have no OTG port).
#   sudo bash setup-usb-gadget.sh && sudo reboot
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo: sudo bash setup-usb-gadget.sh"; exit 1; }

BOOT=/boot/firmware
[ -f "$BOOT/config.txt" ] || BOOT=/boot
CFG="$BOOT/config.txt"
CMDLINE="$BOOT/cmdline.txt"

cp -n "$CFG" "$CFG.bak-radar"
cp -n "$CMDLINE" "$CMDLINE.bak-radar"

echo "==> Enabling the dwc2 USB controller in peripheral mode ($CFG)"
if ! grep -q '^dtoverlay=dwc2' "$CFG"; then
  printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CFG"
fi

# The legacy g_ether (RNDIS) gadget binds as a COM port on Windows; NCM gets a driver automatically.
sed -i 's/modules-load=dwc2,g_ether/modules-load=dwc2/' "$CMDLINE"
rm -f /etc/modprobe.d/g_ether.conf

echo "==> Installing the NCM network gadget service"
install -m 755 "$(dirname "$0")/usb-gadget-ncm.sh" /usr/local/sbin/radar-usb-gadget
cat > /etc/systemd/system/radar-usb-gadget.service <<'EOF'
[Unit]
Description=USB NCM network gadget (PC link for the radar)
After=sys-kernel-config.mount systemd-modules-load.service NetworkManager.service
# bind only once NetworkManager is up, so the PC's first DHCP request is answered
RequiresMountsFor=/boot/firmware

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/radar-usb-gadget
# readable from a PC by putting the SD card in, if the USB link never comes up
StandardOutput=file:/boot/firmware/radar-gadget.log
StandardError=inherit

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable radar-usb-gadget.service

if command -v nmcli >/dev/null; then
  echo "==> Configuring usb0: Pi = 10.42.0.1, DHCP for the PC"
  command -v dnsmasq >/dev/null || apt-get install -y -q dnsmasq-base
  # No gateway/DNS in the DHCP offer, so the PC keeps using its own internet connection.
  install -d /etc/NetworkManager/dnsmasq-shared.d
  printf 'dhcp-option=3\ndhcp-option=6\n' > /etc/NetworkManager/dnsmasq-shared.d/usb-gadget.conf
  nmcli connection delete usb-gadget >/dev/null 2>&1 || true
  nmcli connection add type ethernet ifname usb0 con-name usb-gadget autoconnect yes \
    ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method link-local >/dev/null
else
  echo "!! NetworkManager not found (older OS). Give usb0 a static IP yourself, e.g. in /etc/dhcpcd.conf:"
  echo "     interface usb0"
  echo "     static ip_address=10.42.0.1/24"
fi

echo
echo "Done. Reboot, then connect the Pi's USB-C/OTG *data* port to your PC."
echo "The radar will be at http://10.42.0.1:8080"
