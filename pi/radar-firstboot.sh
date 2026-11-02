#!/bin/bash
# One-shot setup run straight from the SD card's boot partition, for when you can't SSH in yet.
# Copy this file and the ld2410-radar/ folder onto the boot partition, then append to cmdline.txt:
#   systemd.run=/boot/firmware/radar-firstboot.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target
# (and add dtoverlay=dwc2,dr_mode=peripheral to config.txt).
# It installs the radar service and the usb0 network, removes itself from cmdline.txt, and reboots.
set +e
BOOT=/boot/firmware
LOG=$BOOT/radar-firstboot.log
exec >"$LOG" 2>&1
echo "radar first boot: $(date)"

# Strip ourselves from cmdline.txt first, so a failure below can never trap the Pi in this mode.
sed -i 's| systemd.run.*||' "$BOOT/cmdline.txt"

# --- radar server ---
install -d /opt/ld2410-radar/web
install -m 644 "$BOOT/ld2410-radar/ld2410.py" "$BOOT/ld2410-radar/radar_server.py" /opt/ld2410-radar/
install -m 644 "$BOOT/ld2410-radar/web/index.html" /opt/ld2410-radar/web/
cat > /etc/systemd/system/ld2410-radar.service <<'EOF'
[Unit]
Description=LD2410C radar web server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/ld2410-radar/radar_server.py --serial /dev/serial0 --baud 256000 --port 8080
Restart=always
RestartSec=2
DynamicUser=yes
SupplementaryGroups=dialout

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/ld2410-radar.service /etc/systemd/system/multi-user.target.wants/ld2410-radar.service
python3 -c "import serial" || echo "WARNING: python3-serial missing - run: sudo apt install python3-serial"

# --- USB network (usb0) ---
install -m 755 "$BOOT/ld2410-radar/usb-gadget-ncm.sh" /usr/local/sbin/radar-usb-gadget
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
ln -sf /etc/systemd/system/radar-usb-gadget.service /etc/systemd/system/multi-user.target.wants/radar-usb-gadget.service
rm -f /etc/modprobe.d/g_ether.conf   # left over from the older g_ether setup

if [ -x /usr/sbin/dnsmasq ]; then
  # Pi = 10.42.0.1 and hands the PC an address. No gateway/DNS, so the PC keeps using its own internet.
  IPV4="method=shared
address1=10.42.0.1/24"
  install -d /etc/NetworkManager/dnsmasq-shared.d
  printf 'dhcp-option=3\ndhcp-option=6\n' > /etc/NetworkManager/dnsmasq-shared.d/usb-gadget.conf
else
  echo "dnsmasq missing - usb0 falls back to link-local; reach the Pi as <hostname>.local"
  IPV4="method=link-local"
fi
cat > /etc/NetworkManager/system-connections/usb-gadget.nmconnection <<EOF
[connection]
id=usb-gadget
uuid=$(cat /proc/sys/kernel/random/uuid)
type=ethernet
interface-name=usb0
autoconnect=true

[ethernet]

[ipv4]
$IPV4

[ipv6]
method=link-local
EOF
chmod 600 /etc/NetworkManager/system-connections/usb-gadget.nmconnection

echo "done"
sync
exit 0
