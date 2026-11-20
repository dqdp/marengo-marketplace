"""Open or resume a catalog release PR using the operator's GitHub credentials."""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

CATALOG = "dqdp/marengo-marketplace"
SOURCE = "dqdp/market_data_skill"


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def propose(commit, base):
    catalog = base/"catalog"
    source = base/"source"
    run("git","clone","-q","--branch","main",f"git@github.com:{CATALOG}.git",str(catalog))
    if json.loads((catalog/"release.json").read_text())["commit"] == commit:
        return "Already published: " + commit
    run("git","clone","-q","--no-checkout",f"git@github.com:{SOURCE}.git",str(source))
    branch = "release/market-data-" + commit
    prs = json.loads(run("gh","pr","list","--repo",CATALOG,"--base","main","--head",branch,
                         "--state","open","--json","url"))
    if prs:
        return prs[0]["url"]
    run(sys.executable,str(catalog/"tools/release.py"),"--source",str(source),"--ref",commit,cwd=catalog)
    release = json.loads((catalog/"release.json").read_text())
    title = "Marengo Market Data " + release["version"]
    run("git","switch","-c",branch,cwd=catalog)
    run("git","add","--",".agents",".claude-plugin","chatgpt","plugins","release.json",cwd=catalog)
    if run("git","ls-remote","--heads","origin","refs/heads/"+branch,cwd=catalog):
        run("git","fetch","origin","refs/heads/"+branch,cwd=catalog)
        if run("git","write-tree",cwd=catalog) != run("git","rev-parse","FETCH_HEAD^{tree}",cwd=catalog):
            raise SystemExit("Existing release branch differs from the generated tree")
    else:
        run("git","commit","-m",title,cwd=catalog)
        run("git","push","origin","HEAD:refs/heads/"+branch,cwd=catalog)
    message=base/"body.md"
    message.write_text(f"Обновление скилла из [коммита источника](https://github.com/{SOURCE}/commit/{commit}).\n"
                       "Обе поставки собраны из одной версии. После слияния обновите каталог и проверьте версию в клиенте.\n")
    return run("gh","pr","create","--repo",CATALOG,"--base","main","--head",branch,"--title",title,
               "--body-file",str(message),cwd=catalog)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="Full 40-character Git SHA from the private skill repository")
    args=parser.parse_args()
    if not re.fullmatch("[0-9a-f]{40}",args.commit):
        parser.error("Supply the full source SHA")
    with TemporaryDirectory() as tmp:
        print(propose(args.commit,Path(tmp)))
