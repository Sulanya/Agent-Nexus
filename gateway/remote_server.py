"""Agent Nexus Gateway v1.0 -- 移动浏览器远程查看+控制 Codex / Claude 会话(全功能版)。

架构:
  手机浏览器 <--HTTPS(cloudflared)--> 本服务 <--stdio JSON-RPC--> codex app-server

- 模块化 Python 网关；源码启动器依赖 psutil 等本机辅助包
- 长驻 app-server 子进程; 对手机开 REST + SSE
- SSE: 流式 delta / 工具卡片 / 回合状态 / 审批请求
- 控制: 发消息 / 停止 / 审批应答 / 新建会话

鉴权: ?k=<token> 或 X-Auth 头或 cookie; token 存 gateway/.secrets/token.txt
"""
import base64
from collections import deque
import copy
import gzip
import hashlib
import json
import os
import queue
import re
import socket
import struct
import shutil
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_config import codex_config, model_rows, read_json, safe_url, version_for, workbuddy_config
from thread_visibility import is_internal_thread
from rpc_contract import (MAX_FRAME_BYTES as RPC_MAX_FRAME_BYTES,
                          API_VERSION as HTTP_API_VERSION, RPC_VERSION as WS_RPC_VERSION,
                          authorize_http, authorize_message as authorize_ws_message,
                          negotiate_ws_version,
                          contract_document as rpc_contract_document)

try:
    import indexer as thread_index
except Exception:          # 索引器缺失只影响列表，网关本身照常工作
    thread_index = None
import urllib.request

GATEWAY_DIR = Path(__file__).resolve().parent
APP_DIR = GATEWAY_DIR.parent
UI_DIR = APP_DIR / "ui"
SECRETS_DIR = GATEWAY_DIR / ".secrets"
TOKEN_FILE = SECRETS_DIR / "token.txt"
PORT = int(os.environ.get("AGENTREMOTE_PORT", "8790"))
HOST = os.environ.get("AGENTREMOTE_HOST", "127.0.0.1")


def _user_home() -> Path:
    # 用户主目录: USERPROFILE / HOME 优先(可移植), 最后才用 Path.home()
    for _key in ("USERPROFILE", "HOME"):
        _val = (os.environ.get(_key) or "").strip()
        if _val and Path(_val).is_dir():
            return Path(_val)
    return Path.home()


USER_HOME = _user_home()
MAX_TURNS = 30          # thread detail 最多返回的回合数(取尾部)
MAX_OUTPUT = 24000      # 单条命令输出截断字符数
MAX_ITEMS_PER_TURN = 200    # 单回合最多条目数(防超长自动回合把手机端打爆)
MAX_TURN_BYTES = 900000     # 单回合 items JSON 字节预算
HUB_HISTORY_MAX = 4000      # 断线补发环形缓冲: 事件数上限
HUB_HISTORY_BYTES = 12 * 1024 * 1024   # 断线补发环形缓冲: 字节上限
MAX_TOTAL_BYTES = 3500000   # 整个 thread detail 的 items 总预算(超出丢更早回合)
MAX_TOTAL_ITEMS = 900       # 整个 thread detail 的条目总数上限
MAX_REQUEST_BODY = 64 * 1024 * 1024   # 入站请求体硬上限(含 chunked / ASR 图片)

# 手机/WAN 首屏用的轻量预算(light=1): 只留最近正文, 更早回合折叠成 elided。
# 实测 2.48MB / 885 items 的会话 -> 约 0.6MB / 280 items, gzip 后实际传输约 100KB。
TRIM_FULL = {
    "turns": MAX_TURNS, "output": MAX_OUTPUT,
    "items_per_turn": MAX_ITEMS_PER_TURN, "turn_bytes": MAX_TURN_BYTES,
    "total_bytes": MAX_TOTAL_BYTES, "total_items": MAX_TOTAL_ITEMS,
}
TRIM_LIGHT = {
    "turns": 10, "output": 6000,
    "items_per_turn": 80, "turn_bytes": 180000,
    "total_bytes": 700000, "total_items": 280,
}
SSE_HEARTBEAT = 15      # 秒


LOG_FILE = APP_DIR / "_run" / "gateway.log"
RECOVERY_JOURNAL = None
SUBMISSION_STORE = None


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_token() -> str:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


TOKEN = load_token()

VIEW_TOKEN_FILE = SECRETS_DIR / "view_token.txt"


def load_view_token() -> str:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if VIEW_TOKEN_FILE.exists():
        token = VIEW_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    VIEW_TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


VIEW_TOKEN = load_view_token()

# ---- 内测试用管控 (TRIAL_GATE_V1): 实现见同目录 trial_core.py ----
try:
    # embed 版 Python (runtime\python) 带 ._pth, 脚本目录不进 sys.path,
    # 必须手动加入同目录才能 import trial_core (2026-10-16 打包实测)
    import sys as _sys, os as _os
    _gd = _os.path.dirname(_os.path.abspath(__file__))
    if _gd not in _sys.path:
        _sys.path.insert(0, _gd)
    from trial_core import TRIAL
except Exception as _trial_exc:
    TRIAL = None
    try:
        log("trial_core import failed: %r" % (_trial_exc,))
    except Exception:
        pass

# ---- 交付包自护层 (GUARD_V1): 实现见同目录 guard.py ----
try:
    # 同 trial_core: embed 版 Python 的脚本目录不进 sys.path (2026-10-16 实测)
    import sys as _gsys, os as _gos
    _ggd = _gos.path.dirname(_gos.path.abspath(__file__))
    if _ggd not in _gsys.path:
        _gsys.path.insert(0, _ggd)
    import guard as GUARD
except Exception as _guard_exc:
    GUARD = None
    try:
        log("guard import failed: %r" % (_guard_exc,))
    except Exception:
        pass


def _dur_secs(d):
    try:
        if d is None:
            return None
        if isinstance(d, dict):
            return round(float(d.get("secs") or 0) + float(d.get("nanos") or 0) / 1e9, 3)
        return round(float(d), 3)
    except Exception:
        return None


CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (USER_HOME / ".codex"))
LOCAL_CONFIG = CODEX_HOME / "config.toml"
_models_cache = {"ts": 0.0, "data": None}

# 不绑定工程的新会话: 用一个中性的空目录, 避免把用户整个 Home 当 cwd。
NEXUS_HOME = USER_HOME / ".agent-nexus"
BLANK_WORKSPACE = NEXUS_HOME / "blank"


# Agent Nexus permission defaults (2026-10-20):
# An AN thread is a remote control for the user's own PC, so it has to match the
# desktop app: full file access, no approval prompts. The old behaviour came from
# the untrusted "blank" cwd, which defaulted to read-only + on-request and made
# every API-setup step ask for permission.
NEXUS_SANDBOX_MODE = "danger-full-access"
NEXUS_APPROVAL_POLICY = "never"
SETTINGS_LOCK = threading.Lock()

# 同一次切换会被手机端重复提交同一毫秒 5 次。按键串行 + 短缓存，
# 让第一个请求真正执行，其余请求只复用结果，绝不再打一遍 app-server。
_SETTINGS_KEY_LOCKS = {}
_SETTINGS_KEY_LOCKS_GUARD = threading.Lock()
_SETTINGS_RESULT_CACHE = {}
_SETTINGS_CACHE_TTL = 10.0
_SETTINGS_BURST_SECS = 2.5


def _settings_key_lock(key):
    with _SETTINGS_KEY_LOCKS_GUARD:
        lock = _SETTINGS_KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SETTINGS_KEY_LOCKS[key] = lock
        if len(_SETTINGS_KEY_LOCKS) > 512:
            stale = [k for k in _SETTINGS_KEY_LOCKS
                     if k not in _SETTINGS_RESULT_CACHE and not _SETTINGS_KEY_LOCKS[k].locked()]
            for k in stale[:128]:
                _SETTINGS_KEY_LOCKS.pop(k, None)
        return lock


def _settings_cache_get(key, max_age=None, valid=None):
    """返回已完成的 settings 响应；过期、不存在或 valid 判定失效时返回 None。

    valid 是「缓存还能不能代表当前真实状态」的回调。A->B->A 这种快速切换里，
    A 的旧缓存必须被判定失效，否则最后一次 A 会被 10 秒前的响应吃掉。
    """
    now = time.time()
    with _SETTINGS_KEY_LOCKS_GUARD:
        rec = _SETTINGS_RESULT_CACHE.get(key)
        if not isinstance(rec, dict):
            return None
        age = now - float(rec.get("ts") or 0.0)
        limit = _SETTINGS_CACHE_TTL if max_age is None else float(max_age)
        if age > limit:
            _SETTINGS_RESULT_CACHE.pop(key, None)
            return None
        resp = copy.deepcopy(rec.get("resp"))
    if valid is not None:
        try:
            if not valid(resp):
                return None
        except Exception:
            return None
    return resp


def _settings_cache_put(key, resp):
    with _SETTINGS_KEY_LOCKS_GUARD:
        _SETTINGS_RESULT_CACHE[key] = {
            "ts": time.time(),
            "resp": copy.deepcopy(resp or {}),
        }
        if len(_SETTINGS_RESULT_CACHE) > 256:
            cutoff = time.time() - _SETTINGS_CACHE_TTL
            for k in [k for k, v in _SETTINGS_RESULT_CACHE.items()
                      if float((v or {}).get("ts") or 0.0) < cutoff]:
                _SETTINGS_RESULT_CACHE.pop(k, None)


def _settings_run_key(engine, tid, model="", provider="", effort=""):
    return "|".join((str(engine or ""), str(tid or ""), str(model or ""),
                     str(provider or ""), str(effort or "")))


def _settings_once(key, fn, max_age=None, valid=None):
    """同一 settings 目标只执行一次。

    fn() 返回 (payload, status_code)。只有 200 + ok=True 才缓存；
    错误结果原样返回，允许用户改完参数后立即重试。
    """
    cached = _settings_cache_get(key, max_age=max_age, valid=valid)
    if cached is not None:
        return cached, 200, True
    lock = _settings_key_lock(key)
    with lock:
        cached = _settings_cache_get(key, max_age=max_age, valid=valid)
        if cached is not None:
            return cached, 200, True
        payload, code = fn()
        code = int(code or 200)
        if code == 200 and isinstance(payload, dict) and payload.get("ok"):
            _settings_cache_put(key, payload)
        return payload, code, False


def blank_workspace() -> str:
    try:
        BLANK_WORKSPACE.mkdir(parents=True, exist_ok=True)
        return str(BLANK_WORKSPACE)
    except OSError:
        return str(USER_HOME)


def nexus_sandbox_policy() -> dict:
    # Verified 2026-10-20 on this machine: workspace-write with writable_roots
    # still denied writes to ~/.codex (Access is denied) and broke TLS inside the
    # sandbox (schannel SEC_E_NO_CREDENTIALS). Full access is what the desktop app
    # uses here and the only level that removes the prompts.
    return {"type": "dangerFullAccess"}


def nexus_thread_owned(tid: str) -> bool:
    try:
        item = _index_thread(tid) or {}
    except Exception:
        return False
    return (item.get("originator") or "") == "Agent Nexus"


# ---------- 会话归组覆盖层 (thread -> folder) ----------
# 不动各引擎自己的落盘记录: 只在本网关记一层 "这条会话归到哪个工程分组"，
# list/detail 返回时把 cwd 换成归组后的值，跨设备（手机/iPad/电脑）共用同一份。
THREAD_GROUPS_FILE = GATEWAY_DIR / "thread_groups.json"
_thread_groups_cache = None
_thread_groups_lock = threading.RLock()


def thread_groups_load():
    """读归组覆盖表；文件 IO 只在缓存冷启动时做一次，而且不在锁里做。"""
    global _thread_groups_cache
    with _thread_groups_lock:
        if _thread_groups_cache is not None:
            return _thread_groups_cache
    try:
        raw = json.loads(THREAD_GROUPS_FILE.read_text(encoding="utf-8"))
        data = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    except (OSError, ValueError):
        data = {}
    with _thread_groups_lock:
        if _thread_groups_cache is None:
            _thread_groups_cache = data
        return _thread_groups_cache


_thread_groups_save_lock = threading.Lock()


def thread_groups_save():
    """落盘：保存锁串行化，全局锁只拷快照，写文件在锁外。

    两个并发 save 共用同一个 .tmp 会互相截断；先后顺序颠倒还会用旧快照
    盖掉新快照。所以快照必须在拿到保存锁之后再取。
    """
    with _thread_groups_save_lock:
        with _thread_groups_lock:
            snapshot = dict(_thread_groups_cache or {})
        payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
        tmp = THREAD_GROUPS_FILE.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, THREAD_GROUPS_FILE)


def thread_group_set(tid, cwd):
    thread_groups_load()          # 确保缓存就绪（IO 在锁外）
    with _thread_groups_lock:
        _thread_groups_cache[str(tid)] = cwd or ""
    thread_groups_save()


def thread_group_apply(t, catalog=None):
    """把网关侧归组覆盖到返回给前端的一条会话上（不改引擎真实 cwd）。"""
    if not isinstance(t, dict):
        return t
    if t.get('engine', 'codex') == 'codex':
        return desktop_project_catalog().apply(t, catalog)
    tid = t.get("id")
    if not tid:
        return t
    # 不在锁里调 load(): 冷启动时 load 会读文件，持锁做 IO 会拖住整张列表。
    data = thread_groups_load()
    if tid not in data:
        return t
    dest = data[tid]
    real = t.get("cwd") or ""
    if dest != real:
        t["cwdOriginal"] = real
        t["cwd"] = dest
        t["moved"] = True
    return t


EFFORT_CANON = ["low", "medium", "high", "xhigh", "max", "ultra"]
STATIC_MODEL_LABELS = {}

# configured provider 中转的可用模型（2026-10-20 实测；其余模型 502，别加）
CONFIGURED_PROVIDER_PROVIDER = "configured-provider"
CONFIGURED_PROVIDER_MODELS = {}


def _local_cfg():
    return codex_config(LOCAL_CONFIG)


def model_provider_of(model_id: str) -> str:
    """模型 -> provider：configured-provider 系列走中转，其余沿用主配置 provider。"""
    if not model_id:
        return ""
    return _local_cfg().get("provider") or ""


def _win_proxy_info():
    """Windows 系统代理体检: socks= 前缀会让 Rust/reqwest 把所有请求发成畸形代理。"""
    if os.name != "nt":
        return {}
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        def _g(name):
            try:
                return winreg.QueryValueEx(key, name)[0]
            except OSError:
                return None
        srv = str(_g("ProxyServer") or "")
        return {"enable": bool(_g("ProxyEnable")),
                "server": "SOCKS" if "socks" in srv.lower() else ("configured" if srv else ""),
                "override": str(_g("ProxyOverride") or ""),
                "socksPrefix": ("socks=" in srv.lower())}
    except Exception:
        return {}


def _models_payload(force: bool = False):
    """Read current configuration and local catalog; never advertise developer defaults."""
    cfg = _local_cfg()
    paths = [LOCAL_CONFIG, CODEX_HOME / "auth.json"]
    if cfg["catalog"]:
        paths.append(Path(cfg["catalog"]))
    config_version = version_for(paths)
    if not force and _models_cache.get('configVersion') == config_version and _models_cache["data"] and time.time() - _models_cache["ts"] < 30:
        return _models_cache["data"]
    models = model_rows(read_json(cfg["catalog"]), cfg["catalog"]) if cfg["catalog"] else []
    source = "local_catalog" if models else "local_config"
    probe_ok = False
    probe_err = ""
    credential_present = False
    if cfg["base_url"] and cfg["env_key"]:
        key = os.environ.get(cfg["env_key"]) or ""
        if not key:
            auth = read_json(CODEX_HOME / "auth.json")
            if isinstance(auth, dict):
                key = str(auth.get(cfg["env_key"]) or "")
        if key:
            credential_present = True
            try:
                req = urllib.request.Request(cfg["base_url"] + "/models",
                                             headers={"Authorization": "Bearer " + key})
                host = urlparse(cfg["base_url"]).hostname
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if host in ("127.0.0.1", "localhost", "::1") else urllib.request.build_opener()
                with opener.open(req, timeout=4) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if not models:
                    models = model_rows(data, safe_url(cfg["base_url"]) + "/models")
                    source = "provider_catalog"
                probe_ok = True
            except Exception as exc:
                probe_err = "模型目录未验证（%s）" % type(exc).__name__
    if not models and globals().get("AS") is not None and AS.status == "ready":
        try:
            models = model_rows(AS.request("model/list", {"includeHidden": False}, timeout=4), "app-server/model/list")
            source = "app_server"
        except Exception as exc:
            probe_err = "本机模型目录读取失败（%s）" % type(exc).__name__
    if cfg["model"] and cfg["model"] not in {row["id"] for row in models}:
        models.insert(0, {"id": cfg["model"], "label": cfg["model"], "efforts": [], "source": str(LOCAL_CONFIG)})
    for _m in models:
        if not _m.get("provider"):
            _m["provider"] = cfg["provider"]
        if _m["id"] == cfg["model"] and cfg["effort"] and cfg["effort"] not in _m["efforts"]:
            _m["efforts"].append(cfg["effort"])
    union = [x for x in EFFORT_CANON
             if any(x in (mm.get("efforts") or []) for mm in models)]
    payload = {
        "models": models,
        "efforts": union,
        "defaultModel": cfg["model"],
        "defaultEffort": cfg["effort"],
        "provider": cfg["provider"],
        "source": source,
        "readAt": time.time(),
        "configVersion": config_version,
        "cfg": {
            "engine": "codex",
            "path": str(LOCAL_CONFIG),
            "exists": LOCAL_CONFIG.is_file(),
            "codexHome": str(CODEX_HOME),
            "baseUrl": safe_url(cfg["base_url"]),
            "provider": cfg["provider"],
            "model": cfg["model"],
            "effort": cfg["effort"],
            "envKey": cfg["env_key"],
            "envKeyPresent": credential_present,
            "probeOk": probe_ok,
            "probeError": probe_err,
            "proxy": _win_proxy_info(),
            "catalogPath": cfg["catalog"],
        },
    }
    _models_cache["ts"] = time.time()
    _models_cache['configVersion'] = config_version
    _models_cache["data"] = payload
    return payload





_CONTEXT_BUDGET_CACHE = {"ts": 0.0, "data": {}}


def _context_budget_map() -> dict:
    """实测上游上下文预算，键为 "provider/model"。

    优先问活着的 router（/router/context），失败再读 context_limits.json。
    拿不到就返回空字典——切换面板只是少几个字段，不影响原有功能。
    """
    now = time.time()
    if now - float(_CONTEXT_BUDGET_CACHE.get("ts") or 0) < 10.0:
        return _CONTEXT_BUDGET_CACHE.get("data") or {}
    out = {}
    ctx = None
    try:
        import urllib.request as _ur
        router_url=os.environ.get("AN_ROUTER_URL", "").rstrip("/")
        if not router_url:raise ValueError("optional router not configured")
        _req = _ur.Request(router_url+"/router/context",
                           headers={"User-Agent": "agent-nexus"})
        with _ur.urlopen(_req, timeout=0.6) as _resp:
            ctx = json.loads(_resp.read().decode("utf-8"))
    except Exception:
        ctx = None
    if isinstance(ctx, dict) and isinstance(ctx.get("providers"), dict):
        for _prov, _models in ctx["providers"].items():
            if not isinstance(_models, dict):
                continue
            for _model, _b in _models.items():
                if not isinstance(_b, dict):
                    continue
                out["%s/%s" % (_prov, _model)] = {
                    "contextWindow": _b.get("limit"),
                    "measuredWindow": _b.get("measured_limit"),
                    "softBudget": _b.get("soft_budget"),
                    "hardBudget": _b.get("hard_budget"),
                    "budgetConfidence": _b.get("confidence"),
                    "budgetSource": "router",
                }
    if not out:
        _cands = []
        _envp = (os.environ.get("AN_CONTEXT_LIMITS") or "").strip()
        if _envp:
            _cands.append(Path(_envp))

        _cands.append(GATEWAY_DIR / "context_limits.json")
        for _cp in _cands:
            try:
                if not _cp.is_file():
                    continue
                _raw = json.loads(_cp.read_text(encoding="utf-8"))
            except Exception:
                continue
            for _prov, _provrec in (_raw.get("providers") or {}).items():
                for _model, _rec in ((_provrec or {}).get("models") or {}).items():
                    if not isinstance(_rec, dict):
                        continue
                    _lim = int(_rec.get("measured_limit") or 0)
                    if _lim <= 0:
                        continue
                    out["%s/%s" % (_prov, _model)] = {
                        "contextWindow": _lim,
                        "measuredWindow": _lim,
                        "softBudget": int(_lim * 0.70),
                        "hardBudget": int(_lim * 0.95),
                        "budgetConfidence": _rec.get("confidence"),
                        "budgetSource": "cache",
                    }
            if out:
                break
    _CONTEXT_BUDGET_CACHE["ts"] = now
    _CONTEXT_BUDGET_CACHE["data"] = out
    return out


def _context_budget_for(provider: str, model: str):
    _m = _context_budget_map()
    _hit = _m.get("%s/%s" % (provider, model))
    if _hit:
        return _hit
    _base = str(model or "").split("(")[0].strip()
    if _base and _base != model:
        return _m.get("%s/%s" % (provider, _base))
    return None
def _providers_payload(engine: str = "codex", force: bool = False) -> dict:
    """给切换面板的 provider 列表；只暴露模型元数据，不返回任何密钥。"""
    eng = engine if engine in ("codex", "claude", "workbuddy") else "codex"
    if eng == "workbuddy":
        wb_ensure_cli()
        payload = wb_models_payload()
    elif eng == "claude":
        payload = claude_models_payload()
    else:
        payload = _models_payload(force=force)
    raw_current = str(payload.get("provider") or "")
    current = raw_current if eng == "codex" else eng
    labels = {
        "configured-provider": "Configured provider",
        "deepseek": "DeepSeek 官方",
        "claude": "Claude Code",
        "workbuddy": "WorkBuddy",
    }
    groups = {}
    order = []
    for m in (payload.get("models") or []):
        if not isinstance(m, dict) or not m.get("id"):
            continue
        mid = str(m["id"])
        pid = str(m.get("provider") or current or eng)
        if pid not in groups:
            groups[pid] = {
                "id": pid,
                "label": labels.get(pid, pid),
                "provider": pid,
                "current": pid == current,
                "models": [],
            }
            order.append(pid)
        _entry = {
            "id": mid,
            "label": str(m.get("label") or mid),
            "efforts": list(m.get("efforts") or []),
        }
        _cb = _context_budget_for(pid, mid)
        if _cb:
            _entry.update(_cb)
        groups[pid]["models"].append(_entry)
    if current and current not in groups:
        groups[current] = {
            "id": current,
            "label": labels.get(current, current),
            "provider": current,
            "current": True,
            "models": [],
        }
        order.append(current)
    providers = [groups[k] for k in order]
    return {
        "ok": True,
        "engine": eng,
        "providers": providers,
        "data": providers,
        "current": current,
        "defaultProvider": current,
        "defaultModel": payload.get("defaultModel"),
        "defaultEffort": payload.get("defaultEffort"),
    }

def find_codex_exe() -> str:
    # (1) PATH 优先: 内测用户自带 codex, npm / 官方安装器都会写进 PATH (macOS 就在这里)
    for _nm in ("codex", "codex.exe"):
        try:
            _w = shutil.which(_nm)
        except Exception:
            _w = None
        if _w:
            return _w
    # (2) Windows 桌面版: %LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(USER_HOME / "AppData" / "Local"))) / "OpenAI" / "Codex" / "bin"
        cands = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        alive = [c for c in cands if c.exists()]
        if not alive:
            raise RuntimeError(f"codex.exe not found under {base}")
        return str(alive[0])
    # (3) macOS / Linux 常见落点 (PATH 没覆盖到的)
    for _p in (
        USER_HOME / ".codex" / "bin" / "codex",
        USER_HOME / ".local" / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
        Path("/Applications/Codex.app/Contents/Resources/codex"),
    ):
        try:
            if _p.is_file():
                return str(_p)
        except OSError:
            pass
    raise RuntimeError("codex 未找到: 请确认终端里 `codex --version` 有输出 (PATH 里能跑)")


# ---------- Claude Code 引擎: 常量与可执行文件定位 ----------

CLAUDE_HOME = USER_HOME / ".claude"
CLAUDE_PROJECTS = CLAUDE_HOME / "projects"
CLAUDE_SESSIONS_DIR = CLAUDE_HOME / "sessions"
CLAUDE_LOCAL_SETTINGS = CLAUDE_HOME / "settings.json"
CLAUDE_ARCHIVE_FILE = GATEWAY_DIR / "claude_archived.json"
CLAUDE_SETTINGS_FILE = GATEWAY_DIR / "claude_settings.json"
CLAUDE_USAGE_FILE = GATEWAY_DIR / "claude_usage.json"
CLAUDE_TRASH_DIR = GATEWAY_DIR / "_claude_trash"

# ---------- WorkBuddy (腾讯 CodeBuddy Code) 引擎: 常量与可执行文件定位 ----------
# WorkBuddy 5.3.8 自带 CLI，契约与 Claude Code 同构:
#   -p --output-format stream-json --include-partial-messages --input-format stream-json
#   --session-id / --resume / --model / --effort / --permission-mode
# 会话落盘 ~/.codebuddy/projects/<slug>/<sid>.jsonl
# 行类型: message(input_text/output_text) / reasoning / function_call / function_call_result
WB_HOME = Path(os.environ.get("CODEBUDDY_CONFIG_DIR") or (USER_HOME / ".codebuddy"))
WB_PROJECTS = WB_HOME / "projects"
# 桌面端(WorkBuddy Electron) 自己一套历史, 与 CLI 平级:
#   ~/.workbuddy/projects/<slug>/<uuid>.jsonl  (行格式同 CLI, 另有 ai-title)
WB_DESKTOP_HOME = USER_HOME / ".workbuddy"
WB_DESKTOP_PROJECTS = WB_DESKTOP_HOME / "projects"
WB_PROJECTS_ALL = (WB_PROJECTS, WB_DESKTOP_PROJECTS)
WB_SESSIONS_DIR = WB_HOME / "sessions"
WB_LOCAL_SETTINGS = WB_HOME / "settings.json"
WB_ARCHIVE_FILE = GATEWAY_DIR / "workbuddy_archived.json"
WB_SETTINGS_FILE = GATEWAY_DIR / "workbuddy_settings.json"
WB_USAGE_FILE = GATEWAY_DIR / "workbuddy_usage.json"
WB_TRASH_DIR = GATEWAY_DIR / "_workbuddy_trash"
WB_HIDDEN_FILE = GATEWAY_DIR / "workbuddy_hidden.json"
WB_MISSING_TRANSCRIPT = ("WorkBuddy 已清理这条会话的本地 JSONL，只剩索引元数据；"
                        "正文无法在本机恢复，也不能继续发送。请在原工作目录新建一个会话。")
_wb_env_cli = (os.environ.get("WORKBUDDY_CLI_DIR") or "").strip()
WB_CLI_DIR = Path(_wb_env_cli) if _wb_env_cli else None
_wb_discovery_lock = threading.RLock()
_workbuddy_exe_cache = {"argv": [], "electron": False, "dir": "", "at": 0.0}
WB_CLI_LAST_ERROR = ""
WB_CLI_SEARCHED = []


def _wb_registry_homes():
    """Windows 注册表里的 WorkBuddy / CodeBuddy 安装目录（对非 D 盘安装最有效）。"""
    if os.name != "nt":
        return []
    try:
        import winreg
    except Exception:
        return []
    out = []
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, sub in roots:
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            i = 0
            while True:
                checkpoints = {}
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, name) as sk:
                        def _val(n):
                            try:
                                return winreg.QueryValueEx(sk, n)[0]
                            except OSError:
                                return ""
                        display = str(_val("DisplayName") or "")
                        if not re.search(r"workbuddy|codebuddy", display, re.I):
                            continue
                        for raw in (str(_val("InstallLocation") or ""),
                                    str(_val("DisplayIcon") or "")):
                            raw = raw.strip().strip('"')
                            if not raw:
                                continue
                            p = Path(raw.split(",")[0].strip())
                            if p.suffix.lower() == ".exe":
                                p = p.parent
                            if p.is_dir():
                                out.append(p)
                except OSError:
                    continue
        finally:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
    return out


def _wb_home_candidates():
    out = []
    for key in ("WORKBUDDY_HOME", "WORKBUDDY_CLI_DIR"):
        raw = (os.environ.get(key) or "").strip().strip('"')
        if raw:
            p = Path(raw)
            out.append(p)
            out.extend(list(p.parents)[:4])
    local = os.environ.get("LOCALAPPDATA") or str(USER_HOME / "AppData" / "Local")
    prog = os.environ.get("ProgramFiles", "C:/Program Files")
    prog86 = os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")
    out += [
        
        Path(local) / "Programs" / "WorkBuddy",
        Path(local) / "WorkBuddy",
        Path(prog) / "WorkBuddy",
        Path(prog86) / "WorkBuddy",
        USER_HOME / "AppData" / "Local" / "Programs" / "WorkBuddy",
    ]
    out += _wb_registry_homes()
    uniq = []
    seen = set()
    for p in out:
        try:
            k = str(p.resolve()).lower()
        except Exception:
            k = str(p).lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _wb_cli_in(home: Path):
    for cli in (home / "resources" / "app.asar.unpacked" / "cli",
                home / "resources" / "app" / "out" / "cli",
                home / "cli", home):
        try:
            if (cli / "bin" / "codebuddy").is_file():
                return cli
        except OSError:
            pass
    return None


def workbuddy_cli_dir():
    """动态发现 WorkBuddy CLI 目录；找不到返回 None（不再写死 D:\\workbuddy）。"""
    global WB_CLI_DIR, WB_CLI_SEARCHED
    with _wb_discovery_lock:
        if WB_CLI_DIR is not None and (WB_CLI_DIR / "bin" / "codebuddy").is_file():
            return WB_CLI_DIR
        searched = []
        for home in _wb_home_candidates():
            searched.append(str(home))
            hit = _wb_cli_in(home)
            if hit is not None:
                WB_CLI_DIR = hit
                WB_CLI_SEARCHED = searched
                return hit
        WB_CLI_SEARCHED = searched
        return None


def _wb_node_candidates():
    out = []
    for x in (os.environ.get("WORKBUDDY_NODE") or "").split(os.pathsep):
        x = x.strip()
        if x:
            out.append(Path(x))
    try:
        _w = shutil.which("node") or shutil.which("node.exe")
        if _w:
            out.append(Path(_w))
    except Exception:
        pass
    out += [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe",
        USER_HOME / "AppData" / "Roaming" / "npm" / "node.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node.exe",
    ]
    uniq = []
    seen = set()
    for p in out:
        k = str(p).lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def find_workbuddy_cli():
    """启动 CLI 的 argv 前缀: [node, codebuddy] 或 [WorkBuddy.exe, codebuddy](ELECTRON_RUN_AS_NODE)。"""
    global WB_CLI_LAST_ERROR
    with _wb_discovery_lock:
        hit = _workbuddy_exe_cache.get("argv") or []
        if hit and all(os.path.exists(p) for p in hit):
            return list(hit)
        cli_dir = workbuddy_cli_dir()
        if cli_dir is None:
            WB_CLI_LAST_ERROR = ("没有找到 WorkBuddy 安装目录。请确认 WorkBuddy 已安装，"
                                 "或设置环境变量 WORKBUDDY_CLI_DIR 指向 ...\\resources\\app.asar.unpacked\\cli")
            return []
        cli = cli_dir / "bin" / "codebuddy"
        for c in _wb_node_candidates():
            try:
                if not c.is_file():
                    continue
                r = subprocess.run([str(c), "--version"], capture_output=True, text=True, timeout=8,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (r.stdout or "").strip())
                if not m or tuple(int(x) for x in m.groups()) < (18, 20, 8):
                    continue
                _workbuddy_exe_cache.update({"argv": [str(c), str(cli)], "electron": False,
                                             "dir": str(cli_dir), "at": time.time()})
                WB_CLI_LAST_ERROR = ""
                return [str(c), str(cli)]
            except Exception:
                continue
        exes = []
        for base in (cli_dir, *list(cli_dir.parents)[:4], *_wb_home_candidates()):
            exes.append(base / "WorkBuddy.exe")
        for exe in exes:
            try:
                if exe.is_file():
                    _workbuddy_exe_cache.update({"argv": [str(exe), str(cli)], "electron": True,
                                                 "dir": str(cli_dir), "at": time.time()})
                    WB_CLI_LAST_ERROR = ""
                    return [str(exe), str(cli)]
            except OSError:
                continue
        WB_CLI_LAST_ERROR = ("已找到 WorkBuddy CLI 脚本，但缺少可用的 Node 或 WorkBuddy.exe。"
                             "请安装 Node.js 20+，或重新安装 WorkBuddy。")
        return []


# ---------- Agent Nexus 全局设置(思考中文化等): 前后端真值幂等 ----------
NEXUS_SETTINGS_FILE = GATEWAY_DIR / "nexus_settings.json"
# 强制中文思考。实测(2026-10-16): 注入前 reasoning 里中文占比 0.07, 注入后 0.93。
NEXUS_THINK_ZH = ("撰写 reasoning（思考过程）时，必须始终使用简体中文。"
                  "即使内部推理习惯使用英文，面向用户展示的思考内容也必须全部转成简体中文；"
                  "只有代码、命令、路径、API 名、变量名、专有名词和必要的原文引用可以保留英文。"
                  "不要输出英文段落。")
_nexus_lock = threading.Lock()


def _nexus_read() -> dict:
    try:
        obj = json.loads(NEXUS_SETTINGS_FILE.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _nexus_write(obj: dict) -> None:
    try:
        tmp = NEXUS_SETTINGS_FILE.with_name(NEXUS_SETTINGS_FILE.name + ".tmp-w")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(NEXUS_SETTINGS_FILE))
    except OSError as exc:
        log("nexus settings save: %s" % exc)
    except Exception as exc:
        log("nexus settings save: %r" % exc)


def nexus_patch(patch: dict) -> dict:
    """合并写入设置(原子); 返回最新全量设置。"""
    with _nexus_lock:
        obj = _nexus_read()
        obj.update(patch)
        _nexus_write(obj)
        return obj


NEXUS_ZH_PROMPT_FILE = GATEWAY_DIR / "nexus_think_zh.txt"


def think_zh_prompt_path() -> str:
    """把中文思考指令落到文件: Claude 侧走 --append-system-prompt-file, argv 保持纯 ASCII。
    返回可用路径; 落盘失败返回 ''(调用方回退到内联文本)。"""
    try:
        txt = NEXUS_THINK_ZH + "\n"
        cur = ""
        try:
            cur = NEXUS_ZH_PROMPT_FILE.read_text(encoding="utf-8")
        except OSError:
            cur = ""
        if cur != txt:
            tmp = NEXUS_ZH_PROMPT_FILE.with_name(NEXUS_ZH_PROMPT_FILE.name + ".tmp-w")
            tmp.write_text(txt, encoding="utf-8")
            os.replace(str(tmp), str(NEXUS_ZH_PROMPT_FILE))
        return str(NEXUS_ZH_PROMPT_FILE) if NEXUS_ZH_PROMPT_FILE.exists() else ""
    except Exception as exc:
        log("nexus zh prompt file: %r" % exc)
        return ""


def think_zh_on() -> bool:
    """固定开: 思考过程始终以简体中文呈现，不提供切换入口。"""
    return True


def think_zh_mark(tid: str) -> None:
    """记住这条会话已注入过, 避免每次 resume 反复叠加同一句 developerInstructions。"""
    if not tid:
        return
    with _nexus_lock:
        obj = _nexus_read()
        lst = obj.get("injected")
        if not isinstance(lst, list):
            lst = []
        if tid in lst:
            return
        lst.append(tid)
        obj["injected"] = lst[-400:]
        _nexus_write(obj)


def think_zh_marked(tid: str) -> bool:
    try:
        return tid in (_nexus_read().get("injected") or [])
    except Exception:
        return False


def think_zh_params(tid: str) -> dict:
    """需要注入时返回 {"developerInstructions": ...}, 否则 {}。"""
    try:
        if think_zh_on() and not think_zh_marked(tid):
            return {"developerInstructions": NEXUS_THINK_ZH}
    except Exception:
        pass
    return {}

CLAUDE = None          # main() 里实例化; 供引擎路由判断
WB = None              # WorkBuddy 引擎实例

_claude_exe_cache = {"path": ""}


def _claude_bin_dirs():
    # claude.exe 候选目录(可移植): 环境变量 CLAUDE_BIN_DIRS(os.pathsep 分隔) > 常见安装点
    out = []
    for x in (os.environ.get("CLAUDE_BIN_DIRS") or "").split(os.pathsep):
        x = x.strip()
        if x:
            out.append(x)
    for p in (
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        USER_HOME / ".npm-global" / "bin",
        USER_HOME / "AppData" / "Roaming" / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin",
        USER_HOME / "AppData" / "Local" / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin",
        USER_HOME / "AppData" / "Local" / "pnpm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin",
        USER_HOME / ".bun" / "install" / "global" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin",
        USER_HOME / ".claude" / "local",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin",
    ):
        out.append(str(p))
    return out


def find_claude_exe() -> str:
    """定位 claude.exe: 直查已知安装点; 兜底解析 `where claude` 拿到的 .cmd 包装。"""
    hit = _claude_exe_cache["path"]
    if hit and os.path.exists(hit):
        return hit
    cands = []
    # PATH 优先: macOS / Linux 的 claude 就是 PATH 里的可执行文件(没有 .exe)
    try:
        _w = shutil.which("claude")
        if _w:
            cands.append(Path(_w))
    except Exception:
        pass
    for _d in _claude_bin_dirs():
        for _pat in ("claude.exe", "claude"):
            try:
                cands += [x for x in sorted(Path(_d).glob(_pat)) if x.is_file()]
            except OSError:
                pass
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = ""
        if os.name == "nt":
            out = subprocess.run(["where", "claude"], capture_output=True, text=True, timeout=6,
                                 creationflags=flags).stdout or ""
        for ln in out.splitlines():
            p = Path(ln.strip())
            if not p.name:
                continue
            if p.suffix.lower() == ".exe" and p.exists():
                cands.append(p)
            elif p.suffix.lower() in (".cmd", ".bat") and p.exists():
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                m = re.search(r'%dp0%\\([^"\r\n]+claude\.exe)', txt, re.I)
                if m:
                    cands.append(p.parent / m.group(1).replace("\\", os.sep))
    except Exception:
        pass
    for c in cands:
        try:
            if c.exists():
                _claude_exe_cache["path"] = str(c)
                return str(c)
        except OSError:
            continue
    return ""


class ApiError(Exception):
    def __init__(self, err):
        super().__init__(str(err))
        self.err = err


def submission_store():
    global SUBMISSION_STORE
    if SUBMISSION_STORE is None:
        from submission_ledger import SubmissionLedger
        SUBMISSION_STORE = SubmissionLedger(APP_DIR / '_run' / 'submissions.sqlite')
    return SUBMISSION_STORE


_QUEUE_INTENTS = None
_QUEUE_INTENTS_LOCK = threading.Lock()


def queue_intent_store():
    global _QUEUE_INTENTS
    if _QUEUE_INTENTS is not None:
        return _QUEUE_INTENTS
    with _QUEUE_INTENTS_LOCK:
        if _QUEUE_INTENTS is None:
            from queue_intents import QueueIntentStore
            _QUEUE_INTENTS = QueueIntentStore(APP_DIR / '_run' / 'queue-intents.sqlite')
    return _QUEUE_INTENTS


_WORKFLOW_STORE = None
_WORKFLOW_WORKER = None
_WORKFLOW_LOCK = threading.Lock()


def workflow_store():
    global _WORKFLOW_STORE
    with _WORKFLOW_LOCK:
        if _WORKFLOW_STORE is None:
            from workflow_tasks import WorkflowTaskStore
            _WORKFLOW_STORE = WorkflowTaskStore(APP_DIR / '_run' / 'workflow-tasks.sqlite')
        return _WORKFLOW_STORE


def workflow_thread(tid, engine):
    if engine == 'claude':
        return {'id':tid,'engine':engine} if CLAUDE is not None and CLAUDE.is_known(tid) else None
    if engine == 'workbuddy':
        return {'id':tid,'engine':engine} if WB is not None and WB.is_known(tid) else None
    if engine != 'codex':
        return None
    row=desktop_project_catalog().data().get('nativeThreads',{}).get(tid)
    return row if row and not row.get('archived') else None


def workflow_preflight(task):
    tid=task['target_thread_id'];engine=task['target_engine']
    if workflow_thread(tid,engine) is None:
        return '目标会话不存在或已归档，请检查任务目标'
    if engine == 'codex':
        if AS is None or AS.status != 'ready':return 'Codex 引擎暂未就绪'
        if AS.control.is_desktop(tid):return '会话已交给电脑端，等待你显式接管后再执行'
        if not thread_writable(tid,fresh=True):return '电脑端持有会话，等待释放后执行'
    elif engine == 'claude':
        if CLAUDE is None or CLAUDE.status == 'no-cli':return 'Claude 引擎暂不可用'
    else:
        if WB is None or WB.status == 'no-cli':return 'WorkBuddy 引擎暂不可用'
    return None


def workflow_dispatch(task):
    from workflow_runtime import dispatch_task
    return dispatch_task(task,submission_store(),workflow_preflight,
                         lambda tid,body:Handler._submit_turn(None,tid,body),reconcile_submission)


def workflow_reconcile(task):
    store=workflow_store()
    receipt,status=submission_store().resolve(task['target_thread_id'],task['cid'],reconcile_submission)
    if status==200:
        task=store.reconcile(task['id'],receipt)
    elif status==409:
        task=store.mark_uncertain(task['id'],'任务标识冲突，需人工核对')
        task=store.review_uncertain(task['id'],force=True)
    elif task and task['state'] in ('accepted','queued','running','reconciling'):
        task=store.mark_uncertain(task['id'])
    if task and task['state'] in ('accepted','reconciling') and not task.get('turn_id'):
        task=store.review_uncertain(task['id'],max_age=600)
    if not task or task['state'] in ('completed','failed','cancelled'):return
    if not task.get('turn_id') or RECOVERY_JOURNAL is None:return
    from workflow_runtime import observe_events
    events=RECOVERY_JOURNAL.events_after(task.get('event_cursor',0),tid=task['target_thread_id'],limit=500)
    cursor,terminal=observe_events(task,events)
    store.advance_cursor(task['id'],cursor)
    if terminal:store.finish(task['id'],*terminal)


def workflow_create(body):
    import datetime
    kind=str(body.get('kind') or 'scheduled')
    target=str(body.get('targetThreadId') or '').strip()
    engine=str(body.get('targetEngine') or 'codex')
    source=str(body.get('sourceThreadId') or '').strip() or None
    source_engine=str(body.get('sourceEngine') or engine)
    request_id=str(body.get('requestId') or '').strip()
    if not request_id or len(request_id)>200:raise ValueError('请提供稳定的任务请求标识')
    run_at=body.get('runAt')
    if isinstance(run_at,str):
        parsed=datetime.datetime.fromisoformat(run_at.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError('执行时间必须包含时区')
        run_at=parsed.timestamp()
    if not isinstance(run_at,(int,float)):raise ValueError('请指定执行时间')
    if kind not in ('scheduled','handoff'):raise ValueError('invalid_workflow_kind')
    store=workflow_store()
    existing=store.find_request(request_id)
    if existing:
        return store.create(kind,target,engine,body.get('instruction'),run_at,
            source_thread_id=source,source_engine=source_engine,title=body.get('title') or '',
            request_id=request_id)
    if workflow_thread(target,engine) is None:raise ValueError('目标会话不存在或已归档')
    if source and workflow_thread(source,source_engine) is None:raise ValueError('来源会话不存在或已归档')
    return store.create(kind,target,engine,body.get('instruction'),run_at,
        source_thread_id=source,source_engine=source_engine,title=body.get('title') or '',
        event_cursor=RECOVERY_JOURNAL.max_seq() if RECOVERY_JOURNAL is not None else 0,
        request_id=request_id)


def reconcile_submission(tid, cid):
    from recovery_projection import receipt_from_events
    events = []
    if HUB is not None:
        with HUB.lock:
            payloads = [payload for _, payload in HUB.history]
        for payload in payloads:
            event = json.loads(payload)
            if (event.get('params') or {}).get('threadId') == tid:
                events.append(event)
    receipt = receipt_from_events(events, tid, cid)
    if receipt:
        if receipt[0].get('mode') == 'started':
            from queue_intents import reconcile_started
            receipt = (reconcile_started(queue_intent_store(), tid, cid, receipt[0]), receipt[1])
        return receipt
    if RECOVERY_JOURNAL is not None:
        receipt = receipt_from_events(RECOVERY_JOURNAL.events_after(0, tid=tid), tid, cid)
        if receipt:
            if receipt[0].get('mode') == 'started':
                from queue_intents import reconcile_started
                receipt = (reconcile_started(queue_intent_store(), tid, cid, receipt[0]), receipt[1])
            return receipt
    # Codex's native queue is the durable outbox. This is the key recovery
    # signal when the app-server acknowledged enqueue before the live event.
    if AS is not None:
        try:
            queued, position, length = AS.queue_find_client_submission(tid, cid)
        except Exception:
            queued, position, length = None, 0, 0
        if queued is not None:
            return ({'mode':'queued','reconciled':True,'clientUserMessageId':cid,
                     'queueId':queued.get('id'),'position':position,'queueLen':length},200)
    from contextlib import closing
    try:
        with closing(sqlite3.connect(QUEUE_DB.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            rows = db.execute('SELECT id,payload_json FROM queued_items WHERE thread_id=? ORDER BY queue_order LIMIT 100', (tid,)).fetchall()
    except sqlite3.Error:
        rows = []
    for index, (queue_id, raw) in enumerate(rows, 1):
        payload = json.loads(raw)
        if payload.get('clientUserMessageId') == cid or payload.get('client_id') == cid:
            return ({'mode':'queued', 'reconciled':True, 'clientUserMessageId':cid,
                     'queueId':queue_id, 'position':index, 'queueLen':len(rows)}, 200)
    for engine in (CLAUDE, WB):
        if engine is None:
            continue
        for index, item in enumerate(engine.queues.get(tid) or [], 1):
            if item.get('cid') == cid:
                return ({'mode':'queued','reconciled':True,'clientUserMessageId':cid,
                         'queueId':item.get('id'),'position':index,'engine':engine.ENGINE}, 200)
    item = _index_thread(tid) or {}
    if item.get('rolloutPath'):
        from history_timeline import read_timeline_window
        history = read_timeline_window(item['rolloutPath'], limit=100) or {}
        for turn in history.get('turns') or []:
            for message in turn.get('items') or []:
                if message.get('clientId') == cid:
                    from queue_intents import reconcile_started
                    receipt = {'mode':'started', 'reconciled':True,'clientUserMessageId':cid,
                               'result':{'turn':{'id':turn['id'],'status':turn.get('status')}}}
                    return (reconcile_started(queue_intent_store(), tid, cid, receipt), 200)
    return None


class Hub:
    """SSE 订阅中心: 每客户端一个队列, 慢客户端丢最旧。"""

    def __init__(self, journal=None):
        self.lock = threading.Lock()
        self.subs = {}
        self.seq = 0
        self.event_seq = 0          # monotonic id per broadcast (NOT the subscription id)
        self.next_seq = 0
        self.history = deque()      # (seq, payload) ring used to replay after a tunnel drop
        self.history_bytes = 0
        self.journal = journal
        self.persist_queue = queue.Queue(maxsize=20000)
        self.persist_pending = threading.Event()
        self.persist_error = None
        self.backlog_dropped = 0
        self.persisted_seq = journal.max_seq() if journal else 0
        self.recovered_events = 0
        self.runtime_pending = {}
        self.runtime_lock = threading.Lock()
        self.writer_stop = threading.Event()
        if journal:
            self.event_seq = journal.max_seq()
            self.next_seq = self.event_seq
            for event in journal.load_recent():
                payload = json.dumps(event, ensure_ascii=False)
                self.history.append((event['seq'], payload))
                self.history_bytes += len(payload)
                self.recovered_events += 1
            self.writer = threading.Thread(target=self._persist_loop, daemon=True)
            self.writer.start()

    def _persist_loop(self):
        while not self.writer_stop.is_set():
            try:
                events = [self.persist_queue.get(timeout=.5)]
            except queue.Empty:
                events = []
            if events and self.persist_queue.empty():
                time.sleep(.01)
            while len(events) < 100:
                try:
                    events.append(self.persist_queue.get_nowait())
                except queue.Empty:
                    break
            while True:
                try:
                    self.journal.append_many(events)
                    for event in events:
                        params = event.get('params') or {}
                        tid = params.get('threadId') or event.get('tid')
                        if SUBMISSION_STORE is not None and tid:
                            from recovery_projection import receipt_from_events
                            item = params.get('item') or {}
                            cid = item.get('clientId') or item.get('client_id') or item.get('clientUserMessageId')
                            if cid:
                                receipt = receipt_from_events([event], tid, cid)
                                if receipt:
                                    SUBMISSION_STORE.confirm(tid, cid, receipt[0], receipt[1])
                    with self.runtime_lock:
                        checkpoints = dict(self.runtime_pending)
                        self.runtime_pending.clear()
                    for tid, state in checkpoints.items():
                        self.journal.save_runtime(tid, state)
                    self.persisted_seq = max(self.persisted_seq, max((event['seq'] for event in events),default=0))
                    for event in events:
                        self._publish(event)
                    self.persist_error = 'backlog_full' if self.backlog_dropped else None
                    break
                except Exception as exc:
                    with self.runtime_lock:
                        for tid, state in checkpoints.items():
                            pending = self.runtime_pending.get(tid) or {}
                            if int(state.get('version') or 0) >= int(pending.get('version') or 0):
                                self.runtime_pending[tid] = state
                    self.persist_error = type(exc).__name__
                    log('event persistence retry: '+self.persist_error)
                    time.sleep(1)
            for event in events:
                self.persist_queue.task_done()
            self.persist_pending.set()

    def flush(self, timeout=5):
        deadline = time.monotonic()+timeout
        while (self.persist_queue.unfinished_tasks or self.runtime_pending) and time.monotonic() < deadline:
            self.persist_pending.wait(.05)
            self.persist_pending.clear()
        return not self.persist_queue.unfinished_tasks and not self.runtime_pending and not self.persist_error

    def checkpoint(self, tid, state):
        if self.journal:
            with self.runtime_lock:
                previous = self.runtime_pending.get(tid) or {}
                if int(state.get('version') or 0) >= int(previous.get('version') or 0):
                    self.runtime_pending[tid] = dict(state)

    def close(self):
        self.flush()
        self.writer_stop.set()
        if self.journal:
            self.writer.join(timeout=2)
            self.journal.close()

    def recovery_status(self):
        return {'enabled':self.journal is not None, 'recoveredEvents':self.recovered_events,
                'head':self.event_seq, 'persistedThrough':self.persisted_seq,
                'pendingWrites':self.persist_queue.unfinished_tasks, 'droppedEvents':self.backlog_dropped,
                'error':self.persist_error}

    def subscribe(self):
        with self.lock:
            self.seq += 1
            sid = self.seq
            q = queue.Queue(maxsize=12000)
            self.subs[sid] = q
        return sid, q

    def unsubscribe(self, sid):
        with self.lock:
            self.subs.pop(sid, None)

    def subscribe_since(self, last):
        """Subscribe and return everything after `last` (tunnel-drop replay).

        Returns (sid, q, backlog, gap, oldest, head). The history snapshot and
        the live queue are attached under the same lock, so the backlog ends
        exactly where live delivery begins: no hole, no duplicate.
        """
        with self.lock:
            self.seq += 1
            sid = self.seq
            q = queue.Queue(maxsize=12000)
            self.subs[sid] = q
            head = self.event_seq
            oldest = self.history[0][0] if self.history else 0
            backlog = []
            gap = False
            if last and last > 0:
                if last > head:
                    gap = True
                    last = 0          # gateway restarted: client seq belongs to the old process
                elif last > 0:
                    if last < oldest - 1 or not self.history:
                        gap = True    # already evicted from the ring, client must re-sync
                    else:
                        backlog = [pl for sq, pl in self.history if sq > last]
        return sid, q, backlog, gap, oldest, head

    def broadcast(self, event: dict):
        # Numbering, buffering and fan-out happen under one lock: two broadcasts
        # must never reach a client out of order, or the client sees a seq jump.
        try:
            _enrich_runtime_notification(event)
        except Exception:
            pass
        with self.lock:
            self.next_seq += 1
            seq = self.next_seq
            ev = dict(event)
            ev["seq"] = seq
            ev['journalEpoch'] = _RUNTIME_EPOCH
            if self.journal:
                try:
                    self.persist_queue.put(ev, timeout=1)
                except queue.Full:
                    self.backlog_dropped += 1
                    self.persist_error = 'backlog_full'
                return
        self._publish(ev)

    def _publish(self, ev):
        with self.lock:
            seq = ev['seq']
            self.event_seq = max(self.event_seq, seq)
            payload = json.dumps(ev, ensure_ascii=False)
            self.history.append((seq, payload))
            self.history_bytes += len(payload)
            while self.history and (len(self.history) > HUB_HISTORY_MAX
                                    or self.history_bytes > HUB_HISTORY_BYTES):
                _old_seq, old = self.history.popleft()
                self.history_bytes -= len(old)
            subs = list(self.subs.values())
            for q in subs:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except Exception:
                        pass


_WS_DELTA_METHODS = frozenset((
    "item/agentMessage/delta",
    "item/reasoning/textDelta",
    "item/reasoning/summaryTextDelta",
    "item/commandExecution/outputDelta",
))


def _ws_merge_payloads(raws):
    """同一帧窗口内, 把同一 item 的连续 delta 合成一条。

    app-server / Claude 的 delta 是逐 token 的: 一个长回答能到每秒数百条。
    每条一个 WS 帧会让手机端每帧都跑一次 JSON.parse + DOM 更新, 隧道下还会
    放大成可见的卡顿。这里只合并「连续且同 method/thread/item」的 delta,
    中间夹着任何其它事件就断开, 保证事件顺序不变、不跨 item 串字。
    """
    out = []
    for raw in raws:
        try:
            ev = json.loads(raw)
        except Exception:
            out.append([None, None, raw, False])
            continue
        if (not isinstance(ev, dict) or ev.get("kind") != "notification"
                or ev.get("method") not in _WS_DELTA_METHODS):
            out.append([None, None, raw, False])
            continue
        p = ev.get("params") or {}
        d = p.get("delta")
        if not isinstance(d, str):
            out.append([None, None, raw, False])
            continue
        iid = p.get("itemId")
        tid_ = p.get("threadId")
        if not isinstance(iid, str) or not iid or not isinstance(tid_, str) or not tid_:
            # 缺 itemId/threadId 的 delta 不能作为合并键: 否则两个不同 item 的
            # 增量会被拼成一段, 表现为手机端"串字"。宁可多条帧, 不可错序。
            out.append([None, None, raw, False])
            continue
        key = (ev.get("method"), tid_, iid)
        if out and out[-1][0] == key and isinstance(out[-1][1], dict):
            previous = out[-1][1]
            if 'deltaParts' not in previous['params']:
                previous['params']['deltaParts'] = [{'seq':previous.get('seq'), 'delta':previous['params']['delta']}]
                previous['firstSeq'] = previous.get('seq')
            previous['params']['deltaParts'].append({'seq':ev.get('seq'), 'delta':d})
            out[-1][1]["params"]["delta"] += d
            _sq = ev.get("seq")
            if isinstance(_sq, int):
                out[-1][1]["seq"] = _sq    # merged frame carries the newest seq
            out[-1][3] = True
            continue
        out.append([key, ev, raw, False])
    return [(json.dumps(x[1], ensure_ascii=False) if x[3] else x[2]) for x in out]


class CodexAS:
    """长驻 codex app-server 子进程 + JSON-RPC 客户端。"""

    def __init__(self, hub: Hub):
        self.hub = hub
        from session_handoff import HandoffPolicy
        self.control = HandoffPolicy(APP_DIR / '_run' / 'session-control.json')
        self.proc = None
        self.wlock = threading.Lock()      # stdin 写锁
        self.plock = threading.Lock()      # pending 表锁
        self.pending = {}                  # id(int) -> {"ev","resp"}
        self.srv_reqs = {}                 # str(id) -> {"method","params","ts"}
        self.next_id = 0
        self.policy_ok = set()
        self.nexus_created = set()
        self.policy_fail_at = {}
        self.resumed = set()               # 已 resume 的 threadId
        self.last_activity = {}            # threadId -> 最近一次手机侧活动时间
        self.current_turn = {}             # threadId -> turnId (running)
        self.thread_provider = {}          # threadId -> 当前 provider(configured-provider / 主配置)
        self.thread_model = {}             # threadId -> 最近一次确认/切换的模型
        self.thread_effort = {}            # threadId -> 最近一次确认/切换的思考程度
        self.pending_switch = {}           # threadId -> (model, provider, effort) 等空闲再切
        self.forced_stop = {}              # threadId -> ts(刚被停止, 30s 内不信 rollout 尾部)
        self.queued_locally = set()        # 手机侧入过队的 tid(队列泵枚举兜底)
        self.status = "starting"
        self.ready = threading.Event()
        self.restart_lock = threading.Lock()
        self.started_once = threading.Event()

    # ---------- 生命周期 ----------
    def start(self):
        exe = find_codex_exe()
        log(f"spawning app-server: {exe}")
        _env1 = dict(os.environ)
        self.proc = subprocess.Popen(
            # Unsubscribe must drop the writer before reporting handoff success.
            [exe, "-c", "thread_unload_delay_secs=0", "app-server"],
            env=_env1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=(os.name != "nt"),
        )
        self.status = "initializing"
        self.resumed.clear()
        self.policy_ok.clear()
        self.policy_fail_at.clear()
        self.last_activity.clear()
        self.current_turn.clear()
        with self.plock:
            self.pending.clear()
            self.srv_reqs.clear()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._err_reader, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        r = self.request("initialize", {
            "clientInfo": {"name": "Agent Nexus", "title": "Agent Nexus", "version": "1.2.0"},
            # experimentalApi: 打开 thread/queue/* 队列接管通道(写锁被占时把消息交给对方执行)
            "capabilities": {"experimentalApi": True},
        }, timeout=30)
        self.send_notification("initialized", {})
        self.status = "ready"
        self.ready.set()
        log(f"app-server ready: {r.get('userAgent')}")
        self.hub.broadcast({"kind": "gateway", "status": "ready"})

    def _on_dead(self, why: str):
        if self.restart_lock.locked():
            return
        with self.restart_lock:
            if self.proc and self.proc.poll() is None:
                return  # 还活着(误报)
            self.status = "dead"
            self.ready.clear()
            log(f"app-server dead: {why}")
            self.hub.broadcast({"kind": "gateway", "status": "dead", "reason": why})
            time.sleep(2)
            try:
                self.start()
            except Exception as exc:
                log(f"restart failed: {exc}")
                self.status = "dead"
                self.hub.broadcast({"kind": "gateway", "status": "dead", "reason": str(exc)})

    def _watchdog(self):
        proc = self.proc
        while True:
            time.sleep(3)
            if proc is None or proc.poll() is not None:
                break
            if self.proc is not proc:
                return  # 已被新一代替代
        self._on_dead(f"exit code {proc.poll() if proc else 'n/a'}")

    RELEASE_IDLE_SECS = 120

    def _reaper(self):
        """手机侧空闲的已加载会话 → thread/unsubscribe → app-server 卸载 → 写锁还给桌面。"""
        proc = self.proc
        while self.proc is proc and proc.poll() is None:
            time.sleep(15)
            now = time.time()
            for tid, ts in list(self.last_activity.items()):
                if tid not in self.resumed:
                    self.last_activity.pop(tid, None)
                    continue
                if tid in self.current_turn or now - ts < self.RELEASE_IDLE_SECS:
                    continue
                try:
                    self.request("thread/unsubscribe", {"threadId": tid}, timeout=15)
                    self.resumed.discard(tid)
                    self.policy_ok.discard(tid)
                    self.last_activity.pop(tid, None)
                    log(f"reaper: released thread {tid[:8]} (idle {int(now - ts)}s)")
                except ApiError as exc:
                    log(f"reaper: unsubscribe {tid[:8]} failed: {exc.err}")

    def ensure_resumed(self, tid: str):
        """Resume without reclaiming an explicitly handed-off conversation."""
        if self.control.is_desktop(tid):
            raise ApiError({'code':'takeover_required','message':'已交给电脑端，请先点“接管到手机”。'})
        if tid in self.resumed:
            self._ensure_nexus_policy(tid)
            return
        for attempt in range(4):
            try:
                params = {"threadId": tid}
                if tid in self.nexus_created or nexus_thread_owned(tid):
                    params["sandbox"] = NEXUS_SANDBOX_MODE
                    params["approvalPolicy"] = NEXUS_APPROVAL_POLICY
                params.update(think_zh_params(tid))
                _rr = self.request("thread/resume", params, timeout=60)
                _th = (_rr or {}).get("thread") or {}
                _pv = _th.get("modelProvider") or model_provider_of(_th.get("model") or "")
                if _pv:
                    self.thread_provider[tid] = _pv
                _mdl = _th.get("model")
                if _mdl:
                    self.thread_model[tid] = str(_mdl)
                _eff = _th.get("reasoningEffort") or _th.get("effort")
                if _eff:
                    self.thread_effort[tid] = str(_eff)
                if "developerInstructions" in params:
                    think_zh_mark(tid)
                self.resumed.add(tid)
                self._ensure_nexus_policy(tid)
                return
            except ApiError as exc:
                err = exc.err
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
                if isinstance(msg, str) and "is closing" in msg and attempt < 3:
                    time.sleep(2)
                    continue
                raise

    def _ensure_nexus_policy(self, tid: str):
        if tid in self.policy_ok:
            return
        if tid not in self.nexus_created and not nexus_thread_owned(tid):
            self.policy_ok.add(tid)
            return
        if tid in self.current_turn:
            return
        now = time.time()
        if now - self.policy_fail_at.get(tid, 0.0) < 30:
            return
        try:
            self.request("thread/settings/update", {
                "threadId": tid,
                "sandboxPolicy": nexus_sandbox_policy(),
                "approvalPolicy": NEXUS_APPROVAL_POLICY,
            }, timeout=15)
            self.policy_ok.add(tid)
            self.policy_fail_at.pop(tid, None)
            log(f"nexus sandbox: {tid[:8]} -> danger-full-access + never")
        except ApiError as exc:
            self.policy_fail_at[tid] = time.time()
            log(f"nexus sandbox {tid[:8]} skipped: {exc.err}")


    def provider_hint(self, tid: str) -> str:
        """会话当前 provider；旧会话没有记录时按主配置 provider 推断。"""
        return self.thread_provider.get(tid) or (_local_cfg().get("provider") or "")

    def switch_thread_model(self, tid: str, model: str, provider: str, effort: str = "") -> dict:
        """跨 provider 切模型：thread/settings/update 不认 modelProvider，只能解绑重挂。

        2026-10-20 实测：thread/unsubscribe -> thread/resume(model+modelProvider)
        之后下一回合确实走新 provider 并正常出字。
        """
        expect = model_provider_of(model)
        if provider and expect and provider != expect:
            raise ApiError({"code": "bad_model_provider", "message": "模型 %s 属于通道 %s，不能配到 %s" % (model, expect, provider)})
        # New threads have no rollout until their first turn is persisted.
        # Same-provider changes are supplied explicitly on turn/start.
        if True:  # always unload: loaded thread ignores resume overrides
            try:
                self.request("thread/unsubscribe", {"threadId": tid}, timeout=15)
            except ApiError as exc:
                log("provider switch: unsubscribe %s skipped: %s" % (tid[:8], _err_text(exc)))
            self.resumed.discard(tid)
            self.policy_ok.discard(tid)
        nexus_owned = tid in self.nexus_created or nexus_thread_owned(tid)
        params = {"threadId": tid, "model": model, "modelProvider": provider}
        if nexus_owned:
            params["sandbox"] = NEXUS_SANDBOX_MODE
            params["approvalPolicy"] = NEXUS_APPROVAL_POLICY
        params.update(think_zh_params(tid))
        res = {}
        last_exc = None
        for attempt in range(4):
            try:
                res = self.request("thread/resume", params, timeout=60)
                break
            except ApiError as exc:
                last_exc = exc
                msg = _err_text(exc)
                if "is closing" in msg and attempt < 3:
                    time.sleep(2)
                    continue
                raise
        else:
            raise last_exc
        if "developerInstructions" in params:
            think_zh_mark(tid)
        self.resumed.add(tid)
        _th = (res or {}).get("thread") or {}
        if isinstance(_th, dict):
            _got_pv = str(_th.get("modelProvider") or "")
            _got_md = str(_th.get("model") or "")
            if _got_pv and _got_pv != provider:
                raise ApiError({"code": "rebind_verify_failed", "message": "rebind 未生效: 期望 %s/%s 实际 %s/%s" % (model, provider, _got_md, _got_pv)})
        self._ensure_nexus_policy(tid)
        if effort:
            self.request("thread/settings/update", {"threadId": tid, "effort": effort}, timeout=30)
            _wait_codex_settings(tid, model, effort, allow_missing=tid in self.nexus_created)
            self.thread_effort[tid] = effort
        self.thread_provider[tid] = provider
        self.thread_model[tid] = model
        self.last_activity[tid] = time.time()
        log("provider switch %s: %s -> %s" % (tid[:8], model, provider))
        return res or {}

    def queue_add(self, tid: str, text: str, client_id: str = None, extra_input=None) -> dict:
        """持久入队；Codex 空闲时可能立刻派发，忙时留队由 QueuePump 推进。

        queue/add ACK 只证明消息被接受；开始状态必须另查队列/事件/历史。
        """
        cid = client_id or ("ar-" + uuid.uuid4().hex)
        inp = []
        if text:
            inp.append({"type": "text", "text": text, "text_elements": []})
        if extra_input:
            inp.extend(extra_input)
        return self.request("thread/queue/add", {
            "threadId": tid,
            "input": inp,
            "clientUserMessageId": cid,
        }, timeout=30)

    # ---------- 共享队列(thread/queue/*) ----------
    # 实测(2026-10-16; 反编译 queue_service.rs + 运行时验证):
    #   * queue/add: 会话**空闲**时 codex 会立刻执行它; 会话**忙**时只入库,
    #     而且回合正常结束后 **不会** 自动推进(codex 的空闲派发只由 enqueue 触发,
    #     不挂在回合完成事件上; 中断(Interrupted)更是显式跳过)。
    #   * thread/queue/start = "空闲就开跑"的原子原语:
    #         忙     -> InvalidRequest("thread already has an active or pending turn"), 不丢消息
    #         空闲   -> 真开跑, 并把该条从队列删除
    #         未加载 -> InvalidRequest("resume the thread before starting a queued message")
    #     回包可能延迟到回合真正开跑之后 → 调用方要容忍 timeout, 用 queue_list 复验。
    #   * 队列推进统一由 QueuePump 轮询负责。

    def queue_list(self, tid: str) -> list:
        """该会话待执行的排队消息(会话未加载也能读)。"""
        res = self.request("thread/queue/list", {"threadId": tid, "limit": 50}, timeout=20)
        return res.get("data") or []

    def queue_find_client_submission(self, tid: str, client_id: str):
        """Find durable enqueue evidence across native queue pages."""
        from codex_dispatch import find_in_pages
        def fetch(cursor):
            params = {"threadId": tid, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            return self.request("thread/queue/list", params, timeout=20)
        try:
            return find_in_pages(fetch, client_id)
        except ValueError as exc:
            code = str(exc)
            message = ("Codex 队列分页游标重复，等待核对" if code == 'queue_cursor_cycle'
                       else "Codex 队列超过核对上限，等待刷新")
            raise ApiError({"code": code, "message": message}) from exc

    def queue_delete(self, tid: str, qid: str) -> bool:
        res = self.request("thread/queue/delete",
                           {"threadId": tid, "queuedSubmissionId": qid}, timeout=20)
        return bool(res.get("deleted"))

    def queue_reorder(self, tid: str, ids) -> None:
        self.request("thread/queue/reorder",
                     {"threadId": tid, "queuedSubmissionIds": [str(i) for i in ids]}, timeout=20)

    def queue_start(self, tid: str, qid=None) -> dict:
        params = {"threadId": tid}
        if qid:
            params["queuedSubmissionId"] = qid
        return self.request("thread/queue/start", params, timeout=25)

    def release(self, tid: str) -> bool:
        """Block automatic reacquisition, unsubscribe and verify native unload."""
        from session_handoff import unsubscribe_verified
        with self.control.guard(tid):
            if tid in self.current_turn:
                raise ApiError({'code':'handoff_busy','message':'本会话仍在运行，请先停止或等回合完成，再交还电脑。'})
            if self.queue_list(tid):
                raise ApiError({'code':'handoff_queue_pending','message':'还有排队消息，请先处理或取消，再交还电脑。'})
            with self.plock:
                pending=list(self.srv_reqs.values())
            if any(((item.get('params') or {}).get('threadId') or
                    (item.get('params') or {}).get('thread_id'))==tid for item in pending):
                raise ApiError({'code':'handoff_approval_pending','message':'还有待确认操作，请先处理，再交还电脑。'})
            self.control.set_desktop(tid)
            released=unsubscribe_verified(self.request,tid)
            if not released:
                invalidate_lock_caches(tid)
                return False
            self.resumed.discard(tid)
            self.policy_ok.discard(tid)
            self.last_activity.pop(tid, None)
            self.queued_locally.discard(tid)
            self.pending_switch.pop(tid,None)
            invalidate_lock_caches(tid)
            return True

    def takeover(self,tid):
        with self.control.guard(tid):
            info=thread_lock_info(tid,wait=.1)
            ours=any(owner.get('ours') for owner in info.get('owners') or [])
            if info.get('held') and not ours:
                raise ApiError({'code':'writer_locked','message':'电脑端仍持有会话，请先在电脑结束或关闭该会话。','lock':info})
            desktop=self.control.is_desktop(tid)
            self.control.set_desktop(tid,False)
            try:
                self.ensure_resumed(tid)
            except Exception:
                if desktop:self.control.set_desktop(tid)
                raise
            invalidate_lock_caches(tid)
            return {'ok':True,'takenOver':True,'verified':True,'control':'phone'}

    # ---------- 读写 ----------
    def _write(self, obj: dict):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send_notification(self, method: str, params: dict):
        with self.wlock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, timeout: float = 90) -> dict:
        tid=(params or {}).get('threadId')
        if tid and method in ('thread/resume','turn/start','thread/queue/add','thread/queue/start',
                               'thread/settings/update','thread/compact/start'):
            with self.control.guard(tid):
                if self.control.is_desktop(tid):
                    raise ApiError({'code':'takeover_required','message':'已交给电脑端；请先点“接管到手机”再执行。'})
                return self._request(method,params,timeout)
        return self._request(method,params,timeout)

    def _request(self, method: str, params: dict, timeout: float = 90) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            raise ApiError({"code": "appserver_down", "message": "app-server 未运行"})
        with self.wlock:
            self.next_id += 1
            mid = self.next_id
            ev = threading.Event()
            with self.plock:
                self.pending[mid] = {"ev": ev, "resp": None}
            self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        if not ev.wait(timeout):
            with self.plock:
                self.pending.pop(mid, None)
            raise ApiError({"code": "timeout", "message": f"{method} 超时"})
        with self.plock:
            entry = self.pending.pop(mid, None)
        resp = (entry or {}).get("resp")
        if resp is None:
            raise ApiError({"code": "no_response", "message": method})
        if "error" in resp:
            raise ApiError(resp["error"])
        return resp.get("result") or {}

    def respond(self, req_id, result=None, error=None) -> bool:
        key = str(req_id)
        with self.plock:
            if key not in self.srv_reqs:
                return False
        with self.wlock:
            msg = {"jsonrpc": "2.0", "id": req_id}
            if error is not None:
                msg["error"] = error
            else:
                msg["result"] = result if result is not None else {}
            self._write(msg)
        with self.plock:
            self.srv_reqs.pop(key, None)
        return True

    def _reader(self):
        proc = self.proc
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if "method" in obj:
                if "id" in obj:
                    # server -> client 请求(审批等)
                    with self.plock:
                        self.srv_reqs[str(obj["id"])] = {
                            "method": obj.get("method"),
                            "params": obj.get("params") or {},
                            "ts": time.time(),
                        }
                    self.hub.broadcast({
                        "kind": "request",
                        "id": obj.get("id"),
                        "method": obj.get("method"),
                        "params": obj.get("params") or {},
                    })
                else:
                    m = obj.get("method")
                    p = obj.get("params") or {}
                    tid = p.get("threadId")
                    if m == "turn/started" and tid:
                        turn = p.get("turn") or {}
                        self.forced_stop.pop(tid, None)     # 新回合真开跑: 停止窗口结束
                        _run_cache[tid] = (time.time(), True)
                        if turn.get("id"):
                            self.current_turn[tid] = turn["id"]
                    elif m in ("turn/completed", "turn/failed") and tid:
                        completed_id = (p.get('turn') or {}).get('id') or p.get('turnId')
                        if completed_id and self.current_turn.get(tid) and self.current_turn[tid] != completed_id:
                            continue
                        self.current_turn.pop(tid, None)
                        self.forced_stop.pop(tid, None)
                        self.last_activity[tid] = time.time()
                        # Keep an explicit false hint. Popping here falls back to a
                        # stale rollout tail that may still say this turn is running.
                        _run_cache[tid] = (time.time(), False)
                        try:
                            if PUMP is not None:
                                PUMP.kick(tid)         # 下一条排队消息: 立刻发, 不等节拍
                        except Exception:
                            pass
                    elif m == "thread/closed" and tid:
                        self.resumed.discard(tid)
                        self.policy_ok.discard(tid)
                        self.last_activity.pop(tid, None)
                        try:
                            invalidate_lock_caches(tid)
                        except Exception:
                            pass
                        # 会话已关 = 不可能还在跑: 这里必须清掉 run 标记,
                        # 否则 current_turn 永久残留 -> thread_running 恒真 -> 队列永远推不动。
                        if self.current_turn.pop(tid, None) is not None:
                            log(f"thread/closed {tid[:8]}: run flag cleared")
                        _run_cache[tid] = (time.time(), False)
                        try:
                            if PUMP is not None:
                                PUMP.kick(tid)
                        except Exception:
                            pass
                    elif m == "serverRequest/resolved":
                        rid = p.get("requestId")
                        if rid is not None:
                            with self.plock:
                                self.srv_reqs.pop(str(rid), None)
                    # error / mcp 通知以前只广播不落盘 -> 用户报"后端错误"时日志里查不到。
                    # 补一条瘦日志(截断, 防大 JSON 灌爆 gateway.log)。
                    if m == "error" or m.startswith("mcp"):
                        try:
                            _brief = json.dumps(p, ensure_ascii=False)
                        except Exception:
                            _brief = str(p)
                        log("[notify:%s] %s" % (m, _brief[:400]))
                    self.hub.broadcast({"kind": "notification", "method": m, "params": p})
            else:
                mid = obj.get("id")
                with self.plock:
                    entry = self.pending.get(mid)
                    if entry is not None:
                        entry["resp"] = obj
                        entry["ev"].set()
        self._on_dead("stdout closed")

    def _err_reader(self):
        proc = self.proc
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log(f"[app-server] {line[:300]}")


def _norm_item_images(items):
    """Codex 桌面端贴的图在线程 item 里是 content 里的 dataURL(image/input_image):
    落盘换成 localImage path, 手机端才渲染得出(同时也把大 base64 从响应里摘掉)。"""
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        cont = item.get("content")
        if not isinstance(cont, list):
            continue
        out = []
        for c in cont:
            if isinstance(c, dict) and c.get("type") in ("image", "input_image"):
                url = c.get("url") or c.get("image_url") or c.get("imageUrl") or ""
                pth = _save_dataurl_cached(url)
                if pth:
                    out.append({"type": "localImage", "path": pth})
                continue          # 落盘失败(非 dataURL / 超大): 丢掉, 别把 base64 发给手机
            out.append(c)
        item["content"] = out


def _item_size(item) -> int:
    try:
        return len(json.dumps(item, ensure_ascii=False))
    except Exception:
        return 0


def _thin_turn_items(turn, items, idx, max_items=200, turn_bytes=900000) -> None:
    """Keep user messages and a bounded output tail in their original order."""
    dicts = [it for it in items if isinstance(it, dict)]
    if len(dicts) <= 1:
        return
    users = {i for i, item in enumerate(dicts) if item.get("type") == "userMessage"}
    budget = turn_bytes - sum(_item_size(dicts[i]) for i in users)
    kept = set(users)
    outputs = 0
    for i in range(len(dicts) - 1, -1, -1):
        if i in users:
            continue
        if outputs >= max(1, max_items - len(users)):
            break
        sz = _item_size(dicts[i])
        if sz > budget and outputs:
            break
        kept.add(i)
        outputs += 1
        budget -= sz
    dropped = len(dicts) - len(kept)
    if dropped <= 0:
        return
    turn["itemsElided"] = dropped
    result = []
    gap = 0
    base = turn.get("id") or ("i%d" % idx)
    for i, item in enumerate(dicts):
        if i not in kept:
            gap += 1
            continue
        if gap:
            result.append({"id": "elided-%s-%s" % (base, i - gap),
                           "type": "elided", "count": gap})
            gap = 0
        result.append(item)
    turn["items"] = result


def trim_thread(thread: dict, light: bool = False) -> dict:
    """控制单线程 detail 的体积(三层预算: 回合数 / 单回合条目+字节 / 全线程总数)。

    超长自动回合实测 4591 条 / 20MB(第02期长跑会话) —— 手机端要建几千个 DOM
    节点直接卡死。这里按「保留最新」裁剪, 被裁部分在回合头部插一条 elided
    标记(UI 显示一行系统提示); 电脑端不受影响(直读会话文件)。
    """
    _b = TRIM_LIGHT if light else TRIM_FULL
    max_turns = _b["turns"]
    max_output = _b["output"]
    max_items = _b["items_per_turn"]
    turn_bytes = _b["turn_bytes"]
    total_bytes = _b["total_bytes"]
    total_items = _b["total_items"]
    src = thread.get("turns") or []
    if len(src) > max_turns:
        src = src[-max_turns:]
        thread["turnsTrimmed"] = True
    for idx, turn in enumerate(src):
        items = turn.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            out = item.get("aggregatedOutput")
            if isinstance(out, str) and len(out) > max_output:
                item["aggregatedOutput"] = out[:max_output] + "\n...[截断]"
        _norm_item_images(items)
        _thin_turn_items(turn, items, idx, max_items, turn_bytes)
    kept = []
    total = 0
    nitems = 0
    for turn in reversed(src):
        size = sum(_item_size(it) for it in (turn.get("items") or []))
        n = len(turn.get("items") or [])
        if kept and (total + size > total_bytes or nitems + n > total_items):
            thread["turnsTrimmed"] = True
            break
        kept.append(turn)
        total += size
        nitems += n
    thread["turns"] = list(reversed(kept))
    if light:
        thread["light"] = True
    return thread


# 同一把写锁, 两个平台两套原语: Windows 用 msvcrt, macOS/Linux 用 fcntl (锁语义一致)
if os.name == "nt":
    import msvcrt
    _fcntl = None
else:
    msvcrt = None
    try:
        import fcntl as _fcntl
    except Exception:
        _fcntl = None


def _lock_is_free(fh) -> bool:
    """能拿一次排他锁 = 没别人持有(可写)。拿不到 = 电脑端正拿着写这个会话。"""
    if msvcrt is not None:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return True
        except OSError:
            return False
    if _fcntl is not None:
        try:
            _fcntl.lockf(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB, 1, 0, os.SEEK_SET)
            _fcntl.lockf(fh.fileno(), _fcntl.LOCK_UN, 1, 0, os.SEEK_SET)
            return True
        except OSError:
            return False
    return True   # 两个原语都没有 -> 不拦(宁可让 app-server 自己报错, 也别误判成电脑端占用)


_LOCK_PROBE_CACHE = {}
_LOCK_PROBE_LOCK = threading.Lock()
_LOCK_PROBE_TTL = 4.0


def probe_thread_lock(tid, fresh=False, ttl=None):
    """True = 可写(手机可接); False = 其他进程持有写锁。

    列表 / 轮询走缓存(默认 4 秒)，只有发送、中断、强制接管这类马上要写的动作
    才用 fresh=True 实时探。探锁本身很便宜；真正慢的是锁归属扫描，见
    _lock_owner_snapshot —— 那件事只在后台线程里做。
    """
    key = str(tid)
    ttl = _LOCK_PROBE_TTL if ttl is None else float(ttl)
    now = time.time()
    if not fresh:
        with _LOCK_PROBE_LOCK:
            hit = _LOCK_PROBE_CACHE.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
    path = USER_HOME / ".codex" / "thread-writer-locks" / (key + ".lock")
    result = True
    try:
        if path.exists():
            try:
                fh = open(path, "r+b")
            except OSError:
                result = False
            else:
                try:
                    free = _lock_is_free(fh)
                finally:
                    fh.close()
                if free:
                    # 进程已死但锁文件留在磁盘：安全清掉，下次不再误判。
                    try:
                        path.unlink()
                    except OSError:
                        pass
                result = free
    except OSError:
        result = True
    with _LOCK_PROBE_LOCK:
        _LOCK_PROBE_CACHE[key] = (time.time(), result)
    return result


_LOCK_OWNERS_CACHE = {}
_LOCK_OWNERS_LOCK = threading.Lock()
# 只有这两个进程会拿 thread-writer-locks。旧名单把 python / node / workbuddy
# 也扫进来，一次全表 open_files() 要 6.7 秒，直接把网关拖死。
_LOCK_OWNER_NAMES = {"codex.exe", "chatgpt.exe", "codex", "chatgpt"}
_OURS_OWNER_NAMES = {"codex.exe", "codex"}
_LOCK_SNAPSHOT_TTL = 30.0
_LOCK_SCAN_WORKERS = 16


def thread_lock_path(tid):
    return USER_HOME / ".codex" / "thread-writer-locks" / (str(tid) + ".lock")


_LOCK_SNAPSHOT = {"ts": 0.0, "map": {}, "gen": 0, "ok": False, "error": ""}
_LOCK_SNAPSHOT_LOCK = threading.Lock()
_LOCK_SNAPSHOT_EVENT = threading.Event()
_LOCK_SNAPSHOT_BUSY = False


def _scan_lock_owners():
    """后台线程专用：谁打开着 ~/.codex/thread-writer-locks/*.lock。

    两个约束保证它比旧实现快一个数量级：
      1) 只扫 codex.exe / chatgpt.exe；
      2) 只认 thread-writer-locks 目录下的 .lock，hermes / codex\tmp 的锁一律不算。
    进程之间用 8 个 worker 有界并发（open_files 是阻塞调用，串行会很慢）。
    """
    import psutil
    from concurrent.futures import ThreadPoolExecutor

    root_dir = USER_HOME / ".codex" / "thread-writer-locks"
    root = os.path.normcase(os.path.abspath(str(root_dir)))
    root_prefix = root + os.sep
    # 先看有没有真的被占用的锁。全是残留文件时直接返回，一次 open_files 都不做。
    held = []
    try:
        for lf in root_dir.glob("*.lock"):
            try:
                fh = open(lf, "r+b")
            except OSError:
                held.append(os.path.normcase(os.path.abspath(str(lf))))
                continue
            try:
                if not _lock_is_free(fh):
                    held.append(os.path.normcase(os.path.abspath(str(lf))))
            finally:
                fh.close()
    except OSError:
        return {}
    if not held:
        return {}
    held_set = set(held)
    procs = []
    # 只取 pid/name。psutil 取 cmdline 要读进程内存，537 个进程挨个读
    # 本身就要 1 秒以上；持有者的 cmdline 等匹配出来再补。
    for pr in psutil.process_iter(["pid", "name"]):
        name = (pr.info.get("name") or "").lower()
        if name in _LOCK_OWNER_NAMES:
            procs.append((pr.info.get("pid"), pr.info.get("name") or ""))

    def scan_one(item):
        pid, name = item
        rows = []
        try:
            for f in psutil.Process(pid).open_files():
                try:
                    key = os.path.normcase(os.path.abspath(f.path))
                except (OSError, ValueError):
                    continue
                if key in held_set:
                    rows.append((key, {"pid": pid, "name": name, "cmd": ""}))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
        return rows

    mp = {}
    if procs:
        workers = min(_LOCK_SCAN_WORKERS, len(procs))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lock-scan") as ex:
            for rows in ex.map(scan_one, procs):
                for key, info in rows:
                    mp.setdefault(key, []).append(info)
    # 只给真正持锁的进程补 cmdline（通常 0~3 个），用于 UI 展示。
    for infos in mp.values():
        for info in infos:
            try:
                info["cmd"] = " ".join(
                    psutil.Process(int(info.get("pid") or 0)).cmdline() or [])[:180]
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                info["cmd"] = ""
    return mp


def _refresh_lock_snapshot():
    global _LOCK_SNAPSHOT_BUSY
    mp = None
    err = ""
    try:
        mp = _scan_lock_owners()
    except Exception as exc:
        err = "%r" % (exc,)
        log("lock-owner scan failed: %s" % err)
    now = time.time()
    with _LOCK_SNAPSHOT_LOCK:
        if mp is not None:
            _LOCK_SNAPSHOT["map"] = mp
            _LOCK_SNAPSHOT["gen"] += 1
            _LOCK_SNAPSHOT["ok"] = True
            _LOCK_SNAPSHOT["error"] = ""
            _LOCK_SNAPSHOT["ts"] = now
        else:
            _LOCK_SNAPSHOT["ok"] = False
            _LOCK_SNAPSHOT["error"] = err
            _LOCK_SNAPSHOT["ts"] = now - _LOCK_SNAPSHOT_TTL + 5.0    # 5 秒后重试
        _LOCK_SNAPSHOT_BUSY = False
        _LOCK_SNAPSHOT_EVENT.set()


def _kick_lock_snapshot():
    """踢一次后台刷新；已有刷新在跑就返回 False（single-flight）。"""
    global _LOCK_SNAPSHOT_BUSY
    with _LOCK_SNAPSHOT_LOCK:
        if _LOCK_SNAPSHOT_BUSY:
            return False
        _LOCK_SNAPSHOT_BUSY = True
        _LOCK_SNAPSHOT_EVENT.clear()
    try:
        threading.Thread(target=_refresh_lock_snapshot,
                         name="lock-owner-scan", daemon=True).start()
    except Exception as exc:
        with _LOCK_SNAPSHOT_LOCK:
            _LOCK_SNAPSHOT_BUSY = False
        log("lock-owner scan start failed: %r" % (exc,))
        return False
    return True


def _lock_snapshot_read(wait=0.0):
    """读锁归属快照（纯内存）。过期就踢后台刷新，最多等 wait 秒。

    返回 (gen, map)。请求线程在这里绝不会调用 psutil.Process.open_files()。
    """
    now = time.time()
    with _LOCK_SNAPSHOT_LOCK:
        stale = (now - _LOCK_SNAPSHOT["ts"]) >= _LOCK_SNAPSHOT_TTL
        gen = _LOCK_SNAPSHOT["gen"]
        mp = _LOCK_SNAPSHOT["map"]
    if stale:
        _kick_lock_snapshot()
        if wait > 0:
            deadline = now + float(wait)
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                _LOCK_SNAPSHOT_EVENT.wait(min(left, 0.25))
                with _LOCK_SNAPSHOT_LOCK:
                    if _LOCK_SNAPSHOT["gen"] != gen or not _LOCK_SNAPSHOT_BUSY:
                        gen = _LOCK_SNAPSHOT["gen"]
                        mp = _LOCK_SNAPSHOT["map"]
                        break
    return gen, mp


def _lock_owner_snapshot(wait=0.0):
    return _lock_snapshot_read(wait=wait)[1]


def invalidate_lock_caches(tid=None):
    """锁状态变了就清缓存（force-unlock / release / thread-closed）。"""
    with _LOCK_PROBE_LOCK:
        if tid is None:
            _LOCK_PROBE_CACHE.clear()
        else:
            _LOCK_PROBE_CACHE.pop(str(tid), None)
    with _LOCK_OWNERS_LOCK:
        if tid is None:
            _LOCK_OWNERS_CACHE.clear()
        else:
            _LOCK_OWNERS_CACHE.pop(str(tid), None)


def thread_lock_owners(tid, path=None, wait=0.0):
    """锁持有者列表（查不到返回空）。只读后台快照，按快照代次 gen 自动失效。"""
    p = str(path or thread_lock_path(tid))
    key = os.path.normcase(os.path.abspath(p))
    gen, mp = _lock_snapshot_read(wait=wait)
    with _LOCK_OWNERS_LOCK:
        hit = _LOCK_OWNERS_CACHE.get(str(tid))
        if hit and hit[0] == gen:
            return list(hit[1])
        out = list(mp.get(key) or [])
        # 空的别缓存：一次扫描恰好赶上锁刚被拿走，会在整个 gen 周期里
        # 让 UI 一直显示"没有持有者"。空了就每次从内存快照重读，成本为零。
        if out:
            _LOCK_OWNERS_CACHE[str(tid)] = (gen, out)
        return list(out)


_AS_TREE_CACHE = {"ts": 0.0, "pids": set()}


def as_process_tree_pids():
    """本网关 app-server 的进程树(cmd -> node -> codex.exe), 15 秒缓存。"""
    now = time.time()
    if _AS_TREE_CACHE["pids"] and now - _AS_TREE_CACHE["ts"] < 15:
        return set(_AS_TREE_CACHE["pids"])
    pids = set()
    try:
        import psutil
        proc = getattr(AS, "proc", None)
        if proc is not None and proc.poll() is None:
            root = psutil.Process(proc.pid)
            pids.add(root.pid)
            for ch in root.children(recursive=True):
                pids.add(ch.pid)
    except Exception:
        pids = set()
    if pids:
        _AS_TREE_CACHE["ts"] = now
        _AS_TREE_CACHE["pids"] = pids
    return pids


_OURS_LOCKS = {"ts": 0.0, "paths": set(), "busy": False, "gen": 0}
_OURS_LOCKS_LOCK = threading.Lock()
_OURS_LOCKS_EVENT = threading.Event()


def _lock_path_key(tid_or_path):
    p = str(tid_or_path)
    if not p.endswith(".lock"):
        p = str(thread_lock_path(p))
    return os.path.normcase(os.path.abspath(p))


def _refresh_ours_locks():
    """后台扫一次自家进程树, 更新本网关持有的 thread-writer-locks 集合。"""
    out = set()
    root = os.path.normcase(os.path.abspath(
        str(USER_HOME / ".codex" / "thread-writer-locks"))) + os.sep
    try:
        import psutil
        for pid in as_process_tree_pids():
            try:
                pr = psutil.Process(int(pid))
                if (pr.name() or "").lower() not in _OURS_OWNER_NAMES:
                    continue
                for f in pr.open_files():
                    try:
                        key = os.path.normcase(os.path.abspath(f.path))
                    except (OSError, ValueError):
                        continue
                    if key.startswith(root) and key.endswith(".lock"):
                        out.add(key)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except Exception:
        pass
    with _OURS_LOCKS_LOCK:
        _OURS_LOCKS["ts"] = time.time()
        _OURS_LOCKS["paths"] = set(out)
        _OURS_LOCKS["gen"] += 1
        _OURS_LOCKS["busy"] = False
        _OURS_LOCKS_EVENT.set()


def _kick_ours_locks():
    with _OURS_LOCKS_LOCK:
        if _OURS_LOCKS["busy"]:
            return False
        _OURS_LOCKS["busy"] = True
        _OURS_LOCKS_EVENT.clear()
    try:
        threading.Thread(target=_refresh_ours_locks, name="ours-lock-scan",
                         daemon=True).start()
    except Exception:
        with _OURS_LOCKS_LOCK:
            _OURS_LOCKS["busy"] = False
        return False
    return True


def _ours_lock_paths(wait=0.0):
    """本网关 app-server 的 codex.exe 当前打开着哪些 thread-writer-locks。

    只扫自家进程树里的 codex.exe（通常 1 个，~110ms），10 秒缓存。
    请求线程只读内存缓存；过期就踢一次后台刷新（single-flight），
    绝不在 HTTP 线程里调用 psutil.Process.open_files()。
    wait>0 只对"从未扫过"的冷缓存生效（发送路径最多等一次），
    稳态下即使 fresh=True 也不会把请求线程挂住。
    """
    now = time.time()
    with _OURS_LOCKS_LOCK:
        ts = _OURS_LOCKS["ts"]
        paths = set(_OURS_LOCKS["paths"])
        gen = _OURS_LOCKS["gen"]
    if ts and now - ts < 10.0:
        return paths
    _kick_ours_locks()
    if wait > 0 and not ts:
        deadline = now + float(wait)
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            _OURS_LOCKS_EVENT.wait(min(left, 0.25))
            with _OURS_LOCKS_LOCK:
                if _OURS_LOCKS["gen"] != gen or not _OURS_LOCKS["busy"]:
                    paths = set(_OURS_LOCKS["paths"])
                    break
    return paths


def thread_lock_ok_for_us(tid, fresh=False):
    """写锁是否由本网关自己的 app-server 持有。

    本家的 app-server 在 thread/read 后会保留线程与写锁(thread_unload_delay=30s)，
    老逻辑把它当成"电脑端占用" -> 自己把自己锁死。自家进程持有的锁一律不拦。
    """
    try:
        if probe_thread_lock(tid, fresh=fresh):
            return True
    except Exception:
        return True
    try:
        return _lock_path_key(tid) in _ours_lock_paths(
            wait=(0.8 if fresh else 0.0))
    except Exception:
        return False          # 查不出来 -> 保守当作外部占用


def thread_writable(tid, fresh=False):
    """网关能写吗 = 已 resume, 或锁空闲, 或锁在本家 app-server 手里。

    fresh=False：列表 / 轮询用，读 4 秒探测缓存，绝不等慢 IO。
    fresh=True ：发送 / 中断 / 强制接管用，实时探锁；归属快照最多等 0.8 秒。
    """
    try:
        if AS.control.is_desktop(tid):
            return False
        if tid in AS.resumed:
            return True
    except Exception:
        pass
    return thread_lock_ok_for_us(tid, fresh=fresh)


def thread_lock_summary(tid, running=None):
    """列表专用轻量锁摘要：只给 held/active/stale，不查 owner、不碰 psutil。

    列表只需要知道"是不是被占着 / 对方是不是在跑"；完整 owner 留给单会话详情
    和强制接管。
    """
    path = thread_lock_path(tid)
    info = {"held": False, "active": False, "stale": False}
    if AS.control.is_desktop(tid):
        info.update(control='desktop',message='已交给电脑端；请先接管到手机再发送。')
    try:
        if not path.exists():
            return info
    except OSError:
        return info
    try:
        fh = open(path, "r+b")
    except OSError:
        info["held"] = True
    else:
        try:
            free = _lock_is_free(fh)
        finally:
            fh.close()
        if free:
            info["stale"] = True       # 残留文件，下一次探测会自动清掉
            return info
        info["held"] = True
    try:
        run = thread_running(tid) if running is None else bool(running)
    except Exception:
        run = False
    info["active"] = bool(run)
    info["stale"] = not bool(run)
    return info


def thread_lock_info(tid, wait=0.0):
    """锁状态: held=被占用; active=对方回合在跑; stale=有锁但对方空闲/残留。"""
    path = thread_lock_path(tid)
    info = {"path": str(path), "held": False, "active": False,
            "stale": False, "owners": []}
    if AS.control.is_desktop(tid):
        info.update(control='desktop',message='已交给电脑端；请先接管到手机再发送。')
    try:
        if not path.exists():
            return info
    except OSError:
        return info
    try:
        fh = open(path, "r+b")
    except OSError:
        info["held"] = True
    else:
        try:
            free = _lock_is_free(fh)
        finally:
            fh.close()
        if free:
            info["stale"] = True       # 残留文件，下一次探测会自动清掉
            return info
        info["held"] = True
    try:
        running = thread_running(tid)
    except Exception:
        running = False
    info["active"] = bool(running)
    info["stale"] = not bool(running)  # 有锁但没回合 = 按空闲占用处理
    info["owners"] = thread_lock_owners(tid, path, wait=wait)
    try:
        info["ours"] = _lock_path_key(path) in _ours_lock_paths(
            wait=(0.8 if wait > 0 else 0.0))
    except Exception:
        info["ours"] = False
    try:
        info["mtime"] = int(path.stat().st_mtime)
    except OSError:
        pass
    return info


def force_release_thread_lock(tid):
    """把锁文件移进 _bak-YYYYMMDD/（不杀别人的进程，只清占用标记）。"""
    path = thread_lock_path(tid)
    try:
        if not path.exists():
            return {"moved": False, "reason": "no_lock"}
    except OSError:
        return {"moved": False, "reason": "stat_failed"}
    bak = path.parent / ("_bak-" + time.strftime("%Y%m%d"))
    try:
        bak.mkdir(parents=True, exist_ok=True)
        dest = bak / (path.name + "." + time.strftime("%H%M%S"))
        os.replace(path, dest)
        return {"moved": True, "to": str(dest)}
    except OSError as exc:
        try:
            path.unlink()
            return {"moved": True, "deleted": True, "note": str(exc)}
        except OSError as exc2:
            return {"moved": False, "error": str(exc2)}


def cleanup_thread_locks():
    """启动自愈：只删“没人持有”的残留锁；被活进程持有的锁一律不动。"""
    root = USER_HOME / ".codex" / "thread-writer-locks"
    try:
        if not root.exists():
            return 0
    except OSError:
        return 0
    removed = 0
    for p in root.glob("*.lock"):
        if p.name.startswith("."):
            continue
        try:
            fh = open(p, "r+b")
        except OSError:
            continue
        try:
            free = _lock_is_free(fh)
        except Exception:
            free = False
        finally:
            fh.close()
        if free:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        log("thread-lock cleanup: removed %d stale lock file(s)" % removed)
    return removed


# ---------- rollout 尾部跟随(运行细节: 电脑端执行也能实时看到) ----------

SESSION_ROOTS = [USER_HOME / ".codex" / "sessions", USER_HOME / ".codex" / "archived_sessions"]
_rollout_cache = {}
_rollout_cache_ts = {}
_run_cache = {}
_RUNTIME_EPOCH = secrets.token_hex(8)
_RUNTIME_STATUS = {}
_RUNTIME_STATUS_LOCK = threading.RLock()


def _runtime_phase(running, writable, queue_len, missing=False, event=None):
    if missing:
        return "read_only"
    if running:
        return "running_mobile" if writable else "running_desktop"
    if event == "failed":
        return "failed"
    if event in ("stopped", "closed"):
        return "stopped"
    if event == "completed":
        return "completed"
    if queue_len:
        return "queued"
    if not writable:
        return "desktop_locked"
    return "idle"


def runtime_status_payload(tid, running, writable, queue_len=None,
                           source="snapshot", last_event=None, missing=False,
                           turn_id=None, error=None):
    """Return one versioned runtime state shared by list, detail, poll and SSE."""
    if queue_len is None:
        try:
            queue_len = int(queue_counts().get(tid, 0))
        except Exception:
            queue_len = 0
    running = bool(running)
    writable = bool(writable)
    queue_len = max(0, int(queue_len or 0))
    now_ms = int(time.time() * 1000)
    with _RUNTIME_STATUS_LOCK:
        previous = _RUNTIME_STATUS.get(tid) or {}
        event = last_event if last_event is not None else previous.get("event")
        # A generic idle notification can arrive after the terminal turn event.
        # Keep the more specific terminal state until the next turn starts.
        if last_event == "status_idle" and previous.get("event") in (
                "completed", "failed", "stopped", "closed"):
            event = previous.get("event")
        phase = _runtime_phase(
            running, writable, queue_len, bool(missing), event)
        active_turn = turn_id or previous.get('turnId')
        failure = error if phase == 'failed' else None
        if phase == 'failed' and failure is None:
            failure = previous.get('error')
        signature = (running, writable, queue_len, phase, active_turn, failure)
        changed = previous.get("signature") != signature
        if changed:
            version = int(previous.get("version") or 0) + 1
            changed_at = now_ms
        else:
            version = int(previous.get("version") or 0)
            changed_at = int(previous.get("changedAt") or now_ms)
        state = {
            "epoch": _RUNTIME_EPOCH,
            "version": version,
            "signature": signature,
            "phase": phase,
            "turnId": active_turn,
            "error": failure,
            "running": running,
            "writable": writable,
            "queueLen": queue_len,
            "source": source,
            "event": event or ("started" if running else "snapshot"),
            "changedAt": changed_at,
            "observedAt": now_ms,
        }
        _RUNTIME_STATUS[tid] = state
        result = dict(state)
    hub = globals().get('HUB')
    if changed and hub is not None and hasattr(hub,'checkpoint'):
        hub.checkpoint(tid,result)
    return result


def _runtime_run_hint(tid, fallback):
    """A fresh app-server transition wins over a stale index snapshot."""
    try:
        hit = _run_cache.get(tid)
        if hit and time.time() - hit[0] < 8:
            return bool(hit[1])
    except Exception:
        pass
    return bool(fallback)


def _enrich_runtime_notification(event):
    """Attach the same versioned runtime state to status-changing SSE events."""
    if not isinstance(event, dict) or event.get("kind") != "notification":
        return
    method = str(event.get("method") or "")
    params = event.get("params")
    if not isinstance(params, dict):
        return
    tid = params.get("threadId")
    if not tid:
        return
    turn_id = params.get('turnId') or (params.get('turn') or {}).get('id')
    if method in ('turn/completed', 'turn/failed') and turn_id:
        with _RUNTIME_STATUS_LOCK:
            previous = dict(_RUNTIME_STATUS.get(tid) or {})
        if previous.get('running') and previous.get('turnId') and previous['turnId'] != turn_id:
            params['staleTurn'] = True
            params['runtime'] = previous
            return
    running = None
    last_event = None
    if method == "turn/started":
        running = True
        last_event = "started"
        _run_cache[tid] = (time.time(), True)
    elif method in ("turn/completed", "turn/failed"):
        running = False
        status = str(((params.get("turn") or {}).get("status") or "")).lower()
        last_event = "stopped" if status in ("interrupted", "aborted", "cancelled") else (
            "failed" if method == "turn/failed" or status == "failed" else "completed")
        _run_cache[tid] = (time.time(), False)
    elif method == "thread/status/changed":
        status = str(((params.get("status") or {}).get("type") or "")).lower()
        running = status == "active"
        last_event = "status_active" if running else "status_idle"
        _run_cache[tid] = (time.time(), running)
    elif method == "thread/closed":
        running = False
        last_event = "closed"
        _run_cache[tid] = (time.time(), False)
    if running is None:
        return
    writable = True
    try:
        writable = thread_writable(tid)
    except Exception:
        pass
    try:
        queue_len = int(queue_counts().get(str(tid), 0))
    except Exception:
        queue_len = 0
    failure = None
    if last_event == 'failed':
        failure = str(((params.get('turn') or {}).get('error') or params.get('error') or {}).get('message') or '') \
            if isinstance((params.get('turn') or {}).get('error') or params.get('error') or {}, dict) else str((params.get('turn') or {}).get('error') or params.get('error') or '')
        if 'context_budget_exceeded' in failure:
            failure = '当前会话上下文超出模型预算，请压缩上下文或新建会话后继续'
        elif 'invalid_encrypted_content' in failure or 'reasoning_text' in failure:
            failure = '模型服务拒绝了思考历史格式，请检查当前 provider 的协议兼容性'
        else:
            failure = re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', failure)[:240]
    params["runtime"] = runtime_status_payload(
        tid, running, writable, queue_len,
        source="event", last_event=last_event, turn_id=turn_id, error=failure)


def find_rollout(tid: str):
    """在 sessions / archived_sessions 里找该会话的 rollout jsonl 文件。"""
    now = time.time()
    hit = _rollout_cache.get(tid)
    if hit is not None and now - _rollout_cache_ts.get(tid, 0) < 120:
        try:
            if hit.exists():
                return hit
        except OSError:
            pass
    best = None
    for root in SESSION_ROOTS:
        try:
            if not root.exists():
                continue
            for p in root.glob("**/*" + tid + "*.jsonl"):
                try:
                    if best is None or p.stat().st_mtime > best.stat().st_mtime:
                        best = p
                except OSError:
                    continue
        except OSError:
            continue
    if best is not None:
        _rollout_cache[tid] = best
        _rollout_cache_ts[tid] = now
    return best


# Codex 会话详情缓存: app-server 的 thread/read 每次都要把整份 rollout jsonl 重新解析,
# 实测 105MB 会话单次要 ~500ms(读盘 80ms + 切行 57ms + json 280ms), 而手机端每次
# 打开/回看都在打这个接口。文件没变(mtime_ns + size 一致)时直接复用上次数好的结果,
# 命中后 ~2ms。running 时最多复用 1 秒, 防止 app-server 的内存态还没落盘就被缓存冻住。
_CX_DETAIL_CACHE = {}
_CX_DETAIL_MAX = 4


def _cx_detail_stat(tid):
    p = find_rollout(tid)
    if p is None:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def cx_detail_cache_get(tid, light, running):
    ent = _CX_DETAIL_CACHE.get(tid)
    if not ent:
        return None
    try:
        if _cx_detail_stat(tid) != ent.get("key"):
            return None
    except Exception:
        return None
    if running and time.time() - ent.get("at", 0) > 1.0:
        return None
    val = ent.get("light" if light else "full")
    # 只浅拷贝外层: 缓存建好后 trim_thread 不再改嵌套结构, 调用方只写顶层三个键。
    return dict(val) if isinstance(val, dict) else None


def cx_detail_cache_put(tid, light, thread):
    try:
        key = _cx_detail_stat(tid)
    except Exception:
        return
    if key is None:
        return
    ent = _CX_DETAIL_CACHE.get(tid)
    if not ent or ent.get("key") != key:
        ent = {"key": key}
        _CX_DETAIL_CACHE[tid] = ent
    ent["at"] = time.time()
    ent["light" if light else "full"] = dict(thread) if isinstance(thread, dict) else thread
    while len(_CX_DETAIL_CACHE) > _CX_DETAIL_MAX:
        _CX_DETAIL_CACHE.pop(next(iter(_CX_DETAIL_CACHE)), None)


def _clip(s, n):
    s = s if isinstance(s, str) else str(s or "")
    return s if len(s) <= n else s[:n] + "…[截断]"


_IMG_EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                    "image/webp": ".webp", "image/gif": ".gif", "image/heic": ".heic",
                    "image/heif": ".heif", "image/bmp": ".bmp", "image/tiff": ".tiff",
                    "image/avif": ".avif"}
_IMG_MAX_BYTES = 20 * 1024 * 1024
_dataurl_img_cache = {}


def _save_dataurl_image(dataurl, prefix="img-") -> str:
    """dataURL 图片 -> gateway/uploads/<prefix><ts>-<rand><ext>; 任何异常都返回 ''(绝不抛)。"""
    if not isinstance(dataurl, str):
        return ""
    s = dataurl.strip()
    if not s.startswith("data:"):
        return ""
    head, sep, b64 = s.partition(",")
    if not sep or not b64:
        return ""
    mime = head[5:].split(";")[0].strip().lower()
    ext = _IMG_EXT_BY_MIME.get(mime, ".png")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return ""
    if not raw or len(raw) > _IMG_MAX_BYTES:
        return ""
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        fp = UPLOAD_DIR / (prefix + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ext)
        fp.write_bytes(raw)
    except OSError:
        return ""
    return str(fp)


def _save_dataurl_cached(dataurl) -> str:
    """电脑端贴的图(线程里内嵌 dataURL): 按内容 sha 去重落盘, 手机端才渲染得出,
    顺带把几百 KB 的 base64 从线程 JSON / 尾随事件里摘掉。"""
    if not isinstance(dataurl, str) or len(dataurl) < 64 or not dataurl.startswith("data:"):
        return ""
    sha = hashlib.sha1(dataurl.encode("utf-8", "ignore")).hexdigest()[:16]
    hit = _dataurl_img_cache.get(sha)
    if hit and os.path.exists(hit):
        return hit
    pth = _save_dataurl_image(dataurl, prefix="cx-")
    if pth:
        if len(_dataurl_img_cache) > 400:
            for k in list(_dataurl_img_cache)[:120]:
                _dataurl_img_cache.pop(k, None)
        _dataurl_img_cache[sha] = pth
    return pth


def _save_images(images):
    """手机端 dataURL 图片 -> 存 gateway/uploads/, 返回 (items, failed)。
    failed = 没存下的张数(超 8 张 / 空 / 解码失败 / 超 20MB); 调用方回给前端提示, 不再静默丢。"""
    out = []
    failed = 0
    if not isinstance(images, list):
        return out, failed
    for idx, im in enumerate(images):
        if not isinstance(im, dict) or idx >= 8:
            failed += 1
            continue
        pth = _save_dataurl_image(im.get("dataUrl") or im.get("dataurl") or "")
        if pth:
            out.append({"type": "localImage", "path": pth})
        else:
            failed += 1
    return out, failed


# ---------- 语音输入: 本地 faster-whisper 转写 (2026-10-15) ----------
ASR_MODEL_DIR = APP_DIR / "asr" / "models" / "faster-whisper-small"
_asr_state = {"model": None, "err": None, "lock": threading.Lock(), "load_lock": threading.Lock()}


def _asr_get_model():
    """懒加载模型(首次调用约 1-2s); 失败缓存错误, 不影响网关其余功能。"""
    if _asr_state["model"] is not None or _asr_state["err"]:
        return _asr_state["model"]
    with _asr_state["load_lock"]:
        if _asr_state["model"] is None and not _asr_state["err"]:
            try:
                from faster_whisper import WhisperModel
                _asr_state["model"] = WhisperModel(str(ASR_MODEL_DIR), device="cpu",
                                                   compute_type="int8", cpu_threads=8)
                log("asr model loaded: %s" % ASR_MODEL_DIR)
            except Exception as exc:
                _asr_state["err"] = str(exc)
                log("asr model load failed: %s" % exc)
    return _asr_state["model"]


def asr_transcribe(body: dict) -> dict:
    """{"audio": base64, "format": "mp4|webm|wav|...", "language": "zh"(默认)|auto} -> {ok,text,...}"""
    b64 = str(body.get("audio") or "")
    if not b64:
        return {"error": "empty audio"}
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception:
        return {"error": "bad base64"}
    if len(raw) > 25 * 1024 * 1024:
        return {"error": "audio too large (>25MB)"}
    model = _asr_get_model()
    if model is None:
        return {"error": "asr unavailable: " + (_asr_state["err"] or "?")}
    ext = re.sub(r"[^a-z0-9]", "", str(body.get("format") or "bin").lower())[:5] or "bin"
    tdir = APP_DIR / "_run" / "asr_tmp"
    try:
        tdir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    fp = tdir / ("clip-%s.%s" % (uuid.uuid4().hex[:10], ext))
    try:
        fp.write_bytes(raw)
        lang = body.get("language") or "zh"
        if lang == "auto":
            lang = None
        t0 = time.time()
        with _asr_state["lock"]:
            segs, info = model.transcribe(str(fp), language=lang, beam_size=5,
                                          vad_filter=False, condition_on_previous_text=False)
            text = "".join(sg.text for sg in segs).strip()
        return {"ok": True, "text": text, "language": getattr(info, "language", None),
                "secs": round(time.time() - t0, 2), "bytes": len(raw)}
    except Exception as exc:
        return {"error": "asr failed: %s" % exc}
    finally:
        try:
            os.remove(str(fp))
        except Exception:
            pass


def tail_convert(obj: dict, context=None):
    payload = obj.get('payload') or {}
    turn_id = payload.get('turn_id')
    if context is not None and turn_id:
        context['turnId'] = turn_id
    event = _tail_convert_record(obj)
    if event:
        event['turnId'] = turn_id or (context or {}).get('turnId')
    return event


def _tail_convert_record(obj: dict):
    """rollout 单行 -> 手机端运行流事件; None = 跳过。"""
    t = obj.get("type")
    if t == "response_item":
        # function_call 在命令「开始执行」时就落盘(比 item_completed 早 0.6~6s+),
        # 先发一张 运行中 命令卡; 完成事件到达后同 id 覆盖更新。
        pl = obj.get("payload") or {}
        if pl.get("type") == "function_call":
            args = pl.get("arguments")
            cmd = None
            try:
                a = json.loads(args) if isinstance(args, str) else args
                if isinstance(a, dict):
                    cv = a.get("cmd") or a.get("command")
                    if isinstance(cv, str):
                        cmd = cv
                    elif isinstance(cv, list):
                        cmd = " ".join(str(x) for x in cv)
            except Exception:
                cmd = None
            if cmd:
                return {"k": "cmd", "ts": obj.get("timestamp"), "id": pl.get("call_id"),
                        "cmd": _clip(cmd, 2000), "status": "running", "exit": None,
                        "dur": None, "out": ""}
        return None
    if t != "event_msg":
        return None
    p = obj.get("payload") or {}
    pt = p.get("type")
    ts = obj.get("timestamp")
    if pt == "task_started":
        return {"k": "turn_start", "ts": ts, "turnId": p.get("turn_id")}
    if pt in ("task_complete", "turn_aborted"):
        return {"k": "turn_end", "ts": ts, "turnId": p.get("turn_id")}
    if pt == "token_count":
        info = p.get("info") or {}
        use = info.get("last_token_usage") or info.get("total_token_usage") or {}
        if not use:
            return None
        return {"k": "usage", "ts": ts, "id": "tok",
                "in": int(use.get("input_tokens") or 0),
                "out": int(use.get("output_tokens") or 0),
                "reason": int(use.get("reasoning_output_tokens") or 0),
                "win": info.get("model_context_window")}
    if pt != "item_completed":
        return None
    it = p.get("item") or {}
    ty = it.get("type")
    if ty == "UserMessage":
        txts, imgs = [], []
        for c in (it.get("content") or []):
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("local_image", "localImage") and c.get("path"):
                imgs.append(str(c["path"]))
            elif c.get("type") in ("image", "input_image"):
                pth = _save_dataurl_cached(c.get("url") or c.get("image_url") or "")
                if pth:
                    imgs.append(pth)
            elif c.get("text"):
                txts.append(str(c["text"]))
        return {"k": "user", "ts": ts, "id": it.get("id"), "text": _clip("".join(txts).strip(), 4000),
                "images": imgs, "clientId": it.get("client_id")}
    if ty == "AgentMessage":
        txt = "".join((c or {}).get("text") or "" for c in (it.get("content") or []))
        return {"k": "agent", "ts": ts, "id": it.get("id"), "text": _clip(txt, 20000)}
    if ty == "Reasoning":
        sm = it.get("summary_text") or []
        raw = it.get("raw_content") or []
        txt = "\n".join(str(x) for x in sm) if sm else (str(raw[0]) if raw else "")
        return {"k": "reason", "ts": ts, "id": it.get("id"), "text": _clip(txt, 3000)}
    if ty == "CommandExecution":
        cmd = ""
        pc = it.get("parsed_cmd") or []
        if pc and isinstance(pc[0], dict) and pc[0].get("cmd"):
            cmd = str(pc[0]["cmd"])
        elif it.get("command"):
            raw = it["command"]
            cmd = raw if isinstance(raw, str) else " ".join(str(x) for x in raw)
        return {"k": "cmd", "ts": ts, "id": it.get("id"), "cmd": _clip(cmd, 2000),
                "status": it.get("status"), "exit": it.get("exit_code"),
                "dur": _dur_secs(it.get("duration")), "out": _clip(it.get("aggregated_output") or "", 6000)}
    if ty == "FileChange":
        paths = []
        for c in (it.get("changes") or []):
            if isinstance(c, dict):
                v = c.get("path") or c.get("file")
                if v:
                    paths.append(str(v))
        if not paths and it.get("path"):
            paths.append(str(it["path"]))
        return {"k": "file", "ts": ts, "id": it.get("id"), "paths": [_clip(x, 400) for x in paths[:20]]}
    if ty == "McpToolCall":
        nm = "/".join(str(x) for x in [it.get("server"), it.get("tool")] if x) or "MCP"
        out = it.get("result")
        if out is None:
            out = it.get("error")
        return {"k": "mcp", "ts": ts, "id": it.get("id"), "name": nm,
                "out": _clip(json.dumps(out, ensure_ascii=False) if out is not None else "", 2000)}
    if ty == "ImageView":
        return {"k": "img", "ts": ts, "id": it.get("id"), "path": _clip(it.get("path"), 400)}
    if ty == "ContextCompaction":
        return {"k": "info", "ts": ts, "text": "上下文已压缩"}
    return {"k": "item", "ts": ts, "id": it.get("id"), "type": ty}


MARK_START = b'"type":"event_msg","payload":{"type":"task_started"'
MARK_ENDS = (b'\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\"',
             b'\"type\":\"event_msg\",\"payload\":{\"type\":\"turn_aborted\"')


def tail_running(tid: str) -> bool:
    # 最后一个 task_started / task_complete 标记决定是否在跑。
    # 不能固定只读尾部窗口: 活动量大的 rollout 一回合能长几 MB, 最后一个
    # task_started 会被挤出窗口(实测 9MB 文件最后 4MB 无标记) → 漏判。
    # 改为从 EOF 倒序分块(1MB + 64KB 重叠)找最近的精确标记, 命中即返回。
    # 模式带完整前缀, JSON 字符串里的同名子串是转义形态, 不会误伤。
    p = find_rollout(tid)
    if p is None:
        return False
    try:
        size = p.stat().st_size
    except OSError:
        return False
    CHUNK = 1 << 20
    OVER = 1 << 16
    end = size
    try:
        fh = open(p, "rb")
    except OSError:
        return False
    try:
        while end > 0:
            start = max(0, end - CHUNK)
            fh.seek(start)
            data = fh.read(end - start)
            best_off = -1
            best_started = False
            o = data.rfind(MARK_START)
            if o > best_off:
                best_off, best_started = o, True
            for pat in MARK_ENDS:
                o = data.rfind(pat)
                if o > best_off:
                    best_off, best_started = o, False
            if best_off >= 0:
                return best_started
            if start == 0:
                break
            end = start + OVER
    finally:
        fh.close()
    return False

class Tailer:
    """轮询被订阅会话的 rollout 尾部, 增量广播给手机(电脑端执行也能看到)。"""

    POLL = 0.5
    BACKFILL_BYTES = 400 * 1024
    MAX_BATCH = 120

    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self.watch = {}   # tid -> {"path","pos","buf","refs"}
        threading.Thread(target=self._loop, daemon=True).start()

    def watch_thread(self, tid: str):
        with self.lock:
            ent = self.watch.get(tid)
            if ent:
                ent["refs"] += 1
                return
            self.watch[tid] = {"path": None, "pos": None, "buf": b"", "refs": 1}

    def unwatch_thread(self, tid: str):
        with self.lock:
            ent = self.watch.get(tid)
            if ent:
                ent["refs"] -= 1
                if ent["refs"] <= 0:
                    self.watch.pop(tid, None)

    def _loop(self):
        while True:
            time.sleep(self.POLL)
            with self.lock:
                tids = list(self.watch.keys())
            for tid in tids:
                try:
                    self._poll_one(tid)
                except Exception as exc:
                    log(f"tailer {tid[:8]}: {exc}")

    def _poll_one(self, tid: str):
        with self.lock:
            ent = self.watch.get(tid)
            if ent is None:
                return
        if ent["path"] is None:
            p = find_rollout(tid)
            if p is not None:
                ent["path"] = p
            else:
                wp = find_wb_session(tid)
                if wp is not None:
                    ent["path"] = wp
                    ent["mode"] = "workbuddy"
                else:
                    cp = find_claude_session(tid)
                    if cp is None:
                        return
                    ent["path"] = cp
                    ent["mode"] = "claude"
        path = ent["path"]
        try:
            size = path.stat().st_size
        except OSError:
            ent["path"] = None
            return
        if ent["pos"] is None:
            start = max(0, size - self.BACKFILL_BYTES)
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    data = fh.read()
            except OSError:
                return
            lines = data.split(b"\n")
            if start > 0 and lines:
                lines = lines[1:]
            evs = []
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if ent.get("mode") == "workbuddy":
                    evs.extend(wb_tail_convert(obj, ent.setdefault("tmeta", {})) or [])
                elif ent.get("mode") == "claude":
                    evs.extend(claude_tail_convert(obj, ent.setdefault("tmeta", {})) or [])
                else:
                    ev = tail_convert(obj, ent.setdefault('_turn_meta', {}))
                    if ev:
                        evs.append(ev)
            ent["pos"] = size
            if evs:
                self.hub.broadcast({"kind": "tail", "tid": tid, "ev": evs[-self.MAX_BATCH:], "backfill": True})
            return
        if size < ent["pos"]:
            ent["pos"] = None
            ent["buf"] = b""
            return
        if size == ent["pos"]:
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(ent["pos"])
                data = fh.read(size - ent["pos"])
        except OSError:
            return
        ent["pos"] = size
        data = ent["buf"] + data
        parts = data.split(b"\n")
        ent["buf"] = parts.pop() if parts else b""
        evs = []
        for raw in parts:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if ent.get("mode") == "workbuddy":
                evs.extend(wb_tail_convert(obj, ent.setdefault("tmeta", {})) or [])
            elif ent.get("mode") == "claude":
                evs.extend(claude_tail_convert(obj, ent.setdefault("tmeta", {})) or [])
            else:
                ev = tail_convert(obj, ent.setdefault('_turn_meta', {}))
                if ev:
                    evs.append(ev)
        if evs:
            self.hub.broadcast({"kind": "tail", "tid": tid, "ev": evs[-self.MAX_BATCH:]})


# ---------- Claude Code 引擎(第二引擎: 事件翻译为与 Codex 相同的 UI 词汇) ----------

_claude_scan = {"ts": 0.0, "map": {}}
_claude_meta_cache = {}
_claude_img_cache = {}
_claude_reg_cache = {"ts": 0.0, "data": {}}


_proj_scan = {}


def claude_sessions_map(force=False, root=None) -> dict:
    """sessionId -> jsonl 路径(15 秒缓存)。root=None 扫 Claude; 传目录则扫该引擎的 projects。"""
    now = time.time()
    if root is None:
        cache = _claude_scan
        base = CLAUDE_PROJECTS
    else:
        base = Path(root)
        cache = _proj_scan.setdefault(str(base), {"ts": 0.0, "map": {}})
    if not force and now - cache["ts"] < 2:
        return cache["map"]
    m = {}
    try:
        for p in base.rglob("*.jsonl"):
            m.setdefault(p.stem, p)
    except OSError:
        pass
    cache.update({"ts": now, "map": m})
    return m


def find_claude_session(tid: str, force=False, root=None):
    return claude_sessions_map(force=force, root=root).get(tid)


def _pid_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)      # 信号 0 = 只探存在性, 不真发信号
        except ProcessLookupError:
            return False
        except PermissionError:
            return True          # 存在, 只是不归我们管
        except OSError:
            return False
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        h = kernel.OpenProcess(0x1000, False, pid)
        if not h:
            return ctypes.get_last_error() == 5  # access denied still means present
        try:
            code = wintypes.DWORD(0)
            ok = kernel.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(h)
    except Exception:
        return False


def claude_session_registry(force=False) -> dict:
    """~/.claude/sessions/<pid>.json 交互会话注册表 -> {sessionId: {pid, status, cwd}}。"""
    now = time.time()
    if not force and now - _claude_reg_cache["ts"] < 5:
        return _claude_reg_cache["data"]
    data = {}
    try:
        for p in CLAUDE_SESSIONS_DIR.glob("*.json"):
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = j.get("sessionId")
            if not sid:
                continue
            ent = data.get(sid) or {"pid": 0, "status": "", "cwd": "", "_u": 0}
            if int(j.get("updatedAt") or 0) >= int(ent.get("_u") or 0):
                ent.update({"pid": int(j.get("pid") or 0), "status": str(j.get("status") or ""),
                            "cwd": str(j.get("cwd") or ""), "_u": int(j.get("updatedAt") or 0)})
            data[sid] = ent
    except OSError:
        pass
    alive = {sid: ent for sid, ent in data.items() if ent.get("pid") and _pid_alive(ent["pid"])}
    _claude_reg_cache.update({"ts": now, "data": alive})
    return alive


def claude_running(tid: str) -> bool:
    if CLAUDE is not None:
        _fs = CLAUDE.forced_stop.get(tid)
        if _fs:
            if time.time() - _fs < 20:
                return False
            CLAUDE.forced_stop.pop(tid, None)
    if CLAUDE is not None and tid in CLAUDE.turns:
        return True
    ent = claude_session_registry().get(tid)
    return bool(ent and ent.get("status") == "busy")


_IMG_REF_RE = re.compile(r"^\s*\[Image:\s*source:\s*(.+?)\]\s*$", re.S)


def _claude_img_ref(txt) -> str:
    """'[Image: source: C:\\...\\images\\1.png]' -> 路径(不是这个形状就返回 '')。"""
    m = _IMG_REF_RE.match(str(txt or ""))
    return m.group(1).strip() if m else ""


def _claude_text_of(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        # Claude: text; WorkBuddy(CodeBuddy) JSONL: input_text / output_text / reasoning_text
        if b.get("type") in ("text", "input_text", "output_text", "reasoning_text") \
                and isinstance(b.get("text"), str):
            out.append(b["text"])
    return "\n".join(out)


def tool_result_output(tr, content) -> str:
    out = ""
    if isinstance(tr, dict):
        so = str(tr.get("stdout") or "")
        se = str(tr.get("stderr") or "")
        out = (so + ("\n" + se if se else "")).strip()
        if not out and tr.get("interrupted"):
            out = "(已中断)"
    if not out:
        if isinstance(content, str):
            out = content
        elif isinstance(content, list):
            out = "\n".join(str(b.get("text") or "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    return out


def _claude_save_image_block(block) -> str:
    """JSONL 内嵌图片块 -> gateway/uploads 本地文件(sha 去重), 供 /api/fs/raw 渲染。"""
    try:
        src = block.get("source") or {}
        if not isinstance(src, dict) or src.get("type") != "base64":
            return ""
        raw = base64.b64decode(src.get("data") or "")
        if not raw or len(raw) > 12 * 1024 * 1024:
            return ""
        sha = hashlib.sha1(raw).hexdigest()[:16]
        hit = _claude_img_cache.get(sha)
        if hit and os.path.exists(hit):
            return hit
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
               "image/gif": ".gif"}.get(str(src.get("media_type") or "").lower(), ".png")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        fp = UPLOAD_DIR / ("cl-" + sha + ext)
        if not fp.exists():
            fp.write_bytes(raw)
        _claude_img_cache[sha] = str(fp)
        return str(fp)
    except Exception:
        return ""


def _claude_tool_item(b: dict) -> dict:
    """Claude tool_use 块 -> Codex 形状的卡片 item。"""
    name = str(b.get("name") or "tool")
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    tid = str(b.get("id") or uuid.uuid4().hex)
    if name == "Bash":
        cmd = inp.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        return {"id": tid, "type": "commandExecution", "command": _clip(cmd or "Bash", 2000),
                "status": "in_progress", "exitCode": None, "aggregatedOutput": ""}
    return {"id": tid, "type": "mcpToolCall", "toolName": name, "arguments": inp, "output": None}


WB_INTERRUPT_TEXT = "Interrupted by user"


def wb_config_root(path) -> Path:
    """会话文件属于哪个 WB 配置根(.codebuddy / .workbuddy), 用于 CODEBUDDY_CONFIG_DIR。"""
    try:
        p = os.path.abspath(str(path)).lower()
    except Exception:
        return WB_HOME
    for root in WB_PROJECTS_ALL:
        try:
            if p.startswith(os.path.abspath(str(root)).lower()):
                return root.parent
        except Exception:
            continue
    return WB_HOME


def wb_normalize_models_json(root) -> bool:
    """把 models.json 兼容成 CodeBuddy CLI 认的 schema。

    CLI 5.3.8 的 CustomModelsProductProvider 只收带 id 的条目, 老格式
    (model/api_key/api_url)会被整条跳过 -> custom-local:X 退化成腾讯云默认端点
    (实测 400 model [X] service info not found)。这里只补字段, 不删旧键,
    首次改写前留 .bak-nexus-<时间戳>。
    """
    p = Path(root) / "models.json"
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    models = obj.get("models")
    if not isinstance(models, list):
        return False
    fixed = []
    for m in models:
        if not isinstance(m, dict):
            fixed.append(m)
            continue
        out = dict(m)
        mid = str(out.get("id") or out.get("model") or "").strip()
        if not mid:
            fixed.append(out)
            continue
        out["id"] = mid
        if not str(out.get("name") or "").strip():
            out["name"] = mid
        if not out.get("vendor") and out.get("provider"):
            out["vendor"] = out.get("provider")
        if not out.get("apiKey") and out.get("api_key"):
            out["apiKey"] = out.get("api_key")
        if not out.get("url") and out.get("api_url"):
            out["url"] = out.get("api_url")
        url = str(out.get("url") or "").strip().rstrip("/")
        if url.endswith("/v1"):
            # CLI 把 url 直接当 baseURL(不再补路径), 老的 api_url 是 base, 要补全
            out["url"] = url + "/chat/completions"
        fixed.append(out)
    if json.dumps(fixed, sort_keys=True) == json.dumps(models, sort_keys=True):
        return False
    try:
        backup = p.with_suffix(p.suffix + ".bak-nexus-" + time.strftime("%Y%m%d-%H%M%S"))
        backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    obj["models"] = fixed
    try:
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return False
    log("workbuddy: normalized models.json schema at %s" % p)
    return True


def _is_wb_path(path) -> bool:
    try:
        p = os.path.abspath(str(path)).lower()
    except Exception:
        return False
    for root in WB_PROJECTS_ALL:
        try:
            if p.startswith(os.path.abspath(str(root)).lower()):
                return True
        except Exception:
            continue
    return False


_WB_USERQ_RE = re.compile(r"<user_query>([\s\S]*?)</user_query>")
_WB_SYSREM_RE = re.compile(r"<system-reminder\b[^>]*>[\s\S]*?</system-reminder>")
_WB_TEAMMATE_RE = re.compile(r"<teammate-message\b[^>]*>[\s\S]*?</teammate-message>")


def _wb_clean_text(txt: str) -> str:
    """桌面端把真实提问包在 <user_query> 里, 前面塞 ~2 万字的 <system-reminder>。
    原样渲染会让历史里出现一整屏"用户消息"。CLI 落盘没有这些包裹, 清洗是无损的。
    """
    if not txt:
        return ""
    _m = _WB_USERQ_RE.findall(txt)
    if _m:
        txt = "\n".join(x.strip() for x in _m if x.strip())
    txt = _WB_SYSREM_RE.sub("", txt)
    txt = _WB_TEAMMATE_RE.sub("", txt)
    return txt.strip()


def _wb_is_interrupt_row(j) -> bool:
    """官方中断闭合记录? (CodeBuddy isUserInterrupted 的同一判据)"""
    if not isinstance(j, dict) or j.get("type") != "message" or j.get("role") != "assistant":
        return False
    c = j.get("content")
    if isinstance(c, str):
        return c.strip() == WB_INTERRUPT_TEXT
    if isinstance(c, list):
        for b in c:
            if (isinstance(b, dict) and b.get("type") in ("output_text", "text")
                    and str(b.get("text") or "").strip() == WB_INTERRUPT_TEXT):
                return True
    return False


_wb_dbmeta_cache = {"key": None, "data": {}}


def wb_desktop_db_meta() -> dict:
    """桌面端 WorkBuddy 的会话索引(~/.workbuddy/workbuddy.db)。

    桌面端把标题/模型/工作目录/更新时间写在这个 sqlite 里, JSONL 里没有 ai-title;
    只读打开, 按 mtime+size 缓存。返回 {sessionId: {...}}。
    """
    db = WB_DESKTOP_HOME / "workbuddy.db"
    try:
        st = db.stat()
    except OSError:
        return {}
    key = (int(st.st_mtime_ns), st.st_size)
    wal=Path(str(db)+'-wal')
    try:
        wal_stat=wal.stat();key+=(int(wal_stat.st_mtime_ns),wal_stat.st_size)
    except OSError:
        key+=(0,0)
    if _wb_dbmeta_cache["key"] == key:
        return _wb_dbmeta_cache["data"]
    data = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                              uri=True, timeout=2)
        try:
            for sid, cwd, title, ctitle, model, created, upd in con.execute(
                    "select id, cwd, title, custom_title, model, created_at, "
                    "last_activity_at from sessions"):
                sid = str(sid or "")
                if not sid:
                    continue
                ts = int(upd or created or 0)
                data[sid] = {"cwd": str(cwd or ""),
                             "title": str(ctitle or title or ""),
                             "model": str(model or ""),
                             "updatedAt": int(ts / 1000) if ts > 1e11 else int(ts)}
            for sid, used in con.execute("select session_id, used from session_usage"):
                sid = str(sid or "")
                if sid in data:
                    data[sid]["used"] = int(used or 0)
        finally:
            con.close()
    except Exception as exc:
        log("wb db meta: %s" % exc)
        data = {}
    _wb_dbmeta_cache.update({"key": key, "data": data})
    return data


def wb_artifact_summary(tid: str, max_diff: int = 4000) -> list:
    """WorkBuddy 的产物索引(~/.workbuddy/artifact-index/<sid>.json)。

    JSONL 被清理后, 这里仍留着任务清单与文件改动 —— 是这条会话唯一还能找回的
    内容。只读、按需读取; 密钥字样一律打码后再下发。
    """
    fp = WB_DESKTOP_HOME / "artifact-index" / (tid + ".json")
    try:
        obj = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return []
    out, seen = [], set()
    for a in (obj.get("artifacts") or []):
        if not isinstance(a, dict):
            continue
        typ = str(a.get("type") or "")
        title = str(a.get("title") or a.get("name") or "")
        item = {"type": typ, "title": title}
        tasks = a.get("tasks") or []
        if tasks:
            item["tasks"] = [{"content": str(t.get("content") or ""),
                              "status": str(t.get("status") or "")}
                             for t in tasks if isinstance(t, dict)][:60]
        fi = a.get("fileChangeInfo") or {}
        diff = str(fi.get("diff") or "")
        if diff:
            diff = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}", "sk-***", diff)
            diff = re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)([^\s\"']{4,})",
                          r"\1***", diff)
            item["file"] = str(fi.get("uri") or "")
            item["changeType"] = str(fi.get("changeType") or "")
            item["diff"] = diff[:max_diff] + ("\n…(已截断)" if len(diff) > max_diff else "")
        key = (item["type"], item["title"], item.get("diff") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def claude_session_meta(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    _wb_flavor = _is_wb_path(path)
    _desktop_meta=wb_desktop_db_meta().get(path.stem,{}) if _wb_flavor else {}
    _source_stamp=(int(st.st_mtime_ns),json.dumps(_desktop_meta,sort_keys=True,ensure_ascii=False)) if _wb_flavor else int(st.st_mtime_ns)
    hit = _claude_meta_cache.get(key)
    if hit and hit[0] == _source_stamp and hit[1] == st.st_size:
        return hit[2]
    meta = {"id": path.stem, "cwd": "", "name": "", "preview": "", "lastPrompt": "",
            "userTurns": 0, "mtime": st.st_mtime, "mb": round(st.st_size / 1048576.0, 2),
            "model": "", "modelName": "", "usage": None}
    _wb_flavor = _is_wb_path(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or '"type"' not in line or len(line) > 12000000:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if not isinstance(j, dict):
                    continue
                t = j.get("type")
                if not meta["cwd"] and isinstance(j.get("cwd"), str):
                    meta["cwd"] = j["cwd"]
                if t == "ai-title" and isinstance(j.get("aiTitle"), str):
                    meta["name"] = j["aiTitle"]
                elif t == "custom-title" and isinstance(j.get("customTitle"), str):
                    meta["name"] = j["customTitle"]
                elif t == "last-prompt" and isinstance(j.get("lastPrompt"), str):
                    meta["lastPrompt"] = j["lastPrompt"]
                elif t == "message" and j.get("role") == "user":
                    # WorkBuddy(CodeBuddy): {type:"message", role:"user", content:[...]}
                    txt = _claude_text_of(j.get("content")).strip()
                    if _wb_flavor:
                        txt = _wb_clean_text(txt)
                    if txt:
                        meta["userTurns"] += 1
                        if not meta["preview"]:
                            meta["preview"] = txt[:280]
                        meta["lastPrompt"] = txt[:280]
                        if _wb_flavor and not meta["name"] and not _claude_img_ref(txt):
                            # WB 不写 ai-title/custom-title: 列表标题退回首条用户输入
                            meta["name"] = " ".join(txt.split())[:48]
                elif t == "message":
                    # WB assistant 行: 本会话实际模型 + 最近一次调用用量(逐行覆盖 = 末次为准)
                    _pd = j.get("providerData") if isinstance(j.get("providerData"), dict) else {}
                    _rm = str(_pd.get("requestModelId") or "").strip()
                    _mm = str(_pd.get("model") or "").strip()
                    if _rm or _mm:
                        meta["model"] = (_rm if _rm and _rm.lower() != "auto" else (_mm or _rm))
                        meta["modelName"] = str(_pd.get("requestModelName") or "")
                    _u = _wb_usage_of(j)
                    if _u:
                        meta["usage"] = _u
                elif t == "user" and not j.get("isSidechain"):
                    msg = j.get("message") or {}
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        txt = _claude_text_of(msg.get("content")).strip()
                        if txt:
                            meta["userTurns"] += 1
                            if not meta["preview"]:
                                meta["preview"] = txt[:280]
    except OSError:
        pass
    if _wb_flavor:
        # 桌面端 JSONL 不写标题/模型, 真值在 workbuddy.db: 只补空缺, 不覆盖落盘值。
        _dbm = _desktop_meta
        if _dbm:
            if _dbm.get('title'):
                meta["name"] = str(_dbm.get("title") or "")
            if not meta["model"]:
                meta["model"] = str(_dbm.get("model") or "")
            if not meta["cwd"]:
                meta["cwd"] = str(_dbm.get("cwd") or "")
            _dmt = int(_dbm.get("updatedAt") or 0)
            if _dmt > int(meta["mtime"] or 0):
                meta["mtime"] = _dmt
            if not meta.get("usage") and _dbm.get("used"):
                _du = int(_dbm["used"])
                meta["usage"] = {"inputTokens": _du, "outputTokens": 0,
                                 "contextWindow": 0, "costUsd": None,
                                 "ts": int(meta["mtime"] or 0)}
    if not meta["cwd"]:
        meta["cwd"] = str(USER_HOME)
    _claude_meta_cache[key] = (_source_stamp, st.st_size, meta)
    return meta

_claude_cfg_cache = {"key": None, "data": None}

def claude_local_cfg() -> dict:
    """本机 ~/.claude/settings.json (按 mtime+size 缓存)。"""
    try:
        st = CLAUDE_LOCAL_SETTINGS.stat()
        key = (int(st.st_mtime_ns), st.st_size)
        if _claude_cfg_cache["key"] == key:
            return _claude_cfg_cache["data"]
        cfg = json.loads(CLAUDE_LOCAL_SETTINGS.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
        _claude_cfg_cache.update({"key": key, "data": cfg})
        return cfg
    except Exception:
        return {}

def claude_thread_model(tid: str):
    """会话实际生效的模型/思考程度: 网关 per-thread 覆盖 > settings.json 默认。"""
    model = effort = ""
    try:
        st = (CLAUDE.settings.get(tid) or {}) if CLAUDE is not None else {}
        model = str(st.get("model") or "")
        effort = str(st.get("effort") or "")
    except Exception:
        pass
    if not model or not effort:
        cfg = claude_local_cfg()
        if not model:
            model = str(cfg.get("model") or "")
        if not effort:
            ms = cfg.get("modelSettings") or {}
            if model and isinstance(ms.get(model), dict):
                effort = str(ms[model].get("effortLevel") or "")
    return (model or "deepseek-flash", effort or "high")

def _claude_next_iid(ordinals: dict, mid: str) -> str:
    """块 id = <message_id>-<序号>: 每消息独立计数器, 与流式 content_block index 对齐
    (JSONL 每行只放一个内容块, enumerate 恒为 0, 会让思考/正文的 id 撞车)。"""
    idx = ordinals.get(mid, 0)
    ordinals[mid] = idx + 1
    return "%s-%d" % (mid, idx)

def claude_read_thread(tid: str, light: bool = False) -> dict:
    """Claude 会话 JSONL -> Codex 形状 thread。三轮(流式/历史/尾随)共用同一套 item id。"""
    now_sec = int(time.time())
    path = find_claude_session(tid)
    if path is None:
        # 15s 扫描缓存间隙(回合进行中/刚 spawn, jsonl 未被收录): 强制刷新一次再判 404
        path = find_claude_session(tid, force=True)
    if path is None:
        pend = (CLAUDE.pending.get(tid) if CLAUDE is not None else None)
        if pend:
            return {"id": tid, "engine": "claude", "cwd": pend.get("cwd") or "", "name": "",
                    "preview": "", "turns": [], "updatedAt": now_sec, "recencyAt": now_sec,
                    "writable": True, "status": {"type": "idle"}, "source": "claude-code"}
        reg = (claude_session_registry().get(tid) or {}) if CLAUDE is not None else {}
        known = (CLAUDE is not None) and (tid in CLAUDE.known)
        if reg or known:
            # 会话已知(live pid 注册表 / 刚 spawn)但 jsonl 暂时不可见: 返回占位 stub 而非 404,
            # 否则运行条与「停止」按钮会随扫描间隙忽有忽无(实测 e0a83e84)。
            busy = (CLAUDE is not None) and (tid in CLAUDE.turns or str(reg.get("status") or "") == "busy")
            return {"id": tid, "engine": "claude", "cwd": str(reg.get("cwd") or ""), "name": "",
                    "preview": "", "turns": [], "updatedAt": now_sec, "recencyAt": now_sec,
                    "writable": True, "status": {"type": "active" if busy else "idle"},
                    "source": "claude-code"}
        raise ApiError({"code": "not_found", "message": "claude 会话不存在: " + tid})
    meta = claude_session_meta(path)
    turns = []
    cur = None
    block_ord = {}
    tool_index = {}
    prev_user_had_img = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or len(line) > 16000000:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if not isinstance(j, dict) or j.get("isSidechain"):
                    continue
                t = j.get("type")
                if t == "user":
                    msg = j.get("message") or {}
                    if not isinstance(msg, dict) or msg.get("role") != "user":
                        continue
                    content = msg.get("content")
                    blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}] if isinstance(content, str) else [])
                    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
                    if results:
                        tr = j.get("tool_use_result") or {}
                        for rb in results:
                            it = tool_index.get(rb.get("tool_use_id"))
                            if it is None:
                                continue
                            out = tool_result_output(tr, rb.get("content"))
                            it["status"] = "failed" if rb.get("is_error") else "completed"
                            it["exitCode"] = 1 if rb.get("is_error") else 0
                            if it.get("type") == "commandExecution":
                                it["aggregatedOutput"] = _clip(out, MAX_OUTPUT)
                            else:
                                it["output"] = _clip(out, MAX_OUTPUT)
                                if rb.get("is_error"):
                                    it["error"] = _clip(out, 800) or "error"
                    txt = _claude_text_of(content).strip()
                    imgs = [b for b in blocks if isinstance(b, dict) and b.get("type") == "image"]
                    if j.get("isMeta") or j.get("turnCompanion"):
                        # CLI 会把同一条附图消息再记一份 meta 副本(正文是 "[Image: source: <临时文件>]"),
                        # 之前两份都渲染 -> 手机端"一条消息两个气泡"。这里: 前面已有 image 块就整个跳过,
                        # 只有孤立引用且文件还在时才把它还原成图片。
                        ref = _claude_img_ref(txt)
                        if ref and not prev_user_had_img and os.path.isfile(ref):
                            cur = {"id": j.get("uuid") or ("t%d" % len(turns)), "items": []}
                            turns.append(cur)
                            cur["items"].append({"id": j.get("uuid") or ("u%d" % len(turns)),
                                                 "type": "userMessage",
                                                 "content": [{"type": "localImage", "path": ref}]})
                        continue
                    prev_user_had_img = bool(imgs)
                    if txt or imgs:
                        cur = {"id": j.get("uuid") or ("t%d" % len(turns)), "items": []}
                        turns.append(cur)
                        citems = []
                        if txt:
                            citems.append({"type": "text", "text": txt})
                        for b in imgs[:8]:
                            pth = _claude_save_image_block(b)
                            if pth:
                                citems.append({"type": "localImage", "path": pth})
                        cur["items"].append({"id": j.get("uuid") or ("u%d" % len(turns)),
                                             "type": "userMessage", "content": citems})
                    continue
                if t == "assistant":
                    msg = j.get("message") or {}
                    if not isinstance(msg, dict):
                        continue
                    mid = msg.get("id") or j.get("uuid") or ""
                    if cur is None:
                        cur = {"id": "t" + str(len(turns)), "items": []}
                        turns.append(cur)
                    for b in msg.get("content") or []:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        iid = _claude_next_iid(block_ord, mid)
                        if bt == "thinking" and (b.get("thinking") or "").strip():
                            cur["items"].append({"id": iid, "type": "reasoning", "summary": [],
                                                 "content": [b.get("thinking")]})
                        elif bt == "text" and (b.get("text") or "").strip():
                            cur["items"].append({"id": iid, "type": "agentMessage", "text": b.get("text")})
                        elif bt == "tool_use":
                            it = _claude_tool_item(b)
                            cur["items"].append(it)
                            tool_index[it["id"]] = it
                    continue
    except OSError as exc:
        raise ApiError({"code": "io", "message": str(exc)})
    mt = int(meta.get("mtime") or 0)
    _cm, _ce = claude_thread_model(tid)
    thread = {
        "id": tid, "engine": "claude",
        "model": _cm, "reasoningEffort": _ce,
        "cwd": meta.get("cwd") or "",
        "name": meta.get("name") or "",
        "preview": meta.get("preview") or "",
        "turns": turns, "updatedAt": mt, "recencyAt": mt,
        "writable": True, "status": {"type": "idle"}, "source": "claude-code",
    }
    return trim_thread(thread, light=light)


def claude_models_payload() -> dict:
    """Read Claude settings only; unconfigured models remain client defaults."""
    model = ""
    effort = ""
    try:
        cfg = claude_local_cfg()
        env = cfg.get("env") or {}
        model = str(cfg.get("model") or env.get("ANTHROPIC_MODEL") or "")
        effort = str(cfg.get("effortLevel") or env.get("CLAUDE_CODE_EFFORT_LEVEL") or "")
        ms = cfg.get("modelSettings") or {}
        if model and isinstance(ms.get(model), dict):
            effort = str(ms[model].get("effortLevel") or "")
    except Exception:
        pass
    effs = ["low", "medium", "high", "xhigh", "max"]
    models = [{"id": model, "label": model, "efforts": list(effs), "source": str(CLAUDE_LOCAL_SETTINGS)}] if model else []
    env = (claude_local_cfg().get("env") or {})
    for name in ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        configured = str(env.get(name) or "")
        if configured and configured not in {row["id"] for row in models}:
            models.append({"id": configured, "label": configured, "efforts": list(effs), "source": str(CLAUDE_LOCAL_SETTINGS)})
    return {"models": models, "efforts": effs,
            "defaultModel": model, "defaultEffort": effort,
            "source": "local_claude", "readAt": time.time(),
            "provider": safe_url(env.get("ANTHROPIC_BASE_URL") or "") or "claude-code",
            "cfg": {"engine": "claude", "path": str(CLAUDE_LOCAL_SETTINGS),
                    "exists": CLAUDE_LOCAL_SETTINGS.is_file()}}


def claude_tail_convert(obj: dict, state: dict = None):
    """Claude JSONL 单行 -> tail 事件列表(与通知路径同 id, UI 端 upsert 自动去重)。"""
    if state is None:
        state = {}
    t = obj.get("type")
    ts = obj.get("timestamp")
    if obj.get("isSidechain"):
        return None
    if t == "user":
        msg = obj.get("message") or {}
        if not isinstance(msg, dict) or msg.get("role") != "user":
            return None
        content = msg.get("content")
        blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}] if isinstance(content, str) else [])
        evs = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                it = state.get(b.get("tool_use_id"))
                if not it:
                    continue
                out = tool_result_output(obj.get("tool_use_result") or {}, b.get("content"))
                done = b.get("is_error") is not True
                if it.get("kind") == "cmd":
                    evs.append({"k": "cmd", "ts": ts, "id": b.get("tool_use_id"),
                                "cmd": it.get("cmd") or "", "status": ("completed" if done else "failed"),
                                "exit": (0 if done else 1), "dur": None, "out": _clip(out, MAX_OUTPUT)})
                else:
                    evs.append({"k": "mcp", "ts": ts, "id": b.get("tool_use_id"),
                                "name": it.get("name") or "tool", "out": _clip(out, 6000)})
        txt = _claude_text_of(content).strip()
        if txt:
            state["tok"] = {"msgs": {}}
            evs.append({"k": "user", "ts": ts, "id": obj.get("uuid") or ("cu%d" % int(time.time() * 1000)),
                        "text": _clip(txt, 4000)})
        return evs or None
    if t == "assistant":
        msg = obj.get("message") or {}
        if not isinstance(msg, dict):
            return None
        mid = msg.get("id") or obj.get("uuid") or ""
        evs = []
        u = msg.get("usage")
        if isinstance(u, dict) and u:
            tk = state.setdefault("tok", {"msgs": {}})
            tk.setdefault("msgs", {})[str(mid)] = int(u.get("output_tokens") or 0)
            tk["ctx"] = (int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                         + int(u.get("cache_creation_input_tokens") or 0))
            evs.append({"k": "usage", "ts": ts, "id": "tok", "in": tk["ctx"],
                        "out": sum(tk["msgs"].values()), "win": tk.get("win")})
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            iid = _claude_next_iid(state.setdefault("_bidx", {}), mid)
            if bt == "thinking" and (b.get("thinking") or "").strip():
                evs.append({"k": "reason", "ts": ts, "id": iid, "text": b.get("thinking")})
            elif bt == "text" and (b.get("text") or "").strip():
                evs.append({"k": "agent", "ts": ts, "id": iid, "text": b.get("text")})
            elif bt == "tool_use":
                it = _claude_tool_item(b)
                if it["type"] == "commandExecution":
                    state[it["id"]] = {"kind": "cmd", "cmd": it.get("command") or ""}
                    evs.append({"k": "cmd", "ts": ts, "id": it["id"], "cmd": it.get("command") or "",
                                "status": "running", "exit": None, "dur": None, "out": ""})
                else:
                    state[it["id"]] = {"kind": "mcp", "name": it.get("toolName") or ""}
                    evs.append({"k": "mcp", "ts": ts, "id": it["id"], "name": it.get("toolName") or "", "out": ""})
        return evs or None
    return None

def wb_tail_convert(obj: dict, state: dict = None):
    """WorkBuddy JSONL 单行 -> tail 事件(message/reasoning/function_call[_result])。"""
    if state is None:
        state = {}
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    ts = obj.get("timestamp")
    rid = str(obj.get("id") or "")
    if t in ("file-history-snapshot", "system"):
        return None
    if t == "reasoning":
        txt = "".join(str(b.get("text") or "") for b in (obj.get("rawContent") or [])
                      if isinstance(b, dict)).strip()
        if not txt:
            txt = _wb_text_of_blocks(obj.get("content")).strip()
        if not txt:
            return None
        return [{"k": "reason", "ts": ts, "id": (rid or uuid.uuid4().hex) + "-r", "text": _clip(txt, 6000)}]
    if t == "message":
        role = obj.get("role")
        blocks = obj.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        if not isinstance(blocks, list):
            return None
        if role == "user":
            txt = _wb_clean_text(_wb_text_of_blocks(blocks))
            if not txt:
                return None
            state["tok"] = {"msgs": {}}
            return [{"k": "user", "ts": ts,
                     "id": (rid or ("wu%d" % int(time.time() * 1000))) + "-u",
                     "text": _clip(txt, 4000)}]
        if role == "assistant":
            if _wb_is_interrupt_row(obj):
                return None
            evs = []
            _u = _wb_usage_of(obj)
            if _u:
                # 电脑端/CLI 自己跑的回合没有 live reader, 只能靠 tail 把用量推给手机
                evs.append({"k": "usage", "ts": ts, "id": "tok",
                            "in": _u["inputTokens"], "out": _u["outputTokens"], "win": 0})
            for k, b in enumerate(blocks):
                if not isinstance(b, dict):
                    continue
                if b.get("type") in ("output_text", "text") and str(b.get("text") or "").strip():
                    evs.append({"k": "agent", "ts": ts, "id": "%s-t%d" % (rid or uuid.uuid4().hex, k),
                                "text": b.get("text")})
            return evs or None
        return None
    if t == "function_call":
        cid = str(obj.get("callId") or rid or uuid.uuid4().hex)
        if cid in state:
            return None            # 重复落盘的同一 callId: tail 不再推第二张卡
        it = _wb_tool_item(obj.get("name"), _wb_args(obj.get("arguments")), cid)
        if it.get("type") == "commandExecution":
            state[cid] = {"kind": "cmd", "cmd": it.get("command") or ""}
            return [{"k": "cmd", "ts": ts, "id": cid, "cmd": it.get("command") or "",
                     "status": "running", "exit": None, "dur": None, "out": ""}]
        state[cid] = {"kind": "mcp", "name": it.get("toolName") or ""}
        return [{"k": "mcp", "ts": ts, "id": cid, "name": it.get("toolName") or "", "out": ""}]
    if t == "function_call_result":
        cid = str(obj.get("callId") or "")
        it = state.get(cid)
        if not it:
            return None
        o = obj.get("output")
        if isinstance(o, dict):
            out = str(o.get("text") or "")
        elif isinstance(o, str):
            out = o
        elif isinstance(o, list):
            out = _wb_text_of_blocks(o)
        else:
            out = ""
        if not out:
            out = _wb_text_of_blocks(obj.get("content"))
        if it.get("done") and not out:
            return None            # 重复的空调 result: 不许把已推给前端的输出抹掉
        if out:
            it["done"] = True
        ok = str(obj.get("status") or "completed") != "error"
        if it.get("kind") == "cmd":
            return [{"k": "cmd", "ts": ts, "id": cid, "cmd": it.get("cmd") or "",
                     "status": ("completed" if ok else "failed"), "exit": (0 if ok else 1),
                     "dur": None, "out": _clip(out, MAX_OUTPUT)}]
        return [{"k": "mcp", "ts": ts, "id": cid, "name": it.get("name") or "tool",
                 "out": _clip(out, 6000)}]
    return None

def _est_tokens(s: str) -> float:
    """CJK chars ~= 1 token each, others ~4 chars/token; smooth live estimate."""
    if not s:
        return 0.0
    cjk = 0
    for ch in s:
        if ord(ch) > 0x2E80:
            cjk += 1
    return cjk + max(1.0, (len(s) - cjk) / 4.0)


class ClaudeAS:
    """Claude Code 引擎: 每回合一个 headless 进程; 流式事件 -> 与 Codex 同款的通知。

    WorkBuddyAS 继承本类(CLI 契约同构)，只覆盖磁盘适配与启动 argv。
    """

    ENGINE = "claude"
    LABEL = "Claude"
    SOURCE = "claude-code"
    TURN_PREFIX = "cl-"
    SHORT = "cl"
    PROJECTS = CLAUDE_PROJECTS
    ARCHIVE_FILE = CLAUDE_ARCHIVE_FILE
    SETTINGS_FILE = CLAUDE_SETTINGS_FILE
    USAGE_FILE = CLAUDE_USAGE_FILE
    TRASH_DIR = CLAUDE_TRASH_DIR
    PENDING_FILE = None
    spawn_env = None

    def __init__(self, hub):
        self._init_engine(hub, [find_claude_exe()])

    def _init_engine(self, hub, argv):
        self.hub = hub
        self.lock = threading.Lock()
        self.turns = {}      # tid -> Popen(正在执行的回合)
        self.turn_gen = {}   # tid -> 回合代号(interrupt 后旧 reader 不许再动状态)
        self.turn_ids = {}
        self.forced_stop = {}  # tid -> ts(刚点过停止: registry 的 busy 缓存不许再挡队列)
        self.queues = {}     # tid -> [排队消息, 当前回合结束后自动发送]
        self.pending = self._load_json(self.PENDING_FILE, {}) if self.PENDING_FILE else {}
        self._pending_save_lock = threading.Lock()
        self.known = set()   # 已确认本引擎的 tid (粘性; 防 15s 扫描缓存间隙错路由)
        self.not_claude = set()  # 已确认非本引擎的 tid (负缓存, 避免反复全盘扫描)
        self.archived = self._load_json(self.ARCHIVE_FILE, [])
        self.settings = self._load_json(self.SETTINGS_FILE, {})
        self.last_usage = self._load_json(self.USAGE_FILE, {})
        self.argv = [x for x in (argv or []) if x]
        self.exe = self.argv[0] if self.argv else ""
        self.status = "ready" if self.exe else "no-cli"
        if not self.exe:
            log("%s: 未找到 CLI, %s 引擎不可用(Codex 引擎不受影响)"
                % (type(self).__name__, self.LABEL))
        else:
            log("%s ready: %s" % (type(self).__name__, " ".join(self.argv)))

    # ---------- 磁盘适配(子类覆盖) ----------
    def sessions_map(self, force=False):
        return claude_sessions_map(force=force, root=self.PROJECTS)

    def find_session(self, tid, force=False):
        return self.sessions_map(force=force).get(tid)

    def session_registry(self, force=False):
        return claude_session_registry(force=force)

    def is_running(self, tid):
        _fs = self.forced_stop.get(tid)
        if _fs:
            if time.time() - _fs < 20:
                return False
            self.forced_stop.pop(tid, None)
        if tid in self.turns:
            return True
        ent = self.session_registry().get(tid)
        return bool(ent and ent.get("status") == "busy")

    def thread_model(self, tid):
        return claude_thread_model(tid)

    def read_thread(self, tid, light=False):
        return claude_read_thread(tid, light=light)

    def local_cfg(self):
        return claude_local_cfg()

    def models_payload(self):
        return claude_models_payload()

    def spawn_env_for(self, tid, path):
        """每回合的启动环境(子类可覆盖: WB 要按会话所在根切 CODEBUDDY_CONFIG_DIR)。"""
        return self.spawn_env

    def _make_cmd(self, tid, path, model, effort):
        cmd = list(self.argv) + ["-p", "--output-format", "stream-json", "--verbose",
                                 "--include-partial-messages", "--input-format", "stream-json"]
        cmd += (["--resume", tid] if path is not None else ["--session-id", tid])
        if model:
            cmd += ["--model", str(model)]
        if effort:
            cmd += ["--effort", str(effort)]
        if think_zh_on():
            if self.ENGINE == "workbuddy":
                # CodeBuddy CLI 没有 --append-system-prompt-file(实测 "unknown option"),
                # 只认内联文本; Windows 子进程走 CreateProcessW, 中文 argv 安全。
                cmd += ["--append-system-prompt", NEXUS_THINK_ZH]
            else:
                _zhf = think_zh_prompt_path()
                if _zhf:
                    cmd += ["--append-system-prompt-file", _zhf]
                else:
                    cmd += ["--append-system-prompt", NEXUS_THINK_ZH]
        return cmd

    @staticmethod
    def _load_json(p: Path, default):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            return obj if isinstance(obj, type(default)) else default
        except Exception:
            return default

    def _save_json(self, p: Path, obj):
        try:
            p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            log(f"{self.ENGINE} save {p.name}: {exc}")

    def _save_pending(self):
        if self.PENDING_FILE is None:
            return
        with self._pending_save_lock:
            with self.lock:
                snapshot = dict(self.pending)
            temporary = self.PENDING_FILE.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.PENDING_FILE)

    def is_known(self, tid: str) -> bool:
        if tid in self.known or tid in self.pending or tid in self.turns:
            return True
        if tid in self.not_claude:
            return False
        p = self.find_session(tid)
        if p is None:
            p = self.find_session(tid, force=True)  # 绕开 15s 扫描缓存, 首条消息也必须路由正确
        if p is not None:
            self.known.add(tid)
            return True
        self.not_claude.add(tid)
        return False

    def list_threads(self, limit=60, q="", cwd="", archived=False):
        out = []
        ask = (q or "").lower()
        want_cwd = (cwd or "").lower()
        reg_all = self.session_registry()
        for tid, path in self.sessions_map().items():
            # Claude's internal child-agent logs are not independent desktop
            # chats. Keep them resolvable for diagnostics, not in the sidebar.
            if self.ENGINE == 'claude' and ('subagents' in path.parts or path.stem.startswith('agent-')):
                continue
            meta = claude_session_meta(path)
            if not meta:
                continue
            if (tid in self.archived) != bool(archived):
                continue
            mc = (meta.get("cwd") or "").lower()
            if want_cwd and mc != want_cwd:
                continue
            if ask:
                hay = " ".join([meta.get("name") or "", meta.get("preview") or "",
                                meta.get("lastPrompt") or "", meta.get("cwd") or ""]).lower()
                if ask not in hay:
                    continue
            reg = reg_all.get(tid) or {}
            run = self.is_running(tid)
            mt = int(meta.get("mtime") or 0)
            _cm, _ce = self.thread_model(tid)
            out.append({
                "id": tid, "engine": self.ENGINE,
                "model": _cm, "reasoningEffort": _ce,
                "name": meta.get("name") or "",
                "preview": meta.get("preview") or "",
                "cwd": meta.get("cwd") or reg.get("cwd") or "",
                "updatedAt": mt, "recencyAt": mt,
                "writable": True, "loaded": tid in self.turns, "running": run,
                "status": {"type": "active" if run else "idle"},
                "source": self.SOURCE,
            })
        seen={row['id'] for row in out}
        with self.lock:
            pending=dict(self.pending)
        for tid,entry in pending.items():
            if tid in seen or (tid in self.archived)!=bool(archived):continue
            directory=str(entry.get('cwd') or '')
            preview=str(entry.get('lastText') or '')
            if want_cwd and directory.lower()!=want_cwd:continue
            if ask and ask not in (preview+' '+directory).lower():continue
            model,effort=self.thread_model(tid)
            running=self.is_running(tid)
            stamp=int(entry.get('updated') or entry.get('created') or time.time())
            out.append({'id':tid,'engine':self.ENGINE,'model':model,'reasoningEffort':effort,
                        'name':'','preview':preview,'cwd':directory,'updatedAt':stamp,'recencyAt':stamp,
                        'writable':True,'running':running,'loaded':tid in self.turns,
                        'queueLen':len(self.queues.get(tid) or []),'pendingTranscript':True,
                        'source':self.SOURCE,'status':{'type':'active' if running else 'idle'}})
        out.sort(key=lambda x: x.get("recencyAt") or 0, reverse=True)
        return {"data": out[:limit], "engine": self.ENGINE}

    def new_thread(self, cwd=None, model=None, effort=None):
        tid = str(uuid.uuid4())
        cwd = cwd or blank_workspace()
        with self.lock:
            self.pending[tid] = {"cwd": cwd, "created": time.time()}
            self.known.add(tid)
            if model or effort:
                st = self.settings.setdefault(tid, {})
                if model:
                    st["model"] = model
                if effort:
                    st["effort"] = effort
                self._save_json(self.SETTINGS_FILE, self.settings)
        now_sec = int(time.time())
        self._save_pending()
        return {"id": tid, "engine": self.ENGINE, "cwd": cwd, "name": "", "preview": "",
                "pendingTranscript": True,
                "turns": [], "updatedAt": now_sec, "recencyAt": now_sec, "writable": True,
                "status": {"type": "idle"}, "source": self.SOURCE}

    def set_archived(self, tid: str, flag: bool):
        with self.lock:
            if flag:
                if tid not in self.archived:
                    self.archived.append(tid)
            else:
                self.archived = [x for x in self.archived if x != tid]
            self._save_json(self.ARCHIVE_FILE, self.archived)
        return True

    def rename(self, tid: str, name: str):
        path = self.find_session(tid)
        if path is None:
            raise ApiError({"code": "not_found", "message": self.LABEL + " 会话不存在"})
        line = {"type": "custom-title", "customTitle": name, "sessionId": tid}
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise ApiError({"code": "io", "message": str(exc)})
        _claude_meta_cache.pop(str(path), None)
        return True

    def delete(self, tid: str):
        path = self.find_session(tid)
        with self.lock:
            self.pending.pop(tid, None)
        self._save_pending()
        if path is None:
            return True
        try:
            self.TRASH_DIR.mkdir(parents=True, exist_ok=True)
            dest = self.TRASH_DIR / (path.stem + "-" + time.strftime("%Y%m%d-%H%M%S") + ".jsonl")
            shutil.move(str(path), str(dest))
        except Exception as exc:
            raise ApiError({"code": "io", "message": str(exc)})
        _claude_meta_cache.pop(str(path), None)
        self.sessions_map(force=True)
        return True

    def update_settings(self, tid: str, model=None, effort=None):
        with self.lock:
            st = self.settings.setdefault(tid, {})
            if model:
                st["model"] = model
            if effort:
                st["effort"] = effort
            self._save_json(self.SETTINGS_FILE, self.settings)
        return st

    def send_turn(self, tid, text, images=None, model=None, effort=None, cid=None):
        if not self.exe:
            raise ApiError({"code": "no_cli", "message": "本机未找到 %s CLI, %s 引擎不可用"
                                                     % (self.LABEL, self.LABEL)})
        with self.lock:
            if tid in self.turns:
                q = self.queues.setdefault(tid, [])
                if len(q) >= 20:
                    raise ApiError({"code": "queue_full",
                                    "message": "排队已满(20 条): 等当前回合结束或点「停止」后再发"})
                q.append({"id": "cq-" + uuid.uuid4().hex[:10], "cid": cid,
                          "text": text, "images": list(images or []),
                          "model": model, "effort": effort})
                pos = len(q)
            else:
                pos = 0
        if pos:
            self.hub.broadcast({"kind": "notification", "method": "item/completed", "params": {
                "threadId": tid, "item": {
                    "id": "cq-" + uuid.uuid4().hex[:8], "type": "agentMessage",
                    "text": "⏳ 已排队（第 %d 条）：当前回合结束后自动发送" % pos}}})
            return {"turnId": None, "queued": True, "position": pos, "engine": self.ENGINE,
                    "queueId": q[-1].get("id")}
        return self._spawn_turn(tid, text, images, model, effort, cid)

    def _spawn_turn(self, tid, text, images=None, model=None, effort=None, cid=None):
        payload = {"id": "cq-" + uuid.uuid4().hex[:10], "cid": cid,
                   "text": text, "images": list(images or []), "model": model, "effort": effort}
        content = []
        if text:
            content.append({"type": "text", "text": text})
        for pth in (images or []):
            try:
                raw = Path(str(pth)).read_bytes()
            except OSError:
                continue
            _sfx = Path(str(pth)).suffix.lower()
            if _sfx not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                log("%s: 跳过不支持的图片 %s" % (self.ENGINE, _sfx))
                continue
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".gif": "image/gif"}[_sfx]
            content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                        "data": base64.b64encode(raw).decode("ascii")}})
        if not content:
            content.append({"type": "text", "text": "(空消息)"})
        msg = {"type": "user", "message": {"role": "user", "content": content}}

        def _queue_front_locked():
            q = self.queues.setdefault(tid, [])
            if len(q) >= 20:
                raise ApiError({"code": "queue_full",
                                "message": "排队已满(20 条): 等当前回合结束或点「停止」后再发"})
            q.insert(0, dict(payload))

        with self.lock:
            self.known.add(tid)
            race = tid in self.turns  # 上一回合刚结束/排空续跑窗口: 不并发第二个进程
            if race:
                _queue_front_locked()
            else:
                path = self.find_session(tid)
                if path is None:
                    # 15s 扫描缓存可能还没看到刚结束回合写出的 jsonl -> 强制刷新一次, 否则误报 not_found
                    path = self.find_session(tid, force=True)
                pend = self.pending.get(tid)
                if path is None and pend is None:
                    if self.ENGINE == "workbuddy" and (wb_desktop_db_meta().get(tid) or {}):
                        raise ApiError({"code": "transcript_missing", "message": WB_MISSING_TRANSCRIPT})
                    raise ApiError({"code": "not_found", "message": "%s 会话不存在" % self.LABEL})
                st = self.settings.setdefault(tid, {})
                if model:
                    st["model"] = model
                if effort:
                    st["effort"] = effort
                if model or effort:
                    self._save_json(self.SETTINGS_FILE, self.settings)
                model = st.get("model")
                effort = st.get("effort")
                _okm, _whym = engine_model_ok(self.ENGINE, model)
                if not _okm:
                    if self.ENGINE == "workbuddy":
                        raise ApiError({"code": "model_not_found", "message": _whym})
                    log("%s %s: 丢弃不属于本引擎的模型 %r（回落本机默认）"
                        % (self.ENGINE, tid[:8], model))
                    model = None
                    st.pop("model", None)
                    self._save_json(self.SETTINGS_FILE, self.settings)
                _oke, _whye = engine_effort_ok(self.ENGINE, effort)
                if not _oke:
                    log("%s %s: 丢弃不支持的思考程度 %r" % (self.ENGINE, tid[:8], effort))
                    effort = None
                    st.pop("effort", None)
                    self._save_json(self.SETTINGS_FILE, self.settings)
                cwd = (pend or {}).get("cwd") or ""
        if race:
            self.hub.broadcast({"kind": "notification", "method": "item/completed", "params": {
                "threadId": tid, "item": {
                    "id": "cq-" + uuid.uuid4().hex[:8], "type": "agentMessage",
                    "text": "⏳ 已排队（第 1 条）：当前回合结束后自动发送"}}})
            return {"turnId": None, "queued": True, "position": 1, "engine": self.ENGINE}

        if not cwd and path is not None:
            cwd = claude_session_meta(path).get("cwd") or ""
        if not cwd or not os.path.isdir(cwd):
            cwd = blank_workspace()
        cmd = self._make_cmd(tid, path, model, effort)
        turn_id = self.TURN_PREFIX + uuid.uuid4().hex[:12]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with self.lock:
            if tid in self.turns:
                # 极小窗口: 别的线程抢先启动 -> 同样插队首, 绝不并发双进程
                _queue_front_locked()
                race = True
            else:
                proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, creationflags=flags,
                                        env=self.spawn_env_for(tid, path),
                                        start_new_session=(os.name != "nt"))
                self.turns[tid] = proc
                gen = self.turn_gen.get(tid, 0) + 1
                self.turn_gen[tid] = gen
                self.turn_ids[tid] = turn_id
                self.forced_stop.pop(tid, None)
                if self.PENDING_FILE:
                    self.pending.setdefault(tid, {"cwd": cwd, "created": time.time()}).update(
                        lastText=text, lastError="", lastStatus="running", turnId=turn_id, updated=time.time())
                else:
                    self.pending.pop(tid, None)
                race = False
        if race:
            self.hub.broadcast({"kind": "notification", "method": "item/completed", "params": {
                "threadId": tid, "item": {
                    "id": "cq-" + uuid.uuid4().hex[:8], "type": "agentMessage",
                    "text": "⏳ 已排队（第 1 条）：当前回合结束后自动发送"}}})
            return {"turnId": None, "queued": True, "position": 1, "engine": self.ENGINE}
        self._save_pending()
        self.hub.broadcast({"kind": "notification", "method": "turn/started",
                            "params": {"threadId": tid, "turn": {"id": turn_id, "engine": self.ENGINE}}})
        threading.Thread(target=self._stdin_writer, args=(proc, msg), daemon=True).start()
        threading.Thread(target=self._reader, args=(tid, proc, gen, turn_id), daemon=True).start()
        return {"turnId": turn_id, "cwd": cwd, "engine": self.ENGINE}

    def interrupt(self, tid: str, clear_queue: bool = False) -> dict:
        """停止当前回合。默认 **保留** 队列: 立刻续跑下一条, 不等旧进程收尾。

        旧实现的坑: 杀完进程要等 _reader 的 proc.wait(timeout=8) 走完才推队列,
        这 8 秒里 tid 还在 self.turns 里 -> 新消息只能继续排队, 用户看到的就是
        “停了但发不出去”。现在改成 interrupt 里主动摘状态 + 立刻起下一条。
        """
        with self.lock:
            proc = self.turns.get(tid)
            turn_id = self.turn_ids.get(tid) or self.SHORT
            kept = len(self.queues.get(tid) or [])
            dropped = len(self.queues.pop(tid, None) or []) if clear_queue else 0
            nxt = None
            if proc is not None:
                # 先摘 running 状态 + 换代: 旧 reader 之后无论多晚收尾都不许再动状态
                if self.turns.get(tid) is proc:
                    self.turns.pop(tid, None)
                    self.turn_ids.pop(tid, None)
                self.turn_gen[tid] = self.turn_gen.get(tid, 0) + 1
                self.forced_stop[tid] = time.time()
                if not clear_queue:
                    q = self.queues.get(tid) or []
                    if q:
                        nxt = q.pop(0)
                        if not q:
                            self.queues.pop(tid, None)
            kept_after = len(self.queues.get(tid) or [])
        if dropped:
            self.hub.broadcast({"kind": "notification", "method": "item/completed", "params": {
                "threadId": tid, "item": {
                    "id": "cq-" + uuid.uuid4().hex[:8], "type": "agentMessage",
                    "text": "⏹ 已停止并清空 %d 条排队消息（未发送）" % dropped}}})
        if proc is not None:
            self._kill_tree(proc)
            # 杀完、起下一条之前: 子类在这里补引擎侧的收尾(WB 要闭合未完成的 user 消息,
            # 否则 --resume 会把那条旧消息当成待执行请求重跑)。
            try:
                self._after_kill_before_next(tid, proc)
            except Exception as exc:
                log("%s after-kill %s: %r" % (self.ENGINE, tid[:8], exc))
            # 先让 UI 停表(旧回合中止), 紧接着新回合的 turn/started 会再接管
            self.hub.broadcast({"kind": "notification", "method": "turn/completed",
                                "params": {"threadId": tid,
                                           "turn": {"id": turn_id, "status": "aborted",
                                                    "engine": self.ENGINE}}})
            if nxt is not None:
                try:
                    self._spawn_turn(tid, nxt.get("text") or "", nxt.get("images") or [],
                                     nxt.get("model"), nxt.get("effort"), nxt.get("cid"))
                except Exception as exc:
                    log("%s interrupt->next %s: %r" % (self.ENGINE, tid[:8], exc))
                    with self.lock:                 # 发不出去 -> 放回队首, 一条不丢
                        self.queues.setdefault(tid, []).insert(0, nxt)
                    nxt = None
            return {"stopped": True, "queueKept": kept_after, "cleared": dropped,
                    "nextStarted": nxt is not None, "engine": self.ENGINE}
        return {"stopped": False, "queueKept": kept, "cleared": dropped, "engine": self.ENGINE,
                "reason": "no_running_turn"}

    def _after_kill_before_next(self, tid, proc):
        """杀进程后的引擎钩子(默认无操作)。"""
        return None

    # ---------- Claude 队列视图 / 操作(网关内存队列) ----------

    def queue_view(self, tid: str) -> list:
        with self.lock:
            q = list(self.queues.get(tid) or [])
        return [{"id": it.get("id"), "text": it.get("text") or "",
                 "images": list(it.get("images") or []),
                 "clientUserMessageId": it.get("cid"), "position": i + 1}
                for i, it in enumerate(q)]

    def queue_len(self, tid: str) -> int:
        with self.lock:
            return len(self.queues.get(tid) or [])

    def queue_delete(self, tid: str, qid: str) -> bool:
        with self.lock:
            q = self.queues.get(tid) or []
            for i, it in enumerate(q):
                if it.get("id") == qid:
                    q.pop(i)
                    if not q:
                        self.queues.pop(tid, None)
                    return True
        return False

    def queue_top(self, tid: str, qid: str) -> bool:
        with self.lock:
            q = self.queues.get(tid) or []
            for i, it in enumerate(q):
                if it.get("id") == qid:
                    q.insert(0, q.pop(i))
                    return True
        return False

    def queue_clear(self, tid: str) -> int:
        with self.lock:
            return len(self.queues.pop(tid, None) or [])

    def queue_send(self, tid: str, qid=None) -> dict:
        """立即把某条排队消息发出去(跳过等待)。忙 -> busy(不丢消息)。"""
        with self.lock:
            if tid in self.turns:
                raise ApiError({"code": "busy",
                                "message": "当前回合还在跑，没法插队；先「停止」或等它结束，队列会自动推进。"})
            q = self.queues.get(tid) or []
            idx = None
            for i, it in enumerate(q):
                if qid is None or it.get("id") == qid:
                    idx = i
                    break
            if idx is None:
                raise ApiError({"code": "invalid_request", "message": "队列是空的"})
            item = q.pop(idx)
            if not q:
                self.queues.pop(tid, None)
        try:
            r = self._spawn_turn(tid, item.get("text") or "", item.get("images") or [],
                                 item.get("model"), item.get("effort"), item.get("cid"))
        except Exception:
            with self.lock:                      # 发不出去 → 放回队首, 不丢
                self.queues.setdefault(tid, []).insert(0, item)
            raise
        return {"ok": True, "engine": self.ENGINE, "result": r}

    @staticmethod
    def _kill_tree(proc):
        """杀进程树(防 Bash 工具子进程成孤儿继续跑)。
        Windows: taskkill /T; macOS/Linux: killpg(子进程已 start_new_session 独立成组)。"""
        _pid = int(proc.pid)
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(_pid)],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
        else:
            try:
                import signal as _signal
                os.killpg(os.getpgid(_pid), _signal.SIGKILL)
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass

    def _stdin_writer(self, proc, msg):
        try:
            data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            proc.stdin.write(data)
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass

    def _reader(self, tid, proc, gen=0, turn_id=None):
        started = time.time()
        turn_id = turn_id or self.turn_ids.get(tid) or self.SHORT
        finished = False
        emitted_text = False
        cur_msg = ""
        tool_meta = {}
        block_ord = {}
        stderr_buf = []
        hub = self.hub
        tok = {"ctx": 0, "cache_read": 0, "cache_write": 0, "out_done": 0, "call_out": 0,
               "est": 0.0, "win": None, "cost": None, "last": 0.0}

        def tok_norm():
            out = int(tok["out_done"] + max(tok["call_out"], tok["est"]))
            ctx = int(tok["ctx"])
            return {"inputTokens": ctx, "outputTokens": out,
                    "contextWindow": int(tok["win"]) if tok["win"] else 0,
                    "costUsd": float(tok["cost"]) if tok["cost"] is not None else 0}

        def tok_payload():
            n = tok_norm()
            total = {"totalTokens": n["inputTokens"] + n["outputTokens"],
                     "inputTokens": n["inputTokens"],
                     "cachedInputTokens": int(tok["cache_read"]),
                     "cacheWriteInputTokens": int(tok["cache_write"]),
                     "outputTokens": n["outputTokens"], "reasoningOutputTokens": 0}
            return {"threadId": tid, "turnId": turn_id, "engine": self.ENGINE,
                    "tokenUsage": {"total": total, "last": dict(total),
                                   "modelContextWindow": int(tok["win"]) if tok["win"] else None},
                    "costUsd": n["costUsd"]}

        def emit_tokens(force=False):
            now = time.time()
            if not force and now - tok["last"] < 0.25:
                return
            tok["last"] = now
            notif("thread/tokenUsage/updated", tok_payload())

        def fold_call():
            tok["out_done"] += int(max(tok["call_out"], tok["est"]))
            tok["call_out"] = 0
            tok["est"] = 0.0

        def err_reader():
            try:
                for raw in iter(proc.stderr.readline, b""):
                    s = raw.decode("utf-8", "replace").rstrip()
                    if s:
                        stderr_buf.append(s)
                        if len(stderr_buf) > 40:
                            del stderr_buf[:20]
            except Exception:
                pass

        def notif(method, params):
            # 被 interrupt 顶掉的旧进程: 管道里残留的尾流一律丢弃,
            # 否则会插到新回合中间, 看起来像时序错乱。
            if self.turn_gen.get(tid, 0) != gen:
                return
            params.setdefault("turnId", turn_id)
            hub.broadcast({"kind": "notification", "method": method, "params": params})

        threading.Thread(target=err_reader, daemon=True).start()
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("parent_tool_use_id"):
                    continue  # 子代理侧链: 不混入主对话流
                t = obj.get("type")
                if t == "system":
                    continue
                if t == "stream_event":
                    ev = obj.get("event") or {}
                    et = ev.get("type")
                    mid = obj.get("api_message_id") or cur_msg
                    if et == "message_start":
                        fold_call()
                        mus = (ev.get("message") or {}).get("usage") or {}
                        if isinstance(mus, dict) and mus:
                            tok["cache_read"] = int(mus.get("cache_read_input_tokens") or 0)
                            tok["cache_write"] = int(mus.get("cache_creation_input_tokens") or 0)
                            tok["ctx"] = (int(mus.get("input_tokens") or 0)
                                          + tok["cache_read"] + tok["cache_write"])
                        cur_msg = str((ev.get("message") or {}).get("id") or mid)
                        emit_tokens(force=True)
                        continue
                    if et == "content_block_delta":
                        idx = int(ev.get("index") or 0)
                        d = ev.get("delta") or {}
                        dt = d.get("type")
                        if self.ENGINE == "workbuddy":
                            # WorkBuddy 落盘是「一条记录一个块」: 正文记录 id == api_message_id,
                            # 与 wb_read_thread / wb_tail_convert 的 <rid>-t<k> 对齐。
                            # 思考块落盘时是另一条 reasoning 记录(其 id 此时还不知道), 所以
                            # 这里不推流, 由 assistant/tail 的 <rid>-r 一次成卡; 否则会多出一张思考卡。
                            iid = "%s-t0" % (mid or ("wb-%s" % tid))
                        else:
                            iid = "%s-%d" % (mid, idx)
                        if dt == "thinking_delta" and d.get("thinking"):
                            tok["est"] += _est_tokens(d["thinking"])
                            emit_tokens()
                            if self.ENGINE != "workbuddy":
                                notif("item/reasoning/textDelta",
                                      {"threadId": tid, "itemId": iid, "delta": d["thinking"]})
                        elif dt == "text_delta" and d.get("text"):
                            emitted_text = True
                            tok["est"] += _est_tokens(d["text"])
                            emit_tokens()
                            notif("item/agentMessage/delta",
                                  {"threadId": tid, "itemId": iid, "delta": d["text"]})
                    if et == "message_delta":
                        mus = ev.get("usage") or {}
                        if isinstance(mus, dict):
                            # CodeBuddy(WB) 的 message_start.usage 全是 0, 真实 input 只在这里
                            # (实测 message_delta.usage.input_tokens=25505); Claude 那边
                            # message_delta 一般不带 input_tokens, 有值才覆盖, 不影响 CC。
                            if mus.get("input_tokens") is not None:
                                tok["ctx"] = (int(mus.get("input_tokens") or 0)
                                              + int(mus.get("cache_read_input_tokens") or 0)
                                              + int(mus.get("cache_creation_input_tokens") or 0))
                            if mus.get("output_tokens") is not None:
                                tok["call_out"] = max(tok["call_out"], int(mus.get("output_tokens") or 0))
                        emit_tokens(force=True)
                        continue
                    continue
                if t == "assistant":
                    msg = obj.get("message") or {}
                    if not isinstance(msg, dict):
                        continue
                    mid = str(msg.get("id") or obj.get("uuid") or "")
                    _wb_engine = (self.ENGINE == "workbuddy")
                    for _bi, b in enumerate(msg.get("content") or []):
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if _wb_engine:
                            # 与 wb_read_thread / wb_tail_convert 完全同 id:
                            # 思考记录 -> <rid>-r, 正文第 k 块 -> <rid>-t<k>
                            iid = ("%s-r" % mid) if bt == "thinking" else ("%s-t%d" % (mid, _bi))
                        else:
                            iid = _claude_next_iid(block_ord, mid)
                        if bt == "thinking":
                            txt = b.get("thinking") or ""
                            if txt.strip():
                                notif("item/completed", {"threadId": tid, "item": {
                                    "id": iid, "type": "reasoning", "summary": [], "content": [txt]}})
                        elif bt == "text":
                            txt = b.get("text") or ""
                            if txt.strip():
                                notif("item/completed", {"threadId": tid, "item": {
                                    "id": iid, "type": "agentMessage", "text": txt}})
                        elif bt == "tool_use":
                            it = _claude_tool_item(b)
                            tool_meta[it["id"]] = it
                            notif("item/started", {"threadId": tid, "item": it})
                    continue
                if t == "user":
                    msg = obj.get("message") or {}
                    for b in (msg.get("content") or []):
                        if not isinstance(b, dict) or b.get("type") != "tool_result":
                            continue
                        base = tool_meta.get(b.get("tool_use_id"))
                        if base is None:
                            continue
                        out = tool_result_output(obj.get("tool_use_result") or {}, b.get("content"))
                        done = dict(base)
                        done["status"] = "failed" if b.get("is_error") else "completed"
                        done["exitCode"] = 1 if b.get("is_error") else 0
                        if done.get("type") == "commandExecution":
                            done["aggregatedOutput"] = _clip(out, MAX_OUTPUT)
                        else:
                            done["output"] = _clip(out, MAX_OUTPUT)
                            if b.get("is_error"):
                                done["error"] = _clip(out, 800) or "error"
                        notif("item/completed", {"threadId": tid, "item": done})
                    continue
                if t == "result":
                    finished = True
                    txt = obj.get("result")
                    failed = bool(obj.get("is_error") or str(obj.get("subtype") or "").startswith("error"))
                    if failed:
                        messages = obj.get("errors") or obj.get("error") or txt or "客户端未能完成请求"
                        if isinstance(messages, list):
                            messages = "\n".join(str(message.get("message") or message) if isinstance(message, dict) else str(message) for message in messages)
                        elif isinstance(messages, dict):
                            messages = str(messages.get("message") or messages)
                        message = re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', str(messages))[:1500]
                        with self.lock:
                            if tid in self.pending:
                                self.pending[tid].update(lastError=message, lastStatus="failed", updated=time.time())
                        self._save_pending()
                        notif("item/completed", {"threadId": tid, "item": {
                            "id": "cerr-" + turn_id, "type": "agentMessage", "text": "⚠ 请求失败：" + message}})
                        notif("turn/completed", {"threadId": tid, "turn": {
                            "id": turn_id, "status": "failed", "engine": self.ENGINE,
                            "error": {"message": message}, "durationMs": obj.get("duration_ms") or int((time.time() - started) * 1000)}})
                        continue
                    if not emitted_text and isinstance(txt, str) and txt.strip():
                        notif("item/completed", {"threadId": tid, "item": {
                            "id": "cres-%s" % str(obj.get("session_id") or "")[:8],
                            "type": "agentMessage", "text": txt}})
                    rus = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
                    if rus:
                        fold_call()
                        if rus.get("output_tokens") is not None:
                            tok["out_done"] = max(tok["out_done"], int(rus.get("output_tokens") or 0))
                    mu = obj.get("modelUsage")
                    if isinstance(mu, dict):
                        for _md in mu.values():
                            cw = _md.get("contextWindow") if isinstance(_md, dict) else None
                            if cw:
                                tok["win"] = int(cw)
                                break
                    if obj.get("total_cost_usd") is not None:
                        tok["cost"] = float(obj.get("total_cost_usd") or 0)
                    emit_tokens(force=True)
                    n = tok_norm()
                    rec = {"inputTokens": n["inputTokens"], "outputTokens": n["outputTokens"],
                           "contextWindow": n["contextWindow"], "costUsd": n["costUsd"],
                           "ts": int(time.time())}
                    self.last_usage[tid] = rec
                    if len(self.last_usage) > 300:
                        _keep = sorted(self.last_usage.items(),
                                       key=lambda kv: (kv[1] or {}).get("ts") or 0)[-200:]
                        self.last_usage = dict(_keep)
                    self._save_json(self.USAGE_FILE, self.last_usage)
                    dur = obj.get("duration_ms") or int((time.time() - started) * 1000)
                    notif("turn/completed", {"threadId": tid, "turn": {
                        "id": turn_id, "status": "completed", "durationMs": dur,
                        "costUsd": obj.get("total_cost_usd"), "engine": self.ENGINE,
                        "usage": rec}})
                    continue
        except Exception as exc:
            log(f"{self.ENGINE} reader {tid[:8]}: {exc}")
        finally:
            # 只有“当前这一代”负责收尾/续跑。被 interrupt 顶掉的旧进程可能还被
            # 孙子进程占着 stdout(EOF 晚到), 绝不能让它去动新回合的状态。
            with self.lock:
                superseded = (self.turn_gen.get(tid) or 0) != gen
                if not superseded and self.turns.get(tid) is proc:
                    self.turns.pop(tid, None)
                    self.turn_ids.pop(tid, None)
                nxt = None
                if not superseded:
                    q = self.queues.get(tid) or []
                    if q:
                        nxt = q.pop(0)
                        if not q:
                            self.queues.pop(tid, None)
            if superseded:
                return
            if self.PENDING_FILE and self.find_session(tid, force=True) is not None and not (self.pending.get(tid) or {}).get("lastError"):
                with self.lock:
                    self.pending.pop(tid, None)
                self._save_pending()
            try:
                if proc.poll() is None:
                    proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if not finished:
                err = "\n".join(stderr_buf[-12:]).strip()
                if err:
                    hub.broadcast({"kind": "notification", "method": "item/completed", "params": {
                        "threadId": tid, "item": {"id": "cerr-" + uuid.uuid4().hex[:8], "type": "agentMessage",
                                                   "text": "⚠️ %s 进程结束(未产出结果):\n```\n" % self.LABEL + _clip(err, 1500) + "\n```"}}})
                hub.broadcast({"kind": "notification", "method": "turn/completed",
                               "params": {"threadId": tid,
                                          "turn": {"id": turn_id, "status": "failed" if err else "aborted", "engine": self.ENGINE,
                                                   "error": {"message": re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', err)[:1500]} if err else None}}})
            if nxt is not None:
                try:
                    self._spawn_turn(tid, nxt.get("text") or "", nxt.get("images") or [],
                                     nxt.get("model"), nxt.get("effort"), nxt.get("cid"))
                except ApiError as exc:
                    log(f"{self.ENGINE} queue drain {tid[:8]}: {exc.err}")
                    hub.broadcast({"kind": "notification", "method": "item/completed", "params": {
                        "threadId": tid, "item": {
                            "id": "cqe-" + uuid.uuid4().hex[:8], "type": "agentMessage",
                            "text": "⚠️ 排队消息发送失败: %s" % (exc.err.get("message") if isinstance(exc.err, dict) else exc.err)}}})
                except Exception as exc:
                    log(f"{self.ENGINE} queue drain {tid[:8]}: {exc}")


# ---------- WorkBuddy (CodeBuddy Code) 引擎: 第三引擎 ----------

_wb_reg_cache = {"ts": 0.0, "data": {}}


def wb_session_registry(force=False) -> dict:
    """~/.codebuddy/sessions/<pid>.json -> {sessionId: {pid, cwd, status}}。

    与 Claude 的差别: CodeBuddy 没有 status 字段, lastHeartbeat 只证明进程活着
    (空闲时也在跳), 所以这里不据此推断 busy —— 误判 running 会让新消息只排队不发送。
    回合真值只认 WorkBuddyAS.turns / forced_stop, 这里只补 pid/cwd。
    """
    now = time.time()
    if not force and now - _wb_reg_cache["ts"] < 5:
        return _wb_reg_cache["data"]
    data = {}
    try:
        _reg_paths = list(WB_SESSIONS_DIR.glob("*.json"))
        try:
            _reg_paths += list((WB_DESKTOP_HOME / "sessions").glob("*.json"))
        except OSError:
            pass
        for p in _reg_paths:
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = j.get("sessionId")
            try:
                pid = int(j.get("pid") or 0)
            except Exception:
                pid = 0
            sid = str(sid or "")
            # interactive-<pid> / prewarm-* 是宿主进程槽位, 不是会话 id
            if not sid or sid.startswith(("interactive-", "prewarm-")):
                continue
            if not pid or not _pid_alive(pid):
                continue
            data.setdefault(sid, {"pid": pid, "cwd": str(j.get("cwd") or ""),
                                  "status": "", "hb": int(j.get("lastHeartbeat") or 0)})
    except OSError:
        pass
    _wb_reg_cache.update({"ts": now, "data": data})
    return data


def wb_sessions_map(force=False) -> dict:
    """WB 会话有两个落点: CLI(~/.codebuddy) + 桌面端(~/.workbuddy)。
    同 id 以 CLI 为准(网关新建的会话都在那里)。"""
    out = {}
    for _root in WB_PROJECTS_ALL:
        try:
            for _k, _v in claude_sessions_map(force=force, root=_root).items():
                out.setdefault(_k, _v)
        except OSError:
            continue
    return out


def find_wb_session(tid: str, force=False):
    return wb_sessions_map(force=force).get(tid)


def _wb_args(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {"raw": raw}
        except Exception:
            return {"raw": raw}
    return {}


def _wb_tool_item(name, args, cid):
    name = str(name or "tool")
    if name in ("Bash", "PowerShell"):
        cmd = args.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        return {"id": cid, "type": "commandExecution", "command": _clip(cmd or name, 2000),
                "status": "in_progress", "exitCode": None, "aggregatedOutput": ""}
    return {"id": cid, "type": "mcpToolCall", "toolName": name, "arguments": args, "output": None}


def _wb_ts_sec(ts) -> int:
    """WorkBuddy 的 timestamp 是毫秒(1792221367365); 已是秒就原样返回。"""
    try:
        v = float(ts or 0)
    except Exception:
        return 0
    return int(v / 1000.0) if v > 1e11 else int(v)


def _wb_usage_of(obj: dict):
    """WorkBuddy 行 -> {inputTokens, outputTokens, contextWindow, costUsd, ts}(没有就 None)。

    落盘三种承载: providerData.usage(驼峰) > providerData.rawUsage(OpenAI 原样)
    > message.usage(input/output_tokens)。credit 是额度不是美元, 不映射到 costUsd。
    """
    if not isinstance(obj, dict):
        return None
    pd = obj.get("providerData") if isinstance(obj.get("providerData"), dict) else {}
    u = pd.get("usage") if isinstance(pd.get("usage"), dict) else {}
    ru = pd.get("rawUsage") if isinstance(pd.get("rawUsage"), dict) else {}
    it = int(u.get("inputTokens") or ru.get("prompt_tokens") or 0)
    ot = int(u.get("outputTokens") or ru.get("completion_tokens") or 0)
    if not it and not ot:
        mu = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        mu = mu.get("usage") if isinstance(mu.get("usage"), dict) else {}
        it = int(mu.get("input_tokens") or 0)
        ot = int(mu.get("output_tokens") or 0)
    if it <= 0 and ot <= 0:
        return None
    return {"inputTokens": max(0, it), "outputTokens": max(0, ot),
            "contextWindow": 0, "costUsd": None, "ts": _wb_ts_sec(obj.get("timestamp"))}


def _wb_image_path(block) -> str:
    """WB 图片块 -> 本地文件。兼容 image(source.base64) / input_image(image_url= data URL)。"""
    if not isinstance(block, dict):
        return ""
    p = _claude_save_image_block(block)
    if p:
        return p
    url = block.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url.startswith("data:"):
        return ""
    try:
        head, data = url.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
        return _claude_save_image_block({"source": {"type": "base64", "media_type": mime, "data": data}})
    except Exception:
        return ""


def _wb_text_of_blocks(blocks) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    out = []
    for b in blocks:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            out.append(b["text"])
    return "\n".join(out)


def wb_read_thread(tid: str, light: bool = False) -> dict:
    """WorkBuddy(CodeBuddy) 会话 JSONL -> Codex 形状 thread。

    行类型与 Claude 不同: message(role/content input_text|output_text) / reasoning /
    function_call / function_call_result，工具结果为 OpenAI 形状而非 tool_use。
    """
    now_sec = int(time.time())
    path = find_wb_session(tid)
    if path is None:
        path = find_wb_session(tid, force=True)
    if path is None:
        pend = (WB.pending.get(tid) if WB is not None else None)
        if pend:
            turn_id = str(pend.get("turnId") or tid)
            items = []
            if pend.get("lastText"):
                items.append({"type": "userMessage", "id": turn_id + "-user",
                              "content": [{"type": "text", "text": pend["lastText"]}]})
            if pend.get("lastError"):
                items.append({"type": "agentMessage", "id": "cerr-" + turn_id,
                              "text": "⚠ 请求失败：" + pend["lastError"]})
            model, effort = wb_thread_model(tid)
            return {"id": tid, "engine": "workbuddy", "model": model, "reasoningEffort": effort,
                    "cwd": pend.get("cwd") or "", "name": "", "preview": pend.get("lastText") or "",
                    "turns": [{"id": turn_id, "items": items, "status": pend.get("lastStatus") or "idle",
                               "error": {"message": pend["lastError"]} if pend.get("lastError") else None}] if items else [],
                    "updatedAt": pend.get("updated") or now_sec, "recencyAt": pend.get("updated") or now_sec,
                    "writable": True, "status": {"type": "active" if tid in WB.turns else "idle"}, "source": "codebuddy-code"}
        if WB is not None and tid in WB.known and not wb_desktop_db_meta().get(tid):
            busy = tid in WB.turns
            return {"id": tid, "engine": "workbuddy", "cwd": "", "name": "",
                    "preview": "", "turns": [], "updatedAt": now_sec, "recencyAt": now_sec,
                    "writable": True, "status": {"type": "active" if busy else "idle"},
                    "source": "codebuddy-code"}
        _dbm = wb_desktop_db_meta().get(tid) or {}
        if _dbm and tid not in (WB.hidden if WB is not None else ()):
            # JSONL 已被 WorkBuddy 清理, 但桌面端索引还在: 列出来并明确标注,
            # 不能当 404 吞掉(用户会以为历史丢了), 也不能当可写会话(续发必然失败)。
            _cm, _ce = wb_thread_model(tid)
            _mt = int(_dbm.get("updatedAt") or 0) or now_sec
            return trim_thread({
                "id": tid, "engine": "workbuddy",
                "model": _cm, "reasoningEffort": _ce,
                "cwd": _dbm.get("cwd") or "",
                "name": _dbm.get("title") or "",
                "preview": "", "turns": [],
                "updatedAt": _mt, "recencyAt": _mt,
                "writable": False, "missingTranscript": True,
                "missingReason": WB_MISSING_TRANSCRIPT,
                "artifacts": wb_artifact_summary(tid),
                "status": {"type": "idle"}, "source": "codebuddy-code",
            })
        raise ApiError({"code": "not_found", "message": "workbuddy 会话不存在: " + tid})
    meta = claude_session_meta(path)
    turns = []
    cur = None
    tool_index = {}
    prev_user_had_img = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or len(line) > 16000000:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if not isinstance(j, dict):
                    continue
                t = j.get("type")
                if t in ("file-history-snapshot", "system"):
                    continue
                row_id = str(j.get("id") or "")
                if t == "reasoning":
                    txt = "".join(str(b.get("text") or "") for b in (j.get("rawContent") or [])
                                  if isinstance(b, dict)).strip()
                    if not txt:
                        txt = _wb_text_of_blocks(j.get("content")).strip()
                    if not txt:
                        continue
                    if cur is None:
                        cur = {"id": "t%d" % len(turns), "items": []}
                        turns.append(cur)
                    cur["items"].append({"id": (row_id or uuid.uuid4().hex) + "-r",
                                         "type": "reasoning", "summary": [], "content": [txt]})
                    continue
                if t == "message":
                    role = j.get("role")
                    blocks = j.get("content")
                    blocks = blocks if isinstance(blocks, list) else (
                        [{"type": "text", "text": blocks}] if isinstance(blocks, str) else [])
                    if role == "user":
                        txt = _wb_clean_text(_wb_text_of_blocks(blocks))
                        imgs = [b for b in blocks if isinstance(b, dict)
                                and b.get("type") in ("image", "input_image", "image_url")]
                        ref = _claude_img_ref(txt)
                        if ref and not prev_user_had_img and os.path.isfile(ref):
                            cur = {"id": "t%d" % len(turns), "items": []}
                            turns.append(cur)
                            cur["items"].append({"id": (row_id or "u%d" % len(turns)) + "-u",
                                                 "type": "userMessage",
                                                 "content": [{"type": "localImage", "path": ref}]})
                            continue
                        prev_user_had_img = bool(imgs)
                        if txt or imgs:
                            cur = {"id": "t%d" % len(turns), "items": []}
                            turns.append(cur)
                            citems = []
                            if txt:
                                citems.append({"type": "text", "text": txt})
                            for b in imgs[:8]:
                                pth = _wb_image_path(b)
                                if pth:
                                    citems.append({"type": "localImage", "path": pth})
                            cur["items"].append({"id": (row_id or "u%d" % len(turns)) + "-u",
                                                 "type": "userMessage", "content": citems})
                        continue
                    if role == "assistant":
                        if _wb_is_interrupt_row(j):
                            continue          # 官方中断闭合记录: 不是真回复, 不渲染
                        if cur is None:
                            cur = {"id": "t%d" % len(turns), "items": []}
                            turns.append(cur)
                        for k, b in enumerate(blocks):
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") in ("output_text", "text") and str(b.get("text") or "").strip():
                                cur["items"].append({"id": "%s-t%d" % (row_id or uuid.uuid4().hex, k),
                                                     "type": "agentMessage", "text": b.get("text")})
                        continue
                if t == "function_call":
                    cid = str(j.get("callId") or row_id or uuid.uuid4().hex)
                    if cid in tool_index:
                        continue          # 同一 callId 重复落盘: 只保留第一张工具卡
                    if cur is None:
                        cur = {"id": "t%d" % len(turns), "items": []}
                        turns.append(cur)
                    it = _wb_tool_item(j.get("name"), _wb_args(j.get("arguments")), cid)
                    cur["items"].append(it)
                    tool_index[cid] = it
                    continue
                if t == "function_call_result":
                    cid = str(j.get("callId") or "")
                    it = tool_index.get(cid)
                    if it is None:
                        continue
                    out = ""
                    o = j.get("output")
                    if isinstance(o, dict):
                        out = str(o.get("text") or "")
                    elif isinstance(o, str):
                        out = o
                    elif isinstance(o, list):
                        # WB 有的工具(present_files / conversation_search)把 output 写成
                        # [{type:"input_text",text:"..."}], 只认 dict/str 会整段吞掉。
                        out = _wb_text_of_blocks(o)
                    if not out:
                        out = _wb_text_of_blocks(j.get("content"))
                    ok = str(j.get("status") or "completed") != "error"
                    _has = bool(it.get("aggregatedOutput") or it.get("output"))
                    if it.get("type") == "commandExecution":
                        if out or not _has:
                            it["aggregatedOutput"] = _clip(out, MAX_OUTPUT)
                    else:
                        if out or not _has:
                            it["output"] = _clip(out, MAX_OUTPUT)
                    it["status"] = "completed" if ok else "failed"
                    it["exitCode"] = 0 if ok else 1
                    if not ok and it.get("type") != "commandExecution":
                        it["error"] = _clip(out, 800) or "error"
                    continue
    except OSError as exc:
        raise ApiError({"code": "io", "message": str(exc)})
    # 桌面端中断/崩溃时只有 function_call 没有 result: 不能让历史里那张卡永远转圈。
    # 判据 = 网关没在跑这个 turn, 且文件 3 分钟没动过(真跑着的会话不误伤)。
    try:
        _quiet = (time.time() - os.path.getmtime(path)) > 180
    except OSError:
        _quiet = True
    _live = False
    if WB is not None:
        try:
            _live = tid in WB.turns
        except Exception:
            _live = False
    if _quiet and not _live:
        for _it in tool_index.values():
            if str(_it.get("status")) in ("in_progress", "running"):
                _it["status"] = "failed"
                if _it.get("type") == "commandExecution":
                    _it["exitCode"] = _it.get("exitCode") if _it.get("exitCode") is not None else 1
                    if not _it.get("aggregatedOutput"):
                        _it["aggregatedOutput"] = "[no result] 该调用未返回结果（回合被中断或进程退出）"
                else:
                    if not _it.get("output"):
                        _it["output"] = "[no result] 该调用未返回结果（回合被中断或进程退出）"
                    _it["error"] = _it.get("error") or "aborted"
    mt = int(meta.get("mtime") or 0)
    _cm, _ce = wb_thread_model(tid)
    pending_error = (WB.pending.get(tid) or {}) if WB is not None else {}
    if pending_error.get("lastError"):
        error = {"message": pending_error["lastError"]}
        turn_id = str(pending_error.get("turnId") or tid)
        if not turns:
            turns.append({"id": turn_id, "items": []})
        turns[-1].update(status="failed", error=error)
        if not any(item.get("id") == "cerr-" + turn_id for item in turns[-1]["items"]):
            turns[-1]["items"].append({"type": "agentMessage", "id": "cerr-" + turn_id,
                                       "text": "⚠ 请求失败：" + error["message"]})
    _th = {
        "id": tid, "engine": "workbuddy",
        "model": _cm, "reasoningEffort": _ce,
        "cwd": meta.get("cwd") or "",
        "name": meta.get("name") or "",
        "preview": meta.get("preview") or "",
        "turns": turns, "updatedAt": mt, "recencyAt": mt,
        "writable": True, "status": {"type": "idle"}, "source": "codebuddy-code",
    }
    if meta.get("usage"):
        # 历史会话的 token 条(CC 没有落盘 usage, WB 的 JSONL 自带, 这里补齐)
        _th["usage"] = meta["usage"]
    _arts = wb_artifact_summary(tid)
    if _arts:
        # 任务清单/文件改动与正文同源: 正文在时也要给, 否则手机端只剩聊天记录,
        # 看不到桌面上那次跑出来的任务进度与 diff(只有 DB-only 会话才显示是不对的)。
        _th["artifacts"] = _arts
    return trim_thread(_th, light=light)


WB_BUILTIN_MODELS = [
    ("auto", "Auto（自动路由）"),
    ("glm-5.3", "GLM-5.3"),
    ("glm-5.3-flash", "GLM-5.3 Flash"),
    ("glm-5.2", "GLM-5.2"),
    ("glm-5.1", "GLM-5.1"),
    ("glm-5v-turbo", "GLM-5V Turbo"),
    ("kimi-k3-1", "Kimi K3.1"),
    ("kimi-k2.8-preview", "Kimi K2.8 Preview"),
    ("kimi-k2.7", "Kimi K2.7"),
    ("kimi-k2.6", "Kimi K2.6"),
    ("minimax-m3", "MiniMax M3"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
    ("hy3", "HY3"),
    ("hy3-x", "HY3-X"),
    ("hy4-preview", "HY4 Preview"),
    ("step-5-preview", "Step-5 Preview"),
    ("space-bunny", "Space Bunny"),
]


def wb_local_models():
    """CLI(~/.codebuddy) 与桌面端(~/.workbuddy) 的自定义模型都要列出来。
    两边字段名不同: CLI 用 model, 桌面端用 id。"""
    out = []
    for p in (WB_HOME / "models.json", WB_DESKTOP_HOME / "models.json"):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for m in (obj.get("models") or []):
            if not isinstance(m, dict):
                continue
            mid = str(m.get("model") or m.get("id") or "").strip()
            if not mid:
                continue
            mid = "custom-local:" + mid
            if mid not in out:
                out.append(mid)
    return out


def wb_thread_model(tid: str):
    """网关显式选择 > 会话 JSONL 实际模型 > 本机默认。历史会话不该一律显示 absen-auto。"""
    model = effort = ""
    try:
        st = (WB.settings.get(tid) or {}) if WB is not None else {}
        model = str(st.get("model") or "")
        effort = str(st.get("effort") or "")
    except Exception:
        pass
    if not model:
        p = find_wb_session(tid)
        if p is not None:
            m = claude_session_meta(p)
            model = str(m.get("model") or "")
            if not effort:
                effort = str(m.get("effort") or "")
    if not model:
        # 只剩 workbuddy.db 索引的旧会话: 模型名只在这里有
        _dbm = wb_desktop_db_meta().get(tid) or {}
        model = str(_dbm.get("model") or "")
    local = workbuddy_config(USER_HOME, workbuddy_cli_dir())
    return (model or local["defaultModel"], effort or local["defaultEffort"])


def wb_models_payload(tid="") -> dict:
    cli_dir = workbuddy_cli_dir()
    effs = ["minimal", "low", "medium", "high", "xhigh", "max"]
    path = find_wb_session(tid) if tid else None
    root = wb_config_root(path) if path else None
    config = workbuddy_config(USER_HOME, cli_dir, root=root)
    models = config["models"]
    for model in models:
        if not model["efforts"]:
            model["efforts"] = list(effs)
    return {"models": models, "efforts": effs,
            "defaultModel": config["defaultModel"], "defaultEffort": config["defaultEffort"],
            "source": "local_workbuddy", "readAt": config["readAt"],
            "configVersion": config["configVersion"],
            "provider": "workbuddy → codebuddy cli",
            "cfg": {"engine": "workbuddy",
                    "path": str(cli_dir or ""),
                    "exists": bool(cli_dir is not None and cli_dir.is_dir()),
                    "cliFound": bool(cli_dir is not None),
                    "hint": ("" if cli_dir is not None else WB_CLI_LAST_ERROR),
                    "searched": list(WB_CLI_SEARCHED[-8:]),
                    "model": config["defaultModel"], "effort": config["defaultEffort"],
                    "configRoot": config["configRoot"], "defaultSource": config["defaultSource"],
                    "defaultHint": ("跟随 WorkBuddy 客户端默认模型" if not config["defaultModel"] else ""),
                    "modelsFrom": config["sources"]}}


def wb_runtime_payload(tid, running, writable, queue_len=0, missing=False):
    saved = (WB.pending.get(tid) or {}) if WB is not None else {}
    failure = str(saved.get("lastError") or "") if not running else ""
    return runtime_status_payload(tid, running, writable, queue_len, source="workbuddy",
                                  missing=missing, last_event="failed" if failure else None,
                                  turn_id=saved.get("turnId"), error=failure or None)


def wb_model_for_thread_ok(tid, model):
    valid, message = engine_model_ok("workbuddy", model)
    if not valid or not model or not str(model).startswith("custom-local:"):
        return valid, message
    path = find_wb_session(tid) if tid else None
    if path is None:
        return True, ""
    config = workbuddy_config(USER_HOME, workbuddy_cli_dir(), root=wb_config_root(path))
    if model in {row["id"] for row in config["models"]}:
        return True, ""
    return False, "此 WorkBuddy 会话的配置目录没有模型 %s，请在 %s 配置后重试" % (model, wb_config_root(path))


def wb_ensure_cli(force=False):
    """每次入口都轻量自检: 网关启动后安装/挪位置/首次扫描失败都能自愈。"""
    if WB is None:
        return False
    try:
        return bool(WB.refresh_cli(force=force))
    except Exception as exc:
        log("workbuddy refresh_cli: %r" % (exc,))
        return False


def wb_cli_error_message():
    msg = "本机未找到 WorkBuddy CLI"
    detail = str(WB_CLI_LAST_ERROR or "").strip()
    if detail:
        msg += "：" + detail
    searched = [str(x) for x in WB_CLI_SEARCHED[-6:] if str(x).strip()]
    if searched:
        msg += "；已搜索：" + "、".join(searched)
    return msg


# ---------- 引擎模型隔离: 每个 Agent 只认自己的模型表 ----------
_ENGINE_MODEL_CACHE = {"exp": 0.0, "ids": {}, "efforts": {}, "default": {}}


def engine_models_info(engine: str):
    """返回 (ids, efforts, defaultModel)。每个引擎各读各的模型表, 60s 缓存。"""
    eng = engine if engine in ("codex", "claude", "workbuddy") else "codex"
    now = time.time()
    try:
        if eng == "workbuddy":
            pay = wb_models_payload()
        elif eng == "claude":
            pay = claude_models_payload()
        else:
            pay = _models_payload()
    except Exception as exc:
        log("engine_models_info %s: %r" % (eng, exc))
        return ([], [], "")
    ids = [str(m.get("id")) for m in (pay.get("models") or []) if m.get("id")]
    efforts = [str(x) for x in (pay.get("efforts") or [])]
    default = str(pay.get("defaultModel") or (ids[0] if ids else ""))
    _ENGINE_MODEL_CACHE["ids"][eng] = ids
    _ENGINE_MODEL_CACHE["efforts"][eng] = efforts
    _ENGINE_MODEL_CACHE["default"][eng] = default
    _ENGINE_MODEL_CACHE["exp"] = now + 60.0
    _ENGINE_MODEL_CACHE["configVersion"] = version_for([LOCAL_CONFIG])
    return (ids, efforts, default)


def engine_model_ok(engine: str, model: str):
    """跨引擎的模型名挡在网关层, 返回 (ok, message)。空 model 一律放行(用本机默认)。"""
    if not model:
        return (True, "")
    eng = engine if engine in ("codex", "claude", "workbuddy") else "codex"
    ids, _effs, _def = engine_models_info(eng)
    if model in ids:
        return (True, "")
    if eng == "codex":
        # Codex 的模型表来自 provider 探测, 探测失败会退化成静态名单;
        # 所以这里只拦"确实属于别的 Agent"的模型, 未知型号交给 CLI 自己判断。
        for other in ("claude", "workbuddy"):
            o_ids, _oe, _od = engine_models_info(other)
            if model in o_ids:
                return (False, "模型 %s 属于 %s，不能在 Codex 会话里用；已拦下，避免会话启动后才报错"
                               % (model, {"claude": "Claude", "workbuddy": "WorkBuddy"}[other]))
        return (True, "")
    if not ids:
        return (True, "")
    label = {"claude": "Claude", "workbuddy": "WorkBuddy"}.get(eng, "Codex")
    tail = "、".join(ids[:10]) + ("…" if len(ids) > 10 else "")
    return (False, "模型 %s 不属于 %s 引擎，已拦下。%s 可用：%s"
                   % (model, label, label, tail))


def engine_effort_ok(engine: str, effort: str):
    if not effort:
        return (True, "")
    _ids, effs, _def = engine_models_info(engine)
    if not effs or effort in effs:
        return (True, "")
    label = {"claude": "Claude", "workbuddy": "WorkBuddy"}.get(engine, "Codex")
    return (False, "思考程度 %s 不适用于 %s 引擎" % (effort, label))


class WorkBuddyAS(ClaudeAS):
    """WorkBuddy (腾讯 CodeBuddy Code) 引擎: CLI 契约与 Claude Code 同构。"""

    ENGINE = "workbuddy"
    LABEL = "WorkBuddy"
    SOURCE = "codebuddy-code"
    TURN_PREFIX = "wb-"
    SHORT = "wb"
    PROJECTS = WB_PROJECTS
    ARCHIVE_FILE = WB_ARCHIVE_FILE
    SETTINGS_FILE = WB_SETTINGS_FILE
    USAGE_FILE = WB_USAGE_FILE
    TRASH_DIR = WB_TRASH_DIR
    PENDING_FILE = GATEWAY_DIR / "workbuddy_pending.json"

    def __init__(self, hub):
        self.spawn_env = dict(os.environ)
        argv = find_workbuddy_cli()
        if argv and _workbuddy_exe_cache.get("electron"):
            self.spawn_env["ELECTRON_RUN_AS_NODE"] = "1"
        self._init_engine(hub, argv)
        self.hidden = self._load_json(WB_HIDDEN_FILE, [])
        self._cli_retry_at = 0.0

    def refresh_cli(self, force=False):
        """WorkBuddy 可能是网关启动后才安装/挪位置的: 定期重扫, 自愈 no-cli。"""
        if self.status == "ready" and self.argv and all(os.path.exists(p) for p in self.argv):
            return True
        now = time.time()
        if not force and now - self._cli_retry_at < 4.0:
            return self.status == "ready"
        self._cli_retry_at = now
        argv = find_workbuddy_cli()
        if not argv:
            self.status = "no-cli"
            self.exe = ""
            return False
        self.argv = argv
        self.exe = argv[0]
        if _workbuddy_exe_cache.get("electron"):
            self.spawn_env["ELECTRON_RUN_AS_NODE"] = "1"
        else:
            self.spawn_env.pop("ELECTRON_RUN_AS_NODE", None)
        self.status = "ready"
        log("WorkBuddyAS ready (dynamic): %s" % " ".join(self.argv))
        return True

    def sessions_map(self, force=False):
        return wb_sessions_map(force=force)

    def list_threads(self, limit=60, q="", cwd="", archived=False):
        """CC 同款列表 + workbuddy.db 里"只有索引、没有 JSONL"的旧会话。

        桌面端 2026-10 的迁移会清理本地 JSONL, 索引仍留在 workbuddy.db。
        只扫 JSONL 会让历史少一半; 这里补成只读条目, 详情带 missingTranscript。
        """
        res = ClaudeAS.list_threads(self, limit=100000, q=q, cwd=cwd, archived=archived)
        out = res.get("data") or []
        seen = {t.get("id") for t in out}
        ask = (q or "").lower()
        want = (cwd or "").lower()
        now_sec = int(time.time())
        for sid, m in wb_desktop_db_meta().items():
            if not sid or sid in seen or sid in self.hidden:
                continue
            if (sid in self.archived) != bool(archived):
                continue
            mc = str(m.get("cwd") or "")
            if want and mc.lower() != want:
                continue
            name = str(m.get("title") or "")
            if ask and ask not in (name + " " + mc).lower():
                continue
            _cm, _ce = wb_thread_model(sid)
            mt = int(m.get("updatedAt") or 0) or now_sec
            out.append({
                "id": sid, "engine": self.ENGINE,
                "model": _cm, "reasoningEffort": _ce,
                "name": name, "preview": "", "cwd": mc,
                "updatedAt": mt, "recencyAt": mt,
                "writable": False, "missingTranscript": True,
                "loaded": False, "running": False, "queueLen": 0,
                "status": {"type": "idle"}, "source": self.SOURCE,
            })
        with self.lock:
            pending = dict(self.pending)
        for sid, entry in pending.items():
            if sid in seen or (sid in self.archived) != bool(archived):
                continue
            directory = str(entry.get("cwd") or "")
            preview = str(entry.get("lastText") or "")
            if want and directory.lower() != want:
                continue
            if ask and ask not in (preview + " " + directory).lower():
                continue
            model, effort = self.thread_model(sid)
            out.append({"id": sid, "engine": self.ENGINE, "model": model, "reasoningEffort": effort,
                        "cwd": directory, "preview": preview, "name": "", "writable": True,
                        "running": sid in self.turns, "updatedAt": entry.get("updated") or entry.get("created") or now_sec,
                        "recencyAt": entry.get("updated") or entry.get("created") or now_sec,
                        "source": self.SOURCE, "pendingTranscript": True,
                        "status": {"type": "active" if sid in self.turns else "idle"}})
        out.sort(key=lambda x: x.get("recencyAt") or 0, reverse=True)
        return {"data": out[:limit], "engine": self.ENGINE}

    def is_known(self, tid: str) -> bool:
        """DB-only 旧会话也算 WorkBuddy。

        它们没有 JSONL(被桌面端清理), 只有 workbuddy.db 索引。若只按文件认领,
        详情/状态/中断/队列请求都会掉进 Codex 分支, 前端看到 not found —— 这正是
        WB 历史读不出来的第二个根因。
        """
        if tid in self.known or tid in self.pending or tid in self.turns:
            return True
        if tid in self.not_claude:
            return False
        if self.find_session(tid) is None and self.find_session(tid, force=True) is None:
            if not wb_desktop_db_meta().get(tid):
                self.not_claude.add(tid)
                return False
        self.known.add(tid)
        self.not_claude.discard(tid)
        return True

    def rename(self, tid, name):
        if self.find_session(tid) is None and wb_desktop_db_meta().get(tid) and tid not in self.hidden:
            raise ApiError({"code": "transcript_missing",
                            "message": "这条 WorkBuddy 会话只剩索引，不能重命名（避免与桌面端索引冲突）"})
        return ClaudeAS.rename(self, tid, name)

    def delete(self, tid):
        if self.find_session(tid) is None and wb_desktop_db_meta().get(tid):
            with self.lock:
                if tid not in self.hidden:
                    self.hidden.append(tid)
                    self._save_json(WB_HIDDEN_FILE, self.hidden)
            return True
        return ClaudeAS.delete(self, tid)

    def find_session(self, tid, force=False):
        return find_wb_session(tid, force=force)

    def session_registry(self, force=False):
        return wb_session_registry(force=force)

    def is_running(self, tid):
        # 复用父类: forced_stop 20s 窗口 + turns + registry。WB 以前绕过停止窗口,
        # 停止后 running 仍是真值 -> 队列/「发送」按钮判断和 Claude 不一致。
        return ClaudeAS.is_running(self, tid)

    def thread_model(self, tid):
        return wb_thread_model(tid)

    def read_thread(self, tid, light=False):
        return wb_read_thread(tid, light=light)

    def local_cfg(self):
        config = workbuddy_config(USER_HOME, workbuddy_cli_dir())
        return {"model": config["defaultModel"], "effort": config["defaultEffort"]}

    def models_payload(self):
        return wb_models_payload()

    def update_settings(self, tid: str, model=None, effort=None):
        previous = dict(self.settings.get(tid) or {})
        expected = dict(super().update_settings(tid, model, effort))
        stored = read_json(self.SETTINGS_FILE)
        actual = stored.get(tid) if isinstance(stored, dict) else None
        if not isinstance(actual, dict) or any(actual.get(key) != expected.get(key) for key in ("model", "effort")):
            with self.lock:
                self.settings[tid] = dict(actual) if isinstance(actual, dict) else previous
            raise ApiError({"code": "settings_unconfirmed", "message": "WorkBuddy 设置未能持久化回读，未确认切换成功，请重试"})
        return dict(actual)

    def _make_cmd(self, tid, path, model, effort):
        config = workbuddy_config(USER_HOME, workbuddy_cli_dir(), root=wb_config_root(path) if path else None)
        model = model or config["defaultModel"]
        if not effort and model == config["defaultModel"]:
            effort = config["defaultEffort"]
        # dontAsk 会把 Bash 一并拒掉(实测), 远端控制就退化成"只会看"。
        # 默认给足权限(与 Codex 全自动档等价); 要收紧就设 WORKBUDDY_PERMISSION_MODE=acceptEdits/dontAsk。
        perm = (os.environ.get("WORKBUDDY_PERMISSION_MODE") or "bypassPermissions").strip()
        cmd = list(self.argv) + ["-p", "--output-format", "stream-json", "--verbose",
                                 "--include-partial-messages", "--input-format", "stream-json",
                                 "--permission-mode", perm]
        cmd += (["--resume", tid] if path is not None else ["--session-id", tid])
        if model:
            cmd += ["--model", str(model)]
        if effort:
            cmd += ["--effort", str(effort)]
        if think_zh_on():
            _zhf = think_zh_prompt_path()
            txt = ""
            if _zhf:
                try:
                    txt = Path(_zhf).read_text(encoding="utf-8").strip()
                except OSError:
                    txt = ""
            cmd += ["--append-system-prompt", txt or NEXUS_THINK_ZH]
        return cmd

    def spawn_env_for(self, tid, path):
        # 桌面端会话在 ~/.workbuddy/projects, CLI 只认 CODEBUDDY_CONFIG_DIR 指定的根;
        # 不切的话 --resume 会报 "No conversation found"(实测)。
        env = dict(self.spawn_env or {})
        config = workbuddy_config(USER_HOME, workbuddy_cli_dir())
        cfg_root = wb_config_root(path) if path is not None else Path(config["configRoot"])
        model = str((self.settings.get(tid) or {}).get("model") or "")
        if model.startswith("custom-local:"):
            roots = next((row.get("configRoots") or [] for row in config["models"] if row["id"] == model), [])
            if path is None and roots:
                cfg_root = Path(roots[0])
            elif str(cfg_root) not in roots:
                raise ApiError({"code": "model_config_missing", "message": "此 WorkBuddy 会话的配置目录没有模型 %s，请在 %s 配置该模型后重试" % (model, cfg_root)})
        try:
            wb_normalize_models_json(cfg_root)
        except Exception as exc:
            log("workbuddy: models.json normalize failed: %s" % exc)
        env["CODEBUDDY_CONFIG_DIR"] = str(cfg_root)
        return env

    def _after_kill_before_next(self, tid, proc):
        # 等旧 CLI 真正退出再追加: 否则它的收尾写盘会和我们的追加交错成半行。
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        self.close_interrupted_turn(tid)

    def close_interrupted_turn(self, tid):
        """把最后一条「没有助手回复」的 user 消息用官方中断记录闭合。

        CodeBuddy --resume 会把末尾未闭合的 user 消息当成待执行请求重跑 —— 实测:
        停止后再发新消息, 新回合会先把旧的 Start-Sleep 跑完。官方 CLI 的中断写法是
        追加一条 content="Interrupted by user" / status=incomplete / providerData.skipRun
        的 assistant 记录(常量 INTERRUPTED_BY_USER_MESSAGE, 由 isUserInterrupted 识别)。
        实测补上这条后, 旧任务不再重放(43.9s -> 9.4s, 无工具调用)。
        """
        path = self.find_session(tid)
        if path is None:
            path = self.find_session(tid, force=True)   # 刚落盘的会话还没进 15s 扫描缓存
        if path is None:
            return False
        try:
            last_row = None
            after = []
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(j, dict):
                        continue
                    if j.get("type") in ("file-history-snapshot", "system", "ai-title",
                                         "custom-title", "last-prompt", "resend-fork-notice",
                                         "resume-fork-notice"):
                        continue
                    # 只留「最后一条 user 之后」的窗口: 遇到新 user 就重置。
                    # 旧实现 deque(maxlen=400) 在长回合里会读不到那条 user -> 静默不闭合。
                    if j.get("type") == "message" and j.get("role") == "user":
                        last_row = j
                        after = []
                    else:
                        after.append(j)
            if last_row is None:
                return False
            if any(_wb_is_interrupt_row(_j) for _j in after):
                return False                       # 已经闭合过, 不重复写
            if any(_j.get("type") == "message" and _j.get("role") == "assistant" for _j in after):
                return False                       # 真出过回复, 不是中断现场
            cwd = str(last_row.get("cwd") or "")
            if not cwd:
                cwd = str(claude_session_meta(path).get("cwd") or "")
            now = int(time.time() * 1000)
            extra = []
            calls, results = {}, set()
            for _j in after:
                if _j.get("type") == "function_call":
                    calls[str(_j.get("callId") or _j.get("id") or "")] = str(_j.get("name") or "tool")
                elif _j.get("type") == "function_call_result":
                    results.add(str(_j.get("callId") or ""))
            for _cid, _name in calls.items():
                if not _cid or _cid in results:
                    continue
                # 悬着的工具调用补一条中断结果, 保持 history 合法(否则下次请求可能因
                # tool_calls 没有对应 result 被上游拒)。
                extra.append({"id": str(uuid.uuid4()), "type": "function_call_result",
                              "name": _name, "callId": _cid, "status": "error",
                              "output": {"type": "text", "text": "Request was aborted."},
                              "providerData": {"toolResult": {"title": "aborted",
                                                              "content": "Request was aborted."}},
                              "timestamp": now, "sessionId": tid, "cwd": cwd})
            marker = {"id": str(uuid.uuid4()), "timestamp": now + 1, "type": "message",
                      "role": "assistant",
                      "content": [{"type": "output_text", "text": WB_INTERRUPT_TEXT}],
                      "status": "incomplete",
                      "providerData": {"skipRun": True,
                                       "error": {"message": "Request was aborted."}},
                      "sessionId": tid, "cwd": cwd}
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                for _r in extra + [marker]:
                    fh.write(json.dumps(_r, ensure_ascii=False) + "\n")
            _claude_meta_cache.pop(str(path), None)
            log("workbuddy: closed interrupted turn %s (+%d dangling tool results)"
                % (tid[:8], len(extra)))
            return True
        except OSError as exc:
            log("workbuddy interrupt marker %s: %s" % (tid[:8], exc))
            return False


# ---------- 文件浏览(限工作区) ----------

FS_EXTRA = GATEWAY_DIR / "fs_roots.json"
UPLOAD_DIR = GATEWAY_DIR / "uploads"
_fsroots_cache = {"ts": 0, "data": None}
CODEX_STATE_FILE = USER_HOME / ".codex" / ".codex-global-state.json"
_projects_cache = {"ts": 0, "data": None}
_desktop_projects = None


def desktop_project_catalog():
    global _desktop_projects
    if _desktop_projects is None:
        from desktop_projects import DesktopProjects
        _desktop_projects = DesktopProjects(CODEX_HOME)
    return _desktop_projects


def read_projects():
    return desktop_project_catalog().projects()




def fs_roots():
    now = time.time()
    if _fsroots_cache["data"] is not None and now - _fsroots_cache["ts"] < 30:
        return _fsroots_cache["data"]
    roots = []
    if FS_EXTRA.exists():
        try:
            for x in json.loads(FS_EXTRA.read_text(encoding="utf-8")):
                if isinstance(x, str) and x.strip():
                    roots.append(x.strip())
        except Exception:
            pass
    try:
        for project in read_projects():
            roots.extend(project.get('rootPaths') or [])
        for t in (_index_snapshot() or {}).get("threads") or []:
            cwd = t.get("cwd")
            if cwd and cwd not in roots:
                roots.append(cwd)
    except Exception:
        pass
    try:
        for _tid, _p in wb_sessions_map().items():
            _meta = claude_session_meta(_p)
            _cwd = (_meta or {}).get("cwd")
            if _cwd and _cwd not in roots:
                roots.append(_cwd)
    except Exception:
        pass
    try:
        for _tid, _p in claude_sessions_map().items():
            _meta = claude_session_meta(_p)
            _cwd = (_meta or {}).get("cwd")
            if _cwd and _cwd not in roots:
                roots.append(_cwd)
    except Exception:
        pass
    out = []
    seen = set()
    for r in roots:
        try:
            real = os.path.realpath(r)
        except OSError:
            continue
        key = real.lower()
        if key in seen or not os.path.isdir(real):
            continue
        seen.add(key)
        name = os.path.basename(real.rstrip("\\/")) or real
        out.append({"path": real, "name": name})
    out.sort(key=lambda x: x["path"].lower())
    _fsroots_cache.update({"ts": now, "data": out})
    return out


def fs_allowed(p: str):
    """返回 realpath 或 None(不在允许根内)。"""
    if not p:
        return None
    try:
        real = os.path.realpath(p)
    except OSError:
        return None
    if not os.path.exists(real):
        return None
    rl = real.lower()
    for r in fs_roots():
        rr = os.path.realpath(r["path"]).lower()
        if rl == rr or rl.startswith(rr + os.sep):
            return real
    return None


def fs_list(p: str):
    real = fs_allowed(p)
    if real is None:
        raise ApiError({"code": "fs_denied", "message": "路径不在允许的工作区范围内"})
    if not os.path.isdir(real):
        raise ApiError({"code": "not_dir", "message": "不是文件夹"})
    try:
        names = os.listdir(real)
    except OSError as exc:
        raise ApiError({"code": "fs_error", "message": str(exc)})
    items = []
    for n in names:
        fp = os.path.join(real, n)
        try:
            st = os.stat(fp)
            isdir = os.path.isdir(fp)
            items.append({
                "name": n, "dir": isdir, "size": 0 if isdir else st.st_size,
                "mtime": int(st.st_mtime), "hidden": n.startswith("."),
            })
        except OSError:
            continue
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parent = os.path.dirname(real.rstrip("\\/"))
    try:
        dino = str(os.stat(real).st_ino or 0)
    except OSError:
        dino = "0"
    return {"path": real, "items": items, "ino": dino,
            "parent": parent if parent and parent != real else None,
            "isRoot": any(os.path.realpath(r["path"]).lower() == real.lower() for r in fs_roots())}


def fs_stat(p: str):
    """单个路径 stat (轻量, 不读内容, 供前端变更轮询)。"""
    real = fs_allowed(p)
    if real is None:
        raise ApiError({"code": "fs_denied", "message": "路径不在允许的工作区范围内"})
    try:
        st = os.stat(real)
    except OSError as exc:
        raise ApiError({"code": "fs_error", "message": str(exc)})
    return {"path": real, "mtime": int(st.st_mtime), "size": st.st_size,
            "dir": os.path.isdir(real), "ino": str(getattr(st, "st_ino", 0) or 0)}


_BAD_NAME_CHARS = '<>:"/\\|?*'


def fs_name_ok(name: str) -> str:
    n = (name or "").strip()
    if not n or n in (".", ".."):
        raise ApiError({"code": "bad_name", "message": "名称不能为空或无效"})
    if any((c in _BAD_NAME_CHARS) or ord(c) < 32 for c in n):
        raise ApiError({"code": "bad_name", "message": "名称不能包含 < > : \" / \\ | ? * 等特殊字符"})
    if n != n.rstrip(" ."):
        raise ApiError({"code": "bad_name", "message": "名称不能以空格或点结尾"})
    if n[0] in "~$":
        raise ApiError({"code": "bad_name", "message": "名称不能以 ~ 或 $ 开头"})
    if len(n) > 120:
        raise ApiError({"code": "bad_name", "message": "名称过长"})
    return n


def fs_mkdir(p: str, name: str):
    real = fs_allowed(p)
    if real is None:
        raise ApiError({"code": "fs_denied", "message": "路径不在允许的工作区范围内"})
    if not os.path.isdir(real):
        raise ApiError({"code": "not_dir", "message": "父路径不是文件夹"})
    n = fs_name_ok(name)
    fp = os.path.join(real, n)
    if os.path.exists(fp):
        raise ApiError({"code": "exists", "message": "已存在同名文件或文件夹"})
    try:
        os.mkdir(fp)
    except OSError as exc:
        raise ApiError({"code": "fs_error", "message": str(exc)})
    log("fs mkdir: %s" % fp)
    return {"ok": True, "path": fp, "name": n}


def fs_newfile(p: str, name: str):
    real = fs_allowed(p)
    if real is None:
        raise ApiError({"code": "fs_denied", "message": "路径不在允许的工作区范围内"})
    if not os.path.isdir(real):
        raise ApiError({"code": "not_dir", "message": "父路径不是文件夹"})
    n = fs_name_ok(name)
    fp = os.path.join(real, n)
    if os.path.exists(fp):
        raise ApiError({"code": "exists", "message": "已存在同名文件或文件夹"})
    try:
        with open(fp, "x", encoding="utf-8"):
            pass
    except OSError as exc:
        raise ApiError({"code": "fs_error", "message": str(exc)})
    log("fs touch: %s" % fp)
    return {"ok": True, "path": fp, "name": n}


IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def fs_read(p: str, max_bytes: int = 200000):
    real = fs_allowed(p)
    if real is None:
        raise ApiError({"code": "fs_denied", "message": "路径不在允许的工作区范围内"})
    if os.path.isdir(real):
        raise ApiError({"code": "is_dir", "message": "这是文件夹, 用 /api/fs/list"})
    try:
        st0 = os.stat(real)
    except OSError as exc:
        raise ApiError({"code": "fs_error", "message": str(exc)})
    size = st0.st_size
    mtime = int(st0.st_mtime)
    ino = str(getattr(st0, "st_ino", 0) or 0)
    ext = os.path.splitext(real)[1].lower()
    try:
        with open(real, "rb") as fh:
            head = fh.read(8192)
    except OSError as exc:
        raise ApiError({"code": "fs_error", "message": str(exc)})
    if ext in IMG_EXT:
        return {"path": real, "image": True, "size": size, "mtime": mtime,
                "name": os.path.basename(real), "ino": ino}
    if b"\x00" in head:
        return {"path": real, "binary": True, "size": size, "mtime": mtime,
                "name": os.path.basename(real), "ino": ino}
    m = max(1000, min(int(max_bytes), 800000))
    try:
        with open(real, "rb") as fh:
            data = fh.read(m)
    except OSError as exc:
        raise ApiError({"code": "fs_error", "message": str(exc)})
    text = data.decode("utf-8", "replace")
    return {"path": real, "text": text, "size": size, "mtime": mtime,
            "truncated": size > m, "name": os.path.basename(real), "ino": ino}


_SKIP_SCAN_DIRS = {"$recycle.bin", "system volume information", ".git", ".svn", ".hg",
                   "node_modules", "__pycache__", ".venv", "venv", "windows"}


def _scan_find_ino(anchor: str, target: int, max_depth: int, budget: int):
    """BFS 扫描 anchor 子树, 找 st_ino == target 的条目, 返回路径或 None。"""
    queue = [(anchor, 0)]
    seen = 0
    while queue:
        d, depth = queue.pop(0)
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > budget:
                        return None
                    try:
                        st = os.stat(e.path)   # DirEntry.stat() 的 st_ino 在 Windows 上恒为 0
                    except OSError:
                        continue
                    if st.st_ino == target:
                        return e.path
                    if depth < max_depth and e.is_dir():
                        if e.name.lower() in _SKIP_SCAN_DIRS:
                            continue
                        queue.append((e.path, depth + 1))
        except OSError:
            continue
    return None


def fs_follow(p: str, ino: str):
    """旧路径失效后按文件系统 inode 跟随重命名/移动; 返回新路径 / same / lost。"""
    try:
        target = int(str(ino).strip())
    except (TypeError, ValueError):
        raise ApiError({"code": "bad_ino", "message": "ino 参数无效"})
    if target <= 0:
        raise ApiError({"code": "bad_ino", "message": "ino 参数无效"})
    try:
        real = os.path.realpath(p)
        st = os.stat(real)
        if st.st_ino == target:
            return {"same": True, "path": real}
    except (OSError, TypeError):
        pass
    roots = [os.path.realpath(r["path"]) for r in fs_roots()]

    def in_roots(x: str) -> bool:
        xl = x.lower()
        return any(xl == rr.lower() or xl.startswith(rr.lower() + os.sep) for rr in roots)

    anc = None
    try:
        cur = os.path.dirname(os.path.realpath(p))
    except OSError:
        cur = os.path.dirname(p)
    while cur:
        if os.path.isdir(cur) and in_roots(cur):
            anc = cur
            break
        nxt = os.path.dirname(cur)
        if nxt == cur:
            break
        cur = nxt
    if anc is None:
        return {"lost": True}
    found = _scan_find_ino(anc, target, 6, 12000)
    if not found:
        al = anc.lower()
        cands = [rr for rr in roots if al == rr.lower() or al.startswith(rr.lower() + os.sep)]
        root = max(cands, key=len) if cands else None
        if root and root.lower() != al:
            m = 8 if root != os.path.splitdrive(root)[0] + os.sep else 3
            found = _scan_find_ino(root, target, m, 30000)
    if found and in_roots(found):
        return {"path": os.path.realpath(found)}
    return {"lost": True}


AS: CodexAS = None
HUB: Hub = None
TAILER: "Tailer" = None
PUMP: "QueuePump" = None

QUEUE_DB = USER_HOME / ".codex" / "queue_1.sqlite"


def _err_text(exc) -> str:
    """ApiError / 任意异常 -> 人能看的一行。"""
    err = getattr(exc, "err", None)
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)
    if err is not None:
        return str(err)
    return str(exc)


def _is_timeout(exc) -> bool:
    err = getattr(exc, "err", None)
    return isinstance(err, dict) and err.get("code") == "timeout"


_QUEUE_COUNTS = {"ts": 0.0, "map": {}}
_QUEUE_COUNTS_LOCK = threading.Lock()
_QUEUE_COUNTS_TTL = 1.5


def queue_counts() -> dict:
    """共享队列 queue_1.sqlite 里每个 threadId 还有几条待执行(只读打开; 失败给空)。

    1.5 秒缓存：/api/threads 和手机端每秒级 /state 轮询都走这里，
    绝不走 app-server RPC（大 thread/read 会把 RPC 堵几十秒）。
    """
    _now = time.time()
    with _QUEUE_COUNTS_LOCK:
        if _QUEUE_COUNTS["ts"] and _now - _QUEUE_COUNTS["ts"] < _QUEUE_COUNTS_TTL:
            return dict(_QUEUE_COUNTS["map"])
    if not QUEUE_DB.exists():
        return {}
    con = None
    try:
        con = sqlite3.connect("file:" + QUEUE_DB.as_posix() + "?mode=ro", uri=True, timeout=1.0)
        out = {str(r[0]): int(r[1])
               for r in con.execute("select thread_id, count(*) from queued_items group by thread_id")}
    except Exception:
        return {}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    with _QUEUE_COUNTS_LOCK:
        _QUEUE_COUNTS["ts"] = time.time()
        _QUEUE_COUNTS["map"] = dict(out)
    return out


def queue_thread_ids() -> set:
    return set(queue_counts())


# /api/threads 的"最后一版好列表"缓存。
# app-server 是单条 JSON-RPC 管道：一个大 thread/read（includeTurns）会把后面的
# thread/list 一起堵住，手机端就会看到"网关响应超时（20 秒）"。
# 这里给 thread/list 设 3 秒上限，超时就回最后一版列表 + stale=true，
# 让 UI 永远能在几秒内拿到东西，而不是白屏。
THREAD_LIST_TIMEOUT = 3.0
_THREAD_LIST_CACHE = {}
_THREAD_LIST_CACHE_LOCK = threading.Lock()
_THREAD_LIST_CACHE_MAX = 16


def _thread_list_cache_key(limit, archived, params):
    return "|".join(str(x) for x in (
        limit, int(bool(archived)), params.get("searchTerm") or "",
        params.get("cwd") or "", params.get("sortKey") or ""))


def _thread_list_cache_put(key, res):
    try:
        blob = json.dumps(res, ensure_ascii=False)
    except Exception:
        return
    with _THREAD_LIST_CACHE_LOCK:
        if (key not in _THREAD_LIST_CACHE
                and len(_THREAD_LIST_CACHE) >= _THREAD_LIST_CACHE_MAX):
            oldest = min(_THREAD_LIST_CACHE.items(),
                         key=lambda kv: kv[1][0])[0]
            _THREAD_LIST_CACHE.pop(oldest, None)
        _THREAD_LIST_CACHE[key] = (time.time(), blob)


def _thread_list_cache_get(key):
    with _THREAD_LIST_CACHE_LOCK:
        hit = _THREAD_LIST_CACHE.get(key)
    if not hit:
        return None
    try:
        return json.loads(hit[1])
    except Exception:
        return None


def thread_list_cached(key, params):
    """thread/list + 3 秒上限；app-server 被 thread/read 堵住时回最后一版好列表。"""
    try:
        res = AS.request("thread/list", params, timeout=THREAD_LIST_TIMEOUT)
    except ApiError as exc:
        if not _is_timeout(exc):
            raise
        cached = _thread_list_cache_get(key)
        if cached is None:
            raise
        cached["stale"] = True
        log("threads stale fallback: app-server busy > %.1fs, "
            "serving last list (n=%d)"
            % (THREAD_LIST_TIMEOUT, len(cached.get("data") or [])))
        return cached
    _thread_list_cache_put(key, res)
    return res


def _index_snapshot():
    """索引器快照；不可用时返回 None，调用方降级，绝不在请求里读盘。"""
    if thread_index is None:
        return None
    try:
        return thread_index.read_snapshot()
    except Exception:
        return None


def _wait_codex_settings(tid, model=None, effort=None, timeout=2, allow_missing=False):
    from contextlib import closing
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            uri = Path(thread_index.state_db()).as_uri()+'?mode=ro'
            with closing(sqlite3.connect(uri,uri=True,timeout=.15)) as db:
                row = db.execute('SELECT model,reasoning_effort FROM threads WHERE id=?',(tid,)).fetchone()
            if row is None and allow_missing:
                return {}
            if row is not None and (not model or row[0] == model) and (not effort or row[1] == effort):
                return {'model':row[0],'effort':row[1]}
        except (OSError,sqlite3.Error,TypeError):
            pass
        time.sleep(.025)
    raise ApiError({'code':'settings_unconfirmed','message':'设置已提交，持久化结果尚未确认，请稍后刷新'})


def codex_thread_settings(tid):
    try:
        from contextlib import closing
        dbpath = thread_index.state_db()
        with closing(sqlite3.connect(Path(dbpath).as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("SELECT model, reasoning_effort FROM threads WHERE id=?", (tid,)).fetchone()
        if row:
            result = {k: v for k, v in zip(("model", "effort"), row) if v}
            if row[1]:
                result["reasoningEffort"] = row[1]
            return result
    except Exception as exc:
        log("thread settings read %s: %s" % (tid[:8], type(exc).__name__))
    return {}


def _index_thread(tid):
    snap = _index_snapshot()
    if not snap:
        return None
    for item in snap.get("threads") or []:
        if item.get("id") == tid:
            return item
    return None


def _index_tails():
    """最近 20 条消息（索引器单独落盘）；不可用时静默降级为空。"""
    if thread_index is None:
        return {}
    try:
        return thread_index.read_tails() or {}
    except Exception:
        return {}

HEAVY_SESSION_BYTES = 50 * 1024 * 1024

# Codex 会清理/迁移 rollout jsonl（会话源文件），但 ~/.codex/thread_history_1.sqlite
# 里仍保留完整的 thread_items 投影。rollout 缺失时从这里回读正文：
# 只读展示（不能续发，app-server 的 resume 依赖 rollout），但内容、工具调用、
# 图片引用、压缩标记都在，足够把"正文缺失"变成"历史回读"。
HISTORY_DB = CODEX_HOME / "thread_history_1.sqlite"
HISTORY_NOTE = "电脑端的会话源文件已清理，正文已从本地对话历史库回读（只读）"
HISTORY_PAGE_ITEMS = 120
HISTORY_PAGE_BYTES = 3 * 1024 * 1024
_HISTORY_HAS_CACHE = {}
_HISTORY_HAS_TTL_OK = 300.0
_HISTORY_HAS_TTL_NO = 30.0


def _history_conn():
    return sqlite3.connect("file:" + HISTORY_DB.as_posix() + "?mode=ro",
                           uri=True, timeout=2.0)


def history_has_items(tid):
    """历史库里有没有这条会话的正文（带缓存；缺失只缓存 30 秒，便于自愈）。"""
    tid = str(tid or "")
    if not tid:
        return False
    try:
        if not HISTORY_DB.exists():
            return False
    except OSError:
        return False
    now = time.time()
    hit = _HISTORY_HAS_CACHE.get(tid)
    if hit and now - hit[0] < hit[1]:
        return hit[2]
    ok = False
    con = None
    try:
        con = _history_conn()
        ok = con.execute(
            "select 1 from thread_items where thread_id=? limit 1", (tid,)).fetchone() is not None
    except Exception:
        ok = False
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    _HISTORY_HAS_CACHE[tid] = (now, _HISTORY_HAS_TTL_OK if ok else _HISTORY_HAS_TTL_NO, ok)
    return ok


def history_projected_bytes(tid):
    """projection_state 里已消费的 rollout 字节数 = 原会话文件的近似大小。"""
    con = None
    try:
        con = _history_conn()
        row = con.execute(
            "select next_rollout_byte_offset from thread_history_projection_state "
            "where thread_id=?", (str(tid),)).fetchone()
        return int(row[0]) if row and row[0] else 0
    except Exception:
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def history_page(tid, before=None, limit=HISTORY_PAGE_ITEMS, max_bytes=HISTORY_PAGE_BYTES):
    """从 thread_history_1.sqlite 读一页正文（旧 -> 新）。

    返回 (rows, next_before, has_more)；rows 每项是
    (rollout_ordinal, turn_id, item_id, item_json)。
    next_before 用负的 ordinal 编码（0 = 没有更早的了）——前端把 nextEnd 当数字用，
    负号让 /tail 一眼分得清"历史库分页"还是"rollout 文件偏移"。
    """
    if not HISTORY_DB.exists():
        return [], 0, False
    con = None
    try:
        con = _history_conn()
        if before:
            rows = con.execute(
                "select rollout_ordinal, turn_id, item_id, item_json from thread_items "
                "where thread_id=? and rollout_ordinal<? "
                "order by rollout_ordinal desc limit ?",
                (str(tid), int(before), int(limit))).fetchall()
        else:
            rows = con.execute(
                "select rollout_ordinal, turn_id, item_id, item_json from thread_items "
                "where thread_id=? order by rollout_ordinal desc limit ?",
                (str(tid), int(limit))).fetchall()
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    if not rows:
        return [], 0, False
    kept = []
    used = 0
    for r in rows:
        n = len(r[3] or "")
        if len(kept) >= 8 and used + n > max_bytes:
            break
        kept.append(r)
        used += n
    oldest = int(kept[-1][0])
    has_more = False
    con = None
    try:
        con = _history_conn()
        has_more = con.execute(
            "select 1 from thread_items where thread_id=? and rollout_ordinal<? limit 1",
            (str(tid), oldest)).fetchone() is not None
    except Exception:
        has_more = len(rows) >= int(limit)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    kept.reverse()
    return kept, (-oldest if has_more else 0), has_more


def history_turn_statuses(tid, turn_ids):
    ids = [t for t in (turn_ids or []) if t]
    if not ids:
        return {}
    con = None
    try:
        con = _history_conn()
        q = ("select turn_id, status from thread_turns where thread_id=? and turn_id in (%s)"
             % ",".join("?" * len(ids)))
        return {str(r[0]): str(r[1]) for r in con.execute(q, [str(tid)] + ids)}
    except Exception:
        return {}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _history_thread_payload(tid, item=None, before=None, limit=HISTORY_PAGE_ITEMS):
    """rollout 丢失时的只读详情：结构对齐 app-server thread/read 的 thread。"""
    item = item or _index_thread(tid) or {}
    try:
        rows, next_before, has_more = history_page(tid, before=before, limit=limit)
    except Exception as exc:
        log("history read failed %s: %s" % (str(tid)[:8], exc))
        return None
    if not rows:
        return None
    turns = []
    by_turn = {}
    for ordinal, turn_id, item_id, raw in rows:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if not obj.get("id"):
            obj["id"] = item_id
        key = str(turn_id or "")
        turn = by_turn.get(key)
        if turn is None:
            turn = {"id": key or ("history-turn-%s" % item_id),
                    "status": "completed", "items": []}
            by_turn[key] = turn
            turns.append(turn)
        turn["items"].append(obj)
    if not turns:
        return None
    try:
        statuses = history_turn_statuses(tid, list(by_turn.keys()))
        for turn in turns:
            st = statuses.get(turn["id"])
            if st:
                turn["status"] = st
    except Exception:
        pass
    size = history_projected_bytes(tid)
    return {
        "id": tid,
        "name": item.get("name") or "",
        "title": item.get("title") or "",
        "cwd": item.get("cwd") or "",
        "model": item.get("model") or "",
        "reasoningEffort": item.get("reasoningEffort") or "",
        "createdAt": item.get("createdAt") or 0,
        "updatedAt": item.get("updatedAt") or 0,
        "turns": turns,
        "partial": True,
        "partialKind": "history_db",
        "partialNote": HISTORY_NOTE,
        "loadedMessages": sum(len(t["items"]) for t in turns),
        "nextEnd": next_before,
        "hasMore": bool(has_more),
        "heavy": size >= HEAVY_SESSION_BYTES,
        "sizeBytes": size,
        "turnsTrimmed": False,
        "historyFallback": True,
    }



def _thread_size_bytes(item=None, tid=None):
    """索引快照里的 sizeBytes 可能缺失或过期；rollout 文件 stat 兜底。"""
    item = item or {}
    try:
        size = int(item.get("sizeBytes") or 0)
    except (TypeError, ValueError):
        size = 0
    if size > 0:
        return size
    path = str(item.get("rolloutPath") or "")
    if not path and tid:
        try:
            p = find_rollout(tid)
            path = str(p) if p else ""
        except Exception:
            path = ""
    if path:
        try:
            return int(os.path.getsize(path))
        except OSError:
            return 0
    return 0


def _tail_thread_payload(tid, item=None, kind=None, note=None, limit=40):
    """只返回 rollout 尾部若干条消息的“可看”会话，绝不整文件解析。

    nextEnd 是更早一页的绝对文件偏移；前端带着它继续 /tail 分页。
    """
    item = item or _index_thread(tid) or {}
    path = str(item.get("rolloutPath") or "")
    if not path and tid:
        try:
            _p = find_rollout(tid)
            path = str(_p) if _p else ""
        except Exception:
            path = ""
    rows = []
    next_end = 0
    has_more = False
    size = _thread_size_bytes(item, tid)
    win = {}
    if path and thread_index is not None:
        try:
            from history_timeline import read_timeline_window
            win = read_timeline_window(path, limit=limit)
            if win is None:
                win = thread_index.read_messages_window(
                    path, limit=limit,
                    max_chars=getattr(thread_index, "MSG_TEXT_CHARS_TAIL", 20000))
            rows = win.get("msgs") or []
            next_end = int(win.get("nextEnd") or 0)
            has_more = bool(win.get("hasMore"))
            size = int(win.get("size") or size or 0)
        except Exception:
            rows = []
    if not rows:
        tail = _index_tails().get(tid) or {}
        rows = list(tail.get("msgs") or [])[-limit:]
        size = size or int(tail.get("size") or 0)
    turns = list((win or {}).get('turns') or [])
    for i, m in enumerate([] if turns else rows):
        mid = str(m.get("id") or "")
        if not mid:
            off = m.get("off")
            mid = ("tail-%s-%s" % (str(tid)[:8], off)) if off is not None \
                else ("tail-%s-%d" % (str(tid)[:8], i))
        if str(m.get("role") or "") == "user":
            it = {"id": mid, "type": "userMessage",
                  "content": [{"type": "text", "text": str(m.get("text") or "")}]}
        else:
            it = {"id": mid, "type": "agentMessage", "text": str(m.get("text") or "")}
        turns.append({"id": "tail-turn-" + mid, "status": "completed", "items": [it]})
    _kind = kind or ("heavy_session" if size >= HEAVY_SESSION_BYTES else "timeout_read")
    if note:
        _note = note
    elif _kind == "heavy_session":
        _note = ("会话较大（%.0f MB），已加载最近 %d 条消息，向上滑动可继续加载"
                 % (size / (1024.0 * 1024.0), len(rows))) if size else \
                ("已加载最近 %d 条消息，向上滑动可继续加载" % len(rows))
    else:
        _note = "读取超时，已返回部分内容"
    return {
        "id": tid,
        "name": item.get("name") or "",
        "title": item.get("title") or "",
        "cwd": item.get("cwd") or "",
        "model": item.get("model") or "",
        "reasoningEffort": item.get("reasoningEffort") or "",
        "createdAt": item.get("createdAt") or 0,
        "updatedAt": item.get("updatedAt") or 0,
        "turns": turns,
        "partial": True,
        "partialKind": _kind,
        "partialNote": _note,
        "loadedMessages": len(rows),
        "nextEnd": next_end,
        "hasMore": has_more,
        "heavy": size >= HEAVY_SESSION_BYTES,
        "sizeBytes": size,
        "turnsTrimmed": False,
    }


def _index_lock_fields(item, tid):
    """把索引器的锁快照翻译成列表/详情要的 writable / running / lock。"""
    lock = dict((item or {}).get("lock") or {})
    held = bool(lock.get("held"))
    try:
        ours = tid in AS.resumed
    except Exception:
        ours = False
    writable = (not held) or bool(lock.get("stale")) or ours
    if AS.control.is_desktop(tid):
        writable = False
        lock['control'] = 'desktop'
        lock['message'] = '已交给电脑端；接管到手机后才可发送。'
    run = bool((item or {}).get("running"))
    try:
        run = run or tid in AS.current_turn
        fs = AS.forced_stop.get(tid)
        if fs and time.time() - fs < 30:
            run = False
    except Exception:
        pass
    run = _runtime_run_hint(tid, run)
    return writable, bool(run), lock


_indexer_supervisor = None


def _indexer_owner():
    """Background-only probe: require a live PID and the actual singleton lock."""
    try:
        old = int(thread_index.PID_FILE.read_text(encoding="utf-8").strip())
        if not _pid_alive(old):
            return None
        with open(thread_index.LOCK_FILE, "r+b") as guard:
            if thread_index._is_free(guard):
                return None
        try:
            import psutil
            args = psutil.Process(old).cmdline()
            if not any(Path(arg).name.lower() in ("indexer.py", "indexer.pyc") for arg in args):
                return None
        except ImportError:
            pass
        return old
    except Exception:
        return None


def _spawn_thread_indexer():
    py = os.environ.get("AGENTREMOTE_PYTHON") or sys.executable
    script = GATEWAY_DIR / "indexer.py"
    if not script.is_file():
        script = GATEWAY_DIR / "indexer.pyc"
    if getattr(sys, "frozen", False) or "python" not in Path(py).name.lower():
        worker = threading.Thread(target=thread_index.loop,
                                  kwargs={"interval": 2.0}, daemon=True)
        worker.start()
        return worker
    if os.name == "nt":
        # A venv pythonw launcher can re-exec console Python. Use the base GUI
        # interpreter and detached stdio so no terminal appears during recovery.
        py = str(Path(sys.base_prefix) / "pythonw.exe")
    run_dir = APP_DIR / "_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
    with open(run_dir / "indexer.log", "a", encoding="utf-8") as logf:
        return subprocess.Popen([py, "-X", "utf8", str(script), "--interval", "2"],
                                cwd=str(GATEWAY_DIR), stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=logf, creationflags=flags)


def _indexer_health(snap=None):
    meta = thread_index.snapshot_meta(snap) if thread_index is not None else {
        "state": "unavailable", "stale": True}
    if _indexer_supervisor is not None:
        owner = _indexer_supervisor.status()
        meta.update(pid=owner.get("pid"), supervisor=owner.get("state"),
                    restarts=owner.get("restarts", 0))
    return meta


def _start_thread_indexer():
    """Watch worker exits throughout gateway life, not just at startup."""
    global _indexer_supervisor
    if thread_index is None:
        log("indexer: module not importable; list API will stay empty")
        return
    if _indexer_supervisor is None:
        from indexer_supervisor import IndexerSupervisor
        _indexer_supervisor = IndexerSupervisor(_indexer_owner, _spawn_thread_indexer, log)
    _indexer_supervisor.start()


def threads_snapshot_payload(limit=60, archived=False, show_all=False,
                             want_q="", want_cwd=""):
    """P1: 列表只读索引器快照。绝不在这里读 jsonl、探锁或走 app-server。"""
    _t_start = time.time()
    snap = _index_snapshot()
    if not snap:
        return {
            "data": [], "engine": "codex", "stale": True,
            "indexer": {"state": "warming"},
            "note": "索引器正在预热，约 2 秒后自动恢复",
        }
    picked = []
    catalog = desktop_project_catalog().data()
    indexed = {item['id']: item for item in snap.get('threads') or []}
    if catalog.get('nativeThreads'):
        indexed = {tid:item for tid,item in indexed.items() if tid in catalog['nativeThreads']}
    for tid, native in catalog.get('nativeThreads', {}).items():
        item = dict(indexed.get(tid) or {'id':tid, 'engine':'codex', 'visible':native.get('source')!='exec'
                    and native.get('thread_source') in (None,'','user'), 'threadSource':native.get('thread_source')})
        item['visible'] = native.get('source')!='exec' and native.get('thread_source') in (None,'','user')
        for source, target in [('archived','archived'),('name','name'),('title','title'),('cwd','cwd'),
                               ('project_id','projectId'),('source','source'),('updated_at','updatedAt')]:
            if source in native:item[target] = native[source]
        if native.get('name') or native.get('title'):
            item['displayTitle'] = native.get('name') or native.get('title')
        if native.get('recency_at') is not None:item['recencyAt'] = native['recency_at']
        indexed[tid] = item
    for item in indexed.values():
        if is_internal_thread(item):
            continue
        if bool(item.get("archived")) != bool(archived):
            continue
        if not show_all:
            if not item.get("visible"):
                continue
        if want_cwd and (item.get("cwd") or "") != want_cwd:
            continue
        if want_q:
            hay = " ".join(str(item.get(k) or "") for k in
                           ("name", "title", "preview",
                            "firstUserMessage", "cwd")).lower()
            if want_q not in hay:
                continue
        picked.append(item)
    picked.sort(key=lambda item: (item.get('recencyAt') or item.get('updatedAt') or 0, item.get('id','')), reverse=True)
    total = len(picked)
    if limit > 0:
        picked = picked[:limit]
    qc = queue_counts()
    try:
        resumed = AS.resumed
    except Exception:
        resumed = set()
    data = []
    for item in picked:
        tid = str(item.get("id") or "")
        if not tid:
            continue
        row = dict(item)
        _hist_fb = bool(row.get("missingTranscript")) and history_has_items(tid)
        if _hist_fb:
            row["missingTranscript"] = False
            row["missingReason"] = HISTORY_NOTE
            row["historyFallback"] = True
        row.pop("rolloutPath", None)
        thread_group_apply(row, catalog)
        _w, _run, _lock = _index_lock_fields(item, tid)
        row["engine"] = "codex"
        row["writable"] = False if _hist_fb else bool(_w)
        row["running"] = bool(_run)
        row["loaded"] = tid in resumed
        row["queueLen"] = int(qc.get(tid, 0))
        row["status"] = {"type": "active" if _run else "idle"}
        row["runtime"] = runtime_status_payload(
            tid, _run, row["writable"], row["queueLen"],
            source="list", missing=_hist_fb)
        if not _w:
            row["lock"] = _lock
        data.append(row)
    _el = time.time() - _t_start
    if _el >= 0.25:
        log("threads snapshot: n=%d total=%.3fs" % (len(data), _el))
    indexer_meta = _indexer_health(snap)
    return {
        "data": data,
        "engine": "codex",
        "projects": copy.deepcopy(catalog['projects']),
        "catalogRevision": catalog.get('revision'),
        "total": total,
        "hasMore": len(data) < total,
        "stale": indexer_meta["stale"],
        "indexer": indexer_meta,
    }


def thread_running(tid: str) -> bool:
    """真在跑? app-server 的 current_turn 优先, 其次 rollout 尾部标记(8 秒缓存)。"""
    _fs = AS.forced_stop.get(tid)
    if _fs:
        # 刚点过停止: 旧回合的 task_complete 还没落盘, tail_running 会骗人。
        # 30 秒窗口内一律当真值“已停”, 队列才能马上接上; 新 turn/started 会清掉。
        if time.time() - _fs < 30:
            return False
        AS.forced_stop.pop(tid, None)
    if tid in AS.current_turn:
        return True
    now = time.time()
    hit = _run_cache.get(tid)
    if hit and now - hit[0] < 8:
        return hit[1]
    run = tail_running(tid)
    _run_cache[tid] = (now, run)
    return run


def codex_can_write(tid: str) -> bool:
    """网关能写这条会话吗(= 我方已持写锁, 锁空闲, 或锁在我方 app-server 手里)。"""
    return thread_writable(tid)


def _queue_item_text(items):
    """队列项 input -> (文本, 图片URL列表)。"""
    text, images = "", []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            text += (c.get("text") or "")
        elif t in ("image", "input_image", "image_url", "localImage", "local_image"):
            u = c.get("url") or c.get("path") or c.get("image_url")
            if u:
                images.append(u)
    return text, images


def queue_snapshot(tid: str, engine: str = "") -> dict:
    """统一队列视图(Codex = 共享 sqlite 队列; Claude = 网关内存队列)。"""
    if WB is not None and (engine == "workbuddy" or WB.is_known(tid)):
        return {"engine": "workbuddy", "data": WB.queue_view(tid)}
    if CLAUDE is not None and (engine == "claude" or CLAUDE.is_known(tid)):
        return {"engine": "claude", "data": CLAUDE.queue_view(tid)}
    try:
        raw = AS.queue_list(tid)
    except ApiError as exc:
        msg = _err_text(exc)
        if "thread not found" in msg or "is archived" in msg:
            return {"engine": "codex", "data": []}
        raise
    data = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        text, imgs = _queue_item_text(it.get("input"))
        data.append({"id": it.get("id"), "text": text, "images": imgs,
                     "clientUserMessageId": it.get("clientUserMessageId")})
    return {"engine": "codex", "data": data}


def codex_queue_start(tid: str, qid=None, timeout: float = 20) -> dict:
    """推一条(空闲才开跑)。回包可能延迟到回合跑起来之后 → 容忍 timeout。"""
    deadline = time.time() + 8.0
    while True:
        try:
            return AS.queue_start(tid, qid)
        except ApiError as exc:
            if _is_timeout(exc):
                return {"__late__": True}
            _m = _err_text(exc)
            # 刚 interrupt 完: app-server 松开 "active turn" 有几帧延迟。
            # 短等重试, 别让用户点完停止还要再等一个 12 秒冷却。
            if ("active or pending turn" in _m or "turn in progress" in _m) and time.time() < deadline:
                time.sleep(0.4)
                continue
            raise


def queue_delete_item(tid: str, qid: str) -> dict:
    if WB is not None and WB.is_known(tid):
        return {"ok": WB.queue_delete(tid, qid), "engine": "workbuddy"}
    if CLAUDE is not None and CLAUDE.is_known(tid):
        return {"ok": CLAUDE.queue_delete(tid, qid), "engine": "claude"}
    ok = AS.queue_delete(tid, qid)
    AS.queued_locally.add(tid)
    return {"ok": ok, "engine": "codex"}


def queue_top_item(tid: str, qid: str) -> dict:
    if WB is not None and WB.is_known(tid):
        return {"ok": WB.queue_top(tid, qid), "engine": "workbuddy"}
    if CLAUDE is not None and CLAUDE.is_known(tid):
        return {"ok": CLAUDE.queue_top(tid, qid), "engine": "claude"}
    ids = [i.get("id") for i in AS.queue_list(tid) if isinstance(i, dict)]
    if qid not in ids:
        raise ApiError({"code": "invalid_request",
                        "message": "该排队消息已不在了（可能已被发送或删除）"})
    AS.queue_reorder(tid, [qid] + [i for i in ids if i != qid])
    return {"ok": True, "engine": "codex"}


def queue_clear_all(tid: str) -> dict:
    if WB is not None and WB.is_known(tid):
        return {"ok": True, "cleared": WB.queue_clear(tid), "engine": "workbuddy"}
    if CLAUDE is not None and CLAUDE.is_known(tid):
        return {"ok": True, "cleared": CLAUDE.queue_clear(tid), "engine": "claude"}
    n = 0
    for qid in [i.get("id") for i in AS.queue_list(tid) if isinstance(i, dict)]:
        try:
            if AS.queue_delete(tid, qid):
                n += 1
        except ApiError as exc:
            log("queue clear %s: %s" % (tid[:8], _err_text(exc)))
    AS.queued_locally.discard(tid)
    return {"ok": True, "cleared": n, "engine": "codex"}


def queue_send_now(tid: str, qid=None) -> dict:
    """把某条排队消息立刻发出去(不等当前回合)。忙 → 409 busy，保持排队不丢。"""
    if WB is not None and WB.is_known(tid):
        return WB.queue_send(tid, qid)
    if CLAUDE is not None and CLAUDE.is_known(tid):
        return CLAUDE.queue_send(tid, qid)
    if AS.status != "ready":
        raise ApiError({"code": "appserver_down", "message": "app-server 未就绪"})
    items = AS.queue_list(tid)
    if not items:
        raise ApiError({"code": "invalid_request", "message": "队列是空的"})
    target = qid or (items[0] or {}).get("id")
    if thread_running(tid):
        raise ApiError({"code": "busy",
                        "message": "当前回合还在跑，插不进去；点「停止」或等它结束，队列会自动推进。"})
    if tid not in AS.resumed:
        if not thread_writable(tid, fresh=True):
            raise ApiError({"code": "thread_in_use",
                            "message": "该会话正被电脑端占用（写锁），现在发不出去；"
                                       "电脑端空闲或关闭该会话后会自动发出。"})
        AS.ensure_resumed(tid)
    AS.queued_locally.add(tid)
    r = codex_queue_start(tid, target)
    return {"ok": True, "engine": "codex", "queuedSubmissionId": target,
            "late": bool(isinstance(r, dict) and r.get("__late__"))}


def _queue_jump_mem(tid: str, qid: str, eng, engine: str) -> dict:
    """Claude / WorkBuddy: 置顶 -> 若在跑就 interrupt(保留队列, 自动续跑队首)。"""
    ids = [i.get("id") for i in eng.queue_view(tid)]
    if qid not in ids:
        return {"ok": False, "engine": engine, "reason": "not_found",
                "message": "这条排队消息已经不在了（可能已被发送或删除）"}
    if not eng.queue_top(tid, qid):
        return {"ok": False, "engine": engine, "reason": "not_found",
                "message": "置顶失败：这条排队消息已经不在了"}
    busy = tid in eng.turns
    if busy:
        r = eng.interrupt(tid, clear_queue=False) or {}
        started = bool(r.get("nextStarted"))
        if started:
            return {"ok": True, "engine": engine, "interrupted": True,
                    "started": True, "queueLen": int(r.get("queueKept") or 0),
                    "message": "已打断当前回合，正在发送这条"}
        # 竞态: busy 是在 interrupt 之前读的, 回合可能刚好自己结束了。
        # 这时 interrupt 返回 stopped=False, 队列原样保留 -> 直接把队首发出去,
        # 不能对用户报“打断了但没启动”。
        if not r.get("stopped"):
            try:
                eng.queue_send(tid, qid)
            except ApiError as exc:
                return {"ok": False, "engine": engine, "interrupted": False,
                        "started": False, "reason": "start_failed",
                        "message": "当前空闲，但这条没能启动：%s" % _err_text(exc)}
            return {"ok": True, "engine": engine, "interrupted": False,
                    "started": True, "message": "当前空闲，已直接发送"}
        return {"ok": False, "engine": engine, "interrupted": True,
                "started": False, "queueLen": int(r.get("queueKept") or 0),
                "reason": "start_failed",
                "message": "已打断当前回合，但这条没能启动（已留在队首）"}
    try:
        eng.queue_send(tid, qid)
    except ApiError as exc:
        return {"ok": False, "engine": engine, "interrupted": False,
                "started": False, "reason": "start_failed",
                "message": "当前空闲，但这条没能启动：%s" % _err_text(exc)}
    return {"ok": True, "engine": engine, "interrupted": False, "started": True,
            "message": "当前空闲，已直接发送"}


def _queue_jump_codex(tid: str, qid: str) -> dict:
    """Codex: 置顶 -> turn/interrupt -> thread/queue/start(指定 qid)。"""
    if AS.status != "ready":
        raise ApiError({"code": "appserver_down", "message": "app-server 未就绪"})
    items = AS.queue_list(tid)
    if not items:
        return {"ok": False, "engine": "codex", "reason": "empty",
                "message": "队列是空的"}
    ids = [i.get("id") for i in items if isinstance(i, dict)]
    if qid not in ids:
        return {"ok": False, "engine": "codex", "reason": "not_found",
                "message": "这条排队消息已经不在了（可能已被发送或删除）"}
    mine = tid in AS.resumed
    if not mine and not thread_writable(tid, fresh=True):
        return {"ok": False, "engine": "codex", "reason": "pc_holds_lock",
                "message": "这一回合是电脑端在跑（写锁在电脑端），AN 不能真正打断它；"
                           "可先「强制接管」清掉占用，或等电脑端空闲后再跳队。"}
    # 先置顶: 万一 QueuePump 抢先开跑, 起的也是这一条
    try:
        AS.queue_reorder(tid, [qid] + [i for i in ids if i != qid])
    except ApiError as exc:
        log("queue jump reorder %s: %s" % (tid[:8], _err_text(exc)))

    turn_id = AS.current_turn.get(tid)
    if not turn_id and mine:
        try:
            tl = AS.request("thread/turns/list",
                            {"threadId": tid, "limit": 5, "sortDirection": "desc"},
                            timeout=30)
            for t in tl.get("data") or []:
                st = (t.get("status") or {}).get("type") if isinstance(t.get("status"), dict) else t.get("status")
                if st in ("inProgress", "running", "in_progress"):
                    turn_id = t.get("id")
                    break
        except ApiError as exc:
            log("queue jump scan %s: %s" % (tid[:8], _err_text(exc)))

    interrupted = False
    if turn_id:
        AS.request("turn/interrupt", {"threadId": tid, "turnId": turn_id}, timeout=30)
        # 与 /interrupt 路由同一套状态清理: 不能等 turn/completed 晚到
        AS.current_turn.pop(tid, None)
        _run_cache.pop(tid, None)
        AS.forced_stop[tid] = time.time()
        interrupted = True
        try:
            HUB.broadcast({"kind": "notification", "method": "turn/completed",
                           "params": {"threadId": tid,
                                      "turn": {"id": turn_id, "status": "aborted",
                                               "engine": "codex"}}})
        except Exception:
            pass
        log("queue jump %s turn=%s qid=%s" % (tid[:8], str(turn_id)[:13], str(qid)[:13]))

    if tid not in AS.resumed:
        AS.ensure_resumed(tid)
    AS.queued_locally.add(tid)
    try:
        r = codex_queue_start(tid, qid)
    except ApiError as exc:
        msg = _err_text(exc)
        # QueuePump 抢先启动 -> 队列里已经没有这条了, 视为成功
        try:
            left = [i for i in AS.queue_list(tid) if isinstance(i, dict) and i.get("id") == qid]
        except ApiError:
            left = []
        if not left:
            return {"ok": True, "engine": "codex", "interrupted": interrupted,
                    "started": True,
                    "message": "已打断当前回合，正在发送这条" if interrupted
                               else "当前空闲，已直接发送"}
        log("queue jump start %s: %s" % (tid[:8], msg))
        return {"ok": False, "engine": "codex", "interrupted": interrupted,
                "started": False, "reason": "start_failed",
                "message": ("已打断当前回合，但这条没启动成功：%s" % msg) if interrupted
                           else ("这条没启动成功：%s" % msg)}

    return {"ok": True, "engine": "codex", "interrupted": interrupted,
            "started": True,
            "late": bool(isinstance(r, dict) and r.get("__late__")),
            "message": "已打断当前回合，正在发送这条" if interrupted
                       else "当前空闲，已直接发送"}


def queue_jump_item(tid: str, qid: str) -> dict:
    """打断当前回合，并立刻发送指定的排队消息。

    与 queue_send_now 的区别: 后者忙时直接 409；这里先停掉当前回合再发。
    返回结构化结果，前端据此说人话，不再把“只置顶、等空闲”伪装成成功。
    """
    if WB is not None and WB.is_known(tid):
        return _queue_jump_mem(tid, qid, WB, "workbuddy")
    if CLAUDE is not None and CLAUDE.is_known(tid):
        return _queue_jump_mem(tid, qid, CLAUDE, "claude")
    return _queue_jump_codex(tid, qid)


class QueuePump:
    """把共享队列真正推起来: 当前回合结束后自动发下一条。

    codex 自己**不会**在回合结束时推进队列(2026-10-16 实测: 忙时入队, 回合正常结束后
    30 秒零提升), 所以由网关补这个泵。节拍 2.5 秒; 每条会话每轮最多推一条,
    下一条等这回合结束(靠 queue/start 的"空闲才开跑"语义保证不并发)。
    """

    POLL = 2.5
    COOLDOWN = 12.0        # 同一会话两次尝试之间的最短间隔(防重复猛推)

    def __init__(self, hub: Hub):
        self.hub = hub
        self._tail = set()     # 上轮见过队列的 tid
        self._firing = set()
        self._last = {}

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        log("QueuePump started (poll %.1fs)" % self.POLL)

    def kick(self, tid: str):
        # 刚结束 / 刚被停止一个回合 -> 立刻推一次队列, 不等下一个节拍。
        if not tid:
            return
        self._last.pop(tid, None)

        def go():
            try:
                if self._step(tid):
                    self._tail.add(tid)
            except Exception as exc:
                log("queue kick %s: %r" % (tid[:8], exc))

        threading.Thread(target=go, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(self.POLL)
            try:
                self.tick()
                if time.monotonic() - getattr(self, '_test_sweep_at', 0) >= 30:
                    self._test_sweep_at = time.monotonic()
                    from test_lifecycle import TestLifecycle
                    TestLifecycle(APP_DIR / '_run' / 'test-lifecycle.sqlite').sweep(
                        _index_snapshot(),
                        lambda tid: thread_running(tid) or bool(AS.queue_list(tid)),
                        lambda tid: AS.request('thread/archive', {'threadId': tid}, timeout=30))
            except Exception as exc:
                log("queue pump: %r" % (exc,))

    def tick(self):
        if AS is None or AS.status != "ready":
            return
        tids = (queue_thread_ids() | set(AS.queued_locally) | set(self._tail)
                | set(AS.pending_switch) | queue_intent_store().pending_thread_ids())
        alive = set()
        for tid in tids:
            try:
                if self._step(tid):
                    alive.add(tid)
            except ApiError as exc:
                msg = _err_text(exc)
                if "thread not found" in msg or "is archived" in msg:
                    AS.queued_locally.discard(tid)
                    continue
                alive.add(tid)
            except Exception as exc:
                log("queue pump %s: %r" % (tid[:8], exc))
                alive.add(tid)
        self._tail = alive

    def _step(self, tid: str) -> bool:
        if AS.control.is_desktop(tid):
            return False
        items = AS.queue_list(tid)
        durable_settings = queue_intent_store().get_thread_settings(tid)
        if not items:
            AS.queued_locally.discard(tid)
            if tid not in AS.pending_switch and not durable_settings:
                return False
        if tid in self._firing:
            return True
        if thread_running(tid):
            return True                      # 正在跑: 下一条等它结束
        if not codex_can_write(tid):
            return True                      # 写锁在电脑端: 不抢, 保持排队
        if tid not in AS.resumed:
            try:
                AS.ensure_resumed(tid)
            except ApiError as exc:
                log("queue pump resume %s: %s" % (tid[:8], _err_text(exc)))
                return True
        with SETTINGS_LOCK:
            _head = items[0] if items else {}
            _cid = (_head or {}).get('clientUserMessageId')
            _intent = None
            _ps = AS.pending_switch.get(tid)
            if durable_settings:
                try:
                    from queue_intents import apply_intent
                    apply_intent(AS,tid,durable_settings,model_provider_of,_wait_codex_settings)
                    queue_intent_store().complete_thread_settings(tid)
                    # Durable state is newer than any legacy in-memory slot;
                    # never let a pre-upgrade/stale value replay next turn.
                    AS.pending_switch.pop(tid,None)
                except ApiError as exc:
                    log("queue pump durable settings %s: %s" % (tid[:8],_err_text(exc)))
                    return True
                except Exception as exc:
                    log("queue pump durable settings %s: %s" % (tid[:8],type(exc).__name__))
                    return True
            elif _ps:
                try:
                    AS.switch_thread_model(tid, _ps[0], _ps[1], _ps[2])
                    if AS.pending_switch.get(tid) == _ps:
                        AS.pending_switch.pop(tid, None)
                except ApiError as exc:
                    log("queue pump provider switch %s: %s" % (tid[:8], _err_text(exc)))
                    return True
        if not items:
            return False
        qid = (items[0] or {}).get("id")
        if not qid:
            return True
        now = time.time()
        if now - self._last.get(tid, 0) < self.COOLDOWN:
            return True
        self._last[tid] = now
        self._firing.add(tid)

        def fire():
            try:
                from queue_intents import promote_head
                result=promote_head(AS,queue_intent_store(),tid,_head,
                    model_provider_of,_wait_codex_settings,
                    lambda:codex_queue_start(tid,qid))
                if isinstance(result,dict) and result.get('__late__'):
                    log("queue pump: native start pending confirmation for %s qid=%s" % (tid[:8],str(qid)[:13]))
                    return
                log("queue pump: promoted %s qid=%s" % (tid[:8], str(qid)[:13]))
            except ApiError as exc:
                self._last.pop(tid, None)     # 失败不占 12s 冷却: 下个节拍马上再试
                log("queue pump %s: %s" % (tid[:8], _clip(_err_text(exc), 160)))
            except Exception as exc:
                log("queue pump %s: %r" % (tid[:8], exc))
            finally:
                time.sleep(1.5)
                self._firing.discard(tid)
                AS.last_activity[tid] = time.time()

        threading.Thread(target=fire, daemon=True).start()
        return True


AUTH_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>访问链接已失效 · Agent Nexus</title>
<style>
:root{color-scheme:dark;--bg:#080a10;--line:rgba(148,163,255,.18);--fg:#eef0ff;--sub:#a6acc8;--dim:#777f9f;--rose:#fb7185}
*{box-sizing:border-box}
html,body{min-height:100%;margin:0}
body{background:radial-gradient(1000px 700px at 50% -20%,#1a2350 0%,#0b0e18 52%,var(--bg) 100%);color:var(--fg);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;display:flex;min-height:100svh;flex-direction:column;align-items:center;justify-content:center;padding:28px 18px 18px;overflow-x:hidden}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);background-size:40px 40px;mask-image:linear-gradient(#000,transparent 72%)}
.wrap{position:relative;width:min(460px,100%);z-index:1}
.card{background:linear-gradient(180deg,rgba(22,27,45,.96),rgba(13,16,28,.98));border:1px solid var(--line);border-radius:24px;padding:24px 22px 20px;box-shadow:0 28px 90px rgba(0,0,0,.55),inset 0 1px rgba(255,255,255,.04);text-align:center;animation:in .5s cubic-bezier(.2,.8,.2,1) both}
.eyebrow{display:flex;align-items:center;justify-content:center;gap:7px;color:#b9c0e6;font-size:10px;font-weight:700;letter-spacing:.19em;text-transform:uppercase}
.dot{width:6px;height:6px;border-radius:50%;background:var(--rose);box-shadow:0 0 12px rgba(251,113,133,.8)}
.ticket{position:relative;width:240px;max-width:90%;height:92px;margin:20px auto 18px;border-radius:14px;background:linear-gradient(135deg,rgba(129,140,248,.20),rgba(103,232,249,.08));border:1px solid rgba(148,163,255,.32);overflow:hidden;transform:rotate(-1.5deg)}
.ticket:before,.ticket:after{content:"";position:absolute;top:50%;width:18px;height:18px;border-radius:50%;background:#0d101c;border:1px solid rgba(148,163,255,.28);transform:translateY(-50%)}
.ticket:before{left:-10px}.ticket:after{right:-10px}
.stub{position:absolute;left:15px;top:14px;bottom:14px;width:66px;border-right:1px dashed rgba(148,163,255,.35);display:flex;align-items:center;justify-content:center;font:800 25px/1 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:-2px;color:#dbe2ff}
.lines{position:absolute;left:94px;right:72px;top:28px;display:grid;gap:9px}
.lines i{height:5px;border-radius:9px;background:rgba(196,205,255,.18)}
.lines i:nth-child(2){width:74%}.lines i:nth-child(3){width:48%}
.stamp{position:absolute;right:12px;top:50%;transform:translateY(-50%) rotate(8deg);font:800 9px/1 ui-monospace,Consolas,monospace;letter-spacing:.12em;color:var(--rose);border:1px solid rgba(251,113,133,.5);border-radius:6px;padding:5px 6px}
h1{font-size:clamp(22px,7vw,28px);line-height:1.2;margin:0 0 9px;letter-spacing:-.03em}
.lead{margin:0 auto 18px;max-width:340px;color:var(--sub);font-size:13.5px;line-height:1.75}
.lead b{color:#dfe4ff;font-weight:650}
.steps{display:grid;gap:8px;text-align:left;background:rgba(129,140,248,.07);border:1px solid rgba(148,163,255,.16);border-radius:14px;padding:13px 14px}
.steps div{display:flex;align-items:flex-start;gap:10px;color:#c9cfeb;font-size:12.5px;line-height:1.55}
.num{flex:none;width:20px;height:20px;border-radius:7px;background:rgba(129,140,248,.20);border:1px solid rgba(129,140,248,.35);color:#c7d2fe;font:700 10px/18px ui-monospace,Consolas,monospace;text-align:center}
.note{margin:15px 0 0;color:var(--dim);font-size:11px;line-height:1.6}
.foot{margin:15px 0 0;text-align:center;color:#666f90;font-size:10px;letter-spacing:.08em}
@keyframes in{from{opacity:0;transform:translateY(14px) scale(.985)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.card{animation:none}.ticket{transform:none}}
@media (prefers-color-scheme:light){:root{color-scheme:light}body{background:radial-gradient(900px 650px at 50% -15%,#dfe5ff,#f4f6fc 58%,#eef1f8)}body:before{opacity:.45}.card{background:linear-gradient(180deg,#fff,#f8f9ff);box-shadow:0 24px 70px rgba(40,50,110,.16)}.ticket:before,.ticket:after{background:#f8f9ff}.lead{color:#5d6685}.lead b{color:#1c2650}.eyebrow{color:#68719a}.stub{color:#33417f}.stamp{color:#b4233f;border-color:rgba(180,35,63,.42)}.num{background:#e7eaff;border-color:#c8cef8;color:#3b4a97}.lines i{background:rgba(90,105,180,.20)}.note{color:#6b7290}h1{color:#1a2038}.steps{background:#f3f5ff}.steps div{color:#3e4664}.foot{color:#6b7290}}
</style>
</head>
<body>
<main class="wrap">
  <section class="card" role="main">
    <div class="eyebrow"><span class="dot"></span> Agent Nexus · 访问校验</div>
    <div class="ticket" aria-hidden="true">
      <div class="stub">401</div>
      <div class="lines"><i></i><i></i><i></i></div>
      <div class="stamp">EXPIRED</div>
    </div>
    <h1>这张通行证，过期了</h1>
    <p class="lead">二维码还是那张二维码，<br><b>但电脑那边的钥匙已经换了。</b></p>
    <div class="steps">
      <div><span class="num">1</span><span>回到电脑上的 Agent Nexus 启动器</span></div>
      <div><span class="num">2</span><span>点「停止服务」，再点「重新启动」</span></div>
      <div><span class="num">3</span><span>用新弹出的二维码重新扫码</span></div>
    </div>
    <p class="note">这是故意的安全设计：每次重启都会换一把钥匙，旧链接不能继续访问你的电脑。</p>
  </section>
  <p class="foot">Agent Nexus contributors · 个人开发者</p>
</main>
</body>
</html>
"""


UI_MISSING_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>界面文件缺失 · Agent Nexus</title>
<style>
:root{color-scheme:dark;--bg:#080a10;--line:rgba(148,163,255,.18);--fg:#eef0ff;--sub:#a6acc8;--dim:#777f9f;--amber:#fbbf24}
*{box-sizing:border-box}
html,body{min-height:100%;margin:0}
body{background:radial-gradient(1000px 700px at 50% -20%,#2a2350 0%,#0b0e18 52%,var(--bg) 100%);color:var(--fg);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;display:flex;min-height:100svh;flex-direction:column;align-items:center;justify-content:center;padding:28px 18px 18px;overflow-x:hidden}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);background-size:40px 40px;mask-image:linear-gradient(#000,transparent 72%)}
.wrap{position:relative;width:min(460px,100%);z-index:1}
.card{background:linear-gradient(180deg,rgba(28,27,45,.96),rgba(15,16,28,.98));border:1px solid var(--line);border-radius:24px;padding:24px 22px 20px;box-shadow:0 28px 90px rgba(0,0,0,.55),inset 0 1px rgba(255,255,255,.04);text-align:center;animation:in .5s cubic-bezier(.2,.8,.2,1) both}
.eyebrow{display:flex;align-items:center;justify-content:center;gap:7px;color:#d8c6a6;font-size:10px;font-weight:700;letter-spacing:.19em;text-transform:uppercase}
.dot{width:6px;height:6px;border-radius:50%;background:var(--amber);box-shadow:0 0 12px rgba(251,191,36,.8)}
.box{position:relative;width:188px;height:112px;margin:20px auto 18px;border:1px solid rgba(251,191,36,.34);border-radius:16px;background:linear-gradient(145deg,rgba(251,191,36,.12),rgba(251,191,36,.03));transform:rotate(-1deg)}
.box:before,.box:after{content:"";position:absolute;left:50%;width:34px;height:3px;border-radius:9px;background:rgba(251,191,36,.48);transform:translateX(-50%)}
.box:before{top:34px}.box:after{top:50px;width:24px}
.box i{position:absolute;left:50%;bottom:18px;width:44px;height:20px;border:2px dashed rgba(251,191,36,.55);border-radius:7px;transform:translateX(-50%)}
.box b{position:absolute;right:-10px;top:-10px;width:32px;height:32px;border-radius:50%;background:#231b10;border:1px solid rgba(251,191,36,.55);color:var(--amber);font:800 20px/30px ui-monospace,Consolas,monospace}
h1{font-size:clamp(22px,7vw,28px);line-height:1.2;margin:0 0 9px;letter-spacing:-.03em}
.lead{margin:0 auto 18px;max-width:350px;color:var(--sub);font-size:13.5px;line-height:1.75}
.lead b{color:#ffe8ad;font-weight:650}
.steps{display:grid;gap:8px;text-align:left;background:rgba(251,191,36,.07);border:1px solid rgba(251,191,36,.16);border-radius:14px;padding:13px 14px}
.steps div{display:flex;align-items:flex-start;gap:10px;color:#e2d8c7;font-size:12.5px;line-height:1.55}
.num{flex:none;width:20px;height:20px;border-radius:7px;background:rgba(251,191,36,.16);border:1px solid rgba(251,191,36,.34);color:#ffe09a;font:700 10px/18px ui-monospace,Consolas,monospace;text-align:center}
.actions{display:flex;gap:10px;justify-content:center;margin-top:16px;flex-wrap:wrap}
.btn{appearance:none;border:1px solid rgba(251,191,36,.38);background:rgba(251,191,36,.14);color:#ffe8b4;border-radius:12px;padding:10px 16px;font:650 13px/1.2 inherit;cursor:pointer}
.btn:active{transform:translateY(1px)}
.note{margin:15px 0 0;color:var(--dim);font-size:11px;line-height:1.6}
.foot{margin:15px 0 0;text-align:center;color:#6f6a60;font-size:10px;letter-spacing:.08em}
@keyframes in{from{opacity:0;transform:translateY(14px) scale(.985)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.card{animation:none}.box{transform:none}}
@media (prefers-color-scheme:light){:root{color-scheme:light}body{background:radial-gradient(900px 650px at 50% -15%,#fff0c9,#f7f5ef 58%,#f1efe8)}body:before{opacity:.35}.card{background:linear-gradient(180deg,#fff,#fffaf0);box-shadow:0 24px 70px rgba(100,75,20,.16)}.eyebrow{color:#8a6420}.lead{color:#665b48}.lead b{color:#5a3b00}.steps{background:#fff8e7}.steps div{color:#574c39}.note{color:#776d5b}.foot{color:#877d6b}h1{color:#241b08}.btn{background:#ffe9b4;color:#5a3b00}}
</style>
</head>
<body>
<main class="wrap">
  <section class="card" role="main">
    <div class="eyebrow"><span class="dot"></span> Agent Nexus · 启动自检</div>
    <div class="box" aria-hidden="true"><b>!</b><i></i></div>
    <h1>只差一块拼图</h1>
    <p class="lead">电脑已经在跑，但<b>手机界面文件没找到</b>。<br>不是你的网络问题，是发行包解压不完整。</p>
    <div class="steps">
      <div><span class="num">1</span><span>回到电脑上的 Agent Nexus 文件夹，确认存在 <b>ui\index.html</b></span></div>
      <div><span class="num">2</span><span>整个压缩包重新解压一次，别只拖出来一半</span></div>
      <div><span class="num">3</span><span>重新启动网关，再扫一次二维码</span></div>
    </div>
    <div class="actions"><button class="btn" onclick="location.reload()">重新检查</button></div>
    <p class="note">这条提示不会泄露你的目录、链接或访问码。修好后刷新即可。</p>
  </section>
  <p class="foot">Agent Nexus contributors · 个人开发者</p>
</main>
</body>
</html>
"""


def _send_html_page(handler, body_text, status=200, cache_control="no-store, max-age=0, must-revalidate"):
    body = body_text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", cache_control)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception as exc:
        handler.close_connection = True
        if not handler._client_disconnected(exc):
            log("html_page_write_error %s: %r" % (getattr(handler, "path", "?"), exc))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Agent Nexus/1.2"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass  # 静音, 自己打的日志够用

    def log_request(self, code='-', size='-'):
        try:
            if self.command is None:
                rr = (getattr(self, "raw_requestline", None) or b"")[:120]
                log("req< [badline] %r -> %s" % (rr, code))
                # 坏请求行意味着流已经错位；绝不在脏连接上继续解析后半截。
                self.close_connection = True
                return
            _ms = ""
            try:
                _t0 = getattr(self, "_req_t0", None)
                if _t0:
                    _ms = " %.0fms" % ((time.time() - _t0) * 1000.0)
            except Exception:
                _ms = ""
            log("req< %s %s -> %s%s" % (self.command, self.path.split("?")[0], code, _ms))
        except Exception:
            pass

    def end_headers(self):
        # HTTP/1.1 keep-alive: 每个 JSON 都带 Content-Length, 端口复用能把
        # 公网隧道下每请求一次 TCP/TLS 握手的 RTT 省掉(手机端体感差异很大)。
        # 只有两种情况主动 close: 客户端要求, 或 POST 正文没读完(残留字节会串帧)。
        try:
            if getattr(self, "_req_body_left", 0) > 0 and not getattr(self, "_req_body_read", False):
                # 只写 Connection: close 不够；服务器循环还会继续读残留字节。
                # 必须同时置 close_connection，避免 chunked 终止块被当成下一条请求行。
                self.close_connection = True
            buf = getattr(self, "_headers_buffer", None) or []
            if not any(b"Connection" in h for h in buf):
                self.send_header("Connection", "close" if self.close_connection else "keep-alive")
        except Exception:
            pass
        super().end_headers()

    def _accepts_gzip(self) -> bool:
        try:
            return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        except Exception:
            return False

    def _auth_level(self, qs):
        tok = None
        from_qs = False
        if "k" in qs and qs["k"]:
            tok = qs["k"][0]
            from_qs = True
        if not tok:
            tok = self.headers.get("X-Auth")
        if not tok:
            cookie = self.headers.get("Cookie") or ""
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("arc="):
                    tok = part[4:]
                    break
        if tok and tok == TOKEN:
            # 首次用 ?k= 进来时记住凭据，之后浏览器同域访问自动带上，不必每次都拼 token
            if from_qs:
                self._auth_cookie_pending = tok
            return "full"
        if tok and tok == VIEW_TOKEN:
            if from_qs:
                self._auth_cookie_pending = tok
            return "view"
        return None

    def _auth_ok(self, qs) -> bool:
        return self._auth_level(qs) is not None

    # ---- 内测试用拦截 (TRIAL_GATE_V1) ----
    def _trial_gate(self, path) -> bool:
        """试用到期后拦截业务 API; 返回 True 表示已写响应。
        白名单: /, /index.html (结束页), /api/trial (查状态/续期), /api/ui/ver"""
        try:
            if TRIAL is None:
                return False
            if path in ("/", "/index.html", "/api/trial", "/api/ui/ver"):
                return False
            if not TRIAL.blocked():
                return False
            self._json({"error": {"code": "trial_expired",
                                  "message": TRIAL.block_msg()}}, 403)
            return True
        except Exception:
            return False

    # ---- 只读观赏 token 加固 (VIEW_FIX_V1) ----
    def _view_guard(self, qs, path) -> bool:
        """view token 不得访问 文件系统/审批 接口; 仅放行 gateway/uploads 下的图片。"""
        try:
            if self._auth_level(qs) != "view":
                return False
        except Exception:
            return False
        deny = path.startswith("/api/fs/") or path in ("/api/approvals",)
        if not deny:
            return False
        if path == "/api/fs/raw":
            try:
                p = (qs.get("path") or [""])[0]
                real = os.path.realpath(p) if p else ""
                if real:
                    rl = os.path.normcase(real)
                    ul = os.path.normcase(os.path.realpath(str(UPLOAD_DIR)))
                    if rl == ul or rl.startswith(ul + os.sep):
                        return False
            except Exception:
                pass
        self._json({"error": {"code": "read_only",
                              "message": "这是只读观赏链接: 只能看, 不能操作。"}}, 403)
        return True

    @staticmethod
    def _client_disconnected(exc) -> bool:
        """True when the peer vanished while we were writing a response.

        Windows surfaces these as WinError 10053/10054/10055/10058/10060,
        POSIX as EPIPE/ECONNRESET.  They are normal when a phone scans the QR
        twice, refreshes a page, or the tunnel reconnects; they must never be
        reported as an API failure.
        """
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return True
        code = getattr(exc, "winerror", None)
        if code is None:
            code = getattr(exc, "errno", None)
        if code in (32, 100, 103, 104, 10053, 10054, 10055, 10058, 10060):
            return True
        text = str(exc).lower()
        return any(marker in text for marker in (
            "connection reset", "connection aborted", "broken pipe",
            "econnreset", "epipe", "winerror 10053", "winerror 10054",
            "winerror 10055", "winerror 10058", "winerror 10060",
        ))

    def _err(self, kind, message, code=500, retry_after=None, **extra):
        body = {"ok": False, "error_kind": kind, "message": message}
        if retry_after is not None:
            body["retry_after"] = retry_after
        if extra:
            body.update(extra)
        return self._json(body, code)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        enc = None
        if len(body) >= 1024 and self._accepts_gzip():
            try:
                gz = gzip.compress(body, 5)
                if len(gz) < len(body) - 64:
                    body, enc = gz, "gzip"
            except Exception:
                pass
        # end_headers() flushes the header buffer.  A phone that closes the
        # connection between request and response used to raise 10054 there,
        # outside the old write-only try block, and the outer handler logged a
        # false api_500.  Keep the whole response write in one boundary.
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if enc:
                self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self.close_connection = True
            if not self._client_disconnected(exc):
                log("api_write_error %s: %r" % (getattr(self, "path", "?"), exc))

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            part = self.rfile.read(n - len(buf))
            if not part:
                raise ValueError("unexpected_eof")
            buf.extend(part)
        return bytes(buf)

    def _drain_body(self) -> None:
        """Consume a body we will not parse; never leave bytes in the stream."""
        try:
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                while True:
                    line = self.rfile.readline(65537)
                    if not line:
                        self.close_connection = True
                        return
                    tok = line.strip().split(b";", 1)[0].strip()
                    if not tok:
                        continue
                    n = int(tok, 16)
                    if n == 0:
                        while True:
                            t = self.rfile.readline(65537)
                            if not t or t in (b"\r\n", b"\n"):
                                return
                    if n > MAX_REQUEST_BODY:
                        self.close_connection = True
                        return
                    self._read_exact(n)
                    self._read_exact(2)
            else:
                n = int(self.headers.get("Content-Length") or 0)
                if 0 < n <= MAX_REQUEST_BODY:
                    self._read_exact(n)
        except Exception:
            self.close_connection = True

    def _read_body(self) -> dict:
        """读取 Content-Length 或 chunked 请求体；framing 出错就关闭连接。"""
        self._req_body_read = False
        self._req_body_error = ""
        chunked = bool(getattr(self, "_req_chunked", False))
        raw = b""
        try:
            if chunked:
                parts = []
                total = 0
                while True:
                    line = self.rfile.readline(65537)
                    if not line:
                        raise ValueError("eof_before_chunk_size")
                    if len(line) > 65536:
                        raise ValueError("chunk_size_line_too_long")
                    token = line.strip().split(b";", 1)[0].strip()
                    if not token:
                        continue
                    try:
                        size = int(token, 16)
                    except ValueError:
                        raise ValueError("invalid_chunk_size")
                    if size < 0:
                        raise ValueError("negative_chunk_size")
                    if total + size > MAX_REQUEST_BODY:
                        raise ValueError("body_too_large")
                    if size == 0:
                        while True:
                            trailer = self.rfile.readline(65537)
                            if not trailer:
                                raise ValueError("eof_in_trailer")
                            if len(trailer) > 65536:
                                raise ValueError("trailer_too_long")
                            if trailer in (b"\r\n", b"\n"):
                                break
                        break
                    parts.append(self._read_exact(size))
                    total += size
                    if self._read_exact(2) != b"\r\n":
                        raise ValueError("chunk_missing_crlf")
                raw = b"".join(parts)
            else:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except Exception:
                    raise ValueError("invalid_content_length")
                if n < 0 or n > MAX_REQUEST_BODY:
                    raise ValueError("body_too_large")
                self._req_body_left = n
                if n > 0:
                    raw = self._read_exact(n)
        except Exception as exc:
            self._req_body_error = str(exc) or "body_read_failed"
            self.close_connection = True
            log("badbody: %s" % self._req_body_error)
            return {}
        self._req_body_read = True
        self._req_body_left = 0
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._req_body_error = "invalid_json"
            self.close_connection = True
            log("badbody: invalid_json %r" % (exc,))
            return {}
        return obj if isinstance(obj, dict) else {"value": obj}

    def _auth_fail(self, path):
        """401 分流: 页面请求给人看的失效页, API 请求仍返回 JSON。"""
        if path not in ("/", "/index.html"):
            return self._json({"error": "unauthorized"}, 401)
        from access_recovery import login_page
        qs=parse_qs(urlparse(self.path).query)
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie') or '')
        except Exception:pass
        has_credential=bool(self.headers.get('X-Auth') or (qs.get('k') or [''])[0] or cookie.get('arc'))
        body = login_page(has_credential).encode("utf-8")
        try:
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self.close_connection = True
            if not self._client_disconnected(exc):
                log("auth_page_write_error %s: %s" % (path, type(exc).__name__))

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        self._req_t0 = time.time()
        _te = (self.headers.get('Transfer-Encoding') or '').lower()
        if 'chunked' in _te or int(self.headers.get('Content-Length') or 0) > 0:
            self._drain_body()
        self._req_body_left = 0
        self._req_body_read = False
        try:
            log("req> GET %s" % path)
        except Exception:
            pass
        qs = parse_qs(u.query)
        # 健康检查必须无 token 可用：它只证明网关进程活着，不泄露任何配置。
        if path == "/api/health":
            return self._json({"ok": True, "service": "agent-nexus",
                               "version": "1.2", "pid": os.getpid(), "time": time.time(),
                               "handoffVersion": 2,
                               "accessRecoveryVersion": 1,
                               "dispatchVersion": 1,
                               "queueIntentVersion": 1,
                               "workflowVersion": 2,
                               "projectMembershipVersion": 1,
                               "engineSidebarVersion": 1,
                               "apiContractVersion": HTTP_API_VERSION,
                               "wsContractVersion": WS_RPC_VERSION,
                               "appserverPid": AS.proc.pid if AS.proc and AS.proc.poll() is None else None,
                               "indexer": _indexer_health()})
        if GUARD is not None and GUARD.gate(self, path, qs, "GET"):
            return
        auth_level = self._auth_level(qs)
        auth_error = authorize_http('GET', path, auth_level)
        if auth_error:
            if auth_error['status'] == 401:
                return self._auth_fail(path)
            return self._json({"error": auth_error}, auth_error['status'])
        if self._trial_gate(path):
            return
        if self._view_guard(qs, path):
            return
        try:
            if path in ("/", "/index.html"):
                return self._serve_ui()
            if path == '/api/rpc/contract':
                return self._json(rpc_contract_document())
            if path == "/api/whoami":
                return self._json({"role": self._auth_level(qs) or "none"})
            if path == '/api/workflows':
                return self._json({'data':workflow_store().list((qs.get('threadId') or [None])[0]),'oneShotOnly':True})
            if path.startswith('/api/workflows/') and path.count('/')==3:
                store=workflow_store()
                task=store.get(path.rsplit('/',1)[1])
                return self._json({'task':task,'events':store.events(task['id']) if task else []},200 if task else 404)
            if path == "/api/settings":
                wb_ensure_cli()
                _st = _nexus_read()
                return self._json({
                    "thinkZh": think_zh_on(),
                    "injected": len(_st.get("injected") or []),
                    "claude": bool(CLAUDE is not None and CLAUDE.status != "no-cli"),
                    "workbuddy": bool(WB is not None and WB.status != "no-cli"),
                })
            if path == "/api/ui/ver":
                try:
                    _st = (UI_DIR / "index.html").stat()
                    _v = "%d-%d" % (int(_st.st_mtime), int(_st.st_size))
                except Exception:
                    _v = ""
                return self._json({"v": _v})
            if path == "/api/trial":
                if TRIAL is None:
                    return self._json({"mode": "off"})
                # mac/浏览器路径没有桌面启动器, 这条请求就是"界面真的起来了"
                # 的证据: 第一次拿到试用状态时才起表, 重复请求保持幂等。
                try:
                    TRIAL.activate()
                except Exception:
                    pass
                return self._json(TRIAL.info())
            if path == "/api/status":
                wb_ensure_cli()
                return self._json({
                    "ok": True,
                    "appserver": AS.status,
                    "resumed": len(AS.resumed),
                    "runningTurns": len(AS.current_turn),
                    "pendingApprovals": len(AS.srv_reqs),
                    "tailWatched": (len(TAILER.watch) if TAILER is not None else 0),
                    "runtimeEpoch": _RUNTIME_EPOCH,
                    "indexer": _indexer_health(),
                    "claude": ({"status": CLAUDE.status, "running": len(CLAUDE.turns),
                                "pending": len(CLAUDE.pending)} if CLAUDE is not None else None),
                    "workbuddy": ({"status": WB.status, "running": len(WB.turns),
                                   "pending": len(WB.pending)} if WB is not None else None),
                    "time": time.time(),
                })
            if path == '/api/reliability':
                return self._json({'events':HUB.recovery_status(), 'submissions':submission_store().stats()})
            if path == '/api/usage':
                thread_id = (qs.get('thread') or [''])[0]
                router_url=os.environ.get('AN_ROUTER_URL','').rstrip('/')
                if not router_url:return self._json({'error':{'code':'usage_unavailable','message':'Optional usage router is not configured'}},503)
                endpoint = router_url+'/router/usage'
                if thread_id:
                    from urllib.parse import urlencode
                    endpoint += '?'+urlencode({'thread':thread_id})
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(endpoint,timeout=3) as response:
                        usage = json.load(response)
                    return self._json(usage)
                except Exception:
                    return self._json({'error':{'code':'usage_unavailable','message':'用量记录暂不可用，请稍后刷新'}},503)
            if path.startswith('/api/threads/') and path.count('/') == 4 and path.endswith('/recovery'):
                tid = path.split('/')[3]
                HUB.flush(timeout=.15)
                from recovery_projection import project_items
                events = RECOVERY_JOURNAL.events_after(0, tid=tid) if RECOVERY_JOURNAL else []
                with HUB.lock:
                    head = HUB.event_seq
                with _RUNTIME_STATUS_LOCK:
                    runtime = dict(_RUNTIME_STATUS.get(tid) or {})
                return self._json({'threadId':tid, 'epoch':_RUNTIME_EPOCH, 'through':head,
                                   'persistedThrough':HUB.persisted_seq,
                                   'partial':bool(HUB.backlog_dropped),
                                   'items':project_items(events,tid), 'runtime':runtime})
            if path.startswith('/api/threads/') and path.count('/') == 5 and '/submissions/' in path:
                tid, cid = path.split('/')[3], path.split('/')[5]
                receipt, status = submission_store().resolve(tid, cid, reconcile_submission)
                return self._json(receipt, status)
            if path == "/api/threads":
                if (qs.get("engine") or [""])[0] == "workbuddy":
                    wb_ensure_cli()
                    if WB is None:
                        return self._json({"data": [], "engine": "workbuddy", "workbuddyStatus": "off"})
                    _out = WB.list_threads(
                        limit=int((qs.get("limit") or ["60"])[0]),
                        q=(qs.get("q") or [""])[0],
                        cwd=(qs.get("cwd") or [""])[0],
                        archived=(qs.get("archived") or ["0"])[0] == "1")
                    for _t in _out.get("data") or []:
                        _t["queueLen"] = len(WB.queues.get(_t.get("id")) or [])
                        _wb_run = bool(_t.get("running"))
                        _t["runtime"] = wb_runtime_payload(
                            _t.get("id"), _wb_run, bool(_t.get("writable", True)), _t["queueLen"],
                            missing=bool(_t.get("missingTranscript")))
                        thread_group_apply(_t)
                    return self._json(_out)
                if (qs.get("engine") or [""])[0] == "claude":
                    if CLAUDE is None or CLAUDE.status == "no-cli":
                        return self._json({"data": [], "engine": "claude",
                                           "claudeStatus": (CLAUDE.status if CLAUDE is not None else "off")})
                    _out = CLAUDE.list_threads(
                        limit=int((qs.get("limit") or ["60"])[0]),
                        q=(qs.get("q") or [""])[0],
                        cwd=(qs.get("cwd") or [""])[0],
                        archived=(qs.get("archived") or ["0"])[0] == "1")
                    for _t in _out.get("data") or []:
                        _t["queueLen"] = len(CLAUDE.queues.get(_t.get("id")) or [])
                        thread_group_apply(_t)
                    return self._json(_out)
                try:
                    limit = int((qs.get("limit") or ["60"])[0])
                except (TypeError, ValueError):
                    limit = 60
                limit = 0 if limit == 0 else max(1, min(limit, 500))
                archived = (qs.get("archived") or ["0"])[0] == "1"
                show_all = (qs.get("all") or ["0"])[0] in ("1", "true", "yes")
                want_q = ((qs.get("q") or [""])[0] or "").strip().lower()
                want_cwd = ((qs.get("cwd") or [""])[0] or "").strip()
                return self._json(threads_snapshot_payload(
                    limit=limit, archived=archived, show_all=show_all,
                    want_q=want_q, want_cwd=want_cwd))
            if (path.startswith("/api/threads/") and path.count("/") == 4
                    and path.endswith("/tail")):
                tid = path.split("/")[3]
                item = _index_thread(tid) or {}
                path_file = str(item.get("rolloutPath") or "")
                _h_before_raw = (qs.get("before") or [""])[0]
                try:
                    _h_before = int(_h_before_raw) if _h_before_raw else 0
                except (TypeError, ValueError):
                    _h_before = 0
                if _h_before < 0 or (not path_file and history_has_items(tid)):
                    _h_limit = max(1, min(int((qs.get("limit") or ["40"])[0] or 40), 120)) \
                        if str((qs.get("limit") or ["40"])[0]).lstrip("-").isdigit() else 40
                    _h_rows, _h_next, _h_more = history_page(
                        tid, before=(-_h_before if _h_before < 0 else None),
                        limit=_h_limit, max_bytes=HISTORY_PAGE_BYTES // 3)
                    _h_msgs = []
                    for _ord, _turn, _iid, _raw in _h_rows:
                        try:
                            _obj = json.loads(_raw)
                        except Exception:
                            _obj = {}
                        if not isinstance(_obj, dict):
                            _obj = {}
                        _role = "user" if _obj.get("type") == "userMessage" else "assistant"
                        if _role == "user":
                            _txt = "".join(str(c.get("text") or "")
                                           for c in (_obj.get("content") or [])
                                           if isinstance(c, dict))
                        else:
                            _txt = str(_obj.get("text") or "")
                        _h_msgs.append({"id": _iid, "role": _role, "text": _txt,
                                        "off": _ord, "item": _obj})
                    return self._json({"ok": True, "tid": tid, "msgs": _h_msgs,
                                       "nextEnd": _h_next, "hasMore": bool(_h_more),
                                       "sizeBytes": _thread_size_bytes(item, tid),
                                       "historyFallback": True})
                if not path_file or thread_index is None:
                    return self._err("server_error", "会话文件不存在或索引尚未就绪", 404)
                try:
                    limit = int((qs.get("limit") or ["40"])[0])
                except (TypeError, ValueError):
                    limit = 40
                limit = max(1, min(limit, 120))
                before = (qs.get("before") or [""])[0]
                try:
                    end = int(before) if before else None
                except (TypeError, ValueError):
                    end = None
                try:
                    # 与首屏尾部窗口同一正文上限：否则翻出来的旧消息被截到 1200 字，
                    # 历史看起来像"少了一大截"。
                    from history_timeline import read_timeline_window
                    win = read_timeline_window(path_file, end=end, limit=limit)
                    if win is None:
                        win = thread_index.read_messages_window(
                            path_file, end=end, limit=limit,
                            max_chars=getattr(thread_index, "MSG_TEXT_CHARS_TAIL", 20000))
                except Exception as exc:
                    return self._err("server_error", "读取会话失败：%s" % (exc,), 500)
                return self._json({"ok": True, "tid": tid, "msgs": win.get("msgs") or [],
                                   "nextEnd": win.get("nextEnd") or 0,
                                   "hasMore": bool(win.get("hasMore")),
                                   "sizeBytes": win.get("size") or 0})
            if path.startswith("/api/threads/") and path.count("/") == 3:
                tid = path.split("/")[3]
                light = (qs.get("light") or ["0"])[0] in ("1", "true", "yes")
                if WB is not None and ((qs.get("engine") or [""])[0] == "workbuddy" or WB.is_known(tid)):
                    t = WB.read_thread(tid, light=light)
                    thread_group_apply(t)
                    t["writable"] = not t.get("missingTranscript")
                    t["running"] = WB.is_running(tid) and t["writable"]
                    t["loaded"] = tid in WB.turns
                    t["queueLen"] = len(WB.queues.get(tid) or [])
                    t["runtime"] = wb_runtime_payload(tid, t["running"], t["writable"], t["queueLen"], bool(t.get("missingTranscript")))
                    _lu = WB.last_usage.get(tid)
                    if _lu:
                        t["usage"] = _lu
                    return self._json({"thread": t})
                if CLAUDE is not None and ((qs.get("engine") or [""])[0] == "claude" or CLAUDE.is_known(tid)):
                    t = claude_read_thread(tid, light=light)
                    thread_group_apply(t)
                    t["writable"] = True
                    t["running"] = claude_running(tid)
                    t["loaded"] = tid in CLAUDE.turns
                    t["queueLen"] = len(CLAUDE.queues.get(tid) or [])
                    _lu = CLAUDE.last_usage.get(tid)
                    if _lu:
                        t["usage"] = _lu
                    return self._json({"thread": t})
                # P1: 只读索引器快照，不在请求线程里探锁 / 读 jsonl。
                _cx_item = _index_thread(tid)
                _cx_w, _cxrun, _cx_lock = _index_lock_fields(_cx_item, tid)
                _cx_size = _thread_size_bytes(_cx_item, tid)
                _cx_full = (qs.get("full") or ["0"])[0] in ("1", "true", "yes")

                def _finish_history():
                    _h = _history_thread_payload(tid, _cx_item)
                    if _h is None:
                        return None
                    _h["writable"] = False
                    _h["lock"] = dict((_cx_item or {}).get("lock") or {})
                    _h["running"] = False
                    _h["queueLen"] = 0
                    _h["runtime"] = runtime_status_payload(
                        tid, False, False, 0, source="history", missing=True)
                    _h["status"] = {"type": "idle"}
                    thread_group_apply(_h)
                    return {"ok": True, "thread": _h, "partial": True,
                            "message": _h.get("partialNote")}

                # rollout 源文件被 Codex 清理/迁移，但历史库里还有完整正文：
                # 直接回读，绝不去打 app-server（它只会回 400 missing source rollout）。
                if _cx_item and _cx_item.get("missingTranscript") and history_has_items(tid):
                    _r = _finish_history()
                    if _r is not None:
                        return self._json(_r)

                def _finish_tail(_kind, _note=None):
                    _out = _tail_thread_payload(tid, _cx_item, kind=_kind, note=_note)
                    _out.update(codex_thread_settings(tid))
                    _out["writable"] = bool(_cx_w) and not AS.control.is_desktop(tid)
                    if not _out["writable"]:
                        _out["lock"] = _cx_lock
                    _out["running"] = bool(_cxrun)
                    try:
                        _out["queueLen"] = int(queue_counts().get(str(tid), 0))
                    except Exception:
                        _out["queueLen"] = 0
                    _out["runtime"] = runtime_status_payload(
                        tid, _out["running"], _out["writable"], _out["queueLen"],
                        source="tail",
                        missing=bool(_out.get("historyFallback") or _out.get("missingTranscript")))
                    _out["status"] = {"type": "active" if _out["running"] else "idle"}
                    thread_group_apply(_out)
                    return _out

                # 大会话：永远不叫 app-server 全量解析（344MB 实测会卡 34 秒，
                # 还会把同一个 app-server 上的其它 RPC 一起堵死）。
                # 即使调用方传了 full=1，也只给尾部窗口；历史由 /tail 分页补。
                # Passive reads never materialize a native writer subscription.
                out = _finish_tail("readonly_history")
                return self._json({"thread": out, "partial": True})
            if (path.startswith("/api/threads/") and path.count("/") == 4
                    and path.endswith("/queue")):
                return self._json(queue_snapshot(path.split("/")[3],
                                                 (qs.get("engine") or [""])[0]))
            if (path.startswith("/api/threads/") and path.count("/") == 4
                    and path.endswith("/state")):
                # 轻量真值轮询(跨设备同步): 只回 running/writable/queueLen, 不搬回合正文
                tid = path.split("/")[3]
                if WB is not None and ((qs.get("engine") or [""])[0] == "workbuddy"
                                       or WB.is_known(tid)):
                    _dbm = wb_desktop_db_meta().get(tid) or {}
                    _miss = bool(_dbm and tid not in WB.pending and tid not in WB.turns
                                 and tid not in WB.hidden and WB.find_session(tid) is None)
                    _wb_run = bool(WB.is_running(tid) and not _miss)
                    _wb_q = len(WB.queues.get(tid) or [])
                    _payload = {"ok": True, "tid": tid, "engine": "workbuddy",
                                "running": _wb_run,
                                "writable": not _miss,
                                "missingTranscript": _miss,
                                "missingReason": (WB_MISSING_TRANSCRIPT if _miss else ""),
                                "queueLen": _wb_q}
                    _payload["runtime"] = wb_runtime_payload(
                        tid, _wb_run, not _miss, _wb_q, missing=_miss)
                    _payload["status"] = {"type": "active" if _wb_run else "idle"}
                    return self._json(_payload)
                if CLAUDE is not None and ((qs.get("engine") or [""])[0] == "claude"
                                           or CLAUDE.is_known(tid)):
                    _cl_run = bool(claude_running(tid))
                    _cl_q = len(CLAUDE.queues.get(tid) or [])
                    _payload = {"ok": True, "tid": tid, "engine": "claude",
                                "running": _cl_run, "writable": True,
                                "queueLen": _cl_q}
                    _payload["runtime"] = runtime_status_payload(
                        tid, _cl_run, True, _cl_q, source="claude")
                    _payload["status"] = {"type": "active" if _cl_run else "idle"}
                    return self._json(_payload)
                # /state 是手机端高频轮询：队列长度直接读共享 sqlite，不走 app-server RPC。
                # 否则电脑端在读一个 1.6GB 的大会话时，轮询会跟着排队 20 秒。
                try:
                    _ql = int(queue_counts().get(str(tid), 0))
                except Exception:
                    _ql = 0
                # P1: /state 是手机端每秒级轮询，只读索引器快照。
                _st_item = _index_thread(tid)
                _w, _run, _lock = _index_lock_fields(_st_item, tid)
                _st_hist = bool((_st_item or {}).get("missingTranscript")) and history_has_items(tid)
                if _st_hist:
                    _w = False
                    _run = False
                _payload = {"ok": True, "tid": tid, "engine": "codex",
                            "running": bool(_run),
                            "writable": bool(_w), "queueLen": _ql}
                _payload["runtime"] = runtime_status_payload(
                    tid, _run, _w, _ql, source="state", missing=_st_hist)
                _payload["status"] = {"type": "active" if _run else "idle"}
                _payload.update(codex_thread_settings(tid))
                if not _w:
                    _payload["lock"] = _lock
                if _st_hist:
                    _payload["historyFallback"] = True
                    _payload["missingTranscript"] = False
                return self._json(_payload)
            if path == "/api/models":
                if (qs.get("engine") or [""])[0] == "workbuddy":
                    wb_ensure_cli()
                    return self._json(wb_models_payload((qs.get("thread") or [""])[0]))
                if (qs.get("engine") or [""])[0] == "claude":
                    return self._json(claude_models_payload())
                return self._json(_models_payload(force=bool(qs.get("fresh"))))
            if path == "/api/providers":
                _eng = (qs.get("engine") or ["codex"])[0]
                return self._json(_providers_payload(engine=_eng,
                                                      force=bool(qs.get("fresh"))))
            if path == "/api/fs/roots":
                return self._json({"roots": fs_roots()})
            if path == "/api/projects":
                catalog=desktop_project_catalog().data()
                return self._json({"projects": copy.deepcopy(catalog['projects']),
                                   "catalogRevision":catalog.get('revision')})
            if path == "/api/fs/list":
                return self._json(fs_list((qs.get("path") or [""])[0]))
            if path == "/api/fs/read":
                mx = int((qs.get("max") or ["200000"])[0])
                return self._json(fs_read((qs.get("path") or [""])[0], mx))
            if path == "/api/fs/stat":
                return self._json(fs_stat((qs.get("path") or [""])[0]))
            if path == "/api/fs/follow":
                return self._json(fs_follow((qs.get("path") or [""])[0], (qs.get("ino") or [""])[0]))
            if path == "/api/fs/raw":
                return self._fs_raw((qs.get("path") or [""])[0])
            if path == "/api/approvals":
                with AS.plock:
                    pend = [{"id": k, "method": v["method"], "params": v["params"]} for k, v in AS.srv_reqs.items()]
                return self._json({"pending": pend})
            if path == "/api/events":
                return self._sse()
            if path == "/api/events-probe":
                return self._sse_probe()
            if path == "/api/events-probe2":
                return self._sse_probe2()
            if path == "/api/ws-test":
                return self._ws_test()
            if path == "/api/ws":
                rpc_version, rpc_error = negotiate_ws_version((qs.get('rpc') or [None])[0])
                if rpc_error:
                    return self._json({"error": rpc_error}, 426)
                return self._ws(rpc_version)
        except ApiError as exc:
            if self._client_disconnected(exc) or self._client_disconnected(getattr(exc, "__cause__", None)):
                self.close_connection = True
                return
            return self._api_error(exc)
        except Exception as exc:
            if self._client_disconnected(exc):
                self.close_connection = True
                return
            log("api_500 %s: %r\n%s" % (path, exc, traceback.format_exc()))
            return self._json({"error": str(exc)}, 500)
        return self._json({"error": "not found"}, 404)

    def _api_error(self, exc):
        err = exc.err
        msg = err.get("message") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        if code == "not_found":
            status = 404
        elif code in ("busy", "queue_full"):
            status = 409
        elif code == "read_only":
            status = 403
        elif code in ("no_cli", "appserver_down"):
            status = 503
        elif code in ("timeout", "no_response"):
            status = 502
        elif isinstance(msg, str) and "is archived" in msg:
            err = {"code": "thread_archived",
                   "message": "该会话已归档：请先取消归档再发送。"}
            code, status = "thread_archived", 409
        elif isinstance(msg, str) and "already has an active writer" in msg:
            err = {"code": "thread_in_use",
                   "message": "该会话正被电脑端使用中（写锁占用），暂时不能从手机发消息。"
                              "在电脑上退出或切走该会话后即可；纯查看不受影响。"}
            code, status = "thread_in_use", 409
        elif code:
            # 其余带 code 的是请求/会话层面的问题, 不是上游故障 -> 400, 不再一律 502
            status = 400
        else:
            status = 502
        try:
            log("api_error %s -> %d" % (code or str(err)[:80], status))
        except Exception:
            pass
        return self._json({"error": err}, status)

    def _fs_raw(self, p: str):
        real = fs_allowed(p)
        if real is None:
            return self._json({"error": "fs_denied"}, 403)
        ext = os.path.splitext(real)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                 ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        try:
            st = os.stat(real)
        except OSError as exc:
            return self._json({"error": str(exc)}, 500)
        etag = '"%d-%d"' % (int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        try:
            data = open(real, "rb").read()
        except OSError as exc:
            return self._json({"error": str(exc)}, 500)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _serve_ui(self):
        p = UI_DIR / "index.html"
        if not p.exists():
            return _send_html_page(self, UI_MISSING_HTML, 500)
        try:
            body = p.read_bytes()
        except OSError as exc:
            log("ui_read_error: %r" % (exc,))
            return _send_html_page(self, UI_MISSING_HTML, 500)
        enc = None
        if len(body) >= 1024 and self._accepts_gzip():
            try:
                gz = gzip.compress(body, 5)
                if len(gz) < len(body) - 64:
                    body, enc = gz, "gzip"
            except Exception:
                pass
        # ETag: 页面 101KB(gzip 后), 隧道下每次刷新都整份重下很浪费。
        # no-cache 保持不变(必须回源确认), 但带上 ETag 后未改动时只回 304 空体。
        etag = None
        try:
            st = p.stat()
            etag = 'W/"%s-%x"' % (int(st.st_mtime), st.st_size)
        except OSError:
            etag = None
        if etag and (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if enc:
            self.send_header("Content-Encoding", enc)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", etag)
        _ck = getattr(self, "_auth_cookie_pending", None)
        if _ck:
            # 一年有效：用 ?k= 进来一次之后，同域刷新/书签都不必再带 token
            self.send_header("Set-Cookie", "arc=%s; Path=/; Max-Age=31536000; SameSite=Lax" % _ck)
            self._auth_cookie_pending = None
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _sse(self):
        self.close_connection = True  # 长连接流: 结束后不要复用
        sid, q = HUB.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload: bytes):
            self.wfile.write(("%X\r\n" % len(payload)).encode("ascii"))
            self.wfile.write(payload)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        try:
            hello = json.dumps({"kind": "gateway", "status": AS.status}, ensure_ascii=False)
            chunk(("event: msg\ndata: %s\n\n" % hello).encode("utf-8"))
            while True:
                try:
                    payload = q.get(timeout=SSE_HEARTBEAT)
                except queue.Empty:
                    chunk(b": ping\n\n")
                    continue
                chunk(("event: msg\ndata: %s\n\n" % payload).encode("utf-8"))
        except Exception:
            pass
        finally:
            HUB.unsubscribe(sid)
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass


    def _sse_probe(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(b):
            self.wfile.write(("%X\r\n" % len(b)).encode("ascii"))
            self.wfile.write(b)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        try:
            chunk(b": probe-start\n\n")
            for i in range(12):
                chunk(b": " + b"x" * 5000 + b"\n\n")
                log(f"sse-probe chunk {i+1} sent")
                time.sleep(2)
        except Exception as exc:
            log(f"sse-probe aborted: {exc}")


    def _sse_probe2(self):
        import time as _t
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Finishes", "quickly")
        self.end_headers()

        def chunk(b):
            self.wfile.write(("%X\r\n" % len(b)).encode("ascii"))
            self.wfile.write(b)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        try:
            chunk(b": first\n\n")
            _t.sleep(1)
            chunk(b": second\n\n")
            _t.sleep(1)
            chunk(b": third\n\n")
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass


    # ---------- WebSocket 基础 ----------
    def _ws_accept(self):
        key = self.headers.get("Sec-WebSocket-Key") or ""
        digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        accept = base64.b64encode(digest).decode("ascii")
        resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: " + accept + "\r\n\r\n")
        self.wfile.write(resp.encode("ascii"))
        self.wfile.flush()
        self.close_connection = True

    def _ws_frame(self, payload: bytes, opcode: int = 1):
        b1 = 0x80 | opcode
        n = len(payload)
        if n < 126:
            hdr = bytes([b1, n])
        elif n < 65536:
            hdr = bytes([b1, 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([b1, 127]) + struct.pack(">Q", n)
        self.wfile.write(hdr + payload)
        self.wfile.flush()


    def _ws(self, rpc_version=WS_RPC_VERSION):
        """全事件 WebSocket 通道(远程主链路, CF 下不被缓冲)。"""
        self._ws_accept()
        try:
            _qs = parse_qs(urlparse(self.path).query)
            _last = int((_qs.get("last") or ["0"])[0] or 0)
        except Exception:
            _qs = {}
            _last = 0
        sid, q, backlog, gap, oldest, head = HUB.subscribe_since(_last)
        stop = threading.Event()

        def client_reader():
            # 处理 close/ping + 文本帧(set_watch/watch/unwatch → rollout 运行细节订阅)
            watched = set()
            try:
                while not stop.is_set():
                    b1 = self.rfile.read(1)
                    if not b1:
                        break
                    b2 = self.rfile.read(1)
                    if not b2:
                        break
                    op = b1[0] & 0x0F
                    ln = b2[0] & 0x7F
                    if ln == 126:
                        ln = struct.unpack(">H", self.rfile.read(2))[0]
                    elif ln == 127:
                        ln = struct.unpack(">Q", self.rfile.read(8))[0]
                    if ln > RPC_MAX_FRAME_BYTES:
                        q.put(json.dumps({"kind": "rpc.error", "version": rpc_version,
                                          "error": {"code": "frame_too_large"}}, ensure_ascii=False))
                        log('ws client frame rejected: oversized (%d bytes)' % ln)
                        break
                    mask = None
                    if b2[0] & 0x80:
                        mask = self.rfile.read(4)  # mask key
                    payload = self.rfile.read(ln) if ln else b""
                    if mask and payload:
                        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                    if op == 8:
                        break
                    if op == 1 and payload and TAILER is not None:
                        try:
                            msg = json.loads(payload.decode("utf-8"))
                        except Exception:
                            continue
                        role = self._auth_level(_qs)
                        request, error = authorize_ws_message(msg, role, authenticated=role is not None)
                        request_id = (request or {}).get('id')
                        if error:
                            q.put(json.dumps({"kind": "rpc.error", "version": rpc_version,
                                              "id": error.get('id'), "error": error}, ensure_ascii=False))
                            if error.get('code') == 'unauthorized':
                                log('ws client message rejected: credential no longer valid')
                                break
                            continue
                        method = request['method']
                        tids = request['threadIds']
                        if method == 'thread.watch':
                            for x in tids:
                                if x not in watched:
                                    watched.add(x)
                                    TAILER.watch_thread(x)
                        elif method == 'thread.unwatch':
                            for x in tids:
                                if x in watched:
                                    watched.discard(x)
                                    TAILER.unwatch_thread(x)
                        elif method == 'thread.setWatches':
                            new = set(tids)
                            for x in watched - new:
                                TAILER.unwatch_thread(x)
                            for x in new - watched:
                                TAILER.watch_thread(x)
                            watched = new
                        if request_id is not None:
                            q.put(json.dumps({"kind": "rpc.result", "version": rpc_version,
                                              "id": request_id, "result": {"ok": True}}, ensure_ascii=False))
            except Exception:
                pass
            for x in watched:
                if TAILER is not None:
                    TAILER.unwatch_thread(x)
            stop.set()

        threading.Thread(target=client_reader, daemon=True).start()
        try:
            hello = json.dumps({"kind": "gateway", "status": AS.status}, ensure_ascii=False)
            self._ws_frame(hello.encode("utf-8"))
            # Replay what the client missed while the tunnel was down, then a
            # resume marker carrying the seq the replay ended at. Live frames
            # continue from head+1, so the client keeps one continuous lastSeq.
            if _last:
                log("ws resume last=%d head=%d replayed=%d gap=%s" % (_last, head, len(backlog), gap))
            for _pl in backlog:
                self._ws_frame(_pl.encode("utf-8"))
            _resume = json.dumps({"kind": "resume", "epoch":_RUNTIME_EPOCH, "replayed": len(backlog),
                                  "gap": bool(gap or HUB.backlog_dropped), "oldest": oldest, "seq": head},
                                 ensure_ascii=False)
            self._ws_frame(_resume.encode("utf-8"))
            while not stop.is_set():
                try:
                    payload = q.get(timeout=20)
                except queue.Empty:
                    self._ws_frame(b'{"kind":"hb"}')
                    continue
                # 32ms 合并窗口: 逐 token delta 先攒一小批, 同 item 连续 delta 合并。
                # 首条事件最多晚 32ms, 换来的是手机端事件数量下降一个数量级。
                batch = [payload]
                total = len(payload)
                deadline = time.time() + 0.032
                while len(batch) < 240 and total < 262144:
                    remain = deadline - time.time()
                    if remain <= 0:
                        break
                    try:
                        nxt = q.get(timeout=remain)
                    except queue.Empty:
                        break
                    batch.append(nxt)
                    total += len(nxt)
                for merged in _ws_merge_payloads(batch):
                    self._ws_frame(merged.encode("utf-8"))
        except Exception:
            pass
        finally:
            HUB.unsubscribe(sid)
            stop.set()

    def _ws_test(self):
        self._ws_accept()
        try:
            for i in range(10):
                self._ws_frame(json.dumps({"kind": "tick", "i": i, "t": time.time()}).encode("utf-8"))
                log(f"ws-test tick {i} sent")
                time.sleep(2)
        except Exception as exc:
            log(f"ws-test aborted: {exc}")

    # ---------- POST ----------
    def _submit_turn(self, tid, body):
        def _receipt(obj, code=200):
            return obj, code
        text = (body.get("text") or "").strip()
        imgs, img_failed = _save_images(body.get("images") or [])
        img_warn = {"images": img_failed} if img_failed else None
        if not text and not imgs:
            msg = ("有 %d 张图片没能保存（超 8 张 / 空图 / 超 20MB 或者格式不支持）"
                   % img_failed) if img_failed else "empty text"
            return _receipt({"error": msg}, 400)
        cid = body.get("clientUserMessageId") or ("ar-" + uuid.uuid4().hex)
        inp = []
        if text:
            inp.append({"type": "text", "text": text})
        inp.extend(imgs)
        use_wb = (WB is not None and WB.is_known(tid)) or body.get("engine") == "workbuddy"
        if use_wb:
            wb_ensure_cli()
            if WB is None or WB.status == "no-cli":
                return _receipt({"error": {"code": "no_cli",
                                             "message": wb_cli_error_message()}}, 503)
            _okm, _whym = wb_model_for_thread_ok(tid, body.get("model"))
            if not _okm:
                return _receipt({"error": {"code": "model_not_found",
                                             "message": _whym}}, 400)
            wb_imgs = [str(i.get("path")) for i in imgs if isinstance(i, dict) and i.get("path")]
            res = WB.send_turn(tid, text, wb_imgs, body.get("model"),
                               body.get("effort"), cid)
            mode = "queued" if (isinstance(res, dict) and res.get("queued")) else "started"
            log("turns %s workbuddy -> %s%s" % (tid[:8], mode,
                (" pos=%d" % res.get("position")) if mode == "queued" else ""))
            return _receipt({"mode": mode, "engine": "workbuddy", "warnings": img_warn,
                               "clientUserMessageId": cid, "queueId": res.get("queueId"),
                               "result": res})
        use_claude = (CLAUDE is not None and CLAUDE.is_known(tid)) or body.get("engine") == "claude"
        if use_claude:
            if CLAUDE is None or CLAUDE.status == "no-cli":
                return _receipt({"error": {"code": "no_cli", "message": "本机未找到 claude.exe"}}, 503)
            _okm, _whym = engine_model_ok("claude", body.get("model"))
            if not _okm:
                return _receipt({"error": {"code": "model_not_found",
                                             "message": _whym}}, 400)
            claude_imgs = [str(i.get("path")) for i in imgs if isinstance(i, dict) and i.get("path")]
            res = CLAUDE.send_turn(tid, text, claude_imgs, body.get("model"),
                                   body.get("effort"), cid)
            mode = "queued" if (isinstance(res, dict) and res.get("queued")) else "started"
            log("turns %s claude -> %s%s" % (tid[:8], mode,
                (" pos=%d" % res.get("position")) if mode == "queued" else ""))
            return _receipt({"mode": mode, "engine": "claude", "warnings": img_warn,
                               "clientUserMessageId": cid, "queueId": res.get("queueId"),
                               "result": res})
        _okm, _whym = engine_model_ok("codex", body.get("model"))
        if not _okm:
            return _receipt({"error": {"code": "model_not_found",
                                         "message": _whym}}, 400)
        if AS.control.is_desktop(tid):
            return _receipt({"error": {"code": "takeover_required",
                "message": "会话已交给电脑端，请先接管到手机再发送。"}}, 409)
        # All Codex sends go through the native durable queue; idle sends must
        # not bypass it with turn/start, or a gateway timeout leaves no outbox.
        run = thread_running(tid)
        mdl_q = body.get("model")
        eff_q = body.get("effort")
        # 电脑端正持写锁时, resume/settings 必定失败, 还往 app-server 刷一串
        # ERROR("active writer"), 用户看到的就是"后端错误"。先探锁: 占用就直接跳过,
        # 把真相写进响应, 让 UI 能说"设置随下一条生效"而不是弹红字。
        _pc_locked = False
        try:
            _pc_locked = not thread_writable(tid, fresh=True)
        except Exception:
            _pc_locked = False
        _q_skip = False
        _q_deferred = False
        if mdl_q or eff_q:
            if _pc_locked:
                _q_skip = True
                log(f"queued settings deferred for {tid[:8]}: pc holds writer lock")
            elif run:
                try:
                    _pv1 = model_provider_of(mdl_q) if mdl_q else AS.provider_hint(tid)
                    queue_intent_store().put(tid, cid, mdl_q, _pv1, eff_q)
                    _q_deferred = True
                    log("queued per-message settings persisted for %s cid=%s" %
                        (tid[:8], cid[:8]))
                except Exception as exc:
                    _q_skip = True
                    log("queued settings intent unavailable for %s: %s" %
                        (tid[:8], type(exc).__name__))
            else:
                try:
                    _pv1 = model_provider_of(mdl_q) if mdl_q else ""
                    if mdl_q and _pv1 and _pv1 != AS.provider_hint(tid):
                        AS.switch_thread_model(tid, mdl_q, _pv1, eff_q or "")
                    else:
                        AS.ensure_resumed(tid)
                        sp = {"threadId": tid}
                        if mdl_q:
                            sp["model"] = mdl_q
                        if eff_q:
                            sp["effort"] = eff_q
                        AS.request("thread/settings/update", sp, timeout=30)
                        if mdl_q:
                            AS.thread_provider[tid] = _pv1 or AS.provider_hint(tid)
                            AS.thread_model[tid] = mdl_q
                        if eff_q:
                            AS.thread_effort[tid] = eff_q
                    AS.last_activity[tid] = time.time()
                except ApiError as exc:
                    _q_skip = True
                    log(f"queued settings skipped for {tid[:8]}: {exc.err}")
        from codex_dispatch import enqueue_once
        res = enqueue_once(AS, tid, text, cid, extra_input=imgs)
        AS.last_activity[tid] = time.time()
        if res.get("mode") in ("queued", "accepted"):
            AS.queued_locally.add(tid)
        if res.get("mode") == "queued":
            log("turns %s codex -> native queue pos=%d len=%d" %
                (tid[:8], res.get("position", 0), res.get("queueLen", 0)))
        else:
            log("turns %s codex -> native queue accepted; start evidence pending" % tid[:8])
            confirmed = reconcile_submission(tid, cid)
            if confirmed:
                res = confirmed[0]
                if res.get("mode") not in ("queued", "accepted"):
                    AS.queued_locally.discard(tid)
        return _receipt({**res, "warnings": img_warn,
                         "pcLocked": bool(_pc_locked),
                         "settingsSkipped": bool(_q_skip),
                         "settingsDeferred": bool(_q_deferred)})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        self._req_t0 = time.time()
        try:
            _te = (self.headers.get("Transfer-Encoding") or "").lower()
        except Exception:
            _te = ""
        self._req_chunked = "chunked" in _te
        try:
            self._req_body_left = (1 if self._req_chunked
                                   else int(self.headers.get("Content-Length") or 0))
        except Exception:
            self._req_body_left = 0
        self._req_body_read = False
        self._req_body_error = ""
        try:
            log("req> POST %s" % path)
        except Exception:
            pass
        qs = parse_qs(u.query)
        if GUARD is not None and GUARD.gate(self, path, qs, "POST"):
            return
        lvl = self._auth_level(qs)
        auth_error = authorize_http('POST', path, lvl)
        if auth_error and auth_error['status'] == 401:
            return self._json({"error": "unauthorized"}, 401)
        if auth_error and auth_error['code'] == 'read_only':
            return self._json({"error": {
                "code": "read_only",
                "message": "这是只读观赏链接：只能看，不能操作。要操作请用全权链接。",
            }}, 403)
        if auth_error:
            return self._json({"error": auth_error}, auth_error['status'])
        if self._trial_gate(path):
            return
        body = self._read_body()
        if getattr(self, "_req_body_error", ""):
            return self._json({"ok": False, "error_kind": "bad_request",
                               "message": "请求体不完整或格式错误，连接已关闭，请重试一次"}, 400)
        if GUARD is not None:
            GUARD.note_body(path, body)
        if path == "/api/trial":
            if TRIAL is None:
                return self._json({"ok": False, "error": "试用模块未启用"}, 400)
            code = str((body or {}).get("code") or "").strip()
            if not code:
                return self._json({"ok": False, "error": "续期码为空"}, 400)
            res = TRIAL.redeem(code)
            try:
                log("trial redeem ok=%s remain=%s" % (res.get("ok"), res.get("remain")))
            except Exception:
                pass
            return self._json(res, 200 if res.get("ok") else 400)
        try:
            parts = [p for p in path.split("/") if p]
            if path == '/api/projects/create':
                from desktop_project_commands import create_native_project
                try:
                    project=create_native_project(CODEX_HOME,AS.request,body.get('path') or '',body.get('name'))
                except ValueError as exc:
                    return self._json({'error':{'code':'project_invalid','message':str(exc)}},400)
                except RuntimeError as exc:
                    return self._json({'error':{'code':'project_unconfirmed','message':str(exc)}},409)
                desktop_project_catalog().invalidate()
                HUB.broadcast({'kind':'notification','method':'project/changed','params':{'project':project}})
                return self._json({'ok':True,'project':project})
            if path == '/api/workflows':
                try:task=workflow_create(body)
                except (ValueError,TypeError,OverflowError) as exc:
                    conflict=str(exc)=='workflow_request_conflict'
                    return self._json({'error':{'code':'workflow_request_conflict' if conflict else 'workflow_invalid',
                                               'message':str(exc)}},409 if conflict else 400)
                return self._json({'ok':True,'task':task},201)
            if len(parts)==4 and parts[:2]==['api','workflows']:
                task=workflow_store().get(parts[2])
                if task is None:return self._json({'error':{'message':'任务不存在'}},404)
                try:
                    if parts[3]=='cancel':task=workflow_store().cancel(task['id'])
                    elif parts[3]=='resume':task=workflow_store().resume(task['id'])
                    elif parts[3]=='reconcile':task=workflow_store().retry_reconciliation(task['id'])
                    elif parts[3]=='complete':
                        if task['state'] not in ('accepted','queued','running','reconciling','completed') and not (
                                task['state']=='needs_review' and task.get('outcomeUnknown')):
                            raise ValueError('只有正在执行或结果未知的任务可以人工验收完成')
                        task=workflow_store().finish(task['id'],'completed','由用户验收完成')
                    else:return self._json({'error':{'message':'未知任务操作'}},404)
                except ValueError as exc:
                    return self._json({'error':{'code':'workflow_state_conflict','message':str(exc)}},409)
                return self._json({'ok':True,'task':task})
            # POST /api/threads/<id>/turns
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "threads" and parts[3] == "turns":
                tid = parts[2]
                try:
                    receipt, status = submission_store().run(
                        tid, body, lambda: self._submit_turn(tid, body), reconcile_submission)
                except Exception:
                    cid = str(body.get('clientUserMessageId') or '')
                    pending = submission_store().lookup(tid, cid) if cid else {}
                    if pending.get('found') and pending.get('state') == 'unknown':
                        receipt, status = submission_store().resolve(tid, cid, reconcile_submission)
                    else:
                        raise
                return self._json(receipt, status)
            # POST /api/threads/<id>/queue/<qid>/delete | /top   (排队项操作)
            if (len(parts) == 6 and parts[0] == "api" and parts[1] == "threads"
                    and parts[3] == "queue"):
                _tid, _qid, _act = parts[2], parts[4], parts[5]
                if _act == "delete":
                    return self._json(queue_delete_item(_tid, _qid))
                if _act == "top":
                    return self._json(queue_top_item(_tid, _qid))
                if _act == "jump":
                    return self._json(queue_jump_item(_tid, _qid))
                return self._json({"error": "unknown queue action: %s" % _act}, 404)
            # POST /api/threads/<id>/queue/send | /clear
            if (len(parts) == 5 and parts[0] == "api" and parts[1] == "threads"
                    and parts[3] == "queue"):
                _tid, _act = parts[2], parts[4]
                if _act == "send":
                    return self._json(queue_send_now(_tid, body.get("queuedSubmissionId")))
                if _act == "clear":
                    return self._json(queue_clear_all(_tid))
                return self._json({"error": "unknown queue action: %s" % _act}, 404)
            # POST /api/threads/<id>/interrupt
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "threads" and parts[3] == "interrupt":
                tid = parts[2]
                clear_q = bool(body.get("clearQueue"))
                if WB is not None and WB.is_known(tid):
                    r = WB.interrupt(tid, clear_q)
                    return self._json(dict({"ok": True}, **r))
                if CLAUDE is not None and CLAUDE.is_known(tid):
                    r = CLAUDE.interrupt(tid, clear_q)
                    return self._json(dict({"ok": True}, **r))
                # 真值优先: 写锁在电脑端 = 这一回合根本不是手机侧在跑 → 如实说停不了。
                # 不再靠 thread/turns/list 猜(那正是历史上 26 次 "no running turn" 409 的来源)。
                mine = tid in AS.resumed
                if not mine and not thread_writable(tid, fresh=True):
                    return self._json({
                        "ok": True, "stopped": False, "reason": "pc_holds_lock",
                        "engine": "codex",
                        "message": "这一回合是电脑端在跑（写锁被电脑端占着），手机侧停不下来。"
                                   "可以继续排队；等电脑端空闲或关掉该会话，队列会自动发出。",
                        "queueLen": len(queue_snapshot(tid, "codex").get("data") or []),
                    })
                turn_id = body.get("turnId") or AS.current_turn.get(tid)
                if not turn_id and mine:
                    try:
                        tl = AS.request("thread/turns/list",
                                        {"threadId": tid, "limit": 5, "sortDirection": "desc"},
                                        timeout=30)
                        for t in tl.get("data") or []:
                            st = (t.get("status") or {}).get("type") if isinstance(t.get("status"), dict) else t.get("status")
                            if st in ("inProgress", "running", "in_progress"):
                                turn_id = t.get("id")
                                break
                    except ApiError as exc:
                        log("interrupt scan %s: %s" % (tid[:8], _err_text(exc)))
                if not turn_id:
                    return self._json({"ok": True, "stopped": False, "reason": "no_running_turn",
                                       "engine": "codex", "turnId": None})
                AS.request("turn/interrupt", {"threadId": tid, "turnId": turn_id}, timeout=30)
                # 主动摘 running: app-server 的 turn/completed 可能晚到, 甚至(中断路径)
                # 没有 task_complete 落盘 -> tail_running 会一直为真, 队列永远推不动。
                AS.current_turn.pop(tid, None)
                _run_cache.pop(tid, None)
                AS.forced_stop[tid] = time.time()
                HUB.broadcast({"kind": "notification", "method": "turn/completed",
                               "params": {"threadId": tid,
                                          "turn": {"id": turn_id, "status": "aborted",
                                                   "engine": "codex"}}})
                cleared = 0
                if clear_q:
                    cleared = int(queue_clear_all(tid).get("cleared") or 0)
                _kept = 0 if clear_q else len(queue_snapshot(tid, "codex").get("data") or [])
                log("interrupt %s turn=%s clearQ=%s" % (tid[:8], str(turn_id)[:13], clear_q))
                if PUMP is not None:            # 停掉后立刻续跑下一条, 不等节拍
                    try:
                        PUMP.kick(tid)
                    except Exception:
                        pass
                return self._json({"ok": True, "stopped": True, "turnId": turn_id,
                                   "engine": "codex", "cleared": cleared,
                                   "queueKept": _kept, "queueLen": _kept})
            # POST /api/threads/new
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "threads" and parts[2] == "new":
                if body.get('cleanupOnComplete') and body.get('engine', 'codex') != 'codex':
                    return self._json({'error': {'code': 'unsupported', 'message': '自动测试归档目前仅支持 Codex'}}, 400)
                if body.get("engine") == "workbuddy":
                    wb_ensure_cli()
                    if WB is None or WB.status == "no-cli":
                        return self._json({"error": {"code": "no_cli",
                                                     "message": wb_cli_error_message()}}, 503)
                    _okm, _whym = engine_model_ok("workbuddy", body.get("model"))
                    if not _okm:
                        return self._json({"error": {"code": "model_not_found",
                                                     "message": _whym}}, 400)
                    return self._json({"thread": WB.new_thread(body.get("cwd"), body.get("model"), body.get("effort"))})
                if body.get("engine") == "claude":
                    if CLAUDE is None or CLAUDE.status == "no-cli":
                        return self._json({"error": {"code": "no_cli", "message": "本机未找到 claude.exe"}}, 503)
                    _okm, _whym = engine_model_ok("claude", body.get("model"))
                    if not _okm:
                        return self._json({"error": {"code": "model_not_found",
                                                     "message": _whym}}, 400)
                    return self._json({"thread": CLAUDE.new_thread(body.get("cwd"), body.get("model"), body.get("effort"))})
                _okm, _whym = engine_model_ok("codex", body.get("model"))
                if not _okm:
                    return self._json({"error": {"code": "model_not_found",
                                                 "message": _whym}}, 400)
                params = {}
                default_cfg = _local_cfg()
                if not body.get("model") and default_cfg["model"]:
                    body["model"] = default_cfg["model"]
                if not body.get("effort") and default_cfg["effort"]:
                    body["effort"] = default_cfg["effort"]
                # 空白会话不绑文件夹：三个引擎统一落到中性空目录, 不碰用户 Home。
                params["cwd"] = body.get("cwd") or blank_workspace()
                params["sandbox"] = NEXUS_SANDBOX_MODE
                params["approvalPolicy"] = NEXUS_APPROVAL_POLICY
                if body.get('projectId'):
                    if body['projectId'] not in desktop_project_catalog().data()['byId']:
                        return self._json({'error': {'code': 'project_not_found', 'message': 'Codex 项目不存在，请刷新后重试'}}, 400)
                    params['projectId'] = body['projectId']
                if body.get("model"):
                    params["model"] = body["model"]
                    _pv = model_provider_of(body["model"])
                    if _pv:
                        params["modelProvider"] = _pv
                if think_zh_on():
                    params["developerInstructions"] = NEXUS_THINK_ZH
                res = AS.request("thread/start", params, timeout=60)
                t = res.get("thread") or {}
                if t.get("id"):
                    AS.resumed.add(t["id"])
                    AS.nexus_created.add(t["id"])
                    AS.thread_provider[t["id"]] = (model_provider_of(body.get("model") or "")
                                                   or (_local_cfg().get("provider") or ""))
                    if body.get("model"):
                        AS.thread_model[t["id"]] = str(body["model"])
                    if body.get("effort"):
                        AS.thread_effort[t["id"]] = str(body["effort"])
                    if "developerInstructions" in params:
                        think_zh_mark(t["id"])
                    AS.last_activity[t["id"]] = time.time()
                    AS._ensure_nexus_policy(t["id"])
                    if body.get("model") or body.get("effort"):
                        sp0 = {"threadId": t["id"]}
                        if body.get("model"):
                            sp0["model"] = body["model"]
                            _pv2 = model_provider_of(body["model"])
                            if _pv2:
                                sp0["modelProvider"] = _pv2
                        if body.get("effort"):
                            sp0["effort"] = body["effort"]
                        try:
                            AS.request("thread/settings/update", sp0, timeout=30)
                            if body.get("model"):
                                AS.thread_model[t["id"]] = str(body["model"])
                            if body.get("effort"):
                                AS.thread_effort[t["id"]] = str(body["effort"])
                        except ApiError as exc:
                            log(f"new-thread settings skipped: {exc.err}")
                if body.get('cleanupOnComplete') is True and t.get('id'):
                    from test_lifecycle import TestLifecycle
                    TestLifecycle(APP_DIR / '_run' / 'test-lifecycle.sqlite').register(t['id'])
                if params.get('projectId'):
                    if t.get('projectId') != params['projectId']:
                        return self._json({'error':{'code':'project_not_confirmed',
                            'message':'引擎未确认工程归属；会话已创建，请勿重复新建。'},
                            'threadId':t.get('id')},409)
                    t['projectAssignmentState'] = 'awaiting-first-message'
                thread_group_apply(t)
                if params.get('projectId'):
                    created_catalog=desktop_project_catalog().data()
                    # Newly created, unpersisted threads are not yet in the
                    # native table; the engine's response is their confirmation.
                    if t['id'] not in created_catalog.get('nativeThreads',{}):
                        created_catalog={**created_catalog,'memberships':{
                            **created_catalog['memberships'],t['id']:params['projectId']}}
                        desktop_project_catalog().apply(t,created_catalog)
                return self._json({"thread": t})
            # POST /api/threads/<id>/archive | unarchive | rename
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "threads":
                tid, action = parts[2], parts[3]
                is_wb = WB is not None and WB.is_known(tid)
                is_claude = CLAUDE is not None and CLAUDE.is_known(tid)
                if action == "archive":
                    if is_wb:
                        WB.set_archived(tid, True)
                        return self._json({"ok": True, "engine": "workbuddy"})
                    if is_claude:
                        CLAUDE.set_archived(tid, True)
                        return self._json({"ok": True, "engine": "claude"})
                    AS.request("thread/archive", {"threadId": tid}, timeout=30)
                    return self._json({"ok": True})
                if action == "unarchive":
                    if is_wb:
                        WB.set_archived(tid, False)
                        return self._json({"ok": True, "engine": "workbuddy"})
                    if is_claude:
                        CLAUDE.set_archived(tid, False)
                        return self._json({"ok": True, "engine": "claude"})
                    AS.request("thread/unarchive", {"threadId": tid}, timeout=30)
                    return self._json({"ok": True})
                if action == "release":
                    if is_wb:
                        return self._json({"ok": True, "released": False, "engine": "workbuddy"})
                    if is_claude:
                        return self._json({"ok": True, "released": False, "engine": "claude"})
                    if int(queue_counts().get(str(tid),0)):
                        return self._json({'error':{'code':'handoff_queue_pending',
                            'message':'还有排队消息，请先处理或取消后再交给电脑。'}},409)
                    released=AS.release(tid)
                    info=thread_lock_info(tid,wait=.1)
                    ours=any(owner.get('ours') for owner in info.get('owners') or [])
                    if not released or ours:
                        return self._json({'ok':False,'released':False,'control':'desktop',
                            'error':{'code':'handoff_pending','message':'交还尚未确认，AN 仍在释放占用，请稍后重试。'}},409)
                    return self._json({'ok':True,'released':True,'verified':True,
                                       'control':'desktop','lock':info})
                if action == "takeover":
                    if is_wb or is_claude:
                        return self._json({'error':{'message':'此操作只适用于 Codex。'}},400)
                    return self._json(AS.takeover(tid))
                if action == "force-unlock":
                    # 用户最高控制权：清掉占用标记；对方真在跑时默认拦一下，force=true 可强夺。
                    info = thread_lock_info(tid, wait=0.8)
                    if tid in AS.resumed:
                        try:
                            AS.release(tid)
                        except Exception:
                            pass
                    if not info.get("held"):
                        try:
                            thread_lock_path(tid).unlink()
                        except OSError:
                            pass
                        AS.control.set_desktop(tid,False)
                        return self._json({"ok": True, "alreadyFree": True,
                                           "lock": thread_lock_info(tid, wait=0.8),
                                           "owners": info.get("owners") or []})
                    if info.get("active") and not bool(body.get("force")):
                        return self._json({"error": {
                            "code": "writer_active",
                            "message": "对方正在执行回合；确定要夺回请带 force=true 再试。",
                            "lock": info}}, 409)
                    rel = force_release_thread_lock(tid)
                    AS.control.set_desktop(tid,False)
                    invalidate_lock_caches(tid)
                    return self._json({"ok": True, "unlocked": rel,
                                       "owners": info.get("owners") or [],
                                       "lock": thread_lock_info(tid, wait=0.8)})
                if action == "move":
                    if not is_wb and not is_claude:
                        from desktop_project_commands import assign_thread
                        if 'projectId' not in body:
                            return self._json({'error':{'code':'project_id_required',
                                'message':'请选择 Codex 桌面端工程，不能仅修改手机目录。'}},400)
                        try:
                            result=assign_thread(AS.request,desktop_project_catalog(),tid,body['projectId'])
                        except (ValueError,RuntimeError) as exc:
                            return self._json({'error':{'code':'project_not_confirmed','message':str(exc)}},409)
                        HUB.broadcast('threads_changed',{'threadId':tid})
                        return self._json(result)
                    # 只改本网关的归组覆盖层，不碰引擎会话文件里的真实 cwd。
                    _dest = body.get("cwd")
                    if _dest is None:
                        return self._json({"error": {"code": "bad_request",
                                                     "message": "missing cwd"}}, 400)
                    _dest = str(_dest)
                    if _dest and not os.path.isdir(_dest):
                        return self._json({"error": {"code": "folder_not_found",
                                                     "message": "目标文件夹不存在: " + _dest}}, 400)
                    thread_group_set(tid, _dest)
                    return self._json({"ok": True, "cwd": _dest})
                if action == "delete":
                    if is_wb:
                        WB.delete(tid)
                        return self._json({"ok": True, "engine": "workbuddy"})
                    if is_claude:
                        CLAUDE.delete(tid)
                        return self._json({"ok": True, "engine": "claude"})
                    AS.request("thread/delete", {"threadId": tid}, timeout=30)
                    return self._json({"ok": True})
                if action == "name":
                    if is_wb:
                        WB.rename(tid, body.get("name") or "")
                        return self._json({"ok": True, "engine": "workbuddy"})
                    if is_claude:
                        CLAUDE.rename(tid, body.get("name") or "")
                        return self._json({"ok": True, "engine": "claude"})
                    AS.request("thread/name/set", {"threadId": tid, "name": body.get("name") or ""}, timeout=30)
                    return self._json({"ok": True})
                if action == "settings":
                    if is_wb:
                        _mdl_w = body.get("model")
                        _eff_w = body.get("effort")
                        _key_w = _settings_run_key("workbuddy", tid, _mdl_w or "", "", _eff_w or "")

                        def _run_wb_settings():
                            _okm, _whym = wb_model_for_thread_ok(tid, _mdl_w)
                            if not _okm:
                                return ({"error": {"code": "model_not_found",
                                                   "message": _whym}}, 400)
                            _oke, _whye = engine_effort_ok("workbuddy", _eff_w)
                            if not _oke:
                                return ({"error": {"code": "effort_not_supported", "message": _whye}}, 400)
                            cur = WB.update_settings(tid, _mdl_w, _eff_w)
                            return ({"ok": True, "engine": "workbuddy", "note": "下次发消息生效",
                                     "model": cur.get("model"), "effort": cur.get("effort")}, 200)

                        _resp_w, _code_w, _hit_w = _settings_once(
                            _key_w, _run_wb_settings, max_age=0)
                        if _hit_w:
                            _resp_w = dict(_resp_w or {}, deduped=True)
                        return self._json(_resp_w, _code_w)
                    if is_claude:
                        _mdl_c = body.get("model")
                        _eff_c = body.get("effort")
                        _key_c = _settings_run_key("claude", tid, _mdl_c or "", "", _eff_c or "")

                        def _run_claude_settings():
                            _okm, _whym = engine_model_ok("claude", _mdl_c)
                            if not _okm:
                                return ({"error": {"code": "model_not_found",
                                                   "message": _whym}}, 400)
                            cur = CLAUDE.update_settings(tid, _mdl_c, _eff_c)
                            return ({"ok": True, "engine": "claude", "note": "下次发消息生效",
                                     "model": cur.get("model"), "effort": cur.get("effort")}, 200)

                        _resp_c, _code_c, _hit_c = _settings_once(
                            _key_c, _run_claude_settings, max_age=0)
                        if _hit_c:
                            _resp_c = dict(_resp_c or {}, deduped=True)
                        return self._json(_resp_c, _code_c)
                    mdl = body.get("model")
                    eff = body.get("effort")
                    if not mdl and not eff:
                        return self._json({"error": "empty settings"}, 400)
                    _okm, _whym = engine_model_ok("codex", mdl)
                    if not _okm:
                        return self._json({"error": {"code": "model_not_found",
                                                     "message": _whym}}, 400)
                    _pv3 = (model_provider_of(mdl) if mdl else "") or AS.provider_hint(tid)
                    _key_x = _settings_run_key("codex", tid, mdl or "", _pv3 or "", eff or "")

                    def _valid_x(_resp):
                        # 缓存只用于同一毫秒 burst 去重；一旦真实状态变了就必须重跑。
                        # 尤其 A->B->A：A 的旧缓存不能盖掉 B 之后的真实状态。
                        if not isinstance(_resp, dict) or not _resp.get("ok"):
                            return False
                        _pending = AS.pending_switch.get(tid)
                        if _pending and tuple(map(str, _pending)) != (str(mdl), str(_pv3 or ""), str(eff or "")):
                            return False
                        if _resp.get("note"):
                            _ps = AS.pending_switch.get(tid)
                            if (_ps and str(_ps[0]) == str(mdl)
                                    and str(_ps[1] or "") == str(_pv3 or "")
                                    and str(_ps[2] or "") == str(eff or "")):
                                return True
                            # pending 可能已被回合结束消费；落到下面的状态核对。
                        if mdl:
                            if str(AS.thread_model.get(tid) or "") != str(mdl):
                                return False
                            _cur_pv = AS.thread_provider.get(tid)
                            if _cur_pv is not None and _pv3 and str(_cur_pv) != str(_pv3):
                                return False
                            if eff:
                                _cur_eff = codex_thread_settings(tid).get("effort") or AS.thread_effort.get(tid)
                                if _cur_eff is not None and str(_cur_eff) != str(eff):
                                    return False
                        elif eff:
                            _cur_eff = codex_thread_settings(tid).get("effort") or AS.thread_effort.get(tid)
                            if _cur_eff is not None and str(_cur_eff) != str(eff):
                                return False
                        return True

                    def _run_codex_settings():
                        if mdl:
                            # Model and provider must switch as a pair. A bare
                            # thread/settings/update(model) leaves deepseek-flash on the
                            # configured-provider provider -> upstream 404. SETTINGS_LOCK stops
                            # concurrent clients from overwriting each other mid-switch.
                            if thread_running(tid):
                                try:
                                    queue_intent_store().put_thread_settings(tid,mdl,_pv3,eff or "")
                                except Exception as exc:
                                    log("deferred settings persistence failed for %s: %s" %
                                        (tid[:8],type(exc).__name__))
                                    return ({"error":{"code":"settings_intent_unavailable",
                                        "message":"模型设置尚未持久保存，请稍后重试。"}},503)
                                _prev = AS.pending_switch.get(tid)
                                AS.pending_switch[tid] = (mdl, _pv3, eff or "")
                                AS.last_activity[tid] = time.time()
                                if _prev != (mdl, _pv3, eff or ""):
                                    log("pending switch %s: %s -> %s" % (tid[:8], mdl, _pv3))
                                return ({"ok": True, "model": mdl, "effort": eff,
                                         "deferred": True, "note": "applies after current turn"}, 200)
                            with SETTINGS_LOCK:
                                AS.switch_thread_model(tid, mdl, _pv3, eff or "")
                                AS.pending_switch.pop(tid, None)
                            return ({"ok": True, "model": mdl, "effort": eff}, 200)
                        AS.ensure_resumed(tid)
                        params = {"threadId": tid}
                        if eff:
                            params["effort"] = eff
                        AS.request("thread/settings/update", params, timeout=30)
                        _wait_codex_settings(tid, effort=eff, allow_missing=tid in AS.nexus_created)
                        if eff:
                            AS.thread_effort[tid] = str(eff)
                        AS.last_activity[tid] = time.time()
                        return ({"ok": True, "model": mdl, "effort": eff}, 200)

                    _resp_x, _code_x, _hit_x = _settings_once(
                        _key_x, _run_codex_settings, valid=_valid_x)
                    if _hit_x:
                        _resp_x = dict(_resp_x or {}, deduped=True)
                    return self._json(_resp_x, _code_x)
            # POST /api/settings  {"thinkZh": true|false}
            if path == "/api/settings":
                if isinstance(body.get("thinkZh"), bool) and not body["thinkZh"]:
                    log("settings: thinkZh is locked on; ignored false")
                # 中文思考是产品固定行为，不接受关闭。返回值始终保持前后端幂等。
                nexus_patch({"thinkZh": True})
                return self._json({"ok": True, "thinkZh": True, "locked": True})
            # POST /api/asr  {"audio": base64, "format": "mp4"} 语音转文字
            if path == "/api/asr":
                res = asr_transcribe(body)
                if res.get("error"):
                    return self._json(res, 400)
                return self._json(res)
            # POST /api/approvals  {"id":..., "result": {...}}
            if path == "/api/approvals":
                rid = body.get("id")
                result = body.get("result")
                if rid is None:
                    return self._json({"error": "missing id"}, 400)
                ok = AS.respond(rid, result=result)
                return self._json({"ok": ok})
            # POST /api/threads/<tid>/force-rebind {"model":..., "provider":...}
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "threads" and parts[3] == "force-rebind":
                _tid = parts[2]
                _mdl = str(body.get("model") or "").strip()
                _pv = str(body.get("provider") or "").strip() or model_provider_of(_mdl)
                if not _mdl:
                    return self._json({"ok": False, "error": {"code": "bad_request", "message": "model 必填"}}, 400)
                if AS is None:
                    return self._json({"ok": False, "error": {"code": "no_appserver", "message": "app-server 未就绪"}}, 503)
                try:
                    AS.switch_thread_model(_tid, _mdl, _pv, str(body.get("effort") or ""))
                except ApiError as exc:
                    _e = exc.err if isinstance(exc.err, dict) else {"message": str(exc.err)}
                    return self._json({"ok": False, "error": _e}, 400)
                return self._json({"ok": True, "model": _mdl, "provider": _pv})
            # POST /api/compact
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "threads" and parts[3] == "compact":
                if WB is not None and WB.is_known(parts[2]):
                    return self._json({"error": {"code": "unsupported",
                                                 "message": "WorkBuddy 引擎不支持压缩上下文"}}, 501)
                if CLAUDE is not None and CLAUDE.is_known(parts[2]):
                    return self._json({"error": {"code": "unsupported",
                                                 "message": "Claude 引擎不支持压缩上下文"}}, 501)
                AS.request("thread/compact/start", {"threadId": parts[2]}, timeout=30)
                return self._json({"ok": True})
            # POST /api/fs/mkdir | /api/fs/newfile  {"path": 父目录, "name": 名称}
            if path in ("/api/fs/mkdir", "/api/fs/newfile"):
                parent = body.get("path") or ""
                name = body.get("name") or ""
                if path == "/api/fs/mkdir":
                    return self._json(fs_mkdir(parent, name))
                return self._json(fs_newfile(parent, name))
        except ApiError as exc:
            if self._client_disconnected(exc) or self._client_disconnected(getattr(exc, "__cause__", None)):
                self.close_connection = True
                return
            return self._api_error(exc)
        except Exception as exc:
            if self._client_disconnected(exc):
                self.close_connection = True
                return
            log("api_500 %s: %r\n%s" % (path, exc, traceback.format_exc()))
            return self._json({"error": str(exc)}, 500)
        return self._json({"error": "not found"}, 404)


class NexusHTTPServer(ThreadingHTTPServer):
    """TCP_NODELAY + 更大的 accept 队列。

    默认 ThreadingHTTPServer 会开 Nagle: 小 JSON / WS 帧被攒着等 ACK,
    公网隧道下就是几十毫秒的额外顿挫。每个连接进入时关掉 Nagle。
    """
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def server_bind(self):
        super().server_bind()
        try:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def process_request(self, request, client_address):
        try:
            request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        super().process_request(request, client_address)

    def handle_error(self, request, client_address):
        """Drop tracebacks for peers that vanished mid-request.

        A phone that scans the QR twice or refreshes while a response is
        still being written produces WinError 10054 inside socketserver.
        That is a normal client disconnect, not a backend failure, and it
        must not pollute gateway.err.log with a fake crash report.
        """
        exc = sys.exc_info()[1]
        if exc is not None and Handler._client_disconnected(exc):
            return
        super().handle_error(request, client_address)


def main():
    global AS, HUB, TAILER, CLAUDE, WB, PUMP, RECOVERY_JOURNAL, _RUNTIME_EPOCH
    global _WORKFLOW_WORKER
    try:
        cleanup_thread_locks()
    except Exception as _lce:
        log("thread-lock cleanup failed: %r" % (_lce,))
    from event_journal import EventJournal
    (APP_DIR / '_run').mkdir(parents=True, exist_ok=True)
    RECOVERY_JOURNAL = EventJournal(APP_DIR / '_run' / 'event-journal.sqlite')
    _RUNTIME_EPOCH = RECOVERY_JOURNAL.epoch()
    restored = RECOVERY_JOURNAL.load_runtime()
    for event in RECOVERY_JOURNAL.load_recent():
        params = event.get('params') or {}
        tid = params.get('threadId')
        state = params.get('runtime')
        if tid and state and state.get('version',0) >= (restored.get(tid) or {}).get('version',0):
            restored[tid] = state
    for state in restored.values():
        if isinstance(state.get('signature'), list):
            state['signature'] = tuple(state['signature'])
    _RUNTIME_STATUS.update(restored)
    submission_store()
    HUB = Hub(RECOVERY_JOURNAL)
    AS = CodexAS(HUB)
    TAILER = Tailer(HUB)
    CLAUDE = ClaudeAS(HUB)
    WB = WorkBuddyAS(HUB)
    PUMP = QueuePump(HUB)
    try:
        AS.start()
    except Exception as exc:
        log(f"initial start failed: {exc}")
        AS.status = "dead"
    PUMP.start()
    from workflow_tasks import WorkflowWorker
    _WORKFLOW_WORKER=WorkflowWorker(workflow_store(),workflow_dispatch,workflow_reconcile,logger=log)
    _WORKFLOW_WORKER.start()
    # ---- 内测试用: 建档 + 到期守望 (TRIAL_GATE_V1) ----
    try:
        if TRIAL is not None:
            TRIAL.ensure()
            TRIAL.watch_start()
            _ti = TRIAL.info()
            log("trial mode=%s remain=%s id=%s" % (_ti.get("mode"), _ti.get("remain"), _ti.get("id")))
    except Exception as _te:
        log("trial init failed: %r" % (_te,))
    # ---- 交付包自护层 (GUARD_V1) ----
    try:
        if GUARD is not None:
            GUARD.boot()
            _gs = GUARD.status()
            log("guard enforcing=%s tripped=%s reason=%s" % (_gs.get("enforcing"), _gs.get("tripped"), _gs.get("reason")))
    except Exception as _ge:
        log("guard init failed: %r" % (_ge,))
    _start_thread_indexer()
    server = NexusHTTPServer((HOST, PORT), Handler)
    log(f"Agent Nexus gateway listening on port {PORT}; authenticated access required")
    server.serve_forever()


if __name__ == "__main__":
    main()
