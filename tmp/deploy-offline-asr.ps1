param(
  [string]$Key = "D:\download\ss1.pem",
  [string]$Server = "root@39.106.210.20",
  [switch]$RestartEngine,
  [switch]$CopySettings
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$SshArgs = @("-i", $Key, "-o", "StrictHostKeyChecking=no")

Write-Host "== 1/5 检查 SSH =="
& ssh @SshArgs $Server "cd /opt/livenotes && echo ok"
if ($LASTEXITCODE -ne 0) { throw "SSH 不通：先在华为云控制台解封本机 IP，或确认 22 端口安全组" }

Write-Host "== 2/5 上传代码 =="
$AppFiles = @(
  "app\config.py", "app\store.py", "app\live.py", "app\livebrief.py",
  "app\server.py", "app\offline_asr.py"
) | ForEach-Object { Join-Path $Root $_ }
& scp @SshArgs $AppFiles "${Server}:/opt/livenotes/app/"
if ($LASTEXITCODE -ne 0) { throw "上传 app 失败" }
& scp @SshArgs (Join-Path $Root "tools\offline_asr.py") "${Server}:/opt/livenotes/tools/"
& scp @SshArgs (Join-Path $Root "tools\feishu_bot.py") "${Server}:/opt/livenotes/tools/"
if ($LASTEXITCODE -ne 0) { throw "上传 tools 失败" }
& ssh @SshArgs $Server "cd /opt/livenotes && chown livenotes:livenotes app/*.py tools/*.py"

Write-Host "== 3/5 安装 sherpa-onnx 与模型（约 270MB，首次较慢）=="
& ssh @SshArgs $Server "cd /opt/livenotes && .venv/bin/pip install -i https://repo.huaweicloud.com/repository/pypi/simple 'sherpa-onnx>=2.0.1'"
if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }
& ssh @SshArgs $Server "cd /opt/livenotes && .venv/bin/python tools/fetch_models.py"
if ($LASTEXITCODE -ne 0) { throw "模型下载失败" }

Write-Host "== 4/5 安装 systemd 服务 =="
& scp @SshArgs (Join-Path $PSScriptRoot "livenotes-offline-asr.service") "${Server}:/etc/systemd/system/livenotes-offline-asr.service"
& ssh @SshArgs $Server "systemctl daemon-reload && systemctl enable --now livenotes-offline-asr && systemctl restart livenotes-feishu"

if ($CopySettings) {
  $Settings = Join-Path $env:LOCALAPPDATA "LiveNotes\data\settings.json"
  if (Test-Path $Settings) {
    Write-Host "== 复制大模型设置（含 key，600）=="
    & scp @SshArgs $Settings "${Server}:/opt/livenotes/data/settings.json"
    & ssh @SshArgs $Server "chown livenotes:livenotes /opt/livenotes/data/settings.json && chmod 600 /opt/livenotes/data/settings.json"
  }
}

if ($RestartEngine) {
  Write-Host "== 重启引擎：会中断正在录的场次（WAV 会保留）=="
  & ssh @SshArgs $Server "systemctl restart livenotes"
}

Write-Host "== 5/5 状态 =="
& ssh @SshArgs $Server "systemctl --no-pager --full status livenotes-offline-asr | head -20; echo ---; free -m; echo ---; journalctl -u livenotes-offline-asr -n 20 --no-pager"
Write-Host "完成。看飞书 /状态 或发 /总结 CH01。"
