# ---------- Claude Code 引擎(第二引擎: 事件翻译为与 Codex 相同的 UI 词汇) ----------

_claude_scan = {"ts": 0.0, "map": {}}
_claude_meta_cache = {}
_claude_img_cache = {}
_claude_reg_cache = {"ts": 0.0, "data": {}}


def claude_sessions_map(force=False) -> dict:
    """sessionId -> jsonl 路径(15 秒缓存)。"""
    now = time.time()
    if not force and now - _claude_scan["ts"] < 15:
        return _claude_scan["map"]
    m = {}
    try:
        for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
            m.setdefault(p.stem, p)
    except OSError:
        pass
    _claude_scan.update({"ts": now, "map": m})
    return m


def find_claude_session(tid: str):
    return claude_sessions_map().get(tid)


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
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
    if CLAUDE is not None and tid in CLAUDE.turns:
        return True
    ent = claude_session_registry().get(tid)
    return bool(ent and ent.get("status") == "busy")


def _claude_text_of(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
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


def claude_session_meta(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    hit = _claude_meta_cache.get(key)
    if hit and hit[0] == int(st.st_mtime_ns) and hit[1] == st.st_size:
        return hit[2]
    meta = {"id": path.stem, "cwd": "", "name": "", "preview": "", "lastPrompt": "",
            "userTurns": 0, "mtime": st.st_mtime, "mb": round(st.st_size / 1048576.0, 2)}
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
    if not meta["cwd"]:
        meta["cwd"] = str(USER_HOME)
    _claude_meta_cache[key] = (int(st.st_mtime_ns), st.st_size, meta)
    return meta

def claude_read_thread(tid: str) -> dict:
    """Claude 会话 JSONL -> Codex 形状 thread。三轮(流式/历史/尾随)共用同一套 item id。"""
    now_ms = int(time.time() * 1000)
    path = find_claude_session(tid)
    if path is None:
        pend = (CLAUDE.pending.get(tid) if CLAUDE is not None else None)
        if pend:
            return {"id": tid, "engine": "claude", "cwd": pend.get("cwd") or "", "name": "",
                    "preview": "", "turns": [], "updatedAt": now_ms, "recencyAt": now_ms,
                    "writable": True, "status": {"type": "idle"}, "source": "claude-code"}
        raise ApiError({"code": "not_found", "message": "claude 会话不存在: " + tid})
    meta = claude_session_meta(path)
    turns = []
    cur = None
    tool_index = {}
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
                    if txt or imgs:
                        cur = {"id": j.get("uuid") or ("t%d" % len(turns)), "items": []}
                        turns.append(cur)
                        citems = []
                        if txt:
                            citems.append({"type": "text", "text": txt})
                        for b in imgs[:4]:
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
                    for i, b in enumerate(msg.get("content") or []):
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        iid = "%s-%d" % (mid, i)
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
    mt = int((meta.get("mtime") or 0) * 1000)
    thread = {
        "id": tid, "engine": "claude",
        "cwd": meta.get("cwd") or "",
        "name": meta.get("name") or "",
        "preview": meta.get("preview") or "",
        "turns": turns, "updatedAt": mt, "recencyAt": mt,
        "writable": True, "status": {"type": "idle"}, "source": "claude-code",
    }
    return trim_thread(thread)


def claude_models_payload() -> dict:
    """Claude 引擎模型列表: 默认取自 ~/.claude/settings.json; 实测可用者兜底。"""
    model = ""
    effort = ""
    try:
        cfg = json.loads(CLAUDE_LOCAL_SETTINGS.read_text(encoding="utf-8"))
        model = str(cfg.get("model") or "")
        ms = cfg.get("modelSettings") or {}
        if model and isinstance(ms.get(model), dict):
            effort = str(ms[model].get("effortLevel") or "")
    except Exception:
        pass
    effs = ["low", "medium", "high", "xhigh", "max"]
    models = [
        {"id": "deepseek-flash", "label": "DeepSeek V4.1 Flash", "efforts": list(effs)},
        {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "efforts": list(effs)},
    ]
    if model and model not in [m["id"] for m in models]:
        models.insert(0, {"id": model, "label": model, "efforts": list(effs)})
    return {"models": models, "efforts": effs,
            "defaultModel": model or models[0]["id"], "defaultEffort": effort or "high",
            "provider": "claude-code → api.deepseek.com/anthropic"}


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
            evs.append({"k": "user", "ts": ts, "id": obj.get("uuid") or ("cu%d" % int(time.time() * 1000)),
                        "text": _clip(txt, 4000)})
        return evs or None
    if t == "assistant":
        msg = obj.get("message") or {}
        if not isinstance(msg, dict):
            return None
        mid = msg.get("id") or obj.get("uuid") or ""
        evs = []
        for i, b in enumerate(msg.get("content") or []):
            if not isinstance(b, dict):
                continue
            iid = "%s-%d" % (mid, i)
            bt = b.get("type")
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

class ClaudeAS:
    """Claude Code 引擎: 每回合一个 headless 进程; 流式事件 -> 与 Codex 同款的通知。"""

    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self.turns = {}      # tid -> Popen(正在执行的回合)
        self.pending = {}    # tid -> {"cwd":...}(新建未发言的会话)
        self.archived = self._load_json(CLAUDE_ARCHIVE_FILE, [])
        self.settings = self._load_json(CLAUDE_SETTINGS_FILE, {})
        self.exe = find_claude_exe()
        self.status = "ready" if self.exe else "no-cli"
        if not self.exe:
            log("ClaudeAS: 未找到 claude.exe, Claude 引擎不可用(Codex 引擎不受影响)")
        else:
            log(f"ClaudeAS ready: {self.exe}")

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
            log(f"claude save {p.name}: {exc}")

    def is_known(self, tid: str) -> bool:
        return bool(find_claude_session(tid)) or tid in self.pending or tid in self.turns

    def list_threads(self, limit=60, q="", cwd="", archived=False):
        out = []
        ask = (q or "").lower()
        want_cwd = (cwd or "").lower()
        reg_all = claude_session_registry()
        for tid, path in claude_sessions_map().items():
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
            run = claude_running(tid)
            mt = int((meta.get("mtime") or 0) * 1000)
            out.append({
                "id": tid, "engine": "claude",
                "name": meta.get("name") or "",
                "preview": meta.get("preview") or "",
                "cwd": meta.get("cwd") or reg.get("cwd") or "",
                "updatedAt": mt, "recencyAt": mt,
                "writable": True, "loaded": tid in self.turns, "running": run,
                "status": {"type": "active" if run else "idle"},
                "source": "claude-code",
            })
        out.sort(key=lambda x: x.get("recencyAt") or 0, reverse=True)
        return {"data": out[:limit], "engine": "claude"}

    def new_thread(self, cwd=None, model=None, effort=None):
        tid = str(uuid.uuid4())
        cwd = cwd or str(USER_HOME)
        with self.lock:
            self.pending[tid] = {"cwd": cwd, "created": time.time()}
            if model or effort:
                st = self.settings.setdefault(tid, {})
                if model:
                    st["model"] = model
                if effort:
                    st["effort"] = effort
                self._save_json(CLAUDE_SETTINGS_FILE, self.settings)
        now_ms = int(time.time() * 1000)
        return {"id": tid, "engine": "claude", "cwd": cwd, "name": "", "preview": "",
                "turns": [], "updatedAt": now_ms, "recencyAt": now_ms, "writable": True,
                "status": {"type": "idle"}, "source": "claude-code"}

    def set_archived(self, tid: str, flag: bool):
        with self.lock:
            if flag:
                if tid not in self.archived:
                    self.archived.append(tid)
            else:
                self.archived = [x for x in self.archived if x != tid]
            self._save_json(CLAUDE_ARCHIVE_FILE, self.archived)
        return True

    def rename(self, tid: str, name: str):
        path = find_claude_session(tid)
        if path is None:
            raise ApiError({"code": "not_found", "message": "claude 会话不存在"})
        line = {"type": "custom-title", "customTitle": name, "sessionId": tid}
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise ApiError({"code": "io", "message": str(exc)})
        _claude_meta_cache.pop(str(path), None)
        return True

    def delete(self, tid: str):
        path = find_claude_session(tid)
        with self.lock:
            self.pending.pop(tid, None)
        if path is None:
            return True
        try:
            CLAUDE_TRASH_DIR.mkdir(parents=True, exist_ok=True)
            dest = CLAUDE_TRASH_DIR / (path.stem + "-" + time.strftime("%Y%m%d-%H%M%S") + ".jsonl")
            os.replace(str(path), str(dest))
        except OSError as exc:
            raise ApiError({"code": "io", "message": str(exc)})
        _claude_meta_cache.pop(str(path), None)
        claude_sessions_map(force=True)
        return True

    def update_settings(self, tid: str, model=None, effort=None):
        with self.lock:
            st = self.settings.setdefault(tid, {})
            if model:
                st["model"] = model
            if effort:
                st["effort"] = effort
            self._save_json(CLAUDE_SETTINGS_FILE, self.settings)
        return st

    def send_turn(self, tid, text, images=None, model=None, effort=None):
        if not self.exe:
            raise ApiError({"code": "no_cli", "message": "本机未找到 claude.exe, Claude 引擎不可用"})
        with self.lock:
            if tid in self.turns:
                raise ApiError({"code": "busy", "message": "该会话正在执行中: 先点「停止」再发消息"})
            path = find_claude_session(tid)
            pend = self.pending.get(tid)
            if path is None and pend is None:
                raise ApiError({"code": "not_found", "message": "claude 会话不存在"})
            st = self.settings.setdefault(tid, {})
            if model:
                st["model"] = model
            if effort:
                st["effort"] = effort
            if model or effort:
                self._save_json(CLAUDE_SETTINGS_FILE, self.settings)
            model = st.get("model")
            effort = st.get("effort")
            cwd = (pend or {}).get("cwd") or ""
        if not cwd and path is not None:
            cwd = claude_session_meta(path).get("cwd") or ""
        if not cwd or not os.path.isdir(cwd):
            cwd = str(USER_HOME)
        cmd = [self.exe, "-p", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--input-format", "stream-json"]
        cmd += (["--resume", tid] if path is not None else ["--session-id", tid])
        if model:
            cmd += ["--model", str(model)]
        if effort:
            cmd += ["--effort", str(effort)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=flags)
        content = []
        if text:
            content.append({"type": "text", "text": text})
        for pth in (images or []):
            try:
                raw = Path(str(pth)).read_bytes()
            except OSError:
                continue
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".gif": "image/gif"}.get(Path(str(pth)).suffix.lower(), "image/png")
            content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                        "data": base64.b64encode(raw).decode("ascii")}})
        if not content:
            content.append({"type": "text", "text": "(空消息)"})
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        with self.lock:
            self.turns[tid] = proc
            self.pending.pop(tid, None)
        threading.Thread(target=self._stdin_writer, args=(proc, msg), daemon=True).start()
        threading.Thread(target=self._reader, args=(tid, proc), daemon=True).start()
        turn_id = "cl-" + uuid.uuid4().hex[:12]
        self.hub.broadcast({"kind": "notification", "method": "turn/started",
                            "params": {"threadId": tid, "turn": {"id": turn_id, "engine": "claude"}}})
        return {"turnId": turn_id, "cwd": cwd, "engine": "claude"}

    def interrupt(self, tid: str) -> bool:
        with self.lock:
            proc = self.turns.get(tid)
        if proc is None:
            return False
        try:
            proc.kill()
        except Exception:
            pass
        return True

    def _stdin_writer(self, proc, msg):
        try:
            data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            proc.stdin.write(data)
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass

    def _reader(self, tid, proc):
        started = time.time()
        finished = False
        emitted_text = False
        cur_msg = ""
        tool_meta = {}
        stderr_buf = []
        hub = self.hub

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
                        cur_msg = str((ev.get("message") or {}).get("id") or mid)
                        continue
                    if et == "content_block_delta":
                        idx = int(ev.get("index") or 0)
                        d = ev.get("delta") or {}
                        dt = d.get("type")
                        iid = "%s-%d" % (mid, idx)
                        if dt == "thinking_delta" and d.get("thinking"):
                            notif("item/reasoning/textDelta",
                                  {"threadId": tid, "itemId": iid, "delta": d["thinking"]})
                        elif dt == "text_delta" and d.get("text"):
                            emitted_text = True
                            notif("item/agentMessage/delta",
                                  {"threadId": tid, "itemId": iid, "delta": d["text"]})
                    continue
                if t == "assistant":
                    msg = obj.get("message") or {}
                    if not isinstance(msg, dict):
                        continue
                    mid = str(msg.get("id") or obj.get("uuid") or "")
                    for i, b in enumerate(msg.get("content") or []):
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        iid = "%s-%d" % (mid, i)
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
                    if not emitted_text and isinstance(txt, str) and txt.strip():
                        notif("item/completed", {"threadId": tid, "item": {
                            "id": "cres-%s" % str(obj.get("session_id") or "")[:8],
                            "type": "agentMessage", "text": txt}})
                    dur = obj.get("duration_ms") or int((time.time() - started) * 1000)
                    notif("turn/completed", {"threadId": tid, "turn": {
                        "id": "cl", "status": "completed", "durationMs": dur,
                        "costUsd": obj.get("total_cost_usd"), "engine": "claude"}})
                    continue
        except Exception as exc:
            log(f"claude reader {tid[:8]}: {exc}")
        finally:
            try:
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            with self.lock:
                self.turns.pop(tid, None)
            if not finished:
                err = "\n".join(stderr_buf[-12:]).strip()
                if err:
                    hub.broadcast({"kind": "notification", "method": "item/completed", "params": {
                        "threadId": tid, "item": {"id": "cerr-" + uuid.uuid4().hex[:8], "type": "agentMessage",
                                                   "text": "⚠️ Claude 进程结束(未产出结果):\n```\n" + _clip(err, 1500) + "\n```"}}})
                hub.broadcast({"kind": "notification", "method": "turn/completed",
                               "params": {"threadId": tid,
                                          "turn": {"id": "cl", "status": "aborted", "engine": "claude"}}})
