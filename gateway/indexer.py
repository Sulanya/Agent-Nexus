"""Agent Nexus · Codex 会话索引器

唯一的职责：把「读 Codex 磁盘」从 HTTP 请求路径里彻底拿走。

- 元数据来自 ~/.codex/state_*.sqlite（标题 / 时间 / cwd / 模型 / 归档状态）
- 运行状态与最后一条回复来自 rollout JSONL 的尾部（默认 256KB，最多回扫 4MB）
- 产出是一个不可变快照 _run/index/threads.json，网关只读它，不碰磁盘

用法：
    python indexer.py --once         # 构建一次（改完代码 / 手工重建）
    python indexer.py                # 常驻，默认 2 秒一轮
    python indexer.py --interval 3 --verbose
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

VERSION = 1
SNAPSHOT_STALE_SECONDS = 15.0

APP_DIR = Path(__file__).resolve().parent.parent
RUN_DIR = APP_DIR / "_run"
INDEX_DIR = RUN_DIR / "index"
SNAPSHOT = INDEX_DIR / "threads.json"
LOCK_FILE = INDEX_DIR / "indexer.lock"
PID_FILE = INDEX_DIR / "indexer.pid"

TAIL_BYTES = 256 * 1024
MAX_TAIL_BYTES = 4 * 1024 * 1024
HEAVY_BYTES = 50 * 1024 * 1024
REFRESH_SECONDS = 2.0
LAST_MSG_CHARS = 240
MSG_TEXT_CHARS = 1200      # 每条预加载消息的正文上限
MSG_TEXT_CHARS_TAIL = 20000  # 详情页尾部窗口：保留更完整的正文，仍设硬上限
LAST_MSGS = 20             # 每个会话预加载的消息条数
TAILS_MAX_THREADS = 80     # 最多为多少个会话产出预加载正文
TAILS = INDEX_DIR / "tails.json"

START_MARK = b'"type":"event_msg","payload":{"type":"task_started"'
END_MARKS = (
    b'"type":"event_msg","payload":{"type":"task_complete"',
    b'"type":"event_msg","payload":{"type":"turn_aborted"',
)
ASSISTANT_RE = re.compile(rb'"role"\s*:\s*"assistant"')

_FILE_CACHE = {}
_snap_cache = {"key": None, "snap": None, "checked": 0.0}
_verbose = False


def _v(msg):
    if _verbose:
        print("[indexer] " + msg, flush=True)


def _user_home() -> Path:
    for key in ("USERPROFILE", "HOME"):
        val = (os.environ.get(key) or "").strip()
        if val and Path(val).is_dir():
            return Path(val)
    return Path.home()


def codex_home() -> Path:
    for key in ("AGENTREMOTE_CODEX_HOME", "CODEX_HOME"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return Path(val)
    return _user_home() / ".codex"


def sessions_root() -> Path:
    val = (os.environ.get("AGENTREMOTE_SESSIONS_DIR") or "").strip()
    return Path(val) if val else (codex_home() / "sessions")


def locks_root() -> Path:
    val = (os.environ.get("AGENTREMOTE_LOCKS_DIR") or "").strip()
    return Path(val) if val else (codex_home() / "thread-writer-locks")


def state_db():
    override = (os.environ.get("AGENTREMOTE_STATE_DB") or "").strip()
    if override:
        return Path(override)
    home = codex_home()
    try:
        cands = sorted(home.glob("state_*.sqlite"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in cands:
        try:
            con = sqlite3.connect("file:" + path.as_posix() + "?mode=ro",
                                  uri=True, timeout=1.0)
            try:
                con.execute("select 1 from threads limit 1")
            finally:
                con.close()
            return path
        except Exception:
            continue
    return None


_EXT_PREFIX = chr(92) * 2 + "?" + chr(92)

def _norm_path(value) -> str:
    text = str(value or "").strip()
    if text.startswith(_EXT_PREFIX):
        text = text[4:]
    return text


def _secs(ms_value, sec_value) -> float:
    try:
        if ms_value not in (None, ""):
            return float(ms_value) / 1000.0
    except (TypeError, ValueError):
        pass
    try:
        return float(sec_value or 0)
    except (TypeError, ValueError):
        return 0.0


def _read_rows():
    path = state_db()
    if path is None:
        return None, None
    try:
        con = sqlite3.connect("file:" + path.as_posix() + "?mode=ro",
                              uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute("select * from threads")]
        finally:
            con.close()
        return path, rows
    except Exception as exc:
        _v("state db read failed: %r" % (exc,))
        return path, None


def _rollout_map() -> dict:
    out = {}
    try:
        for path in sessions_root().rglob("*.jsonl"):
            name = path.name
            if not name.startswith("rollout-"):
                continue
            stem = name[len("rollout-"):-len(".jsonl")]
            parts = stem.split("-")
            if len(parts) >= 10:
                out["-".join(parts[-5:])] = path
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 锁探测

if os.name == "nt":
    import msvcrt

    def _is_free(fh) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return True
        except OSError:
            return False
else:
    try:
        import fcntl as _fcntl
    except Exception:
        _fcntl = None

    def _is_free(fh) -> bool:
        if _fcntl is None:
            return True
        try:
            _fcntl.lockf(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB, 1, 0, os.SEEK_SET)
            _fcntl.lockf(fh.fileno(), _fcntl.LOCK_UN, 1, 0, os.SEEK_SET)
            return True
        except OSError:
            return False


def _lock_state(tid, running):
    """held=别人握着; active=持有者还有回合在跑; stale=占位但没人跑"""
    path = locks_root() / (str(tid) + ".lock")
    info = {"held": False, "active": False, "stale": False}
    try:
        if not path.exists():
            return info
    except OSError:
        return info
    try:
        fh = open(path, "r+b")
    except OSError:
        info["held"] = True
        info["active"] = bool(running)
        info["stale"] = not bool(running)
        return info
    try:
        free = _is_free(fh)
    finally:
        try:
            fh.close()
        except Exception:
            pass
    if free:
        info["stale"] = True
        return info
    info["held"] = True
    info["active"] = bool(running)
    info["stale"] = not bool(running)
    return info


# ---------------------------------------------------------------- JSONL 尾读

def _marker_running(data):
    idx_start = data.rfind(START_MARK)
    idx_end = -1
    for mark in END_MARKS:
        pos = data.rfind(mark)
        if pos > idx_end:
            idx_end = pos
    best = max(idx_start, idx_end)
    if best < 0:
        return None
    return best == idx_start


def _tail_payload(path, size):
    want = TAIL_BYTES
    while True:
        start = max(0, size - want)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(size - start)
        if _marker_running(data) is not None or start == 0 or want >= MAX_TAIL_BYTES:
            return start, data
        want = min(want * 4, MAX_TAIL_BYTES)


def _message_text(record):
    payload = record.get("payload") or {}
    content = payload.get("content") or []
    texts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]))
    if not texts and payload.get("message"):
        texts.append(str(payload["message"]))
    text = "\n".join(t for t in texts if t).strip()
    if len(text) > LAST_MSG_CHARS:
        text = text[:LAST_MSG_CHARS] + "…"
    return text


_INJECT_PREFIXES = (
    "<app-context>", "<environment_context>", "<user_instructions>",
    "<permissions instructions>", "<collaboration_mode>",
    "<skills_instructions>", "# AGENTS.md instructions",
    "<codex_apps_client_time_context>", "<turn_aborted>",
)


def _looks_injected(text: str) -> bool:
    """跳过 harness 注入的上下文（AGENTS.md / app-context 等），只留真人消息。"""
    head = text.lstrip()[:400]
    if not head:
        return True
    for pref in _INJECT_PREFIXES:
        if head.startswith(pref):
            return True
    low = head.lower()
    return head.startswith("<") and ("instructions" in low or "context>" in low)


def _message_parts(record) -> str:
    payload = record.get("payload") or {}
    content = payload.get("content") or []
    texts = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("text"):
                texts.append(str(part["text"]))
            elif part.get("content"):
                texts.append(str(part["content"]))
    if not texts and payload.get("message"):
        texts.append(str(payload["message"]))
    return "\n".join(t for t in texts if t).strip()


def _message_from_record(record, max_chars=MSG_TEXT_CHARS):
    payload = record.get("payload") or {}
    if payload.get("type") != "message":
        return None
    role = str(payload.get("role") or "")
    if role not in ("user", "assistant"):
        return None
    text = _message_parts(record)
    if not text or _looks_injected(text):
        return None
    cut = len(text) > max_chars
    return {
        "role": role,
        "text": text[:max_chars] + ("\u2026" if cut else ""),
        "truncated": cut,
        "ts": record.get("timestamp") or "",
        "itemId": str(payload.get("id") or ""),
    }


def _last_messages(data: bytes, limit: int = LAST_MSGS, max_chars: int = MSG_TEXT_CHARS):
    """从 rollout 尾部解析最近 N 条 user/assistant 消息（保持顺序）。"""
    out = []
    for raw in data.split(b"\n"):
        if not raw.strip() or b'"type":"response_item"' not in raw:
            continue
        if b'"type":"message"' not in raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        msg = _message_from_record(record, max_chars)
        if msg is None:
            continue
        out.append(msg)
        if len(out) > limit * 6:
            out = out[-limit:]
    return out[-limit:]


def _scan_messages(data: bytes, base: int, skip_first: bool = True,
                   max_chars: int = MSG_TEXT_CHARS):
    """返回 [(绝对行首偏移, message)]；base>0 时首行可能是半截，跳过。"""
    out = []
    pos = 0
    for idx, raw in enumerate(data.split(b"\n")):
        line_start = pos
        pos += len(raw) + 1
        if skip_first and base > 0 and idx == 0:
            continue
        if not raw.strip() or b'"type":"response_item"' not in raw:
            continue
        if b'"type":"message"' not in raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        msg = _message_from_record(record, max_chars)
        if msg is not None:
            msg = dict(msg)
            msg["off"] = base + line_start
            msg["id"] = str(msg.get("itemId") or "") or ("m%d" % (base + line_start))
            out.append((base + line_start, msg))
    return out


def _align_line_start(fh, raw_start, ceiling, probe_bytes=8 * 1024 * 1024):
    """把窗口起点前移到最近的整行行首。

    返回 (start, aligned)。找不到换行（单行比窗口还大）时回退到未对齐起点，
    调用方据此跳过首行残片，至少保证游标继续前进、不会死循环。
    """
    if raw_start <= 0:
        return 0, True
    try:
        fh.seek(raw_start)
        probe = fh.read(min(probe_bytes, max(0, ceiling - raw_start)))
    except OSError:
        return raw_start, False
    nl = probe.find(b"\n")
    if nl < 0:
        return raw_start, False
    return raw_start + nl + 1, True


def read_messages_window(path, end=None, max_bytes=2 * 1024 * 1024,
                         limit=LAST_MSGS, max_chars=MSG_TEXT_CHARS,
                         max_scan_bytes=64 * 1024 * 1024):
    """反向窗口读：只为“看历史”，不经过 app-server、不整文件解析。

    返回 {msgs, nextEnd, hasMore}。nextEnd 是更早一页的读取终点，可直接回传。
    工具输出块的 JSON 行很大，文件里会出现几十 MB 一段没有任何 message 记录；
    这种空段要继续往回扫，否则会返回空页、用户得反复点"加载更早"。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"msgs": [], "nextEnd": 0, "hasMore": False, "size": 0}
    cursor = size if end is None else max(0, min(int(end), size))
    collected = []
    scanned = 0
    try:
        fh = open(path, "rb")
    except OSError:
        return {"msgs": [], "nextEnd": 0, "hasMore": False, "size": int(size)}
    try:
        while cursor > 0 and scanned < max_scan_bytes:
            raw_start = max(0, cursor - max_bytes)
            # 窗口起点必须落在整行行首：跨块的消息会被两头各截一半，
            # 两个块都 json 解析失败 -> 翻页时静默丢消息。
            start, aligned = _align_line_start(fh, raw_start, cursor)
            if start >= cursor:
                start, aligned = raw_start, False
            try:
                fh.seek(start)
                data = fh.read(cursor - start)
            except OSError:
                break
            scanned += len(data)
            collected = _scan_messages(data, start, skip_first=not aligned,
                                       max_chars=max_chars) + collected
            cursor = start
            if len(collected) >= limit:
                break
    finally:
        try:
            fh.close()
        except Exception:
            pass
    if collected:
        page = collected[-limit:]
        next_end = page[0][0]
    else:
        page = []
        next_end = cursor
    return {
        "msgs": [m for _, m in page],
        "nextEnd": int(next_end),
        "hasMore": bool(next_end > 0),
        "size": int(size),
    }


