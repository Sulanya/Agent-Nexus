"""Read engine-local model metadata without persisting or exposing credentials."""
import fnmatch
import copy
import json
import os
from pathlib import Path
import re
import time
import tomllib
import urllib.parse

_NATIVE_CATALOG_CACHE = {}


def user_home():
    return Path(os.environ.get("USERPROFILE") or Path.home())


def read_json(path):
    try:
        value = Path(path).read_text(encoding="utf-8-sig")
        return json.loads(value)
    except (OSError, ValueError):
        return {}


def version_for(paths):
    parts = []
    for path in paths:
        path = Path(path)
        try:
            stamp = path.stat()
            parts.append((str(path), stamp.st_mtime_ns, stamp.st_size))
        except OSError:
            parts.append((str(path), None, None))
    return tuple(parts)


def codex_config(path):
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    provider = str(data.get("model_provider") or "")
    section = (data.get("model_providers") or {}).get(provider) or {}
    catalog = str(data.get("model_catalog_json") or "")
    catalog_path = Path(catalog).expanduser() if catalog else None
    if catalog_path is not None and not catalog_path.is_absolute():
        catalog_path = path.parent / catalog_path
    return {"model": str(data.get("model") or ""),
            "effort": str(data.get("model_reasoning_effort") or ""),
            "provider": provider, "base_url": str(section.get("base_url") or "").rstrip("/"),
            "env_key": str(section.get("env_key") or ""),
            "catalog": str(catalog_path or ""), "path": str(path)}


def safe_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value))
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.split("@")[-1], parsed.path, "", ""))
    except ValueError:
        return ""


def model_rows(data, source):
    items = data.get("models", data.get("data", [])) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model = str(item.get("id") or item.get("slug") or item.get("model") or "").strip()
        if not model or item.get("hidden") is True:
            continue
        levels = item.get("supported_reasoning_levels") or item.get("supported_reasoning_efforts") or item.get("supportedReasoningEfforts") or item.get("efforts") or []
        efforts = [str(level.get("effort") or level.get("reasoningEffort") or "") if isinstance(level, dict) else str(level)
                   for level in levels if isinstance(level, (dict, str))]
        rows.append({"id": model, "label": str(item.get("display_name") or item.get("displayName") or item.get("name") or model),
                     "efforts": [effort for effort in efforts if effort], "source": source})
    return rows


def workbuddy_catalog(config_root, product):
    candidates = []
    paths = []
    storage_paths = list((config_root / "local_storage").glob("*.info"))
    signature = version_for(storage_paths + ([product] if product else []))
    cache_key = str(config_root)
    cached = _NATIVE_CATALOG_CACHE.get(cache_key)
    if cached and cached[0] == signature:
        return copy.deepcopy(cached[1]), list(cached[2])
    for path in storage_paths:
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
        except OSError:
            continue
        value = read_json(path)
        if not isinstance(value, list):
            continue
        for entry in value:
            if not isinstance(entry, dict):
                continue
            data = entry.get("data")
            if isinstance(data, dict) and isinstance(data.get("models"), list):
                try:
                    timestamp = float(entry.get("ts") or 0)
                except (TypeError, ValueError):
                    timestamp = 0.0
                candidates.append((timestamp, data, str(path)))
                paths.append(path)
    if candidates:
        _, catalog, source = max(candidates, key=lambda entry: entry[0])
    else:
        catalog, source = read_json(product) if product else {}, str(product or "")
    rows = model_rows(catalog, source)
    cli = next((agent for agent in catalog.get("agents", [])
                if isinstance(agent, dict) and agent.get("name") == "cli"), {}) if isinstance(catalog, dict) else {}
    allowed = cli.get("models") or []
    if allowed:
        rows = [row for row in rows if row["id"] in allowed]
    if len(_NATIVE_CATALOG_CACHE) >= 8:
        _NATIVE_CATALOG_CACHE.clear()
    _NATIVE_CATALOG_CACHE[cache_key] = (signature, copy.deepcopy(rows), list(paths))
    return rows, paths


