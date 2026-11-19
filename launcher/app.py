# -*- coding: utf-8 -*-
"""Agent Nexus desktop host.

The window is only a control surface: the actual gateway, tunnel and AI CLIs
keep running on this computer.  Everything the browser can call is exposed
through the small Api class at the bottom of this file.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "Agent Nexus"
FALLBACK_VERSION = "1.8.0"
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT / "_internal"))
else:
    ROOT = Path(__file__).resolve().parent.parent
    RESOURCE_ROOT = Path(__file__).resolve().parent

RUN_DIR = ROOT / "_run"
LOG_FILE = RUN_DIR / "launcher.log"
WEB_DIR = RESOURCE_ROOT / "launcher" / "web"
if not WEB_DIR.exists():
    WEB_DIR = ROOT / "launcher" / "web"
ICON_FILE = RESOURCE_ROOT / "launcher" / "assets" / "icon.ico"
if not ICON_FILE.exists():
    ICON_FILE = ROOT / "launcher" / "assets" / "icon.ico"

_LOG_LOCK = threading.Lock()
_MUTEX_HANDLE = None


def log(message: str) -> None:
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + str(message) + "\n"
        with _LOG_LOCK:
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass


def _version() -> str:
    for candidate in (ROOT / "VERSION", ROOT / "deploy" / "VERSION"):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            pass
    return FALLBACK_VERSION


def _single_instance_guard() -> bool:
    """Keep two launchers from racing on the same trial file.

    A user who double-clicks because the first window is still painting used
    to get two processes writing trial.json at once.  The second one could
    read a half-written record and show a bogus "expired" page.  The mutex is
    scoped to this extracted folder, so separate copies still work.
    """
    global _MUTEX_HANDLE
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        key = hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:16]
        # windll + GetLastError() is unreliable: ctypes may clear the thread's
        # last-error before we read it, so the mutex silently looked new and a
        # second double-click opened a second WebView2 on the same storage.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.CreateMutexW(None, False, "Local\\AgentNexusLauncher-" + key)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
    except Exception as exc:
        log("single-instance guard skipped: %r" % (exc,))
    return True


def _focus_existing_window(title: str = APP_NAME) -> bool:
    """Second double-click should raise the first window, not scare the user."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            found = []
            enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def _visit(handle, _lparam):
                length = user32.GetWindowTextLengthW(handle)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(handle, buf, length + 1)
                    if title.lower() in buf.value.lower() and user32.IsWindowVisible(handle):
                        found.append(handle)
                        return False
                return True

            user32.EnumWindows(enum_proc(_visit), 0)
            hwnd = found[0] if found else 0
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception as exc:
        log("focus existing window failed: %r" % (exc,))
    return False


def _ps_json(script: str, timeout: float = 15.0):
    if not IS_WINDOWS:
        return None
    prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", prefix + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
        raw = (completed.stdout or "").strip()
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        log("powershell failed: %r" % (exc,))
        return None


