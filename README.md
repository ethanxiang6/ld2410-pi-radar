# LD2410C Pi Radar

A radar-scope web page for the HLK-LD2410C presence sensor wired to a Raspberry Pi.
Plug the Pi into your PC with one USB cable. The Pi shows up as a network adapter
("USB tethering"), and the radar opens in your browser at `http://10.42.0.1:8080`.
It works from Windows and Linux.

```
 LD2410C ──UART──▶ Raspberry Pi ──USB cable (network)──▶ PC browser
                   radar_server.py                        radar UI
```

**Range only.** The LD2410C reports *how far away* someone is (0 to about 6 m), not which
direction. So the scope draws a person as an arc across the sensor's ±60° field of view, with a
blip on the centre line. It can tell whether someone is **moving away** or **coming closer**
(from how their distance changes), but not left/right. It also reports at most one moving and
one still target, so it can't track several people. For X/Y positions of up to 3 people, use
the HLK-LD2450.

## Features

- **Person tracking:** one smooth person track built from the sensor's moving and still
  targets plus its raw per-gate energy. It keeps following you after the sensor's own moving
  target drops out (it does that past about 2 m when you walk away). It ignores the stale and
  bogus distances the sensor sometimes reports.
- **Empty-room calibration:** you leave the room for about 25 s. The Pi records each gate's
  background noise and writes custom trigger thresholds to the sensor.
- **Desk mode:** 0.2 m gates (about 1.8 m range) instead of 0.75 m. Needs sensor firmware 2.44+.
- **Live gate-energy chart** with each gate's trigger threshold, plus a hold-time setting,
  factory reset and a 60 s distance history.
- **Self-contained:** the Pi needs Python 3 and pyserial; the page has no internet dependencies.

## 1. Wiring

| LD2410C | Raspberry Pi (header pin) |
|---------|---------------------------|
| VCC     | 5V (pin 2 or 4)           |
| GND     | GND (pin 6)               |
| TX      | GPIO15 / RXD (pin 10)     |
| RX      | GPIO14 / TXD (pin 8)      |
| OUT     | optional, not used        |

The sensor's logic is 3.3 V, so it connects straight to the Pi's GPIO.
A USB-TTL adapter also works. Use `SERIAL=/dev/ttyUSB0` when installing.

## 2. Pi setup (once)

USB tethering works on **Pi Zero / Zero 2 W / 4 / 5**. On a Pi 4/5 it goes through the
**USB-C power port**: one cable powers the Pi *and* networks it. On a Zero, use the port marked
"USB" (not "PWR"). A Pi 3 can't do it.

### Over SSH (Pi on your Wi-Fi or Ethernet)

**Windows:**

```powershell
.\windows\deploy.ps1 -PiHost raspberrypi.local -User pi -UsbGadget
```

The first run installs your SSH key on the Pi and asks for the Pi password twice. Later runs
just copy the files, and the Pi restarts the server by itself.

**Linux:**

```bash
scp -r pi pi@raspberrypi.local:ld2410-radar
ssh -t pi@raspberrypi.local "cd ld2410-radar && sudo bash install.sh && sudo bash setup-usb-gadget.sh && sudo reboot"
```

Leave out `setup-usb-gadget.sh` if you only want the radar on your network.
`install.sh` enables the GPIO UART, installs the `ld2410-radar` systemd service, and makes
`/opt/ld2410-radar` writable by your user. A path unit restarts the service whenever its code
changes, so later updates are just:

```bash
scp pi/*.py pi@10.42.0.1:/opt/ld2410-radar/ && scp pi/web/index.html pi@10.42.0.1:/opt/ld2410-radar/web/
```

### No SSH? Use the SD card

Put the Pi's SD card in your PC (Windows or Linux) and open its boot partition:

1. Copy `pi/radar-firstboot.sh` to the boot partition. Copy `ld2410.py`, `radar_server.py`,
   `usb-gadget-ncm.sh` and `web/index.html` into an `ld2410-radar/` folder there.
