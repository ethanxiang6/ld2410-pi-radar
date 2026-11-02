#!/usr/bin/env python3
"""Opens the LD2410C radar page as soon as the Pi is reachable. Works on Windows and Linux.

    python start_radar.py              # wait for the Pi, open the radar in your browser, exit
    python start_radar.py --watch      # keep running: open the radar every time the Pi is plugged in
    python start_radar.py --install    # start "--watch" automatically when you log in
    python start_radar.py --uninstall  # remove that again

Only uses the standard library. If the PC asked the Pi for an address while the Pi was still
booting, Windows gives up and sits on a 169.254.x.x address for minutes; this script asks again.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HOSTS = ["10.42.0.1", "humansensor.local", "raspberrypi.local"]
PORT = 8080
USB_SUBNET = "10.42.0."
APP_NAME = "ld2410-radar"
IS_WINDOWS = platform.system() == "Windows"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# --- making sure the PC has an address on the Pi's USB link ---

def usb_links_windows():
    """[(adapter name, [ipv4...])] for the Pi's USB network adapters."""
    ps = ("Get-NetAdapter | Where-Object { $_.InterfaceDescription -match 'UsbNcm|RNDIS' -and $_.Status -eq 'Up' } | "
          "ForEach-Object { [pscustomobject]@{ name = $_.Name; ips = @(Get-NetIPAddress -InterfaceIndex $_.ifIndex "
          "-AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object IPAddress) } } | ConvertTo-Json -Compress")
    out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]).strip()
    if not out:
        return []
    data = json.loads(out)
    return [(d["name"], d.get("ips") or []) for d in (data if isinstance(data, list) else [data])]


def driver_name(dev):
    driver = dev / "device" / "driver"
    return driver.resolve().name if driver.exists() else None


def usb_links_linux(net=Path("/sys/class/net")):
    """[(interface, [ipv4...])] for interfaces driven by a USB network-gadget driver."""
    links = []
    for dev in net.glob("*"):
        if driver_name(dev) in ("cdc_ncm", "cdc_ether", "rndis_host"):
            out = run(["ip", "-4", "-o", "addr", "show", "dev", dev.name])
            links.append((dev.name, re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)))
    return links


def fix_usb_address():
    """Ask for a new address on any Pi USB link that doesn't have one in the Pi's subnet."""
    links = usb_links_windows() if IS_WINDOWS else usb_links_linux()
    for name, ips in links:
        if any(ip.startswith(USB_SUBNET) for ip in ips):
            continue
        log(f"'{name}' has no address from the Pi yet - asking again")
        if IS_WINDOWS:
            run(["ipconfig", "/renew", name], timeout=30)
        elif shutil.which("nmcli"):
            run(["nmcli", "device", "connect", name], timeout=30)
        elif shutil.which("dhclient"):
            run(["dhclient", "-1", name], timeout=30)
    return bool(links)


# --- finding the radar ---

def find_radar():
    for host in HOSTS:
        url = f"http://{host}:{PORT}/"
        try:
            with urllib.request.urlopen(url + "api/state", timeout=2) as r:
                if r.status == 200:
                    return url
        except OSError:
            pass
    return None


def wait_for_radar(timeout=None):
    end = None if timeout is None else time.time() + timeout
    last_fix = 0.0
    while end is None or time.time() < end:
        url = find_radar()
        if url:
            return url
        if time.time() - last_fix > 10:  # don't hammer DHCP; the Pi needs ~30-60 s to boot anyway
            fix_usb_address()
            last_fix = time.time()
        time.sleep(2)
    return None


# --- start at login ---

def autostart_path():
    if IS_WINDOWS:
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{APP_NAME}.vbs"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / f"{APP_NAME}.desktop"


def install():
    script = Path(__file__).resolve()
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        # pythonw has no console window; a .vbs launcher keeps the Startup entry silent
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = pyw if pyw.exists() else Path(sys.executable)
        path.write_text(f'CreateObject("WScript.Shell").Run """{exe}"" ""{script}"" --watch", 0, False\n')
    else:
        path.write_text("[Desktop Entry]\nType=Application\nName=LD2410C Radar\n"
                        f"Comment=Open the radar page when the Raspberry Pi is plugged in\n"
                        f'Exec="{sys.executable}" "{script}" --watch\n'
                        "Terminal=false\nX-GNOME-Autostart-enabled=true\n")
    log(f"Installed: {path}")
    log("The radar will open by itself whenever the Pi is plugged in after you log in.")


def uninstall():
    path = autostart_path()
    if path.exists():
        path.unlink()
        log(f"Removed {path}")
    else:
        log("Nothing to remove")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="keep running and open the radar each time the Pi appears")
    mode.add_argument("--install", action="store_true", help="run --watch automatically at login")
    mode.add_argument("--uninstall", action="store_true", help="remove the login entry")
    ap.add_argument("--no-browser", action="store_true", help="just report where the radar is")
    ap.add_argument("--timeout", type=float, default=120, help="seconds to wait in the default mode (default 120)")
    args = ap.parse_args()

    if args.install:
        return install()
    if args.uninstall:
        return uninstall()

    if not args.watch:
        log("Looking for the radar (plug the Pi in if it isn't already)...")
        url = wait_for_radar(args.timeout)
        if not url:
            log("Couldn't reach the Pi. Check it has had a minute to boot, the cable is in its USB-C port "
                "and is a data cable, and that a USB network adapter appeared on this PC.")
            return 1
        log(f"Radar is at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    log("Watching for the Pi - the radar opens whenever it is plugged in.")
    was_up = False
    while True:
        url = find_radar() if was_up else wait_for_radar()
        if url and not was_up:
            log(f"Pi connected: {url}")
            if not args.no_browser:
                webbrowser.open(url)
        elif not url and was_up:
            log("Pi disconnected")
        was_up = bool(url)
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
