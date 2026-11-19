#!/bin/bash
# Agent Nexus - self-host launcher (macOS)
#   tools/an.sh start|stop|status|restart|link|renew [--no-tunnel] [--port N]
#
# 与 Windows 版 tools/an.ps1 一一对应, 两者行为保持一致。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$ROOT/_run"
CFG="$ROOT/an.config.json"

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_CYAN=$'\033[36m'; C_GRAY=$'\033[90m'; C_WHITE=$'\033[97m'; C_OFF=$'\033[0m'
else
  C_RED=; C_GREEN=; C_YELLOW=; C_CYAN=; C_GRAY=; C_WHITE=; C_OFF=
fi
say()   { if [ -n "${2:-}" ]; then printf '%s%s%s\n' "$2" "$1" "$C_OFF"; else printf '%s\n' "$1"; fi }
blank() { printf '\n'; }

ACTION="start"; NOTUNNEL=0; PORT_ARG=0; NOPAUSE=0
_prev=""
for _a in "$@"; do
  if [ "$_prev" = "--port" ]; then PORT_ARG="$_a"; _prev=""; continue; fi
  case "$_a" in
    start|stop|status|restart|link|renew) ACTION="$_a" ;;
    --no-tunnel|-NoTunnel) NOTUNNEL=1 ;;
    --no-pause|-NoPause)   NOPAUSE=1 ;;
    --port) _prev="--port" ;;
    --port=*) PORT_ARG="${_a#--port=}" ;;
    *) : ;;
  esac
done
case "$PORT_ARG" in ''|*[!0-9]*) PORT_ARG=0 ;; esac

mkdir -p "$RUN_DIR" 2>/dev/null || true

# 这个文件夹能不能写: 只读会让网关生不出 token 和日志, 先给一句人话提示。
ROOT_WRITABLE=0
_probe="$RUN_DIR/.w-$$"
if ( : > "$_probe" ) 2>/dev/null; then ROOT_WRITABLE=1; rm -f "$_probe"; fi

# ---- 运行时发现 (Apple Silicon / Intel 双架构包) ----------------------
ARCH_RAW="$(uname -m)"
if [ "$ARCH_RAW" = "x86_64" ]; then
  _t="$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)"
  [ "$_t" = "1" ] && ARCH_RAW="arm64"        # 终端跑在 Rosetta 下也优先用原生 arm64
fi
case "$ARCH_RAW" in
  arm64|aarch64) RT_DIR="arm64"; CF_ARCH="arm64" ;;
  *)             RT_DIR="x64";   CF_ARCH="amd64" ;;
esac

PY_BIN=""
for _p in "$ROOT/runtime/$RT_DIR/python/bin/python3" "$ROOT/runtime/python/bin/python3" \
          /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$_p" ]; then PY_BIN="$_p"; break; fi
done
if [ -z "$PY_BIN" ] && command -v python3 >/dev/null 2>&1; then PY_BIN="$(command -v python3)"; fi

CF_BIN=""
for _p in "$ROOT/tools/cloudflared-$CF_ARCH" "$ROOT/tools/cloudflared"; do
  if [ -x "$_p" ]; then CF_BIN="$_p"; break; fi
done

# ---- 配置 -------------------------------------------------------------
port=8790; host=127.0.0.1; tunnel=true; protocol=http2; autoOpenBrowser=false
load_cfg() {
  [ -n "$PY_BIN" ] || return 0
  local out=""
  out="$("$PY_BIN" - "$CFG" <<'PYEOF' 2>/dev/null
import json,os,sys
p=sys.argv[1]; d={}
try:
    if os.path.exists(p):
        d=json.load(open(p,encoding='utf-8'))
except Exception:
    d={}
def emit(k,dflt):
    v=d.get(k)
    if isinstance(dflt,bool):
        print('%s=%s'%(k,'true' if (dflt if v is None else bool(v)) else 'false'))
    elif isinstance(dflt,int):
        try: n=int(v)
        except Exception: n=dflt
        print('%s=%d'%(k,n))
    else:
        print('%s=%s'%(k,(dflt if v is None else str(v))))
emit('port',8790); emit('host','127.0.0.1'); emit('tunnel',True)
emit('protocol','http2'); emit('autoOpenBrowser',False)
PYEOF
)" || out=""
  [ -n "$out" ] || return 0
  while IFS= read -r _line; do
    case "$_line" in
      port=*)            port="${_line#port=}" ;;
      host=*)            host="${_line#host=}" ;;
      tunnel=*)          tunnel="${_line#tunnel=}" ;;
      protocol=*)        protocol="${_line#protocol=}" ;;
      autoOpenBrowser=*) autoOpenBrowser="${_line#autoOpenBrowser=}" ;;
    esac
  done <<< "$out"
}