def _last_assistant(data):
    text = ""
    for match in ASSISTANT_RE.finditer(data):
        line_start = data.rfind(b"\n", 0, match.start()) + 1
        line_end = data.find(b"\n", match.start())
        if line_end < 0:
            line_end = len(data)
        raw = data[line_start:line_end]
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        found = _message_text(record)
        if found:
            text = found
    return text


def scan_rollout(path, size, prev=None):
    """只读尾部，返回 running / lastAssistant / 消息数估算。"""
    if size <= 0:
        return {"running": False, "lastAssistant": "",
                "messagesEstimate": 0, "messagesExact": True,
                "lastMessages": []}
    try:
        start, data = _tail_payload(path, size)
    except OSError as exc:
        _v("tail read failed %s: %r" % (path, exc))
        if prev:
            return dict(prev)
        return {"running": False, "lastAssistant": "",
                "messagesEstimate": 0, "messagesExact": False,
                "lastMessages": []}

    running = _marker_running(data)
    if running is None:
        running = bool(prev.get("running")) if prev else False

    covered = start == 0
    count = data.count(b'"type":"response_item"')
    if covered:
        estimate = count
    else:
        estimate = int(round(count * size / max(1, len(data)))) if count else 0

    return {
        "running": bool(running),
        "lastAssistant": _last_assistant(data),
        "messagesEstimate": estimate,
        "messagesExact": covered,
        "lastMessages": _last_messages(data),
    }


