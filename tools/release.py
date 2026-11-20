"""Build the two Marengo skill distributions from an exact, local Git commit."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
NAME = "marengo-market-data"
TITLE = "Marengo Market Data"
DESCRIPTION = "Market Data Skill для Marengo, БКС, Alpha Vantage и MOEX ISS."
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def encode(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifests(version):
    entry = {"name": NAME, "source": "./plugins/" + NAME, "description": DESCRIPTION, "version": version}
    codex_entry = {"name": NAME, "source": {"source":"local", "path":"./plugins/"+NAME},
                   "policy":{"installation":"AVAILABLE","authentication":"ON_INSTALL"}, "category":"Productivity"}
    common = {"name": NAME, "description": DESCRIPTION, "version": version, "author":{"name":"Marengo"}}
    claude = {"name":"marengo", "owner":{"name":"Marengo"}, "plugins":[entry]}
    openai = {"name":"marengo", "interface":{"displayName":"Marengo"}, "plugins":[codex_entry]}
    codex_root = dict(openai, plugins=[dict(codex_entry, source={"source":"local","path":"./chatgpt/plugins/"+NAME})])
    return {".claude-plugin/marketplace.json":encode(claude),
            ".agents/plugins/marketplace.json":encode(codex_root),
            "chatgpt/.agents/plugins/marketplace.json":encode(openai),
            f"plugins/{NAME}/.claude-plugin/plugin.json":encode(common),
            f"chatgpt/plugins/{NAME}/.codex-plugin/plugin.json":encode(dict(common,skills="./skills",
                interface={"displayName":TITLE,"shortDescription":DESCRIPTION,"developerName":"Marengo","category":"Productivity"}))}


EDITION = "editions/price-charts"
PAYLOAD = ("SKILL.md", "scripts/chart.py", "scripts/greeks.py", "references/alpha-vantage.md")


def build(source, ref):
    commit = git(source, "rev-parse", "--verify", "--end-of-options", ref+"^{commit}")
    payload = {}
    for name in PAYLOAD:
        path = f"{EDITION}/{name}"
        entry = git(source, "ls-tree", commit, "--", path)
        if not entry.startswith("100644 blob ") and not entry.startswith("100755 blob "):
            raise ValueError("Missing regular skill file: " + path)
        payload[name] = subprocess.check_output(["git", "-C", str(source), "show", f"{commit}:{path}"])
    match = re.search(r"^version: (\S+)$", payload["SKILL.md"].decode("utf-8"), re.M)
    if not match or not VERSION.fullmatch(match[1]):
        raise ValueError("Invalid skill version")
    version = match[1]
    result = manifests(version)
    for prefix in ("", "chatgpt/"):
        for name, content in payload.items():
            result[f"{prefix}plugins/{NAME}/skills/market-data/{name}"] = content
    return result, {"version":version,"source":"dqdp/market_data_skill","commit":commit,"edition":EDITION,
                    "sha256":{k:digest(v) for k,v in sorted(result.items())}}


def managed_paths():
    folders = ("plugins","chatgpt",".claude-plugin",".agents")
    return {p.relative_to(ROOT).as_posix() for f in folders for p in (ROOT/f).rglob("*") if p.is_file() or p.is_symlink()}


def verify():
    lock = json.loads((ROOT/"release.json").read_text(encoding="utf-8"))
    if managed_paths() != set(lock["sha256"]):
        raise ValueError("Catalog inventory differs from release.json")
    for name in managed_paths():
        p = ROOT/name
        if p.is_symlink() or digest(p.read_bytes()) != lock["sha256"][name]:
            raise ValueError("Content mismatch: "+name)
    for name,data in manifests(lock["version"]).items():
        if (ROOT/name).read_bytes() != data:
            raise ValueError("Manifest mismatch: "+name)
    return lock


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path)
    parser.add_argument("--ref",default="HEAD")
    args=parser.parse_args()
    if not args.source:
        print("Verified",verify()["version"])
        return
    old=verify() if (ROOT/"release.json").exists() else None
    files,lock=build(args.source.resolve(),args.ref)
    if old == lock:
        print("Already current",lock["version"])
        return
    if old and tuple(map(int,lock["version"].split("."))) <= tuple(map(int,old["version"].split("."))):
        raise ValueError("Release requires a higher SKILL.md version")
    if not old and managed_paths():
        raise ValueError("Unmanaged files in catalog")
    for name,data in files.items():
        path=ROOT/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        if path.suffix==".sh":
            path.chmod(0o755)
    for name in managed_paths()-set(files):
        (ROOT/name).unlink()
    (ROOT/"release.json").write_bytes(encode(lock))
    print("Built",verify()["version"])


if __name__ == "__main__":
    main()
