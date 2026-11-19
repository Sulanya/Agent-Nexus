# Agent Nexus - self-host launcher (Windows / PowerShell 5.1+)
#   an.ps1 -Action start|stop|status|restart|link   [-NoTunnel] [-NoPause] [-Port 8790]
param(
  [ValidateSet('start','stop','status','restart','link')]
  [string]$Action = 'start',
  [switch]$NoTunnel,
  [switch]$NoPause,
  [int]$Port = 0
)

$ErrorActionPreference = 'Stop'
# PowerShell 5.1 默认按控制台代码页写 stdout；启动器用 UTF-8 读取，
# 不显式设置就会把中文错误提示变成一串问号/乱码。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$Root    = Split-Path -Parent $PSScriptRoot
$RunDir  = Join-Path $Root '_run'
$CfgPath = Join-Path $Root 'an.config.json'
$Version = '1.8.0'
# 版本号单一真源: deploy\VERSION (打包时写入), 读不到才退回上面的兜底值
try { $v = [System.IO.File]::ReadAllText((Join-Path $Root 'VERSION')).Trim(); if ($v) { $Version = $v } } catch { }

if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Force -Path $RunDir | Out-Null }
try { if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Force -Path $RunDir | Out-Null } } catch { }
# 这个文件夹能不能写: 只读会让网关生不出 token 和日志, 先给一句人话提示。
$script:RootWritable = $false
$script:HadFailure = $false
try {
  $probe = Join-Path $RunDir ('.w-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
  [System.IO.File]::WriteAllText($probe, 'ok')
  Remove-Item -LiteralPath $probe -Force
  $script:RootWritable = $true
} catch { }

function Say([string]$m, [string]$c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Blank() { Write-Host '' }

# Windows 的 Start-Process 不会给 -ArgumentList 的数组元素加引号:
# 路径只要带空格 (例如 C:\Users\x\下载 测试\Agent Nexus 便携版),
# 参数会被劈成两半, 网关和隧道会静默起不来。这里按 Windows 规则手动加引号。
function Join-Args([string[]]$list) {
  $out = @()
  foreach ($item in $list) {
    $t = [string]$item
    if ($t -match '[\s"]') {
      $t = $t -replace '\\+$', '$0$0'
      $t = $t -replace '(\\*)"', '$1$1\"'
      $out += ('"' + $t + '"')
    } else {
      $out += $t
    }
  }
  return ($out -join ' ')
}

function Banner {
  $line = '  +' + ('-' * 62) + '+'
  Blank
  Say $line 'DarkCyan'
  Say ('  |' + ('      A G E N T   N E X U S   -   remote gateway').PadRight(62) + '|') 'DarkCyan'
  Say ('  |' + ('      v' + $Version + '   your PC, on your phone').PadRight(62) + '|') 'DarkCyan'
  Say $line 'DarkCyan'
  Blank
}

function Read-Cfg {
  $cfg = [pscustomobject]@{
    _readme = 'Agent Nexus config. Save it, then reopen AgentNexus.exe to apply.'
    port = 8790; host = '127.0.0.1'; tunnel = $true
    protocol = 'http2'; autoOpenBrowser = $false; extraRoots = @()
  }
  if (Test-Path $CfgPath) {
    try {
      $j = Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($j.port) { $cfg.port = [int]$j.port }
      if ($j.host) { $cfg.host = [string]$j.host }
      if ($null -ne $j.tunnel) { $cfg.tunnel = [bool]$j.tunnel }
      if ($j.protocol) { $cfg.protocol = [string]$j.protocol }
      if ($null -ne $j.autoOpenBrowser) { $cfg.autoOpenBrowser = [bool]$j.autoOpenBrowser }
      if ($j.extraRoots) { $cfg.extraRoots = @($j.extraRoots) }
    } catch {
      $cfg | Add-Member -NotePropertyName _error -NotePropertyValue ([string]$_.Exception.Message) -Force
    }
  }
  return $cfg
}

function Save-Cfg($cfg) {
  try {
    [System.IO.File]::WriteAllText($CfgPath, ($cfg | ConvertTo-Json -Depth 4),
      (New-Object System.Text.UTF8Encoding $false))
  } catch { }
}

function Test-Listen([int]$p, [int]$timeoutMs = 350) {
  if ($p -le 0) { return $false }
  $c = New-Object System.Net.Sockets.TcpClient
  $iar = $null
  try {
    $iar = $c.BeginConnect('127.0.0.1', $p, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne($timeoutMs)) { return $false }
    $c.EndConnect($iar)
    return $true
  } catch { return $false }
  finally {
    try { $c.Close() } catch { }
    try { if ($iar) { $iar.AsyncWaitHandle.Close() } } catch { }
  }
}

function Test-ContainsPath([string]$text, [string]$needle) {
  if (-not $text -or -not $needle) { return $false }
  return ($text.IndexOf($needle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Stop-ProcTree([int]$procId) {
  if ($procId -le 0) { return }
  $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
  try {
    if (Test-Path -LiteralPath $taskkill) {
      & $taskkill /PID $procId /T /F 2>$null | Out-Null
    } else {
      Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
  } catch { }
}

function Clear-Links {
  foreach ($name in @('LINKS.txt', 'TUNNEL-URL.txt')) {
    $p = Join-Path $RunDir $name
    try {
      if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
    } catch { }
  }
}

function Get-GwProcs {
  @(Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { (Test-ContainsPath $_.CommandLine 'remote_server.py') -and (Test-ContainsPath $_.CommandLine $Root) })
}

function Get-CfProcs {
  @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { (Test-ContainsPath $_.ExecutablePath $Root) -or (Test-ContainsPath $_.CommandLine $Root) })
}

function Get-Py([switch]$Console) {
  $pw = Join-Path $Root 'runtime\python\pythonw.exe'
  $pe = Join-Path $Root 'runtime\python\python.exe'
  if (-not $Console) { if (Test-Path $pw) { return @{ exe = $pw; pre = @() } } }
  if (Test-Path $pe) { return @{ exe = $pe; pre = @() } }
  if (Get-Command py -ErrorAction SilentlyContinue) { return @{ exe = 'py'; pre = @('-3') } }
  if (Get-Command python -ErrorAction SilentlyContinue) { return @{ exe = 'python'; pre = @() } }
  return $null
}

function Get-LanIPv4 {
  $found = New-Object System.Collections.ArrayList
  try {
    foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
      if ($nic.OperationalStatus -ne [System.Net.NetworkInformation.OperationalStatus]::Up) { continue }
      if ($nic.NetworkInterfaceType -eq [System.Net.NetworkInformation.NetworkInterfaceType]::Loopback) { continue }
      foreach ($addr in $nic.GetIPProperties().UnicastAddresses) {
        if ($addr.Address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { continue }
        $ip = [string]$addr.Address
        if ($ip -match '^192\.168\.') { [void]$found.Add($ip) }
        elseif ($ip -match '^10\.') { [void]$found.Add($ip) }
        elseif ($ip -match '^172\.(1[6-9]|2[0-9]|3[0-1])\.') { [void]$found.Add($ip) }
      }
    }
  } catch { }
  if ($found.Count -gt 0) { return [string]$found[0] }
  return ''
}

function Get-TunnelUrl([switch]$Fresh) {
  $log = Join-Path $RunDir 'cloudflared.log'
  if (Test-Path $log) {
    $m = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches | Select-Object -Last 1
    if ($m) { return ($m.Matches | Select-Object -Last 1).Value }
  }
  # -Fresh: start 路径专用 —— 只认本次运行写出的 cloudflared.log。
  # 绝不回退到 TUNNEL-URL.txt: 那是上一轮的旧地址(隧道早死了), 回退会让下面的轮询第一次
  # 就"成功", 于是把死链写进 LINKS.txt 给用户 (实测: 第二次启动拿到上一轮域名, 打开 530)。
  if ($Fresh) { return '' }
  $f = Join-Path $RunDir 'TUNNEL-URL.txt'
  if (Test-Path $f) { return (Get-Content $f -Raw).Trim() }
  return ''
}

function Ensure-FsRoots($cfg) {
  $p = Join-Path $Root 'gateway\fs_roots.json'
  if (Test-Path $p) { return }
  $list = New-Object System.Collections.ArrayList
  $cands = @()
  if ($env:USERPROFILE) {
    $cands += (Join-Path $env:USERPROFILE '.codex')
    $cands += (Join-Path $env:USERPROFILE 'Documents')
    $cands += (Join-Path $env:USERPROFILE 'Desktop')
  }
  foreach ($e in @($cfg.extraRoots)) { if ($e) { $cands += $e } }
  foreach ($c in $cands) { if ($c -and (Test-Path $c)) { [void]$list.Add((Get-Item $c).FullName) } }
  [System.IO.File]::WriteAllText($p, (ConvertTo-Json -InputObject @($list) -Depth 3),
    (New-Object System.Text.UTF8Encoding $false))
}

function Start-Gateway([int]$port, [string]$bind) {
  $py = Get-Py -Console
  if (-not $py) { throw 'Python not found (missing runtime\python and no system Python).' }
  $env:AGENTREMOTE_PORT = [string]$port
  $env:AGENTREMOTE_HOST = $bind
  $env:PYTHONUTF8 = '1'
  $script  = Join-Path $Root 'gateway\remote_server.py'
  if (-not (Test-Path $script)) { $script = Join-Path $Root 'gateway\remote_server.pyc' }   # 交付包只带 .pyc
  if (-not (Test-Path $script)) { throw 'Missing gateway\remote_server.py (or .pyc).' }
  $outLog  = Join-Path $RunDir 'gateway.out.log'
  $errLog  = Join-Path $RunDir 'gateway.err.log'
  $args = @()
  $args += $py.pre
  $args += @('-X', 'utf8', $script)
  # 不用 -RedirectStandardOutput: PowerShell 5.1 在这种模式下拿不到 ExitCode
  # (读出来是空字符串), 网关崩溃时用户只能看到 “code ”。让 cmd 自己重定向,
  # 启动器就能可靠拿到真实退出码。
  $inner = (Join-Args (@($py.exe) + $args)) + ' 1>"' + $outLog + '" 2>"' + $errLog + '"'
  $cmdArgs = '/d /s /c "' + $inner + '"'
  $proc = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') -ArgumentList $cmdArgs `
    -WorkingDirectory $Root -WindowStyle Hidden -PassThru
  if (-not $proc) { throw 'Start-Process did not return a gateway process.' }
  return @{ proc = $proc; pid = $proc.Id }
}

function Start-Tunnel([int]$port, [string]$proto, [string]$bind = '127.0.0.1') {
  $cf = Join-Path $Root 'tools\cloudflared.exe'
  if (-not (Test-Path $cf)) { Say '[!] 缺少 tools\cloudflared.exe, 跳过外网隧道 (只在本机可用)' 'Yellow'; return '' }
  $log = Join-Path $RunDir 'cloudflared.log'
  if (Test-Path $log) { Move-Item -LiteralPath $log -Destination (Join-Path $RunDir 'cloudflared.prev.log') -Force }
  # 旧地址一并清掉, 否则 TUNNEL-URL.txt 会在新隧道就绪前被当成结果返回 (见 Get-TunnelUrl -Fresh)
  foreach ($stale in @('TUNNEL-URL.txt', 'LINKS.txt')) {
    $sp = Join-Path $RunDir $stale
    if (Test-Path $sp) { Remove-Item -LiteralPath $sp -Force }
  }
  $connectHost = '127.0.0.1'
  if ($bind -and $bind -ne '0.0.0.0' -and $bind -ne '::') { $connectHost = $bind }
  $cfArgs = @('tunnel', '--url', ('http://' + $connectHost + ':' + $port), '--protocol', $proto, '--logfile', $log)
  $proc = Start-Process -FilePath $cf -WorkingDirectory $Root -WindowStyle Hidden -ArgumentList (Join-Args $cfArgs) -PassThru
  for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Milliseconds 800
    try { if ($proc -and $proc.HasExited) { Say ('[!] cloudflared 提前退出, code ' + $proc.ExitCode) 'Yellow'; return '' } } catch { }
    $u = Get-TunnelUrl -Fresh
    if ($u) { return $u }
  }
  return ''
}

function Show-Qr([string]$link) {
  $py = Get-Py -Console
  $qr = Join-Path $Root 'tools\qrprint.py'
  if (-not $py) {
    Say '[!] 二维码组件不可用: 缺少 Python 运行时; 上面的链接仍可手动复制。' 'Yellow'
    return
  }
  if (-not (Test-Path $qr)) {
    Say '[!] 二维码组件缺失 (tools\qrprint.py); 上面的链接仍可手动复制。' 'Yellow'
    return
  }
  try {
    $env:PYTHONUTF8 = '1'
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    $a = @(); $a += $py.pre; $a += @('-X', 'utf8', $qr, $link)
    & $py.exe @a
  } catch {
    Say '[!] 二维码渲染失败, 手动打开上面的链接即可' 'Yellow'
  }
}

# ---- self check -------------------------------------------------------
# 关键: 自检绝不能去查刚生成的公网域名。
# 实测 (2026-10-16): 建隧道后立刻查 DNS 会得到 NXDOMAIN, 而这个“解析不到”会被递归解析器
# 负缓存约 60 秒 -> 手机跟着一起打不开。隔 36 秒不查、再查就正常。所以公网通路改用
# “云边缘 IP + Host 头”直接探活: 不需要解析那个域名, 也不污染任何缓存。
function Test-Local([int]$port, [string]$tok) {
  try { [System.Net.WebRequest]::DefaultWebProxy = $null } catch { }
  $script:LastLocalAuthError = $false
  $hosts = @('127.0.0.1')
  $lan = Get-LanIPv4
  if ($lan) { $hosts += $lan }
  foreach ($h in ($hosts | Select-Object -Unique)) {
    $u = 'http://' + $h + ':' + $port + '/?k=' + $tok
    try {
      $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 6 -Headers @{ 'X-Nexus-Selfcheck' = '1' }
      if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400) { return $true }
    } catch {
      $c = 0
      try { $c = [int]$_.Exception.Response.StatusCode } catch { }
      if ($c -eq 401 -or $c -eq 403) { $script:LastLocalAuthError = $true }
    }
  }
  return $false
}

function Test-Edge([string]$url, [string]$tok) {
  $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
  if (-not (Test-Path $curl)) { return '' }
  $h = ([Uri]$url).Host
  $ips = New-Object System.Collections.ArrayList
  foreach ($apex in @('trycloudflare.com', 'cloudflare.com')) {
    try {
      foreach ($a in @(Resolve-DnsName $apex -Type A -ErrorAction Stop | Where-Object { $_.IPAddress })) {
        if (-not $ips.Contains($a.IPAddress)) { [void]$ips.Add($a.IPAddress) }
      }
    } catch { }
  }
  foreach ($fallback in @('104.16.230.132', '104.16.231.132')) {
    if (-not $ips.Contains($fallback)) { [void]$ips.Add($fallback) }
  }
  foreach ($ip in $ips) {
    $code = ''
    try {
      $code = (& $curl --noproxy '*' -sS -m 20 -A 'NexusSelfCheck/1' -H 'X-Nexus-Selfcheck: 1' -o NUL -w '%{http_code}' --resolve ($h + ':443:' + $ip) ($url + '/?k=' + $tok)) 2>$null
    } catch { $code = '' }
    if ($code -eq '200' -or $code -eq '401' -or $code -eq '403') { return $ip }
  }
  return ''
}

function Test-PublicDns([string]$url) {
  $h = ([Uri]$url).Host
  $ips = @()
  try {
    $ips = @(Resolve-DnsName $h -Type A -ErrorAction Stop | Where-Object { $_.IPAddress } | ForEach-Object { $_.IPAddress })
  } catch { $ips = @() }
  if ($ips.Count -gt 0) {
    Say ('   域名解析  : 已生效 (' + ($ips -join ',') + ')') 'Green'
  } else {
    Say '   域名解析  : 你的网络还没解析到它' 'Yellow'
    Say '               等 1 分钟再刷新; 仍不行就把手机切成流量。' 'DarkGray'
    Say '               别连续点 —— 每次失败都会把“解析不到”再缓存一分钟。' 'DarkGray'
  }
}

function Self-Check([int]$port, [string]$tok, [string]$url, [bool]$fresh) {
  Blank
  Say '  自检:' 'DarkGray'
  if (Test-Local $port $tok) { Say '    本机网关  : 通' 'Green' }
  elseif ($script:LastLocalAuthError) { Say '    本机网关  : 有响应但访问 token 不匹配 (看 _run\gateway.err.log)' 'Red' }
  else { Say '    本机网关  : 没通 (看 _run\gateway.err.log)' 'Yellow' }
  $h = ''
  try { $h = ([Uri]$url).Host } catch { }
  if ($h -match '^(127\.|localhost$|::1$|\[::1\])') {
    Say '    本机模式  : 只有这台电脑能打开; 想给同 Wi-Fi 手机用就把 host 改成 0.0.0.0' 'DarkGray'
    return
  }
  if ($h -match '^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)') {
    Say '    局域网模式: 手机需和电脑在同一 Wi-Fi, 直接打开上面的地址' 'DarkGray'
    return
  }
  $edge = Test-Edge $url $tok
  if ($edge) { Say ('    外网通路  : 通 (经 Cloudflare 边缘 ' + $edge + ')') 'Green' }
  else { Say '    外网通路  : 隧道还在连, 过 10 秒在控制台点「重新检测」再看' 'Yellow' }
  if ($fresh) {
    Say '    域名解析  : 新地址刚开始生效 (约 1 分钟)' 'Yellow'
    Say '               现在打不开就等 1 分钟再刷新, 别连续点或反复重试 ——' 'DarkGray'
    Say '               每次失败都会让“解析不到”再被缓存一分钟。' 'DarkGray'
  } else {
    Test-PublicDns $url
  }
}

function Show-Links([string]$url, [string]$tok, [string]$vtok, [int]$port, [bool]$tunnelOn = $true) {
  Blank
  if ($url) {
    $full = $url + '/?k=' + $tok
    Say '  ------------------------------------------------------------------' 'Green'
    Say '   手机打开这个链接 (或扫下面二维码):' 'White'
    Blank
    Say ('   ' + $full) 'Cyan'
    Blank
    Say '  ------------------------------------------------------------------' 'Green'
    Blank
    Show-Qr $full
    if ($vtok) {
      Say ('   只读观赏链接 (给别人看, 只能看不能改):') 'DarkGray'
      Say ('   ' + $url + '/?k=' + $vtok) 'DarkGray'
      Blank
    }
    try { Set-Clipboard -Value $full; Say '   (链接已复制到剪贴板)' 'DarkGray' } catch { }
    Say '   iOS: Safari 分享 -> 添加到主屏幕    安卓: Chrome 菜单 -> 添加到主屏幕' 'DarkGray'
    if (-not $tunnelOn) {
      $hh = ''
      try { $hh = ([Uri]$url).Host } catch { }
      if ($hh -match '^(127\.|localhost$|::1$|\[::1\])') {
        Say '   (本机模式: 只有这台电脑能打开; 想给同 Wi-Fi 手机用就把 host 改成 0.0.0.0)' 'DarkGray'
      } elseif ($hh -match '^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)') {
        Say '   (局域网模式: 手机要和这台电脑连同一个 Wi-Fi)' 'DarkGray'
      } else {
        Say '   (自定义地址模式: 请确认手机能访问这个地址)' 'DarkGray'
      }
    }
  } else {
    Say ('   [本机] http://127.0.0.1:' + $port + '/?k=' + $tok) 'Cyan'
    if ($tunnelOn) { Say '   (隧道没起来: 检查网络, 或在控制台点「重新检测」看日志)' 'Yellow' }
    else {
      Say '   (本机模式: "tunnel": false -- 现在只有这台电脑能打开)' 'DarkGray'
      Say '   (想让同一个 Wi-Fi 的手机也能开: 把 an.config.json 的 "host" 改成 0.0.0.0 再启动)' 'DarkGray'
    }
  }
  Blank
}

function Get-TrialState([string]$Cmd = 'ensure') {
  $py = Get-Py -Console
  if (-not $py) { return $null }
  $sc = Join-Path $Root 'gateway\trial_core.py'
  if (-not (Test-Path $sc)) { $sc = Join-Path $Root 'gateway\trial_core.pyc' }
  if (-not (Test-Path $sc)) { return $null }
  $raw = ''
  try {
    $a = @()
    if ($py.pre) { $a += $py.pre }
    $a += @($sc, $Cmd)
    $raw = (& $py.exe $a 2>$null | Out-String).Trim()
  } catch { return $null }
  if (-not $raw) { return $null }
  try { return ($raw | ConvertFrom-Json) } catch { return $null }
}


function Cmd-Start($cfg) {
  if ($cfg._error) {
    Say '[x] an.config.json 解析失败, 已拒绝启动, 不会偷偷使用默认端口。' 'Red'
    Say ('    错误: ' + $cfg._error) 'Yellow'
    Say '    修好 JSON 后重试; 也可以先备份它再删除, 让程序重新生成默认配置。' 'DarkGray'
    $script:HadFailure = $true
    return
  }
  $usePort = $cfg.port
  if ($Port -gt 0) { $usePort = $Port }
  if ($usePort -lt 1 -or $usePort -gt 65535) {
    Say ('[x] 端口配置不合法: ' + $usePort + ' (必须是 1-65535)') 'Red'
    $script:HadFailure = $true
    return
  }
  # 解压不完整 (杀软拦截 / 下载断流) 要在启动前说人话, 不能把缺文件误判成篡改结界。
  $required = @(
    @{ Label = '网关程序'; Paths = @('gateway\remote_server.py', 'gateway\remote_server.pyc') },
    @{ Label = '试用模块'; Paths = @('gateway\trial_core.py', 'gateway\trial_core.pyc') },
    @{ Label = '自护模块'; Paths = @('gateway\guard.py', 'gateway\guard.pyc') },
    @{ Label = '界面文件'; Paths = @('ui\index.html') }
  )
  $missing = @()
  foreach ($item in $required) {
    $found = $false
    foreach ($rel in $item.Paths) {
      if (Test-Path -LiteralPath (Join-Path $Root $rel)) { $found = $true; break }
    }
    if (-not $found) { $missing += $item.Label }
  }
  if ($missing.Count -gt 0) {
    Blank
    Say '[x] 安装不完整: 关键文件缺失, 不能启动。' 'Red'
    Say ('    缺少: ' + ($missing -join '、')) 'Yellow'
    Say '    多半是解压没完成, 或被杀毒软件/安全软件拦截后删掉了。' 'Yellow'
    Say '    请删除这个文件夹, 重新完整解压, 再双击 AgentNexus.exe。' 'Yellow'
    Blank
    $script:HadFailure = $true
    return
  }
  $tokFile  = Join-Path $Root 'gateway\.secrets\token.txt'
  $vtokFile = Join-Path $Root 'gateway\.secrets\view_token.txt'
  $gw = @(Get-GwProcs)
  if ($gw.Count -gt 0 -and (Test-Listen $usePort)) {
    $pids = (($gw | ForEach-Object { $_.ProcessId }) -join ',')
    Say ('[=] 网关已在运行 (pid ' + $pids + '  端口 ' + $usePort + ')') 'Green'
  } else {
    if ($gw.Count -gt 0) {
      Say '[!] 发现本包残留网关但端口未监听, 先清理旧进程 ...' 'Yellow'
      Start-Sleep -Milliseconds 1200
      if (-not (Test-Listen $usePort)) {
        foreach ($old in @(Get-GwProcs)) { Stop-ProcTree ([int]$old.ProcessId) }
        Start-Sleep -Milliseconds 600
      }
    }
    if (Test-Listen $usePort) {
      $np = $usePort + 1
      while (($np -le ($usePort + 20)) -and (Test-Listen $np)) { $np++ }
      if ($np -gt ($usePort + 20)) {
        Say ('[x] 端口 ' + $usePort + ' 附近的端口都不可用, 请先关掉占用程序或改 an.config.json') 'Red'
        $script:HadFailure = $true
        return
      }
      Say ('[!] 端口 ' + $usePort + ' 被别的程序占用, 改用 ' + $np) 'Yellow'
      $usePort = $np
      $cfg.port = $np
      Save-Cfg $cfg
    }
    Say ('[*] 启动网关 (端口 ' + $usePort + ') ...') 'Gray'
    if (-not $RootWritable) {
      Say '[x] 这个文件夹不可写: 网关要在这里写日志和访问 token。' 'Red'
      Say '    换个位置解压 - 放「文档」或「桌面」, 别放进 Program Files 这类只读目录。' 'Yellow'
      $script:HadFailure = $true
      return
    }
    # 上一次运行留下的 token / 链接绝不能被新进程复用。
    Clear-Links
    foreach ($stale in @($tokFile, $vtokFile)) {
      try {
        if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Force }
      } catch { }
    }
    Ensure-FsRoots $cfg
    $launch = $null
    try { $launch = Start-Gateway $usePort $cfg.host }
    catch {
      Clear-Links
      Say ('[x] 网关启动命令失败: ' + $_.Exception.Message) 'Red'
      Say '    检查 runtime\python 是否存在, 以及 gateway\remote_server.py 是否完整。' 'Yellow'
      $script:HadFailure = $true
      return
    }
    $proc = $launch.proc
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
      Start-Sleep -Milliseconds 400
      $exited = $false
      $exitCode = ''
      try {
        if ($proc -and $proc.HasExited) {
          $exited = $true
          try {
            $null = $proc.WaitForExit(1500)
            $exitCode = [string]$proc.ExitCode
          } catch {
            $exitCode = '未知'
          }
        }
      } catch { }
      if ($exited) {
        if (-not $exitCode) { $exitCode = '未知' }
        Say ('[x] 网关进程提前退出 (code ' + $exitCode + ')') 'Red'
        break
      }
      if (Test-Listen $usePort 150) { $ok = $true; break }
    }
    if (-not $ok) {
      if ($proc) { Stop-ProcTree ([int]$proc.Id) }
      foreach ($old in @(Get-GwProcs)) { Stop-ProcTree ([int]$old.ProcessId) }
      Clear-Links
      Say '[x] 网关没起来. 日志尾部:' 'Red'
      foreach ($f in @('gateway.out.log', 'gateway.err.log', 'gateway.log')) {
        $p = Join-Path $RunDir $f
        if (Test-Path -LiteralPath $p) {
          Say ('--- ' + $f + ' ---') 'DarkGray'
          try { Get-Content -LiteralPath $p -Tail 12 } catch { }
        }
      }
      $script:HadFailure = $true
      return
    }
    Say '[=] 网关已就绪' 'Green'
  }

  $tok = ''; $vtok = ''
  for ($i = 0; $i -lt 20; $i++) {
    if (Test-Path -LiteralPath $tokFile) { $tok = (Get-Content -LiteralPath $tokFile -Raw).Trim() }
    if ($tok) { break }
    try { if ($proc -and $proc.HasExited) { break } } catch { }
    Start-Sleep -Milliseconds 400
  }
  if (-not $tok) {
    if ($proc) { Stop-ProcTree ([int]$proc.Id) }
    foreach ($old in @(Get-GwProcs)) { Stop-ProcTree ([int]$old.ProcessId) }
    Clear-Links
    Say '[x] 网关没有写出访问 token, 未发布任何链接。' 'Red'
    foreach ($f in @('gateway.out.log', 'gateway.err.log')) {
      $p = Join-Path $RunDir $f
      if (Test-Path -LiteralPath $p) {
        Say ('--- ' + $f + ' ---') 'DarkGray'
        try { Get-Content -LiteralPath $p -Tail 12 } catch { }
      }
    }
    $script:HadFailure = $true
    return
  }
  if (Test-Path -LiteralPath $vtokFile) { $vtok = (Get-Content -LiteralPath $vtokFile -Raw).Trim() }

  Write-Host -NoNewline '[*] 检测引擎: '
  $cx = Get-CodexExe
  $cl = Get-ClaudeExe
  $wb = Get-WorkBuddyExe
  $names = @()
  if ($cx) { $names += 'Codex' }
  if ($cl) { $names += 'Claude Code' }
  if ($wb) { $names += 'WorkBuddy' }
  if ($names.Count -gt 0) { Say ($names -join ' + ') 'Green' }
  else { Say '未找到 Codex / Claude Code / WorkBuddy (装上其中一个并登录后就能用)' 'Yellow' }
  if ($trial -and $trial.mode -eq 'active') {
    $rr = [int][Math]::Max(0, [double]$trial.remain)
    $hh = [int][Math]::Floor($rr / 3600); $mi = [int][Math]::Floor(($rr % 3600) / 60); $se = $rr % 60
    $rrTxt = if ($hh -gt 0) { ('{0}:{1:d2}:{2:d2}' -f $hh, $mi, $se) } else { ('{0}:{1:d2}' -f $mi, $se) }
    Say ('[*] 内测试用: 剩余 ' + $rrTxt + '  (编号 #' + $trial.id + ')') 'Cyan'
  }

  $url = ''
  $tunnelUrl = ''
  $fresh = $false
  if ($cfg.tunnel -and -not $NoTunnel) {
    $cf = @(Get-CfProcs)
    if ($cf.Count -gt 0) {
      Say '[=] 隧道已在运行' 'Green'
      $tunnelUrl = Get-TunnelUrl
    } else {
      Say '[*] 打外网隧道 (cloudflared quick tunnel, 约 5-20 秒) ...' 'Gray'
      $tunnelUrl = Start-Tunnel $usePort $cfg.protocol $cfg.host
      if (-not $tunnelUrl) { Say '[!] 隧道没拿到公网地址, 看 _run\cloudflared.log' 'Yellow' }
      else { $fresh = $true }
    }
  }

  # 本机模式: 没有隧道也要给出可用的本机链接, 否则启动器会误判"网关没起来"。
  $localHost = '127.0.0.1'
  if ($cfg.host -eq '0.0.0.0' -or $cfg.host -eq '::' -or $cfg.host -eq '*') {
    $lan = Get-LanIPv4
    if ($lan) { $localHost = $lan }
  } elseif ($cfg.host -and $cfg.host -ne '127.0.0.1' -and $cfg.host -ne 'localhost') {
    $localHost = $cfg.host
  }
  if ($tunnelUrl) { $url = $tunnelUrl } else { $url = 'http://' + $localHost + ':' + $usePort }

  if ($tunnelUrl) {
    [System.IO.File]::WriteAllText((Join-Path $RunDir 'TUNNEL-URL.txt'), $tunnelUrl, (New-Object System.Text.UTF8Encoding $false))
  }
  $linkText = 'FULL : ' + $url + '/?k=' + $tok + [Environment]::NewLine
  if ($vtok) { $linkText += 'VIEW : ' + $url + '/?k=' + $vtok + [Environment]::NewLine }
  if ($url -and $tok) {
    [System.IO.File]::WriteAllText((Join-Path $RunDir 'LINKS.txt'), $linkText,
      (New-Object System.Text.UTF8Encoding $false))
  }
  Show-Links $url $tok $vtok $usePort ([bool]($cfg.tunnel -and -not $NoTunnel))
  if ($url) { Self-Check $usePort $tok $url $fresh }
  if ($url -and $cfg.autoOpenBrowser) { try { Start-Process ($url + '/?k=' + $tok) } catch { } }
  if ($url) {
    if ($tunnelUrl) { Say '  提醒: 这个地址是免费临时隧道, 重启网关上会换新地址; 想要固定地址见 README。' 'DarkGray' }
    else { Say '  提醒: 当前是本机/局域网模式; 公网访问需要在 an.config.json 里打开 tunnel。' 'DarkGray' }
    Say '  关闭本窗口不会停服务 (网关在后台跑); 要停服务请在控制台点「停止」。' 'DarkGray'
  }
  Blank
}

function Get-CodexExe {
  $base = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
  if (Test-Path $base) {
    $f = Get-ChildItem $base -Recurse -Filter codex.exe -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($f) { return $f.FullName }
  }
  $c = Get-Command codex -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  return ''
}

function Get-ClaudeExe {
  $dirs = @()
  if ($env:CLAUDE_BIN_DIRS) { $dirs += ($env:CLAUDE_BIN_DIRS -split ';') }
  if ($env:USERPROFILE) {
    $dirs += (Join-Path $env:USERPROFILE 'AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin')
    $dirs += (Join-Path $env:USERPROFILE 'AppData\Local\npm\node_modules\@anthropic-ai\claude-code\bin')
    $dirs += (Join-Path $env:USERPROFILE '.claude\local')
  }
  foreach ($d in $dirs) {
    if ($d -and (Test-Path $d)) {
      $f = Get-ChildItem $d -Filter claude.exe -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($f) { return $f.FullName }
    }
  }
  $c = Get-Command claude -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  return ''
}

function Get-WorkBuddyExe {
  $roots = @()
  foreach ($k in @('WORKBUDDY_CLI_DIR', 'WORKBUDDY_HOME')) {
    $raw = [Environment]::GetEnvironmentVariable($k)
    if ($raw) {
      $roots += $raw.Trim('"')
      try { $roots += (Split-Path -Parent (Split-Path -Parent $raw.Trim('"'))) } catch { }
    }
  }
  if ($env:LOCALAPPDATA) {
    $roots += (Join-Path $env:LOCALAPPDATA 'Programs\WorkBuddy')
    $roots += (Join-Path $env:LOCALAPPDATA 'WorkBuddy')
  }
  if ($env:USERPROFILE) {
    $roots += (Join-Path $env:USERPROFILE '.workbuddy')
    $roots += (Join-Path $env:USERPROFILE '.codebuddy')
  }
  if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles 'WorkBuddy') }
  if (${env:ProgramFiles(x86)}) { $roots += (Join-Path ${env:ProgramFiles(x86)} 'WorkBuddy') }
  foreach ($root in ($roots | Select-Object -Unique)) {
    if (-not $root -or -not (Test-Path $root)) { continue }
    foreach ($cli in @((Join-Path $root 'resources\app.asar.unpacked\cli'), (Join-Path $root 'resources\app\out\cli'), (Join-Path $root 'cli'))) {
      foreach ($name in @('codebuddy.exe', 'codebuddy')) {
        $candidate = Join-Path (Join-Path $cli 'bin') $name
        if (Test-Path $candidate) { return $candidate }
      }
    }
  }
  $c = Get-Command codebuddy -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  $c = Get-Command workbuddy -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  return ''
}

