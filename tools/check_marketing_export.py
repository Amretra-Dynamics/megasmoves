"""Check the locally exported public site before GitHub Pages publication."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote
import json
import re

ROOT = Path(__file__).resolve().parents[1] / "marketing/out"
class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        for key,value in attrs:
            if key in {"href", "src"} and value: self.links.append(value)

def main():
    assert (ROOT / 'CNAME').read_text().strip() == 'megasmoves.io'
    assert (ROOT / '.nojekyll').is_file()
    required=['index.html','download/index.html','docs/index.html','wow/index.html',
              'terms/index.html','privacy/index.html','legal/index.html','install.ps1','install.sh']
    for name in required: assert (ROOT/name).is_file(), name
    broken=[]; checked=set(); pages=list(ROOT.rglob('*.html'))
    for page in pages:
        parser=Links();parser.feed(page.read_text(encoding='utf-8'))
        for href in parser.links:
            url=urlsplit(href)
            if url.scheme or url.netloc or not url.path:continue
            path=unquote(url.path)
            target=ROOT/path.lstrip('/') if path.startswith('/') else page.parent/path
            if target.is_dir():target/= 'index.html'
            if not target.exists(): broken.append((str(page.relative_to(ROOT)),href))
            checked.add(path)
    assert not broken, broken
    catalog=json.loads((ROOT/'downloads.json').read_text())
    assert catalog['scope']=='public-release' and catalog['channel']=='preview'
    for row in catalog['artifacts']:
        assert row['href'].startswith('https://github.com/Amretra-Dynamics/megasmoves/releases/download/')
        assert row['href'].endswith('/'+row['filename'])
        assert re.fullmatch(r'[a-f0-9]{64}',row['sha256'])
    package=json.loads((ROOT/'python-package.json').read_text())
    import hashlib
    wheel=ROOT/'downloads'/package['filename']
    assert wheel.stat().st_size == package['size_bytes']
    assert hashlib.sha256(wheel.read_bytes()).hexdigest()==package['sha256']
    for p in ROOT.rglob('*'):
        assert not p.is_symlink(), p
        if p.is_file(): assert p.stat().st_size < 100_000_000, p
    print(json.dumps({'html_pages':len(pages),'local_links_and_assets':len(checked),'broken_links':broken,'native_downloads':len(catalog['artifacts']),'python_wheel_verified':True}))

if __name__=='__main__':main()
