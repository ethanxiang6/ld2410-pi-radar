# Copies the Pi side of the radar to your Raspberry Pi over SSH and installs it.
#   .\deploy.ps1 -User ethan                       # over the USB cable (10.42.0.1)
#   .\deploy.ps1 -PiHost raspberrypi.local -User pi
#   .\deploy.ps1 -User ethan -Full                 # force a full (sudo) reinstall
#   .\deploy.ps1 -User ethan -UsbGadget            # also set up USB tethering (reboots the Pi)
# The first run installs your SSH key on the Pi (asks for the Pi password) and does a full
# install (asks for it again, for sudo). After that, updates just copy the files: the Pi
# restarts the server by itself, no password needed.
param(
    [string]$PiHost = "10.42.0.1",
    [string]$User = "pi",
    [switch]$Full,
    [switch]$UsbGadget
)
# No $ErrorActionPreference = "Stop": Windows PowerShell turns ssh's stderr into terminating errors.
# Native commands are checked through $LASTEXITCODE instead.
$src = Join-Path $PSScriptRoot "..\pi" | Resolve-Path -ErrorAction Stop
$target = "$User@$PiHost"
$dest = "/opt/ld2410-radar"

$key = Join-Path $HOME ".ssh\id_ed25519"
if (-not (Test-Path $key)) {
    Write-Host "==> Creating an SSH key for this PC ($key)"
    New-Item -ItemType Directory -Force (Split-Path $key) | Out-Null
    # empty passphrase: Windows PowerShell drops a bare "" argument, PowerShell 7.3+ passes it correctly
    if ($PSVersionTable.PSVersion -ge [version]"7.3") { ssh-keygen -t ed25519 -N "" -f $key -q }
    else { ssh-keygen -t ed25519 -N '""' -f $key -q }
    if ($LASTEXITCODE) { throw "ssh-keygen failed" }
}
ssh -o BatchMode=yes -o ConnectTimeout=5 $target true 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Installing your SSH key on the Pi - enter the Pi password for $User"
    Get-Content "$key.pub" | ssh -o StrictHostKeyChecking=accept-new $target "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    if ($LASTEXITCODE) { throw "could not install the SSH key" }
}

ssh $target "test -w $dest/radar_server.py && systemctl is-enabled --quiet ld2410-radar-reload.path"
$quick = ($LASTEXITCODE -eq 0) -and -not $Full -and -not $UsbGadget

if ($quick) {
    Write-Host "==> Updating $dest (the Pi restarts the server automatically)"
    scp "$src\ld2410.py" "$src\radar_server.py" "${target}:$dest/"
    scp "$src\web\index.html" "${target}:$dest/web/"
    if ($LASTEXITCODE) { throw "scp failed" }
} else {
    Write-Host "==> Full install - you'll be asked for the Pi password once more (sudo)"
    ssh $target "rm -rf ~/ld2410-radar"
    scp -r "$src" "${target}:ld2410-radar"
    if ($LASTEXITCODE) { throw "scp failed" }
    # Strip any CRLF that Windows may have added, then install.
    $cmd = "cd ~/ld2410-radar && sed -i 's/\r$//' *.sh *.py web/*.html && sudo bash install.sh"
    if ($UsbGadget) { $cmd += " && sudo bash setup-usb-gadget.sh && sudo reboot" }
    ssh -t $target $cmd
    if ($LASTEXITCODE -and -not $UsbGadget) { throw "install failed" }
}

Write-Host "`nDone. Open the radar with .\open-radar.ps1 (or http://${PiHost}:8080)"
