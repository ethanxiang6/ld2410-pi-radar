#!/bin/bash
# Creates a USB CDC-NCM network gadget on the Pi's USB-C / OTG port via configfs.
# Windows 10/11, macOS and Linux all have NCM drivers built in, so the PC sets it up
# automatically (the old g_ether/RNDIS gadget shows up on Windows as a COM port).
# The Pi side appears as interface usb0. Run at boot by radar-usb-gadget.service.
set -e
G=/sys/kernel/config/usb_gadget/radar

modprobe libcomposite
[ -d /sys/kernel/config/usb_gadget ] || mount -t configfs none /sys/kernel/config
[ -e "$G/UDC" ] && [ -n "$(cat "$G/UDC")" ] && { echo "gadget already bound"; exit 0; }

# Stable serial + MACs derived from the Pi's serial number, so the PC sees the same device every boot.
SERIAL=$(tr -d '\0' < /sys/firmware/devicetree/base/serial-number 2>/dev/null || cat /etc/machine-id)
H=$(echo -n "$SERIAL" | sha256sum)
HOST_MAC="02:${H:0:2}:${H:2:2}:${H:4:2}:${H:6:2}:${H:8:2}"
DEV_MAC="02:${H:10:2}:${H:12:2}:${H:14:2}:${H:16:2}:${H:18:2}"

mkdir -p "$G" && cd "$G"
echo 0x1d6b > idVendor    # Linux Foundation
echo 0x0104 > idProduct   # Multifunction Composite Gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB
echo 0xEF > bDeviceClass  # misc / interface association, so Windows matches the NCM function
echo 0x02 > bDeviceSubClass
echo 0x01 > bDeviceProtocol

mkdir -p strings/0x409
echo "$SERIAL" > strings/0x409/serialnumber
echo "Raspberry Pi" > strings/0x409/manufacturer
echo "LD2410C Radar" > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "NCM network" > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower

mkdir -p functions/ncm.usb0
echo "$HOST_MAC" > functions/ncm.usb0/host_addr
echo "$DEV_MAC" > functions/ncm.usb0/dev_addr
[ -e configs/c.1/ncm.usb0 ] || ln -s functions/ncm.usb0 configs/c.1/

UDC=$(ls /sys/class/udc | head -n1)
[ -n "$UDC" ] || { echo "no USB device controller - is dtoverlay=dwc2,dr_mode=peripheral in config.txt?"; exit 1; }
echo "$UDC" > UDC
echo "NCM gadget bound to $UDC (Pi $DEV_MAC, PC $HOST_MAC)"