save_cfg_port() {
  [ -n "$PY_BIN" ] || return 0
  "$PY_BIN" - "$CFG" "$1" <<'PYEOF' >/dev/null 2>&1 || true
import json,os,sys
p,port=sys.argv[1],int(sys.argv[2])
d={}
try:
    if os.path.exists(p): d=json.load(open(p,encoding='utf-8'))
except Exception: d={}
d['port']=port
if '_readme' not in d:
    d['_readme']='Agent Nexus config. Save it, then double-click START.command again to apply.'
tmp=p+'.tmp-w'
json.dump(d,open(tmp,'w',encoding='utf-8'),ensure_ascii=False,indent=2)
os.replace(tmp,p)
PYEOF
}

ensure_fs_roots() {
  local p="$ROOT/gateway/fs_roots.json"
  [ -f "$p" ] && return 0
  [ -n "$PY_BIN" ] || return 0
  "$PY_BIN" - "$p" "$HOME" "$CFG" <<'PYEOF' >/dev/null 2>&1 || true
import json,os,sys
out,home,cfg=sys.argv[1],sys.argv[2],sys.argv[3]
cands=[os.path.join(home,'.codex'),os.path.join(home,'Documents'),os.path.join(home,'Desktop')]
try:
    d=json.load(open(cfg,encoding='utf-8'))
    for e in (d.get('extraRoots') or []):
        if e: cands.append(e)
except Exception: pass
res=[]
for c in cands:
    try:
        if c and os.path.exists(c): res.append(os.path.abspath(c))
    except Exception: pass
tmp=out+'.tmp-w'
json.dump(res,open(tmp,'w',encoding='utf-8'),ensure_ascii=False,indent=1)
os.replace(tmp,out)
PYEOF
}

# ---- 进程 / 端口 ------------------------------------------------------
gw_pids() {
  pgrep -fl "remote_server\.py" 2>/dev/null | while IFS= read -r _l; do
    _pid="${_l%% *}"; _cmd="${_l#* }"
    case "$_cmd" in *"$ROOT"*) printf '%s\n' "$_pid" ;; esac
  done
}
cf_pids() {
  pgrep -fl "cloudflared" 2>/dev/null | while IFS= read -r _l; do
    _pid="${_l%% *}"; _cmd="${_l#* }"
    case "$_cmd" in *"$ROOT"*) printf '%s\n' "$_pid" ;; esac
  done
}
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
join_pids() { tr '\n' ',' | sed 's/,$//'; }

tunnel_url() {   # $1="fresh" 只认本轮 cloudflared.log
  local log="$RUN_DIR/cloudflared.log" u=""
  if [ -f "$log" ]; then
    u="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" 2>/dev/null | tail -n 1)"
    if [ -n "$u" ]; then printf '%s' "$u"; return 0; fi
  fi
  # -Fresh: start 路径专用 —— 绝不回退 TUNNEL-URL.txt(那是上一轮的旧地址, 隧道早死了)
  [ "${1:-}" = "fresh" ] && return 0
  if [ -f "$RUN_DIR/TUNNEL-URL.txt" ]; then tr -d '\r\n' < "$RUN_DIR/TUNNEL-URL.txt"; fi
}

start_gateway() {
  local entry="$ROOT/gateway/remote_server.py"
  [ -f "$entry" ] || entry="$ROOT/gateway/remote_server.pyc"   # 交付包只带 .pyc (源码已编译交付)
  AGENTREMOTE_PORT="$1" AGENTREMOTE_HOST="$2" PYTHONUTF8=1 \
    nohup "$PY_BIN" -X utf8 "$entry" \
      >> "$RUN_DIR/gateway.out.log" 2>> "$RUN_DIR/gateway.err.log" &
  disown 2>/dev/null || true
}