def _norm_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class Api:
    def __init__(self, activate_trial: bool = True) -> None:
        self.activate_trial = bool(activate_trial)
        self.window = None
        self.version = _version()
        self.port = 8790
        self._start_lock = threading.Lock()
        self._proc_cache = (0.0, [])
        self._qr_cache = {}
        self._network_cache = (0.0, {})
        self._last_link = ""
        self._last_error = ""
        self._config_error = ""
        self.ui_ready_at = 0.0
        self._load_backend()

    # ---------------------------------------------------------------- backend
    def _load_backend(self) -> None:
        gateway = ROOT / "gateway"
        if str(gateway) not in sys.path:
            sys.path.insert(0, str(gateway))
        import importlib
        self.agent_config = importlib.import_module("agent_config")
        self.trial_core = importlib.import_module("trial_core")
        self.trial = self.trial_core._Trial()
        self.guard_module = None
        log("backend loaded from %s (version %s, scope %s)" % (
            gateway, self.version, getattr(self.trial_core, "BUILD_SCOPE", "?")))

    def _guard(self):
        if self.guard_module is None:
            import importlib
            self.guard_module = importlib.import_module("guard")
        return self.guard_module

    def _an_script(self) -> Path:
        direct = ROOT / "tools" / "an.ps1"
        if direct.exists():
            return direct
        nested = ROOT / "deploy" / "tools" / "an.ps1"
        if nested.exists():
            return nested
        return direct

    def _script_root(self) -> Path:
        return self._an_script().parent.parent

    def _run_dir(self) -> Path:
        return self._script_root() / "_run"

    def _config_data(self) -> dict:
        cfg = ROOT / "an.config.json"
        if not cfg.exists():
            cfg = ROOT / "deploy" / "an.config.json"
        try:
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self._config_error = "an.config.json 读取失败: %s" % exc
            log(self._config_error)
            return {}

    def _config_port(self) -> int:
        obj = self._config_data()
        try:
            port = int(obj.get("port") or 8790)
            if 1 <= port <= 65535:
                self.port = port
                self._config_error = ""
            else:
                self._config_error = "an.config.json 端口超出 1-65535: %s" % port
                log(self._config_error)
        except (TypeError, ValueError):
            self._config_error = "an.config.json 的 port 必须是数字"
            log(self._config_error)
        return self.port

    def _config_tunnel(self) -> bool:
        return bool(self._config_data().get("tunnel", True))

    def _network_status(self, force: bool = False) -> dict:
        now = time.time()
        if not force and now - self._network_cache[0] < 5.0:
            return dict(self._network_cache[1])
        cfg = self._config_data()
        mode = str(cfg.get("networkMode") or "standard").strip().lower()
        rows = []
        if IS_WINDOWS:
            script = r'''
$items = @(Get-NetIPConfiguration -ErrorAction SilentlyContinue |
  Where-Object { $_.NetAdapter.Status -eq 'Up' -and $_.IPv4DefaultGateway } |
  ForEach-Object {
    $ip = @($_.IPv4Address | ForEach-Object { $_.IPAddress } |
      Where-Object { $_ -and $_ -notmatch '^169\.254\.' }) | Select-Object -First 1
    $dns = @($_.DNSServer.ServerAddresses | Where-Object { $_ })
    [pscustomobject]@{
      alias = [string]$_.InterfaceAlias
      ip = [string]$ip
      prefix = [int](@($_.IPv4Address | Where-Object { $_.IPAddress -eq $ip })[0].PrefixLength)
      gateway = [string]$_.IPv4DefaultGateway.NextHop
      dns = @($dns)
    }
  })
if ($items.Count -eq 0) { '[]' } else { $items | ConvertTo-Json -Compress }
'''
            rows = _norm_list(_ps_json(script, timeout=8.0))
        else:
            try:
                route = subprocess.run(
                    ["sh", "-c", "route -n get default 2>/dev/null"],
                    capture_output=True, text=True, timeout=3,
                ).stdout
                gateway = ""
                interface = ""
                for line in route.splitlines():
                    key, _, value = line.strip().partition(":")
                    if key == "gateway":
                        gateway = value.strip()
                    elif key == "interface":
                        interface = value.strip()
                ip = ""
                if interface:
                    ip = subprocess.run(
                        ["ipconfig", "getifaddr", interface],
                        capture_output=True, text=True, timeout=3,
                    ).stdout.strip()
                rows = [{"alias": interface, "ip": ip, "gateway": gateway, "dns": []}]
            except Exception:
                rows = []
        row = next((item for item in rows if item.get("ip")), rows[0] if rows else {})
        proxy = self._proxy_check()
        enterprise = mode in {"enterprise", "enterprise_lan", "lan"}
        info = {
            "mode": mode,
            "enterprise": enterprise,
            "bindHost": str(cfg.get("host") or "127.0.0.1"),
            "port": self._config_port(),
            "interface": str(row.get("alias") or ""),
            "lanIp": str(row.get("ip") or ""),
            "prefix": row.get("prefix"),
            "gateway": str(row.get("gateway") or ""),
            "dns": [str(value) for value in (row.get("dns") or []) if value],
            "proxy": proxy,
            "tunnelEnabled": self._config_tunnel(),
            "message": ("同一局域网设备可访问；程序不会修改代理、DNS、路由或防火墙"
                        if enterprise else "按当前配置使用本机或公网通道"),
        }
        self._network_cache = (now, info)
        return dict(info)

    def _port_open(self, port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.55)
        try:
            return sock.connect_ex(("127.0.0.1", int(port))) == 0
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _processes(self, force: bool = False):
        now = time.time()
        if not force and now - self._proc_cache[0] < 2.5:
            return self._proc_cache[1]
        script = (
            "$r=$env:AN_ROOT; "
            "$p=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or Name='cloudflared.exe'\" "
            "| Where-Object { $_.CommandLine -and ($_.CommandLine.IndexOf($r, [StringComparison]::OrdinalIgnoreCase) -ge 0) } "
            "| ForEach-Object { [pscustomobject]@{ name=$_.Name; pid=$_.ProcessId; cmd=$_.CommandLine } }); "
            "if($p.Count -eq 0){'[]'}else{$p|ConvertTo-Json -Compress}"
        )
        env = dict(os.environ)
        env["AN_ROOT"] = str(ROOT)
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=12, creationflags=CREATE_NO_WINDOW, env=env,
            )
            rows = json.loads((completed.stdout or "[]").strip() or "[]")
        except Exception as exc:
            log("process scan failed: %r" % (exc,))
            rows = []
        rows = _norm_list(rows)
        self._proc_cache = (now, rows)
        return rows

    def _stack_state(self, with_qr: bool = False):
        port = self._config_port()
        tunnel_enabled = self._config_tunnel()
        rows = self._processes()
        gateway = any("remote_server.py" in str(r.get("cmd") or "") for r in rows)
        tunnel = any(str(r.get("name") or "").lower() == "cloudflared.exe" or
                     "cloudflared" in str(r.get("cmd") or "").lower() for r in rows)
        listening = self._port_open(port)
        ready = gateway and listening and (tunnel or not tunnel_enabled)
        link = self._read_link() if ready else ""
        qr = self._make_qr(link) if (with_qr and link) else ""
        return {
            "running": bool(link and ready),
            "link": link,
            "qr": qr,
            "port": port,
            "gateway": bool(gateway and listening),
            "tunnel": bool(tunnel and link),
            "local": not tunnel_enabled,
        }

    def _read_link(self) -> str:
        path = self._run_dir() / "LINKS.txt"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        match = re.search(r"^\s*FULL\s*:\s*(https?://\S+)", text, re.MULTILINE)
        if not match:
            return ""
        link = match.group(1).strip()
        try:
            host = (urllib.parse.urlsplit(link).hostname or "").lower()
        except Exception:
            return ""
        if not host:
            return ""
        if host != "localhost":
            accepted = host.endswith(".trycloudflare.com")
            try:
                ip = ipaddress.ip_address(host)
                accepted = accepted or ip.is_loopback or ip.is_private
            except ValueError:
                pass
            if not accepted:
                return ""
        self._last_link = link
        return link

    def _make_qr(self, link: str) -> str:
        if not link:
            return ""
        if link in self._qr_cache:
            return self._qr_cache[link]
        try:
            import qrcode
            from qrcode.constants import ERROR_CORRECT_H
            qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H,
                               box_size=10, border=3)
            qr.add_data(link)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#11131b", back_color="white").convert("RGBA")
            try:
                from PIL import Image, ImageDraw
                size = img.size[0]
                badge_size = int(size * 0.16)
                pad = int(size * 0.025)
                icon = Image.open(ICON_FILE).convert("RGBA")
                badge = Image.new("RGBA", (badge_size + pad * 2, badge_size + pad * 2), (0, 0, 0, 0))
                draw = ImageDraw.Draw(badge)
                draw.rounded_rectangle(
                    [0, 0, badge.width - 1, badge.height - 1],
                    radius=int(badge_size * 0.24), fill="white",
                )
                icon = icon.resize((badge_size, badge_size), Image.LANCZOS)
                badge.alpha_composite(icon, (pad, pad))
                img.alpha_composite(badge, ((size - badge.width) // 2,
                                            (size - badge.height) // 2))
            except Exception as exc:
                log("QR logo skipped: %r" % (exc,))
            buffer = io.BytesIO()
            img.save(buffer, format="PNG", optimize=True)
            data = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            self._qr_cache[link] = data
            return data
        except Exception as exc:
            log("QR generation failed: %r" % (exc,))
            return ""

    def _root_writable(self) -> bool:
        probe = self._run_dir() / (".write-" + str(os.getpid()))
        try:
            self._run_dir().mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except Exception:
            return False

    def _trial_payload(self) -> dict:
        try:
            st = self.trial.info() or {}
        except Exception as exc:
            log("trial info failed: %r" % (exc,))
            st = {"mode": "pending", "id": "", "remain": None, "until": 0}
        mode = str(st.get("mode") or "pending")
        if mode == "off":
            operator = (ROOT / "gateway" / ".secrets" / "operator.flag").exists()
            mode = "unlimited" if operator else "pending"
        hours = float(st.get("totalHours") or st.get("hours") or 1.0)
        if mode == "unlimited":
            hours = 0.0
        until = float(st.get("until") or 0)
        remain = st.get("remain")
        if remain is None and mode == "active":
            remain = max(0.0, until - time.time())
        return {
            "mode": mode,
            "id": str(st.get("id") or ""),
            "until": until,
            "remain": remain,
            "totalHours": hours,
            "total": int(hours * 3600),
            "destructRemain": st.get("destructRemain"),
            "destructAt": st.get("destructAt"),
            "graceSeconds": st.get("graceSeconds"),
            "selfDestructEnabled": False,
            "dataDeletionEnabled": False,
        }

    def _arm_trial_clock(self) -> None:
        return None

    def _activate_trial_after_start(self, reason: str) -> dict:
        """Start the one-hour clock only after a usable link exists."""
        if self.activate_trial:
            try:
                self.trial.activate(1.0)
            except Exception as exc:
                log("trial activate (%s) failed: %r" % (reason, exc))
        self._arm_trial_clock()
        return self._trial_payload()

    # ---------------------------------------------------------------- discovery
    def _find_codex(self) -> str:
        homes = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            homes.append(Path(local) / "OpenAI" / "Codex" / "bin")
        home = self.agent_config.user_home()
        homes.extend([
            home / "AppData" / "Local" / "OpenAI" / "Codex" / "bin",
            home / "Library" / "Application Support" / "OpenAI" / "Codex" / "bin",
        ])
        for base in homes:
            try:
                found = sorted(list(base.glob("*/codex.exe")) + list(base.glob("*/codex")),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                if found:
                    return str(found[0])
            except Exception:
                pass
        return shutil.which("codex") or ""

    def _find_claude(self) -> str:
        dirs = []
        if os.environ.get("CLAUDE_BIN_DIRS"):
            dirs.extend([p for p in re.split(r"[;:]", os.environ["CLAUDE_BIN_DIRS"]) if p])
        home = Path.home()
        dirs.extend([
            str(home / "AppData" / "Roaming" / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"),
            str(home / "AppData" / "Local" / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"),
            str(home / ".claude" / "local"),
            str(home / ".local" / "bin"),
            str(home / "Library" / "Application Support" / "Claude" / "bin"),
        ])
        for folder in dirs:
            for name in ("claude.exe", "claude.cmd", "claude"):
                candidate = Path(folder) / name
                try:
                    if candidate.is_file():
                        return str(candidate)
                except OSError:
                    pass
        return shutil.which("claude") or ""

    def _find_workbuddy(self) -> str:
        """Find the WorkBuddy CLI without starting the Electron app."""
        roots = []
        for key in ("WORKBUDDY_CLI_DIR", "WORKBUDDY_HOME"):
            raw = (os.environ.get(key) or "").strip().strip('"')
            if raw:
                roots.append(Path(raw))
                try:
                    roots.extend(list(Path(raw).parents)[:3])
                except Exception:
                    pass
        home = self.agent_config.user_home()
        local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        prog = Path(os.environ.get("ProgramFiles") or "C:/Program Files")
        prog86 = Path(os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)")
        roots.extend([

            local / "Programs" / "WorkBuddy", local / "WorkBuddy",
            prog / "WorkBuddy", prog86 / "WorkBuddy",
            home / ".workbuddy", home / "Library" / "Application Support" / "WorkBuddy",
            Path("/Applications/WorkBuddy.app/Contents"),
            home / "Applications" / "WorkBuddy.app" / "Contents",
        ])
        seen = set()
        for root in roots:
            try:
                key = str(root.resolve()).lower()
            except Exception:
                key = str(root).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            for cli in (root / "resources" / "app.asar.unpacked" / "cli",
                        root / "resources" / "app" / "out" / "cli",
                        root / "cli", root):
                candidate = cli / "bin" / "codebuddy"
                try:
                    if candidate.is_file():
                        return str(candidate)
                except OSError:
                    pass
        return shutil.which("codebuddy") or shutil.which("workbuddy") or ""

    def _codex_config(self) -> dict:
        home = Path(os.environ.get("CODEX_HOME") or (self.agent_config.user_home() / ".codex"))
        path = home / "config.toml"
        config = self.agent_config.codex_config(path)
        return {"path": str(path), "model": config["model"], "provider": config["provider"],
                "reasoning": config["effort"]}

    def _claude_config(self) -> dict:
        path = self.agent_config.user_home() / ".claude" / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            env = data.get("env") if isinstance(data, dict) else {}
            if not isinstance(env, dict):
                env = {}
            model = str(data.get("model") or env.get("ANTHROPIC_MODEL") or "")
            base = str(env.get("ANTHROPIC_BASE_URL") or "")
            effort = ""
            settings = data.get("modelSettings")
            if isinstance(settings, dict):
                for value in settings.values():
                    if isinstance(value, dict) and value.get("effortLevel"):
                        effort = str(value.get("effortLevel"))
                        break
            return {"path": str(path), "model": model, "baseUrl": base, "effort": effort}
        except Exception:
            return {"path": str(path), "model": "", "baseUrl": "", "effort": ""}

    def _workbuddy_config(self) -> dict:
        config = self.agent_config.workbuddy_config()
        return {"path": config["defaultSource"] or config["configRoot"],
                "model": config["defaultModel"], "effort": config["defaultEffort"],
                "provider": "本机配置", "baseUrl": ""}

    @staticmethod
    def _url_host(url: str) -> str:
        try:
            parts = urllib.parse.urlsplit(str(url or ""))
            return parts.hostname or ""
        except Exception:
            return ""

    def _proxy_check(self) -> dict:
        return self.agent_config.proxy_diagnostic()

    def _find_package_file(self, rel: str):
        for base in (ROOT, RESOURCE_ROOT):
            try:
                candidate = base / rel
                if candidate.is_file():
                    return candidate
            except Exception:
                pass
        return None

    @staticmethod
    def _manifest_seal(data: dict) -> str:
        clone = dict(data)
        clone.pop("sealSha256", None)
        raw = json.dumps(clone, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest().upper()

    def _integrity_check(self) -> dict:
        return {"state": "ok", "sub": "自动读取本机配置与模型 · 无授权或设备绑定限制",
                "badge": "本机模式", "present": False}

    def _version_of(self, executable: str) -> str:
        try:
            completed = subprocess.run(
                [executable, "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=7,
                creationflags=CREATE_NO_WINDOW,
            )
            text = (completed.stdout or completed.stderr or "").strip().splitlines()
            return text[0].strip() if text else ""
        except Exception:
            return ""

    def run_checks(self):
        port = self._config_port()
        stack = self._stack_state()
        rows = []

        runtime_ok = (ROOT / "runtime" / "python" / "python.exe").exists()
        writable = self._root_writable()
        rows.append({
            "id": "env", "state": "ok" if writable else "fail",
            "sub": "%s %s · 内置运行时 %s · 目录%s" % (
                platform.system(), platform.release(),
                "3.11" if runtime_ok else "系统 Python",
                "可写" if writable else "不可写",
            ),
            "badge": "通过" if writable else "不可写",
        })

        codex = self._find_codex()
        cfg = self._codex_config()
        if codex:
            bits = []
            version = self._version_of(codex)
            if version:
                bits.append(version)
            if cfg.get("model"):
                bits.append("模型 " + cfg["model"])
            if cfg.get("provider"):
                bits.append(cfg["provider"])
            if cfg.get("reasoning"):
                bits.append("思考 " + cfg["reasoning"])
            rows.append({
                "id": "codex", "state": "ok",
                "sub": " · ".join(bits) if bits else "已找到 Codex",
                "badge": "已就绪" if cfg.get("model") else "已安装",
            })
        else:
            rows.append({"id": "codex", "state": "warn",
                         "sub": "未检测到 codex 命令 · 可跳过", "badge": "可跳过"})

        claude = self._find_claude()
        ccfg = self._claude_config()
        if claude:
            bits = []
            version = self._version_of(claude)
            if version:
                bits.append(version)
            if ccfg.get("model"):
                bits.append("模型 " + ccfg["model"])
            if ccfg.get("effort"):
                bits.append("思考 " + ccfg["effort"])
            host = self._url_host(ccfg.get("baseUrl"))
            if host:
                bits.append(host)
            rows.append({
                "id": "claude", "state": "ok",
                "sub": " · ".join(bits) if bits else "已找到 Claude Code",
                "badge": "已就绪" if ccfg.get("model") else "已安装",
            })
        else:
            rows.append({"id": "claude", "state": "warn",
                         "sub": "未检测到 claude 命令 · 可跳过", "badge": "可跳过"})

        workbuddy = self._find_workbuddy()
        wcfg = self._workbuddy_config()
        if workbuddy:
            bits = [Path(workbuddy).name]
            if wcfg.get("model"):
                bits.append("模型 " + wcfg["model"])
            else:
                bits.append("跟随 WorkBuddy 客户端默认模型")
            if wcfg.get("effort"):
                bits.append("思考 " + wcfg["effort"])
            if wcfg.get("provider"):
                bits.append(wcfg["provider"])
            host = self._url_host(wcfg.get("baseUrl"))
            if host:
                bits.append(host)
            rows.append({
                "id": "workbuddy", "state": "ok",
                "sub": " · ".join(bits), "badge": "已就绪" if wcfg.get("model") else "已安装",
            })
        else:
            rows.append({"id": "workbuddy", "state": "warn",
                         "sub": "未检测到 WorkBuddy CLI · 可跳过", "badge": "可跳过"})

        proxy = self._proxy_check()
        rows.append({"id": "proxy", "state": proxy["state"], "sub": proxy["sub"],
                     "badge": proxy["badge"]})

        network = self._network_status()
        network_state = "ok" if network.get("lanIp") and network.get("gateway") else "warn"
        network_bits = []
        if network.get("lanIp"):
            network_bits.append(network["lanIp"])
        if network.get("gateway"):
            network_bits.append("网关 " + network["gateway"])
        if network.get("interface"):
            network_bits.append(network["interface"])
        rows.append({
            "id": "network", "state": network_state,
            "sub": " · ".join(network_bits) or "未取得默认网关；不修改系统网络",
            "badge": "局域网" if network.get("enterprise") else "已读取",
        })

        integrity = self._integrity_check()
        rows.append({"id": "parity", "state": integrity["state"],
                     "sub": integrity["sub"], "badge": integrity["badge"]})

        rows.append({
            "id": "gateway", "state": "ok" if stack["gateway"] else "warn",
            "sub": "%s:%d · %s" % (network.get("bindHost") or "127.0.0.1", port,
                                    "已监听" if stack["gateway"] else "未启动"),
            "badge": "运行中" if stack["gateway"] else "未启动",
        })
        if stack.get("local"):
            rows.append({
                "id": "tunnel", "state": "ok",
                "sub": ("局域网模式 · 手机需同一企业网络 · %s" % network.get("lanIp"))
                        if network.get("enterprise") else "本机模式 · 仅这台电脑可打开",
                "badge": "局域网" if network.get("enterprise") else "本机模式",
            })
        else:
            rows.append({
                "id": "tunnel", "state": "ok" if stack["tunnel"] else "warn",
                "sub": "trycloudflare · %s" % ("已连接" if stack["tunnel"] else "待启动"),
                "badge": "已连接" if stack["tunnel"] else "待启动",
            })
        return rows

    # ---------------------------------------------------------------- public API
    def boot(self):
        self._config_port()
        trial = None
        try:
            self.trial.ensure(1.0)
            trial = self._trial_payload()
            self._arm_trial_clock()
            log("trial scope=%s mode=%s remain=%s destruct=%s" % (
                getattr(self.trial_core, "BUILD_SCOPE", "?"),
                trial.get("mode"), trial.get("remain"), trial.get("destructRemain")))
        except Exception as exc:
            log("trial boot failed: %r" % (exc,))
            trial = {"mode": "unlimited", "remain": None,
                     "selfDestructEnabled": False, "dataDeletionEnabled": False}
        stack = self._stack_state(with_qr=True)
        network = self._network_status(force=True)
        return {
            "version": self.version,
            "osName": "Windows" if IS_WINDOWS else platform.system(),
            "trial": trial,
            "running": stack["running"],
            "link": stack["link"],
            "qr": stack["qr"],
            "port": stack["port"],
            "lastError": self._last_error,
            "configError": self._config_error,
            "network": network,
        }

    def trial_status(self):
        self._config_port()
        return self._trial_payload()

    def js_log(self, message=""):
        log("js " + str(message)[:1200])
        return True

    def ui_ready(self):
        """Page heartbeat: proves WebView2 actually rendered app.js.

        A window can exist while WebView2 shows a blank page (company policy,
        broken runtime, GPU compositing).  The launcher uses this timestamp to
        fall back to the default browser instead of leaving the user on white.
        """
        self.ui_ready_at = time.time()
        return {"ok": True}

    def start_stack(self):
        if not self._start_lock.acquire(blocking=False):
            # 结构化 busy 让前端继续等，而不是把"后台还在启动"渲染成启动失败。
            return {"ok": False, "busy": True,
                    "error": "启动正在进行中，请稍候（首次启动约 40–90 秒）"}
        try:
            deploy = self._script_root()
            script = self._an_script()
            if not script.exists():
                return {"ok": False, "error": "缺少 tools\\an.ps1，包不完整"}

            # 已经在线时再次点“启动”只返回现有二维码, 不能停旧隧道重开:
            # quick tunnel 一停, 手机上刚扫到的链接立刻失效, 用户会以为后端坏了。
            current = self._stack_state(with_qr=True)
            if current.get("running") and current.get("link"):
                self._last_error = ""
                trial = self._activate_trial_after_start("reuse")
                log("start_stack reused existing stack: %s" % current["link"])
                return {"ok": True, "link": current["link"], "qr": current.get("qr") or self._make_qr(current["link"]),
                        "port": current.get("port") or self._config_port(), "reused": True,
                        "local": current.get("local", False), "trial": trial}

            # 每次启动都先收干净，避免复用上一条临时隧道 / 旧二维码。
            if not self._stop_stack_impl():
                return {"ok": False,
                        "error": "旧网关/隧道没有停干净，已中止本次启动。请查看 _run\\gateway.err.log 或重启 AgentNexus 后再试。"}
            run_dir = self._run_dir()
            run_dir.mkdir(parents=True, exist_ok=True)
            for name in ("LINKS.txt", "TUNNEL-URL.txt", "cloudflared.log", "cloudflared.prev.log"):
                try:
                    (run_dir / name).unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
            self._qr_cache.clear()

            started = time.time()
            log("starting stack: %s" % script)
            # cloudflared and the gateway inherit this handle.  Capturing
            # through a pipe keeps it open after an.ps1 exits, so run() would
            # wait forever even though the stack is already online.  Redirect
            # to a file instead; an.ps1 can finish as soon as it has launched
            # its children.
            start_log = run_dir / "launcher-start.log"
            try:
                with start_log.open("w", encoding="utf-8", errors="replace") as stream:
                    subprocess.run(
                        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(script), "-Action", "start", "-NoPause"],
                        cwd=str(deploy), stdout=stream, stderr=subprocess.STDOUT,
                        text=True, timeout=90, creationflags=CREATE_NO_WINDOW,
                    )
            except subprocess.TimeoutExpired:
                log("start timed out")
            except Exception as exc:
                log("start command failed: %r" % (exc,))
            try:
                output = start_log.read_text(encoding="utf-8", errors="replace")
            except Exception:
                output = ""

            link = self._read_link()
            port = self._config_port()
            state = self._stack_state()
            if link and state["running"]:
                self._last_error = ""
                trial = self._activate_trial_after_start("start")
                log("stack ready; private link generated locally")
                return {"ok": True, "link": link, "qr": self._make_qr(link), "port": port,
                        "local": not self._config_tunnel(), "trial": trial}

            sections = []
            for name in ("launcher-start.log", "gateway.out.log", "gateway.err.log", "cloudflared.log"):
                path = run_dir / name
                try:
                    text = path.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        sections.append("--- %s ---\n%s" % (name, "\n".join(text.splitlines()[-12:])))
                except FileNotFoundError:
                    continue
                except Exception:
                    continue
            tail = "\n".join(sections) if sections else (output.strip() or "没有输出")
            self._last_error = tail
            hint = ("排查顺序: 1) 端口是否被占用 2) runtime\\python 是否完整 "
                    "3) gateway\\.secrets 是否可写 4) 是否需要公司代理放行")
            return {"ok": False, "error": "网关或隧道没有起来。\n" + hint + "\n" + tail}
        except Exception as exc:
            self._last_error = str(exc)
            log("start_stack failed:\n" + traceback.format_exc())
            return {"ok": False, "error": "启动失败: %s" % exc}
        finally:
            self._start_lock.release()

    def _stop_stack_impl(self):
        deploy = self._script_root()
        script = self._an_script()
        completed = None
        if script.exists():
            try:
                completed = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(script), "-Action", "stop", "-NoPause"],
                    cwd=str(deploy), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=45,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                log("stop failed: %r" % (exc,))
        self._proc_cache = (0.0, [])
        deadline = time.time() + 12.0
        while time.time() < deadline:
            # Only this package's own processes decide whether stop succeeded.
            # Do not use the port as a proxy: another program may own it, or our
            # gateway may still be alive while its socket has not bound yet.
            rows = self._processes(force=True)
            gateway_alive = any(
                "remote_server.py" in str(r.get("cmd") or "") for r in rows
            )
            tunnel_alive = any(
                str(r.get("name") or "").lower() == "cloudflared.exe"
                or "cloudflared" in str(r.get("cmd") or "").lower()
                for r in rows
            )
            if not gateway_alive and not tunnel_alive:
                if completed is not None and completed.returncode not in (0, None):
                    log("stop returned code %s but stack is clean" % completed.returncode)
                return True
            time.sleep(0.4)
        log("stop verification failed: gateway/tunnel or port still alive")
        return False

    def stop_stack(self):
        ok = self._stop_stack_impl()
        self._last_link = ""
        if not ok:
            return {"ok": False, "error": "停止未完成：仍有网关/隧道进程或端口占用。请稍后重试，或查看任务管理器。"}
        return {"ok": True}

    def renew(self, code):
        return {"ok": True, "disabled": True, "trial": self._trial_payload()}

    def copy_text(self, text):
        text = str(text or "")
        if not text:
            return {"ok": False}
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            root.destroy()
            return {"ok": True}
        except Exception:
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", "Set-Clipboard -Value $env:AN_CLIP"],
                    env=dict(os.environ, AN_CLIP=text), timeout=8,
                    creationflags=CREATE_NO_WINDOW,
                )
                return {"ok": True}
            except Exception:
                return {"ok": False}

    def open_external(self, link):
        link = str(link or "")
        if link.startswith(("http://", "https://")):
            webbrowser.open(link)
            return {"ok": True}
        return {"ok": False}

    def open_folder(self, which="root"):
        path = self._run_dir() if str(which) == "run" else ROOT
        try:
            path.mkdir(parents=True, exist_ok=True)
            if IS_WINDOWS:
                os.startfile(str(path))
            else:
                subprocess.Popen(["open", str(path)])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def minimize_app(self):
        try:
            self.window.minimize()
            return {"ok": True}
        except Exception:
            return {"ok": False}

    def quit_app(self):
        try:
            # Let the HTTP response leave the server before WebView2 tears the
            # page down.  Calling destroy() inline makes the request look like
            # a backend failure to the UI even though the command succeeded.
            threading.Timer(0.15, self.window.destroy).start()
            return {"ok": True}
        except Exception:
            threading.Timer(0.15, lambda: os._exit(0)).start()
            return {"ok": True}


_HTTP_API_METHODS = {
    "boot", "run_checks", "start_stack", "stop_stack", "trial_status",
    "renew", "copy_text", "open_external", "open_folder", "minimize_app",
    "quit_app", "js_log", "ui_ready",
}


class NexusHandler(SimpleHTTPRequestHandler):
    """Static UI plus a tiny same-origin JSON bridge.

    WebView2 injects pywebview's JavaScript bridge asynchronously.  On a cold
    second launch the page could see a half-created ``pywebview.api`` object,
    every call would fail, and the UI showed a false "backend offline" screen.
    Serving the same methods over HTTP removes that timing dependency and also
    makes the console usable from an ordinary browser.
    """

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/health":
            api_obj = getattr(self.server, "api_ref", None)
            self._send_json({
                "ok": True,
                "version": getattr(api_obj, "version", "?"),
                "pid": os.getpid(),
            })
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self.send_error(404)
            return
        method = path[len("/api/"):]
        api_obj = getattr(self.server, "api_ref", None)
        if method not in _HTTP_API_METHODS or api_obj is None:
            self._send_json({"error": "unknown API method: " + method}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            args = body.get("args") if isinstance(body, dict) else []
            if args is None:
                args = []
            if not isinstance(args, list):
                raise ValueError("args must be a JSON array")
            fn = getattr(api_obj, method)
            result = fn(*args)
        except Exception as exc:
            log("http api %s failed: %r" % (method, exc))
            self._send_json({"error": str(exc)}, 500)
            return
        self._send_json(result)


def _serve_web(api_obj):
    handler = lambda *args, **kwargs: NexusHandler(*args, directory=str(WEB_DIR), **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.api_ref = api_obj
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _message_box(text: str, title: str = APP_NAME) -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        pass


def _emit(text: str) -> None:
    """Console builds print; windowed builds leave a JSON file for smoke tests."""
    try:
        print(text)
    except Exception:
        pass
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / "launcher-probe.json").write_text(text, encoding="utf-8")
    except Exception:
        pass


def _preflight_runtime():
    """Windowed builds fail silently if pythonnet / pywebview is not bundled.

    Returns (problems, webview_module).  The caller shows a readable message
    instead of letting PyInstaller's bare "Failed to start embedded python
    interpreter!" dialog look like a white-screen crash.
    """
    problems = []
    webview_mod = None
    try:
        import webview as webview_mod  # noqa: F401
    except Exception as exc:
        problems.append("pywebview: %r" % (exc,))
    if IS_WINDOWS:
        try:
            import clr  # noqa: F401
        except Exception as exc:
            problems.append("pythonnet/clr: %r" % (exc,))
        else:
            try:
                import webview.platforms.winforms  # noqa: F401
            except Exception as exc:
                problems.append("webview.winforms: %r" % (exc,))
    return problems, webview_mod


def _window_size():
    """Clamp the launcher to the actual work area (company laptops are small)."""
    width, height = 1180, 760
    if IS_WINDOWS:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            sw = int(user32.GetSystemMetrics(0) or 0)
            sh = int(user32.GetSystemMetrics(1) or 0)
            if sw > 0:
                width = max(900, min(width, int(sw * 0.94)))
            if sh > 0:
                height = max(620, min(height, int(sh * 0.90)))
        except Exception:
            pass
    return width, height


def _webview_storage() -> str:
    """Keep WebView2's profile stable and away from a shared global folder."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return ""
    try:
        key = hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:10]
        path = Path(base) / "AgentNexus" / ("webview-" + key)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except Exception:
        return ""


def _open_in_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _browser_mode(url: str, note: str = "") -> int:
    """Keep the local console alive without WebView2 and serve it in a browser."""
    log("browser mode: " + url)
    _open_in_browser(url)
    if note:
        _message_box(note + "\n\n已用默认浏览器打开控制台。\n在控制台里点「退出」即可结束本进程。")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


def _watch_webview_ready(api_obj, window, url: str, timeout: float = 10.0) -> None:
    """If WebView2 never renders app.js, open the same console in a browser."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(api_obj, "ui_ready_at", 0.0):
            return
        time.sleep(0.5)
    log("webview heartbeat missing after %.0fs; opening browser fallback" % timeout)
    _open_in_browser(url)
    try:
        hide = getattr(window, "hide", None)
        if callable(hide):
            hide()
    except Exception:
        pass


def _main_impl() -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if "--probe" in sys.argv:
        api = Api(activate_trial=False)
        _emit(json.dumps({"boot": api.boot(), "checks": api.run_checks(),
                          "version": api.version}, ensure_ascii=False, indent=2))
        return 0
    if "--version" in sys.argv:
        _emit(_version())
        return 0
    force_browser = "--browser" in sys.argv
    if not _single_instance_guard():
        if _focus_existing_window():
            return 0
        _message_box(
            "Agent Nexus 已经打开了。\n\n"
            "请先使用已经打开的那个窗口；如果看不到它，在任务栏里找一找。\n"
            "这个提示是为了避免两个窗口同时写试用档案，把倒计时弄乱。",
            "Agent Nexus",
        )
        return 0
    if not WEB_DIR.exists():
        _message_box("启动器界面文件缺失：\n%s" % WEB_DIR)
        return 2
    problems, webview = _preflight_runtime()
    if problems:
        detail = "\n".join(problems)
        log("launcher preflight failed:\n" + detail)
        api = Api()
        server = _serve_web(api)
        url = "http://127.0.0.1:%d/index.html?v=%s&ver=%s" % (
            server.server_address[1], int(time.time()), api.version)
        return _browser_mode(
            url,
            "图形启动器组件缺失，已切换到浏览器控制台。\n\n%s\n\n"
            "请重新解压完整压缩包（不要只复制 AgentNexus.exe），"
            "并确认 _internal 文件夹与 exe 在同一目录。" % detail,
        )

    api = Api()
    server = _serve_web(api)
    url = "http://127.0.0.1:%d/index.html?v=%s&ver=%s" % (
        server.server_address[1], int(time.time()), api.version)
    if force_browser:
        return _browser_mode(url)
    try:
        webview.settings["DRAG_REGION_SELECTOR"] = ".drag"
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    except Exception:
        pass
    win_w, win_h = _window_size()
    window = webview.create_window(
        APP_NAME, url=url,
        width=win_w, height=win_h, min_size=(900, 620),
        frameless=True, easy_drag=False,
        background_color="#07080c",
        confirm_close=False,
    )
    api.window = window
    threading.Thread(target=_watch_webview_ready, args=(api, window, url),
                     daemon=True).start()
    storage = _webview_storage()
    start_kwargs = {"icon": str(ICON_FILE) if ICON_FILE.exists() else None}
    if storage:
        start_kwargs.update({"private_mode": False, "storage_path": storage})
    try:
        try:
            webview.start(**start_kwargs)
        except TypeError:
            # Older pywebview builds do not accept storage_path/private_mode.
            webview.start(icon=start_kwargs.get("icon"))
    except Exception as exc:
        log("webview failed:\n" + traceback.format_exc())
        return _browser_mode(
            url,
            "图形界面启动失败，已切换到浏览器控制台。\n\n%s\n\n"
            "请确认 Windows 已安装 WebView2 Runtime。" % exc,
        )
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
    return 0


def main() -> int:
    try:
        return _main_impl()
    except SystemExit:
        raise
    except Exception as exc:
        log("fatal launcher error:\n" + traceback.format_exc())
        _message_box(
            "Agent Nexus 启动时遇到未处理错误：\n\n%s\n\n"
            "日志已写入 _run\\launcher.log。\n"
            "请把这一步的日志发给Agent Nexus contributors，不要反复双击。" % exc,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
