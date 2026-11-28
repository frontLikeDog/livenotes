# 等云端场次全部结束后自动部署。
# Magpie Meta Tunnel 会拦 22 端口，必须绑当前 Wi-Fi 源的地址；旧值 192.168.0.199
# 是上次的 DHCP 地址，现在网卡已经变成别的地址，这里在部署时动态取。
param([switch]$SkipTests)

$ErrorActionPreference = 'Stop'

$ip = ''
try { $ip = (Get-NetIPAddress -InterfaceAlias 'WLAN' -AddressFamily IPv4 -ErrorAction Stop).IPAddress } catch {}
if (-not $ip) {
  $ip = (Get-NetIPAddress -AddressFamily IPv4 |
         Where-Object { $_.IPAddress -like '192.168.*' -or $_.IPAddress -like '10.*' } |
         Select-Object -First 1).IPAddress
}
Write-Host "BindAddress=$ip"

try {
  & (Join-Path $PSScriptRoot 'deploy-offline-asr.ps1') -BindAddress $ip -RestartEngine -SkipTests:$SkipTests
} catch {
  Write-Host "部署失败：$($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
exit 0
