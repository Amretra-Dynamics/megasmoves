"""Publish a checked local export; no hosted Node build or persistent runner.

Run from a clean source checkout after npm ci and npm run build in marketing.
GitHub authentication uses the existing Git credential manager. No tokens stored.
"""
from pathlib import Path
import argparse, shutil, subprocess, tempfile

REPO=Path(__file__).resolve().parents[1]
REMOTE='https://github.com/Amretra-Dynamics/megasmoves.git'
def run(*args,cwd=REPO):
    return subprocess.check_output(args,cwd=cwd,text=True,stderr=subprocess.STDOUT).strip()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--publish',action='store_true');args=parser.parse_args()
    run('python',str(REPO/'tools/check_marketing_export.py'))
    source=run('git','rev-parse','HEAD')
    if run('git','status','--porcelain','--','marketing','tools/check_marketing_export.py','tools/publish_pages_local.py'):
        raise SystemExit('Commit the marketing source and publishing tools before deploying.')
    if not args.publish:
        print('Export verified. Use --publish to push its exact bytes to gh-pages.');return
    with tempfile.TemporaryDirectory(prefix='megas-pages-publish-') as temporary:
        target=Path(temporary)
        run('git','init',cwd=target)
        run('git','config','core.autocrlf','false',cwd=target)
        run('git','remote','add','origin',REMOTE,cwd=target)
        existing=run('git','ls-remote','--heads','origin','gh-pages',cwd=target)
        if existing:
            run('git','fetch','--depth=1','origin','gh-pages',cwd=target)
            run('git','checkout','-b','gh-pages','FETCH_HEAD',cwd=target)
            for item in target.iterdir():
                if item.name=='.git':continue
                assert item.resolve().is_relative_to(target.resolve())
                if item.is_dir():shutil.rmtree(item)
                else:item.unlink()
        else:run('git','checkout','--orphan','gh-pages',cwd=target)
        shutil.copytree(REPO/'marketing/out',target,dirs_exist_ok=True)
        (target/'site-version.json').write_text('{"source_commit":"'+source+'","build":"local-static-export"}\n')
        run('git','config','user.name','Amretra Dynamics',cwd=target)
        run('git','config','user.email','abhinavmusrif@users.noreply.github.com',cwd=target)
        run('git','add','--all',cwd=target)
        run('git','commit','-m','Publish locally verified website '+source[:12],cwd=target)
        run('git','push','origin','gh-pages',cwd=target)
        print('Published gh-pages commit '+run('git','rev-parse','HEAD',cwd=target))

if __name__=='__main__':main()
