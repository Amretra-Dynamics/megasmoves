"""Publish verified marketing/out into main/docs; never create deployment branches."""
from pathlib import Path
import argparse, json, shutil, subprocess
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"marketing"/"out"
DOCS=ROOT/"docs"
def cmd(*args):
    return subprocess.check_output(args,cwd=ROOT,text=True,stderr=subprocess.STDOUT).strip()
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--publish",action="store_true")
    args=p.parse_args()
    if cmd("git","branch","--show-current")!="main":
        raise SystemExit("Only main may be published")
    cmd("python",str(ROOT/"tools"/"check_marketing_export.py"))
    if cmd("git","status","--porcelain"):
        raise SystemExit("Commit source changes before publishing")
    cmd("git","fetch","origin","main")
    sha=cmd("git","rev-parse","HEAD")
    if sha!=cmd("git","rev-parse","origin/main"):
        raise SystemExit("main is not up to date")
    if not args.publish:
        print("Export valid; use --publish to commit checked files to main/docs")
        return
    if DOCS.is_symlink(): raise SystemExit("Unsafe docs symlink")
    if DOCS.exists():
        if not (DOCS/".nojekyll").is_file() or not (DOCS/"CNAME").is_file():
            raise SystemExit("docs contains non-site content")
        shutil.rmtree(DOCS)
    shutil.copytree(OUT,DOCS)
    (DOCS/"site-version.json").write_text(json.dumps({"source_commit":sha,"build":"main-docs-export"})+"\n",encoding="utf8")
    cmd("git","add","-A","--","docs")
    changes=cmd("git","diff","--cached","--name-only")
    if not changes:
        print("Already published");return
    if any(not name.startswith("docs/") for name in changes.splitlines()):
        raise SystemExit("Non-website path staged")
    cmd("git","diff","--cached","--check")
    cmd("git","commit","-m","Publish verified MegasMoves static website on main/docs")
    cmd("git","push","origin","main")
    print("PUBLISHED_MAIN_DOCS="+cmd("git","rev-parse","HEAD"))
if __name__=="__main__":main()
