# Sample PC resources every 5 s into a CSV until a stop file appears.
# usage: powershell -File sysmon.ps1 <out.csv>    stop: create <out.csv>.stop
param([string]$Out)
$stop = "$Out.stop"
Remove-Item $stop -ErrorAction SilentlyContinue
"time,gpu_busy_pct,gpu_dedicated_gb,gpu_shared_gb,cpu_pct,ram_used_gb" | Out-File -Encoding utf8 $Out
$ramTotal = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB
while (-not (Test-Path $stop)) {
    $c = Get-Counter -ErrorAction SilentlyContinue -Counter @(
        '\GPU Engine(*engtype_3D)\Utilization Percentage',
        '\GPU Engine(*engtype_Compute*)\Utilization Percentage',
        '\GPU Adapter Memory(*)\Dedicated Usage',
        '\GPU Adapter Memory(*)\Shared Usage',
        '\Processor(_Total)\% Processor Time',
        '\Memory\Available MBytes')
    $s = $c.CounterSamples
    # Engine utilization is per process+engine; summed per engine type and capped at 100.
    $gpu = [math]::Min(100, (($s | Where-Object Path -match 'engtype_3d' | Measure-Object CookedValue -Sum).Sum +
                             ($s | Where-Object Path -match 'engtype_compute' | Measure-Object CookedValue -Sum).Sum))
    $ded = ($s | Where-Object Path -match 'dedicated usage' | Measure-Object CookedValue -Maximum).Maximum / 1GB
    $sha = ($s | Where-Object Path -match 'shared usage' | Measure-Object CookedValue -Maximum).Maximum / 1GB
    $cpu = ($s | Where-Object Path -match 'processor time').CookedValue
    $ram = $ramTotal - (($s | Where-Object Path -match 'available mbytes').CookedValue / 1024)
    "{0},{1:N1},{2:N2},{3:N2},{4:N1},{5:N2}" -f (Get-Date -Format 'HH:mm:ss'), $gpu, $ded, $sha, $cpu, $ram | Out-File -Append -Encoding utf8 $Out
    Start-Sleep -Seconds 4
}
Remove-Item $stop -ErrorAction SilentlyContinue
