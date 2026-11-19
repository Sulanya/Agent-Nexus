"""Export the existing AN wordmark paths as a self-contained README SVG."""
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]


def main():
    source=(ROOT/'ui/index.html').read_text(encoding='utf-8')
    letters=re.search(r'<g class="wm-letters">(.*?)</g>',source)
    if not letters:raise RuntimeError('Original AN wordmark paths not found')
    svg='''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="144" viewBox="0 0 600 144" role="img" aria-labelledby="title desc">
<title id="title">Agent Nexus</title>
<desc id="desc">Original AN connection icon and outlined wordmark on a dark background.</desc>
<defs>
<linearGradient id="tile" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#7c3aed"/><stop offset=".48" stop-color="#6366f1"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient>
<linearGradient id="letters" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#f5f3ff"/><stop offset=".3" stop-color="#c4b5fd"/><stop offset=".58" stop-color="#a5b4fc"/><stop offset="1" stop-color="#7dd3fc"/></linearGradient>
<radialGradient id="shine" cx=".2" cy=".1" r="1"><stop stop-color="#fff" stop-opacity=".4"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
</defs>
<rect width="600" height="144" rx="20" fill="#0d1117"/>
<g transform="translate(30 26)"><rect width="92" height="92" rx="28" fill="url(#tile)"/><rect x="1" y="1" width="90" height="90" rx="27" fill="url(#shine)" stroke="#fff" stroke-opacity=".3"/>
<g transform="scale(3.833333)"><path d="M6.1 6.1C13.5 6.1 5.3 17.9 17.9 17.9" fill="none" stroke="white" stroke-width="1.95" stroke-linecap="round"/><circle cx="6.1" cy="6.1" r="2.02" fill="white"/><circle cx="17.9" cy="17.9" r="2.02" fill="white"/><circle cx="10.05" cy="12" r="1.42" fill="#38bdf8"/></g></g>
<text x="150" y="49" fill="#a5b4fc" font-family="Arial,sans-serif" font-size="16" font-weight="700" letter-spacing="10">AGENT</text>
<g transform="translate(150 54) scale(5.3 4)" fill="url(#letters)">'''+letters.group(1)+'''</g>
</svg>'''
    output=ROOT/'docs/images/logo.svg'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(svg+'\n',encoding='utf-8')
    print('Exported original AN brand artwork: docs/images/logo.svg')


if __name__=='__main__':main()