function Cmd-Stop {
  $n = 0
  foreach ($p in @(Get-CfProcs)) { Stop-ProcTree ([int]$p.ProcessId); $n++ }
  foreach ($p in @(Get-GwProcs)) {
    Stop-ProcTree ([int]$p.ProcessId)
    $n++
  }
  $stCfg = Read-Cfg
  $stPort = $stCfg.port
  if ($Port -gt 0) { $stPort = $Port }
  $released = $false
  for ($i = 0; $i -lt 20; $i++) {
    $gwLeft = @(Get-GwProcs).Count
    $cfLeft = @(Get-CfProcs).Count
    # A foreign program may own the configured port.  Stop succeeded once this
    # package's own gateway and tunnel are gone; the next start auto-shifts the
    # port if the old one is still occupied.
    if ($gwLeft -eq 0 -and $cfLeft -eq 0) { $released = $true; break }
    Start-Sleep -Milliseconds 400
  }
  if ($released) {
    Say ('[=] 已停止 ' + $n + ' 个进程 (网关 + 隧道), 本包网关和隧道进程已退出') 'Green'
  } else {
    Say ('[!] 停止请求已发出, 但仍有本包进程未退出; ' + $stPort + ' 未释放; 请稍后重试或查看任务管理器') 'Red'
    $script:HadFailure = $true
  }
  Clear-Links
}