def workbuddy_config(home=None, cli_dir=None, root=None):
    home = Path(home or user_home())
    roots = [Path(root)] if root else [home / ".workbuddy", home / ".codebuddy"]
    configured_root = os.environ.get("CODEBUDDY_CONFIG_DIR")
    if not root and configured_root:
        roots.insert(0, Path(configured_root))
    roots = list(dict.fromkeys(roots))
    product_path = Path(cli_dir) / "product.json" if cli_dir else None
    models = []
    catalog_paths = []
    for config_root in roots:
        native, native_paths = workbuddy_catalog(config_root, product_path)
        catalog_paths.extend(native_paths)
        if native and not models:
            models = native
    by_id = {model["id"]: model for model in models}
    custom_roots = {}
    sources = []
    default_model = default_effort = ""
    default_source = ""
    active_root = next((path for path in roots if path.is_dir()), roots[0])
    for config_root in roots:
        for name in ("settings.json", "user-state.json", "models.json"):
            path = config_root / name
            sources.append(path)
            data = read_json(path)
            if name == "models.json":
                for row in model_rows(data, str(path)):
                    model = row["id"]
                    row["id"] = model if model.startswith("custom-local:") else "custom-local:" + model
                    custom_roots.setdefault(row["id"], []).append(str(config_root))
                    if row["id"] not in by_id:
                        models.insert(0, row)
                        by_id[row["id"]] = row
                continue
            if not isinstance(data, dict):
                continue
            model = data.get("model") or data.get("selectedModel") or data.get("currentModel") or data.get("defaultModel")
            if isinstance(model, dict):
                model = model.get("id") or model.get("model")
            if not default_model and isinstance(model, str) and model.strip():
                default_model = model.strip()
                default_source = str(path)
                active_root = config_root
            if not default_effort:
                effort = data.get("effort") or data.get("reasoningEffort") or data.get("effortLevel")
                if isinstance(effort, str):
                    default_effort = effort
            settings = data.get("modelSettings") or {}
            if not default_effort and isinstance(settings, dict):
                selected = settings.get(default_model) or {}
                if isinstance(selected, dict):
                    default_effort = str(selected.get("effortLevel") or "")
    if default_model and "custom-local:" + default_model in by_id:
        default_model = "custom-local:" + default_model
    if default_model and default_model not in by_id:
        models.insert(0, {"id": default_model, "label": default_model, "efforts": [], "source": default_source})
    for row in models:
        row["configRoots"] = custom_roots.get(row["id"], [])
        if not row["efforts"] and default_effort and row["id"] == default_model:
            row["efforts"] = [default_effort]
    all_paths = sources + catalog_paths + ([product_path] if product_path else [])
    return {"models": models, "defaultModel": default_model, "defaultEffort": default_effort,
            "configRoot": str(active_root), "defaultSource": default_source,
            "sources": [str(path) for path in sources if path.is_file()],
            "configVersion": version_for(all_paths), "readAt": time.time()}


def proxy_diagnostic():
    enabled, server = False, ""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as registry:
                enabled = bool(winreg.QueryValueEx(registry, "ProxyEnable")[0])
                try:
                    server = str(winreg.QueryValueEx(registry, "ProxyServer")[0] or "")
                except OSError:
                    pass
        except OSError:
            pass
    environment_proxy = any(os.environ.get(name) for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"))
    if not enabled and not environment_proxy:
        return {"state": "ok", "sub": "未检测到已启用的代理", "badge": "直连", "verified": False}
    socks = "socks" in server.lower()
    return {"state": "warn", "badge": "待验证",
            "sub": ("检测到 SOCKS 系统代理，部分客户端可能不兼容；实际连通性尚未验证"
                    if socks else "代理已配置，实际连通性尚未验证；不影响本机自检"),
            "verified": False}