# ---------------------------------------------------------------- 快照构建

def _entry(row, path, stat_info, scan):
    tid = str(row.get("id") or "")
    size = int(stat_info.st_size) if stat_info else 0
    updated = _secs(row.get("updated_at_ms"), row.get("updated_at"))
    recency = _secs(row.get("recency_at_ms"), row.get("recency_at")) or updated
    source = str(row.get("source") or "")
    thread_source = row.get("thread_source")
    thread_source = str(thread_source) if thread_source is not None else ""
    running = bool(scan.get("running"))
    name = str(row.get("name") or "").strip()
    title = str(row.get("title") or "").strip()
    preview = str(row.get("preview") or "").strip()
    first_user = str(row.get("first_user_message") or "").strip()
    return {
        "id": tid,
        "rolloutPath": str(path) if path else "",
        "name": name,
        "title": title,
        "preview": preview,
        "firstUserMessage": first_user,
        "displayTitle": name or title or preview or first_user or "(空会话)",
        "cwd": _norm_path(row.get("cwd")),
        "createdAt": _secs(row.get("created_at_ms"), row.get("created_at")),
        "updatedAt": updated,
        "recencyAt": recency,
        "archived": bool(row.get("archived")),
        "pinned": bool(row.get("is_pinned")),
        "projectId": row.get("project_id"),
        "model": row.get("model"),
        "modelProvider": row.get("model_provider"),
        "reasoningEffort": row.get("reasoning_effort"),
        "originator": row.get("originator"),
        "source": source,
        "threadSource": thread_source,
        "historyMode": row.get("history_mode"),
        "tokensUsed": row.get("tokens_used"),
        "sizeBytes": size,
        "heavy": size >= HEAVY_BYTES,
        "missingTranscript": path is None or not stat_info,
        "running": running,
        "lock": _lock_state(tid, running),
        "lastAssistant": scan.get("lastAssistant") or "",
        "lastMessages": scan.get("lastMessages") or [],
        "messagesEstimate": int(scan.get("messagesEstimate") or 0),
        "messagesExact": bool(scan.get("messagesExact")),
        "visible": (source != "exec" and thread_source in ("", "user")),
    }