start_tunnel() {
  local p="$1" proto="$2" log="$RUN_DIR/cloudflared.log" u=""
  [ -f "$log" ] && mv -f "$log" "$RUN_DIR/cloudflared.prev.log" 2>/dev/null
  rm -f "$RUN_DIR/TUNNEL-URL.txt" "$RUN_DIR/LINKS.txt" 2>/dev/null
  nohup "$CF_BIN" tunnel --url "http://127.0.0.1:$p" --protocol "$proto" --logfile "$log" \
      >> "$RUN_DIR/cloudflared.out.log" 2>&1 &
  disown 2>/dev/null || true
  local i=0
  while [ "$i" -lt 45 ]; do
    sleep 0.8
    u="$(tunnel_url fresh)"
    if [ -n "$u" ]; then printf '%s' "$u"; return 0; fi
    i=$((i+1))
  done
}

show_qr() {
  [ -n "$PY_BIN" ] || return 0
  PYTHONUTF8=1 "$PY_BIN" -X utf8 "$ROOT/tools/qrprint.py" "$1" 2>/dev/null \
    || say '[!] 二维码渲染失败, 手动打开上面的链接即可' "$C_YELLOW"
}

# ---- 自检 -------------------------------------------------------------
# 关键: 自检绝不能去查刚生成的公网域名。实测建隧道后立刻查 DNS 会得到 NXDOMAIN,
# 而这个“解析不到”会被递归解析器负缓存约 60 秒 -> 手机跟着一起打不开。
# 所以公网通路改用“云边缘 IP + Host 头”直接探活: 不解析那个域名, 也不污染缓存。
test_local() {
  local code
  code="$(curl --noproxy '*' -s -A 'NexusSelfCheck/1' -H 'X-Nexus-Selfcheck: 1' -o /dev/null -w '%{http_code}' -m 15 "http://127.0.0.1:$1/?k=$2" 2>/dev/null)"
  case "$code" in 200|201|202|204|301|302|303|304|401|403) return 0 ;; esac
  return 1
}

test_edge() {
  local url="$1" tok="$2" h ip code ips
  h="${url#https://}"; h="${h%%/*}"
  ips="$(dig +short "$h" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
  ips="$(printf '%s\n%s\n%s\n' "$ips" '104.16.230.132' '104.16.231.132')"
  for ip in $ips; do
    [ -n "$ip" ] || continue
    code="$(curl --noproxy '*' -s -A 'NexusSelfCheck/1' -H 'X-Nexus-Selfcheck: 1' -o /dev/null -w '%{http_code}' -m 20 --resolve "$h:443:$ip" "$url/?k=$tok" 2>/dev/null)"
    case "$code" in 200|401|403) printf '%s' "$ip"; return 0 ;; esac
  done
  return 1
}

test_public_dns() {
  local h ips
  h="${1#https://}"; h="${h%%/*}"
  ips="$(dig +short "$h" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | tr '\n' ',' | sed 's/,$//')"
  if [ -n "$ips" ]; then
    say "   域名解析  : 已生效 ($ips)" "$C_GREEN"
  else
    say '   域名解析  : 你的网络还没解析到它' "$C_YELLOW"
    say '               等 1 分钟再刷新; 仍不行就把手机切成流量。' "$C_GRAY"
    say '               别连续点 —— 每次失败都会把“解析不到”再缓存一分钟。' "$C_GRAY"
  fi
}

self_check() {
  local port="$1" tok="$2" url="$3" fresh="$4" edge
  blank; say '  自检:' "$C_GRAY"
  if test_local "$port" "$tok"; then say '    本机网关  : 通' "$C_GREEN"
  else say '    本机网关  : 没通 (看 _run/gateway.log)' "$C_YELLOW"; fi
  case "$url" in
    http://127.0.0.1:*|http://localhost:*|http://\[::1\]:*)
      say '    本机模式  : 未开隧道, 跳过外网自检' "$C_GRAY"
      return 0
      ;;
  esac
  edge="$(test_edge "$url" "$tok")"
  if [ -n "$edge" ]; then say "    外网通路  : 通 (经 Cloudflare 边缘 $edge)" "$C_GREEN"
  else say '    外网通路  : 隧道还在连, 过 10 秒在启动器里重新检测看看' "$C_YELLOW"; fi
  if [ "$fresh" = "1" ]; then
    say '    域名解析  : 新地址刚开始生效 (约 1 分钟)' "$C_YELLOW"
    say '               现在打不开就等 1 分钟再刷新, 别连续点或反复重试 ——' "$C_GRAY"
    say '               每次失败都会让“解析不到”再被缓存一分钟。' "$C_GRAY"
  else
    test_public_dns "$url"
  fi
}

