# -*- coding: utf-8 -*-
"""Compatibility API for releases without licensing or anti-analysis restrictions."""

import argparse
import json


def _status():
    return {
        "ok": True,
        "mode": "disabled",
        "enforcing": False,
        "tripped": False,
        "reason": "",
        "reason_text": "",
        "selfDestructEnabled": False,
        "dataDeletionEnabled": False,
    }


class Guard:
    def to_dict(self):
        return _status()

    def boot(self):
        return self

    def stop(self):
        return None

    def status(self):
        return _status()

    def unlock(self, code):
        return _status()

    def report(self, payload):
        return _status()

    def html(self):
        return ""


GUARD = Guard()
_G = GUARD


def gate(handler, path, query, method="GET"):
    return False


def note_body(path, body):
    return None


def boot():
    return GUARD


def status():
    return _status()


def html():
    return ""


def unlock(code):
    return GUARD.unlock(code)


def report(payload):
    return GUARD.report(payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="status")
    parser.add_argument("--code", default="")
    parser.parse_args()
    print(json.dumps(_status(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
