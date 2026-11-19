# -*- coding: utf-8 -*-
"""Unlimited internal beta. Legacy trial and deletion commands are inert."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parent
ROOT_DIR = GATEWAY_DIR.parent
RUN_DIR = ROOT_DIR / "_run"
BUILD_SCOPE = "b" + hashlib.sha256(b"AgentNexus|internal").hexdigest()[:16]
TRIAL_FILES = ()
HOURS_DEFAULT = 0.0
GRACE_SECONDS = 0.0
SELF_DESTRUCT_ENABLED = False
_FP_CACHE = {"at": 0.0, "value": ("", False)}


def _machine_raw():
    if os.name == "nt":
        try:
            import winreg
            for view in (getattr(winreg, "KEY_WOW64_64KEY", 0), 0):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        r"SOFTWARE\Microsoft\Cryptography", 0,
                                        winreg.KEY_READ | view) as registry:
                        value, _ = winreg.QueryValueEx(registry, "MachineGuid")
                    value = str(value or "").strip().lower()
                    if value:
                        return "w1-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20], True
                except OSError:
                    continue
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                    capture_output=True, timeout=8)
            match = re.search(rb'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout or b"")
            if match:
                value = match.group(1).decode("ascii", "replace").strip().lower()
                if value:
                    return "m1-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20], True
        except Exception:
            pass
    try:
        import platform
        seed = "%s|%s|%s" % (platform.node(), uuid.getnode(), platform.machine())
        return "f1-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20], False
    except Exception:
        return "", False


def machine_fp():
    now = time.time()
    if _FP_CACHE["value"][0] and now - _FP_CACHE["at"] < 60:
        return _FP_CACHE["value"]
    value = _machine_raw()
    _FP_CACHE.update(at=now, value=value)
    return value


def read_state():
    return {"mode": "unlimited", "policy": "unlimited-beta", "id": "",
            "remain": None, "until": 0.0, "started": 0.0, "hours": 0.0,
            "totalHours": 0.0, "now": time.time(), "destructAt": None,
            "destructRemain": None, "graceSeconds": 0.0,
            "selfDestructEnabled": False, "dataDeletionEnabled": False}


def ensure(hours=HOURS_DEFAULT):
    return read_state()


def activate(hours=None):
    return read_state()


def _disabled_result(reason=""):
    return {"ok": True, "disabled": True, "reason": "self-destruct-disabled",
            "requestedReason": str(reason), "files": 0, "bytes": 0,
            "deleter": False, "selfDestructEnabled": False}


def redeem(code):
    return {"ok": True, "message": "当前内测版不限时，无需续期", "trial": read_state()}


def rebind_machine():
    return {"ok": True, "message": "当前内测版不绑定试用机器", "trial": read_state()}


def make_code(hours=2.0, trial_id="", until=None):
    return "当前内测版不限时，无需续期码"


def clock_files():
    return []


def clock_read():
    return None


def clock_write(at, mode=""):
    return None


def clock_clear():
    return _disabled_result()


def destruct_info():
    return None


def write_ended_note(reason="trial-expired"):
    return _disabled_result(reason)


def self_destruct(reason="trial-expired"):
    return _disabled_result(reason)


def scramble_tree(root, keep_names):
    return _disabled_result()


def _deleter_script(work, tag, root, keep_name, receipt_src, me_pid):
    return None


def spawn_deleter(root, keep_name, me_pid, receipt_text=""):
    return False, "self-destruct-disabled"


def destroy_now(reason="trial-expired", hard_exit=True):
    return _disabled_result(reason)


def arm_destroy():
    return _disabled_result()


class _Trial:
    def ensure(self, hours=HOURS_DEFAULT):
        return ensure(hours)

    def activate(self, hours=HOURS_DEFAULT):
        return activate(hours)

    def info(self):
        return read_state()

    def blocked(self):
        return False

    def block_msg(self):
        return "当前内测版不限时"

    def redeem(self, code):
        return redeem(code)

    def watch_start(self):
        return _disabled_result()

    def _watch_loop(self, state):
        return _disabled_result()


TRIAL = _Trial()


def _cli(argv):
    command = argv[0] if argv else "status"
    if command in ("status", "ensure", "activate", "expire-now", "destruct"):
        result = read_state()
    elif command in ("destroy-now", "arm-destroy", "selfdestruct"):
        result = _disabled_result(command)
    elif command in ("renew", "rebind"):
        result = redeem("")
    elif command == "make":
        result = {"ok": True, "message": make_code()}
    elif command == "fp":
        fingerprint, confident = machine_fp()
        result = {"mid": fingerprint, "confident": confident}
    else:
        result = {"ok": False, "error": "unknown command"}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