show_links() {
  local url="$1" tok="$2" vtok="$3" p="$4" ton="$5" full
  blank
  if [ -n "$url" ]; then
    full="$url/?k=$tok"
    say '  ------------------------------------------------------------------' "$C_GREEN"
    say '   手机打开这个链接 (或扫下面二维码):' "$C_WHITE"
    blank
    say "   $full" "$C_CYAN"
    blank
    say '  ------------------------------------------------------------------' "$C_GREEN"
    blank
    show_qr "$full"
    if [ -n "$vtok" ]; then
      say '   只读观赏链接 (给别人看, 只能看不能改):' "$C_GRAY"
      say "   $url/?k=$vtok" "$C_GRAY"
      blank
    fi
    if printf '%s' "$full" | pbcopy 2>/dev/null; then say '   (链接已复制到剪贴板)' "$C_GRAY"; fi
    say '   加主屏: Safari 分享 -> 添加到主屏幕; Chrome 菜单 -> 添加到主屏幕' "$C_GRAY"
  else
    say "   [本机] http://127.0.0.1:$p/?k=$tok" "$C_CYAN"
    if [ "$ton" = "1" ]; then
      say '   (隧道没起来: 检查网络, 或看 _run/cloudflared.log)' "$C_YELLOW"
    else
      say '   (本机模式: "tunnel": false -- 现在只有这台电脑能打开)' "$C_GRAY"
      say '   (想让同一个 Wi-Fi 的手机也能开: 把 an.config.json 的 "host" 改成 0.0.0.0 再启动)' "$C_GRAY"
    fi
  fi
  blank
}