def build_once(verbose=False):
    global _verbose
    _verbose = verbose
    started = time.time()
    db_path, rows = _read_rows()
    if rows is None:
        _v("no rows; keeping previous snapshot")
        return None

    fallback = None
    entries = []
    missing = 0
    for row in rows:
        tid = str(row.get("id") or "")
        if not tid:
            continue
        raw_path = _norm_path(row.get("rollout_path"))
        path = Path(raw_path) if raw_path else None
        stat_info = None
        if path is not None:
            try:
                stat_info = path.stat()
            except OSError:
                stat_info = None
        if stat_info is None:
            if fallback is None:
                fallback = _rollout_map()
            path = fallback.get(tid)
            if path is not None:
                try:
                    stat_info = path.stat()
                except OSError:
                    stat_info = None

        if stat_info is None:
            missing += 1
            scan = {"running": False, "lastAssistant": "",
                    "messagesEstimate": 0, "messagesExact": False}
            entries.append(_entry(row, None, None, scan))
            continue

        key = (stat_info.st_mtime_ns, stat_info.st_size)
        cached = _FILE_CACHE.get(tid)
        if cached and cached.get("key") == key:
            scan = cached["data"]
        else:
            scan = scan_rollout(path, stat_info.st_size,
                                prev=(cached or {}).get("data"))
            _FILE_CACHE[tid] = {"key": key, "data": scan}
        entries.append(_entry(row, path, stat_info, scan))

    entries.sort(key=lambda e: (e.get("recencyAt") or 0), reverse=True)

    tails = {}
    for item in entries:
        if item.get("archived") or item.get("missingTranscript"):
            continue
        msgs = item.get("lastMessages") or []
        if not msgs:
            continue
        tails[item["id"]] = {
            "size": item.get("sizeBytes") or 0,
            "updatedAt": item.get("updatedAt") or 0,
            "partial": not bool(item.get("messagesExact")),
            "msgs": msgs,
        }
        if len(tails) >= TAILS_MAX_THREADS:
            break

    # 正文只在 tails.json 里留一份，快照保持轻量。
    for item in entries:
        item.pop("lastMessages", None)

    payload = {
        "version": VERSION,
        "generatedAt": time.time(),
        "buildMs": int((time.time() - started) * 1000),
        "count": len(entries),
        "visibleCount": sum(1 for e in entries
                            if e.get("visible") and not e.get("archived")),
        "missingTranscript": missing,
        "heavyCount": sum(1 for e in entries if e.get("heavy")),
        "db": str(db_path) if db_path else "",
        "sessionsRoot": str(sessions_root()),
        "threads": entries,
    }
    publish(payload)
    try:
        publish_tails(tails)
    except Exception as exc:
        _v("tails publish failed: %r" % (exc,))
    return payload


