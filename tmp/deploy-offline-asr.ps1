param(
  [string]$Key = "D:\download\ss1.pem",
  [string]$Server = "root@39.106.210.20",
  # Magpie 的 Meta Tunnel 会在沙箱层拦掉所有 22 端口，绑到 Wi-Fi 网卡的源地址即可绕开。
  # 走 VPN 或换网卡时用 -BindAddress "" 关掉。
  [string]$BindAddress = "192.168.0.199",
  [string]$ApiBase = "http://127.0.0.1:8765",
  [switch]$RestartEngine,
  [switch]$CopySettings,
  [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Remote = "/opt/livenotes"
$Stage = Join-Path $env:TEMP ("livenotes-deploy-" + (Get-Date -Format "yyyyMMddHHmmss"))

$SshOpts = @("-i", $Key, "-o", "StrictHostKeyChecking=no")
if ($BindAddress) { $SshOpts = $SshOpts + @("-o", "BindAddress=$BindAddress") }

# 原生命令的 stderr 在 ErrorActionPreference=Stop 下会变成致命 ErrorRecord，
# 而 ssh/pip/systemctl 正常也会往 stderr 写字（ssh 的 Warning、pip 的进度）。
# 所以调用期间临时降到 Continue，并把 stderr 行按普通文本收回来。
#
# 编码：远端（Debian，LANG 未设时也是 UTF-8）输出的是 UTF-8，而 PS 5.1 会把
# 管道默认按控制台代码页（GBK）解，中文全成乱码，日志没法看。这里临时把
# [Console]::OutputEncoding 设成 UTF-8 收完再还原。
function Invoke-Native {
  param([string]$Exe, [string[]]$ArgList)
  $prevEap = $ErrorActionPreference
  $prevEnc = [Console]::OutputEncoding
  $ErrorActionPreference = "Continue"
  [Console]::OutputEncoding = [Text.Encoding]::UTF8
  try {
    $out = & $Exe @ArgList 2>&1 | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { [string]$_ }
    }
    $code = $LASTEXITCODE
  } finally {
    [Console]::OutputEncoding = $prevEnc
    $ErrorActionPreference = $prevEap
  }
  $text = ($out | ForEach-Object { $_ }) -join "`n"
  if ($null -eq $text) { $text = "" }
  return [pscustomobject]@{ Code = $code; Text = $text }
}

function Invoke-Remote {
  param([string]$Command, [switch]$Quiet)
  $r = Invoke-Native "ssh" ($SshOpts + @($Server, $Command))
  if (-not $Quiet -and $r.Text.Trim()) { Write-Host $r.Text.TrimEnd() }
  return $r
}

New-Item -ItemType Directory -Force -Path $Stage | Out-Null

Write-Host "== 1/8 检查 SSH =="
$r = Invoke-Remote "cd $Remote && echo ok" -Quiet
if ($r.Code -ne 0) {
  throw "SSH 不通（$($r.Text.Trim())）。先在华为云控制台解封本机 IP / 确认 22 端口安全组；走 VPN 时试试 -BindAddress ''"
}

Write-Host "== 2/8 本地测试 =="
if ($SkipTests) {
  Write-Host "   已跳过（-SkipTests）"
} else {
  Push-Location $Root
  try {
    # 优先用项目 venv：全局 python 常常没装 pytest。
    $Py = "python"
    foreach ($c in @(".venv\Scripts\python.exe", "venv\Scripts\python.exe", ".venv/bin/python")) {
      if (Test-Path (Join-Path $Root $c)) { $Py = (Join-Path $Root $c); break }
    }
    Write-Host "   用 $Py"
    $t = Invoke-Native $Py @("-m", "pytest", "-q")
    Write-Host $t.Text.TrimEnd()
    if ($t.Code -ne 0) { throw "本地测试没过，不部署。确认要强推用 -SkipTests" }
  } finally { Pop-Location }
}

Write-Host "== 3/8 打包（git archive HEAD: app/ + tools/）=="
# 不再手写文件白名单：以前漏过 app/reading.py，新机器人一 import 就挂。
# 从 HEAD 打包，工作区里未提交的改动不会上云，所以先提醒。
$st = Invoke-Native "git" @("-C", $Root, "status", "--porcelain", "--", "app", "tools")
if ($st.Text.Trim()) {
  Write-Host "   注意：以下改动未提交，不会被部署：" -ForegroundColor Yellow
  foreach ($line in ($st.Text.Trim() -split "`r?`n")) { Write-Host "     $line" -ForegroundColor Yellow }
}
$Tarball = Join-Path $Stage "code.tar.gz"
$ar = Invoke-Native "git" @("-C", $Root, "archive", "--format=tar.gz", "-o", $Tarball, "HEAD", "app", "tools")
if ($ar.Code -ne 0) { throw "git archive 失败：$($ar.Text.Trim())" }
Write-Host ("   包大小 {0:N0} KB" -f ((Get-Item $Tarball).Length / 1KB))

Write-Host "== 4/8 上传 =="
# -O 走老式 scp 协议：OpenSSH 9+ 默认用 SFTP，部分服务器没装 sftp-server。
$u = Invoke-Native "scp" ($SshOpts + @("-O", $Tarball, "${Server}:/tmp/livenotes-code.tar.gz"))
if ($u.Code -ne 0) { throw "上传代码包失败：$($u.Text.Trim())" }
$u = Invoke-Native "scp" ($SshOpts + @("-O", (Join-Path $PSScriptRoot "deploy-remote.sh"), "${Server}:/tmp/deploy-remote.sh"))
if ($u.Code -ne 0) { throw "上传部署脚本失败：$($u.Text.Trim())" }
$u = Invoke-Native "scp" ($SshOpts + @("-O", (Join-Path $PSScriptRoot "live-guard.py"), "${Server}:/tmp/live-guard.py"))
if ($u.Code -ne 0) { throw "上传闸门脚本失败：$($u.Text.Trim())" }
$u = Invoke-Native "scp" ($SshOpts + @("-O", (Join-Path $PSScriptRoot "restart-engine.sh"), "${Server}:/tmp/restart-engine.sh"))
if ($u.Code -ne 0) { throw "上传重启脚本失败：$($u.Text.Trim())" }

Write-Host "== 5/8 云端解包（自动备份）=="
$r = Invoke-Remote "bash /tmp/deploy-remote.sh /tmp/livenotes-code.tar.gz"
if ($r.Code -ne 0) {
  throw "云端解包/编译失败，见上面的输出。代码已覆盖但没有重启任何服务，修好后重跑即可。"
}
$Backup = ""
foreach ($line in ($r.Text -split "`r?`n")) {
  if ($line -like "BACKUP=*") { $Backup = $line.Substring(7).Trim() }
}
Write-Host "   备份：$Backup"

Write-Host "== 6/8 依赖与模型 =="
$r = Invoke-Remote "cd $Remote && .venv/bin/pip install -q -i https://repo.huaweicloud.com/repository/pypi/simple 'sherpa-onnx>=2.0.1'" -Quiet
if ($r.Code -ne 0) {
  Write-Host "   $($r.Text.TrimEnd())" -ForegroundColor Red
  throw "依赖安装失败"
}
$r = Invoke-Remote "cd $Remote && .venv/bin/python -c 'import sherpa_onnx as s; print(s.__version__)'" -Quiet
Write-Host "   sherpa-onnx $($r.Text.Trim())"

$r = Invoke-Remote "cd $Remote && .venv/bin/python tools/fetch_models.py" -Quiet
$tail = ($r.Text.TrimEnd() -split "`r?`n" | Select-Object -Last 3) -join " | "
Write-Host "   $tail"
if ($r.Code -ne 0) { throw "模型准备失败（上面是最后几行）" }

Write-Host "== 7/8 服务单元与重启 =="
# 离线服务单元只在内容变了才覆盖，否则每次部署都白改一次 systemd。
$Want = (Get-FileHash (Join-Path $PSScriptRoot "livenotes-offline-asr.service") -Algorithm SHA256).Hash.ToLower()
$r = Invoke-Remote "sha256sum -b /etc/systemd/system/livenotes-offline-asr.service 2>/dev/null | cut -d' ' -f1" -Quiet
$Have = (($r.Text -split "`r?`n") | Where-Object { $_ -match "^[0-9a-f]{64}$" } | Select-Object -First 1)
if ($Have -ne $Want) {
  Write-Host "   服务单元有变化，重新安装"
  $u = Invoke-Native "scp" ($SshOpts + @("-O", (Join-Path $PSScriptRoot "livenotes-offline-asr.service"), "${Server}:/etc/systemd/system/livenotes-offline-asr.service"))
  if ($u.Code -ne 0) { throw "上传服务单元失败：$($u.Text.Trim())" }
  $r = Invoke-Remote "systemctl daemon-reload && systemctl enable livenotes-offline-asr" -Quiet
  if ($r.Code -ne 0) { throw "systemd 重载失败：$($r.Text.Trim())" }
} else {
  Write-Host "   服务单元没变，跳过"
}

# 补转写的收尾接口和发布开关：只补缺失的键，不动已有值。
$envCmd = "cd $Remote; ENV=livenotes.env; " +
  "touch `$ENV; " +
  "grep -q '^LIVENOTES_API=' `$ENV || echo 'LIVENOTES_API=$ApiBase' >> `$ENV; " +
  "grep -q '^LIVENOTES_OFFLINE_ASR_FINALIZE=' `$ENV || echo 'LIVENOTES_OFFLINE_ASR_FINALIZE=1' >> `$ENV; " +
  "grep -E '^(LIVENOTES_API|LIVENOTES_OFFLINE_ASR_FINALIZE|FEISHU_PUBLISH_ENABLED)=' `$ENV"
$r = Invoke-Remote $envCmd -Quiet
Write-Host (($r.Text.TrimEnd() -split "`r?`n") -join "   ")
if ($r.Code -ne 0) { throw "写 $Remote/livenotes.env 失败：$($r.Text.Trim())" }

# 重启顺序有讲究：离线补转写要调引擎的 /api/sessions/finalize，
# 先把引擎换上新接口，再让补转写去用；反过来会先撞一轮 404。
#
# 统一走 /tmp/restart-engine.sh：它先过闸门（有场次在录就拒绝），再停下游、
# 重启引擎、等端口真的 LISTEN、最后拉起下游。不能直接 systemctl restart
# livenotes —— 下游三个服务都跟着引擎走，会一起重启并猛连 8765，新引擎在
# 端口探测阶段就判「已被占用」退出，两边拉锯（实测重试 6 次才起来）。
$g = Invoke-Remote "python3 /tmp/live-guard.py $ApiBase" -Quiet
$guard = $g.Text.Trim()
Write-Host "   引擎闸门：$guard"
if ($RestartEngine) {
  if ($g.Code -eq 3) {
    throw "有场次正在录（$guard），拒绝重启。等它停了再跑，或去掉 -RestartEngine 只更新代码"
  }
  Write-Host "   重启引擎（按顺序：停下游 → 重启引擎 → 等端口 → 拉起下游）"
  $r = Invoke-Remote "bash /tmp/restart-engine.sh $ApiBase" -Quiet
  Write-Host (($r.Text.TrimEnd() -split "`r?`n") -join "`n   ")
  if ($r.Code -ne 0) { throw "引擎重启失败：$($r.Text.Trim())" }
  foreach ($u in @("livenotes", "livenotes-feishu", "livenotes-offline-asr", "livenotes-capture")) {
    $s = Invoke-Remote "systemctl is-active $u" -Quiet
    Write-Host "   $u`: $($s.Text.Trim())"
  }
} else {
  Write-Host "   没传 -RestartEngine，引擎还是旧进程（代码已就位，下次重启自动生效）。" -ForegroundColor Yellow
  Write-Host "   新接口 /api/sessions/finalize 要等引擎重启后才存在；下播后带 -RestartEngine 再跑一次。" -ForegroundColor Yellow
}

$r = Invoke-Remote "systemctl restart livenotes-offline-asr; sleep 2; systemctl is-active livenotes-offline-asr" -Quiet
Write-Host "   offline-asr: $($r.Text.Trim())"
$r = Invoke-Remote "journalctl -u livenotes-offline-asr -n 6 --no-pager | tail -6" -Quiet
Write-Host $r.Text.TrimEnd()

if ($CopySettings) {
  $Settings = Join-Path $env:LOCALAPPDATA "LiveNotes\data\settings.json"
  if (Test-Path $Settings) {
    Write-Host "== 复制大模型设置（含 key，600）=="
    $u = Invoke-Native "scp" ($SshOpts + @("-O", $Settings, "${Server}:$Remote/data/settings.json"))
    if ($u.Code -ne 0) { throw "上传 settings.json 失败：$($u.Text.Trim())" }
    $r = Invoke-Remote "chown livenotes:livenotes $Remote/data/settings.json; chmod 600 $Remote/data/settings.json" -Quiet
  } else {
    Write-Host "   本机没有 $Settings，跳过"
  }
}

Write-Host "== 8/8 状态 =="
Invoke-Remote "curl -s --max-time 5 $ApiBase/api/health || echo NO_ENGINE" | Out-Null
Invoke-Remote "systemctl --no-pager --full status livenotes livenotes-feishu livenotes-offline-asr 2>/dev/null | grep -E '^(Active:)'" | Out-Null
Invoke-Remote "free -m | head -3" | Out-Null
Invoke-Remote "journalctl -u livenotes-offline-asr -n 12 --no-pager" | Out-Null

Write-Host ""
Write-Host "完成。看飞书 /状态、/文档 CH01。回滚："
Write-Host "   ssh $Server -i $Key 'tar -xzf $Backup/code.tar.gz -C $Remote && systemctl restart livenotes'"
Write-Host "发布默认关；要开就在 $Remote/livenotes.env 设 FEISHU_PUBLISH_ENABLED=1（另需 docx/bitable 权限）。"
Write-Host "本地暂存目录 $Stage（可删）。"
