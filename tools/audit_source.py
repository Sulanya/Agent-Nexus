"""Review staged/source files for common private artifacts; never print secrets."""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ALLOWED_SUFFIXES={'.py','.cjs','.js','.html','.md','.json','.txt','.yml','.yaml','.ps1','.sh','.cmd','.command','.example','.svg'}
PATTERNS={
    'private-key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'provider-token':re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'credential-url':re.compile(r'https?://[^\s\x22\x27<>]+[?&](?:k|token|api_key)=[A-Za-z0-9_-]{16,}'),
    'private-network':re.compile(r'https?://[A-Za-z0-9.-]+\.trycloudflare\.com[?/#]'),
}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--staged',action='store_true');args=parser.parse_args()
    findings=[];scanned=0
    if args.staged:
        names=subprocess.check_output(['git','diff','--cached','--name-only','--diff-filter=ACMR','-z'],cwd=ROOT).decode().split('\0')
        files=[ROOT/name for name in names if name]
    else:
        files=[p for p in ROOT.rglob('*') if p.is_file() and not any(part in ('.git','.venv','venv','__pycache__','node_modules') for part in p.relative_to(ROOT).parts)]
    for path in files:
        rel=path.relative_to(ROOT).as_posix()
        if rel in ('docs/images/desktop.png','docs/images/tablet.png','docs/images/mobile.png','docs/images/overview.png'):
            data=subprocess.check_output(['git','show',':'+rel],cwd=ROOT) if args.staged else path.read_bytes()
            if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                findings.append({'path':rel,'rule':'invalid-demo-image'})
            scanned+=1
            continue
        if any(part in ('_run','.secrets','runtime','dist','_build') for part in Path(rel).parts) or any(x in path.name for x in ('.bak','.sqlite','.db','.log')) or path.name=='.env' or path.name=='an.config.json':
            findings.append({'path':rel,'rule':'private-runtime-artifact'});continue
        if path.suffix not in ALLOWED_SUFFIXES and path.name not in ('LICENSE','VERSION','.gitignore','.env.example','.gitattributes','.editorconfig'):
            findings.append({'path':rel,'rule':'unexpected-file-type'});continue
        # Staged mode scans the actual index blob, not an unstaged clean copy.
        data=subprocess.check_output(['git','show',':'+rel],cwd=ROOT) if args.staged else path.read_bytes()
        try:text=data.decode('utf-8-sig')
        except UnicodeDecodeError:
            findings.append({'path':rel,'rule':'non-utf8-source'});continue
        scanned+=1
        if rel=='tools/audit_source.py':continue
        for rule,pattern in PATTERNS.items():
            if pattern.search(text):findings.append({'path':rel,'rule':rule})
    print(json.dumps({'ok':not findings,'scanned':scanned,'findings':findings},ensure_ascii=False,indent=2))
    return 1 if findings else 0


if __name__=='__main__':sys.exit(main())
