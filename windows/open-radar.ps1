# Finds the Pi (USB tether first, then the network) and opens the radar in your browser.
param(
    [string[]]$Hosts = @("10.42.0.1", "humansensor.local", "raspberrypi.local"),
    [int]$Port = 8080
)

# If Windows asked the Pi for an address while it was still booting, it gives up and sits on a
# 169.254.x.x fallback for minutes. Ask again.
$usb = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match 'UsbNcm|RNDIS' -and $_.Status -eq 'Up' }
foreach ($a in $usb) {
    $ip = Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
    if (-not ($ip | Where-Object { $_.IPAddress -like '10.42.0.*' })) {
        Write-Host "Asking the Pi for an address on '$($a.Name)' ..."
        ipconfig /renew "$($a.Name)" | Out-Null
    }
}

foreach ($h in $Hosts) {
    $url = "http://${h}:$Port/"
    Write-Host -NoNewline "Trying $url ... "
    try {
        Invoke-WebRequest -Uri "${url}api/state" -TimeoutSec 3 -UseBasicParsing | Out-Null
        Write-Host "found"
        Start-Process $url
        return
    } catch {
        Write-Host "no"
    }
}
Write-Host ""
Write-Host "Couldn't reach the Pi. Check:"
Write-Host "  - it has had ~1 minute to boot"
Write-Host "  - the cable is in the Pi's USB-C port and is a data cable (not charge-only)"
Write-Host "  - Device Manager shows 'UsbNcm Host Device' under Network adapters"