def publish(payload):
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    tmp = INDEX_DIR / (".threads.%d.tmp" % os.getpid())
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    last = None
    for _ in range(6):
        try:
            os.replace(tmp, SNAPSHOT)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last


_TAILS_STATE = {"hash": None}


def publish_tails(tails):
    """最近 N 条消息单独落盘；内容没变就不写，避免每 2 秒几 MB 的磁盘写。"""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    slim = {}
    for tid, row in (tails or {}).items():
        slim[tid] = {
            "size": row.get("size"),
            "updatedAt": row.get("updatedAt"),
            "partial": bool(row.get("partial")),
            "msgs": [
                {"role": m.get("role"), "text": m.get("text"),
                 "truncated": bool(m.get("truncated"))}
                for m in (row.get("msgs") or [])
            ],
        }
    digest = hashlib.sha1(
        json.dumps(slim, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if digest == _TAILS_STATE["hash"]:
        return
    blob = json.dumps({"version": 1, "at": time.time(), "items": slim},
                      ensure_ascii=False).encode("utf-8")
    tmp = INDEX_DIR / (".tails.%d.tmp" % os.getpid())
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    last = None
    for _ in range(6):
        try:
            os.replace(tmp, TAILS)
            _TAILS_STATE["hash"] = digest
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last


def read_tails(max_age=0.5):
    """网关侧只读 tails.json（0.5 秒 stat 缓存）。"""
    now = time.time()
    if _tails_cache["data"] is not None and now - _tails_cache["checked"] < max_age:
        return _tails_cache["data"]
    try:
        st = TAILS.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        _tails_cache["checked"] = now
        return _tails_cache["data"] or {}
    if key != _tails_cache["key"]:
        try:
            with open(TAILS, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if int(loaded.get("version") or 0) == 1:
                _tails_cache["data"] = loaded.get("items") or {}
                _tails_cache["key"] = key
        except Exception as exc:
            _v("tails read failed: %r" % (exc,))
    _tails_cache["checked"] = now
    return _tails_cache["data"] or {}


_tails_cache = {"key": None, "data": None, "checked": 0.0}


# ---------------------------------------------------------------- 快照读取

def read_snapshot(max_age=0.4):
    """网关侧只读入口：文件没变就返回内存对象，最多每 0.4 秒 stat 一次。"""
    now = time.time()
    snap = _snap_cache["snap"]
    if snap is not None and now - _snap_cache["checked"] < max_age:
        return snap
    try:
        st = SNAPSHOT.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        _snap_cache["checked"] = now
        return snap
    if key != _snap_cache["key"]:
        try:
            with open(SNAPSHOT, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if int(loaded.get("version") or 0) == VERSION:
                _snap_cache["snap"] = loaded
                _snap_cache["key"] = key
                snap = loaded
            else:
                _v("snapshot version mismatch: %r" % (loaded.get("version"),))
        except Exception as exc:
            _v("snapshot read failed: %r" % (exc,))
    _snap_cache["checked"] = now
    return snap


def snapshot_meta(snap=None, now=None):
    snap = read_snapshot() if snap is None else snap
    if not snap:
        return {"state": "warming", "indexer": "warming", "stale": True,
                "generatedAt": None, "age": None}
    now = time.time() if now is None else now
    try:
        generated = float(snap.get("generatedAt") or 0)
        valid = math.isfinite(generated) and 0 < generated <= now + 5
        age = max(0.0, now - generated) if valid else None
    except (TypeError, ValueError):
        age = None
    stale = age is None or age > SNAPSHOT_STALE_SECONDS
    return {
        "state": "stale" if stale else "ready",
        "stale": stale,
        "generatedAt": snap.get("generatedAt"),
        "age": age,
        "count": snap.get("count"),
        "visibleCount": snap.get("visibleCount"),
        "buildMs": snap.get("buildMs"),
        "version": snap.get("version"),
    }


def find_thread(snap, tid):
    if not snap:
        return None
    for item in snap.get("threads") or []:
        if item.get("id") == tid:
            return item
    return None


# ---------------------------------------------------------------- 常驻循环

def _single_instance():
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+b")
    if os.name == "nt":
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return None
    else:
        if _fcntl is not None:
            try:
                _fcntl.lockf(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB,
                             1, 0, os.SEEK_SET)
            except OSError:
                fh.close()
                return None
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return fh


def loop(interval=REFRESH_SECONDS, verbose=False):
    global _verbose
    _verbose = verbose
    guard = _single_instance()
    if guard is None:
        print("[indexer] another instance is running; exit", flush=True)
        return 1
    crash = None
    try:
        import faulthandler
        crash = open(RUN_DIR / "indexer-crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=crash, all_threads=True)
    except Exception:
        crash = None
    print("[indexer] started pid=%d interval=%.1fs snapshot=%s"
          % (os.getpid(), interval, SNAPSHOT), flush=True)
    try:
        while True:
            try:
                built = build_once(verbose)
                if built is not None and verbose:
                    _v("built %d threads in %dms (visible=%d, missing=%d)"
                       % (built["count"], built["buildMs"],
                          built["visibleCount"], built["missingTranscript"]))
            except Exception as exc:
                import traceback
                print("[indexer] build failed: %r" % (exc,), flush=True)
                traceback.print_exc()
            time.sleep(max(0.5, float(interval)))
    except KeyboardInterrupt:
        return 0
    finally:
        if crash is not None:
            try:
                crash.close()
            except Exception:
                pass
        try:
            guard.close()
        except Exception:
            pass
        try:
            if (PID_FILE.exists()
                    and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid())):
                PID_FILE.unlink()
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agent Nexus thread indexer")
    ap.add_argument("--once", action="store_true", help="构建一次后退出")
    ap.add_argument("--interval", type=float, default=REFRESH_SECONDS)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.once:
        built = build_once(args.verbose)
        if built is None:
            print("[indexer] build failed", flush=True)
            return 2
        print("[indexer] built %d threads in %dms (visible=%d, missing=%d) -> %s"
              % (built["count"], built["buildMs"], built["visibleCount"],
                 built["missingTranscript"], SNAPSHOT), flush=True)
        return 0
    return loop(args.interval, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
