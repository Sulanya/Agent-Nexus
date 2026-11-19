"""Create a source ZIP from the reviewed SHA-256 manifest, excluding local state."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    manifest_path=ROOT/'docs/SOURCE_MANIFEST.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    version=(ROOT/'VERSION').read_text().strip()
    output=ROOT.parent/('AgentNexus-'+version+'-source.zip')
    if output.exists():raise RuntimeError('Source ZIP already exists; review and move it before rebuilding')
    entries=[]
    for entry in manifest['files']:
        path=(ROOT/entry['path']).resolve()
        if not path.is_relative_to(ROOT):raise RuntimeError('Manifest path escapes source directory')
        if not path.is_file():raise RuntimeError('Manifest file is missing: '+entry['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
            raise RuntimeError('Reviewed source changed: '+entry['path'])
        entries.append((path,entry['path']))
    entries.append((manifest_path,'docs/SOURCE_MANIFEST.json'))
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as bundle:
        for path,name in entries:
            info=zipfile.ZipInfo('AgentNexus/'+name,date_time=(2026,11,18,0,0,0))
            info.create_system=3
            info.external_attr=((0o100755 if path.suffix in ('.sh','.command') else 0o100644)<<16)
            info.compress_type=zipfile.ZIP_DEFLATED
            bundle.writestr(info,path.read_bytes())
    print(json.dumps({'ok':True,'files':len(entries),'archive':str(output),
                      'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__=='__main__':
    try:main()
    except Exception as exc:print(str(exc),file=sys.stderr);sys.exit(1)
