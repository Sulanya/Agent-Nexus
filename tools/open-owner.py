"""Compatibility entry for optional tunnel recovery; use tools/dev.py for new code."""
from dev import config, connect


def refresh(open_browser=False):
    connect(config(),open_browser=open_browser)
    return {'ok':True,'localOwnerVerified':True,'opened':open_browser}


if __name__=='__main__':refresh(open_browser=True)
