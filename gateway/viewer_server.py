"""AgentRemote Gateway v0.2 - read-only Codex session viewer (stdlib only).

- Incremental JSONL parsing: only new bytes are parsed after first read.
- Context-block detection: injected instruction blocks are folded.
"""
import json
import os
import secrets
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

GATEWAY_DIR = Path(__file__).resolve().parent
APP_DIR = GATEWAY_DIR.parent
UI_DIR = APP_DIR / "ui"
SECRETS_DIR = GATEWAY_DIR / ".secrets"
TOKEN_FILE = SECRETS_DIR / "token.txt"
SESSIONS_DIR = Path.home() / ".codex" / "sessions"
PORT = int(os.environ.get("AGENTREMOTE_PORT", "8790"))
MAX_SESSIONS = 40
MAX_LINES_SCAN = 4000
MAX_MESSAGES = 1500
MAX_TEXT = 8000
CTX_TEXT = 400

CONTEXT_PREFIXES = (
    "# AGENTS.md",
    "<environment_context>",
    "<ENVIRONMENT_CONTEXT",
    "<app-context>",
    "<INSTRUCTIONS>",
    "<user_instructions>",
    "<turn_context>",
)

_cache = {}


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


def iter_json_lines(path: Path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def message_text(payload: dict) -> str:
    chunks = []
    for item in payload.get("content") or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("input_text") or item.get("output_text")
            if isinstance(text, str):
                chunks.append(text)
        elif isinstance(item, str):
            chunks.append(item)
    return "\n".join(chunks).strip()


def is_context_block(text: str) -> bool:
    s = text.lstrip()
    if s.startswith(CONTEXT_PREFIXES):
        return True
    head = s[:220]
    return "AGENTS.md instructions" in head or "<app-context>" in head


def quick_meta(path: Path) -> dict:
    meta = {}
    for i, obj in enumerate(iter_json_lines(path)):
        if obj.get("type") == "session_meta":
            payload = obj.get("payload") or {}
            meta = {
                "session_id": payload.get("session_id", ""),
                "cwd": payload.get("cwd", ""),
                "started": payload.get("timestamp") or obj.get("timestamp", ""),
                "originator": payload.get("originator", ""),
                "cli": payload.get("cli_version", ""),
            }
            break
        if i > 4:
            break
    return meta


def first_user_text(path: Path) -> str:
    fallback = ""
    for i, obj in enumerate(iter_json_lines(path)):
        if i > MAX_LINES_SCAN:
            break
        if obj.get("type") == "response_item":
            payload = obj.get("payload") or {}
            if payload.get("type") == "message" and payload.get("role") == "user":
                text = message_text(payload)
                if not text:
                    continue
                if is_context_block(text):
                    if not fallback:
                        fallback = text[:180]
                    continue
                return text[:180]
    return fallback


def list_sessions() -> list:
    if not SESSIONS_DIR.exists():
        return []
    files = sorted(SESSIONS_DIR.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_SESSIONS]
    out = []
    for path in files:
        st = path.stat()
        out.append({
            "file": path.relative_to(SESSIONS_DIR).as_posix(),
            "id": path.stem.replace("rollout-", ""),
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "size_kb": int(st.st_size / 1024),
            "meta": quick_meta(path),
            "first_user": first_user_text(path),
        })
    return out


def local_hms(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts[11:19] if len(ts) >= 19 else ts


def apply_line(data: dict, raw: bytes) -> bool:
    try:
        obj = json.loads(raw)
    except Exception:
        return False
    otype = obj.get("type")
    payload = obj.get("payload") or {}
    if otype == "session_meta" and not data["meta"]:
        data["meta"] = {
            "session_id": payload.get("session_id", ""),
            "cwd": payload.get("cwd", ""),
            "started": payload.get("timestamp") or obj.get("timestamp", ""),
            "cli": payload.get("cli_version", ""),
        }
    elif otype == "response_item":
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                text = message_text(payload)
                if text:
                    ctx = role == "user" and is_context_block(text)
                    if ctx:
                        text = text[:CTX_TEXT] + "\n……（长上下文已折叠）"
                    elif len(text) > MAX_TEXT:
                        text = text[:MAX_TEXT] + "\n……（已截断）"
                    data["messages"].append({
                        "role": role, "text": text,
                        "ts": local_hms(obj.get("timestamp", "")),
                        "ctx": ctx,
                    })
                    if len(data["messages"]) > MAX_MESSAGES:
                        del data["messages"][: len(data["messages"]) - MAX_MESSAGES]
        elif ptype == "function_call":
            data["tool_calls"] += 1
    return True


def build_timeline(path: Path) -> dict:
    st = path.stat()
    key = str(path)
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _cache.get(key)
    if cached and cached["stamp"] == stamp:
        cached["data"]["parsed_at"] = time.strftime("%H:%M:%S")
        return cached["data"]

    if cached and st.st_size >= cached["offset"]:
        data = cached["data"]
        offset = cached["offset"]
    else:
        data = {"meta": {}, "messages": [], "tool_calls": 0}
        offset = 0

    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            line_start = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            stripped = raw.strip()
            if not stripped:
                offset = fh.tell()
                continue
            if not apply_line(data, stripped):
                offset = line_start
                break
            offset = fh.tell()

    data["parsed_at"] = time.strftime("%H:%M:%S")
    _cache[key] = {"stamp": stamp, "offset": offset, "data": data}
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentRemote/0.2"

    def _authorized(self, params: dict) -> bool:
        if self.headers.get("X-Auth") == TOKEN:
            return True
        values = params.get("k") or []
        return bool(values) and values[0] == TOKEN

    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path

        if path in ("/", "/index.html"):
            index = UI_DIR / "index.html"
            if not index.exists():
                return self._send(b"ui/index.html missing", "text/plain; charset=utf-8", 500)
            return self._send(index.read_bytes(), "text/html; charset=utf-8")

        if not path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not self._authorized(params):
            return self._json({"error": "unauthorized"}, 401)

        if path == "/api/ping":
            return self._json({"ok": True, "service": "AgentRemote", "mode": "read-only",
                               "time": time.strftime("%Y-%m-%d %H:%M:%S"), "sessions_dir": str(SESSIONS_DIR)})

        if path == "/api/sessions":
            try:
                return self._json({"sessions": list_sessions()})
            except Exception as exc:
                return self._json({"error": f"list failed: {exc}"}, 500)

        if path == "/api/session":
            rel = (params.get("file") or [""])[0]
            target = (SESSIONS_DIR / rel).resolve()
            root = SESSIONS_DIR.resolve()
            if not rel or root not in target.parents or target.suffix != ".jsonl" or not target.is_file():
                return self._json({"error": "bad file"}, 400)
            try:
                data = build_timeline(target)
                out = dict(data)
                out["file"] = rel
                return self._json(out)
            except Exception as exc:
                return self._json({"error": f"parse failed: {exc}"}, 500)

        return self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stderr.flush()


def main() -> None:
    print(f"AgentRemote viewer v0.2 on http://127.0.0.1:{PORT}")
    print(f"sessions dir: {SESSIONS_DIR}")
    print(f"token file  : {TOKEN_FILE}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
