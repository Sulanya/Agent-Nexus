# -*- coding: utf-8 -*-
# Agent Nexus self-host package: print a QR code in the terminal.
#   python qrprint.py "<text>"
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
QR_LIB = os.path.join(HERE, "QR")
if QR_LIB not in sys.path:
    sys.path.insert(0, QR_LIB)


def main():
    link = sys.argv[1] if len(sys.argv) > 1 else ""
    if not link:
        print("usage: qrprint.py <text>")
        return 2
    try:
        import qrcode
    except Exception as exc:
        print("[qr] qrcode lib unavailable: %s" % exc)
        return 1
    try:
        q = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        q.add_data(link)
        q.make(fit=True)
        q.print_ascii(invert=True)
        return 0
    except Exception as exc:
        print("[qr] render failed: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