2. Add `dtoverlay=dwc2,dr_mode=peripheral` to the end of `config.txt`.
3. In `cmdline.txt` (one line), remove `console=serial0,115200`. Then append:
   `systemd.run=/boot/firmware/radar-firstboot.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target`

On the next boot the Pi installs everything, removes that line again, and reboots.
It writes a log to `radar-firstboot.log` on the boot partition.

### Show it on a monitor on the Pi (optional)

With Raspberry Pi OS (desktop, auto-login) and a screen on the Pi's HDMI port, run this on the Pi
as your normal user (not sudo). It opens the radar full-screen in Chromium at every boot:

```bash
bash ld2410-radar/setup-kiosk.sh            # undo with --remove
```

### Power and cables

Plug into a **USB-C port on the PC** if you can (1.5–3 A); a USB-A port (0.5–0.9 A) may
not be enough for a Pi 4. Use a cable that **carries data**, since many charging cables don't.

## 3. Open the radar

```bash
python start_radar.py
```

(On Linux, use `python3`.) This waits for the Pi, fixes the PC's address on the USB link if it
needs it, and opens the radar in your browser. You can also just browse to http://10.42.0.1:8080.

**Open it automatically:**

```bash
python start_radar.py --install     # at every login, watch for the Pi and open the radar when it appears
python start_radar.py --uninstall   # undo
```

On Windows this adds a hidden launcher to your Startup folder. On Linux it adds an XDG
autostart entry (`~/.config/autostart/ld2410-radar.desktop`), which GNOME, KDE, Xfce and
most desktops honour. `python start_radar.py --watch` does the same in a terminal.

**No driver needed:** the Pi presents a USB **NCM** network adapter. Windows 10/11 sets it up
by itself ("UsbNcm Host Device"), and Linux uses `cdc_ncm`.

## 4. Using it

1. **Calibrate:** in *Tuning*, click **Calibrate empty room** and leave within the 10 s
   countdown. The factory thresholds often treat furniture as a still person.
2. **Desk mode:** if you sit within about 1.8 m of the sensor, switch to **0.2 m desk** and
   calibrate again.
3. **Hold time:** how long "PERSON" stays after the last detection. Raise it if it clears
   while you sit very still.
4. **Raw readings** shows the sensor's own moving and still arcs under the tracked person.

Mount the sensor upright at chest height, with its antenna side facing the room. Avoid metal
in front of or behind it, and don't point it at fans, AC vents or curtains.

## Try it without hardware

```bash
python pi/radar_server.py --demo --port 8080            # simulated person
python pi/radar_server.py --replay walk.jsonl --port 8080  # play back a recording
```

## Server options

```
--serial /dev/serial0   UART device
--baud 256000           sensor baud (factory default)
--port 8080             HTTP port
--basic                 don't enable engineering mode (no per-gate energy)
--demo                  simulated data
--replay FILE           play back a recording (JSON lines of {"t", "frame", "config"})
```

Endpoints: `/` (UI), `/events` (Server-Sent Events stream), `/api/state` (latest JSON), and
POST `/api/calibrate`, `/api/hold`, `/api/resolution`, `/api/thresholds`, `/api/factory-reset`.

## Troubleshooting

- **"no sensor" / "No frames from sensor"**: swap TX and RX, check 5 V, and run
  `journalctl -u ld2410-radar -f`.
- **PC can't reach 10.42.0.1 after the Pi reboots**: the PC asked for an address too early.
  `start_radar.py` fixes this. By hand: `ipconfig /renew` on Windows, or
  `nmcli device connect <iface>` on Linux.
- **Garbled data on a Pi 3/4/Zero**: `/dev/serial0` is the mini UART when Bluetooth is on.
  Add `dtoverlay=disable-bt` to `/boot/firmware/config.txt` and reboot to use the full UART.
- **Pi 5**: GPIO14/15 is `/dev/ttyAMA0`. Add `dtparam=uart0=on` to config.txt and install
  with `sudo SERIAL=/dev/ttyAMA0 bash install.sh`.