# ---- 试用 -------------------------------------------------------------
TRIAL_SC="$ROOT/gateway/trial_core.py"
[ -f "$TRIAL_SC" ] || TRIAL_SC="$ROOT/gateway/trial_core.pyc"
GUARD_SC="$ROOT/gateway/guard.py"
[ -f "$GUARD_SC" ] || GUARD_SC="$ROOT/gateway/guard.pyc"
trial_run() { [ -n "$PY_BIN" ] && [ -f "$TRIAL_SC" ] && "$PY_BIN" -X utf8 "$TRIAL_SC" "$@" 2>/dev/null; }
tfield() {
  "$PY_BIN" -c 'import json,sys
try: d=json.loads(sys.stdin.read().strip() or "{}")
except Exception: d={}
v=d.get(sys.argv[1])
print("" if v is None else v)' "$1" 2>/dev/null
}
fmt_remain() {
  local s="$1" h m
  s="${s%.*}"
  case "$s" in ''|*[!0-9]*) s=0 ;; esac
  h=$(( s / 3600 )); m=$(( (s % 3600) / 60 ))
  if [ "$h" -gt 0 ]; then printf '%d:%02d:%02d' "$h" "$m" "$(( s % 60 ))"
  else printf '%d:%02d' "$m" "$(( s % 60 ))"; fi
}
fmt_cn() {
  local s="$1"
  s="${s%.*}"
  case "$s" in ''|*[!0-9]*) s=0 ;; esac
  if [ "$s" -ge 3600 ]; then printf '%d 小时 %d 分' "$(( s / 3600 ))" "$(( (s % 3600) / 60 ))"
  elif [ "$s" -ge 60 ]; then printf '%d 分 %02d 秒' "$(( s / 60 ))" "$(( s % 60 ))"
  else printf '%d 秒' "$s"; fi
}
detect_engines() {
  local names=""
  if command -v codex >/dev/null 2>&1 || [ -x "$HOME/.codex/bin/codex" ] \
     || ls -d "$HOME"/.codex/* >/dev/null 2>&1; then names="Codex"; fi
  if command -v claude >/dev/null 2>&1; then
    if [ -n "$names" ]; then names="$names + Claude Code"; else names="Claude Code"; fi
  fi
  if command -v codebuddy >/dev/null 2>&1 || command -v workbuddy >/dev/null 2>&1 \
     || [ -x "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy" ] \
     || [ -x "$HOME/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy" ] \
     || ls -d "$HOME"/.workbuddy "$HOME"/.codebuddy >/dev/null 2>&1; then
    if [ -n "$names" ]; then names="$names + WorkBuddy"; else names="WorkBuddy"; fi
  fi
  if [ -n "$names" ]; then say "$names" "$C_GREEN"
  else say '未找到 Codex / Claude Code / WorkBuddy (装上其中一个并登录后就能用)' "$C_YELLOW"; fi
}

banner() {
  local line='  +--------------------------------------------------------------+'
  local VERSION="1.8.0"
  [ -f "$ROOT/VERSION" ] && VERSION="$(tr -d ' \r\n' < "$ROOT/VERSION")"
  blank
  say "$line" "$C_CYAN"
  printf '  |%-62s|\n' '      A G E N T   N E X U S   -   remote gateway'
  printf '  |%-62s|\n' "      v$VERSION   your PC, on your phone"
  say "$line" "$C_CYAN"
  blank
}

cmd_start() {
  local nport i ok tok vtok url fresh ton
  nport="$port"
  [ "$PORT_ARG" -gt 0 ] 2>/dev/null && nport="$PORT_ARG"
  if [ -n "$(gw_pids)" ] && port_open "$nport"; then
    say "[=] 网关已在运行 (pid $(gw_pids | join_pids)  端口 $nport)" "$C_GREEN"
  else
    if port_open "$nport"; then
      while port_open "$nport" && [ "$nport" -lt "$(( port + 20 ))" ]; do nport=$(( nport + 1 )); done
      say "[!] 端口 $port 被别的程序占用, 改用 $nport" "$C_YELLOW"
      port="$nport"; save_cfg_port "$nport"
    fi
    if [ -z "$PY_BIN" ]; then
      say '[x] 没找到 Python 运行时 (runtime/ 丢了?), 也没找到系统 python3。' "$C_RED"
      say '    重新解压整个包; 或自己装一个 python3 再试。' "$C_YELLOW"
      return 0
    fi
    if [ "$ROOT_WRITABLE" != "1" ]; then
      say '[x] 这个文件夹不可写: 网关要在这里写日志和访问 token。' "$C_RED"
      say '    换个位置解压 - 放「文档」或「桌面」, 别放进只读目录。' "$C_YELLOW"
      return 0
    fi
    say "[*] 启动网关 (端口 $nport) ..." "$C_GRAY"
    ensure_fs_roots
    start_gateway "$nport" "$host"
    ok=0
    for i in $(seq 1 40); do
      sleep 0.5
      if port_open "$nport"; then ok=1; break; fi
    done
    if [ "$ok" != "1" ]; then
      say '[x] 网关没起来. 日志尾部:' "$C_RED"
      local f
      for f in gateway.log gateway.err.log; do
        if [ -f "$RUN_DIR/$f" ]; then say "--- $f ---" "$C_GRAY"; tail -n 12 "$RUN_DIR/$f"; fi
      done
      return 0
    fi
    say '[=] 网关已就绪' "$C_GREEN"
  fi

  tok=""; vtok=""
  for i in $(seq 1 20); do
    if [ -f "$ROOT/gateway/.secrets/token.txt" ]; then tok="$(tr -d ' \r\n' < "$ROOT/gateway/.secrets/token.txt")"; fi
    [ -n "$tok" ] && break
    sleep 0.4
  done
  if [ -f "$ROOT/gateway/.secrets/view_token.txt" ]; then
    vtok="$(tr -d ' \r\n' < "$ROOT/gateway/.secrets/view_token.txt")"
  fi

  printf '[*] 检测引擎: '
  detect_engines
  if [ "$mode" = "active" ]; then
    say "[*] 内测试用: 剩余 $(fmt_remain "$(printf '%s' "$tj" | tfield remain)")  (编号 #$tid)" "$C_CYAN"
  fi

  url=""; fresh="0"
  if [ "$tunnel" = "true" ] && [ "$NOTUNNEL" != "1" ]; then
    if [ -n "$(cf_pids)" ]; then
      say '[=] 隧道已在运行' "$C_GREEN"
      url="$(tunnel_url)"
    else
      if [ -z "$CF_BIN" ]; then
        say '[!] 缺少 tools/cloudflared-* , 跳过外网隧道 (只在本机可用)' "$C_YELLOW"
      else
        say '[*] 打外网隧道 (cloudflared quick tunnel, 约 5-20 秒) ...' "$C_GRAY"
        url="$(start_tunnel "$nport" "$protocol")"
        if [ -z "$url" ]; then say '[!] 隧道没拿到公网地址, 看 _run/cloudflared.log' "$C_YELLOW"
        else fresh="1"; fi
      fi
    fi
  fi

  if [ -n "$url" ]; then
    printf '%s' "$url" > "$RUN_DIR/TUNNEL-URL.txt"
    printf 'FULL : %s/?k=%s\nVIEW : %s/?k=%s\n' "$url" "$tok" "$url" "$vtok" > "$RUN_DIR/LINKS.txt"
  fi
  ton=0; { [ "$tunnel" = "true" ] && [ "$NOTUNNEL" != "1" ]; } && ton=1
  show_links "$url" "$tok" "$vtok" "$nport" "$ton"
  [ -n "$url" ] && self_check "$nport" "$tok" "$url" "$fresh"
  if [ -n "$url" ] && [ "$autoOpenBrowser" = "true" ]; then open "$url/?k=$tok" 2>/dev/null || true; fi
  if [ -n "$url" ]; then
    say '  提醒: 这个地址是免费临时隧道, 重启网关上会换新地址; 想要固定地址见 README。' "$C_GRAY"
    say '  关闭本窗口不会停服务 (网关在后台跑); 要停服务请运行 tools/an.sh stop。' "$C_GRAY"
  fi
  blank
}

cmd_stop() {
  local n=0 p
  for p in $(cf_pids); do kill -9 "$p" 2>/dev/null && n=$(( n + 1 )); done
  for p in $(gw_pids); do
    pkill -9 -P "$p" 2>/dev/null || true
    kill -9 "$p" 2>/dev/null && n=$(( n + 1 ))
  done
  say "[=] 已停止 $n 个进程 (网关 + 隧道)" "$C_GREEN"
}

cmd_status() {
  local sp u tr gs
  if [ -n "$(gw_pids)" ]; then say "网关        : 运行中  pid $(gw_pids | join_pids)" "$C_GREEN"
  else say '网关        : 已停止' "$C_RED"; fi
  sp="$port"; [ "$PORT_ARG" -gt 0 ] 2>/dev/null && sp="$PORT_ARG"
  if port_open "$sp"; then say "端口 $sp    : 监听中" "$C_GREEN"
  else say "端口 $sp    : 未监听" "$C_YELLOW"; fi
  if [ -n "$(cf_pids)" ]; then say "隧道        : 运行中  pid $(cf_pids | join_pids)" "$C_GREEN"
  else say '隧道        : 已停止' "$C_YELLOW"; fi
  u="$(tunnel_url)"
  if [ -n "$u" ]; then say "当前地址    : $u" "$C_CYAN" else say '当前地址    : (无, 还没建过隧道)' "$C_GRAY"; fi
  if command -v codex >/dev/null 2>&1; then say "Codex       : $(command -v codex)" "$C_GRAY"
  else say 'Codex       : 未找到' "$C_GRAY"; fi
  if command -v claude >/dev/null 2>&1; then say "Claude Code : $(command -v claude)" "$C_GRAY"
  else say 'Claude Code : 未找到' "$C_GRAY"; fi
  say '本机模式    : 不限时，无设备绑定和反爬锁定' "$C_CYAN"
  [ -f "$RUN_DIR/gateway.log" ] && say "网关日志    : $RUN_DIR/gateway.log" "$C_GRAY"
}

cmd_link() {
  local tok vtok
  tok=""; vtok=""
  [ -f "$ROOT/gateway/.secrets/token.txt" ] && tok="$(tr -d ' \r\n' < "$ROOT/gateway/.secrets/token.txt")"
  [ -f "$ROOT/gateway/.secrets/view_token.txt" ] && vtok="$(tr -d ' \r\n' < "$ROOT/gateway/.secrets/view_token.txt")"
  local lp="$port"; [ "$PORT_ARG" -gt 0 ] 2>/dev/null && lp="$PORT_ARG"
  local ton=0; [ "$tunnel" = "true" ] && ton=1
  show_links "$(tunnel_url)" "$tok" "$vtok" "$lp" "$ton"
}

cmd_renew() {
  say '本版本不限时，无需续期或解锁。' "$C_CYAN"
}

banner
load_cfg
case "$ACTION" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; sleep 2; cmd_start ;;
  status)  cmd_status ;;
  link)    cmd_link ;;
  renew)   cmd_renew ;;
esac

if [ "$NOPAUSE" != "1" ] && [ -t 0 ]; then
  blank
  printf '%s按回车键关闭本窗口 (服务继续在后台运行) ...%s' "$C_GRAY" "$C_OFF"
  IFS= read -r _dummy || true
fi
exit 0