function Cmd-Status($cfg) {
  $gw = @(Get-GwProcs); $cf = @(Get-CfProcs)
  if ($gw.Count -gt 0) { Say ('网关        : 运行中  pid ' + (($gw | ForEach-Object { $_.ProcessId }) -join ',')) 'Green' }
  else { Say '网关        : 已停止' 'Red' }
  $stPort = $cfg.port
  if ($Port -gt 0) { $stPort = $Port }
  if (Test-Listen $stPort) { Say ('端口 ' + $stPort + '    : 监听中') 'Green' } else { Say ('端口 ' + $stPort + '    : 未监听') 'Yellow' }
  if ($cf.Count -gt 0) { Say ('隧道        : 运行中  pid ' + (($cf | ForEach-Object { $_.ProcessId }) -join ',')) 'Green' }
  else { Say '隧道        : 已停止' 'Yellow' }
  $u = Get-TunnelUrl
  if ($u) { Say ('当前地址    : ' + $u) 'Cyan' } else { Say '当前地址    : (无, 还没建过隧道)' 'DarkGray' }
  $cx = Get-CodexExe; $cl = Get-ClaudeExe; $wb = Get-WorkBuddyExe
  Say ('Codex       : ' + $(if ($cx) { $cx } else { '未找到' })) 'DarkGray'
  Say ('Claude Code : ' + $(if ($cl) { $cl } else { '未找到' })) 'DarkGray'
  Say ('WorkBuddy   : ' + $(if ($wb) { $wb } else { '未找到' })) 'DarkGray'
  Say '本机模式    : 不限时，无设备绑定和反爬锁定' 'Cyan'
  $glog = Join-Path $RunDir 'gateway.log'
  if (Test-Path $glog) { Say ('网关日志    : ' + $glog) 'DarkGray' }
}

Banner
$cfg = Read-Cfg
switch ($Action) {
  'start'   { Cmd-Start $cfg }
  'stop'    { Cmd-Stop }
  'restart' { Cmd-Stop; Start-Sleep -Seconds 2; Cmd-Start $cfg }
  'status'  { Cmd-Status $cfg }
  'link'    {
    $tok = ''; $vtok = ''
    $tf = Join-Path $Root 'gateway\.secrets\token.txt'
    $vf = Join-Path $Root 'gateway\.secrets\view_token.txt'
    if (Test-Path $tf) { $tok = (Get-Content $tf -Raw).Trim() }
    if (Test-Path $vf) { $vtok = (Get-Content $vf -Raw).Trim() }
    $lkPort = $cfg.port
    if ($Port -gt 0) { $lkPort = $Port }
    Show-Links (Get-TunnelUrl) $tok $vtok $lkPort ([bool]$cfg.tunnel)
  }
}
if ($script:HadFailure) { exit 1 }
if (-not $NoPause) {
  Blank
  Write-Host '按回车键关闭本窗口 (服务继续在后台运行) ...' -ForegroundColor DarkGray
  try { [void](Read-Host) } catch { }
}
